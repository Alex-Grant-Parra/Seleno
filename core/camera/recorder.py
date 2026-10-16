"""Recording the live view, and snapshots of it.

* Video: every frame the camera sends, into an MJPEG AVI (core/camera/avi.py)
  in photos/recordings/<night>/ (no re-encoding, so nothing is lost). For
  the Moon and planets: stack the best frames afterwards in AutoStakkert
  or PIPP. A new file starts before one reaches 1 GB.
* Timelapse: one frame every interval_s, played back at 10 fps.
* Snapshot: the current live-view frame saved as a JPEG, at once, without
  the shutter (utils/handlers.liveViewSnapshot also uploads it).

Recording holds live view open (core/camera/service.py) until it stops.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from core.camera.avi import MAX_BYTES, MjpegAviWriter
from utils.log import get_logger

log = get_logger("recorder")
HOLDER = "recording"
TIMELAPSE_PLAYBACK_FPS = 10.0
MIN_FREE_MB = 300.0


def _night_folder(kind: str) -> str:
	from utils import photos

	night = os.path.basename(photos.night_folder())
	return os.path.join(photos.base_dir(), kind, night)


def disk_free_mb(path: str = ".") -> float:
	import shutil

	try:
		return shutil.disk_usage(path).free / (1024.0 * 1024.0)
	except OSError:
		return float("inf")


def snapshot(service=None, name: str = "snapshot") -> str:
	"""Save the newest live-view frame; returns its path. Raises
	RuntimeError when there's no recent frame."""
	if service is None:
		from core.camera.service import get_camera_service

		service = get_camera_service()
	_seq, frame, at = service.frames.latest()
	if frame is None or time.monotonic() - at > 10.0:
		raise RuntimeError("No live view frame: turn live view on first")
	folder = _night_folder("snapshots")
	os.makedirs(folder, exist_ok=True)
	path = os.path.join(folder, f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.jpg")
	with open(path, "wb") as f:
		f.write(frame)
	log.info("Snapshot saved: %s", path)
	return path


class Recorder:
	def __init__(self, service=None) -> None:
		self._service = service
		self._thread: Optional[threading.Thread] = None
		self._stop = threading.Event()
		self.kind = ""
		self.interval_s = 0.0
		self.path = ""
		self.frames = 0
		self.files = 0
		self.started_at = 0.0
		self.error = ""

	@property
	def service(self):
		if self._service is None:
			from core.camera.service import get_camera_service

			self._service = get_camera_service()
		return self._service

	@property
	def running(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def start(self, kind: str = "video", interval_s: float = 10.0, name: str = "") -> str:
		if self.running:
			return "Already recording"
		if kind not in ("video", "timelapse"):
			raise ValueError("kind is video or timelapse")
		if disk_free_mb() < MIN_FREE_MB:
			raise RuntimeError("The Pi's disk is nearly full")
		self.kind, self.interval_s = kind, max(0.5, float(interval_s))
		self.frames = self.files = 0
		self.error = ""
		self.name = name or kind
		self.started_at = time.monotonic()
		self._stop.clear()
		self.service.acquire_preview(HOLDER)
		self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
		self._thread.start()
		return "Recording video" if kind == "video" else f"Timelapse: a frame every {self.interval_s:.0f} s"

	def stop(self) -> str:
		if not self.running:
			return "Not recording"
		self._stop.set()
		self._thread.join(timeout=10.0)
		return f"Recording saved: {self.frames} frames"

	def _new_file(self) -> MjpegAviWriter:
		folder = _night_folder("recordings")
		os.makedirs(folder, exist_ok=True)
		stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
		self.path = os.path.join(folder, f"{self.name}_{stamp}.avi")
		self.files += 1
		fps = TIMELAPSE_PLAYBACK_FPS if self.kind == "timelapse" else max(1.0, self._fps_guess())
		log.info("Recording to %s", self.path)
		return MjpegAviWriter(self.path, fps)

	def _fps_guess(self) -> float:
		return float(self.service.status().get("camera_fps") or 10.0)

	def _run(self) -> None:
		writer: Optional[MjpegAviWriter] = None
		seq = 0
		last = 0.0
		try:
			while not self._stop.is_set():
				new_seq, frame, _at = self.service.frames.wait_newer(seq, 0.5)
				if new_seq == seq or frame is None:
					continue
				seq = new_seq
				now = time.monotonic()
				if self.kind == "timelapse" and now - last < self.interval_s:
					continue
				last = now
				if writer is None or writer.bytes_written > MAX_BYTES:
					if writer is not None:
						writer.close()
					if disk_free_mb() < MIN_FREE_MB:
						self.error = "Disk nearly full: recording stopped"
						log.warning(self.error)
						break
					writer = self._new_file()
				writer.add(frame)
				self.frames += 1
		except Exception as exc:
			self.error = str(exc)[:80]
			log.exception("Recording failed: %s", exc)
		finally:
			if writer is not None:
				writer.close()
			self.service.release_preview(HOLDER)
			log.info("Recording stopped: %d frames in %d file(s)", self.frames, self.files)

	def status(self) -> Dict[str, Any]:
		return {"recording": self.running, "kind": self.kind, "frames": self.frames, "files": self.files,
			"path": self.path, "seconds": round(time.monotonic() - self.started_at) if self.running else 0,
			"error": self.error}


_recorder: Optional[Recorder] = None


def get_recorder() -> Recorder:
	global _recorder
	if _recorder is None:
		_recorder = Recorder()
	return _recorder
