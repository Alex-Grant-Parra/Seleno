"""Live view for the server: what to send, how often and how big.

The camera's frames (core/camera/service.py) are re-encoded on the Pi
before they go anywhere: smaller, and only as often as they're worth.

* Quality presets (config liveview.quality):
	original  as the camera sends it
	high      full size, JPEG quality 80
	medium    half size, quality 70 (the default)
	low       quarter size, quality 60
  Halving or quartering uses JPEG's own scaled decoding, so it's cheap
  (about 5 ms a frame on a Pi 5).
* Frame rate: max_fps while something is changing (a slew, focusing, the
  view itself), still_fps otherwise. A frame that looks the same as the
  last one sent isn't sent, except once every keyframe_s, so a viewer
  that joins late still gets a picture.
* The link: when sends get slow the quality steps down a level (and back
  up when they're quick again), instead of the connection timing out.
* Viewers: when the server says how many are watching
  (utils/handlers.liveViewViewers), nobody watching for viewer_grace_s
  pauses the stream and lets the camera rest. idle_stop_min > 0 turns live
  view off after that long without a viewer (or since it started, if the
  server never says).
* Focus mode: a full-resolution crop round the brightest star is sent
  instead of the whole frame, with its HFR (core/camera/focus.py).

The server stores whatever arrives on the live-view socket as the latest
frame and serves it as image/jpeg, so only JPEG frames are ever sent.
"""
from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from utils.log import get_logger

log = get_logger("liveview")

# (scale, quality) per level, best first.
LEVELS: Tuple[Tuple[str, Optional[Tuple[int, int]]], ...] = (
	("original", None),
	("high", (1, 80)),
	("medium", (2, 70)),
	("low", (4, 60)),
	("lowest", (4, 45)),
)
PRESETS = [name for name, _ in LEVELS[:4]]
CHANGE_THRESHOLD = 2.5      # mean difference (0-255) of the thumbnails
ACTIVE_AFTER_CHANGE_S = 3.0
FOCUS_CROP = 320
HOLDER = "server"


@dataclass
class StreamSettings:
	quality: str = "medium"
	max_fps: float = 10.0
	still_fps: float = 2.0
	keyframe_s: float = 5.0
	adaptive: bool = True
	idle_stop_min: float = 0.0
	viewer_grace_s: float = 60.0

	@classmethod
	def load(cls) -> "StreamSettings":
		try:
			from utils.config_state import get_section

			s = get_section("liveview")
		except Exception:
			return cls()
		quality = str(s.get("quality", "medium")).lower()
		return cls(
			quality=quality if quality in PRESETS else "medium",
			max_fps=max(0.2, float(s.get("max_fps", 10.0))),
			still_fps=max(0.1, float(s.get("still_fps", 2.0))),
			keyframe_s=max(0.5, float(s.get("keyframe_s", 5.0))),
			adaptive=bool(s.get("adaptive", True)),
			idle_stop_min=max(0.0, float(s.get("idle_stop_min", 0) or 0)),
			viewer_grace_s=max(0.0, float(s.get("viewer_grace_s", 60))),
		)


