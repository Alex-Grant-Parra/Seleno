"""Pi-side interface to the SELENO ESP32 firmware.

Newline-delimited JSON over a serial port, in either transport:

* USB: a dev kit's USB-serial chip (/dev/ttyUSB*, /dev/serial/by-id/*)
* GPIO UART: the Pi's GPIO14/15 wired to the SELENO v0.8 board via J7
  (/dev/ttyAMA0 on a Pi 5, /dev/serial0 on a Zero 2 W)

A background thread reads everything the ESP32 sends. Lines with an "event"
member (button presses, emergency stop, motion_done, driver faults, boot)
go to event listeners; everything else is a response. Each request carries
an "id" that the firmware echoes, so a late reply can't be taken for the
answer to the next command. Replies without an id (older firmware) are
still accepted.

Protocol reference: esp32-firmware/PROTOCOL.md.
"""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional

import serial  # pyserial

from esp32 import serial_ports
from esp32.pins import get_display_config, get_led_channels


_DISPLAY_CONFIG = get_display_config()
_DISPLAY_WIDTH = int(_DISPLAY_CONFIG["width"])
_DISPLAY_HEIGHT = int(_DISPLAY_CONFIG["height"])

DEFAULT_BAUDRATE = 921600
DEFAULT_STEPS_PER_REV = 1600

# Safe to send twice: a lost reply to one of these is retried once.
_IDEMPOTENT_COMMANDS = frozenset({
	"ping", "info", "list_motors", "status", "get_position", "i2c_scan",
	"panel", "buttons", "imu", "host",
})

EventCallback = Callable[[Dict[str, Any]], None]


class ESP32Error(RuntimeError):
	"""The ESP32 answered with {"status":"error"}. The link itself is fine."""

	def __init__(self, message: str, response: Dict[str, Any]) -> None:
		# Keep the old wording: callers match on "ESP32 error".
		super().__init__(f"ESP32 error: {message} | Full response: {response}")
		self.error_message = message
		self.response = response


class ESP32TransportError(ConnectionError):
	"""The serial link failed (unplugged, port closed, reader stopped)."""


@dataclass
class ESP32SerialConfig:
	# "auto" tries USB serial devices, then the Pi's GPIO UART.
	port: str = "auto"
	baudrate: int = DEFAULT_BAUDRATE
	timeout: float = 0.5
	# "auto", "usb" or "gpio_uart" (restricts the port search).
	transport: str = serial_ports.TRANSPORT_AUTO


class _Waiter:
	__slots__ = ("request_id", "event", "response")

	def __init__(self, request_id: int) -> None:
		self.request_id = request_id
		self.event = threading.Event()
		self.response: Optional[Dict[str, Any]] = None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
	"""Parse the first balanced {...} in text, tolerating leading noise."""
	start = text.find("{")
	if start < 0:
		return None
	try:
		obj = json.loads(text[start:])
		return obj if isinstance(obj, dict) else None
	except json.JSONDecodeError:
		pass
	depth = 0
	in_string = False
	escaped = False
	for i in range(start, len(text)):
		ch = text[i]
		if in_string:
			if escaped:
				escaped = False
			elif ch == "\\":
				escaped = True
			elif ch == '"':
				in_string = False
			continue
		if ch == '"':
			in_string = True
		elif ch == "{":
			depth += 1
		elif ch == "}":
			depth -= 1
			if depth == 0:
				try:
					obj = json.loads(text[start:i + 1])
				except json.JSONDecodeError:
					return None
				return obj if isinstance(obj, dict) else None
	return None


