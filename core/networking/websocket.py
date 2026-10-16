import asyncio
import os
import websockets
import ujson as json  # Using ujson for faster serialization
import time
import subprocess
import sys
import signal
from datetime import datetime
from utils.handlers import function_map
from utils.liveview_state import is_liveview_enabled, save_liveview_state
from utils.camera_state import camera_state, camera_scanner_task
from utils.esp32_state import esp32_state, esp32_scanner_task


async def peripheral_refresh_task(led_manager, refresh_interval: float = 1.0):
    # Keep the task alive without polling the ESP32 on a fixed interval.
    # The scanner tasks already manage reconnection, and repeated refresh
    # probes were creating serial contention with the LED commands.
    print(f"[peripheral_refresh] Started peripheral refresh task (no polling, interval {refresh_interval}s)")
    while True:
        await asyncio.sleep(refresh_interval)
from utils.config_state import get_client_config, save_client_config, build_service_urls
from utils.LEDmanager import get_led_manager


# Configuration values are provided via `config/client_profile.json` at runtime.
# No hard-coded defaults are kept here.

# Module-level placeholders (populated from the config passed into websocketClient)
CLIENT_ID = None
SERVER_URI = None
LIVEVIEW_URI = None
SERVER_HTTP_URL = None

# Server link state for the front panel: "starting", "connecting",
# "connected", "unreachable", "auth failed" or "disconnected".
server_state = {"state": "starting", "detail": ""}
_loop = None
_open_sockets: set = set()


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


def request_reconnect() -> None:
    loop = _loop
    if loop is None:
        return
    for ws in list(_open_sockets):
        try:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)
        except Exception:
            pass

def load_config(allow_missing: bool = False):
    # Load client configuration from static state.
    try:
        return get_client_config()
    except Exception:
        if allow_missing:
            return {}
        raise

def save_config(config):
    # Save client configuration to static state.
    try:
        save_client_config(config)
        print(f"[config] Configuration saved to config/client_profile.json")
    except Exception as e:
        print(f"[config] Error saving config: {e}")

# Note: configuration is now loaded at runtime inside `websocketClient()` so
# that the `setup` command can create the config file when it doesn't exist.
# There are no hard-coded defaults — the values must come from
# `config/client_profile.json`.
API_TOKEN = None

async def authenticate_with_server(ws):
    # Send authentication message to server
    if not API_TOKEN:
        print("[auth] ERROR: No API token configured!")
        print("[auth] Please set your API token in config/client_profile.json")
        print("[auth] Example config:")
        print(json.dumps({
            "client_id": CLIENT_ID,
            "base_url": SERVER_HTTP_URL,
            "api_token": "your-token-here"
        }, indent=2))
        raise Exception("No API token configured")
    
    auth_message = {
        "token": API_TOKEN,
        "client_id": CLIENT_ID
    }
    
    await ws.send(json.dumps(auth_message))
    print(f"[auth] Sent authentication for client: {CLIENT_ID}")

async def handle_server(ws):
    # Handle incoming WebSocket messages and execute mapped functions
    # Send authentication as first message
    await authenticate_with_server(ws)
    
    last_message_time = time.time()
    
    try:
        async for message in ws:
            try:
                last_message_time = time.time()
                data = json.loads(message)
                print(f"[received] Command: {json.dumps(data)}")
                function_name = data.get("function")

                if function_name in function_map:
                    func = function_map[function_name]
                    args = data.get("args", [])
                    kwargs = data.get("kwargs", {})
                    
                    # Enhanced handling for ESP32 commands with motor_id
                    # If motor_id is in the top-level data, add it to kwargs
                    if "motor_id" in data:
                        kwargs["motor_id"] = data["motor_id"]
                    
                    # Log the function call
                    args_str = ", ".join(repr(arg) for arg in args) if args else ""
                    kwargs_str = ", ".join(f"{k}={repr(v)}" for k, v in kwargs.items()) if kwargs else ""
                    all_args = ", ".join(filter(None, [args_str, kwargs_str]))
                    print(f"[function_call] Calling {function_name}({all_args})")
                    
                    # Run handlers off the event loop so frame streaming and ping/pong
                    # are not blocked by long camera or motor operations.
                    if kwargs:
                        result = await asyncio.to_thread(func, *args, **kwargs)
                    else:
                        result = await asyncio.to_thread(func, *args)
                    
                    response = json.dumps({"result": result, "id": data.get("id")})
                else:
                    response = json.dumps({"error": f"Function '{function_name}' not found", "id": data.get("id")})

                await ws.send(response)
            except Exception as e:
                error_response = json.dumps({"error": str(e)})
                try:
                    await ws.send(error_response)
                except:
                    print(f"[handle_server] Failed to send error response: {e}")
                    raise
    except Exception as e:
        error_type = type(e).__name__
        connection_duration = time.time() - last_message_time
        print(f"[handle_server] Connection ended at {datetime.now()}: {error_type} - {str(e)}")
        print(f"[handle_server] Connection was active for {connection_duration:.1f} seconds since last message")
        raise

