"""What the server can ask of the telescope: the functions it calls by
name over the command socket (core/networking/websocket.py), listed in
function_map at the end.

The camera ones go through the camera service (core/camera/service.py),
which owns the camera; live view for the server is core/camera/stream.py;
photos upload through utils/uploads.py. Commands the ASTRA server sends
today keep their names and arguments; the rest are new (README.md lists
them all).
"""
import os
import threading
import time
from datetime import datetime
from typing import Optional, Any, Dict, Callable
from functools import wraps
from core.camera.backends import CameraError
from core.camera.service import get_camera_service
from core.camera.stream import PRESETS, get_live_stream
from utils import motor_guard, photos
from utils.config_state import get_client_config, get_section, save_section, build_service_urls
from utils.LEDmanager import get_led_manager
from utils.esp32_state import esp32_state
from utils.liveview_state import save_liveview_state
from utils.log import get_logger
from utils.uploads import get_upload_queue
from esp32.interfaceESP32 import ESP32Error
from core.hardware.tracking import trackCoordinates, stop_tracking
from utils.telescope_state import get_telescope_coords

log = get_logger("handlers")


def requires_camera(operation_name: str = "operation"):
    # Returns {"error": ...} when the camera isn't connected or the
    # operation fails, rather than raising into the command loop.
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not get_camera_service().is_available():
                error_msg = f"Camera not connected - cannot {operation_name}"
                log.info(error_msg)
                return {"error": error_msg}
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_msg = f"Failed to {operation_name}: {str(e)}"
                log.warning(error_msg)
                return {"error": error_msg}
        return wrapper
    return decorator


def _server_url() -> str:
    # Read at upload time: the server can be changed on the front panel.
    base = get_client_config().get("base_url")
    return build_service_urls(base)["http_url"].rstrip("/") if base else ""


def echo(message):
    return f"Echo: {message}"


# --- camera settings -------------------------------------------------------------------

# What the web page calls them -> the setting (core/camera/backends.SETTING_ALIASES).
CAMERA_SETTINGS = {
    "shutterSpeed": "shutterspeed",
    "iso": "iso",
    "aperture": "aperture",
    "whiteBalance": "whitebalance",
    "imageFormat": "imageformat",
    "exposureCompensation": "exposurecompensation",
}
def _setting_from_label(label: str) -> str:
    # "/main/capturesettings/shutterspeed" (what the web page sends),
    # "shutterSpeed" or "shutterspeed".
    name = str(label).strip().rsplit("/", 1)[-1]
    return CAMERA_SETTINGS.get(name, name.lower())


@requires_camera("get camera settings")
def get_camera_choices():
    """Choices for each setting the camera has ("shutterSpeed", "iso", ...)
    and "current": their values now."""
    service = get_camera_service()
    choices: Dict[str, Any] = {}
    current: Dict[str, Any] = {}
    start = time.monotonic()
    for label, setting in CAMERA_SETTINGS.items():
        try:
            info = service.get_setting(setting)
        except CameraError:
            if label in ("shutterSpeed", "iso"):
                choices[label] = []
            continue
        choices[label] = info.get("choices") or []
        current[label] = info.get("value")
    choices["current"] = current
    log.info("Camera settings read in %.2f s", time.monotonic() - start)
    return choices


@requires_camera("set camera setting")
def setCameraSetting(label, value):
    setting = _setting_from_label(label)
    get_camera_service().set_setting(setting, value)
    get_live_stream().poke()
    return f"Set {label} to {value}"


# --- photos -----------------------------------------------------------------------------

