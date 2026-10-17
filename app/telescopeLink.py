import requests
import ujson
import os
from flask import session, has_request_context
from app.WebsocketServer import (
    clients,
    DEFAULT_COMMAND_TIMEOUT,
    INTERNAL_COMMAND_HEADER,
    INTERNAL_COMMAND_KEY,
)
import logging

logger = logging.getLogger(__name__)

# Determine the correct URL for /sendCommand
# Use localhost for internal calls to avoid Cloudflare challenges
flask_port = os.getenv("FLASK_PORT", "5000")
app_domain = os.getenv("APP_DOMAIN", "seleno.org")

url = f"http://localhost:{flask_port}/sendCommand"
external_domain = f"https://{app_domain}"

# Seconds the telescope gets to reply, for commands slower than the default
COMMAND_TIMEOUTS = {
    "getCameraChoices": 15,   # reads six camera settings
    "startCapturePlan": 10,   # a large message
}
# Margin on top of the exposure for capturePhoto
CAPTURE_MARGIN_S = 30

# Commands that start a background job and reply at once; poll getJobStatus(name)
JOB_NAMES = {
    "park": "park",
    "dither": "dither",
    "solveAndSync": "solve",
    "centreTarget": "centre",
    "polarAlign": "polar",
}


def command_timeout(command):
    return float(COMMAND_TIMEOUTS.get(command, DEFAULT_COMMAND_TIMEOUT))


def result_error(result):
    """The telescope's refusal message, when a command's result is {"error": ...}.

    Many client commands refuse by returning an error as a normal result (a
    go-to below the horizon, near the Sun, while parked), so a reply alone
    doesn't mean success."""
    if isinstance(result, dict):
        error = result.get("error")
        if error:
            return error if isinstance(error, str) else ujson.dumps(error)
    return None


def shutter_seconds(value):
    # A shutter setting such as "1/250", "30" or '2.5"' in seconds; None for Bulb or unknown
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip().lower().rstrip('s"').strip()
    try:
        if '/' in text:
            num, den = text.split('/', 1)
            return float(num) / float(den)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def load_api_token():
    # Load API token from environment for local telescope client use
    try:
        token = os.getenv("TELESCOPE_API_TOKEN") or os.getenv("API_TOKEN")
        if token:
            return token.strip()
        print("Warning: No telescope API token in environment (set TELESCOPE_API_TOKEN)")
        return None
    except Exception as e:
        print(f"Warning: Could not load API token: {e}")
        return None

class Telescope:
    # Represents a single telescope client
    def __init__(self, client_id: str):
        if not client_id:
            raise ValueError("Telescope requires a valid client_id")
        self.client_id = client_id
        self.camera = CameraController(self)
        self.motor = MotorController(self)
        self.scope = ScopeController(self)
        self.liveview = LiveViewController(self)
        self.uploads = UploadController(self)
        self.device = DeviceController(self)
        self.plans = CapturePlanController(self)

    # Framework to send a command over the websocket.
    # Returns the command's result, or {"error": message} when it couldn't be delivered.
    def send_command(self, command: str, args=None, kwargs=None, timeout=None):
        timeout = command_timeout(command) if timeout is None else float(timeout)
        payload = {"client_id": self.client_id, "command": command, "timeout": timeout}
        if args is not None:
            payload["args"] = args
        if kwargs is not None:
            payload["kwargs"] = kwargs
        try:
            # The HTTP call waits a little longer than the command server does
            resp = requests.post(
                url,
                json=payload,
                timeout=timeout + 4,
                headers={INTERNAL_COMMAND_HEADER: INTERNAL_COMMAND_KEY},
            )
            data = ujson.loads(resp.text)
        except Exception:
            logger.exception("send_command %s failed for %s", command, self.client_id)
            return {"error": "Could not reach the command server"}
        if data.get("status") == "error":
            return {"error": data.get("error") or "Command failed"}
        return data.get("result", data)

    def call(self, command, *args, timeout=None):
        # Positional arguments, without trailing Nones so the client's defaults apply
        args = list(args)
        while args and args[-1] is None:
            args.pop()
        return self.send_command(command, args, timeout=timeout)

    @staticmethod
    def resolve_client_id(provided_id=None):
        if provided_id:
            return provided_id
        try:
            if has_request_context():
                selected = session.get('selected_telescope') or {}
                sel_id = selected.get('telescope_id')
                if sel_id:
                    return sel_id
        except Exception:
            pass
        return None

    @classmethod
    def from_session(cls):
        cid = cls.resolve_client_id()
        if not cid:
            raise ValueError("No selected telescope in session")
        return cls(cid)


