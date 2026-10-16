"""The links to the server: commands, live view and status.

* Commands (wss://ws.<server>/): the server calls functions by name
  (utils/handlers.function_map) and waits about 3 s for each reply, which
  it matches by id. Commands run in lanes, each in arrival order, so a
  photo doesn't hold up a slew and nothing holds up a stop:
    priority  stops (handlers.PRIORITY_FUNCTIONS): run at once
    query     reads (get..., esp status): run at once
    camera    photos and camera settings, one at a time
    motion    go-tos and motor commands, one at a time
  Every reply carries the request's id, errors included.
* Live view (wss://liveview.<server>/): connected only while live view is
  on (and for a minute after). Frames come from core/camera/stream.py,
  which owns the policy (size, rate, link adaptation); the camera itself is
  never touched from here, so a network problem never restarts it.
* Status: with server.status_push_s > 0, a {"type": "status", ...} message
  (utils/client_status.py) that often. The ASTRA server only logs messages
  without an id, so this is off by default.

Reconnects wait 2 s, then longer each time up to a minute, with jitter,
and start from 2 s again after a connection that lasted. Nothing here
blocks the event loop: slow work runs in threads.
"""
import asyncio
import os
import random
import signal
import sys
import time
from typing import Any, Dict, Optional

import ujson as json  # Using ujson for faster serialization
import websockets

from utils.config_state import build_service_urls, get_client_config, get_section, is_offline_mode, save_client_config
from utils.esp32_state import esp32_scanner_task
from utils.LEDmanager import get_led_manager
from utils.log import get_logger

log = get_logger("server")

# Module-level placeholders (populated from the config passed into websocketClient)
CLIENT_ID = None
SERVER_URI = None
LIVEVIEW_URI = None
SERVER_HTTP_URL = None
API_TOKEN = None

# Server link state for the front panel: "starting", "connecting",
# "connected", "unreachable", "auth failed", "disconnected", "offline"
# (offline mode) or "not set up".
server_state = {"state": "starting", "detail": ""}
_loop = None
_open_sockets: set = set()
_command_ws = None  # the command connection, while it's open

LIVEVIEW_IDLE_CLOSE_S = 60.0
FRAME_SEND_TIMEOUT_S = 20.0
QUERY_PREFIXES = ("get", "espStatus", "espInfo", "espEstopStatus", "espDriverStatus", "espPanelStatus",
    "espImuRead", "echo", "liveViewViewers", "unpark", "setClock", "setHorizon", "setTrackingRate")
CAMERA_FUNCTIONS = {"getCameraChoices", "setCameraSetting", "capturePhoto", "setCameraZoom", "liveViewSnapshot",
    "startPhotoSequence", "startRecording", "startLiveView", "setLiveViewOptions", "setLiveViewQuality",
    "setLiveViewFocus", "uploadPending", "retryFailedUploads", "uploadOfflinePhotos"}


def _set_server_state(state: str, detail: str = "") -> None:
    server_state["state"] = state
    server_state["detail"] = detail


def apply_client_config(cfg: dict) -> None:
    # Use new settings (from the front panel) without restarting: update the
    # globals the connect loops read, then drop the open connections so they
    # reconnect with them.
    global CLIENT_ID, SERVER_URI, LIVEVIEW_URI, SERVER_HTTP_URL, API_TOKEN
    CLIENT_ID = cfg.get("client_id") or CLIENT_ID
    API_TOKEN = cfg.get("api_token") or API_TOKEN
    if cfg.get("base_url"):
        urls = build_service_urls(cfg["base_url"])
        SERVER_HTTP_URL = urls["http_url"]
        SERVER_URI = urls["server_uri"]
        LIVEVIEW_URI = urls["liveview_uri"]
    request_reconnect()


def server_enabled() -> bool:
    # False in offline mode or before the server settings are known; the
    # connect loops wait instead of connecting.
    if is_offline_mode():
        return False
    return bool(SERVER_URI and LIVEVIEW_URI and API_TOKEN and CLIENT_ID)


async def _wait_for_server_enabled() -> None:
    while not server_enabled():
        _set_server_state("offline" if is_offline_mode() else "not set up")
        await asyncio.sleep(1.0)