async def run_client():
    # Run the main WebSocket client with automatic reconnection
    
    while True:  # Outer reconnection loop
        connection_start_time = time.time()
        ws = None
        try:
            print(f"[run_client] Connecting to {SERVER_URI} at {datetime.now()}...")
            _set_server_state("connecting", SERVER_URI or "")
            ws = await websockets.connect(SERVER_URI, ping_interval=20, ping_timeout=10)
            _open_sockets.add(ws)
            print(f"[{CLIENT_ID}] Connected to {SERVER_URI} at {datetime.now()}")
            _set_server_state("connected", SERVER_URI or "")
            await handle_server(ws)
            _set_server_state("disconnected", "closed by the server")
                
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            connection_duration = time.time() - connection_start_time
            
            # Provide more specific error information but suppress verbose details
            if 'authentication' in error_msg.lower() or 'unauthorized' in error_msg.lower() or '401' in error_msg or '403' in error_msg:
                _set_server_state("auth failed", error_msg[:60])
            elif 'ConnectionClosed' in error_type:
                _set_server_state("disconnected", error_msg[:60])
            else:
                _set_server_state("unreachable", f"{error_type}: {error_msg}"[:60])
            if 'ConnectionClosed' in error_type or 'ConnectionClosedError' in error_type:
                print(f"[run_client] WebSocket connection closed at {datetime.now()}")
            elif 'ConnectionRefused' in error_type:
                print(f"[run_client] Server appears to be down or unreachable at {datetime.now()}")
            elif 'timeout' in error_msg.lower():
                print(f"[run_client] Connection timeout at {datetime.now()}")
            elif 'authentication' in error_msg.lower() or 'unauthorized' in error_msg.lower():
                print(f"[run_client] Authentication failed at {datetime.now()}")
            elif 'ssl' in error_msg.lower() or 'certificate' in error_msg.lower():
                print(f"[run_client] SSL/TLS error at {datetime.now()}")
            elif 'network' in error_msg.lower() or 'unreachable' in error_msg.lower():
                print(f"[run_client] Network error at {datetime.now()}")
            else:
                print(f"[run_client] Connection lost at {datetime.now()}: {error_type}")
            
            print(f"[run_client] Connection lasted {connection_duration:.1f} seconds")
            print(f"[run_client] Reconnecting in 5 seconds...")
            
        finally:
            # Ensure proper cleanup
            _open_sockets.discard(ws)
            if ws and ws.close_code is None:
                try:
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                except:
                    pass
        
        # Wait before reconnecting
        await asyncio.sleep(5)

async def authenticate_liveview(ws):
    # Send authentication message to liveview server
    if not API_TOKEN:
        raise Exception("No API token configured for liveview")
    
    auth_message = {
        "token": API_TOKEN,
        "client_id": CLIENT_ID
    }
    
    await ws.send(json.dumps(auth_message))
    print(f"[liveview] Sent authentication for client: {CLIENT_ID}")