class CameraController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def get_settings(self):
        return self.telescope.send_command("getCameraChoices")

    def set_settings(self, args):
        return self.telescope.send_command("setCameraSetting", args)

    def capture_photo(self, current_id, exposure_s=None, shutter=None):
        # Bulb exposures take exposure_s; otherwise the shutter setting sets the wait
        seconds = exposure_s if exposure_s else shutter_seconds(shutter)
        timeout = (seconds or 0) + CAPTURE_MARGIN_S
        return self.telescope.call("capturePhoto", current_id, exposure_s, timeout=timeout)

    def get_status(self):
        # Battery and card
        return self.telescope.call("getCameraStatus")

    def start_live_view(self):
        return self.telescope.send_command("startLiveView")

    def stop_live_view(self):
        return self.telescope.send_command("stopLiveView")


class ScopeController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def park(self):
        return self.telescope.call("park")

    def unpark(self):
        return self.telescope.call("unpark")

    def set_park_here(self):
        return self.telescope.call("setParkHere")

    def track_object(self, name):
        # The Pi works out where the Moon or a planet appears from its own site
        return self.telescope.call("trackObject", name)

    def get_planets(self):
        return self.telescope.call("getPlanets")

    def set_tracking_rate(self, rate, custom_arcsec_per_s=None):
        return self.telescope.call("setTrackingRate", rate, custom_arcsec_per_s)

    def estop(self):
        return self.telescope.call("espEstop")

    def clear_estop(self):
        return self.telescope.call("espClearEstop")

    def estop_status(self):
        return self.telescope.call("espEstopStatus")

    def solve_and_sync(self, source=None):
        return self.telescope.call("solveAndSync", source)

    def centre_target(self, source=None):
        return self.telescope.call("centreTarget", source)

    def polar_align(self, step_deg=None, source=None):
        return self.telescope.call("polarAlign", step_deg, source)

    def job_status(self, name):
        return self.telescope.call("getJobStatus", name)

    def get_alignment(self):
        return self.telescope.call("getAlignment")

    def clear_alignment(self):
        return self.telescope.call("clearAlignment")

    def mount_status(self):
        return self.telescope.call("getMountStatus")

    def dither(self, arcmin=None):
        return self.telescope.call("dither", arcmin)


class LiveViewController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def set_quality(self, quality):
        return self.telescope.call("setLiveViewQuality", quality)

    def set_focus(self, on):
        return self.telescope.call("setLiveViewFocus", bool(on))

    def set_zoom(self, level, x=None, y=None):
        return self.telescope.call("setCameraZoom", level, x, y)

    def set_options(self, **options):
        # codec (auto, h264, jpeg), bitrate_kbps, gop_s
        options = {key: value for key, value in options.items() if value is not None}
        return self.telescope.send_command("setLiveViewOptions", [], options)

    def snapshot(self, current_id):
        return self.telescope.call("liveViewSnapshot", current_id)

    def start_recording(self, kind, interval_s=None):
        return self.telescope.call("startRecording", kind, interval_s)

    def stop_recording(self):
        return self.telescope.call("stopRecording")

    def recording_status(self):
        return self.telescope.call("getRecordingStatus")

    def stats(self):
        return self.telescope.call("getLiveViewStats")


class UploadController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def queue(self):
        return self.telescope.call("getUploadQueue")

    def upload_pending(self, kind="raw"):
        return self.telescope.call("uploadPending", kind)

    def retry_failed(self):
        return self.telescope.call("retryFailedUploads")

    def upload_offline(self, current_id):
        return self.telescope.call("uploadOfflinePhotos", current_id)