def request_reconnect() -> None:
    loop = _loop
    if loop is None:
        return
    for ws in list(_open_sockets):
        try:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)
        except Exception:
            pass


class ReconnectBackoff:
    """2 s, 4 s, 8 s ... up to a minute, each with up to 25% jitter (so a
    server coming back isn't hit by every telescope at once); back to 2 s
    after a connection that lasted LASTED_S."""

    FIRST_S = 2.0
    MOST_S = 60.0
    LASTED_S = 60.0

    def __init__(self) -> None:
        self.delay = 0.0

    def after(self, lasted_s: float) -> float:
        if lasted_s >= self.LASTED_S:
            self.delay = 0.0
        self.delay = self.FIRST_S if self.delay <= 0 else min(self.MOST_S, self.delay * 2)
        return self.delay * (1.0 + random.uniform(0.0, 0.25))


def load_config(allow_missing: bool = False):
    # Load client configuration from static state.
    try:
        return get_client_config()
    except Exception:
        if allow_missing:
            return {}
        raise


def save_config(config):
    try:
        save_client_config(config)
        print("[config] Configuration saved to config/client_profile.json")
    except Exception as e:
        print(f"[config] Error saving config: {e}")


async def _authenticate(ws) -> None:
    if not API_TOKEN:
        raise RuntimeError("No API token configured")
    await ws.send(json.dumps({"token": API_TOKEN, "client_id": CLIENT_ID}))


# --- commands -----------------------------------------------------------------------------

def lane_for(function_name: str) -> str:
    from utils.handlers import PRIORITY_FUNCTIONS

    if function_name in PRIORITY_FUNCTIONS:
        return "priority"
    if function_name in CAMERA_FUNCTIONS:
        return "camera"
    if function_name.startswith(QUERY_PREFIXES):
        return "query"
    return "motion"


def _describe_call(function_name: str, args, kwargs) -> str:
    def short(value: Any) -> str:
        text = repr(value)
        return text if len(text) <= 60 else text[:57] + "..."
    parts = [short(a) for a in args] + [f"{k}={short(v)}" for k, v in kwargs.items()]
    return f"{function_name}({', '.join(parts)})"


async def _execute(ws, data: Dict[str, Any]) -> None:
    from utils.handlers import function_map

    request_id = data.get("id")
    function_name = data.get("function")
    func = function_map.get(function_name)
    if func is None:
        reply = {"error": f"Function '{function_name}' not found", "id": request_id}
    else:
        args = data.get("args", []) or []
        kwargs = data.get("kwargs", {}) or {}
        if "motor_id" in data:
            kwargs["motor_id"] = data["motor_id"]
        log.info("Calling %s", _describe_call(function_name, args, kwargs))
        started = time.monotonic()
        try:
            # Off the event loop, so frames and pings carry on.
            result = await asyncio.to_thread(func, *args, **kwargs)
            reply = {"result": result, "id": request_id}
        except Exception as exc:
            log.warning("%s failed: %s", function_name, exc)
            reply = {"error": str(exc), "id": request_id}
        took = time.monotonic() - started
        if took > 3.0:
            log.info("%s took %.1f s (the server stops waiting after about 3 s)", function_name, took)
    try:
        await ws.send(json.dumps(reply))
    except Exception as exc:
        log.info("Couldn't send the reply to %s: %s", function_name, exc)


async def _lane_worker(ws, lane: "asyncio.Queue") -> None:
    while True:
        data = await lane.get()
        try:
            await _execute(ws, data)
        finally:
            lane.task_done()


async def handle_server(ws):
    """Read commands and run them in their lanes until the socket closes."""
    await _authenticate(ws)
    log.info("Authenticated as %s", CLIENT_ID)
    lanes = {"camera": asyncio.Queue(), "motion": asyncio.Queue()}
    workers = [asyncio.create_task(_lane_worker(ws, q)) for q in lanes.values()]
    running: set = set()
    try:
        async for message in ws:
            try:
                data = json.loads(message)
            except ValueError:
                log.warning("Unreadable message from the server: %.80s", message)
                continue
            if not isinstance(data, dict):
                continue
            lane = lane_for(str(data.get("function", "")))
            if lane in lanes:
                lanes[lane].put_nowait(data)
            else:
                task = asyncio.create_task(_execute(ws, data))
                running.add(task)
                task.add_done_callback(running.discard)
    finally:
        for task in workers + list(running):
            task.cancel()


