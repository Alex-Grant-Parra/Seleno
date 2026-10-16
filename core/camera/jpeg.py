"""Splitting a stream of JPEG images (gphoto2 --capture-movie) into frames,
and reading a JPEG's size.

Looking for the first FF D9 after FF D8 ends a frame early when the
frame carries a thumbnail (an EXIF block holds a whole JPEG). This walks
the marker segments instead: segments before the image data are skipped
by their length, and in the entropy-coded data FF is always followed by
00 or a restart marker, so the next real marker is unambiguous.

The scan position is kept between feeds, so each byte is looked at once.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Tuple

SOI = b"\xff\xd8"


class JpegFramer:
	def __init__(self, max_frame_bytes: int = 8 * 1024 * 1024) -> None:
		self.max_frame_bytes = max_frame_bytes
		self._buf = bytearray()
		self._pos = 0          # where parsing continues
		self._in_frame = False
		self._in_scan = False  # inside entropy-coded data
		self.dropped = 0       # frames given up on (corrupt or too big)

	def _resync(self) -> None:
		# Drop up to the next SOI after the current start.
		self.dropped += 1
		nxt = self._buf.find(SOI, 1)
		if nxt < 0:
			# Keep a trailing FF in case it's the first half of an SOI.
			keep = 1 if self._buf[-1:] == b"\xff" else 0
			del self._buf[:len(self._buf) - keep]
		else:
			del self._buf[:nxt]
		self._pos = 0
		self._in_frame = False
		self._in_scan = False

	def feed(self, data: bytes) -> List[bytes]:
		"""Add bytes; returns the frames they completed, oldest first."""
		self._buf += data
		frames: List[bytes] = []
		while True:
			frame = self._next()
			if frame is None:
				break
			frames.append(frame)
		return frames

	def _next(self) -> Optional[bytes]:
		buf = self._buf
		if not self._in_frame:
			start = buf.find(SOI)
			if start < 0:
				keep = 1 if buf[-1:] == b"\xff" else 0
				del buf[:len(buf) - keep]
				return None
			del buf[:start]
			self._pos = 2
			self._in_frame = True
			self._in_scan = False
		n = len(buf)
		pos = self._pos
		while True:
			if len(buf) > self.max_frame_bytes:
				self._resync()
				return None
			if self._in_scan:
				# Entropy-coded data: find FF followed by a real marker.
				while True:
					i = buf.find(b"\xff", pos)
					if i < 0:
						self._pos = n
						return None
					if i + 1 >= n:
						self._pos = i
						return None
					b = buf[i + 1]
					if b == 0x00 or 0xD0 <= b <= 0xD7 or b == 0xFF:
						pos = i + 1 if b == 0xFF else i + 2
						continue
					pos = i
					self._in_scan = False
					break
			# At a marker.
			if pos + 2 > n:
				self._pos = pos
				return None
			if buf[pos] != 0xFF:
				self._resync()
				return None
			marker = buf[pos + 1]
			if marker == 0xFF:  # fill byte
				pos += 1
				continue
			if marker == 0xD9:  # EOI
				end = pos + 2
				frame = bytes(buf[:end])
				del buf[:end]
				self._pos = 0
				self._in_frame = False
				return frame
			if marker == 0xD8:  # a new SOI: the last frame was cut off
				del buf[:pos]
				self.dropped += 1
				pos = 2
				n = len(buf)
				continue
			if 0xD0 <= marker <= 0xD7 or marker == 0x01:
				pos += 2
				continue
			if pos + 4 > n:
				self._pos = pos
				return None
			(length,) = struct.unpack_from(">H", buf, pos + 2)
			if length < 2:
				self._resync()
				return None
			if pos + 2 + length > n:
				self._pos = pos
				return None
			pos += 2 + length
			if marker == 0xDA:  # SOS: the image data follows
				self._in_scan = True


def jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
	"""(width, height) from a JPEG's SOF segment, or None."""
	pos = 2
	n = len(data)
	if data[:2] != SOI:
		return None
	while pos + 4 <= n:
		if data[pos] != 0xFF:
			return None
		marker = data[pos + 1]
		if marker == 0xFF:
			pos += 1
			continue
		if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
			pos += 2
			continue
		(length,) = struct.unpack_from(">H", data, pos + 2)
		if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
			if pos + 9 > n:
				return None
			height, width = struct.unpack_from(">HH", data, pos + 5)
			return width, height
		if marker == 0xDA:
			return None
		pos += 2 + length
	return None
