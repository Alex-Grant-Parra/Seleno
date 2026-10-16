"""Ways of talking to the camera, for core/camera/service.py.

* LibBackend: libgphoto2 in this process (the gphoto2 Python package).
  The camera stays open; preview frames are asked for one at a time;
  photos and settings use the same session. Nothing to kill, no USB
  claim to fight over.
* CliBackend: the gphoto2 command. Live view is `gphoto2 --capture-movie
  --stdout`, read and split into frames (core/camera/jpeg.py); a photo or
  a setting stops it first. Only the service starts gphoto2, so nothing
  else holds the camera.
* FakeBackend: a made-up camera with a star field, for tests and for
  trying the client without one (camera.backend = "fake").

Every backend raises CameraError with a short reason.
"""
from __future__ import annotations

import io
import math
import os
import random
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.camera.jpeg import JpegFramer
from utils.log import get_logger

log = get_logger("camera")


class CameraError(RuntimeError):
	pass


class CameraGone(CameraError):
	"""The camera isn't there any more (unplugged, switched off)."""


# Settings by what they're for: the names cameras use for them.
SETTING_ALIASES: Dict[str, Tuple[str, ...]] = {
	"shutterspeed": ("shutterspeed", "shutterspeed2", "exposuretime"),
	"iso": ("iso", "isospeed"),
	"aperture": ("aperture", "f-number", "fnumber"),
	"whitebalance": ("whitebalance",),
	"imageformat": ("imageformat", "imagequality", "imagesize"),
	"exposurecompensation": ("exposurecompensation", "exposurecomp", "exposurebiascompensation"),
	"batterylevel": ("batterylevel", "battery"),
	"autopoweroff": ("autopoweroff", "autooff", "autopoweroffdelay"),
	"zoom": ("eoszoom", "liveviewzoomratio", "zoom"),
	"zoomposition": ("eoszoomposition",),
	"capturetarget": ("capturetarget",),
}


def _never_choice(choices: List[str]) -> Optional[str]:
	for want in ("never", "off", "disable", "disabled", "0", "none"):
		for c in choices:
			if c.strip().lower() == want:
				return c
	return None


# --- libgphoto2 ----------------------------------------------------------------------