async def run_client():
    # Run the command connection with automatic reconnection.
    global _command_ws
    backoff = ReconnectBackoff()
    while True:
        await _wait_for_server_enabled()
        connection_start_time = time.monotonic()
        ws = None
        try:
            log.info("Connecting to %s", SERVER_URI)
            _set_server_state("connecting", SERVER_URI or "")
            ws = await websockets.connect(SERVER_URI, ping_interval=20, ping_timeout=20)
            _open_sockets.add(ws)
            _command_ws = ws
            _set_server_state("connected", SERVER_URI or "")
            log.info("Connected to %s", SERVER_URI)
            await handle_server(ws)
            _set_server_state("disconnected", "closed by the server")
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            lowered = error_msg.lower()
            if "4001" in error_msg or "authentication" in lowered or "unauthorized" in lowered or "403" in error_msg:
                _set_server_state("auth failed", error_msg[:60])
            elif "ConnectionClosed" in error_type:
                _set_server_state("disconnected", error_msg[:60])
            else:
                _set_server_state("unreachable", f"{error_type}: {error_msg}"[:60])
            log.info("Command link: %s %s", error_type, error_msg[:120])
        finally:
            _open_sockets.discard(ws)
            _command_ws = None
            if ws is not None and ws.close_code is None:
                try:
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                except Exception:
                    pass
        lasted = time.monotonic() - connection_start_time
        if server_enabled():
            delay = backoff.after(lasted)
            log.info("Command link lasted %.0f s; reconnecting in %.0f s", lasted, delay)
            await asyncio.sleep(delay)


# --- live view ------------------------------------------------------------------------------

async def send_frames():
    """Send live view frames while live view is on (core/camera/stream.py)."""
    from core.camera.stream import get_live_stream

    stream = get_live_stream()
    backoff = ReconnectBackoff()
    while True:
        await _wait_for_server_enabled()
        if not stream.enabled:
            await asyncio.sleep(0.5)
            continue
        started = time.monotonic()
        ws = None
        try:
            log.info("Live view: connecting to %s", LIVEVIEW_URI)
            ws = await websockets.connect(LIVEVIEW_URI, max_size=2 * 1024 * 1024, ping_interval=20, ping_timeout=30)
            _open_sockets.add(ws)
            await _authenticate(ws)
            log.info("Live view: connected")
            idle_since: Optional[float] = None
            while server_enabled() and ws.close_code is None:
                if not stream.enabled:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > LIVEVIEW_IDLE_CLOSE_S:
                        break
                    await asyncio.sleep(0.5)
                    continue
                idle_since = None
                frame = await asyncio.to_thread(stream.next_frame, 1.0)
                if frame is None or ws.close_code is not None:
                    continue
                t0 = time.monotonic()
                await asyncio.wait_for(ws.send(frame), timeout=FRAME_SEND_TIMEOUT_S)
                stream.note_sent(len(frame), time.monotonic() - t0)
        except Exception as e:
            log.info("Live view link: %s %s", type(e).__name__, str(e)[:120])
        finally:
            _open_sockets.discard(ws)
            if ws is not None and ws.close_code is None:
                try:
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                except Exception:
                    pass
        lasted = time.monotonic() - started
        if server_enabled() and stream.enabled:
            delay = backoff.after(lasted)
            log.info("Live view link lasted %.0f s; reconnecting in %.0f s", lasted, delay)
            await asyncio.sleep(delay)


# --- status --------------------------------------------------------------------------------

async def status_push_task():
    """{"type": "status", ...} on the command link every
    server.status_push_s seconds (0: off). At least 15 s apart: the server
    allows 120 messages a minute per connection."""
    from utils.client_status import collect

    while True:
        try:
            period = float(get_section("server").get("status_push_s", 0) or 0)
        except Exception:
            period = 0.0
        if period <= 0:
            await asyncio.sleep(10.0)
            continue
        await asyncio.sleep(max(15.0, period))
        ws = _command_ws
        if ws is None or ws.close_code is not None:
            continue
        try:
            status = await asyncio.to_thread(collect)
            await ws.send(json.dumps(status))
        except Exception as exc:
            log.debug("Status push failed: %s", exc)