def thumbnail(jpeg: bytes) -> Optional[bytes]:
	"""A tiny grey picture of the frame, to tell whether it changed."""
	try:
		from PIL import Image

		im = Image.open(io.BytesIO(jpeg))
		im.draft("L", (max(1, im.width // 8), max(1, im.height // 8)))
		return im.convert("L").resize((32, 24)).tobytes()
	except Exception:
		return None


def difference(a: Optional[bytes], b: Optional[bytes]) -> float:
	if a is None or b is None or len(a) != len(b):
		return 255.0
	return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def encode(jpeg: bytes, level: str, crop: Optional[Tuple[int, int, int, int]] = None) -> bytes:
	"""The frame at a quality level; crop (full-resolution pixels) for focus."""
	from PIL import Image

	params = dict(LEVELS).get(level)
	if params is None and crop is None:
		return jpeg
	scale, quality = params or (1, 85)
	im = Image.open(io.BytesIO(jpeg))
	if crop is not None:
		im = im.convert("RGB").crop(crop)
		quality = max(quality, 80)
	else:
		if scale > 1:
			im.draft("RGB", (max(1, im.width // scale), max(1, im.height // scale)))
		im = im.convert("RGB")
	out = io.BytesIO()
	im.save(out, "JPEG", quality=quality)
	return out.getvalue()


class LiveStream:
	"""Live view for the server. Thread-safe; next_frame() blocks (run it
	off the event loop)."""

	def __init__(self, service=None, activity: Optional[Callable[[], bool]] = None) -> None:
		self._service = service
		self._activity = activity or _telescope_moving
		self._lock = threading.Lock()
		self.settings = StreamSettings.load()
		self.enabled = False
		self.viewers: Optional[int] = None
		self._viewers_zero_since: Optional[float] = None
		self._last_viewer_at = 0.0
		self._paused = False
		self.focus = False
		self.focus_result: Dict[str, Any] = {}
		self._last_thumb: Optional[bytes] = None
		self._last_sent_at = 0.0
		self._last_change_at = 0.0
		self._poked_at = 0.0
		self._seq = 0
		self._penalty = 0  # quality levels below the preset
		self._send_ema = 0.0
		self._fast_since: Optional[float] = None
		self.on_auto_stop: Optional[Callable[[str], None]] = None
		self.counters: Dict[str, Any] = {"sent": 0, "bytes": 0, "skipped_same": 0, "skipped_rate": 0,
			"level_changes": 0}
		self._rate_window: list = []

	@property
	def service(self):
		if self._service is None:
			from core.camera.service import get_camera_service

			self._service = get_camera_service()
		return self._service

	# --- control --------------------------------------------------------------------

	def start(self) -> None:
		with self._lock:
			self.enabled = True
			self._paused = False
			self._last_viewer_at = time.monotonic()
			self._viewers_zero_since = None
		self.settings = StreamSettings.load()
		self.service.acquire_preview(HOLDER)
		log.info("Live view on (%s)", self.settings.quality)

	def stop(self) -> None:
		with self._lock:
			self.enabled = False
			self._paused = False
		self.service.release_preview(HOLDER)
		log.info("Live view off")

	def reload_settings(self) -> None:
		self.settings = StreamSettings.load()
		self._penalty = 0
		self.poke()

	def poke(self) -> None:
		"""Something changed (a setting, the zoom): send at the full rate."""
		self._poked_at = time.monotonic()

	def set_viewers(self, count: int) -> None:
		now = time.monotonic()
		with self._lock:
			self.viewers = max(0, int(count))
			if self.viewers > 0:
				self._viewers_zero_since = None
				self._last_viewer_at = now
				if self._paused:
					self._paused = False
					log.info("Live view resumed: %d watching", self.viewers)
					if self.enabled:
						self.service.acquire_preview(HOLDER)
			elif self._viewers_zero_since is None:
				self._viewers_zero_since = now

	def set_focus(self, on: bool) -> None:
		self.focus = bool(on)
		if not on:
			self.focus_result = {}
		self.poke()

	def _tick(self) -> None:
		"""Viewer pauses and the idle stop."""
		now = time.monotonic()
		auto_stop = None
		with self._lock:
			if not self.enabled:
				return
			s = self.settings
			if (self.viewers == 0 and not self._paused and self._viewers_zero_since is not None
					and now - self._viewers_zero_since >= s.viewer_grace_s):
				self._paused = True
				log.info("Nobody watching for %.0f s: live view paused", s.viewer_grace_s)
				self.service.release_preview(HOLDER)
			if s.idle_stop_min > 0 and not self.viewers and now - self._last_viewer_at >= s.idle_stop_min * 60:
				auto_stop = f"No viewers for {s.idle_stop_min:.0f} min: live view off"
		if auto_stop:
			from utils.liveview_state import save_liveview_state

			log.info(auto_stop)
			save_liveview_state(False)
			if self.on_auto_stop:
				self.on_auto_stop(auto_stop)

	@property
	def paused(self) -> bool:
		return self._paused

	# --- frames -----------------------------------------------------------------------

	def level(self) -> str:
		names = [name for name, _ in LEVELS]
		base = names.index(self.settings.quality) if self.settings.quality in names else 2
		return names[min(len(names) - 1, base + self._penalty)]

	def _active(self, now: float) -> bool:
		if self.focus or now - self._poked_at < ACTIVE_AFTER_CHANGE_S:
			return True
		if now - self._last_change_at < ACTIVE_AFTER_CHANGE_S:
			return True
		try:
			return bool(self._activity())
		except Exception:
			return False

	def next_frame(self, timeout: float = 1.0) -> Optional[bytes]:
		"""The next frame worth sending, encoded; None if there's none yet."""
		self._tick()
		if not self.enabled or self._paused:
			time.sleep(min(timeout, 0.5))
			return None
		deadline = time.monotonic() + timeout
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				return None
			seq, frame, _at = self.service.frames.wait_newer(self._seq, remaining)
			if seq == self._seq or frame is None:
				continue
			self._seq = seq
			now = time.monotonic()
			s = self.settings
			active = self._active(now)
			interval = 1.0 / (s.max_fps if active else s.still_fps)
			if now - self._last_sent_at < interval:
				self.counters["skipped_rate"] += 1
				continue
			thumb = thumbnail(frame)
			changed = difference(thumb, self._last_thumb) >= CHANGE_THRESHOLD
			if changed:
				self._last_change_at = now
			elif not active and now - self._last_sent_at < s.keyframe_s:
				self.counters["skipped_same"] += 1
				continue
			crop = None
			if self.focus:
				crop = self._focus(frame)
			try:
				out = encode(frame, self.level(), crop)
			except Exception as exc:
				log.debug("Couldn't re-encode a frame (%s); sending it as it is", exc)
				out = frame
			self._last_thumb = thumb
			self._last_sent_at = now
			return out

	def _focus(self, frame: bytes) -> Optional[Tuple[int, int, int, int]]:
		try:
			from core.camera.focus import analyse

			result = analyse(frame)
		except Exception as exc:
			log.debug("Focus analysis failed: %s", exc)
			return None
		self.focus_result = result
		star, size = result.get("star"), result.get("size")
		if not size:
			return None
		w, h = size
		cx, cy = star if star else (w // 2, h // 2)
		half = FOCUS_CROP // 2
		left = max(0, min(w - FOCUS_CROP, int(cx) - half))
		top = max(0, min(h - FOCUS_CROP, int(cy) - half))
		return left, top, min(w, left + FOCUS_CROP), min(h, top + FOCUS_CROP)

	def note_sent(self, size: int, seconds: float) -> None:
		"""How long a send took: the link adapts to it."""
		now = time.monotonic()
		self.counters["sent"] += 1
		self.counters["bytes"] += size
		self._rate_window.append((now, size))
		self._rate_window = [(t, n) for t, n in self._rate_window if now - t <= 10.0]
		self._send_ema = seconds if not self._send_ema else 0.8 * self._send_ema + 0.2 * seconds
		if not self.settings.adaptive:
			return
		budget = 1.0 / self.settings.max_fps
		names = [name for name, _ in LEVELS]
		base = names.index(self.settings.quality) if self.settings.quality in names else 2
		if self._send_ema > max(0.5, 2.0 * budget) and base + self._penalty < len(LEVELS) - 1:
			self._penalty += 1
			self._send_ema *= 0.5
			self.counters["level_changes"] += 1
			self._fast_since = None
			log.info("Slow link (sends take %.2f s): live view down to %s", seconds, self.level())
		elif self._send_ema < 0.3 * budget and self._penalty > 0:
			self._fast_since = self._fast_since or now
			if now - self._fast_since > 15.0:
				self._penalty -= 1
				self._fast_since = None
				self.counters["level_changes"] += 1
				log.info("Link quick again: live view up to %s", self.level())
		else:
			self._fast_since = None

	def stats(self) -> Dict[str, Any]:
		now = time.monotonic()
		window = [(t, n) for t, n in self._rate_window if now - t <= 10.0]
		span = max(1.0, now - window[0][0]) if window else 1.0
		return {
			"enabled": self.enabled,
			"paused": self._paused,
			"viewers": self.viewers,
			"quality": self.settings.quality,
			"level": self.level(),
			"fps": round(len(window) / span, 1) if window else 0.0,
			"kbps": round(sum(n for _, n in window) * 8 / 1000.0 / span, 1) if window else 0.0,
			"avg_kb": round(sum(n for _, n in window) / len(window) / 1024.0, 1) if window else 0.0,
			"send_ms": round(self._send_ema * 1000.0),
			"focus": self.focus,
			"focus_result": dict(self.focus_result),
			**self.counters,
		}


def _telescope_moving() -> bool:
	try:
		from core.hardware import tracking

		return tracking._panel_state in ("slewing", "centering")
	except Exception:
		return False


_stream: Optional[LiveStream] = None
_stream_lock = threading.Lock()


def get_live_stream() -> LiveStream:
	global _stream
	with _stream_lock:
		if _stream is None:
			_stream = LiveStream()
		return _stream


def set_live_stream(stream: Optional[LiveStream]) -> None:
	global _stream
	with _stream_lock:
		_stream = stream
