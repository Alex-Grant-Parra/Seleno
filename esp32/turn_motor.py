#!/usr/bin/env python3
"""Turn one motor by hand, for bench testing.

	./venv/bin/python -m esp32.turn_motor 1 90          # motor1 (RA) 90 degrees forward
	./venv/bin/python -m esp32.turn_motor 2 -45         # motor2 (DEC) 45 degrees back
	./venv/bin/python -m esp32.turn_motor 1 360 --sps 800

Stop the client first (sudo systemctl stop seleno-client): it holds the
serial port. Ctrl+C stops the motor. The motor is released when it's done.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esp32.interfaceESP32 import ESP32Connection, ESP32SerialConfig  # noqa: E402


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("motor", type=int, choices=[1, 2], help="1 = RA (J1), 2 = DEC (J2)")
	parser.add_argument("degrees", type=float, help="motor shaft degrees; negative turns the other way")
	parser.add_argument("--sps", type=float, default=400, help="speed in steps/s (1600 steps = one turn; default 400)")
	parser.add_argument("--hold", action="store_true", help="keep the motor powered afterwards")
	args = parser.parse_args()

	motor = f"motor{args.motor}"
	conn = ESP32Connection(ESP32SerialConfig(port="auto"))
	try:
		status = conn.send({"cmd": "create_motor", "motor": motor, "axis": args.motor - 1})
		steps_per_rev = int(status.get("steps_per_rev", 1600))
		steps = round(args.degrees / 360.0 * steps_per_rev)
		conn.send({"cmd": "set_speed", "motor": motor, "sps": args.sps})
		conn.send({"cmd": "engage", "motor": motor})
		start = conn.send({"cmd": "get_position", "motor": motor})["position"]
		print(f"{motor}: {args.degrees:+g} deg ({steps:+d} steps) at {args.sps:g} steps/s")
		conn.send({"cmd": "move_steps", "motor": motor, "steps": steps})
		limit = time.time() + abs(steps) / max(args.sps, 1) * 2 + 5
		while time.time() < limit:
			pos = conn.send({"cmd": "get_position", "motor": motor})
			if not pos.get("moving"):
				break
			time.sleep(0.1)
		print(f"{motor}: moved {pos['position'] - start:+d} steps")
	except KeyboardInterrupt:
		print("\nStopping")
	finally:
		try:
			conn.send({"cmd": "stop", "motor": motor})
			if not args.hold:
				conn.send({"cmd": "disengage", "motor": motor})
		except Exception as exc:
			print(f"Warning: couldn't stop/release {motor}: {exc}")
		conn.close()
	return 0


if __name__ == "__main__":
	sys.exit(main())
