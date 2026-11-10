"""Writing SER video: uncompressed frames, the format planetary stacking
software reads (AutoStakkert, PIPP, Siril, RegiStax), and simple enough to
read in a browser.

A 178-byte header, the frames one after another (8-bit RGB here, or
mono), then each frame's UTC time (the trailer), which is how readers
know the frame rate. Format: SER v3 (Grischa Hahn, 2014).
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import BinaryIO, List, Optional

HEADER_BYTES = 178
COLOUR_IDS = {"MONO": 0, "RGB": 100, "BGR": 101}
_EPOCH_OFFSET_S = (datetime(1970, 1, 1) - datetime(1, 1, 1)).total_seconds()


def ser_time(when: datetime) -> int:
	"""SER's time: 100 ns ticks since 0001-01-01 (UTC here)."""
	if when.tzinfo is not None:
		when = when.astimezone(timezone.utc).replace(tzinfo=None)
	return int(round(((when - datetime(1970, 1, 1)).total_seconds() + _EPOCH_OFFSET_S) * 1e7))


class SerWriter:
	def __init__(self, path: str, width: int, height: int, colour: str = "RGB", depth: int = 8,
			observer: str = "", instrument: str = "", telescope: str = "") -> None:
		if colour not in COLOUR_IDS:
			raise ValueError(f"colour is one of {', '.join(COLOUR_IDS)}")
		if depth not in (8, 16):
			raise ValueError("depth is 8 or 16")
		self.path = path
		self.width, self.height = int(width), int(height)
		self.colour, self.depth = colour, depth
		self.frame_bytes = self.width * self.height * (3 if colour != "MONO" else 1) * (depth // 8)
		self._names = (observer, instrument, telescope)
		self._times: List[int] = []
		self._first: Optional[datetime] = None
		self._f: Optional[BinaryIO] = open(path, "wb")
		self._f.write(b"\0" * HEADER_BYTES)  # written properly on close
		self.frames = 0

	@property
	def bytes_written(self) -> int:
		return self._f.tell() if self._f is not None else 0

	def add(self, pixels: bytes, when: Optional[datetime] = None) -> None:
		"""One frame: rows top to bottom, RGB interleaved (or mono), 16-bit
		little-endian."""
		if self._f is None:
			raise ValueError("closed")
		if len(pixels) != self.frame_bytes:
			raise ValueError(f"a frame is {self.frame_bytes} bytes, not {len(pixels)}")
		when = when or datetime.now(timezone.utc)
		self._first = self._first or when
		self._f.write(pixels)
		self._times.append(ser_time(when))
		self.frames += 1

	def _header(self) -> bytes:
		def text(value: str) -> bytes:
			return value.encode("ascii", "replace")[:40].ljust(40, b"\0")

		first = self._first or datetime.now(timezone.utc)
		utc = ser_time(first)
		local = ser_time(first.astimezone().replace(tzinfo=None)) if first.tzinfo else utc
		observer, instrument, telescope = self._names
		# LittleEndian 1: 16-bit data is little-endian (what readers expect in practice).
		return (b"LUCAM-RECORDER" + struct.pack("<7i", 0, COLOUR_IDS[self.colour], 1, self.width, self.height,
			self.depth, self.frames) + text(observer) + text(instrument) + text(telescope)
			+ struct.pack("<qq", local, utc))

	def close(self) -> None:
		f = self._f
		if f is None:
			return
		self._f = None
		f.write(b"".join(struct.pack("<q", t) for t in self._times))
		f.seek(0)
		f.write(self._header())
		f.close()
