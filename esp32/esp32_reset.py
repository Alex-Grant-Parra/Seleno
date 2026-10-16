"""Reset the ESP32, or put it in its ROM bootloader, from the Pi.

Both wiring options drive the same esptool-style transistor pair (Q2/Q3 on
the SELENO v0.8 board, the equivalent pair on a dev kit). Levels below are
the electrical levels on the two control lines, as in the hardware report's
truth table ("Auto-program pair"):

	DTR RTS
	 0   0   runs normally (Pi power-on default)
	 1   1   runs normally
	 1   0   EN low: held in reset
	 0   1   GPIO0 low: download mode if EN rises now

* v0.8 board: Pi GPIO18 is "DTR", GPIO23 is "RTS" (J7 pins 6 and 8).
* Dev kit on USB: the USB-serial chip's DTR/RTS outputs, which are active
  low, so pyserial's ``dtr=True`` means electrical 0.
"""
from __future__ import annotations

import time
from typing import Optional, Protocol

GPIO_DTR = 18
GPIO_RTS = 23

_RESET_HOLD_S = 0.10
_BOOT_STRAP_S = 0.10


class ResetLines(Protocol):
	def set_levels(self, dtr: int, rts: int) -> None: ...
	def release(self) -> None: ...


class SerialResetLines:
	"""DTR/RTS of an open pyserial port (dev kit on USB)."""

	def __init__(self, ser) -> None:
		self.ser = ser

	def set_levels(self, dtr: int, rts: int) -> None:
		# The chip's outputs are inverted: asserted (True) = electrical 0.
		self.ser.dtr = not bool(dtr)
		self.ser.rts = not bool(rts)

	def release(self) -> None:
		self.set_levels(1, 1)


class GpioResetLines:
	"""Pi GPIO18/GPIO23 wired through J7 (SELENO v0.8 board).

	Needs gpiozero with a working pin factory (lgpio on a Pi 5):
	``pip install gpiozero lgpio`` in the venv.
	"""

	def __init__(self, dtr_pin: int = GPIO_DTR, rts_pin: int = GPIO_RTS) -> None:
		try:
			from gpiozero import DigitalOutputDevice
		except ImportError as exc:  # pragma: no cover - depends on the Pi image
			raise RuntimeError(
				"GPIO reset needs gpiozero (and lgpio on a Pi 5): pip install gpiozero lgpio"
			) from exc
		# Both start low: the ESP32 keeps running while the pins are claimed.
		self._dtr = DigitalOutputDevice(dtr_pin, initial_value=False)
		self._rts = DigitalOutputDevice(rts_pin, initial_value=False)

	def set_levels(self, dtr: int, rts: int) -> None:
		self._dtr.value = 1 if dtr else 0
		self._rts.value = 1 if rts else 0

	def release(self) -> None:
		# Back to inputs; the Pi's pull-downs keep both low, so it keeps running.
		self.set_levels(0, 0)
		self._dtr.close()
		self._rts.close()


def reset_to_app(lines: ResetLines) -> None:
	"""Pulse EN with GPIO0 high: a normal reboot into the firmware."""
	lines.set_levels(1, 0)
	time.sleep(_RESET_HOLD_S)
	lines.set_levels(0, 0)


def enter_bootloader(lines: ResetLines) -> None:
	"""Release EN while GPIO0 is low, so the ROM waits for esptool."""
	lines.set_levels(1, 0)
	time.sleep(_RESET_HOLD_S)
	lines.set_levels(0, 1)
	time.sleep(_BOOT_STRAP_S)
	lines.set_levels(0, 0)


def lines_for(transport: str, ser=None) -> ResetLines:
	"""The reset lines for a transport ("usb" or "gpio_uart")."""
	if transport == "usb":
		if ser is None:
			raise ValueError("USB reset needs the open serial port")
		return SerialResetLines(ser)
	if transport == "gpio_uart":
		return GpioResetLines()
	raise ValueError(f"No reset lines for transport {transport!r}")


def reset_esp32(transport: str, ser=None, bootloader: bool = False) -> None:
	lines: Optional[ResetLines] = None
	try:
		lines = lines_for(transport, ser)
		if bootloader:
			enter_bootloader(lines)
		else:
			reset_to_app(lines)
	finally:
		if lines is not None and isinstance(lines, GpioResetLines):
			lines.release()