# --- start and stop ----------------------------------------------------------------------

def start_services() -> None:
    """The background parts that run with or without a server."""
    from core.camera.service import get_camera_service
    from core.camera.stream import get_live_stream
    from utils.uploads import get_upload_queue

    get_camera_service().start()
    get_upload_queue().start()

    def auto_stopped(message: str) -> None:
        try:
            from utils.panel_ui import get_panel_ui

            get_panel_ui().toast(message[:60], 5000)
        except Exception:
            pass
    get_live_stream().on_auto_stop = auto_stopped
    try:
        from core.networking import local_page

        local_page.start_if_enabled()
    except Exception as exc:
        log.warning("Local page not started: %s", exc)
    try:
        from utils import gps

        gps.start()
    except Exception as exc:
        log.warning("GPS reader not started: %s", exc)


def cleanup_camera():
    # Turn live view off and let the camera go (the service closes it).
    try:
        from utils.liveview_state import save_liveview_state

        save_liveview_state(False)
    except Exception as e:
        print(f"[cleanup] Live view: {e}")
    try:
        from core.camera.service import get_camera_service

        get_camera_service().stop(timeout=5.0)
    except Exception as e:
        print(f"[cleanup] Camera: {e}")


def shutdown() -> None:
    """Leave things tidy: state written, camera released, LED off."""
    try:
        from utils.telescope_state import flush

        flush()
    except Exception:
        pass
    try:
        get_led_manager().shutdown()
    except Exception:
        pass
    cleanup_camera()


def handle_exit(signum, frame):
    print(f"[signal] Received signal {signum}, exiting and releasing camera...")
    shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def setup_client_config():
    # Interactive setup for client configuration
    print("=== Telescope Client Configuration Setup ===")
    print()
    current_config = load_config(allow_missing=True)

    print(f"Current client ID: {current_config.get('client_id', 'Not set')}")
    new_client_id = input("Enter client ID (press Enter to keep current): ").strip()
    if new_client_id:
        current_config['client_id'] = new_client_id

    print(f"Current base URL: {current_config.get('base_url', 'Not set')}")
    new_base_url = input("Enter base URL (press Enter to keep current): ").strip()
    if new_base_url:
        current_config['base_url'] = new_base_url

    print(f"Current API token: {'***set***' if current_config.get('api_token') else 'Not set'}")
    new_token = input("Enter API token (press Enter to keep current): ").strip()
    if new_token:
        current_config['api_token'] = new_token

    save_config(current_config)
    print("\nConfiguration saved! You can now run the client.")
    return current_config


async def websocketClient(cfg: dict = None):
    # Run the server links and the background tasks until stopped.
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_client_config()
        return
    if cfg is None:
        cfg = load_config()

    # Offline mode runs without these; the connect loops wait until they're set.
    required_keys = ["client_id", "base_url", "api_token"]
    missing = [k for k in required_keys if not isinstance(cfg.get(k), str) or not cfg.get(k).strip()]
    if missing and not is_offline_mode():
        raise KeyError(f"Missing required values in config/client_profile.json: {', '.join(missing)}. "
            "Run 'python Client.py setup' to create or fix the config.")

    global CLIENT_ID, SERVER_URI, LIVEVIEW_URI, SERVER_HTTP_URL, API_TOKEN, _loop
    _loop = asyncio.get_running_loop()
    CLIENT_ID = cfg.get("client_id") or None
    API_TOKEN = cfg.get("api_token") or None
    if cfg.get("base_url"):
        urls = build_service_urls(cfg["base_url"])
        SERVER_HTTP_URL = urls["http_url"]
        SERVER_URI = urls["server_uri"]
        LIVEVIEW_URI = urls["liveview_uri"]
    if is_offline_mode():
        log.info("Offline mode: not connecting to the server (turn it off in the front panel's Setup tab)")
    leds = get_led_manager()
    await asyncio.to_thread(leds.set_ui_connected, True)

    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    start_services()
    try:
        await asyncio.gather(
            run_client(),
            send_frames(),
            esp32_scanner_task(check_interval=2.0),
            status_push_task(),
        )
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received, exiting and releasing camera...")
    except Exception as e:
        log.exception("Unexpected exception: %s", e)
    finally:
        shutdown()
