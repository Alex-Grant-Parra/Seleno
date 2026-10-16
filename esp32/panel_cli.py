"""Drive and inspect the front-panel UI over the serial link.

Works on the bare dev kit too (no LCD or buttons needed): keys are injected
and the screen is read back as a PNG.

	python -m esp32.panel_cli state
	python -m esp32.panel_cli shot screen.png [--scale 3]
	python -m esp32.panel_cli key down down select [--press long]
	python -m esp32.panel_cli send '{"action":"hello","device":"bench"}'

Don't run it while Client.py holds the port.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from esp32.interfaceESP32 import ESP32Connection, ESP32SerialConfig


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("command", choices=["state", "shot", "key", "send", "events"])
	parser.add_argument("args", nargs="*")
	parser.add_argument("--port", default="auto")
	parser.add_argument("--transport", default="auto", choices=["auto", "usb", "gpio_uart"])
	parser.add_argument("--press", default="click", choices=["click", "down", "up", "long"])
	parser.add_argument("--scale", type=int, default=2, help="shot: enlarge the PNG this many times")
	parser.add_argument("--seconds", type=float, default=10.0, help="events: how long to listen")
	args = parser.parse_args()

	conn = ESP32Connection(ESP32SerialConfig(port=args.port, transport=args.transport))
	try:
		if args.command == "state":
			print(json.dumps(conn.ui_state(), indent=2))
		elif args.command == "shot":
			path = args.args[0] if args.args else "panel.png"
			image = conn.ui_screenshot()
			if args.scale > 1:
				image = image.resize((image.width * args.scale, image.height * args.scale), resample=0)
			image.save(path)
			print(f"saved {path} ({image.width}x{image.height})")
		elif args.command == "key":
			for key in args.args:
				conn.ui_key(key, args.press)
				# Let the UI handle each key before the next (long presses
				# release themselves after about 0.8 s).
				time.sleep(0.9 if args.press == "long" else 0.12)
		elif args.command == "send":
			payload = json.loads(args.args[0]) if args.args else {}
			action = payload.pop("action", "state")
			print(json.dumps(conn.ui(action, **payload), indent=2))
		elif args.command == "events":
			conn.add_event_listener(lambda ev: print(json.dumps(ev)), None)
			time.sleep(args.seconds)
	finally:
		conn.close()
	return 0


if __name__ == "__main__":
	sys.exit(main())
