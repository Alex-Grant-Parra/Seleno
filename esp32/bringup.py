#!/usr/bin/env python3
"""Bring-up checks for the SELENO v0.8 board, from the Pi.

Follows the hardware report's "Bring-up and test" procedure for the steps
that can be checked over the serial link (9-13). The meter checks and first
power-up (steps 1-8) are done by hand before this.

	./venv/bin/python -m esp32.bringup all          # every check that needs no motor
	./venv/bin/python -m esp32.bringup leds         # one check
	./venv/bin/python -m esp32.bringup motor --motor motor1 --run-ma 400

Checks: info, i2c, drivers, leds, buttons, estop, lcd, imu, motor, profile
(profile: automatic microsteps/chopper while tracking and slewing).
Checks that need someone at the board ask before and after (skip the
questions with --yes; then look at the output instead).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esp32.interfaceESP32 import (  # noqa: E402
	ESP32Connection,
	ESP32Display,
	ESP32Error,
	ESP32Motor,
	ESP32SerialConfig,
)

MCP23017 = 0x20
BNO085 = (0x4A, 0x4B)
TMC2209_VERSION = 0x21
LEDS = ("red", "green", "white", "blue", "yellow")
BUTTONS = ("select", "return", "top", "bottom", "left", "right")


class Checks:
	def __init__(self, conn: ESP32Connection, assume_yes: bool) -> None:
		self.conn = conn
		self.assume_yes = assume_yes
		self.results: List[tuple[str, bool, str]] = []

	def record(self, name: str, ok: bool, detail: str = "") -> bool:
		self.results.append((name, ok, detail))
		print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
		return ok

	def ask(self, question: str) -> bool:
		if self.assume_yes:
			print(f"  (--yes) {question} -> assumed yes")
			return True
		answer = input(f"  {question} [y/n] ").strip().lower()
		return answer.startswith("y")

	def pause(self, prompt: str) -> None:
		if not self.assume_yes:
			input(f"  {prompt} (Enter to continue) ")

	# --- checks ---------------------------------------------------------------

	def info(self) -> None:
		print("\n== Firmware and link ==")
		info = self.conn.info()
		print(f"  port {self.conn.cfg.port} ({self.conn.transport}), firmware {info.get('fw')} "
			f"built {info.get('build')}, reset reason {info.get('reset_reason')}, free heap {info.get('heap_free')}")
		self.record("firmware is the v0.8 build", info.get("board") == "seleno-v0.8" and not info.get("devkit"),
			f"board={info.get('board')} devkit={info.get('devkit')}")
		host = self.conn.host_status()
		print(f"  host link {host.get('baud')} baud, ports {host.get('ports')}, "
			f"tx pacing {host.get('tx_chunk')} B / {host.get('tx_gap_us')} us")
		start = time.monotonic()
		for _ in range(50):
			self.conn.send({"cmd": "driver", "motor": "motor1", "action": "status"}, timeout=2.0)
		elapsed = time.monotonic() - start
		self.record("50 long replies without corruption", self.conn.stats["corrupt_lines"] == 0,
			f"{elapsed:.1f}s, stats {self.conn.stats}")

	def i2c(self) -> None:
		print("\n== I2C bus (step 9) ==")
		devices = self.conn.i2c_scan()
		print(f"  found: {[hex(d) for d in devices]}")
		self.record("MCP23017 at 0x20", MCP23017 in devices)
		if any(a in devices for a in BNO085):
			self.record("BNO085 IMU board", True, "present")
		else:
			print("  (no IMU board on J3, fine if it isn't plugged in)")

	def drivers(self) -> None:
		print("\n== TMC2209 drivers over UART (step 11) ==")
		for motor_id in ("motor1", "motor2"):
			m = ESP32Motor(self.conn, motor_id)
			status = m.driver_status()
			live = status.get("live", {})
			self.record(f"{motor_id} driver answers (version 0x21)",
				status.get("present") and status.get("version") == TMC2209_VERSION,
				f"version=0x{int(status.get('version', 0)):02X} address={status.get('address')}")
			self.record(f"{motor_id} configured (microsteps from UART, not straps)", bool(status.get("configured")),
				f"chip microsteps={live.get('microsteps')} settings={status.get('settings', {}).get('microsteps')}")
			before = live.get("ifcnt")
			m.set_current(status.get("settings", {}).get("run_ma", 1000))
			after = m.driver_status().get("live", {}).get("ifcnt")
			if before is not None and after is not None:
				self.record(f"{motor_id} IFCNT counts writes", after != before, f"{before} -> {after}")
			self.record(f"{motor_id} still disabled (ENN high) until engaged", not status.get("enabled"))
			flags = live.get("drv_status", {})
			bad = [k for k in ("ot", "otpw", "s2ga", "s2gb", "s2vsa", "s2vsb") if flags.get(k)]
			self.record(f"{motor_id} no fault flags", not bad and not status.get("diag"), ", ".join(bad))

	def leds(self) -> None:
		print("\n== Front-panel LEDs (step 9) ==")
		for name in LEDS:
			self.conn.send({"cmd": "led", "led": name, "mode": "off"})
		for name in LEDS:
			self.conn.send({"cmd": "led", "led": name, "mode": "on"})
			ok = self.ask(f"Is only the {name.upper()} LED lit?")
			self.conn.send({"cmd": "led", "led": name, "mode": "off"})
			self.record(f"{name} LED", ok)
		self.conn.send({"cmd": "led", "led": "white", "mode": "blink", "interval_ms": 150, "auto_off_ms": 1500})

	def buttons(self, seconds: float = 30.0) -> None:
		print("\n== Front-panel buttons (step 9) ==")
		print(f"  Press and release each button within {seconds:.0f} s, RETURN last "
			"(RETURN triggers the emergency stop; it will be cleared after).")
		seen: Dict[str, List[str]] = {}

		def on_button(ev: Dict[str, Any]) -> None:
			seen.setdefault(ev.get("button", "?"), []).append(ev.get("action", "?"))
			print(f"    {ev.get('button')} {ev.get('action')}" + (f" ({ev['held_ms']} ms)" if "held_ms" in ev else ""))

		token = self.conn.add_event_listener(on_button, "button")
		deadline = time.monotonic() + seconds
		try:
			while time.monotonic() < deadline and not all(
				"press" in seen.get(b, []) and "release" in seen.get(b, []) for b in BUTTONS):
				time.sleep(0.2)
		finally:
			self.conn.remove_event_listener(token)
		for b in BUTTONS:
			actions = seen.get(b, [])
			self.record(f"button {b}", "press" in actions and "release" in actions, ",".join(actions))
		panel = self.conn.panel_status()
		print(f"  EXP_INT interrupts seen: {panel.get('interrupts')}")
		self.record("EXP_INT (IO39) interrupt fires", int(panel.get("interrupts", 0)) > 0)
		if self.conn.estop_status().get("active"):
			self.conn.clear_estop()

	def estop(self) -> None:
		# RETURN's two jobs (see esp32-firmware/src/estop.h): one press during a
		# slew is the latched emergency stop; two quick presses while tracking
		# stop the motors and hold the Pi's moves until it acknowledges.
		print("\n== Emergency stop on RETURN (slewing) ==")
		m = ESP32Motor(self.conn, "motor1")
		started = False
		try:
			m.engage()
			m.set_speed_sps(1200)  # over the 50 full steps/s slew threshold
			m.start_continuous()
			started = True
		except ESP32Error as exc:
			print(f"  (motor1 can't run: {exc.error_message}; testing the latch from the host instead)")
		if started:
			print("  Press RETURN once now.")
			ev = self.conn.wait_for_event("estop", timeout=30.0, predicate=lambda e: e.get("active"))
			self.record("estop event from the button", ev is not None and ev.get("source") == "button", str(ev))
			self.conn.wait_for_event("motion_done", timeout=1.0)
			self.record("motor stopped by the estop", not m.status().get("moving"))
		else:
			self.conn.estop()
		try:
			m.move_steps(10)
			self.record("motion refused while latched", False)
		except ESP32Error as exc:
			self.record("motion refused while latched", "Emergency stop" in exc.error_message)
		self.conn.clear_estop()
		self.record("estop clears", not self.conn.estop_status().get("active"))

		print("\n== RETURN twice while tracking ==")
		try:
			m.set_speed_sps(7)  # about sidereal at 360:1
			m.start_continuous()
		except ESP32Error as exc:
			self.record("tracking-speed move starts", False, exc.error_message)
			return
		self.conn.ui("tracking", state="tracking", target="Bring-up")
		print("  The panel shows the tracking screen. Press RETURN twice, quickly.")
		ev = self.conn.wait_for_event("ui", timeout=30.0, predicate=lambda e: e.get("type") == "stop")
		self.record("panel stop event", ev is not None, str(ev))
		time.sleep(0.3)
		self.record("motor stopped", not m.status().get("moving"))
		self.record("panel back at the menu", self.conn.ui_state().get("screen") == "menu")
		try:
			m.move_steps(10)
			self.record("host moves held until acknowledged", False)
		except ESP32Error as exc:
			self.record("host moves held until acknowledged", "front panel" in exc.error_message)
		self.conn.ui("ack_stop")
		self.conn.ui("tracking", state="idle")
		try:
			m.disengage()
		except ESP32Error:
			pass

	def lcd(self) -> None:
		print("\n== LCD (step 10) ==")
		display = ESP32Display(self.conn)
		display.initialize()
		for color in ("red", "green", "blue"):
			display.clear(color)
			self.record(f"screen fills {color}", self.ask(f"Is the whole screen {color.upper()} (edges included)?"))
		display.clear("black")
		display.draw_rectangle(0, 0, 128, 160, "white")
		display.text("TOP LEFT", x=2, y=2, color="yellow")
		self.record("orientation and edges", self.ask("White border on all four edges, 'TOP LEFT' at the top left?"))
		for level in (255, 128, 32, 0, 255):
			display.set_backlight(level)
			time.sleep(0.4)
		self.record("backlight PWM", self.ask("Did the backlight dim and come back?"))
		# Drawing took the screen from the front-panel UI; hand it back.
		self.conn.ui("mode", mode="menu")
		self.record("menu UI back", self.ask("Is the SELENO menu on the screen again?"))

	def imu(self, seconds: float = 5.0) -> None:
		print("\n== IMU (BNO085 on J3) ==")
		status = self.conn.imu_status()
		if not status.get("present"):
			self.record("IMU present", False, "not found on 0x4A/0x4B (plug the IMU board into J3)")
			return
		self.record("IMU present", True, f"address 0x{int(status['address']):02X}, firmware {status.get('firmware')}")
		end = time.monotonic() + seconds
		while time.monotonic() < end:
			data = self.conn.imu_read()
			rv = data.get("rotation_vector", {}).get("euler_deg", {})
			g = data.get("gravity", {})
			print(f"    yaw {rv.get('yaw', 0):7.2f} pitch {rv.get('pitch', 0):7.2f} roll {rv.get('roll', 0):7.2f}"
				f"  gravity ({g.get('x', 0):.2f}, {g.get('y', 0):.2f}, {g.get('z', 0):.2f})")
			time.sleep(0.5)
		self.record("IMU reports", "rotation_vector" in self.conn.imu_read())

	def motor(self, motor_id: str, run_ma: int, microsteps: int, revolutions: float, sps: float) -> None:
		print(f"\n== First motor run: {motor_id} (step 12) ==")
		print("  Motors must have been plugged in with 12 V OFF, and each cable checked")
		print("  (XH pins 1-2 and 3-4 each read one coil, about 2-3 ohm).")
		if not self.ask(f"Ready to run {motor_id} at {run_ma} mA?"):
			return
		m = ESP32Motor(self.conn, motor_id)
		m.set_current(run_ma, hold_multiplier=0.5)
		m.set_microsteps(microsteps)
		m.set_chopper("stealthchop")
		m.engage()
		m.reset_position()
		m.set_speed_sps(sps)
		try:
			steps = int(m.steps_per_rev * revolutions)
			m.move_steps(steps)
			done = self.conn.wait_for_event("motion_done", timeout=steps / sps + 5.0,
				predicate=lambda e: e.get("motor") == motor_id)
			self.record("move completes", done is not None and done.get("reason") == "complete", str(done))
			self.record("position count", m.get_position() == steps, f"{m.get_position()} of {steps}")
			status = m.driver_status()
			flags = status.get("live", {}).get("drv_status", {})
			self.record("DIAG low, no fault flags", not status.get("diag") and not any(
				flags.get(k) for k in ("ot", "otpw", "s2ga", "s2gb")), str(flags))
			self.record("turned smoothly in the expected direction",
				self.ask(f"Did {motor_id} turn {revolutions} rev forward, smoothly?"))
		finally:
			m.disengage()
			# Back to the normal automatic selection.
			m.set_microsteps("auto")
			m.set_chopper("auto")

	def profile(self, motor_id: str, slew_sps: float) -> None:
		print(f"\n== Automatic microsteps and chopper: {motor_id} ==")
		if not self.ask(f"{motor_id} will track for 3 s, then run at {slew_sps:.0f} sps for 3 s. Go?"):
			return
		m = ESP32Motor(self.conn, motor_id)
		m.set_microsteps("auto")
		m.set_chopper("auto")
		m.engage()
		try:
			for label, sps, want_stealth in (("tracking", 6.68468, True), ("slew", slew_sps, False)):
				m.set_speed_sps(sps)
				m.start_continuous()
				time.sleep(3.0)
				status = m.status()
				live = m.driver_status().get("live", {})
				stealth = live.get("drv_status", {}).get("stealth")
				m.stop()
				self.conn.wait_for_event("motion_done", timeout=5.0, predicate=lambda e: e.get("motor") == motor_id)
				print(f"  {label}: {sps} sps -> firmware 1/{status.get('microsteps')}, chip 1/{live.get('microsteps')}, "
					f"pulse rate {status.get('pulse_rate', 0):.0f}/s, StealthChop active={stealth}")
				self.record(f"{label}: chip microsteps match the firmware's choice",
					live.get("microsteps") == status.get("microsteps"))
				self.record(f"{label}: {'StealthChop' if want_stealth else 'SpreadCycle'} in use", stealth is want_stealth)
		finally:
			m.disengage()

	def summary(self) -> int:
		failed = [r for r in self.results if not r[1]]
		print(f"\n{len(self.results) - len(failed)} passed, {len(failed)} failed")
		for name, _, detail in failed:
			print(f"  FAILED: {name}" + (f" ({detail})" if detail else ""))
		return 1 if failed else 0


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("check", choices=["all", "info", "i2c", "drivers", "leds", "buttons", "estop", "lcd", "imu", "motor", "profile"])
	parser.add_argument("--port", default="auto", help="serial port (default: auto)")
	parser.add_argument("--transport", default="auto", choices=["auto", "usb", "gpio_uart"])
	parser.add_argument("--yes", action="store_true", help="don't ask; assume every visual check passed")
	parser.add_argument("--motor", default="motor1", choices=["motor1", "motor2"])
	parser.add_argument("--run-ma", type=int, default=400, help="first-run current (report: IRUN 8-11, 0.4-0.5 A)")
	parser.add_argument("--microsteps", type=int, default=16)
	parser.add_argument("--revolutions", type=float, default=1.0)
	parser.add_argument("--sps", type=float, default=800.0)
	parser.add_argument("--slew-sps", type=float, default=3000.0, help="profile check: fast speed")
	args = parser.parse_args()

	conn = ESP32Connection(ESP32SerialConfig(port=args.port, transport=args.transport))
	checks = Checks(conn, args.yes)
	plan: Dict[str, List[Callable[[], None]]] = {
		"info": [checks.info],
		"i2c": [checks.i2c],
		"drivers": [checks.drivers],
		"leds": [checks.leds],
		"buttons": [checks.buttons],
		"estop": [checks.estop],
		"lcd": [checks.lcd],
		"imu": [checks.imu],
		"motor": [lambda: checks.motor(args.motor, args.run_ma, args.microsteps, args.revolutions, args.sps)],
		"profile": [lambda: checks.profile(args.motor, args.slew_sps)],
	}
	plan["all"] = plan["info"] + plan["i2c"] + plan["drivers"] + plan["leds"] + plan["buttons"] + plan["lcd"] + plan["imu"]
	try:
		for step in plan[args.check]:
			try:
				step()
			except (ESP32Error, TimeoutError) as exc:
				checks.record(getattr(step, "__name__", "check"), False, str(exc))
	finally:
		conn.close()
	return checks.summary()


if __name__ == "__main__":
	sys.exit(main())
