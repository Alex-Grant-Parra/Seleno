#!/usr/bin/env python3
"""Flash the SELENO v0.8 ESP32 from the Pi over J7.

esptool can't toggle Pi GPIOs as DTR/RTS, so this puts the ESP32 in its ROM
bootloader through GPIO18 ("DTR") / GPIO23 ("RTS"), runs esptool with
--before no_reset --after no_reset, then resets it into the new firmware.
Same sequence as the hardware report ("Programming and firmware", Path A).

Stop anything using the port first (client.service / Client.py).
Needs: pip install esptool gpiozero lgpio   (lgpio for the Pi 5)

	# check the link
	./venv/bin/python -m esp32.seleno_flash flash_id
	# flash a PlatformIO build directory (bootloader, partitions, boot_app0, app)
	./venv/bin/python -m esp32.seleno_flash write --build-dir esp32-firmware/.pio/build/seleno_v08
	# anything else goes straight to esptool
	./venv/bin/python -m esp32.seleno_flash read_mac

A dev kit on USB doesn't need this: esptool / `pio run -t upload` reset it
through the USB chip's own DTR/RTS.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esp32 import serial_ports  # noqa: E402
from esp32.esp32_reset import GpioResetLines, enter_bootloader, reset_to_app  # noqa: E402

# Arduino-ESP32 flash layout (report: "Arduino flash layout").
IMAGES = (
	(0x1000, "bootloader.bin"),
	(0x8000, "partitions.bin"),
	(0xE000, "boot_app0.bin"),
	(0x10000, "firmware.bin"),
)
BOOT_APP0 = Path.home() / ".platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin"


def esptool_command() -> List[str]:
	for name in ("esptool.py", "esptool"):
		path = shutil.which(name)
		if path:
			return [path]
	pio_esptool = Path.home() / ".platformio/packages/tool-esptoolpy/esptool.py"
	if pio_esptool.exists():
		return [sys.executable, str(pio_esptool)]
	return [sys.executable, "-m", "esptool"]


def image_args(build_dir: Path) -> List[str]:
	args: List[str] = []
	for offset, name in IMAGES:
		path = build_dir / name
		if not path.exists() and name == "boot_app0.bin" and BOOT_APP0.exists():
			path = BOOT_APP0  # PlatformIO doesn't copy it into the build dir
		if not path.exists():
			raise SystemExit(f"Missing {path}")
		args += [hex(offset), str(path)]
	return args


def default_port() -> str:
	ports = serial_ports.gpio_uart_ports()
	return ports[0] if ports else "/dev/serial0"


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--port", default=None, help="GPIO UART device (default: /dev/ttyAMA0 on a Pi 5, else /dev/serial0)")
	parser.add_argument("--baud", type=int, default=921600)
	parser.add_argument("--build-dir", type=Path, help="with 'write': PlatformIO build directory")
	parser.add_argument("command", nargs=argparse.REMAINDER, help="'write', or esptool arguments (e.g. flash_id)")
	args = parser.parse_args()
	if not args.command:
		parser.error("give a command: write, flash_id, read_mac, ...")

	port = args.port or default_port()
	conflict = serial_ports.console_conflict(port)
	if conflict:
		raise SystemExit(f"Refusing to use {port}: {conflict}")

	if args.command[0] == "write":
		if args.build_dir is None:
			parser.error("write needs --build-dir")
		esptool_args = ["write_flash", "-z"] + image_args(args.build_dir)
	else:
		esptool_args = list(args.command)

	cmd = esptool_command() + ["--chip", "esp32", "--port", port, "--baud", str(args.baud),
		"--before", "no_reset", "--after", "no_reset"] + esptool_args

	lines = GpioResetLines()
	rc = 1
	try:
		enter_bootloader(lines)
		print("ESP32 in bootloader; running:", " ".join(cmd))
		rc = subprocess.call(cmd)
	finally:
		reset_to_app(lines)
		lines.release()
	return rc


if __name__ == "__main__":
	sys.exit(main())