@requires_camera("capture photo")
def capturePhoto(currentid, exposure_s=None):
    """Take a photo for the account currentid and queue it for upload.
    exposure_s: a bulb exposure in seconds (else the shutter setting).
    Returns once the camera has saved it; the upload carries on."""
    leds = get_led_manager()
    leds.set_command_busy(True)
    leds.flash_capture_started()
    try:
        owner = str(currentid or "").strip()
        info = photos.pointing()
        base = f"{owner or 'photo'}_photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            paths = photos.capture_to(photos.SERVER_DIR, base, float(exposure_s) if exposure_s else None)
        except RuntimeError as exc:
            leds.set_error(True, critical=False)
            return {"error": str(exc)}
        names = [os.path.basename(p) for p in paths]
        photos.record(photos.SERVER_DIR, names, dict(info, owner=owner))
        problems = get_upload_queue().add_many(paths, owner, info)
        queued = sum(1 for p in problems if p is None)
        problem = next((p for p in problems if p), None)
        leds.flash_command_complete(True)
        result: Dict[str, Any] = {"status": "captured", "files": names, "queued": queued}
        if problem:
            result["note"] = problem
        return result
    finally:
        leds.set_command_busy(False)


def startPhotoSequence(kind="light", count=10, exposure_s=None, interval_s=0.0, dither_every=0, dither_arcmin=2.0):
    """A run of frames on the Pi (saved locally, like panel photos):
    kind light, dark, flat or bias."""
    from utils.panel_ui import get_panel_ui

    if not get_camera_service().is_available():
        return {"error": "Camera not connected"}
    plan = photos.SequencePlan(str(kind), int(count), float(exposure_s) if exposure_s else None,
        float(interval_s or 0), int(dither_every or 0), float(dither_arcmin or 2.0))
    ui = get_panel_ui()
    message = ui.photo_run.start(plan.count, ui.toast, plan=plan)
    return {"status": message, **ui.photo_run.status()}


def stopPhotoSequence():
    from utils.panel_ui import get_panel_ui

    return {"status": get_panel_ui().photo_run.stop()}


def getPhotoSequenceStatus():
    from utils.panel_ui import get_panel_ui

    return get_panel_ui().photo_run.status()


def getUploadQueue():
    return get_upload_queue().status()


def uploadPending(kind="raw"):
    """Send the files held back (RAWs with upload.raw = on_request)."""
    return {"released": get_upload_queue().release(str(kind))}


def retryFailedUploads():
    return {"retrying": get_upload_queue().retry_failed()}


def uploadOfflinePhotos(currentid=None):
    """Queue the photos taken offline for upload, under currentid (or the
    photo owner id set on the panel)."""
    owner = str(currentid or photos.owner_id() or "").strip()
    if not owner.isdigit():
        return {"error": "No account id: pass one, or set the photo owner id on the panel"}
    return {"queued": get_upload_queue().queue_folder(photos.local_dir(), owner)}


# --- live view --------------------------------------------------------------------------

def startLiveView(quality=None):
    if quality is not None:
        result = setLiveViewOptions(quality=quality)
        if "error" in result:
            return result
    save_liveview_state(True)
    get_led_manager().apply()
    if not get_camera_service().is_available():
        return "Live view on: waiting for the camera"
    return "Live view started"


def stopLiveView():
    save_liveview_state(False)
    get_led_manager().apply()
    return "Live view stopped"


def liveViewViewers(count):
    """The server says how many are watching: with nobody, the stream
    pauses after liveview.viewer_grace_s and the camera rests."""
    get_live_stream().set_viewers(int(count))
    return {"viewers": int(count)}


def getLiveViewStats():
    return {**get_live_stream().stats(), "camera": get_camera_service().status()}


_LIVEVIEW_OPTIONS = {"quality": str, "max_fps": float, "still_fps": float, "keyframe_s": float,
    "adaptive": bool, "idle_stop_min": float, "viewer_grace_s": float}


def setLiveViewOptions(**options):
    """Change live view settings (saved): quality (original, high, medium,
    low), max_fps, still_fps, keyframe_s, adaptive, idle_stop_min,
    viewer_grace_s."""
    changes: Dict[str, Any] = {}
    for key, value in options.items():
        if key not in _LIVEVIEW_OPTIONS:
            return {"error": f"Unknown option {key}"}
        try:
            changes[key] = _LIVEVIEW_OPTIONS[key](value)
        except (TypeError, ValueError):
            return {"error": f"Bad value for {key}"}
    if "quality" in changes and changes["quality"] not in PRESETS:
        return {"error": f"quality is one of {', '.join(PRESETS)}"}
    if changes:
        save_section("liveview", changes)
        get_live_stream().reload_settings()
    return {"liveview": get_section("liveview")}


