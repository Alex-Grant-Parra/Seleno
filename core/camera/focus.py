"""How sharp a live-view frame is, for focusing.

* hfr: the brightest star's half-flux radius in pixels (the radius holding
  half its light). Smaller is sharper; it's what focusing aims to bring
  down. None when there's no clear star (the Moon, daylight, clouds).
* sharpness: the variance of the image's Laplacian, for anything that
  isn't a star (the Moon, a planet, a distant tree). Bigger is sharper; it
  only means something compared with itself as the focus changes.
* star: (x, y) of the star used, peak: its brightest pixel (255 is
  saturated: shorten the exposure for a true HFR).
"""
from __future__ import annotations

import io
from typing import Any, Dict, Optional, Tuple

import numpy as np

RADIUS = 16  # pixels round the star used for the HFR


def _gray(jpeg: bytes) -> np.ndarray:
	from PIL import Image

	return np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"), dtype=np.float32)


def _box_blur3(a: np.ndarray) -> np.ndarray:
	# 3x3 mean, so a single hot pixel isn't taken for a star.
	p = np.pad(a, 1, mode="edge")
	return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] + p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:]
		+ p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def hfr_at(a: np.ndarray, x: int, y: int, radius: int = RADIUS) -> Optional[Tuple[float, float, float]]:
	"""HFR of the star near (x, y), and its centroid; None if it's not a star."""
	h, w = a.shape
	x0, x1 = max(0, x - radius), min(w, x + radius + 1)
	y0, y1 = max(0, y - radius), min(h, y + radius + 1)
	patch = a[y0:y1, x0:x1]
	if patch.size < 25:
		return None
	border = np.concatenate([patch[0, :], patch[-1, :], patch[:, 0], patch[:, -1]])
	background = float(np.median(border))
	noise = float(np.std(border)) + 1.0
	signal = np.clip(patch - background, 0, None)
	total = float(signal.sum())
	if total <= 0 or float(signal.max()) < 5 * noise:
		return None
	yy, xx = np.mgrid[y0:y1, x0:x1]
	cx = float((signal * xx).sum() / total)
	cy = float((signal * yy).sum() / total)
	r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
	return float((signal * r).sum() / total), cx, cy


def analyse(jpeg: bytes) -> Dict[str, Any]:
	a = _gray(jpeg)
	h, w = a.shape
	out: Dict[str, Any] = {"size": (w, h), "hfr": None, "star": None, "peak": None}
	# Sharpness on a half-size copy (quick, and less noise).
	small = a[::2, ::2]
	lap = (-4 * small[1:-1, 1:-1] + small[:-2, 1:-1] + small[2:, 1:-1] + small[1:-1, :-2] + small[1:-1, 2:])
	out["sharpness"] = round(float(lap.var()), 1)
	blurred = _box_blur3(a)
	margin = RADIUS + 1
	inner = blurred[margin:h - margin, margin:w - margin]
	if inner.size == 0:
		return out
	idx = int(np.argmax(inner))
	y, x = divmod(idx, inner.shape[1])
	x += margin
	y += margin
	out["peak"] = int(a[y, x])
	star = hfr_at(a, x, y)
	if star is not None:
		hfr, cx, cy = star
		out["hfr"] = round(hfr, 2)
		out["star"] = (round(cx, 1), round(cy, 1))
	return out
