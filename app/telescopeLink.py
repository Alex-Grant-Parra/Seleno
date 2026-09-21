import requests
import ujson
import asyncio
import websockets
import subprocess
import os
from flask import session, has_request_context
from app.WebsocketServer import clients
from time import sleep
from PIL import Image
import io

# Determine the correct URL for /sendCommand
# Use localhost for internal calls to avoid Cloudflare challenges
flask_port = os.getenv("FLASK_PORT", "5000")
app_domain = os.getenv("APP_DOMAIN", "seleno.org")

url = f"http://localhost:{flask_port}/sendCommand"
external_domain = f"https://{app_domain}"


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

    # Framework to send a command over the websocket
    def send_command(self, command: str, args=None, kwargs=None):
        payload = {"client_id": self.client_id, "command": command}
        if args is not None:
            payload["args"] = args
        if kwargs is not None:
            payload["kwargs"] = kwargs
        try:
            resp_text = requests.post(url, json=payload, timeout=8).text
            data = ujson.loads(resp_text)
            return data.get("result", data)
        except Exception as e:
            return {"error": str(e)}

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

    def capture_photo(self, current_id):
        return self.telescope.send_command("capturePhoto", current_id)

    def start_live_view(self):
        return self.telescope.send_command("startLiveView")

    def stop_live_view(self):
        return self.telescope.send_command("stopLiveView")

    # Starts LiveView
    def stream_live_view(self):
        # Maximum allowed websocket frame size
        MAX_FRAME_SIZE = 2 * 1024 * 1024 - 10240

        # Attempt to compress JPEG frames until they fit under MAX_FRAME_SIZE.
        # Returns the original data if already small enough, a compressed
        # bytes object if compression succeeds, or None if still too large.
        def compress_frame_if_needed(frame_data: bytes):
            if len(frame_data) <= MAX_FRAME_SIZE:
                return frame_data
            try:
                image = Image.open(io.BytesIO(frame_data))
                # Try progressively lower JPEG quality levels until the resulting bytes are small enough to send.
                for quality in [85, 70, 55, 40, 25]:
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=quality, optimize=True)
                    compressed = output.getvalue()
                    if len(compressed) <= MAX_FRAME_SIZE:
                        print(f"[LiveView] Compressed frame {len(frame_data)} -> {len(compressed)} bytes (q={quality})")
                        return compressed
                # Could not compress below threshold; skip this frame.
                print(f"[LiveView] Frame still too large ({len(frame_data)} bytes), skipping")
                return None
            except Exception as e:
                # If image parsing or compression fails, log and skip
                print(f"[LiveView] Compression error: {e}")
                return None

        # Read frames from gphoto2 stdout and stream them over a websocket.
        async def send_frames():
            api_token = load_api_token()
            if not api_token:
                print("[LiveView] No API token found")
                return

            # Build LiveView websocket URL and connect.
            lv_domain = os.getenv("APP_DOMAIN", "seleno.org")
            uri = f"wss://liveview.{lv_domain}"
            async with websockets.connect(uri, max_size=2*1024*1024) as ws:
                # Authenticate the live view connection with token + client id.
                await ws.send(ujson.dumps({"token": api_token, "client_id": self.telescope.client_id}))

                # Spawn gphoto2 to capture continuous movie frames to stdout.
                proc = subprocess.Popen(["gphoto2", "--capture-movie", "--stdout"], stdout=subprocess.PIPE)
                frame_buffer = b""
                try:
                    while True:
                        # Read a chunk from the camera process stdout.
                        chunk = proc.stdout.read(1024 * 32)
                        if not chunk:
                            break
                        frame_buffer += chunk

                        # JPEG frames end with the 0xFFD9 marker; extract complete
                        # frames as they arrive and process/send them.
                        while b'\xff\xd9' in frame_buffer:
                            end_pos = frame_buffer.find(b'\xff\xd9') + 2
                            complete = frame_buffer[:end_pos]
                            frame_buffer = frame_buffer[end_pos:]
                            processed = compress_frame_if_needed(complete)
                            if processed:
                                try:
                                    # Send raw/compressed bytes over websocket.
                                    await ws.send(processed)
                                except websockets.exceptions.ConnectionClosed:
                                    print("[LiveView] Connection closed during send")
                                    return
                                except Exception as e:
                                    print(f"[LiveView] Send error: {e}")
                finally:
                    proc.terminate()
        asyncio.run(send_frames())


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