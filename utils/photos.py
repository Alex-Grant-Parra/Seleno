"""Photos taken from the front panel (and sequences), saved on the Pi.

Each photo lands in <folder>/<night>/, where the folder is photos/offline
unless `photos.local_dir` in config/client_config.json says otherwise, and
the night is the date the evening started (a session past midnight stays in
one folder). photos.jsonl beside them records when each was taken, what the
telescope was pointing at and where.

Photos the server asks for (utils/handlers.capturePhoto) go to
photos/default and upload through the queue (utils/uploads.py). Panel
photos stay on the Pi unless a photo owner id is set and they're sent with
"Upload offline photos".

Sequences: a run can be lights, darks, flats or bias frames, with an
exposure (bulb, in seconds) or the camera's own setting, a pause between
frames, and dithering (a small random shift of the pointing every few
frames, so stacking averages out fixed noise).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from utils.log import get_logger

log = get_logger("photos")

DEFAULT_BASE = "photos"
DEFAULT_DIR = os.path.join(DEFAULT_BASE, "offline")
SERVER_DIR = os.path.join(DEFAULT_BASE, "default")
LOG_NAME = "photos.jsonl"
MAX_SHOTS = 50
FRAME_KINDS = ("light", "dark", "flat", "bias")
MIN_FREE_MB = 300.0
WARN_FREE_MB = 1024.0


def _settings() -> Dict[str, Any]:
	from utils.config_state import load_static_state

	saved = load_static_state().get("photos", {})
	return saved if isinstance(saved, dict) else {}


def _save_setting(key: str, value: Any) -> None:
	from utils.config_state import load_static_state, save_static_state

	state = load_static_state()
	photos = state.get("photos") if isinstance(state.get("photos"), dict) else {}
	photos[key] = value
	state["photos"] = photos
	save_static_state(state)


def base_dir() -> str:
	return str(_settings().get("base_dir") or DEFAULT_BASE)


def local_dir() -> str:
	return str(_settings().get("local_dir") or DEFAULT_DIR)


def owner_id() -> str:
	"""The server account id offline photos are uploaded under ('' unset)."""
	value = str(_settings().get("owner_id") or "").strip()
	return value if value.isdigit() else ""


def set_owner_id(value: str) -> Optional[str]:
	value = str(value).strip()
	if value and not value.isdigit():
		return "The owner id is a number"
	_save_setting("owner_id", value)
	return None


def shots() -> int:
	try:
		return max(1, min(MAX_SHOTS, int(_settings().get("shots", 1))))
	except (TypeError, ValueError):
		return 1


def set_shots(count: float) -> Optional[str]:
	_save_setting("shots", max(1, min(MAX_SHOTS, int(count))))
	return None


def night_folder(now: Optional[datetime] = None) -> str:
	# Local date at the start of the evening: 01:30 on the 17th is the 16th's night.
	now = now or datetime.now()
	return os.path.join(local_dir(), (now - timedelta(hours=12)).date().isoformat())


def saved_count() -> int:
	count = 0
	for _root, _dirs, files in os.walk(local_dir()):
		count += sum(1 for f in files if f != LOG_NAME and not f.startswith("."))
	return count


def disk_free_mb(path: str = ".") -> float:
	try:
		return shutil.disk_usage(path).free / (1024.0 * 1024.0)
	except OSError:
		return float("inf")


def disk_text() -> str:
	free = disk_free_mb()
	if free == float("inf"):
		return "?"
	return f"{free / 1024:.1f} GB free" if free >= 1024 else f"{free:.0f} MB free"


def _slug(name: str) -> str:
	return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:32] or "seleno"


def pointing() -> Dict[str, Any]:
	"""What the telescope is pointing at, for a photo's record."""
	info: Dict[str, Any] = {}
	try:
		from core.hardware import tracking
		from utils.telescope_state import get_telescope_coords

		target = tracking._target_object if tracking._tracking_active else None
		if target:
			info["target"] = target.get("name")
			info["tracking"] = tracking.is_sidereal_tracking()
		coords = get_telescope_coords()
		if coords:
			info["ra"] = round(coords["right_ascension"], 5)
			info["dec"] = round(coords["declination"], 5)
	except Exception:
		pass
	try:
		from utils.location import get_current_location

		loc = get_current_location() or {}
		if loc.get("latitude") is not None:
			info["site"] = [loc.get("latitude"), loc.get("longitude")]
	except Exception:
		pass
	return info


_pointing = pointing  # the old name


def record(folder: str, names: List[str], info: Dict[str, Any]) -> None:
	"""Add a line to the folder's photos.jsonl."""
	entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": names, **info}
	try:
		os.makedirs(folder, exist_ok=True)
		with open(os.path.join(folder, LOG_NAME), "a") as f:
			f.write(json.dumps(entry) + "\n")
	except OSError as exc:
		log.warning("Could not write the photo log: %s", exc)


def check_disk() -> None:
	free = disk_free_mb()
	if free < MIN_FREE_MB:
		raise RuntimeError("The Pi's disk is nearly full")
	if free < WARN_FREE_MB:
		log.warning("The Pi has %.0f MB free", free)


