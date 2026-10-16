"""Small corrections between catalogue coordinates and where to point.

* Precession: catalogues (the server's stars, plate solves, typed
  coordinates) use the J2000 equator; the sky turns about today's pole,
  which has moved since. By 2026 that's about 0.37 degrees of RA: enough
  to miss a small camera field. The mount works in coordinates of date
  ("JNow"); everything people see stays J2000.
* Refraction: the air lifts what's near the horizon (half a degree at the
  horizon, 5 arcmin at 10 degrees up, 1 arcmin at 45). The mount aims at
  where things appear.

IAU 1976 precession (good to well under an arcsecond for centuries round
2000) and Bennett's refraction formula (0.07 arcmin). Pure maths.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Tuple

DEG = math.pi / 180.0
ARCSEC = DEG / 3600.0
J2000_JD = 2451545.0

Mat = Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]


def julian_date(when: datetime) -> float:
	from utils.sky_view import julian_date as jd

	return jd(when)


def _precession_matrix(jd: float) -> Mat:
	"""J2000 equatorial -> mean equator and equinox of date."""
	t = (jd - J2000_JD) / 36525.0
	zeta = (2306.2181 * t + 0.30188 * t * t + 0.017998 * t ** 3) * ARCSEC
	z = (2306.2181 * t + 1.09468 * t * t + 0.018203 * t ** 3) * ARCSEC
	theta = (2004.3109 * t - 0.42665 * t * t - 0.041833 * t ** 3) * ARCSEC
	cz, sz = math.cos(zeta), math.sin(zeta)
	cZ, sZ = math.cos(z), math.sin(z)
	ct, st = math.cos(theta), math.sin(theta)
	return (
		(cZ * ct * cz - sZ * sz, -cZ * ct * sz - sZ * cz, -cZ * st),
		(sZ * ct * cz + cZ * sz, -sZ * ct * sz + cZ * cz, -sZ * st),
		(st * cz, -st * sz, ct),
	)


def _xyz(ra: float, dec: float) -> Tuple[float, float, float]:
	r, d = ra * DEG, dec * DEG
	return (math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d))


def _radec(v) -> Tuple[float, float]:
	ra = math.atan2(v[1], v[0]) / DEG % 360.0
	dec = math.asin(max(-1.0, min(1.0, v[2]))) / DEG
	return ra, dec


def j2000_to_date(ra: float, dec: float, when: datetime) -> Tuple[float, float]:
	m = _precession_matrix(julian_date(when))
	v = _xyz(ra, dec)
	return _radec(tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3)))


def date_to_j2000(ra: float, dec: float, when: datetime) -> Tuple[float, float]:
	m = _precession_matrix(julian_date(when))
	v = _xyz(ra, dec)
	return _radec(tuple(m[0][i] * v[0] + m[1][i] * v[1] + m[2][i] * v[2] for i in range(3)))


def refraction_deg(true_alt: float, pressure_hpa: float = 1010.0, temp_c: float = 10.0) -> float:
	"""How much the air lifts something at true altitude true_alt (degrees),
	so apparent = true + this. Nothing below -1 degree."""
	if true_alt < -1.0:
		return 0.0
	# Saemundsson's inverse of Bennett: from the true altitude, arcminutes.
	r = 1.02 / math.tan((true_alt + 10.3 / (true_alt + 5.11)) * DEG)
	r *= (pressure_hpa / 1010.0) * (283.0 / (273.0 + temp_c))
	return max(0.0, r) / 60.0


def hadec_to_altaz(ha: float, dec: float, lat: float) -> Tuple[float, float]:
	h, d, p = ha * DEG, dec * DEG, lat * DEG
	sin_alt = math.sin(d) * math.sin(p) + math.cos(d) * math.cos(p) * math.cos(h)
	alt = math.asin(max(-1.0, min(1.0, sin_alt)))
	az = math.atan2(-math.cos(d) * math.sin(h), math.sin(d) * math.cos(p) - math.cos(d) * math.sin(p) * math.cos(h))
	return alt / DEG, (az / DEG) % 360.0


def altaz_to_hadec(alt: float, az: float, lat: float) -> Tuple[float, float]:
	a, z, p = alt * DEG, az * DEG, lat * DEG
	sin_dec = math.sin(a) * math.sin(p) + math.cos(a) * math.cos(p) * math.cos(z)
	dec = math.asin(max(-1.0, min(1.0, sin_dec)))
	ha = math.atan2(-math.cos(a) * math.sin(z), math.sin(a) * math.cos(p) - math.cos(a) * math.sin(p) * math.cos(z))
	return ((ha / DEG + 180.0) % 360.0) - 180.0, dec / DEG


def apparent_hadec(ha: float, dec: float, lat: float) -> Tuple[float, float]:
	"""(ha, dec) of date, lifted by refraction to where it appears."""
	alt, az = hadec_to_altaz(ha, dec, lat)
	lift = refraction_deg(alt)
	if lift <= 0.0:
		return ha, dec
	return altaz_to_hadec(alt + lift, az, lat)


def true_hadec(ha: float, dec: float, lat: float) -> Tuple[float, float]:
	"""The inverse of apparent_hadec (where the telescope really points,
	minus the refraction), to a fraction of an arcsecond."""
	alt, az = hadec_to_altaz(ha, dec, lat)
	true_alt = alt
	for _ in range(4):
		true_alt = alt - refraction_deg(true_alt)
	if true_alt == alt:
		return ha, dec
	return altaz_to_hadec(true_alt, az, lat)


def refraction_enabled() -> bool:
	try:
		from utils.config_state import get_section

		return bool(get_section("tracking").get("refraction", True))
	except Exception:
		return True


def j2000_to_apparent_hadec(ra: float, dec: float, lst: float, lat: float, when: datetime,
		refraction: bool = True) -> Tuple[float, float]:
	"""Where to point for J2000 (ra, dec): hour angle and declination of
	date, lifted by refraction (when refraction and it's switched on)."""
	ra_d, dec_d = j2000_to_date(ra, dec, when)
	ha = ((lst - ra_d + 180.0) % 360.0) - 180.0
	if refraction and refraction_enabled():
		return apparent_hadec(ha, dec_d, lat)
	return ha, dec_d


def apparent_hadec_to_j2000(ha: float, dec: float, lst: float, lat: float, when: datetime,
		refraction: bool = True) -> Tuple[float, float]:
	"""The inverse: the J2000 place the telescope's (ha, dec) shows."""
	if refraction and refraction_enabled():
		ha, dec = true_hadec(ha, dec, lat)
	return date_to_j2000((lst - ha) % 360.0, dec, when)