def setLiveViewQuality(quality):
    return setLiveViewOptions(quality=quality)


def setLiveViewFocus(on=True):
    """Focus mode: a full-size crop round the brightest star, with its HFR
    in getLiveViewStats()."""
    get_live_stream().set_focus(bool(on))
    return {"focus": bool(on)}


@requires_camera("zoom")
def setCameraZoom(level, x=None, y=None):
    """The camera's live-view magnification (1, 5 or 10 on Canon), and
    where (x, y in the sensor's pixels)."""
    service = get_camera_service()
    if service.setting_name("zoom") is None:
        return {"error": "This camera has no live-view zoom"}
    service.set_setting("zoom", str(level))
    if x is not None and y is not None and service.setting_name("zoomposition") is not None:
        service.set_setting("zoomposition", f"{int(x)},{int(y)}")
    get_live_stream().poke()
    return {"zoom": level}


def liveViewSnapshot(currentid=None):
    """Save the current live-view frame (no shutter); uploaded too when an
    account id comes with it."""
    from core.camera.recorder import snapshot

    try:
        path = snapshot()
    except RuntimeError as exc:
        return {"error": str(exc)}
    result: Dict[str, Any] = {"file": os.path.basename(path)}
    owner = str(currentid or "").strip()
    if owner.isdigit():
        problem = get_upload_queue().add(path, owner, photos.pointing())
        result["queued"] = problem is None
    return result


def startRecording(kind="video", interval_s=10.0):
    """Record live view on the Pi: kind "video" (every frame, for stacking
    the Moon and planets) or "timelapse" (a frame every interval_s)."""
    from core.camera.recorder import get_recorder

    if not get_camera_service().is_available():
        return {"error": "Camera not connected"}
    try:
        return {"status": get_recorder().start(str(kind), float(interval_s))}
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc)}


def stopRecording():
    from core.camera.recorder import get_recorder

    return {"status": get_recorder().stop(), **get_recorder().status()}


def getRecordingStatus():
    from core.camera.recorder import get_recorder

    return get_recorder().status()


_camera_info_cache: Dict[str, Any] = {"at": 0.0, "value": {}}


def camera_info(max_age_s: float = 60.0) -> Dict[str, Any]:
    """Battery and card space (asked of the camera at most once a minute)."""
    service = get_camera_service()
    if not service.is_available():
        return {}
    now = time.monotonic()
    if now - _camera_info_cache["at"] < max_age_s:
        return _camera_info_cache["value"]
    info: Dict[str, Any] = {"battery": service.battery()}
    try:
        info["card"] = service.storage()
    except CameraError:
        info["card"] = None
    _camera_info_cache.update(at=now, value=info)
    return info


def getCameraStatus():
    return {**get_camera_service().status(), **camera_info(), "pi_disk": photos.disk_text()}


# --- ESP32 motor controls ---------------------------------------------------------------

def _get_esp32_connection():
    # Get the current ESP32 connection, reconnecting on demand if needed.
    return esp32_state.ensure_connection()


def _esp32_error_response(exc: Exception) -> Dict[str, Any]:
    # An error reply ("Motor not engaged", "Emergency stop active") means the
    # link is fine; only drop the connection for timeouts and I/O failures.
    if isinstance(exc, ESP32Error):
        return {"error": exc.error_message}
    esp32_state.mark_disconnected()
    return {"error": str(exc)}

def espEnable(on: Any, motor_id: str = "motor1") -> Dict[str, Any]:
    # Enable/disable the stepper driver.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        return conn.send({"cmd": "enable", "motor": motor_id, "value": bool(on)})
    except Exception as e:
        return _esp32_error_response(e)

def espSetDirection(forward: Any, motor_id: str = "motor1") -> Dict[str, Any]:
    # Set motor direction: True=forward, False=reverse.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        # Note: The new interface doesn't have a separate set_direction command
        # Direction is controlled via the forward parameter in turn_degrees
        return {"status": "ok", "message": "Direction is controlled via forward parameter in movement commands"}
    except Exception as e:
        return _esp32_error_response(e)

