"""Find the serial port the ESP32 is on.

Two transports reach the same firmware:

* ``usb``       a dev kit's USB-serial chip (CH340 etc.): /dev/serial/by-id/*,
                /dev/ttyUSB*, /dev/ttyACM*.
* ``gpio_uart`` the Pi's own UART on GPIO14/15, wired to the SELENO v0.8
                board through J7. Raspberry Pi 5: /dev/ttyAMA0 (needs
                ``dtparam=uart0=on``). Zero 2 W and older: /dev/serial0
                (needs ``dtoverlay=disable-bt`` so it is the PL011).

A port that carries a Linux console or a login prompt is never probed:
writing JSON at a getty would be read as login attempts.
"""
from __future__ import annotations

import glob
import os
import re
from typing import Iterable, List, Optional

TRANSPORT_AUTO = "auto"
TRANSPORT_USB = "usb"
TRANSPORT_GPIO_UART = "gpio_uart"
TRANSPORTS = (TRANSPORT_AUTO, TRANSPORT_USB, TRANSPORT_GPIO_UART)

_USB_PATTERNS = ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*")


def pi_model() -> str:
	try:
		with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as f:
			return f.read().strip("\x00\n ")
	except OSError:
		return ""


def is_pi5(model: Optional[str] = None) -> bool:
	return "Raspberry Pi 5" in (model if model is not None else pi_model())


def _real(path: str) -> str:
	try:
		return os.path.realpath(path)
	except OSError:
		return path


def usb_ports() -> List[str]:
	ports: List[str] = []
	seen: set[str] = set()
	for pattern in _USB_PATTERNS:
		for port in sorted(glob.glob(pattern)):
			real = _real(port)
			if real in seen:
				continue  # by-id links and their ttyUSB targets are the same device
			seen.add(real)
			ports.append(port)
	return ports


def gpio_uart_ports(model: Optional[str] = None) -> List[str]:
	if is_pi5(model):
		candidates = ["/dev/ttyAMA0"]
	else:
		candidates = ["/dev/serial0", "/dev/ttyAMA0"]
	ports: List[str] = []
	seen: set[str] = set()
	for port in candidates:
		if not os.path.exists(port):
			continue
		real = _real(port)
		if real in seen:
			continue
		seen.add(real)
		ports.append(port)
	return ports


def transport_for_port(port: str) -> str:
	real = _real(port)
	if re.search(r"/tty(USB|ACM)\d+$", real) or port.startswith("/dev/serial/by-id/"):
		return TRANSPORT_USB
	return TRANSPORT_GPIO_UART


def _console_ttys() -> set[str]:
	ttys: set[str] = set()
	try:
		with open("/proc/cmdline", "r", encoding="utf-8") as f:
			cmdline = f.read()
	except OSError:
		return ttys
	for match in re.finditer(r"console=([^,\s]+)", cmdline):
		name = match.group(1)
		ttys.add(_real(f"/dev/{name}"))
	return ttys


def _getty_ttys() -> set[str]:
	ttys: set[str] = set()
	for unit in glob.glob("/run/systemd/units/invocation:serial-getty@*.service") + glob.glob(
		"/sys/fs/cgroup/system.slice/system-serial\\x2dgetty.slice/serial-getty@*.service"
	):
		match = re.search(r"serial-getty@([^.]+)\.service", unit)
		if match:
			ttys.add(_real(f"/dev/{match.group(1)}"))
	return ttys


def console_conflict(port: str) -> Optional[str]:
	"""Explain why a port must not be used, or None if it looks free."""
	real = _real(port)
	if real in _console_ttys():
		return f"{port} carries the Linux console (console= in /proc/cmdline)"
	if real in _getty_ttys():
		return f"{port} has a serial login (serial-getty) running"
	if real.endswith("/ttyS0") and transport_for_port(port) == TRANSPORT_GPIO_UART:
		# The mini UART's baud rate follows the core clock; 921600 isn't reliable.
		return f"{port} is the mini UART; enable the PL011 (dtoverlay=disable-bt)"
	return None


def candidate_ports(preferred: Optional[str] = None, transport: str = TRANSPORT_AUTO,
		model: Optional[str] = None) -> List[str]:
	"""Ports to try, best first. A configured port always comes first."""
	if transport not in TRANSPORTS:
		raise ValueError(f"transport must be one of {TRANSPORTS}")
	ports: List[str] = []
	seen: set[str] = set()

	def add(items: Iterable[str]) -> None:
		for port in items:
			if not port or port == TRANSPORT_AUTO:
				continue
			real = _real(port)
			if real in seen or not os.path.exists(port):
				continue
			seen.add(real)
			ports.append(port)

	if preferred:
		add([preferred])
	# USB first in auto mode: a plugged-in dev kit is a deliberate choice, and
	# the v0.8 board has no USB-serial chip, so the order never hides it.
	if transport in (TRANSPORT_AUTO, TRANSPORT_USB):
		add(usb_ports())
	if transport in (TRANSPORT_AUTO, TRANSPORT_GPIO_UART):
		add(gpio_uart_ports(model))
	return ports