class LibBackend:
	name = "libgphoto2"

	def __init__(self) -> None:
		import gphoto2 as gp  # noqa: F401 (fails here if it isn't installed)

		self._gp = gp
		self.camera = None
		self.model = ""

	def _wrap(self, exc: Exception) -> CameraError:
		gp = self._gp
		code = getattr(exc, "code", None)
		gone = code in (gp.GP_ERROR_IO_USB_FIND, gp.GP_ERROR_MODEL_NOT_FOUND, gp.GP_ERROR_IO,
			gp.GP_ERROR_IO_READ, gp.GP_ERROR_IO_WRITE, gp.GP_ERROR_IO_INIT)
		return (CameraGone if gone else CameraError)(str(exc).strip() or type(exc).__name__)

	def detect(self) -> Optional[str]:
		try:
			cameras = list(self._gp.Camera.autodetect())
		except self._gp.GPhoto2Error:
			return None
		return cameras[0][0] if cameras else None

	def open(self) -> None:
		gp = self._gp
		try:
			camera = gp.Camera()
			camera.init()
		except gp.GPhoto2Error as exc:
			raise self._wrap(exc)
		self.camera = camera
		try:
			self.model = camera.get_abilities().model
		except Exception:
			self.model = self.detect() or "camera"

	def close(self) -> None:
		if self.camera is not None:
			try:
				self.camera.exit()
			except Exception:
				pass
		self.camera = None

	def preview_start(self) -> None:
		pass  # the first capture_preview() starts it

	def preview_frame(self, timeout: float) -> Optional[bytes]:
		gp = self._gp
		try:
			cam_file = self.camera.capture_preview()
			data = cam_file.get_data_and_size()
		except gp.GPhoto2Error as exc:
			raise self._wrap(exc)
		return bytes(memoryview(data))

	def preview_stop(self) -> None:
		# Drop the mirror and end live view, where the camera has a switch.
		for name in ("viewfinder", "eosviewfinder"):
			try:
				self.set_config(name, 0)
				return
			except CameraError:
				continue

	def _widget(self, name: str):
		gp = self._gp
		try:
			return self.camera.get_single_config(name)
		except gp.GPhoto2Error as exc:
			raise CameraError(f"no setting {name}") from exc

	def get_config(self, name: str) -> Dict[str, Any]:
		widget = self._widget(name)
		choices: List[str] = []
		try:
			choices = [widget.get_choice(i) for i in range(widget.count_choices())]
		except Exception:
			pass
		value = widget.get_value()
		return {"name": name, "value": value, "choices": choices}

	def set_config(self, name: str, value: Any) -> None:
		gp = self._gp
		widget = self._widget(name)
		try:
			kind = widget.get_type()
			if kind in (gp.GP_WIDGET_TOGGLE, gp.GP_WIDGET_DATE):
				value = int(value)
			elif kind == gp.GP_WIDGET_RANGE:
				value = float(value)
			else:
				value = str(value)
			widget.set_value(value)
			self.camera.set_single_config(name, widget)
		except gp.GPhoto2Error as exc:
			raise self._wrap(exc)

	def list_config(self) -> List[str]:
		gp = self._gp
		names: List[str] = []

		def walk(widget) -> None:
			for i in range(widget.count_children()):
				child = widget.get_child(i)
				if child.get_type() in (gp.GP_WIDGET_SECTION, gp.GP_WIDGET_WINDOW):
					walk(child)
				else:
					names.append(child.get_name())
		try:
			walk(self.camera.get_config())
		except gp.GPhoto2Error as exc:
			raise self._wrap(exc)
		return names

	def _download(self, path, folder: str, base: str) -> str:
		gp = self._gp
		ext = os.path.splitext(path.name)[1].lower() or ".jpg"
		target = os.path.join(folder, base + ext)
		n = 1
		while os.path.exists(target):
			target = os.path.join(folder, f"{base}-{n}{ext}")
			n += 1
		cam_file = self.camera.file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
		cam_file.save(target)
		try:
			self.camera.file_delete(path.folder, path.name)
		except gp.GPhoto2Error:
			pass  # some cameras only keep it on the card; that's fine
		return os.path.basename(target)

	def _bulb(self, seconds: float) -> None:
		# The shutter speed has to be "bulb" first.
		try:
			info = self.get_config("shutterspeed")
			bulb = next((c for c in info["choices"] if "bulb" in c.lower()), None)
			if bulb and info["value"] != bulb:
				self.set_config("shutterspeed", bulb)
		except CameraError:
			pass
		for name, press, release in (("bulb", 1, 0), ("eosremoterelease", "Press Full", "Release Full")):
			try:
				self.set_config(name, press)
			except CameraError:
				continue
			time.sleep(seconds)
			self.set_config(name, release)
			return
		raise CameraError("This camera has no bulb control")

	def capture(self, folder: str, base: str, bulb_s: Optional[float] = None) -> List[str]:
		gp = self._gp
		os.makedirs(folder, exist_ok=True)
		paths = []
		try:
			if bulb_s:
				self._bulb(bulb_s)
			else:
				paths.append(self.camera.capture(gp.GP_CAPTURE_IMAGE))
			# RAW + JPEG: the second file (or, after bulb, both) arrive as events.
			deadline = time.monotonic() + (bulb_s + 30.0 if bulb_s else 3.0)
			while time.monotonic() < deadline:
				kind, data = self.camera.wait_for_event(500)
				if kind == gp.GP_EVENT_FILE_ADDED:
					paths.append(data)
					deadline = max(deadline, time.monotonic() + 1.0)
				elif kind == gp.GP_EVENT_CAPTURE_COMPLETE and paths:
					deadline = min(deadline, time.monotonic() + 1.0)
				elif kind == gp.GP_EVENT_TIMEOUT and paths:
					break
			return [self._download(p, folder, base) for p in paths]
		except gp.GPhoto2Error as exc:
			raise self._wrap(exc)

	def storage(self) -> Dict[str, Any]:
		gp = self._gp
		try:
			infos = self.camera.get_storageinfo()
		except gp.GPhoto2Error as exc:
			raise self._wrap(exc)
		free_kb = sum(getattr(s, "freekbytes", 0) for s in infos)
		total_kb = sum(getattr(s, "capacitykbytes", 0) for s in infos)
		free_images = sum(max(0, getattr(s, "freeimages", 0)) for s in infos)
		return {"free_mb": round(free_kb / 1024.0, 1), "total_mb": round(total_kb / 1024.0, 1),
			"free_images": free_images or None}


