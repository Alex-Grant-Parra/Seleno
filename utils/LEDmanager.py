from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from esp32.interfaceESP32 import ESP32Connection, ESP32LED
from utils.esp32_state import esp32_state
from utils.log import get_logger

log = get_logger("leds")


@dataclass
class _LedState:
	error_active: bool = False
	error_critical: bool = False
	movement_mode: Optional[str] = None
	tracking_active: bool = False
	tracking_pulse: bool = False
	command_busy: bool = False
	blue_on_when_idle: bool = True


class LEDManager:
	"""The front panel's status LEDs. Each change works out what every LED
	should show and sends only the ones that differ, in one command
	({"cmd":"led","leds":{...}}); older firmware without it gets one command
	per LED."""

	def __init__(self) -> None:
		self._state = _LedState()
		self._lock = threading.RLock()
		self._flash_lock = threading.Lock()
		self._applied_signatures: dict[str, tuple[str, int]] = {}
		self._conn: Optional[ESP32Connection] = None
		self._leds: Optional[ESP32LED] = None
		self._batch_ok = True  # the firmware takes "leds"

	def _ensure_connection(self) -> bool:
		conn = esp32_state.ensure_connection()
		if conn is None:
			self._conn = None
			self._leds = None
			return False
		if self._conn is conn and self._leds is not None:
			return True
		self._conn = conn
		self._leds = ESP32LED(conn)
		self._applied_signatures.clear()
		log.info("ESP32 LED connection established")
		return True

	def _set_channel(self, channel_name: str, mode: str, interval_ms: int = 500) -> None:
		self._set_channels({channel_name: (mode, interval_ms)})

	def _set_channels(self, wanted: dict) -> None:
		"""wanted: {channel: (mode, interval_ms)}; sends what changed."""
		if not self._ensure_connection() or self._leds is None:
			return
		changes = {}
		for name, (mode, interval_ms) in wanted.items():
			if mode not in ("on", "off", "blink"):
				raise ValueError(f"Unsupported LED mode: {mode}")
			signature = (mode, int(interval_ms) if mode == "blink" else 0)
			if self._applied_signatures.get(name) != signature:
				changes[name] = signature
		if not changes:
			return
		if self._batch_ok and len(changes) > 1:
			payload = {name: ({"mode": "blink", "interval_ms": sig[1]} if sig[0] == "blink" else sig[0])
				for name, sig in changes.items()}
			try:
				self._conn.send({"cmd": "led", "leds": payload})
				self._applied_signatures.update(changes)
				return
			except Exception as exc:
				if "LED test" in str(exc):
					raise
				self._batch_ok = False  # older firmware: one at a time
		for name, (mode, interval_ms) in changes.items():
			channel = getattr(self._leds, name, None)
			if channel is None:
				continue
			if mode == "on":
				channel.on()
			elif mode == "off":
				channel.off()
			else:
				channel.blink(interval_ms=interval_ms)
			self._applied_signatures[name] = (mode, interval_ms if mode == "blink" else 0)

	def _apply_locked(self) -> None:
		if not self._ensure_connection() or self._leds is None:
			return
		wanted = {}
		state = self._state
		if state.error_active:
			wanted["red"] = ("blink", 160) if state.error_critical else ("on", 0)
			wanted.update(yellow=("off", 0), green=("off", 0), blue=("off", 0))
			self._set_channels(wanted)
			return
		wanted["red"] = ("off", 0)
		if state.movement_mode == "homing":
			wanted.update(yellow=("on", 0), green=("off", 0))
		elif state.movement_mode == "slewing":
			wanted.update(yellow=("blink", 700), green=("off", 0))
		elif state.tracking_active:
			wanted.update(yellow=("off", 0), green=("blink", 1800) if state.tracking_pulse else ("on", 0))
		else:
			wanted.update(yellow=("off", 0), green=("off", 0))
		if state.command_busy:
			wanted["blue"] = ("blink", 1400)
		else:
			wanted["blue"] = ("on", 0) if state.blue_on_when_idle else ("off", 0)
		self._set_channels(wanted)

	def _flash_white(self, flashes: int = 1, on_time: float = 0.12, off_time: float = 0.08) -> None:
		if not self._ensure_connection() or self._leds is None:
			return
		if flashes <= 0:
			return
		with self._flash_lock:
			for index in range(flashes):
				self._set_channel("white", "on")
				time.sleep(on_time)
				self._set_channel("white", "off")
				if index < flashes - 1:
					time.sleep(off_time)
			with self._lock:
				self._apply_locked()

	def apply(self) -> None:
		with self._lock:
			self._apply_locked()

	def set_error(self, active: bool, critical: bool = False) -> None:
		with self._lock:
			self._state.error_active = bool(active)
			self._state.error_critical = bool(critical)
			if active:
				self._state.movement_mode = None
				self._state.tracking_active = False
				self._state.command_busy = False
				self._state.blue_on_when_idle = False
			else:
				self._state.blue_on_when_idle = True
			self._apply_locked()

	def clear_error(self) -> None:
		self.set_error(False)

	def set_ui_connected(self, connected: bool, force_reapply: bool = False) -> None:
		with self._lock:
			self._state.blue_on_when_idle = bool(connected)
			if force_reapply:
				self._applied_signatures.pop("blue", None)
			# Only touch the blue indicator here; the rest of the state is driven
			# by the normal refresh path.
			self._set_channel("blue", "on" if connected else "off")

	def shutdown(self) -> None:
		with self._lock:
			self._state.blue_on_when_idle = False
			channel = getattr(self._leds, "blue", None) if self._leds is not None else None
			if channel is None:
				return
			try:
				channel.off()
			except Exception:
				pass

	def set_movement(self, mode: Optional[str]) -> None:
		mode = mode.lower() if isinstance(mode, str) else None
		if mode not in {None, "slewing", "homing"}:
			raise ValueError("movement mode must be 'slewing', 'homing', or None")
		with self._lock:
			self._state.movement_mode = mode
			if mode is not None:
				self._state.tracking_active = False
			self._apply_locked()

	def set_tracking(self, active: bool, pulse: bool = False) -> None:
		with self._lock:
			self._state.tracking_active = bool(active)
			self._state.tracking_pulse = bool(pulse)
			if active:
				self._state.movement_mode = None
				self._state.blue_on_when_idle = True
			self._apply_locked()

	def set_idle(self) -> None:
		with self._lock:
			self._state.error_active = False
			self._state.error_critical = False
			self._state.movement_mode = None
			self._state.tracking_active = False
			self._state.tracking_pulse = False
			self._state.command_busy = False
			self._state.blue_on_when_idle = True
			self._apply_locked()

	def set_command_busy(self, active: bool) -> None:
		with self._lock:
			self._state.command_busy = bool(active)
			self._apply_locked()

	def flash_target_acquired(self, major: bool = True) -> None:
		threading.Thread(
			target=self._flash_white,
			kwargs={"flashes": 2 if major else 1, "on_time": 0.14 if major else 0.1},
			daemon=True,
		).start()

	def flash_capture_started(self) -> None:
		threading.Thread(
			target=self._flash_white,
			kwargs={"flashes": 2, "on_time": 0.16, "off_time": 0.08},
			daemon=True,
		).start()

	def flash_parking_complete(self) -> None:
		threading.Thread(
			target=self._flash_white,
			kwargs={"flashes": 1, "on_time": 0.18, "off_time": 0.05},
			daemon=True,
		).start()

	def flash_homing_complete(self) -> None:
		threading.Thread(
			target=self._flash_white,
			kwargs={"flashes": 1, "on_time": 0.16, "off_time": 0.05},
			daemon=True,
		).start()

	def flash_tracking_locked(self) -> None:
		threading.Thread(
			target=self._flash_white,
			kwargs={"flashes": 1, "on_time": 0.12, "off_time": 0.05},
			daemon=True,
		).start()

	def flash_command_complete(self, success: bool = True) -> None:
		if success:
			self.flash_tracking_locked()
		else:
			threading.Thread(
				target=self._flash_white,
				kwargs={"flashes": 2, "on_time": 0.08, "off_time": 0.05},
				daemon=True,
			).start()

	@contextmanager
	def command_scope(self):
		self.set_command_busy(True)
		try:
			yield
		finally:
			self.set_command_busy(False)


_LED_MANAGER: Optional[LEDManager] = None


def get_led_manager() -> LEDManager:
	global _LED_MANAGER
	if _LED_MANAGER is None:
		_LED_MANAGER = LEDManager()
	return _LED_MANAGER
