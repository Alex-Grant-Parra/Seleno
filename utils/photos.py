"""Photos taken from the front panel, saved on the Pi.

Each photo lands in <folder>/<night>/, where the folder is photos/offline
unless `photos.local_dir` in config/client_config.json says otherwise, and
the night is the date the evening started (a session past midnight stays in
one folder). photos.jsonl beside them records when each was taken, what the
telescope was pointing at and where.

These never go to the server. Photos the server asks for
(utils/handlers.capturePhoto) still upload as before.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

DEFAULT_DIR = os.path.join("photos", "offline")
LOG_NAME = "photos.jsonl"
MAX_SHOTS = 50


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


def local_dir() -> str:
	return str(_settings().get("local_dir") or DEFAULT_DIR)


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


def _slug(name: str) -> str:
	return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:32] or "seleno"


def _pointing() -> Dict[str, Any]:
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


def take_photo() -> List[str]:
	"""Capture one photo (the camera may give a RAW and a JPEG) into tonight's
	folder; returns the saved paths. Raises RuntimeError with a short reason."""
	from core.camera.controller import Camera
	from utils.camera_state import camera_state
	from utils.liveview_state import load_liveview_state

	if not camera_state.is_available():
		raise RuntimeError("Camera not found")
	pointing = _pointing()
	folder = night_folder()
	with camera_state.get_command_lock():
		if load_liveview_state():
			camera_state.pause_liveview_for_command()
		try:
			names = Camera.capturePhoto(save_folder=folder, currentid=_slug(str(pointing.get("target") or "seleno")))
		except Exception as exc:
			raise RuntimeError(str(exc)[:80]) from exc
	paths = [os.path.join(folder, n) for n in names or [] if os.path.exists(os.path.join(folder, n))]
	if not paths:
		raise RuntimeError("The camera saved nothing")
	record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"files": [os.path.basename(p) for p in paths], **pointing}
	try:
		with open(os.path.join(folder, LOG_NAME), "a") as f:
			f.write(json.dumps(record) + "\n")
	except OSError as exc:
		print(f"[photos] Could not write the photo log: {exc}")
	print(f"[photos] Saved {', '.join(paths)}")
	return paths


class PhotoRun:
	"""A run of photos in the background, one after another."""

	def __init__(self, capture: Callable[[], List[str]] = take_photo) -> None:
		self._capture = capture
		self._thread: Optional[threading.Thread] = None
		self._stop = threading.Event()
		self.progress = ""

	@property
	def running(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def start(self, count: int, notify: Callable[[str], None]) -> str:
		if self.running:
			return "Already taking photos"
		self._stop.clear()
		self._thread = threading.Thread(target=self._run, args=(count, notify), name="panel-photos", daemon=True)
		self._thread.start()
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
			print(f"[photos] LEDs: {exc}")

	def _run(self, count: int, notify: Callable[[str], None]) -> None:
		saved = 0
		try:
			for n in range(1, count + 1):
				if self._stop.is_set():
					break
				self.progress = f"{n} of {count}" if count > 1 else "busy"
				self._leds(lambda leds: (leds.set_command_busy(True), leds.flash_capture_started()))
				try:
					paths = self._capture()
				except Exception as exc:
					self._leds(lambda leds: leds.flash_command_complete(False))
					notify(f"Photo failed: {exc}")
					return
				saved += 1
				self._leds(lambda leds: leds.flash_command_complete(True))
				if count == 1:
					notify(f"Saved {os.path.basename(paths[-1])}")
			if count > 1:
				notify(f"Saved {saved} of {count} photos")
		finally:
			self.progress = ""
			self._leds(lambda leds: leds.set_command_busy(False))
