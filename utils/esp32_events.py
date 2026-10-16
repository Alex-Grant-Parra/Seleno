"""App-level handling of events the ESP32 pushes to the Pi.

The firmware sends unsolicited JSON lines (see esp32-firmware/PROTOCOL.md):
button presses from the front panel, the emergency stop, motion_done, TMC
driver faults and a boot notice. ESP32Connection delivers them on its event
thread; attach() installs the app's policy for each:

* estop       RETURN press (or a host request) latched the stop and the
              firmware already halted every motor. Stop the Pi's tracking
              loop so it doesn't keep issuing moves, show the critical red
              LED. Clearing the stop turns the LED back to normal.
* boot        The ESP32 restarted: motors are disengaged and the step
              counters are zero. Tracking re-initialises the motors before
              its next move.
* driver_*    TMC2209 fault, over-temperature, brown-out reset or lost driver:
              logged; faults also show the error LED.
* button      Passed to handlers registered with on_button(). Nothing is
              bound by default except logging (RETURN's stop is handled on
              the ESP32 itself).

Handlers run on the connection's event thread, so they may send commands.
Anything slow goes to one worker thread that runs jobs in arrival order.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

ButtonCallback = Callable[[Dict[str, Any]], None]

_button_handlers: List[Tuple[Optional[str], Optional[str], ButtonCallback]] = []
_handlers_lock = threading.Lock()
_estop_active = False


def on_button(button: Optional[str], action: Optional[str], callback: ButtonCallback) -> None:
	"""Register callback(event) for a front-panel button.

	button: "select", "return", "top", "bottom", "left", "right" or None (any).
	action: "press", "release" (event has held_ms), "hold" (after 1 s) or None.
	"""
	with _handlers_lock:
		_button_handlers.append((button, action, callback))


def clear_button_handlers() -> None:
	with _handlers_lock:
		_button_handlers.clear()


def estop_active() -> bool:
	return _estop_active


def _leds():
	from utils.LEDmanager import get_led_manager

	return get_led_manager()


_work: "queue.Queue[Tuple[str, Callable[[], None]]]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _work_loop() -> None:
	while True:
		name, fn = _work.get()
		try:
			fn()
		except Exception as exc:
			print(f"[esp32_events] {name} failed: {exc}")


def _in_thread(name: str, fn: Callable[[], None]) -> None:
	"""Run fn off the event thread. One worker runs jobs in arrival order,
	so a slow stop can't finish after the clear that followed it."""
	global _worker
	with _worker_lock:
		if _worker is None or not _worker.is_alive():
			_worker = threading.Thread(target=_work_loop, name="esp32-app-events", daemon=True)
			_worker.start()
	_work.put((name, fn))


def _on_estop(event: Dict[str, Any]) -> None:
	global _estop_active
	active = bool(event.get("active"))
	_estop_active = active
	if active:
		print(f"[esp32_events] EMERGENCY STOP from {event.get('source', '?')}: all motors halted by the ESP32")

		def stop_everything() -> None:
			from core.hardware.tracking import stop_tracking_for_estop

			stop_tracking_for_estop()
			if _estop_active:  # not cleared while tracking was stopping
				_leds().set_error(True, critical=True)

		_in_thread("esp32-estop", stop_everything)
	else:
		print("[esp32_events] Emergency stop cleared")
		_in_thread("esp32-estop-clear", lambda: _leds().clear_error())


def _on_boot(event: Dict[str, Any]) -> None:
	print(f"[esp32_events] ESP32 booted (firmware {event.get('fw')}, drivers {event.get('drivers')})")

	def reinit() -> None:
		from core.hardware.tracking import on_esp32_reboot

		on_esp32_reboot()

	_in_thread("esp32-boot", reinit)


def _on_driver_event(event: Dict[str, Any]) -> None:
	name = event.get("event", "")
	if not name.startswith("driver_"):
		return
	motor = event.get("motor") or f"axis {event.get('axis')}"
	flags = event.get("drv_status") or {}
	raised = sorted(k for k, v in flags.items() if v is True)
	print(f"[esp32_events] {name} on {motor}" + (f": {', '.join(raised)}" if raised else ""))
	if name in ("driver_fault", "driver_lost"):
		_in_thread("esp32-driver-fault", lambda: _leds().set_error(True, critical=False))


def _on_button(event: Dict[str, Any]) -> None:
	button = event.get("button")
	action = event.get("action")
	if action != "hold":
		held = f" after {event['held_ms']} ms" if "held_ms" in event else ""
		print(f"[esp32_events] button {button} {action}{held}")
	with _handlers_lock:
		handlers = list(_button_handlers)
	for want_button, want_action, callback in handlers:
		if want_button is not None and want_button != button:
			continue
		if want_action is not None and want_action != action:
			continue
		try:
			callback(event)
		except Exception as exc:
			print(f"[esp32_events] button handler error: {exc}")


def _on_link_lost(event: Dict[str, Any]) -> None:
	print(f"[esp32_events] Serial link to the ESP32 lost: {event.get('error')}")

	def drop() -> None:
		from utils.esp32_state import esp32_state

		esp32_state.mark_disconnected()

	_in_thread("esp32-link-lost", drop)


def attach(conn) -> None:
	"""Install the app's event handlers on a new ESP32Connection."""
	conn.add_event_listener(_on_estop, "estop")
	conn.add_event_listener(_on_boot, "boot")
	conn.add_event_listener(_on_button, "button")
	conn.add_event_listener(_on_link_lost, "link_lost")
	conn.add_event_listener(_on_driver_event)
	# Pick up a stop that was latched before this connection existed.
	global _estop_active
	try:
		_estop_active = bool(conn.estop_status().get("active", False))
	except Exception:
		pass