def espSetSpeed(sps: Any, motor_id: str = "motor1") -> Dict[str, Any]:
    # Set speed in steps/sec.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        return conn.send({"cmd": "set_speed", "motor": motor_id, "sps": float(sps)})
    except Exception as e:
        return _esp32_error_response(e)

def espStart(sps: Any, forward: Optional[Any] = None, motor_id: str = "motor1") -> Dict[str, Any]:
    # Start continuous rotation at speed with optional direction, until espStop.
    # Guarded (utils/motor_guard.py): stopped at the axis limits or the Sun.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        fwd = bool(forward) if forward is not None else True
        refused = motor_guard.check_raw_move(motor_id, continuous=True, forward=fwd)
        if refused:
            return {"error": refused}
        conn.send({"cmd": "set_speed", "motor": motor_id, "sps": float(sps)})
        result = conn.send({"cmd": "start_continuous", "motor": motor_id, "forward": fwd})
        motor_guard.watch_continuous(motor_id, fwd)
        return result
    except Exception as e:
        return _esp32_error_response(e)

def espMoveSteps(steps: Any, sps: Optional[Any] = None, forward: Optional[Any] = None, motor_id: str = "motor1") -> Dict[str, Any]:
    # Move a finite number of (micro)steps with optional speed and direction.
    # A negative step count, or forward=False, moves backwards.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        count = int(steps)
        if forward is not None and not bool(forward):
            count = -abs(count)
        refused = motor_guard.check_raw_move(motor_id, steps=count)
        if refused:
            return {"error": refused}
        if sps is not None:
            conn.send({"cmd": "set_speed", "motor": motor_id, "sps": float(sps)})
        return conn.send({"cmd": "move_steps", "motor": motor_id, "steps": count})
    except Exception as e:
        return _esp32_error_response(e)

def espStop(motor_id: str = "motor1") -> Dict[str, Any]:
    # Stop motion.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        return conn.send({"cmd": "stop", "motor": motor_id})
    except Exception as e:
        return _esp32_error_response(e)