class ESP32Connection:
	def __init__(self, cfg: ESP32SerialConfig | None = None, *, scan_all_ports: bool = True) -> None:
		self.cfg = cfg or ESP32SerialConfig()
		self.ser: Optional[serial.Serial] = None
		self.transport = serial_ports.TRANSPORT_USB
		self.firmware: Dict[str, Any] = {}
		self.supports_ids = False
		# Per-motor facts the firmware reports (steps_per_rev, microsteps).
		self.motor_info: Dict[str, Dict[str, Any]] = {}

		self._lock = threading.Lock()          # one request in flight
		self._waiter_lock = threading.Lock()
		self._waiter: Optional[_Waiter] = None
		self._ids = itertools.count(1)
		self._stop = threading.Event()
		self._failure: Optional[BaseException] = None
		self._reader: Optional[threading.Thread] = None
		self._dispatcher: Optional[threading.Thread] = None
		self._events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
		self._listeners: Dict[int, tuple[Optional[str], EventCallback]] = {}
		self._listener_ids = itertools.count(1)
		self._listeners_lock = threading.Lock()
		self.recent_events: Deque[Dict[str, Any]] = deque(maxlen=100)
		self.recent_noise: Deque[str] = deque(maxlen=10)  # non-JSON lines, for diagnosis
		self.stats = {"corrupt_lines": 0, "stale_responses": 0, "noise_lines": 0, "events": 0}

		preferred = None if self.cfg.port in ("", serial_ports.TRANSPORT_AUTO) else self.cfg.port
		if scan_all_ports:
			candidates = serial_ports.candidate_ports(preferred, self.cfg.transport)
		else:
			candidates = [preferred] if preferred else serial_ports.candidate_ports(None, self.cfg.transport)

		last_error: Exception | None = None
		skipped: List[str] = []
		for port in candidates:
			conflict = serial_ports.console_conflict(port)
			if conflict:
				skipped.append(conflict)
				continue
			try:
				self.ser = serial.Serial(port, self.cfg.baudrate, timeout=0.1, write_timeout=5.0)
				self.cfg.port = port
				if not self._probe_identity():
					self.close()
					continue
				self.transport = serial_ports.transport_for_port(port)
				break
			except (serial.SerialException, OSError, RuntimeError) as exc:
				last_error = exc
				self.close()

		if self.ser is None:
			ports_text = ", ".join(candidates) if candidates else "none found"
			detail = f" (skipped: {'; '.join(skipped)})" if skipped else ""
			raise RuntimeError(f"Unable to connect to ESP32 on serial ports: {ports_text}{detail}") from last_error

		self._stop.clear()
		self._reader = threading.Thread(target=self._read_loop, name="esp32-reader", daemon=True)
		self._dispatcher = threading.Thread(target=self._dispatch_loop, name="esp32-events", daemon=True)
		self._reader.start()
		self._dispatcher.start()
		if self.supports_ids:
			try:
				self.firmware = self.send({"cmd": "ping"}, timeout=1.0)
			except Exception:
				self.firmware = {}
		print(f"[ESP32] Connected to {self.cfg.port} ({self.transport}"
			f"{', firmware ' + str(self.firmware.get('fw')) if self.firmware else ''})")
		try:
			ESP32LED.attach(self)
		except NameError:
			pass

	# --- connection -----------------------------------------------------

	def _probe_identity(self) -> bool:
		"""Ask list_motors (understood by old and new firmware) before the
		reader thread starts. A reply that echoes our id means new firmware."""
		assert self.ser is not None
		try:
			self.ser.reset_input_buffer()
			# A bare newline ends any half-received line or stuck transfer.
			self.ser.write(b"\n")
			self.ser.flush()
			time.sleep(0.1)
			self.ser.reset_input_buffer()
			self.ser.write(b'{"cmd":"list_motors","id":0}\n')
			self.ser.flush()
			deadline = time.monotonic() + 2.0
			buf = b""
			while time.monotonic() < deadline:
				buf += self.ser.read(256)
				while b"\n" in buf:
					raw, buf = buf.split(b"\n", 1)
					obj = _extract_json_object(raw.decode("utf-8", errors="ignore"))
					if obj is None or "event" in obj or "status" not in obj:
						continue
					if obj.get("status") != "ok":
						return False
					self.supports_ids = obj.get("id") == 0
					motors = obj.get("data", {}).get("motors")
					return isinstance(motors, list)
			return False
		except Exception as exc:
			print(f"[ESP32] Probe error on {self.cfg.port}: {exc}")
			return False

	def close(self) -> None:
		self._stop.set()
		ser, self.ser = self.ser, None
		try:
			if ser is not None:
				ser.close()
		except Exception:
			pass
		reader = self._reader
		if reader is not None and reader is not threading.current_thread():
			reader.join(timeout=1.0)
		self._reader = None
		dispatcher = self._dispatcher
		if dispatcher is not None:
			self._events.put(None)
			if dispatcher is not threading.current_thread():
				dispatcher.join(timeout=1.0)
		self._dispatcher = None
		self._release_waiter()

	@property
	def is_open(self) -> bool:
		return self.ser is not None and self._failure is None and not self._stop.is_set()

	def _drain(self) -> None:
		try:
			if self.ser is not None:
				self.ser.reset_input_buffer()
		except Exception:
			pass

	# --- reader thread ----------------------------------------------------

	def _read_loop(self) -> None:
		buf = b""
		while not self._stop.is_set():
			ser = self.ser
			if ser is None:
				break
			try:
				chunk = ser.read(ser.in_waiting or 1)
			except (serial.SerialException, OSError, TypeError, AttributeError) as exc:
				if not self._stop.is_set():
					self._failure = exc
					print(f"[ESP32] Serial link lost on {self.cfg.port}: {exc}")
					self._release_waiter()
					self._events.put({"event": "link_lost", "port": self.cfg.port, "error": str(exc)})
				break
			if not chunk:
				continue
			buf += chunk
			if len(buf) > 65536 and b"\n" not in buf:
				buf = b""  # no line ending in 64 KB: garbage
				self.stats["corrupt_lines"] += 1
				continue
			while b"\n" in buf:
				raw, buf = buf.split(b"\n", 1)
				self._handle_line(raw)

	def _handle_line(self, raw: bytes) -> None:
		text = raw.decode("utf-8", errors="replace").strip()
		if not text:
			return
		if "{" not in text:
			# ROM boot banner (115200 baud, unreadable at 921600) and the like.
			self.stats["noise_lines"] += 1
			self.recent_noise.append(text[:120])
			return
		obj = _extract_json_object(text)
		if obj is None:
			self.stats["corrupt_lines"] += 1
			return
		if "event" in obj:
			self.stats["events"] += 1
			self._events.put(obj)
			return
		with self._waiter_lock:
			waiter = self._waiter
			if waiter is not None and ("id" not in obj or obj.get("id") == waiter.request_id):
				waiter.response = obj
				self._waiter = None
				waiter.event.set()
				return
		self.stats["stale_responses"] += 1

	def _release_waiter(self) -> None:
		with self._waiter_lock:
			waiter, self._waiter = self._waiter, None
		if waiter is not None:
			waiter.event.set()

	# --- events -----------------------------------------------------------

	def _dispatch_loop(self) -> None:
		while True:
			event = self._events.get()
			if event is None:
				break
			self.recent_events.append(event)
			name = event.get("event")
			with self._listeners_lock:
				listeners = list(self._listeners.values())
			for wanted, callback in listeners:
				if wanted is not None and wanted != name:
					continue
				try:
					callback(event)
				except Exception as exc:
					print(f"[ESP32] Event listener error for {name}: {exc}")

	def add_event_listener(self, callback: EventCallback, event: Optional[str] = None) -> int:
		"""Call callback(event_dict) for every event, or only events named
		`event` ("button", "estop", "motion_done", "boot", "driver_fault", ...).
		Callbacks run on one dispatcher thread, never the reader, so they may
		send commands. Returns a token for remove_event_listener()."""
		token = next(self._listener_ids)
		with self._listeners_lock:
			self._listeners[token] = (event, callback)
		return token

	def remove_event_listener(self, token: int) -> None:
		with self._listeners_lock:
			self._listeners.pop(token, None)

	def wait_for_event(self, event: str, timeout: float = 5.0,
			predicate: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Optional[Dict[str, Any]]:
		"""Block until a matching event arrives; None on timeout."""
		hit: List[Dict[str, Any]] = []
		done = threading.Event()

		def on_event(ev: Dict[str, Any]) -> None:
			if not done.is_set() and (predicate is None or predicate(ev)):
				hit.append(ev)
				done.set()

		token = self.add_event_listener(on_event, event)
		try:
			done.wait(timeout)
		finally:
			self.remove_event_listener(token)
		return hit[0] if hit else None

	# --- requests -----------------------------------------------------------

	def _check_link(self) -> serial.Serial:
		if self._failure is not None:
			raise ESP32TransportError(f"ESP32 link lost: {self._failure}")
		if self.ser is None:
			raise ESP32TransportError("ESP32 connection is closed")
		return self.ser

	def _arm(self, request_id: int) -> _Waiter:
		waiter = _Waiter(request_id)
		with self._waiter_lock:
			self._waiter = waiter
		return waiter

	def _await(self, waiter: _Waiter, timeout: float) -> Optional[Dict[str, Any]]:
		waiter.event.wait(timeout)
		with self._waiter_lock:
			if self._waiter is waiter:
				self._waiter = None
		if waiter.response is None:
			self._check_link()
		return waiter.response

	def _write(self, data: bytes) -> None:
		ser = self._check_link()
		try:
			ser.write(data)
			ser.flush()
		except (serial.SerialException, OSError) as exc:
			self._failure = exc
			raise ESP32TransportError(f"ESP32 write failed: {exc}") from exc

	def _line(self, payload: Dict[str, Any], request_id: int) -> bytes:
		body = dict(payload)
		body["id"] = request_id
		return (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")

	@staticmethod
	def _parse_response(resp: Dict[str, Any]) -> Dict[str, Any]:
		if resp.get("status") != "ok":
			raise ESP32Error(str(resp.get("message", "ESP32 error")), resp)
		data = resp.get("data", {})
		return data if isinstance(data, dict) else {"value": data}

	def send(self, payload: Dict[str, Any], timeout: Optional[float] = None, *,
			retry: Optional[bool] = None) -> Dict[str, Any]:
		"""Send one command and return its "data". Raises ESP32Error for an
		error reply, TimeoutError if none arrives, ESP32TransportError if the
		link is gone. Read-only commands are retried once on a lost reply;
		motion commands never are."""
		wait_s = float(timeout if timeout is not None else self.cfg.timeout)
		if retry is None:
			retry = payload.get("cmd") in _IDEMPOTENT_COMMANDS
		attempts = 2 if retry else 1
		with self._lock:
			for _ in range(attempts):
				request_id = next(self._ids)
				waiter = self._arm(request_id)
				self._write(self._line(payload, request_id))
				resp = self._await(waiter, wait_s)
				if resp is not None:
					return self._parse_response(resp)
		raise TimeoutError(f"No response from ESP32 to {payload.get('cmd')!r} within {wait_s:.1f}s")

	def send_binary(
		self,
		payload: Dict[str, Any],
		binary_data: bytes | bytearray | memoryview,
		timeout: Optional[float] = None,
	) -> Dict[str, Any]:
		"""A JSON header whose reply only comes after the raw payload."""
		data_bytes = memoryview(binary_data).tobytes()
		baudrate = max(1, int(self.cfg.baudrate))
		transfer_timeout = (len(data_bytes) * 10.0 / baudrate) + 5.0
		if timeout is not None:
			transfer_timeout = max(float(timeout), transfer_timeout)
		with self._lock:
			request_id = next(self._ids)
			waiter = self._arm(request_id)
			self._write(self._line(payload, request_id))
			# Let the firmware parse the header and allocate before the bytes.
			time.sleep(0.02)
			self._write(data_bytes)
			resp = self._await(waiter, transfer_timeout)
		if resp is None:
			raise TimeoutError("No response from ESP32 after binary payload")
		return self._parse_response(resp)

	# --- general commands ---------------------------------------------------

	def ping(self) -> Dict[str, Any]:
		return self.send({"cmd": "ping"})

	def info(self) -> Dict[str, Any]:
		return self.send({"cmd": "info"}, timeout=2.0)

	def reboot(self) -> None:
		"""Software restart; waits for the firmware's boot event."""
		self.send({"cmd": "reboot"})
		self.wait_for_event("boot", timeout=5.0)

	def reset_esp32(self, bootloader: bool = False, wait_for_boot: bool = True) -> Optional[Dict[str, Any]]:
		"""Hardware reset through DTR/RTS (USB) or GPIO18/GPIO23 (J7).
		With bootloader=True the ESP32 stays in the ROM loader for esptool."""
		from esp32.esp32_reset import reset_esp32 as _reset

		with self._lock:
			_reset(self.transport, self.ser, bootloader=bootloader)
		if bootloader or not wait_for_boot:
			return None
		return self.wait_for_event("boot", timeout=5.0)

	def list_motors(self) -> Dict[str, Any]:
		return self.send({"cmd": "list_motors"})

	def host_status(self) -> Dict[str, Any]:
		return self.send({"cmd": "host"})

	def set_tx_pacing(self, chunk_bytes: int, gap_us: int) -> Dict[str, Any]:
		return self.send({"cmd": "host", "action": "set", "tx_chunk": int(chunk_bytes), "tx_gap_us": int(gap_us)})

	def estop(self) -> Dict[str, Any]:
		"""Stop all motion and latch the emergency stop."""
		return self.send({"cmd": "estop", "action": "trigger"})

	def clear_estop(self) -> Dict[str, Any]:
		return self.send({"cmd": "estop", "action": "clear"})

	def estop_status(self) -> Dict[str, Any]:
		return self.send({"cmd": "estop", "action": "status"})

	def stop_all(self) -> Dict[str, Any]:
		return self.send({"cmd": "stop_all"})

	def i2c_scan(self) -> List[int]:
		return list(self.send({"cmd": "i2c_scan"}, timeout=2.0).get("devices", []))

	def panel_status(self) -> Dict[str, Any]:
		return self.send({"cmd": "panel", "action": "status"})

	def panel_reinit(self) -> Dict[str, Any]:
		return self.send({"cmd": "panel", "action": "reinit"}, timeout=2.0)

	def imu_status(self) -> Dict[str, Any]:
		return self.send({"cmd": "imu", "action": "status"})

	def imu_read(self) -> Dict[str, Any]:
		return self.send({"cmd": "imu", "action": "read"})

	def imu_enable(self, on: bool = True) -> Dict[str, Any]:
		return self.send({"cmd": "imu", "action": "enable" if on else "disable"})

	# --- display storage ------------------------------------------------------

	def upload_display_file(self, name: str, data: bytes, timeout: Optional[float] = None) -> Dict[str, Any]:
		"""Store raw bytes on the ESP32's LittleFS for later playback.

		store_begin is acknowledged at once; the bytes follow and a second
		acknowledgement (same id) comes once the file is written."""
		baudrate = max(1, int(self.cfg.baudrate))
		transfer_timeout = timeout if timeout is not None else max(
			self.cfg.timeout, (len(data) * 10.0 / baudrate) + 20.0, 25.0)
		payload = {"cmd": "display", "action": "store_begin", "name": name, "length": len(data)}
		with self._lock:
			request_id = next(self._ids)
			waiter = self._arm(request_id)
			self._write(self._line(payload, request_id))
			first = self._await(waiter, max(self.cfg.timeout, 2.0))
			if first is None:
				raise TimeoutError("No response from ESP32 to store_begin")
			self._parse_response(first)
			final_waiter = self._arm(request_id)
			view = memoryview(data)
			for start in range(0, len(view), 512):
				self._write(view[start:start + 512].tobytes())
				# Pace the upload a little so the ESP32's RX buffer keeps up.
				time.sleep(0.001)
			resp = self._await(final_waiter, transfer_timeout)
		if resp is None:
			raise TimeoutError("No response from ESP32 after upload")
		return self._parse_response(resp)

	def play_display_file(self, name: str, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None) -> Dict[str, Any]:
		"""Play a stored raw RGB565 frame (w*h*2 bytes)."""
		if w is None:
			w = _DISPLAY_WIDTH
		if h is None:
			h = _DISPLAY_HEIGHT
		payload = {"cmd": "display", "action": "play", "name": name, "x": int(x), "y": int(y), "w": int(w), "h": int(h)}
		return self.send(payload, timeout=3.0)

	def play_sequence(self, files: list[str], durations_ms: list[int] | None = None, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None, timeout: float | None = None) -> Dict[str, Any]:
		"""Play stored frames on the ESP32 for smooth animation."""
		if w is None:
			w = _DISPLAY_WIDTH
		if h is None:
			h = _DISPLAY_HEIGHT
		payload: Dict[str, Any] = {"cmd": "display", "action": "play_sequence", "files_csv": ",".join(files), "x": int(x), "y": int(y), "w": int(w), "h": int(h)}
		if durations_ms:
			payload["durations_csv"] = ",".join(str(int(d)) for d in durations_ms)
		if timeout is None:
			timeout = 5.0 + sum(durations_ms or [100] * len(files)) / 1000.0
		return self.send(payload, timeout=timeout)

	def play_sequence_prefix(self, prefix: str, count: int, start: int = 0, pad: int = 4, durations_ms: list[int] | None = None, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None, timeout: float | None = None) -> Dict[str, Any]:
		"""Device-side playback of <prefix><zero-padded index>.rgb frames."""
		if w is None:
			w = _DISPLAY_WIDTH
		if h is None:
			h = _DISPLAY_HEIGHT
		payload: Dict[str, Any] = {"cmd": "display", "action": "play_sequence_prefix", "prefix": prefix, "count": int(count), "start": int(start), "pad": int(pad), "x": int(x), "y": int(y), "w": int(w), "h": int(h)}
		if durations_ms:
			payload["durations_csv"] = ",".join(str(int(d)) for d in durations_ms)
		if timeout is None:
			timeout = 5.0 + sum(durations_ms or [100] * int(count)) / 1000.0
		return self.send(payload, timeout=timeout)

	def delete_motor(self, motor_id: str) -> Dict[str, Any]:
		self.motor_info.pop(motor_id, None)
		return self.send({"cmd": "delete_motor", "motor": motor_id})

	# --- the "board" LED (dev kit onboard LED; unconnected on v0.8) ----------

	def led_on(self) -> Dict[str, Any]:
		return self.send({"cmd": "led", "led": "board", "mode": "on"})

	def led_off(self) -> Dict[str, Any]:
		return self.send({"cmd": "led", "led": "board", "mode": "off"})

	def led_blink(self, interval_ms: int = 500, auto_off_ms: Optional[int] = None) -> Dict[str, Any]:
		payload: Dict[str, Any] = {
			"cmd": "led",
			"led": "board",
			"mode": "blink",
			"interval_ms": int(interval_ms),
		}
		if auto_off_ms is not None:
			payload["auto_off_ms"] = int(auto_off_ms)
		return self.send(payload)


class ESP32LED:
	class Channel:
		def __init__(self, conn: ESP32Connection, name: str, pin: Optional[int] = None) -> None:
			self.conn = conn
			self.name = name
			self.pin = pin  # GPIO for "board"; None for the MCP23017 LEDs

		def action(
			self,
			mode: Optional[str] = None,
			on: Optional[bool] = None,
			interval_ms: int = 500,
			auto_off_ms: Optional[int] = None,
		) -> Dict[str, Any]:
			payload: Dict[str, Any] = {"cmd": "led", "led": self.name}
			if mode is not None:
				payload["mode"] = mode
			elif on is not None:
				payload["on"] = bool(on)
			else:
				payload["mode"] = "on"
			if payload.get("mode") == "blink":
				payload["interval_ms"] = int(interval_ms)
				if auto_off_ms is not None:
					payload["auto_off_ms"] = int(auto_off_ms)
			return self.conn.send(payload)

		def on(self) -> Dict[str, Any]:
			return self.action(mode="on")

		def off(self) -> Dict[str, Any]:
			return self.action(mode="off")

		def toggle(self) -> Dict[str, Any]:
			return self.action(mode="toggle")

		def blink(self, interval_ms: int = 500, auto_off_ms: Optional[int] = None) -> Dict[str, Any]:
			return self.action(mode="blink", interval_ms=interval_ms, auto_off_ms=auto_off_ms)

	def __init__(self, conn: ESP32Connection) -> None:
		self.conn = conn
		leds = get_led_channels()
		for led_name, led_cfg in leds.items():
			channel_name = str(led_cfg.get("channel", led_name))
			pin = led_cfg.get("pin")
			setattr(self, led_name, self.Channel(conn, channel_name, int(pin) if pin is not None else None))

		self.board = getattr(self, "board")
		self.boardLED = self.board

	@classmethod
	def attach(cls, conn: ESP32Connection) -> "ESP32LED":
		instance = cls(conn)
		for led_name in get_led_channels().keys():
			setattr(cls, led_name, getattr(instance, led_name))
		cls.board = instance.board
		cls.boardLED = instance.board
		return instance


class ESP32Buttons:
	"""Front-panel button events from the MCP23017 (SELENO v0.8).

	Buttons: select, return, top, bottom, left, right. Actions: "press",
	"release" (with held_ms) and "hold" (once, after 1 s). RETURN also
	triggers the firmware's emergency stop on its own; this class only
	reports it.

		buttons = ESP32Buttons(conn)
		buttons.on("select", "press", lambda ev: print("select!"))
	"""

	NAMES = ("select", "return", "top", "bottom", "left", "right")

	def __init__(self, conn: ESP32Connection) -> None:
		self.conn = conn
		self._handlers: List[tuple[Optional[str], Optional[str], EventCallback]] = []
		self._lock = threading.Lock()
		self._token = conn.add_event_listener(self._on_event, "button")

	def on(self, button: Optional[str], action: Optional[str], callback: EventCallback) -> None:
		"""callback(event) for button (None = any) and action (None = any)."""
		if button is not None and button not in self.NAMES:
			raise ValueError(f"Unknown button {button!r}; expected one of {self.NAMES}")
		with self._lock:
			self._handlers.append((button, action, callback))

	def close(self) -> None:
		self.conn.remove_event_listener(self._token)

	def state(self) -> Dict[str, bool]:
		return dict(self.conn.panel_status().get("buttons", {}))

	def _on_event(self, event: Dict[str, Any]) -> None:
		with self._lock:
			handlers = list(self._handlers)
		for button, action, callback in handlers:
			if button is not None and event.get("button") != button:
				continue
			if action is not None and event.get("action") != action:
				continue
			callback(event)


def _microsteps_arg(value: int | str) -> int | str:
	if isinstance(value, str):
		if value.strip().lower() == "auto":
			return "auto"
		return int(value)
	return int(value)


class ESP32Motor:
	"""One motor axis. motor1 = RA (U7, J1), motor2 = DEC (U8, J2) on v0.8.

	Positions and speeds are in logical steps: steps_per_rev per revolution
	(1600 by default). That unit doesn't change with the driver's microstep
	setting, which by default the firmware picks per move: fine steps for
	tracking, coarse for slews (see set_microsteps / set_auto). steps_per_rev
	is cached on the connection, so creating ESP32Motor objects is free."""

	def __init__(self, conn: ESP32Connection, motor_id: str) -> None:
		self.conn = conn
		self.motor_id = motor_id
		self._current_speed_us = 1250

	# --- cached facts ---------------------------------------------------------

	def _remember(self, data: Dict[str, Any]) -> Dict[str, Any]:
		if isinstance(data, dict) and "steps_per_rev" in data:
			info = self.conn.motor_info.setdefault(self.motor_id, {})
			info["steps_per_rev"] = int(data["steps_per_rev"])
			if "microsteps" in data:
				info["microsteps"] = int(data["microsteps"])
			if "speed_us" in data and data["speed_us"]:
				self._current_speed_us = int(data["speed_us"])
		return data

	@property
	def _steps_per_rev(self) -> int:
		info = self.conn.motor_info.get(self.motor_id)
		if info is None or "steps_per_rev" not in info:
			try:
				self.status()
			except Exception:
				return DEFAULT_STEPS_PER_REV
			info = self.conn.motor_info.get(self.motor_id, {})
		return int(info.get("steps_per_rev", DEFAULT_STEPS_PER_REV))

	@_steps_per_rev.setter
	def _steps_per_rev(self, value: int) -> None:
		self.conn.motor_info.setdefault(self.motor_id, {})["steps_per_rev"] = int(value)

	@property
	def steps_per_rev(self) -> int:
		return self._steps_per_rev

	@classmethod
	def create(
		cls,
		conn: ESP32Connection,
		motor_id: str,
		step_pin: int,
		dir_pin: int,
		en_pin: int,
		steps_per_rev: int = DEFAULT_STEPS_PER_REV,
		speed_us: Optional[int] = None,
		engage: bool = False,
		replace: bool = False,
		microsteps: Optional[int | str] = None,
		chopper: Optional[str] = None,
	) -> "ESP32Motor":
		"""Bind motor_id to the axis with these pins and configure it.

		The v0.8 board's axes are fixed (pins in esp32/pins.py) and boot
		registered as motor1/motor2, so this is a reconfigure. replace=True
		first releases the id (old behaviour)."""
		if replace:
			try:
				conn.delete_motor(motor_id)
			except ESP32Error:
				pass  # it didn't exist
		payload: Dict[str, Any] = {
			"cmd": "create_motor",
			"motor": motor_id,
			"step": int(step_pin),
			"dir": int(dir_pin),
			"en": int(en_pin),
			"engage": bool(engage),
		}
		payload["steps_per_rev"] = int(steps_per_rev)
		if microsteps is not None:
			payload["microsteps"] = _microsteps_arg(microsteps)
		if speed_us is not None:
			payload["speed_us"] = int(speed_us)
		instance = cls(conn, motor_id)
		data = conn.send(payload, timeout=2.0)
		if not instance._remember(data):
			instance._steps_per_rev = steps_per_rev
		if speed_us is not None:
			instance._current_speed_us = speed_us
		if chopper is not None:
			instance.set_chopper(chopper)
		return instance

	# --- enable and speed -----------------------------------------------------

	def engage(self) -> Dict[str, Any]:
		return self.conn.send({"cmd": "engage", "motor": self.motor_id})

	def disengage(self) -> Dict[str, Any]:
		return self.conn.send({"cmd": "disengage", "motor": self.motor_id})

	def enable(self, on: bool) -> Dict[str, Any]:
		return self.conn.send({"cmd": "enable", "motor": self.motor_id, "value": bool(on)})

	def set_speed_us(self, delay_us: int) -> Dict[str, Any]:
		self._current_speed_us = int(delay_us)
		return self.conn.send({"cmd": "set_speed", "motor": self.motor_id, "speed_us": int(delay_us)})

	def set_speed_sps(self, steps_per_sec: float) -> Dict[str, Any]:
		self._current_speed_us = int(1_000_000 / steps_per_sec)
		return self.conn.send({"cmd": "set_speed", "motor": self.motor_id, "sps": float(steps_per_sec)})

	# --- motion ---------------------------------------------------------------

	def turn_degrees(self, degrees: float, forward: bool = True, timeout: Optional[float] = None, waitUntilFinished: bool = False) -> Dict[str, Any]:
		if degrees < 0:
			degrees = abs(degrees)
			forward = not forward

		steps = int((degrees / 360.0) * self._steps_per_rev)
		duration_s = (steps * self._current_speed_us) / 1_000_000

		if waitUntilFinished:
			initial_position = self.get_position_degrees()
			target_position = initial_position + degrees if forward else initial_position - degrees
			done_token = threading.Event()
			token = self.conn.add_event_listener(
				lambda ev: done_token.set() if ev.get("motor") == self.motor_id else None,
				"motion_done",
			)

		try:
			result = self.conn.send(
				{
					"cmd": "turn_degrees",
					"motor": self.motor_id,
					"degrees": float(degrees),
					"forward": bool(forward),
				},
				timeout=timeout if timeout is not None else max(self.conn.cfg.timeout, 2.0),
			)

			if waitUntilFinished:
				# The firmware reports the end of the move; poll as a fallback.
				deadline = time.monotonic() + duration_s + 5.0
				while time.monotonic() < deadline and not done_token.wait(0.1):
					if not self.status().get("moving", False):
						break
				current_position = self.get_position_degrees()
				result["final_position"] = current_position
				result["target_position"] = target_position
				result["position_error"] = abs(current_position - target_position)
		finally:
			if waitUntilFinished:
				self.conn.remove_event_listener(token)

		return result

	def move_steps(self, steps: int) -> Dict[str, Any]:
		"""Signed relative move in (micro)steps."""
		return self.conn.send({"cmd": "move_steps", "motor": self.motor_id, "steps": int(steps)})

	def start_continuous(self, forward: bool = True) -> Dict[str, Any]:
		"""Run at the set speed until stopped (sidereal tracking path)."""
		return self.conn.send(
			{
				"cmd": "start_continuous",
				"motor": self.motor_id,
				"forward": bool(forward),
			}
		)

	def stop(self) -> Dict[str, Any]:
		return self.conn.send({"cmd": "stop", "motor": self.motor_id})

	def status(self) -> Dict[str, Any]:
		return self._remember(self.conn.send({"cmd": "status", "motor": self.motor_id}))

	# --- position ---------------------------------------------------------------

	def get_position(self) -> int:
		"""Signed position in steps at the current microstep setting."""
		result = self._remember(self.conn.send({"cmd": "get_position", "motor": self.motor_id}))
		return int(result.get("position", 0))

	def get_position_degrees(self, gear_ratio: float = 360.0) -> float:
		# gear_ratio: sky degrees per motor revolution (360 for RA, 144 for DEC)
		steps = self.get_position()
		return (steps * gear_ratio) / self._steps_per_rev

	def get_position_arcmin(self, gear_ratio: float = 360.0) -> float:
		return self.get_position_degrees(gear_ratio) * 60

	def get_position_arcsec(self, gear_ratio: float = 360.0) -> float:
		return self.get_position_arcmin(gear_ratio) * 60

	def get_position_fine(self) -> int:
		"""Exact position in 1/256 microsteps (51200 per revolution)."""
		result = self._remember(self.conn.send({"cmd": "get_position", "motor": self.motor_id}))
		return int(result.get("position_fine", 0))

	def reset_position(self) -> Dict[str, Any]:
		return self.conn.send({"cmd": "reset_position", "motor": self.motor_id})

	def set_position(self, steps: int) -> Dict[str, Any]:
		return self.conn.send({"cmd": "set_position", "motor": self.motor_id, "position": int(steps)})

	# --- TMC2209 driver settings (over its UART) --------------------------------

	def set_microsteps(self, microsteps: int | str) -> Dict[str, Any]:
		"""Driver microsteps: "auto" (default; the firmware picks per move,
		up to 1/256 when tracking, as coarse as 1/4 for fast slews) or a fixed
		power of two 1-256. Positions and speeds keep their units either way.
		A fixed value is refused while the motor moves."""
		return self._remember(self.conn.send(
			{"cmd": "set_microsteps", "motor": self.motor_id, "microsteps": _microsteps_arg(microsteps)}, timeout=2.0))

	def set_auto(self, min_microsteps: Optional[int] = None, max_microsteps: Optional[int] = None,
			max_pulse_hz: Optional[float] = None) -> Dict[str, Any]:
		"""Tune automatic microsteps: the finest power of two in
		[min_microsteps, max_microsteps] whose pulse rate stays within
		max_pulse_hz (defaults 4, 256, 5000)."""
		payload: Dict[str, Any] = {"cmd": "set_auto", "motor": self.motor_id}
		if min_microsteps is not None:
			payload["min_microsteps"] = int(min_microsteps)
		if max_microsteps is not None:
			payload["max_microsteps"] = int(max_microsteps)
		if max_pulse_hz is not None:
			payload["max_pulse_hz"] = float(max_pulse_hz)
		return self._remember(self.conn.send(payload, timeout=2.0))

	def set_accel(self, accel_sps2: float, start_sps: Optional[float] = None) -> Dict[str, Any]:
		"""Acceleration ramp in this motor's steps/s^2 (0 = start and stop
		instantly) and the speed a move may start at without ramping
		(defaults 3200 and 400 at 1600 steps/rev: 2 rev/s^2, 0.25 rev/s).
		Moves land on their exact step count; stop() ramps down, the
		emergency stop doesn't."""
		payload: Dict[str, Any] = {"cmd": "set_accel", "motor": self.motor_id, "accel": float(accel_sps2)}
		if start_sps is not None:
			payload["start_sps"] = float(start_sps)
		return self._remember(self.conn.send(payload, timeout=2.0))

	def set_steps_per_rev(self, steps_per_rev: int) -> Dict[str, Any]:
		"""Change the logical unit (200 x a power of two). The position keeps
		its angle and the speed its angular speed."""
		return self._remember(self.conn.send(
			{"cmd": "set_steps_per_rev", "motor": self.motor_id, "steps_per_rev": int(steps_per_rev)}, timeout=2.0))

	def set_chopper(self, mode: str, hybrid_threshold_sps: Optional[float] = None,
			hybrid_threshold_fsps: Optional[float] = None) -> Dict[str, Any]:
		""""auto" (default: StealthChop below 1 rev/s, SpreadCycle above),
		"stealthchop" (silent), "spreadcycle", or "hybrid" with a threshold in
		this motor's steps/s or in full steps/s."""
		payload: Dict[str, Any] = {"cmd": "set_chopper", "motor": self.motor_id, "mode": mode}
		if hybrid_threshold_sps is not None:
			payload["hybrid_threshold_sps"] = float(hybrid_threshold_sps)
		if hybrid_threshold_fsps is not None:
			payload["hybrid_threshold_fsps"] = float(hybrid_threshold_fsps)
		return self._remember(self.conn.send(payload, timeout=2.0))

	def set_current(self, run_ma: int, hold_ma: Optional[int] = None,
			hold_multiplier: Optional[float] = None) -> Dict[str, Any]:
		"""RMS run current (max 1350 mA on v0.8) and optional hold current."""
		payload: Dict[str, Any] = {"cmd": "set_current", "motor": self.motor_id, "run_ma": int(run_ma)}
		if hold_multiplier is not None:
			payload["hold_multiplier"] = float(hold_multiplier)
		elif hold_ma is not None:
			payload["hold_ma"] = int(hold_ma)
		return self.conn.send(payload, timeout=2.0)

	def driver_status(self) -> Dict[str, Any]:
		"""Live TMC2209 registers: DRV_STATUS flags, temperature, microsteps..."""
		return self.conn.send({"cmd": "driver", "motor": self.motor_id, "action": "status"}, timeout=2.0)

	def driver_reinit(self) -> Dict[str, Any]:
		return self.conn.send({"cmd": "driver", "motor": self.motor_id, "action": "reinit"}, timeout=2.0)

	def driver_set(self, **settings: Any) -> Dict[str, Any]:
		"""interpolate=bool, stall_threshold=0-255, invert_direction=bool."""
		payload: Dict[str, Any] = {"cmd": "driver", "motor": self.motor_id, "action": "set"}
		payload.update(settings)
		return self.conn.send(payload, timeout=2.0)

	def read_register(self, reg: int) -> int:
		data = self.conn.send({"cmd": "driver", "motor": self.motor_id, "action": "read_reg", "reg": int(reg)})
		return int(data.get("value", 0))


class ESP32Display:
	# Control the ST7735S TFT LCD display connected to ESP32.

	# Display constants from centralized hardware map
	_WIDTH_HEIGHT = get_display_config()
	WIDTH = int(_WIDTH_HEIGHT["width"])
	HEIGHT = int(_WIDTH_HEIGHT["height"])

	# Standard colors (RGB565)
	COLORS = {
		"black": "000000",
		"red": "FF0000",
		"green": "00FF00",
		"blue": "0000FF",
		"white": "FFFFFF",
		"yellow": "FFFF00",
		"cyan": "00FFFF",
		"magenta": "FF00FF",
	}

	def __init__(self, conn: ESP32Connection) -> None:
		self.conn = conn
		self._initialized = False
		self._brightness = 255
		self._text_color = "FFFFFF"
		self._bg_color = "000000"
		self._cursor_x = 0
		self._cursor_y = 0

	def initialize(self) -> Dict[str, Any]:
		# Initialize the display hardware.
		result = self.conn.send({"cmd": "display", "action": "init"}, timeout=5.0)
		self._initialized = True
		return result

	def cleanup(self) -> None:
		# Clean up display resources.
		if self._initialized:
			self.power(False)
			self._initialized = False

	def power(self, on: bool) -> Dict[str, Any]:
		# Turn display power on/off.
		return self.conn.send({"cmd": "display", "action": "power", "on": bool(on)})

	def set_backlight(self, brightness: int) -> Dict[str, Any]:
		# Set backlight brightness (0-255).
		# 
		# Args:
		#	brightness: PWM value 0-255 where 0 is off and 255 is full brightness
		brightness = max(0, min(255, int(brightness)))
		self._brightness = brightness
		return self.conn.send({"cmd": "display", "action": "backlight", "brightness": brightness})

	def get_status(self) -> Dict[str, Any]:
		# Get current display status.
		return self.conn.send({"cmd": "display", "action": "status"})

	def format_storage(self) -> Dict[str, Any]:
		# Format persistent display storage on the ESP32 (LittleFS).
		return self.conn.send({"cmd": "display", "action": "format_storage"}, timeout=10.0)

	def storage_info(self) -> Dict[str, Any]:
		"""Return storage total/used/free bytes from the ESP32."""
		return self.conn.send({"cmd": "display", "action": "storage_info"}, timeout=5.0)

	def list_files(self) -> Dict[str, Any]:
		"""Return a list of files stored on the ESP32 LittleFS."""
		return self.conn.send({"cmd": "display", "action": "list_files"}, timeout=5.0)

	def delete_file(self, name: str) -> Dict[str, Any]:
		"""Delete a file on the ESP32 LittleFS by name."""
		return self.conn.send({"cmd": "display", "action": "delete_file", "name": name}, timeout=5.0)

	def blit_rgb565(self, x: int, y: int, width: int, height: int, data: bytes | bytearray | memoryview) -> Dict[str, Any]:
		# Upload a full RGB565 frame or sub-frame in one binary transfer.
		payload = {
			"cmd": "display",
			"action": "blit",
			"x": int(x),
			"y": int(y),
			"w": int(width),
			"h": int(height),
			"format": "RGB565",
			"length": int(len(memoryview(data))),
		}
		return self.conn.send_binary(payload, data)

	def blit_rgb565_compressed(self, x: int, y: int, width: int, height: int, data: bytes | bytearray | memoryview, compress_level: int = 6) -> Dict[str, Any]:
		# Upload an RGB565 frame with optional zlib compression.
		# The firmware decompresses on receive if the frame was compressed.
		data_bytes = bytes(memoryview(data).tobytes())
		compressed = zlib.compress(data_bytes, level=compress_level)
		
		# Only use compression if it saves space (avoid overhead for random data)
		if len(compressed) < len(data_bytes) * 0.95:  # 5% savings threshold
			payload = {
				"cmd": "display",
				"action": "blit",
				"x": int(x),
				"y": int(y),
				"w": int(width),
				"h": int(height),
				"format": "RGB565",
				"compress": "zlib",
				"length": int(len(compressed)),
				"uncompressed_length": int(len(data_bytes)),
			}
			return self.conn.send_binary(payload, compressed, timeout=15.0)
		else:
			# Fallback to uncompressed if compression didn't help
			return self.blit_rgb565(x, y, width, height, data)

	def clear(self, color: Optional[str] = None) -> Dict[str, Any]:
		# Clear the display with a background color.
		# 
		# Args:
		#	color: Hex color string (e.g., "000000" for black) or color name
		if color is None:
			color = self._bg_color
		else:
			color = self._normalize_color(color)
			self._bg_color = color

		return self.conn.send({"cmd": "display", "action": "clear", "color": color}, timeout=3.0)

	def fill_screen(self, color: Optional[str] = None) -> Dict[str, Any]:
		# Fill entire screen with color (alias for clear).
		return self.clear(color)

	# Drawing functions
	def draw_pixel(self, x: int, y: int, color: Optional[str] = None) -> Dict[str, Any]:
		# Draw a single pixel.
		if color is None:
			color = self._text_color
		else:
			color = self._normalize_color(color)

		if not self._validate_coords(x, y):
			raise ValueError(f"Coordinates ({x}, {y}) out of display bounds")

		return self.conn.send(
			{"cmd": "display", "action": "draw_pixel", "x": x, "y": y, "color": color}
		)

	def draw_rectangle(
		self,
		x: int,
		y: int,
		width: int,
		height: int,
		color: Optional[str] = None,
		fill: bool = False,
	) -> Dict[str, Any]:
		# Draw a rectangle.
		# 
		# Args:
		#	x, y: Top-left corner coordinates
		#	width, height: Rectangle dimensions
		#	color: Border/fill color
		#	fill: If True, fill the rectangle; if False, draw outline only
		if color is None:
			color = self._text_color
		else:
			color = self._normalize_color(color)

		action = "fill_rect" if fill else "draw_rect"
		# Use longer timeout for filled rectangles (more SPI data)
		timeout = 3.0 if fill else 1.0
		return self.conn.send(
			{
				"cmd": "display",
				"action": action,
				"x": int(x),
				"y": int(y),
				"w": int(width),
				"h": int(height),
				"color": color,
			},
			timeout=timeout,
		)

	def fill_rectangle(
		self, x: int, y: int, width: int, height: int, color: Optional[str] = None
	) -> Dict[str, Any]:
		# Fill a rectangle with color.
		return self.draw_rectangle(x, y, width, height, color, fill=True)

	def draw_line(
		self, x0: int, y0: int, x1: int, y1: int, color: Optional[str] = None
	) -> Dict[str, Any]:
		# Draw a line from (x0, y0) to (x1, y1).
		if color is None:
			color = self._text_color
		else:
			color = self._normalize_color(color)

		return self.conn.send(
			{
				"cmd": "display",
				"action": "draw_line",
				"x0": int(x0),
				"y0": int(y0),
				"x1": int(x1),
				"y1": int(y1),
				"color": color,
			}
		)

	def draw_circle(
		self,
		x: int,
		y: int,
		radius: int,
		color: Optional[str] = None,
		fill: bool = False,
	) -> Dict[str, Any]:
		# Draw a circle.
		# 
		# Args:
		#	x, y: Center coordinates
		#	radius: Circle radius in pixels
		#	color: Circle color
		#	fill: If True, fill the circle; if False, draw outline only
		if color is None:
			color = self._text_color
		else:
			color = self._normalize_color(color)

		action = "fill_circle" if fill else "draw_circle"
		# Use longer timeout for filled circles (more SPI data)
		timeout = 3.0 if fill else 1.0
		return self.conn.send(
			{
				"cmd": "display",
				"action": action,
				"x": int(x),
				"y": int(y),
				"r": int(radius),
				"color": color,
			},
			timeout=timeout,
		)

	def fill_circle(
		self, x: int, y: int, radius: int, color: Optional[str] = None
	) -> Dict[str, Any]:
		# Fill a circle with color.
		return self.draw_circle(x, y, radius, color, fill=True)

	# Text functions
	def set_cursor(self, x: int, y: int) -> Dict[str, Any]:
		# Set cursor position for text rendering.
		self._cursor_x = int(x)
		self._cursor_y = int(y)
		return self.conn.send(
			{"cmd": "display", "action": "set_cursor", "x": self._cursor_x, "y": self._cursor_y}
		)

	def set_text_color(self, color: str) -> Dict[str, Any]:
		# Set text color.
		color = self._normalize_color(color)
		self._text_color = color
		return self.conn.send({"cmd": "display", "action": "set_text_color", "color": color})

	def set_background_color(self, color: str) -> Dict[str, Any]:
		# Set background color.
		color = self._normalize_color(color)
		self._bg_color = color
		return self.conn.send({"cmd": "display", "action": "set_bg_color", "color": color})

	def set_text_size(self, size: int) -> Dict[str, Any]:
		return self.conn.send({"cmd": "display", "action": "set_text_size", "size": int(size)})

	def text(
		self,
		text: str,
		x: Optional[int] = None,
		y: Optional[int] = None,
		color: Optional[str] = None,
		background: Optional[str] = None,
		size: Optional[int] = None,
		newline: bool = False,
	) -> Dict[str, Any]:
		# Draw text with the firmware's built-in font (LovyanGFX), optionally
		# moving the cursor and setting size/colours first.
		payload: Dict[str, Any] = {"cmd": "display", "action": "text", "text": str(text), "newline": bool(newline)}
		if x is not None or y is not None:
			payload["x"] = int(x or 0)
			payload["y"] = int(y or 0)
		if color is not None:
			payload["color"] = self._normalize_color(color)
		if background is not None:
			payload["bg"] = self._normalize_color(background)
		if size is not None:
			payload["size"] = int(size)
		return self.conn.send(payload, timeout=2.0)

	def set_rotation(self, rotation: int) -> Dict[str, Any]:
		# 0-3, quarter turns. Width and height swap for 1 and 3.
		return self.conn.send({"cmd": "display", "action": "rotation", "rotation": int(rotation) & 3})

	# Utility methods
	@staticmethod
	def _normalize_color(color: str) -> str:
		# Normalize color to hex format.
		# 
		# Args:
		#	color: Can be a color name from COLORS dict or hex string
		# 
		# Returns:
		#	Hex color string (6 characters)
		if color.lower() in ESP32Display.COLORS:
			return ESP32Display.COLORS[color.lower()]
		
		# Clean up hex format
		color = color.lstrip("#").upper()
		if len(color) == 6 and all(c in "0123456789ABCDEF" for c in color):
			return color
		
		raise ValueError(f"Invalid color format: {color}")

	@staticmethod
	def _validate_coords(x: int, y: int) -> bool:
		# Check if coordinates are within display bounds.
		return 0 <= x < ESP32Display.WIDTH and 0 <= y < ESP32Display.HEIGHT

	def draw_test_pattern(self) -> Dict[str, Any]:
		# Draw a test pattern (colors, rectangles, text areas) for debugging.
		# Clear with black
		self.clear("black")
		
		# Draw colored rectangles
		self.fill_rectangle(0, 0, 32, 40, "red")
		self.fill_rectangle(32, 0, 32, 40, "green")
		self.fill_rectangle(64, 0, 32, 40, "blue")
		self.fill_rectangle(96, 0, 32, 40, "yellow")
		
		# Draw circles
		self.fill_circle(32, 80, 15, "cyan")
		self.fill_circle(96, 80, 15, "magenta")
		
		# Draw lines
		self.draw_line(0, 100, 128, 100, "white")
		self.draw_line(64, 60, 64, 160, "white")
		
		return self.get_status()