async def send_frames():
    # Send live camera frames via WebSocket with automatic reconnection
    import fcntl
    import os as os_module
    from core.camera.controller import Camera
    
    JPEG_START = b'\xff\xd8'
    JPEG_END = b'\xff\xd9'
    proc = None
    last_frame_time = 0
    # Allow overriding FPS via env var LIVEVIEW_FPS, but default to 10 (unchanged)
    try:
        fps_env = int(os.environ.get("LIVEVIEW_FPS", "10"))
        frame_interval = 1.0 / fps_env if fps_env > 0 else 1.0 / 10
    except Exception:
        frame_interval = 1 / 10  # 10 FPS

    last_process_check_time = 0
    process_check_interval = 1.0  # Check process health every 1 second
    # Buffer and memory safeguards to protect low-RAM devices.
    # Keep memory bounded by retaining only the newest candidate frame data.
    MAX_BUFFER_SIZE = int(os.environ.get("LIVEVIEW_MAX_BUFFER", 4 * 1024 * 1024))
    MAX_FRAME_SIZE = int(os.environ.get("LIVEVIEW_MAX_FRAME", 2 * 1024 * 1024))
    DROP_MEMORY_THRESHOLD = int(os.environ.get("LIVEVIEW_MEM_DROP", 120 * 1024 * 1024))
    PAUSE_MEMORY_THRESHOLD = int(os.environ.get("LIVEVIEW_MEM_PAUSE", 80 * 1024 * 1024))
    RESTART_MEMORY_THRESHOLD = int(os.environ.get("LIVEVIEW_MEM_RESTART", 50 * 1024 * 1024))
    consecutive_failures = 0  # Track consecutive camera start failures
    process_start_time = 0  # Track when process was started
    pending_frame = None
    low_memory_drop_toggle = False

    def _read_proc_stderr_text(p):
        # Best-effort stderr extraction for terminated gphoto2 processes.
        if not p or p.stderr is None:
            return ""
        try:
            data = p.stderr.read()
            if not data:
                return ""
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace").strip()
            return str(data).strip()
        except Exception:
            return ""

    # Try to import psutil locally (optional dependency) for memory checks
    try:
        import psutil
        _psutil_available = True
    except Exception:
        psutil = None
        _psutil_available = False

    def _available_memory():
        if not _psutil_available:
            return None
        try:
            return psutil.virtual_memory().available
        except Exception:
            return None

    def _trim_buffer(buffer: bytes) -> bytes:
        if len(buffer) <= MAX_BUFFER_SIZE:
            return buffer

        latest_start = buffer.rfind(JPEG_START)
        if latest_start != -1:
            trimmed = buffer[latest_start:]
            if len(trimmed) <= MAX_BUFFER_SIZE:
                print(f"[send_frames] Buffer exceeded {MAX_BUFFER_SIZE} bytes; trimming to latest frame candidate")
                return trimmed

        print(f"[send_frames] Buffer exceeded {MAX_BUFFER_SIZE} bytes; keeping newest tail")
        return buffer[-MAX_BUFFER_SIZE:]

    def _extract_latest_frame(buffer: bytes):
        latest_frame = None

        while True:
            start = buffer.find(JPEG_START)
            if start == -1:
                return latest_frame, buffer[-MAX_BUFFER_SIZE:]

            if start > 0:
                buffer = buffer[start:]
                start = 0

            end = buffer.find(JPEG_END, start + 2)
            if end == -1:
                return latest_frame, _trim_buffer(buffer)

            jpeg = buffer[start:end + 2]
            buffer = buffer[end + 2:]

            if len(jpeg) > MAX_FRAME_SIZE:
                print(f"[send_frames] Dropping oversized frame ({len(jpeg)} bytes > {MAX_FRAME_SIZE})")
                continue

            latest_frame = jpeg

    def _cleanup_usb_camera_lockers() -> None:
        # Best-effort cleanup of processes that commonly lock camera USB endpoints.
        locker_patterns = [
            "gvfs-gphoto2-volume-monitor",
            "gvfsd-gphoto2",
            "gphoto2",
        ]
        for pattern in locker_patterns:
            try:
                subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=2)
            except Exception:
                pass

    while True:  # Outer reconnection loop
        connection_start_time = time.time()
        ws = None
        try:
            print(f"[send_frames] Connecting to {LIVEVIEW_URI} at {datetime.now()}...")
            ws = await websockets.connect(LIVEVIEW_URI, max_size=2*1024*1024, ping_interval=20, ping_timeout=10)
            _open_sockets.add(ws)
            print(f"[send_frames] Connected successfully at {datetime.now()}")
            # Send authentication as first message
            await authenticate_liveview(ws)
            
            while True:
                if not is_liveview_enabled():
                    await asyncio.sleep(0.2)
                    continue
                
                # Check if WebSocket is still open
                if ws.close_code is not None:
                    connection_duration = time.time() - connection_start_time
                    print(f"[send_frames] WebSocket closed silently at {datetime.now()}")
                    print(f"[send_frames] Connection lasted {connection_duration:.1f} seconds")
                    print(f"[send_frames] Close code: {ws.close_code}")
                    break
                
                # Start camera capture process
                if proc is None or proc.poll() is not None:
                    # Check if camera is available before attempting to start
                    if not camera_state.is_available():
                        # Camera not available, wait and retry
                        if proc is not None:
                            try:
                                proc.terminate()
                                proc.wait(timeout=2)
                            except:
                                pass
                            proc = None
                        await asyncio.sleep(2)
                        continue
                    
                    if proc is not None:
                        try:
                            proc.terminate()
                            proc.wait(timeout=2)
                        except:
                            pass
                        
                        # Check if process failed quickly (within 3 seconds = likely startup failure)
                        if time.time() - process_start_time < 3:
                            consecutive_failures += 1
                            print(f"[send_frames] Camera process failed quickly (failure #{consecutive_failures})")
                            
                            # After 3 consecutive quick failures, do deep cleanup
                            if consecutive_failures >= 3:
                                print(f"[send_frames] Multiple failures detected, performing camera reset...")
                                cleanup_camera()
                                await asyncio.sleep(2)
                                Camera.releaseViewfinder()
                                await asyncio.sleep(2)
                                consecutive_failures = 0  # Reset counter after cleanup
                            else:
                                # Short delay for temporary issues
                                await asyncio.sleep(1)
                        else:
                            # Process ran for a while before failing - reset failure counter
                            consecutive_failures = 0
                    
                    try:
                        # Small delay before starting gphoto2 to allow any pending camera commands to complete
                        await asyncio.sleep(0.3)

                        # Some cameras require viewfinder/liveview to be explicitly enabled
                        # before --capture-movie produces frames.
                        try:
                            subprocess.run(
                                ["gphoto2", "--set-config", "viewfinder=1"],
                                capture_output=True,
                                text=True,
                                timeout=3,
                            )
                        except Exception:
                            pass
                        
                        proc = subprocess.Popen([
                            "gphoto2", "--capture-movie", "--stdout"
                        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        
                        # Set non-blocking mode on stdout
                        flags = fcntl.fcntl(proc.stdout, fcntl.F_GETFL)
                        fcntl.fcntl(proc.stdout, fcntl.F_SETFL, flags | os_module.O_NONBLOCK)
                        
                        # Track when process started and reset check timer
                        process_start_time = time.time()
                        last_process_check_time = time.time()
                        
                        print(f"[send_frames] Started gphoto2 process (PID: {proc.pid})")
                    except Exception as proc_error:
                        print(f"[send_frames] Failed to start gphoto2: {proc_error}")
                        consecutive_failures += 1
                        await asyncio.sleep(2)
                        continue
                
                buffer = b''
                pending_frame = None
                try:
                    while is_liveview_enabled() and ws.close_code is None:
                        try:
                            if pending_frame is not None and time.time() - last_frame_time >= frame_interval:
                                available_memory = _available_memory()

                                if available_memory is not None:
                                    if available_memory < RESTART_MEMORY_THRESHOLD:
                                        print(f"[send_frames] Critical low memory ({available_memory} bytes); restarting camera process")
                                        pending_frame = None
                                        break
                                    if available_memory < PAUSE_MEMORY_THRESHOLD:
                                        print(f"[send_frames] Low memory pause ({available_memory} bytes); delaying send")
                                        await asyncio.sleep(0.05)
                                        continue
                                    if available_memory < DROP_MEMORY_THRESHOLD:
                                        low_memory_drop_toggle = not low_memory_drop_toggle
                                        if low_memory_drop_toggle:
                                            print(f"[send_frames] Low memory drop ({available_memory} bytes); skipping latest frame")
                                            pending_frame = None
                                            continue

                                frame_size = len(pending_frame)
                                send_start = time.time()
                                try:
                                    await asyncio.wait_for(ws.send(pending_frame), timeout=2.0)
                                    send_duration = time.time() - send_start
                                    last_frame_time = time.time()
                                    if send_duration > 0.5:
                                        print(f"[send_frames] ws.send took {send_duration:.2f}s for {frame_size} bytes")
                                    pending_frame = None
                                except (websockets.exceptions.ConnectionClosed,
                                       websockets.exceptions.ConnectionClosedError,
                                       asyncio.TimeoutError) as send_error:
                                    print(f"[send_frames] WebSocket send failed: {type(send_error).__name__}")
                                    raise send_error
                                except Exception as send_error:
                                    error_type = type(send_error).__name__
                                    error_msg = str(send_error)
                                    print(f"[send_frames] WebSocket send failed: {error_type} - {error_msg}")
                                    raise send_error

                            # Check if process is still running periodically
                            current_time = time.time()
                            if current_time - last_process_check_time >= process_check_interval:
                                if proc.poll() is not None:
                                    stderr_text = _read_proc_stderr_text(proc)
                                    print(f"[send_frames] Camera process terminated unexpectedly (exit code: {proc.returncode})")
                                    if stderr_text:
                                        print(f"[send_frames] gphoto2 stderr: {stderr_text}")
                                        if "could not claim the usb device" in stderr_text.lower():
                                            print("[send_frames] USB busy detected. Releasing camera lock holders...")
                                            _cleanup_usb_camera_lockers()
                                            await asyncio.sleep(1.5)
                                    break
                                last_process_check_time = current_time
                            
                            # Non-blocking read with timeout handling
                            try:
                                chunk = proc.stdout.read(4096)
                                if not chunk:
                                    # Empty read may mean no data available (non-blocking) or EOF
                                    if proc.poll() is not None:
                                        stderr_text = _read_proc_stderr_text(proc)
                                        print(f"[send_frames] Camera process ended (reached EOF, exit code: {proc.returncode})")
                                        if stderr_text:
                                            print(f"[send_frames] gphoto2 stderr: {stderr_text}")
                                            if "could not claim the usb device" in stderr_text.lower():
                                                print("[send_frames] USB busy detected. Releasing camera lock holders...")
                                                _cleanup_usb_camera_lockers()
                                                await asyncio.sleep(1.5)
                                        break
                                    # No data available right now, yield to event loop
                                    await asyncio.sleep(0.01)
                                    continue
                                    
                                buffer += chunk

                                latest_frame, buffer = _extract_latest_frame(buffer)
                                if latest_frame is not None:
                                    pending_frame = latest_frame
                            except BlockingIOError:
                                # Non-blocking read returned no data, that's OK
                                await asyncio.sleep(0.01)
                                continue
                                
                        except Exception as read_error:
                            error_str = str(read_error).lower()
                            if 'broken pipe' in error_str or 'bad file descriptor' in error_str:
                                print(f"[send_frames] Camera process error (broken pipe), restarting...")
                                break
                            else:
                                raise read_error
                        
                        if not is_liveview_enabled():
                            break
                    
                    # Clean up camera process after inner loop
                    if proc:
                        try:
                            proc.terminate()
                            proc.wait(timeout=2)
                        except:
                            pass
                        proc = None
                        
                except Exception as e:
                    print(f"[send_frames] Inner exception: {type(e).__name__} - {str(e)}")
                    # Clean up camera process on exception
                    if proc:
                        try:
                            proc.terminate()
                            proc.wait(timeout=2)
                        except:
                            pass
                        proc = None
                    raise  # Re-raise to trigger reconnection
        
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            connection_duration = time.time() - connection_start_time
            
            # Provide more specific error information but suppress the asyncio callback errors
            if 'ConnectionClosed' in error_type or 'ConnectionClosedError' in error_type:
                print(f"[send_frames] WebSocket connection closed at {datetime.now()}")
            elif 'timeout' in error_msg.lower():
                print(f"[send_frames] Connection timeout at {datetime.now()}")
            elif 'network' in error_msg.lower() or 'unreachable' in error_msg.lower():
                print(f"[send_frames] Network error at {datetime.now()}")
            elif 'ConnectionRefused' in error_type:
                print(f"[send_frames] Server refused connection at {datetime.now()}")
            else:
                print(f"[send_frames] Connection lost at {datetime.now()}: {error_type}")
            
            print(f"[send_frames] Connection lasted {connection_duration:.1f} seconds")
            print(f"[send_frames] Reconnecting in 5 seconds...")
            
        finally:
            # Ensure proper cleanup
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except:
                    try:
                        proc.kill()
                    except:
                        pass
                proc = None
            
            _open_sockets.discard(ws)
            if ws and ws.close_code is None:
                try:
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                except:
                    pass
        
        # Wait before reconnecting
        await asyncio.sleep(5)

def cleanup_camera():
    # Clean up camera processes
    print("[cleanup] Releasing camera and killing all gphoto2 processes...")
    print("[cleanup] Setting liveview state to false...")
    save_liveview_state(False)
    try:
        subprocess.run(["pkill", "-9", "gphoto2"])
    except Exception as e:
        print(f"[cleanup] Error killing gphoto2: {e}")

def handle_exit(signum, frame):
    # Handle exit signals
    print(f"[signal] Received signal {signum}, exiting and releasing camera...")
    try:
        leds = get_led_manager()
        leds.shutdown()
    except Exception:
        pass
    cleanup_camera()
    os._exit(0)

def setup_client_config():
    # Interactive setup for client configuration
    print("=== Telescope Client Configuration Setup ===")
    print()
    
    # Allow setup to run even if the config file doesn't exist yet
    current_config = load_config(allow_missing=True)
    
    print(f"Current client ID: {current_config.get('client_id', 'Not set')}")
    new_client_id = input("Enter client ID (press Enter to keep current): ").strip()
    if new_client_id:
        current_config['client_id'] = new_client_id
    
    print(f"Current base URL: {current_config.get('base_url', 'Not set')}")
    new_base_url = input("Enter base URL (e.g. https://telescopes.dev/, press Enter to keep current): ").strip()
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
    # Main async function that runs both WebSocket client and frame sender
    # Check if we need to run setup
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_client_config()
        return
    # If no config was passed in, load it from disk (this errors if missing)
    if cfg is None:
        cfg = load_config()

    # Ensure required keys are present in the config
    required_keys = ["client_id", "base_url", "api_token"]
    missing = [k for k in required_keys if not isinstance(cfg.get(k), str) or not cfg.get(k).strip()]
    if missing:
        raise KeyError(f"Missing required values in config/client_profile.json: {', '.join(missing)}. Run 'python Client.py setup' to create or fix the config.")

    # Export values to module-level globals used by other functions
    global CLIENT_ID, SERVER_URI, LIVEVIEW_URI, SERVER_HTTP_URL, API_TOKEN, _loop
    _loop = asyncio.get_running_loop()

    CLIENT_ID = cfg["client_id"]
    API_TOKEN = cfg["api_token"]
    urls = build_service_urls(cfg["base_url"])
    SERVER_HTTP_URL = urls["http_url"]
    SERVER_URI = urls["server_uri"]
    LIVEVIEW_URI = urls["liveview_uri"]
    leds = get_led_manager()
    leds.set_ui_connected(True)
    
    
    
    # Set up signal handlers
    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)
    
    try:
        # Start all background tasks (each handles their own reconnection/error logic)
        task1 = asyncio.create_task(run_client())
        task2 = asyncio.create_task(send_frames())
        task3 = asyncio.create_task(camera_scanner_task(check_interval=2.0))
        task4 = asyncio.create_task(esp32_scanner_task(check_interval=2.0))
        task5 = asyncio.create_task(peripheral_refresh_task(leds, refresh_interval=1.0))
        
        # Wait for all tasks to complete (which should be never, unless interrupted)
        await asyncio.gather(task1, task2, task3, task4, task5)
        
    except KeyboardInterrupt:
        print("[main] KeyboardInterrupt received, exiting and releasing camera...")
    except Exception as e:
        print(f"[main] Unexpected exception: {e}")
    finally:
        try:
            leds.shutdown()
        except Exception:
            pass
        cleanup_camera()