def _esp32_send(payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        return conn.send(payload, timeout=timeout)
    except Exception as e:
        return _esp32_error_response(e)

def espSetMicrosteps(value: Any, motor_id: str = "motor1") -> Dict[str, Any]:
    # TMC2209 microsteps: "auto" (default; chosen per move: fine for tracking,
    # coarse for slews) or a fixed 1, 2, 4 ... 256. Positions and speeds keep
    # their units (1600 steps/rev) either way.
    microsteps: Any = "auto" if str(value).strip().lower() == "auto" else int(value)
    return _esp32_send({"cmd": "set_microsteps", "motor": motor_id, "microsteps": microsteps}, timeout=2.0)

def espSetAuto(motor_id: str = "motor1", min_microsteps: Optional[Any] = None,
               max_microsteps: Optional[Any] = None, max_pulse_hz: Optional[Any] = None) -> Dict[str, Any]:
    # Tune automatic microstep selection.
    payload: Dict[str, Any] = {"cmd": "set_auto", "motor": motor_id}
    if min_microsteps is not None:
        payload["min_microsteps"] = int(min_microsteps)
    if max_microsteps is not None:
        payload["max_microsteps"] = int(max_microsteps)
    if max_pulse_hz is not None:
        payload["max_pulse_hz"] = float(max_pulse_hz)
    return _esp32_send(payload, timeout=2.0)

def espSetCurrent(mA: Any, motor_id: str = "motor1", hold_mA: Optional[Any] = None) -> Dict[str, Any]:
    # RMS run current in mA (1-1350 on the v0.8 board) and optional hold current.
    payload: Dict[str, Any] = {"cmd": "set_current", "motor": motor_id, "run_ma": int(mA)}
    if hold_mA is not None:
        payload["hold_ma"] = int(hold_mA)
    return _esp32_send(payload, timeout=2.0)

def espSetMode(mode: Any, motor_id: str = "motor1", hybrid_threshold_sps: Optional[Any] = None) -> Dict[str, Any]:
    # Chopper mode: "auto" (default; StealthChop below 1 rev/s, SpreadCycle
    # above), "stealth"/"stealthchop"/"silent" (quiet), "spread"/"spreadcycle"
    # (more torque at speed) or "hybrid" (StealthChop below
    # hybrid_threshold_sps, SpreadCycle above).
    payload: Dict[str, Any] = {"cmd": "set_chopper", "motor": motor_id, "mode": str(mode)}
    if hybrid_threshold_sps is not None:
        payload["hybrid_threshold_sps"] = float(hybrid_threshold_sps)
    return _esp32_send(payload, timeout=2.0)

def espSetAccel(sps2: Any, motor_id: str = "motor1", start_sps: Optional[Any] = None) -> Dict[str, Any]:
    # Acceleration ramp in steps/s^2 (0 = instant starts and stops) and the
    # speed moves may start at without ramping. Slews ramp up and down; the
    # tracking rate is below the start speed, so tracking starts at once.
    payload: Dict[str, Any] = {"cmd": "set_accel", "motor": motor_id, "accel": float(sps2)}
    if start_sps is not None:
        payload["start_sps"] = float(start_sps)
    return _esp32_send(payload, timeout=2.0)

def espDriverStatus(motor_id: str = "motor1") -> Dict[str, Any]:
    # Live TMC2209 registers: microsteps, currents, chopper, temperature flags.
    return _esp32_send({"cmd": "driver", "motor": motor_id, "action": "status"}, timeout=2.0)

def espEstop() -> Dict[str, Any]:
    # Halt every motor and latch the emergency stop (same as the RETURN button).
    return _esp32_send({"cmd": "estop", "action": "trigger"})

def espClearEstop() -> Dict[str, Any]:
    return _esp32_send({"cmd": "estop", "action": "clear"})

def espEstopStatus() -> Dict[str, Any]:
    return _esp32_send({"cmd": "estop", "action": "status"})

def espInfo() -> Dict[str, Any]:
    return _esp32_send({"cmd": "info"}, timeout=2.0)

def espPanelStatus() -> Dict[str, Any]:
    # Front-panel buttons and LEDs (MCP23017).
    return _esp32_send({"cmd": "panel", "action": "status"})

def espImuRead() -> Dict[str, Any]:
    # Latest BNO085 orientation, gravity and magnetic field.
    return _esp32_send({"cmd": "imu", "action": "read"})

def espStatus(motor_id: str = "motor1") -> Dict[str, Any]:
    # Query motor status.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        return conn.send({"cmd": "status", "motor": motor_id})
    except Exception as e:
        return _esp32_error_response(e)

def espStatusAll() -> Dict[str, Any]:
    # Query status of all motors.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        return conn.list_motors()
    except Exception as e:
        return _esp32_error_response(e)

def espTurnDegrees(degrees: Any, forward: Any = True, motor_id: str = "motor1") -> Dict[str, Any]:
    # Move motor a specified number of degrees.
    try:
        conn = _get_esp32_connection()
        if not conn:
            return {"error": "ESP32 connection not available"}
        refused = motor_guard.check_raw_move(motor_id, motor_degrees=float(degrees) * (1 if bool(forward) else -1))
        if refused:
            return {"error": refused}
        return conn.send({
            "cmd": "turn_degrees",
            "motor": motor_id,
            "degrees": float(degrees),
            "forward": bool(forward)
        })
    except Exception as e:
        return _esp32_error_response(e)


# --- pointing and tracking ------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _start_job(name: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    """Run something long (a park, a plate solve) in the background: the
    server only waits about 3 s for a reply. getJobStatus(name) says how it
    went."""
    with _jobs_lock:
        job = _jobs.get(name)
        if job and job.get("running"):
            return {"error": f"{name} is already running"}
        job = {"running": True, "started": time.time(), "result": None, "error": None}
        _jobs[name] = job

    def run() -> None:
        try:
            result = fn()
            if isinstance(result, dict) and result.get("error"):
                job["error"] = result["error"]
            job["result"] = result
        except Exception as exc:
            job["error"] = str(exc)[:160]
            log.warning("%s failed: %s", name, exc)
        finally:
            job["running"] = False
            job["finished"] = time.time()

    threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
    return {"status": "started", "job": name}


def getJobStatus(name=None):
    if name is None:
        return {k: {kk: vv for kk, vv in v.items()} for k, v in _jobs.items()}
    return _jobs.get(str(name)) or {"error": f"No job {name}"}


def trackObject(name):
    """Go to and track the Moon, a planet or a named star (worked out on
    the Pi, so it works offline)."""
    from core.hardware import tracking

    return tracking.track_object(str(name))


def park():
    from core.hardware import tracking

    return _start_job("park", tracking.park)


def unpark():
    from core.hardware import tracking

    return tracking.unpark()


def setParkHere():
    from core.hardware import tracking

    return tracking.set_park_here()


def setTrackingRate(rate, custom_arcsec_per_s=None):
    """sidereal, lunar, solar, king or custom (custom_arcsec_per_s)."""
    from core.hardware import tracking

    return tracking.set_rate(str(rate), custom_arcsec_per_s)


def dither(arcmin=2.0):
    from core.hardware import tracking

    return _start_job("dither", lambda: {"settled": tracking.dither(float(arcmin))})


def getAlignment():
    from core.hardware import tracking

    return tracking.alignment_summary()


def clearAlignment():
    from core.hardware import tracking

    return tracking.clear_alignment()


def solveAndSync(source="auto"):
    """Plate-solve where it points, and add that to the alignment."""
    from utils import plate_solve

    return _start_job("solve", lambda: plate_solve.solve_and_sync(str(source)))


def centreTarget(source="auto"):
    from utils import plate_solve

    return _start_job("centre", lambda: plate_solve.centre_target(source=str(source)))


def polarAlign(step_deg=30.0, source="auto"):
    """Three-point polar alignment (turns the RA axis twice)."""
    from utils import plate_solve

    return _start_job("polar", lambda: plate_solve.polar_align(float(step_deg), str(source)))


def getMountStatus():
    from core.hardware import tracking
    from utils import clock
    from utils.telescope_state import get_mount_axes, get_parked, hemisphere

    axes = get_mount_axes()
    return {
        "state": tracking._panel_state,
        "target": (tracking._target_object or {}).get("name") if tracking._tracking_active else None,
        "coords": get_telescope_coords(),
        "axes": axes,
        "parked": get_parked(),
        "hemisphere": hemisphere(),
        "rate": tracking.current_rate(),
        "alignment": tracking.alignment_summary(),
        "clock": clock.status(),
    }


def setClock(utc):
    """Set the time astronomy uses, when the Pi's clock isn't synchronised
    ("2026-10-16 21:30:00", UTC)."""
    from utils import clock

    try:
        offset = clock.set_time(clock.parse_utc(str(utc)), source="manual")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"offset_s": round(offset, 1), **clock.status()}


def setHorizon(min_alt_deg=None, profile=None, stop_when_set=None):
    """min_alt_deg; profile [[azimuth, altitude], ...]; stop_when_set."""
    changes: Dict[str, Any] = {}
    if min_alt_deg is not None:
        changes["min_alt_deg"] = max(-10.0, min(60.0, float(min_alt_deg)))
    if profile is not None:
        try:
            changes["profile"] = [[float(p[0]) % 360.0, float(p[1])] for p in profile]
        except (TypeError, ValueError, IndexError):
            return {"error": "profile is a list of [azimuth, altitude]"}
    if stop_when_set is not None:
        changes["stop_when_set"] = bool(stop_when_set)
    if changes:
        save_section("horizon", changes)
    return get_section("horizon")


def getPlanets():
    """The Sun, Moon and planets now, J2000 (worked out on the Pi)."""
    from core.hardware.tracking import _site
    from utils import clock, ephemeris

    return {"planets": ephemeris.positions(clock.now(), _site())}


# --- the client as a whole ------------------------------------------------------------

def getClientStatus():
    """Everything at once: what the status push sends (websocket.py)."""
    from utils import client_status

    return client_status.collect()


def updateClient():
    """git pull and restart (only with server.allow_remote_update)."""
    from utils import maintenance

    return maintenance.update_client()


def updateFirmware():
    """Build and flash the ESP32 firmware (only with
    server.allow_firmware_update)."""
    from utils import maintenance

    return maintenance.update_firmware()


def get_current_coordinates():
    # The current telescope coordinates (right ascension and declination).
    coords = get_telescope_coords()
    if coords is None:
        return {"error": "Telescope coordinates not available"}
    return {
        "current_right_ascension": coords["right_ascension"],
        "current_declination": coords["declination"]
    }


# Function mapping dictionary
function_map = {
    # Debug
    "echo": echo,
    "getClientStatus": getClientStatus,
    # Main track requests
    "trackCoordinates": trackCoordinates,
    "stopTracking": stop_tracking,
    "getCurrentCoordinates": get_current_coordinates,
    # Camera
    "getCameraChoices": get_camera_choices,
    "setCameraSetting": setCameraSetting,
    "capturePhoto": capturePhoto,
    "getCameraStatus": getCameraStatus,
    "setCameraZoom": setCameraZoom,
    "startPhotoSequence": startPhotoSequence,
    "stopPhotoSequence": stopPhotoSequence,
    "getPhotoSequenceStatus": getPhotoSequenceStatus,
    # Uploads
    "getUploadQueue": getUploadQueue,
    "uploadPending": uploadPending,
    "retryFailedUploads": retryFailedUploads,
    "uploadOfflinePhotos": uploadOfflinePhotos,
    # Live view
    "startLiveView": startLiveView,
    "stopLiveView": stopLiveView,
    "liveViewViewers": liveViewViewers,
    "getLiveViewStats": getLiveViewStats,
    "setLiveViewOptions": setLiveViewOptions,
    "setLiveViewQuality": setLiveViewQuality,
    "setLiveViewFocus": setLiveViewFocus,
    "liveViewSnapshot": liveViewSnapshot,
    "startRecording": startRecording,
    "stopRecording": stopRecording,
    "getRecordingStatus": getRecordingStatus,
    # ESP32 controls (the web page's motor panel uses these)
    "espEnable": espEnable,
    "espSetDirection": espSetDirection,
    "espSetSpeed": espSetSpeed,
    "espStart": espStart,
    "espMoveSteps": espMoveSteps,
    "espTurnDegrees": espTurnDegrees,
    "espStop": espStop,
    "espSetMicrosteps": espSetMicrosteps,
    "espSetCurrent": espSetCurrent,
    "espSetMode": espSetMode,
    "espSetAccel": espSetAccel,
    "espStatus": espStatus,
    "espStatusAll": espStatusAll,
    "espSetAuto": espSetAuto,
    "espDriverStatus": espDriverStatus,
    "espEstop": espEstop,
    "espClearEstop": espClearEstop,
    "espEstopStatus": espEstopStatus,
    "espInfo": espInfo,
    "espPanelStatus": espPanelStatus,
    "espImuRead": espImuRead,
    # Pointing and tracking
    "trackObject": trackObject,
    "park": park,
    "unpark": unpark,
    "setParkHere": setParkHere,
    "setTrackingRate": setTrackingRate,
    "dither": dither,
    "getAlignment": getAlignment,
    "clearAlignment": clearAlignment,
    "solveAndSync": solveAndSync,
    "centreTarget": centreTarget,
    "polarAlign": polarAlign,
    "getMountStatus": getMountStatus,
    "getJobStatus": getJobStatus,
    "setClock": setClock,
    "setHorizon": setHorizon,
    "getPlanets": getPlanets,
    # Maintenance (off unless allowed in config server)
    "updateClient": updateClient,
    "updateFirmware": updateFirmware,
}

# Commands that stop things run at once, even while another command is
# still running (core/networking/websocket.py).
PRIORITY_FUNCTIONS = {"stopTracking", "espStop", "espEstop", "stopLiveView", "stopRecording",
    "stopPhotoSequence"}
