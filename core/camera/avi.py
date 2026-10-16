"""Writing JPEG frames as an MJPEG AVI: no re-encoding, and VLC, ffmpeg,
PIPP and AutoStakkert open it (for stacking Moon and planet video).

AVI 1.0 with an idx1 index; the recorder starts a new file before one
reaches MAX_BYTES, since the 1.0 format's offsets stop at 4 GB and many
readers expect less.
"""
from __future__ import annotations

import struct
from typing import BinaryIO, List, Optional, Tuple

from core.camera.jpeg import jpeg_size

MAX_BYTES = 1_000_000_000


class MjpegAviWriter:
	def __init__(self, path: str, fps: float = 10.0) -> None:
		self.path = path
		self.fps = max(0.01, float(fps))
		self._f: Optional[BinaryIO] = open(path, "wb")
		self._index: List[Tuple[int, int]] = []  # (offset from 'movi', size)
		self._size: Optional[Tuple[int, int]] = None
		self._movi_at = 0
		self._max_chunk = 0
		self.frames = 0

	@property
	def bytes_written(self) -> int:
		return self._f.tell() if self._f is not None else 0

	def _header(self, width: int, height: int) -> None:
		f = self._f
		scale, rate = 1000, int(round(self.fps * 1000))
		avih = struct.pack("<10I4I", int(1e6 / self.fps), 0, 0, 0x10, 0, 0, 1, 0, width, height, 0, 0, 0, 0)
		strh = (b"vids" + b"MJPG" + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, scale, rate, 0, 0, 0,
			0xFFFFFFFF, 0, 0, 0, width, height))
		strf = struct.pack("<IiiHH4sIiiII", 40, width, height, 1, 24, b"MJPG", width * height * 3, 0, 0, 0, 0)
		strl = b"strl" + b"strh" + struct.pack("<I", len(strh)) + strh + b"strf" + struct.pack("<I", len(strf)) + strf
		hdrl = b"hdrl" + b"avih" + struct.pack("<I", len(avih)) + avih + b"LIST" + struct.pack("<I", len(strl)) + strl
		f.write(b"RIFF" + struct.pack("<I", 0) + b"AVI ")
		f.write(b"LIST" + struct.pack("<I", len(hdrl)) + hdrl)
		f.write(b"LIST" + struct.pack("<I", 0) + b"movi")
		self._movi_at = f.tell() - 4  # offsets in idx1 count from the 'movi' fourcc

	def add(self, jpeg: bytes) -> None:
		if self._f is None:
			raise ValueError("closed")
		if self._size is None:
			self._size = jpeg_size(jpeg) or (0, 0)
			self._header(*self._size)
		f = self._f
		offset = f.tell() - self._movi_at
		f.write(b"00dc" + struct.pack("<I", len(jpeg)) + jpeg)
		if len(jpeg) % 2:
			f.write(b"\x00")
		self._index.append((offset, len(jpeg)))
		self._max_chunk = max(self._max_chunk, len(jpeg))
		self.frames += 1

	def close(self) -> None:
		f = self._f
		if f is None:
			return
		self._f = None
		if self._size is None:
			f.close()
			return
		movi_end = f.tell()
		f.write(b"idx1" + struct.pack("<I", 16 * len(self._index)))
		for offset, size in self._index:
			f.write(b"00dc" + struct.pack("<III", 0x10, offset, size))
		end = f.tell()
		f.seek(4)
		f.write(struct.pack("<I", end - 8))
		# avih: total frames and buffer size
		f.seek(32 + 16)
		f.write(struct.pack("<I", self.frames))
		f.seek(32 + 28)
		f.write(struct.pack("<I", self._max_chunk + 8))
		# strh: length and buffer size (after 'LIST' size 'strl' 'strh' size)
		strh_at = 12 + 12 + 8 + 56 + 12 + 8
		f.seek(strh_at + 32)
		f.write(struct.pack("<I", self.frames))
		f.seek(strh_at + 36)
		f.write(struct.pack("<I", self._max_chunk + 8))
		f.seek(self._movi_at - 4)
		f.write(struct.pack("<I", movi_end - self._movi_at))
		f.close()
