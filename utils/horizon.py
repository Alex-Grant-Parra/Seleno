"""The horizon: how high something has to be to go to (and keep) it.

config horizon:
	min_alt_deg     everywhere (0 by default)
	profile         [[azimuth, altitude], ...]: trees, a house; straight
	                lines between the points, round the compass
	stop_when_set   stop tracking when the target sinks below it

The limit at an azimuth is the higher of the two.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple


def _settings() -> dict:
	try:
		from utils.config_state import get_section

		return get_section("horizon")
	except Exception:
		return {"min_alt_deg": 0.0, "profile": [], "stop_when_set": True}


def profile_limit(profile: Sequence[Sequence[float]], az: float) -> Optional[float]:
	points: List[Tuple[float, float]] = []
	for p in profile or []:
		try:
			points.append((float(p[0]) % 360.0, float(p[1])))
		except (TypeError, ValueError, IndexError):
			continue
	if not points:
		return None
	points.sort()
	if len(points) == 1:
		return points[0][1]
	az %= 360.0
	for (a0, h0), (a1, h1) in zip(points, points[1:] + [(points[0][0] + 360.0, points[0][1])]):
		a = az if az >= a0 else az + 360.0
		if a0 <= a <= a1:
			return h0 + (h1 - h0) * ((a - a0) / (a1 - a0) if a1 > a0 else 0.0)
	return points[0][1]


def limit_at(az: float) -> float:
	s = _settings()
	base = float(s.get("min_alt_deg", 0.0) or 0.0)
	extra = profile_limit(s.get("profile") or [], az)
	return base if extra is None else max(base, extra)


def stop_when_set() -> bool:
	return bool(_settings().get("stop_when_set", True))


def check(ra: float, dec: float, lat: float, lst: float) -> Tuple[float, float, float]:
	"""(altitude, azimuth, the limit there) for J2000 ra/dec; precession
	doesn't matter at this scale."""
	from utils.sky_view import radec_to_altaz

	alt, az = radec_to_altaz(ra, dec, lst, lat)
	return alt, az, limit_at(az)