# --- the gphoto2 command ------------------------------------------------------------

class CliBackend:
	name = "cli"

	def __init__(self, binary: str = "gphoto2") -> None:
		self.binary = shutil.which(binary) or binary
		self.model = ""
		self._proc: Optional[subprocess.Popen] = None
		self._reader: Optional[threading.Thread] = None
		self._cond = threading.Condition()
		self._latest: Optional[bytes] = None
		self._seq = 0
		self._taken = 0
		self._stderr = b""

	def _run(self, args: List[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
		try:
			result = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=timeout)
		except subprocess.TimeoutExpired as exc:
			raise CameraError(f"gphoto2 {args[0]} timed out") from exc
		except OSError as exc:
			raise CameraError(f"gphoto2 not found: {exc}") from exc
		if result.returncode != 0:
			message = (result.stderr or result.stdout).strip().splitlines()
			text = next((m for m in reversed(message) if m.strip()), "gphoto2 failed")
			lowered = (result.stderr or "").lower()
			if "could not claim the usb device" in lowered:
				_release_usb_lockers()
			gone = any(s in lowered for s in ("no camera found", "could not detect", "model not found",
				"could not find the requested device", "i/o problem"))
			raise (CameraGone if gone else CameraError)(text.strip()[:160])
		return result

	def detect(self) -> Optional[str]:
		try:
			out = self._run(["--auto-detect"], timeout=5.0).stdout
		except CameraError:
			return None
		for line in out.splitlines()[2:]:
			if "usb:" in line.lower():
				return line.split("usb:")[0].strip() or "camera"
		return None

	def open(self) -> None:
		model = self.detect()
		if not model:
			raise CameraGone("No camera found")
		self.model = model

	def close(self) -> None:
		self.preview_stop()

	def preview_start(self) -> None:
		self.preview_stop()
		try:
			self._run(["--set-config", "viewfinder=1"], timeout=5.0)
		except CameraError:
			pass  # not every camera has (or needs) it
		try:
			self._proc = subprocess.Popen([self.binary, "--capture-movie", "--stdout"],
				stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		except OSError as exc:
			raise CameraError(f"gphoto2 not found: {exc}") from exc
		self._stderr = b""
		self._reader = threading.Thread(target=self._read, args=(self._proc,), name="camera-movie", daemon=True)
		self._reader.start()

	def _read(self, proc: subprocess.Popen) -> None:
		framer = JpegFramer()
		stream = proc.stdout
		try:
			while True:
				chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
				if not chunk:
					break
				for frame in framer.feed(chunk):
					with self._cond:
						self._latest = frame
						self._seq += 1
						self._cond.notify_all()
		except (OSError, ValueError):
			pass
		try:
			self._stderr = proc.stderr.read() or b""
		except Exception:
			pass
		with self._cond:
			self._cond.notify_all()

	def preview_frame(self, timeout: float) -> Optional[bytes]:
		proc = self._proc
		if proc is None:
			raise CameraError("Live view isn't running")
		with self._cond:
			if self._seq == self._taken:
				self._cond.wait(timeout)
			if self._seq != self._taken:
				self._taken = self._seq
				return self._latest
		if proc.poll() is not None:
			if self._reader is not None:
				self._reader.join(timeout=1.0)
			text = self._stderr.decode("utf-8", "replace").strip().splitlines()
			reason = next((t for t in reversed(text) if t.strip()), f"gphoto2 exited ({proc.returncode})")
			self._proc = None
			if "could not claim the usb device" in self._stderr.decode("utf-8", "replace").lower():
				_release_usb_lockers()
			raise CameraError(reason.strip()[:160])
		return None

	def preview_stop(self) -> None:
		proc, self._proc = self._proc, None
		if proc is None:
			return
		if proc.poll() is None:
			try:
				proc.send_signal(signal.SIGINT)  # lets gphoto2 end live view cleanly
				proc.wait(timeout=3.0)
			except Exception:
				try:
					proc.kill()
					proc.wait(timeout=2.0)
				except Exception:
					pass
		if self._reader is not None:
			self._reader.join(timeout=1.0)
		self._reader = None
		try:
			self._run(["--set-config", "viewfinder=0"], timeout=5.0)
		except CameraError:
			pass

	def get_config(self, name: str) -> Dict[str, Any]:
		out = self._run(["--get-config", name], timeout=10.0).stdout
		value, choices = None, []
		for line in out.splitlines():
			line = line.strip()
			if line.startswith("Current:"):
				value = line.split(":", 1)[1].strip()
			elif line.startswith("Choice:"):
				parts = line.split(" ", 2)
				if len(parts) == 3:
					choices.append(parts[2])
		return {"name": name, "value": value, "choices": choices}

	def set_config(self, name: str, value: Any) -> None:
		self._run(["--set-config-value", f"{name}={value}"], timeout=10.0)

	def list_config(self) -> List[str]:
		out = self._run(["--list-config"], timeout=15.0).stdout
		return [line.strip().rsplit("/", 1)[-1] for line in out.splitlines() if line.strip().startswith("/")]

	def capture(self, folder: str, base: str, bulb_s: Optional[float] = None) -> List[str]:
		os.makedirs(folder, exist_ok=True)
		pattern = os.path.join(folder, f"{base}.%C")
		if bulb_s:
			args = ["--set-config-value", "shutterspeed=bulb", "--set-config", "bulb=1",
				f"--wait-event={int(math.ceil(bulb_s))}s", "--set-config", "bulb=0",
				"--wait-event-and-download=30s", "--filename", pattern, "--force-overwrite"]
			timeout = bulb_s + 60.0
		else:
			args = ["--capture-image-and-download", "--filename", pattern, "--force-overwrite"]
			timeout = 60.0
		out = self._run(args, timeout=timeout).stdout
		saved = [os.path.basename(line.split("Saving file as", 1)[1].strip())
			for line in out.splitlines() if "Saving file as" in line]
		if not saved:
			raise CameraError("The camera saved nothing")
		return saved

	def storage(self) -> Dict[str, Any]:
		out = self._run(["--storage-info"], timeout=10.0).stdout
		free_kb = total_kb = free_images = 0
		for line in out.splitlines():
			key, _, value = line.strip().partition("=")
			if not value.strip().lstrip("-").isdigit():
				continue
			if key == "free":
				free_kb += int(value)
			elif key == "totalcapacity":
				total_kb += int(value)
			elif key == "freeimages":
				free_images += max(0, int(value))
		return {"free_mb": round(free_kb / 1024.0, 1), "total_mb": round(total_kb / 1024.0, 1),
			"free_images": free_images or None}


def _release_usb_lockers() -> None:
	"""The desktop's camera volume monitor grabs cameras as they're plugged
	in; gphoto2 then can't. Never our own gphoto2: the service owns those."""
	for pattern in ("gvfs-gphoto2-volume-monitor", "gvfsd-gphoto2"):
		try:
			subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=2)
		except Exception:
			pass


# --- a made-up camera ------------------------------------------------------------------

class FakeBackend:
	"""A camera that isn't there: a drifting star field and photos as
	files. `present` can be switched off to unplug it."""

	name = "fake"

	def __init__(self, width: int = 1024, height: int = 680, fps: float = 15.0, seed: int = 7) -> None:
		self.width, self.height, self.fps = width, height, fps
		self.present = True
		self.model = "Fake Camera"
		self.fail_next_frames = 0
		self.preview_running = False
		self.captures = 0
		self.config: Dict[str, Dict[str, Any]] = {
			"shutterspeed": {"value": "1/30", "choices": ["bulb", "30", "15", "1", "1/30", "1/250"]},
			"iso": {"value": "800", "choices": ["100", "400", "800", "1600", "3200"]},
			"aperture": {"value": "5.6", "choices": ["5.6", "8"]},
			"whitebalance": {"value": "Auto", "choices": ["Auto", "Daylight"]},
			"imageformat": {"value": "RAW + Large Fine JPEG", "choices": ["Large Fine JPEG", "RAW", "RAW + Large Fine JPEG"]},
			"exposurecompensation": {"value": "0", "choices": ["-1", "0", "1"]},
			"batterylevel": {"value": "75%", "choices": []},
			"autopoweroff": {"value": "30", "choices": ["0", "1", "30"]},
			"eoszoom": {"value": "1", "choices": ["1", "5", "10"]},
		}
		rng = random.Random(seed)
		self._stars = [(rng.uniform(0, width), rng.uniform(0, height), rng.choice([1, 1, 1, 2, 3]),
			rng.randint(120, 255)) for _ in range(120)]
		self._t0 = time.monotonic()
		self._last = 0.0
		self.blur = 0.0  # focus: 0 sharp

	def _check(self) -> None:
		if not self.present:
			raise CameraGone("No camera found")

	def detect(self) -> Optional[str]:
		return self.model if self.present else None

	def open(self) -> None:
		self._check()

	def close(self) -> None:
		self.preview_running = False

	def preview_start(self) -> None:
		self._check()
		self.preview_running = True

	def render(self, drift: float = 0.0) -> bytes:
		from PIL import Image, ImageDraw, ImageFilter

		img = Image.new("L", (self.width, self.height), 12)
		draw = ImageDraw.Draw(img)
		for x, y, r, level in self._stars:
			x = (x + drift) % self.width
			draw.ellipse((x - r, y - r, x + r, y + r), fill=level)
		radius = 0.8 + self.blur
		img = img.filter(ImageFilter.GaussianBlur(radius)).convert("RGB")
		buf = io.BytesIO()
		img.save(buf, "JPEG", quality=85)
		return buf.getvalue()

	def preview_frame(self, timeout: float) -> Optional[bytes]:
		self._check()
		if not self.preview_running:
			raise CameraError("Live view isn't running")
		if self.fail_next_frames > 0:
			self.fail_next_frames -= 1
			raise CameraError("Fake failure")
		wait = 1.0 / self.fps - (time.monotonic() - self._last)
		if wait > 0:
			time.sleep(min(wait, timeout))
		self._last = time.monotonic()
		zoom = float(self.config["eoszoom"]["value"] or 1)
		return self.render(drift=(time.monotonic() - self._t0) * 0.5 * zoom)

	def preview_stop(self) -> None:
		self.preview_running = False

	def get_config(self, name: str) -> Dict[str, Any]:
		self._check()
		if name not in self.config:
			raise CameraError(f"no setting {name}")
		return {"name": name, **self.config[name]}

	def set_config(self, name: str, value: Any) -> None:
		self._check()
		if name not in self.config:
			raise CameraError(f"no setting {name}")
		choices = self.config[name]["choices"]
		if choices and str(value) not in choices:
			raise CameraError(f"{value} isn't one of {name}'s choices")
		self.config[name]["value"] = str(value)

	def list_config(self) -> List[str]:
		self._check()
		return list(self.config)

	def capture(self, folder: str, base: str, bulb_s: Optional[float] = None) -> List[str]:
		self._check()
		os.makedirs(folder, exist_ok=True)
		if bulb_s:
			time.sleep(min(bulb_s, 0.05))
		self.captures += 1
		names = []
		fmt = self.config["imageformat"]["value"]
		if "JPEG" in fmt:
			with open(os.path.join(folder, base + ".jpg"), "wb") as f:
				f.write(self.render())
			names.append(base + ".jpg")
		if "RAW" in fmt:
			with open(os.path.join(folder, base + ".cr2"), "wb") as f:
				f.write(b"FAKE RAW " * 1000)
			names.append(base + ".cr2")
		return names

	def storage(self) -> Dict[str, Any]:
		self._check()
		return {"free_mb": 12000.0, "total_mb": 30000.0, "free_images": 900}


def make_backend(kind: str = "auto"):
	"""The backend for config camera.backend."""
	kind = (kind or "auto").lower()
	if kind == "fake":
		return FakeBackend()
	if kind in ("auto", "libgphoto2", "lib"):
		try:
			return LibBackend()
		except ImportError:
			if kind != "auto":
				log.warning("The gphoto2 Python package isn't installed: using the gphoto2 command")
	return CliBackend()
