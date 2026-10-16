"""The camera, owned by one thread.

Everything that uses the camera goes through here: live view for the
server, the local page and recordings; photos from the server and the
panel; settings. One thread talks to the camera (core/camera/backends.py),
so nothing fights over it:

* Live view runs while anyone holds it (acquire_preview / release_preview).
  The newest frame waits in a slot; readers take the newest when they're
  ready, so a slow reader never holds the camera up.
* A photo or a setting is a job for the thread. It pauses live view if the
  backend needs that, does the job, and live view carries on.
* When live view fails it restarts after a growing pause (1 s up to 30 s).
  It never turns itself off. After several failures in a row the camera
  is taken as gone and looked for again.
* While nothing is going on, the thread checks every few seconds whether
  a camera has been plugged in (or unplugged). This used to be a gphoto2
  run every 2 s on the asyncio loop.
* With camera.keep_awake, the camera's auto power-off is switched off
  while live view runs, and put back afterwards.
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.camera.backends import SETTING_ALIASES, CameraError, CameraGone, _never_choice, make_backend
from utils.log import get_logger

log = get_logger("camera")

DETECT_IDLE_S = 3.0          # looking for a camera
CHECK_CONNECTED_S = 10.0     # still there? (when idle)
FAILURES_BEFORE_GONE = 4


class FrameSlot:
	"""The newest frame, and a way to wait for the next one."""

	def __init__(self) -> None:
		self._cond = threading.Condition()
		self.seq = 0
		self.frame: Optional[bytes] = None
		self.at = 0.0

	def publish(self, frame: bytes) -> None:
		with self._cond:
			self.frame = frame
			self.seq += 1
			self.at = time.monotonic()
			self._cond.notify_all()

	def latest(self) -> Tuple[int, Optional[bytes], float]:
		with self._cond:
			return self.seq, self.frame, self.at

	def wait_newer(self, seq: int, timeout: float) -> Tuple[int, Optional[bytes], float]:
		"""The newest frame after `seq`, waiting up to timeout; the same seq
		back means nothing new arrived."""
		with self._cond:
			if self.seq == seq:
				self._cond.wait(timeout)
			return self.seq, self.frame, self.at


class _Backoff:
	def __init__(self, first: float = 1.0, most: float = 30.0) -> None:
		self.first, self.most = first, most
		self.delay = 0.0

	def fail(self) -> float:
		self.delay = self.first if self.delay <= 0 else min(self.most, self.delay * 2)
		return self.delay

	def ok(self) -> None:
		self.delay = 0.0


class CameraService:
	def __init__(self, backend=None, backend_kind: Optional[str] = None) -> None:
		self._backend = backend
		self._backend_kind = backend_kind
		self.frames = FrameSlot()
		self._jobs: "queue.Queue[Tuple[Callable[[Any], Any], bool, concurrent.futures.Future]]" = queue.Queue()
		self._holders: Set[str] = set()
		self._lock = threading.Lock()
		self._thread: Optional[threading.Thread] = None
		self._stop = threading.Event()
		self._wake = threading.Event()
		self.connected = False
		self.model = ""
		self.previewing = False
		self._names: Dict[str, Optional[str]] = {}  # setting -> this camera's name for it
		self._saved_poweroff: Optional[Tuple[str, Any]] = None
		self._preview_backoff = _Backoff()
		self._detect_backoff = _Backoff(first=DETECT_IDLE_S, most=10.0)
		self._next_preview_try = 0.0
		self._next_detect = 0.0
		self._last_seen = 0.0
		self._failures = 0
		self._restart_pending = False
		self._fps = 0.0
		self._last_frame_at = 0.0
		self.stats: Dict[str, Any] = {"frames": 0, "restarts": 0, "errors": 0, "last_error": "", "captures": 0}
		self.listeners: List[Callable[[bool], None]] = []  # called with connected on changes

	# --- life ---------------------------------------------------------------------------

	@property
	def backend(self):
		if self._backend is None:
			kind = self._backend_kind
			if kind is None:
				try:
					from utils.config_state import get_section

					kind = get_section("camera").get("backend", "auto")
				except Exception:
					kind = "auto"
			self._backend = make_backend(kind)
			log.info("Camera backend: %s", self._backend.name)
		return self._backend

	def start(self) -> "CameraService":
		if self._thread is None or not self._thread.is_alive():
			self._stop.clear()
			self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
			self._thread.start()
		return self

	def stop(self, timeout: float = 5.0) -> None:
		self._stop.set()
		self._wake.set()
		if self._thread is not None:
			self._thread.join(timeout)
		self._thread = None

	def is_available(self) -> bool:
		return self.connected

	# --- live view ------------------------------------------------------------------------

	def acquire_preview(self, holder: str) -> None:
		with self._lock:
			self._holders.add(holder)
		self._wake.set()

	def release_preview(self, holder: str) -> None:
		with self._lock:
			self._holders.discard(holder)
		self._wake.set()

	def preview_holders(self) -> List[str]:
		with self._lock:
			return sorted(self._holders)

	def wants_preview(self) -> bool:
		with self._lock:
			return bool(self._holders)

	# --- jobs ---------------------------------------------------------------------------

	def call(self, fn: Callable[[Any], Any], exclusive: bool = True, timeout: Optional[float] = 120.0) -> Any:
		"""Run fn(backend) on the camera thread and return its result.
		exclusive: live view is paused for it (the command-line backend
		can't do anything else while it runs)."""
		if threading.current_thread() is self._thread:
			return fn(self.backend)
		if self._thread is None or not self._thread.is_alive():
			self.start()
		future: concurrent.futures.Future = concurrent.futures.Future()
		self._jobs.put((fn, exclusive, future))
		self._wake.set()
		try:
			return future.result(timeout=timeout)
		except concurrent.futures.TimeoutError as exc:
			raise CameraError("The camera didn't answer in time") from exc

	def _require(self) -> None:
		if not self.connected:
			raise CameraError("Camera not connected")

	def setting_name(self, setting: str) -> Optional[str]:
		"""This camera's name for a setting ("iso", "shutterspeed", ...), or
		None if it has none."""
		if setting in self._names:
			return self._names[setting]
		aliases = SETTING_ALIASES.get(setting, (setting,))

		def find(b) -> Optional[str]:
			for name in aliases:
				try:
					b.get_config(name)
					return name
				except CameraGone:
					raise
				except CameraError:
					continue
			return None
		name = self.call(find, exclusive=self.backend.name == "cli")
		self._names[setting] = name
		return name

	def get_setting(self, setting: str) -> Dict[str, Any]:
		self._require()
		name = self.setting_name(setting) or setting
		return self.call(lambda b: b.get_config(name), exclusive=self.backend.name == "cli")

	def set_setting(self, setting: str, value: Any) -> None:
		self._require()
		name = self.setting_name(setting) or setting
		self.call(lambda b: b.set_config(name, value), exclusive=self.backend.name == "cli")
		log.info("Camera %s set to %s", name, value)

	def capture(self, folder: str, base: str, bulb_s: Optional[float] = None) -> List[str]:
		"""Take a photo into folder as base.<ext> (two files for RAW +
		JPEG). Returns the file names."""
		self._require()
		timeout = 120.0 + (bulb_s or 0.0)
		names = self.call(lambda b: b.capture(folder, base, bulb_s), exclusive=True, timeout=timeout)
		self.stats["captures"] += 1
		return names

	def storage(self) -> Dict[str, Any]:
		self._require()
		return self.call(lambda b: b.storage(), exclusive=self.backend.name == "cli", timeout=30.0)

	def battery(self) -> Optional[str]:
		try:
			return str(self.get_setting("batterylevel").get("value"))
		except CameraError:
			return None

	def list_settings(self) -> List[str]:
		self._require()
		return self.call(lambda b: b.list_config(), exclusive=self.backend.name == "cli", timeout=30.0)

	# --- the thread -------------------------------------------------------------------------

	def _set_connected(self, connected: bool) -> None:
		if connected == self.connected:
			return
		self.connected = connected
		if not connected:
			self._names.clear()
			self.previewing = False
		log.info("Camera %s%s", "connected: " if connected else "disconnected", self.model if connected else "")
		for listener in list(self.listeners):
			try:
				listener(connected)
			except Exception as exc:
				log.debug("Camera listener failed: %s", exc)

	def _note_error(self, exc: Exception) -> None:
		self.stats["errors"] += 1
		self.stats["last_error"] = str(exc)[:120]

	def _gone(self, exc: Exception) -> None:
		log.warning("Camera lost: %s", exc)
		self._note_error(exc)
		try:
			self.backend.close()
		except Exception:
			pass
		self._set_connected(False)
		self._next_detect = time.monotonic() + DETECT_IDLE_S

	def _connect(self) -> None:
		now = time.monotonic()
		if now < self._next_detect:
			return
		backend = self.backend
		try:
			model = backend.detect()
			if not model:
				self._next_detect = now + DETECT_IDLE_S
				return
			backend.open()
		except CameraError as exc:
			self._note_error(exc)
			self._next_detect = now + self._detect_backoff.fail()
			log.info("Camera found but couldn't be opened: %s", exc)
			return
		self._detect_backoff.ok()
		self.model = backend.model or model
		self._failures = 0
		self._last_seen = now
		self._set_connected(True)

	def _check_still_there(self) -> None:
		now = time.monotonic()
		if now - self._last_seen < CHECK_CONNECTED_S:
			return
		self._last_seen = now
		try:
			if not self.backend.detect():
				self._gone(CameraGone("The camera was unplugged or switched off"))
		except Exception as exc:
			log.debug("Camera check failed: %s", exc)

	def _keep_awake(self, on: bool) -> None:
		try:
			from utils.config_state import get_section

			if not get_section("camera").get("keep_awake", True):
				return
		except Exception:
			pass
		backend = self.backend
		try:
			if on:
				for name in SETTING_ALIASES["autopoweroff"]:
					try:
						info = backend.get_config(name)
					except CameraGone:
						raise
					except CameraError:
						continue
					never = _never_choice(info.get("choices") or [])
					if never is not None and str(info.get("value")) != never:
						backend.set_config(name, never)
						self._saved_poweroff = (name, info.get("value"))
						log.info("Camera auto power-off off while live view runs (was %s)", info.get("value"))
					return
			elif self._saved_poweroff is not None:
				name, value = self._saved_poweroff
				self._saved_poweroff = None
				backend.set_config(name, value)
		except CameraError as exc:
			log.debug("Keep-awake: %s", exc)

	def _start_preview(self) -> None:
		now = time.monotonic()
		if now < self._next_preview_try:
			return
		try:
			if self.backend.name != "cli" and self._saved_poweroff is None:
				self._keep_awake(True)
			self.backend.preview_start()
			self.previewing = True
			if self._restart_pending:
				self.stats["restarts"] += 1
				self._restart_pending = False
			log.info("Live view started")
		except CameraGone as exc:
			self._gone(exc)
		except CameraError as exc:
			self._preview_failed(exc)

	def _stop_preview(self, restore: bool = True) -> None:
		# restore=False: a pause for a photo; the camera stays awake.
		if not self.previewing:
			return
		self.previewing = False
		try:
			self.backend.preview_stop()
		except CameraGone as exc:
			self._gone(exc)
			return
		except CameraError as exc:
			log.debug("Stopping live view: %s", exc)
		if restore and self.backend.name != "cli":
			self._keep_awake(False)

	def _preview_failed(self, exc: Exception) -> None:
		self._note_error(exc)
		self._failures += 1
		self._restart_pending = True
		try:
			self.backend.preview_stop()
		except Exception:
			pass
		self.previewing = False
		if self._failures >= FAILURES_BEFORE_GONE and not self.backend.detect():
			self._gone(exc)
			return
		delay = self._preview_backoff.fail()
		self._next_preview_try = time.monotonic() + delay
		log.warning("Live view failed (%s); trying again in %.0f s", exc, delay)

	def _run_job(self, fn, exclusive: bool, future: concurrent.futures.Future) -> None:
		if not future.set_running_or_notify_cancel():
			return
		resume = False
		if exclusive and self.previewing:
			self._stop_preview(restore=False)
			resume = True
		try:
			result = fn(self.backend)
		except CameraGone as exc:
			self._gone(exc)
			future.set_exception(exc)
		except Exception as exc:
			self._note_error(exc)
			future.set_exception(exc)
		else:
			self._last_seen = time.monotonic()
			future.set_result(result)
		if resume:
			self._next_preview_try = 0.0

	def _grab(self) -> None:
		try:
			frame = self.backend.preview_frame(timeout=0.5)
		except CameraGone as exc:
			self._gone(exc)
			return
		except CameraError as exc:
			self._preview_failed(exc)
			return
		if not frame:
			return
		now = time.monotonic()
		if self._last_frame_at:
			dt = max(1e-3, now - self._last_frame_at)
			self._fps = 0.9 * self._fps + 0.1 * (1.0 / dt) if self._fps else 1.0 / dt
		self._last_frame_at = now
		self._last_seen = now
		self._failures = 0
		self._preview_backoff.ok()
		self.stats["frames"] += 1
		self.frames.publish(frame)

	def _run(self) -> None:
		log.info("Camera service started")
		while not self._stop.is_set():
			try:
				# Jobs first (photos and settings mustn't wait behind frames).
				try:
					fn, exclusive, future = self._jobs.get_nowait()
				except queue.Empty:
					pass
				else:
					if self.connected:
						self._run_job(fn, exclusive, future)
					else:
						future.set_exception(CameraError("Camera not connected"))
					continue
				if not self.connected:
					self._connect()
					if not self.connected:
						self._wake.wait(0.5)
						self._wake.clear()
						continue
				want = self.wants_preview()
				if want and not self.previewing:
					self._start_preview()
				elif not want and self.previewing:
					self._stop_preview()
				if self.previewing:
					self._grab()
				else:
					self._check_still_there()
					self._wake.wait(0.5)
					self._wake.clear()
			except Exception as exc:  # the thread never dies
				log.exception("Camera thread error: %s", exc)
				time.sleep(1.0)
		self._stop_preview()
		try:
			self.backend.close()
		except Exception:
			pass

	def status(self) -> Dict[str, Any]:
		seq, frame, at = self.frames.latest()
		age = time.monotonic() - at if frame else None
		return {
			"connected": self.connected,
			"model": self.model,
			"backend": self._backend.name if self._backend is not None else None,
			"live_view": self.previewing,
			"holders": self.preview_holders(),
			"camera_fps": round(self._fps, 1) if self.previewing else 0.0,
			"frame_age_s": round(age, 1) if age is not None else None,
			**self.stats,
		}


_service: Optional[CameraService] = None
_service_lock = threading.Lock()


def get_camera_service() -> CameraService:
	global _service
	with _service_lock:
		if _service is None:
			_service = CameraService()
		return _service


def set_camera_service(service: Optional[CameraService]) -> None:
	"""Swap the service (tests)."""
	global _service
	with _service_lock:
		_service = service