def capture_to(folder: str, base: str, exposure_s: Optional[float] = None) -> List[str]:
	"""Take a photo through the camera service into folder; returns the
	saved paths."""
	from core.camera.backends import CameraError
	from core.camera.service import get_camera_service

	service = get_camera_service()
	if not service.is_available():
		raise RuntimeError("Camera not found")
	check_disk()
	try:
		names = service.capture(folder, base, bulb_s=exposure_s)
	except CameraError as exc:
		raise RuntimeError(str(exc)[:80]) from exc
	paths = [os.path.join(folder, n) for n in names or [] if os.path.exists(os.path.join(folder, n))]
	if not paths:
		raise RuntimeError("The camera saved nothing")
	return paths


def take_photo(kind: str = "light", exposure_s: Optional[float] = None) -> List[str]:
	"""Capture one photo (the camera may give a RAW and a JPEG) into tonight's
	folder; returns the saved paths. Raises RuntimeError with a short reason."""
	info = pointing()
	folder = night_folder()
	if kind != "light":
		folder = os.path.join(folder, kind + "s")
	label = kind if kind != "light" else str(info.get("target") or "seleno")
	base = f"{_slug(label)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
	paths = capture_to(folder, base, exposure_s)
	info = dict(info, kind=kind)
	if exposure_s:
		info["exposure_s"] = exposure_s
	record(folder, [os.path.basename(p) for p in paths], info)
	log.info("Saved %s", ", ".join(paths))
	return paths


@dataclass
class SequencePlan:
	kind: str = "light"            # light, dark, flat or bias
	count: int = 1
	exposure_s: Optional[float] = None   # None: the camera's shutter setting
	interval_s: float = 0.0        # pause between frames
	dither_every: int = 0          # frames between dithers (0: never)
	dither_arcmin: float = 2.0

	def check(self) -> Optional[str]:
		if self.kind not in FRAME_KINDS:
			return f"kind is one of {', '.join(FRAME_KINDS)}"
		if not 1 <= self.count <= 999:
			return "count is 1 to 999"
		if self.exposure_s is not None and not 0 < self.exposure_s <= 3600:
			return "exposure is up to 3600 s"
		if self.interval_s < 0 or self.dither_every < 0 or not 0 < self.dither_arcmin <= 30:
			return "interval and dither must be positive (dither up to 30 arcmin)"
		return None


class PhotoRun:
	"""A run of photos in the background, one after another."""

	def __init__(self, capture: Callable[[], List[str]] = take_photo) -> None:
		self._capture = capture
		self._thread: Optional[threading.Thread] = None
		self._stop = threading.Event()
		self.progress = ""
		self.plan: Optional[SequencePlan] = None
		self.saved = 0
		self.error = ""

	@property
	def running(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def start(self, count: int, notify: Callable[[str], None], plan: Optional[SequencePlan] = None) -> str:
		if self.running:
			return "Already taking photos"
		if plan is not None:
			problem = plan.check()
			if problem:
				return problem
			count = plan.count
		self.plan = plan
		self._stop.clear()
		self.saved = 0
		self.error = ""
		self._thread = threading.Thread(target=self._run, args=(count, notify), name="panel-photos", daemon=True)
		self._thread.start()
		if plan is not None and plan.kind != "light":
			return f"Taking {count} {plan.kind} frames"
		return "Taking a photo" if count == 1 else f"Taking {count} photos"

	def stop(self) -> str:
		self._stop.set()
		return "Stopping after this photo"

	@staticmethod
	def _leds(action: Callable[[Any], None]) -> None:
		# The LEDs are a nicety; a refused LED command mustn't stop the photos.
		try:
			from utils.LEDmanager import get_led_manager

			action(get_led_manager())
		except Exception as exc:
			log.debug("LEDs: %s", exc)

	def _one(self) -> List[str]:
		plan = self.plan
		if plan is None:
			return self._capture()
		return take_photo(plan.kind, plan.exposure_s)

	def _dither(self, n: int) -> None:
		plan = self.plan
		if plan is None or plan.kind != "light" or plan.dither_every <= 0 or n % plan.dither_every:
			return
		try:
			from core.hardware import tracking

			if tracking.is_sidereal_tracking():
				tracking.dither(plan.dither_arcmin)
		except Exception as exc:
			log.warning("Dither failed: %s", exc)

	def status(self) -> Dict[str, Any]:
		return {"running": self.running, "progress": self.progress, "saved": self.saved, "error": self.error,
			"plan": asdict(self.plan) if self.plan else None}

	def _run(self, count: int, notify: Callable[[str], None]) -> None:
		saved = 0
		try:
			for n in range(1, count + 1):
				if self._stop.is_set():
					break
				self.progress = f"{n} of {count}" if count > 1 else "busy"
				self._leds(lambda leds: (leds.set_command_busy(True), leds.flash_capture_started()))
				try:
					paths = self._one()
				except Exception as exc:
					self.error = str(exc)[:80]
					self._leds(lambda leds: leds.flash_command_complete(False))
					notify(f"Photo failed: {exc}")
					return
				saved += 1
				self.saved = saved
				self._leds(lambda leds: leds.flash_command_complete(True))
				if count == 1:
					notify(f"Saved {os.path.basename(paths[-1])}")
				if n < count:
					self._dither(n)
					if self.plan is not None and self.plan.interval_s > 0:
						self._stop.wait(self.plan.interval_s)
			if count > 1:
				notify(f"Saved {saved} of {count} photos")
		finally:
			self.progress = ""
			self._leds(lambda leds: leds.set_command_busy(False))