class DeviceController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def client_status(self):
        return self.telescope.call("getClientStatus")

    def set_clock(self, utc):
        return self.telescope.call("setClock", utc)

    def set_horizon(self, min_alt_deg=None, profile=None, stop_when_set=None):
        return self.telescope.call("setHorizon", min_alt_deg, profile, stop_when_set)

    def esp_info(self):
        return self.telescope.call("espInfo")

    def esp_driver_status(self):
        return self.telescope.call("espDriverStatus")

    def update_client(self):
        return self.telescope.call("updateClient")

    def update_firmware(self):
        return self.telescope.call("updateFirmware")


class CapturePlanController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def start(self, plan):
        return self.telescope.call("startCapturePlan", plan)

    def status(self, plan_id=None):
        return self.telescope.call("getCapturePlanStatus", plan_id)

    def pause(self):
        return self.telescope.call("pauseCapturePlan")

    def resume(self):
        return self.telescope.call("resumeCapturePlan")

    def skip_step(self):
        return self.telescope.call("skipCaptureStep")

    def stop(self):
        return self.telescope.call("stopCapturePlan")


class MotorController:
    def __init__(self, telescope: 'Telescope'):
        self.telescope = telescope

    def enable(self, on, motor_id="motor1"):
        # Enable/disable the stepper driver for a specific motor
        return self.telescope.send_command("espEnable", args=[bool(on)], kwargs={"motor_id": motor_id})

    def set_direction(self, forward, motor_id="motor1"):
        # Set motor direction, True=forward, False=reverse
        return self.telescope.send_command("espSetDirection", args=[bool(forward)], kwargs={"motor_id": motor_id})

    def set_speed(self, sps, motor_id="motor1"):
        # Set continuous target speed
        return self.telescope.send_command("espSetSpeed", args=[float(sps)], kwargs={"motor_id": motor_id})

    def start(self, sps, forward=None, motor_id="motor1"):
        # Start continuous rotation
        args = [float(sps)]
        if forward is not None:
            args.append(bool(forward))
        return self.telescope.send_command("espStart", args=args, kwargs={"motor_id": motor_id})

    def move_steps(self, steps, sps=None, forward=None, motor_id="motor1"):
        # Move a finite number of steps
        args = [int(steps)]
        if sps is not None:
            args.append(float(sps))
        if forward is not None:
            args.append(bool(forward))
        return self.telescope.send_command("espMoveSteps", args=args, kwargs={"motor_id": motor_id})

    def stop(self, motor_id="motor1"):
        # Stop motion and disable driver for a specific motor
        return self.telescope.send_command("espStop", args=[], kwargs={"motor_id": motor_id})

    def set_microsteps(self, value, motor_id="motor1"):
        # Set TMC2209 microstepping value (e.g., 16, 32) for a specific motor
        return self.telescope.send_command("espSetMicrosteps", args=[int(value)], kwargs={"motor_id": motor_id})

    def set_current(self, mA, motor_id="motor1"):
        # Set RMS motor current in milliamps for a specific motor
        return self.telescope.send_command("espSetCurrent", args=[int(mA)], kwargs={"motor_id": motor_id})

    def set_mode(self, mode, motor_id="motor1"):
        # Set chopper mode stealth or spread for a motor
        return self.telescope.send_command("espSetMode", args=[str(mode)], kwargs={"motor_id": motor_id})

    def set_accel(self, sps2, motor_id="motor1"):
        # Set acceleration in steps/sec^2 for ramping for a specific motor
        return self.telescope.send_command("espSetAccel", args=[float(sps2)], kwargs={"motor_id": motor_id})

    def status(self, motor_id="motor1"):
        # Query ESP32 firmware status for a specific motor
        return self.telescope.send_command("espStatus", args=[], kwargs={"motor_id": motor_id})

    def status_all(self):
        # Query status of all motors on the ESP32
        return self.telescope.send_command("espStatusAll", args=[])

    def get_current_coordinates(self):
        # Get current telescope coordinates (right ascension and declination)
        return self.telescope.send_command("getCurrentCoordinates", args=[])


# Convenience helper
def current_telescope() -> Telescope:
    # Getter for current telescope object in session
    return Telescope.from_session()
