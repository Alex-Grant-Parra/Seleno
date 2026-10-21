"""Flats with an automatic exposure (a sequence's exposure_s = "auto").

Short test exposures first, adjusted until the histogram peak sits at
about TARGET (half of full scale); then the flats at that exposure. Each
flat is measured too, and the next one's exposure corrected when it falls
outside BAND (the light changes during twilight flats).

The level is measured on the camera's JPEG (RAW + JPEG, or JPEG only):
there's no RAW decoder on the Pi. The JPEG's tone curve is undone with the
sRGB curve, which puts the peak near where it is in the RAW (within a few
per cent; cameras' curves differ a little from sRGB).

Exposures use the camera's own shutter speeds (bulb can't time fractions
of a second); past the longest one (usually 30 s) they're bulb.
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.log import get_logger

log = get_logger("flats")

TARGET = 0.5
BAND = (0.35, 0.65)
TEST_BAND = (0.42, 0.58)
MAX_TESTS = 8
START_S = 0.1
MAX_S = 120.0


class FlatsError(RuntimeError):
	pass


def linear(value: float) -> float:
	"""sRGB (0-1) to linear light."""
	return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def peak_level(path: str) -> float:
	"""The histogram peak of a JPEG, as a fraction of full scale (linear)."""
	from PIL import Image

	im = Image.open(path)
	im.draft("L", (max(1, im.width // 8), max(1, im.height // 8)))
	hist = im.convert("L").histogram()
	if hist[255] > 0.05 * sum(hist):
		return 1.0  # much of it is clipped: the peak is at the top, or past it
	smooth = [sum(hist[max(0, i - 2):i + 3]) for i in range(256)]
	return linear(max(range(256), key=lambda i: smooth[i]) / 255.0)


def jpeg_of(paths: List[str]) -> Optional[str]:
	return next((p for p in paths if p.lower().endswith((".jpg", ".jpeg"))), None)


def next_exposure(seconds: float, level: float) -> float:
	"""The exposure that should bring the peak to TARGET (light is linear
	in exposure; a clipped frame says only "much less")."""
	if level >= 0.98:
		return seconds / 8.0
	return seconds * TARGET / max(level, TARGET / 16.0)


class Shutter:
	"""The camera's shutter speeds, to set an exposure in seconds."""

	def __init__(self, service) -> None:
		from core.camera.backends import parse_shutter

		self.service = service
		info = service.get_setting("shutterspeed")
		self.original = info.get("value")
		speeds = [(parse_shutter(c), c) for c in info.get("choices") or []]
		self.speeds: List[Tuple[float, str]] = sorted((s, c) for s, c in speeds if s)
		if not self.speeds:
			raise FlatsError("The camera lists no shutter speeds")

	@property
	def longest(self) -> float:
		return self.speeds[-1][0]

	def nearest(self, seconds: float) -> Tuple[float, Optional[str]]:
		"""(seconds, the camera's choice); no choice past the longest (bulb)."""
		if seconds > self.longest * 1.2:
			return min(MAX_S, round(seconds)), None
		return min(self.speeds, key=lambda sc: abs(math.log(sc[0] / seconds)))  # in stops

	def step(self, seconds: float, longer: bool) -> float:
		"""The next speed up or down from seconds (itself at the end)."""
		values = [s for s, _c in self.speeds]
		if longer:
			return next((s for s in values if s > seconds * 1.01), seconds)
		return next((s for s in reversed(values) if s < seconds * 0.99), seconds)

	def set(self, seconds: float) -> Tuple[float, Optional[float]]:
		"""Set the camera for seconds; returns (seconds, bulb seconds or None)."""
		seconds, choice = self.nearest(seconds)
		if choice is None:
			return seconds, seconds
		self.service.set_setting("shutterspeed", choice)
		return seconds, None

	def restore(self) -> None:
		if self.original is not None:
			try:
				self.service.set_setting("shutterspeed", self.original)
			except Exception as exc:
				log.warning("Couldn't put the shutter speed back: %s", exc)


class AutoFlats:
	"""Finds the exposure, then takes flats one at a time with take(bulb_s),
	which returns the saved paths (utils/photos.take_photo)."""

	def __init__(self, take: Callable[[Optional[float]], List[str]], service=None,
			test: Optional[Callable[[str, Optional[float]], List[str]]] = None) -> None:
		if service is None:
			from core.camera.service import get_camera_service

			service = get_camera_service()
		self.shutter = Shutter(service)
		self._take = take
		self._test = test or _test_shot
		self.seconds: Optional[float] = None
		self.last_level: Optional[float] = None
		self.tests = 0

	def _shot(self, take: Callable[[Optional[float]], List[str]], seconds: float) -> Tuple[float, List[str], float]:
		seconds, bulb = self.shutter.set(seconds)
		paths = take(bulb)
		jpeg = jpeg_of(paths)
		if jpeg is None:
			raise FlatsError("Automatic flats need a JPEG: set the camera to RAW + JPEG")
		return seconds, paths, peak_level(jpeg)

	def find(self, start_s: Optional[float] = None) -> float:
		"""Test exposures until the peak is near TARGET; returns the exposure."""
		folder = tempfile.mkdtemp(prefix="flat-test-")
		try:
			seconds = start_s or START_S
			best: Optional[Tuple[float, float]] = None  # (distance from target, seconds)
			for _ in range(MAX_TESTS):
				seconds, _paths, level = self._shot(lambda bulb: self._test(folder, bulb), seconds)
				self.tests += 1
				log.info("Flat test: %.4g s gives %.0f%%", seconds, level * 100)
				if best is None or abs(level - TARGET) < best[0]:
					best = (abs(level - TARGET), seconds)
				if TEST_BAND[0] <= level <= TEST_BAND[1]:
					break
				wanted = self.shutter.nearest(next_exposure(seconds, level))[0]
				if abs(wanted - seconds) < 1e-9:
					wanted = self.shutter.step(seconds, longer=level < TARGET)
				if abs(wanted - seconds) < 1e-9:
					break  # at the end of the camera's range
				seconds = wanted
			assert best is not None
			if best[0] > TARGET - BAND[0]:
				raise FlatsError(f"No exposure gets the flats near half full (best {best[1]:.4g} s): "
					"change the light")
			self.seconds = best[1]
			return self.seconds
		finally:
			shutil.rmtree(folder, ignore_errors=True)

	def next_frame(self) -> List[str]:
		"""One flat; corrects the exposure for the next when this one is out
		of BAND."""
		if self.seconds is None:
			self.find()
		assert self.seconds is not None
		seconds, paths, level = self._shot(self._take, self.seconds)
		self.last_level = level
		if not BAND[0] <= level <= BAND[1]:
			wanted = self.shutter.nearest(next_exposure(seconds, level))[0]
			log.info("Flat at %.0f%%: next exposure %.4g s (was %.4g)", level * 100, wanted, seconds)
			self.seconds = wanted
		return paths

	def status(self) -> Dict[str, Any]:
		return {"exposure_s": self.seconds, "level": round(self.last_level, 3) if self.last_level else None,
			"tests": self.tests}

	def finish(self) -> None:
		self.shutter.restore()


def _test_shot(folder: str, bulb_s: Optional[float]) -> List[str]:
	from utils import photos

	return photos.capture_to(folder, f"test_{len(os.listdir(folder))}", bulb_s)
