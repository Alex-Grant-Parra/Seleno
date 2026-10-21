"""Recording the live view, and snapshots of it.

* Video: every frame the camera sends, into an MJPEG AVI (core/camera/avi.py)
  in photos/recordings/<night>/ (no re-encoding, so nothing is lost). For
  the Moon and planets: stack the best frames afterwards in AutoStakkert
  or PIPP. A new file starts before one reaches 1 GB.
* Lossless video (quality "lossless"): a crop of the live view round the
  planet (the brightest thing in the first frame), decoded and written
  as uncompressed SER (core/camera/ser.py), for lucky imaging. The live
  view's own JPEG is the only compression the frames ever went through.
* Timelapse: one frame every interval_s, played back at 10 fps.
* With an account id, the files are queued for upload when recording
  stops, with what they show in their meta (crop, fps).
* Snapshot: the current live-view frame saved as a JPEG, at once, without
  the shutter (utils/handlers.liveViewSnapshot also uploads it).

Recording holds live view open (core/camera/service.py) until it stops.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.camera.avi import MAX_BYTES, MjpegAviWriter
from core.camera.ser import SerWriter
from utils.log import get_logger

log = get_logger("recorder")
HOLDER = "recording"
TIMELAPSE_PLAYBACK_FPS = 10.0
MIN_FREE_MB = 300.0
QUALITIES = ("standard", "lossless")
CROP_SIZE = (640, 480)


def decode_rgb(jpeg: bytes):
	"""A JPEG as a height x width x 3 uint8 array."""
	import io

	import numpy as np
	from PIL import Image

	return np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))


def planet_crop(rgb, width: int, height: int):
	"""(left, top, width, height) of a crop centred on the brightest blob
	(the planet: the centroid of what's over half its peak), inside the
	frame."""
	import numpy as np

	h, w = rgb.shape[:2]
	width, height = min(width, w) // 2 * 2, min(height, h) // 2 * 2
	grey = rgb.astype(np.float32).mean(axis=2)
	floor = float(np.median(grey))
	bright = grey > floor + 0.5 * (float(grey.max()) - floor)
	if grey.max() - floor < 10 or not bright.any():
		cx, cy = w / 2.0, h / 2.0
	else:
		ys, xs = np.nonzero(bright)
		cx, cy = float(xs.mean()), float(ys.mean())
	left = int(max(0, min(w - width, round(cx - width / 2))))
	top = int(max(0, min(h - height, round(cy - height / 2))))
	return left, top, width, height


def _utc_stamp() -> str:
	from utils import photos

	return photos.utc_stamp()


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
		self.started_utc = ""
		self.error = ""
		self.quality = "standard"
		self.owner = ""
		self.crop_size: Tuple[int, int] = CROP_SIZE
		self.crop: Optional[Dict[str, Any]] = None
		self.paths: List[str] = []

	@property
	def service(self):
		if self._service is None:
			from core.camera.service import get_camera_service

			self._service = get_camera_service()
		return self._service

	@property
	def running(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def start(self, kind: str = "video", interval_s: float = 10.0, name: str = "", quality: str = "standard",
			owner: str = "", crop_size: Optional[Tuple[int, int]] = None) -> str:
		"""quality: "standard" (MJPEG AVI) or "lossless" (SER of the crop
		round the planet, crop_size pixels; video only). owner: an account
		id to upload the files under when it stops."""
		if self.running:
			return "Already recording"
		if kind not in ("video", "timelapse"):
			raise ValueError("kind is video or timelapse")
		quality = (quality or "standard").lower()
		if quality not in QUALITIES:
			raise ValueError(f"quality is one of {', '.join(QUALITIES)}")
		if quality == "lossless" and kind != "video":
			raise ValueError("lossless is for video")
		if disk_free_mb() < MIN_FREE_MB:
			raise RuntimeError("The Pi's disk is nearly full")
		self.kind, self.interval_s = kind, max(0.5, float(interval_s))
		self.quality = quality
		self.owner = str(owner or "").strip()
		self.crop_size = (int(crop_size[0]), int(crop_size[1])) if crop_size else CROP_SIZE
		self.crop = None
		self.frames = self.files = 0
		self.paths = []
		self.error = ""
		self.name = name or kind
		self.started_at = time.monotonic()
		self.started_utc = _utc_stamp()
		self._stop.clear()
		self.service.acquire_preview(HOLDER)
		self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
		self._thread.start()
		if kind == "timelapse":
			return f"Timelapse: a frame every {self.interval_s:.0f} s"
		return "Recording lossless video" if quality == "lossless" else "Recording video"

	def stop(self) -> str:
		if not self.running:
			return "Not recording"
		self._stop.set()
		self._thread.join(timeout=10.0)
		return f"Recording saved: {self.frames} frames"

	def _new_path(self, ext: str) -> str:
		folder = _night_folder("recordings")
		os.makedirs(folder, exist_ok=True)
		stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
		self.path = os.path.join(folder, f"{self.name}_{stamp}{ext}")
		n = 1
		while os.path.exists(self.path):
			self.path = os.path.join(folder, f"{self.name}_{stamp}-{n}{ext}")
			n += 1
		self.files += 1
		self.paths.append(self.path)
		log.info("Recording to %s", self.path)
		return self.path

	def _new_file(self) -> MjpegAviWriter:
		path = self._new_path(".avi")
		fps = TIMELAPSE_PLAYBACK_FPS if self.kind == "timelapse" else max(1.0, self._fps_guess())
		return MjpegAviWriter(path, fps)

	def _new_ser(self, rgb) -> SerWriter:
		if self.crop is None:
			left, top, width, height = planet_crop(rgb, *self.crop_size)
			h, w = rgb.shape[:2]
			self.crop = {"left": left, "top": top, "width": width, "height": height,
				"x": round((left + width / 2) / w, 4), "y": round((top + height / 2) / h, 4)}
		path = self._new_path(".ser")
		return SerWriter(path, self.crop["width"], self.crop["height"], "RGB", 8,
			instrument=str(self.service.status().get("model") or "")[:40], telescope="SELENO")

	def _fps_guess(self) -> float:
		return float(self.service.status().get("camera_fps") or 10.0)

	def _write(self, writer, frame: bytes):
		"""Add the frame, starting a new file when there's none or it's
		full; returns the writer (None: stopped for the disk)."""
		lossless = self.quality == "lossless"
		rgb = decode_rgb(frame) if lossless else None
		if writer is None or writer.bytes_written > MAX_BYTES:
			if writer is not None:
				writer.close()
			if disk_free_mb() < MIN_FREE_MB:
				self.error = "Disk nearly full: recording stopped"
				log.warning(self.error)
				return None
			writer = self._new_ser(rgb) if lossless else self._new_file()
		if lossless:
			c = self.crop
			patch = rgb[c["top"]:c["top"] + c["height"], c["left"]:c["left"] + c["width"]]
			if patch.shape[0] != c["height"] or patch.shape[1] != c["width"]:
				return writer  # the frame changed size (zoom): skip it
			writer.add(patch.tobytes())
		else:
			writer.add(frame)
		return writer

	def _run(self) -> None:
		writer = None
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
				writer = self._write(writer, frame)
				if writer is None:
					break
				self.frames += 1
		except Exception as exc:
			self.error = str(exc)[:80]
			log.exception("Recording failed: %s", exc)
		finally:
			if writer is not None:
				writer.close()
			self.service.release_preview(HOLDER)
			log.info("Recording stopped: %d frames in %d file(s)", self.frames, self.files)
			self._upload()

	def meta(self) -> Dict[str, Any]:
		"""What the recording shows, for its upload."""
		from utils import photos

		seconds = max(1e-3, time.monotonic() - self.started_at)
		info: Dict[str, Any] = dict(photos.pointing(), frame_type="timelapse" if self.kind == "timelapse"
			else "video", captured=self.started_utc, quality=self.quality, frames=self.frames)
		info["fps"] = round(self.frames / seconds, 2) if self.kind == "video" else TIMELAPSE_PLAYBACK_FPS
		if self.crop:
			info["crop"] = {k: self.crop[k] for k in ("x", "y", "width", "height")}
		model = self.service.status().get("model")
		if model:
			info["camera"] = model
		return info

	def _upload(self) -> None:
		if not self.owner.isdigit() or not self.paths:
			return
		try:
			from utils.uploads import get_upload_queue

			meta = self.meta()
			paths = [p for p in self.paths if os.path.exists(p) and os.path.getsize(p) > 0]
			get_upload_queue().add_many(paths, self.owner, meta)
		except Exception as exc:
			log.warning("Couldn't queue the recording for upload: %s", exc)

	def status(self) -> Dict[str, Any]:
		return {"recording": self.running, "kind": self.kind, "quality": self.quality,
			"crop": self.crop, "frames": self.frames, "files": self.files, "path": self.path, "seconds": round(time.monotonic() - self.started_at) if self.running else 0,
			"error": self.error}


_recorder: Optional[Recorder] = None


def get_recorder() -> Recorder:
	global _recorder
	if _recorder is None:
		_recorder = Recorder()
	return _recorder
