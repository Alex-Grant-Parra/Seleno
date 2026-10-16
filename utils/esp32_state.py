from __future__ import annotations

import asyncio
import time
from threading import Lock
from typing import Optional

from esp32.interfaceESP32 import DEFAULT_BAUDRATE, ESP32Connection, ESP32SerialConfig
from utils.config_state import load_static_state, save_static_state


class ESP32State:
	# Thread-safe ESP32 availability and connection state.

	_instance = None
	_lock = Lock()

	def __new__(cls):
		if cls._instance is None:
			with cls._lock:
				if cls._instance is None:
					cls._instance = super().__new__(cls)
					cls._instance._initialized = False
		return cls._instance

	def __init__(self):
		if self._initialized:
			return
		self._available = False
		self._last_check_time = 0.0
		self._conn: Optional[ESP32Connection] = None
		self._state_lock = Lock()
		self._initialized = True

	def is_available(self) -> bool:
		with self._state_lock:
			return self._available

	def set_available(self, available: bool) -> None:
		with self._state_lock:
			changed = self._available != bool(available)
			self._available = bool(available)
			self._last_check_time = time.time()
		if changed:
			status = "available" if available else "unavailable"
			print(f"[esp32_state] ESP32 is now {status}")

	def get_last_check_time(self) -> float:
		with self._state_lock:
			return self._last_check_time

	def get_connection(self) -> Optional[ESP32Connection]:
		with self._state_lock:
			return self._conn

	def _persist_port(self, conn: ESP32Connection) -> None:
		try:
			state = load_static_state()
			esp32 = state.get("esp32", {})
			if not isinstance(esp32, dict):
				esp32 = {}
			esp32["port"] = conn.cfg.port
			esp32["baudrate"] = conn.cfg.baudrate
			esp32["timeout"] = conn.cfg.timeout
			state["esp32"] = esp32
			save_static_state(state)
		except Exception as exc:
			print(f"[esp32_state] Warning: could not save ESP32 port: {exc}")

	def set_connection(self, conn: Optional[ESP32Connection]) -> None:
		old_conn: Optional[ESP32Connection] = None
		with self._state_lock:
			if self._conn is conn:
				self._available = conn is not None
				self._last_check_time = time.time()
				return
			old_conn = self._conn
			self._conn = conn
			self._available = conn is not None
			self._last_check_time = time.time()
		if old_conn is not None and old_conn is not conn:
			old_conn.close()
		if conn is not None:
			self._persist_port(conn)
			try:
				from utils.esp32_events import attach as attach_event_handlers

				attach_event_handlers(conn)
			except Exception as exc:
				print(f"[esp32_state] Warning: could not attach ESP32 event handlers: {exc}")
			try:
				from utils.panel_ui import get_panel_ui

				get_panel_ui().attach(conn)
			except Exception as exc:
				print(f"[esp32_state] Warning: could not attach the front panel: {exc}")
			print(f"[esp32_state] Connected to ESP32 on {conn.cfg.port} ({conn.transport})")
		else:
			print("[esp32_state] ESP32 connection cleared")

	def clear_connection(self) -> None:
		self.set_connection(None)

	def ensure_connection(self) -> Optional[ESP32Connection]:
		conn = self.get_connection()
		if conn is not None and self.is_available():
			return conn

		try:
			config = load_static_state().get("esp32", {})
			if not isinstance(config, dict):
				config = {}
			candidate = ESP32Connection(
				ESP32SerialConfig(
					port=str(config.get("port", "auto")),
					baudrate=int(config.get("baudrate", DEFAULT_BAUDRATE)),
					timeout=float(config.get("timeout", 0.5)),
					transport=str(config.get("transport", "auto")),
				)
			)
			self.set_connection(candidate)
			return candidate
		except Exception:
			self.set_available(False)
			return None

	def mark_disconnected(self) -> None:
		self.set_available(False)
		self.clear_connection()


esp32_state = ESP32State()


def _refresh_led_state_on_esp32_reconnect(force_reapply: bool = False) -> None:
	# Re-assert UI/idle LED state after transport reconnect so blue comes back on.
	try:
		from utils.LEDmanager import get_led_manager

		get_led_manager().set_ui_connected(True, force_reapply=force_reapply)
	except Exception as exc:
		print(f"[esp32_scanner] Warning: failed to refresh LED state: {exc}")


async def esp32_scanner_task(check_interval: float = 2.0):
	# Background task that continuously scans for ESP32 availability.

	print(f"[esp32_scanner] Started ESP32 scanner (checking every {check_interval}s)")
	last_led_reassert_at = 0.0
	led_reassert_interval = max(3.0, check_interval * 2.0)
	while True:
		try:
			conn = esp32_state.get_connection()
			if conn is not None:
				try:
					conn.list_motors()
					now = time.monotonic()
					if not esp32_state.is_available():
						esp32_state.set_available(True)
						_refresh_led_state_on_esp32_reconnect(force_reapply=True)
						last_led_reassert_at = now
					elif now - last_led_reassert_at >= led_reassert_interval:
						# Keep LED state in sync after brief ESP32 reboots that may not
						# be observed as a full disconnect by the scanner.
						_refresh_led_state_on_esp32_reconnect(force_reapply=True)
						last_led_reassert_at = now
				except Exception as exc:
					print(f"[esp32_scanner] ESP32 connection lost: {exc}")
					esp32_state.mark_disconnected()
					conn = None

			if conn is None:
				was_unavailable = not esp32_state.is_available()
				new_conn = esp32_state.ensure_connection()
				if new_conn is not None:
					if was_unavailable:
						esp32_state.set_available(True)
					_refresh_led_state_on_esp32_reconnect(force_reapply=True)
					last_led_reassert_at = time.monotonic()
		except Exception as exc:
				if esp32_state.is_available():
					print(f"[esp32_scanner] ESP32 scan failed: {exc}")
					esp32_state.mark_disconnected()
		await asyncio.sleep(check_interval)
