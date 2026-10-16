"""Where the Sun, Moon and planets are, worked out on the Pi.

Paul Schlyter's method ("How to compute planetary positions"): orbital
elements with their slow drift, Kepler's equation, and the main
perturbations of the Moon, Jupiter, Saturn and Uranus. Good to about 1-2
arcminutes for the planets and a few for the Moon, plenty to find them
(a camera field is a degree or so; a plate solve or the centring does the
rest). The Moon is also given as seen from the site (topocentric): its
parallax is up to a degree.

Positions are for the equator and equinox of date. positions() returns
J2000 like every catalogue the client uses (utils/astrometry.py), in the
shape the sky view and the go-to lists want.

Pure maths, no network: offline mode can find and track the Moon and
planets, and the sky view draws them after the server's copy goes stale.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

DEG = math.pi / 180.0
BODIES = ("Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune")


def _rev(x: float) -> float:
	return x % 360.0


def _sind(x: float) -> float:
	return math.sin(x * DEG)


def _cosd(x: float) -> float:
	return math.cos(x * DEG)


def _day(when: datetime) -> float:
	"""Schlyter's day number: 0.0 at 2000 Jan 0.0 UT (1999-12-31 00:00)."""
	if when.tzinfo is None:
		when = when.replace(tzinfo=timezone.utc)
	epoch = datetime(1999, 12, 31, tzinfo=timezone.utc)
	return (when - epoch).total_seconds() / 86400.0


def _elements(body: str, d: float) -> Tuple[float, float, float, float, float, float]:
	"""(N, i, w, a, e, M): ascending node, inclination, perihelion argument,
	semi-major axis (AU; Earth radii for the Moon), eccentricity, mean
	anomaly."""
	if body == "Sun":
		return 0.0, 0.0, 282.9404 + 4.70935e-5 * d, 1.0, 0.016709 - 1.151e-9 * d, 356.0470 + 0.9856002585 * d
	if body == "Moon":
		return (125.1228 - 0.0529538083 * d, 5.1454, 318.0634 + 0.1643573223 * d, 60.2666, 0.054900,
			115.3654 + 13.0649929509 * d)
	if body == "Mercury":
		return (48.3313 + 3.24587e-5 * d, 7.0047 + 5.00e-8 * d, 29.1241 + 1.01444e-5 * d, 0.387098,
			0.205635 + 5.59e-10 * d, 168.6562 + 4.0923344368 * d)
	if body == "Venus":
		return (76.6799 + 2.46590e-5 * d, 3.3946 + 2.75e-8 * d, 54.8910 + 1.38374e-5 * d, 0.723330,
			0.006773 - 1.302e-9 * d, 48.0052 + 1.6021302244 * d)
	if body == "Mars":
		return (49.5574 + 2.11081e-5 * d, 1.8497 - 1.78e-8 * d, 286.5016 + 2.92961e-5 * d, 1.523688,
			0.093405 + 2.516e-9 * d, 18.6021 + 0.5240207766 * d)
	if body == "Jupiter":
		return (100.4542 + 2.76854e-5 * d, 1.3030 - 1.557e-7 * d, 273.8777 + 1.64505e-5 * d, 5.20256,
			0.048498 + 4.469e-9 * d, 19.8950 + 0.0830853001 * d)
	if body == "Saturn":
		return (113.6634 + 2.38980e-5 * d, 2.4886 - 1.081e-7 * d, 339.3939 + 2.97661e-5 * d, 9.55475,
			0.055546 - 9.499e-9 * d, 316.9670 + 0.0334442282 * d)
	if body == "Uranus":
		return (74.0005 + 1.3978e-5 * d, 0.7733 + 1.9e-8 * d, 96.6612 + 3.0565e-5 * d, 19.18171 - 1.55e-8 * d,
			0.047318 + 7.45e-9 * d, 142.5905 + 0.011725806 * d)
	if body == "Neptune":
		return (131.7806 + 3.0173e-5 * d, 1.7700 - 2.55e-7 * d, 272.8461 - 6.027e-6 * d, 30.05826 + 3.313e-8 * d,
			0.008606 + 2.15e-9 * d, 260.2471 + 0.005995147 * d)
	raise ValueError(body)


def _kepler(m: float, e: float) -> float:
	ecc = m + e / DEG * _sind(m) * (1.0 + e * _cosd(m))
	for _ in range(10):
		delta = (ecc - e / DEG * _sind(ecc) - m) / (1.0 - e * _cosd(ecc))
		ecc -= delta
		if abs(delta) < 1e-7:
			break
	return ecc


def _orbit(body: str, d: float) -> Tuple[float, float, float]:
	"""Heliocentric (geocentric for the Moon) ecliptic x, y, z."""
	n, i, w, a, e, m = _elements(body, d)
	ecc = _kepler(_rev(m), e)
	xv = a * (_cosd(ecc) - e)
	yv = a * math.sqrt(1.0 - e * e) * _sind(ecc)
	v = math.atan2(yv, xv) / DEG
	r = math.hypot(xv, yv)
	return (r * (_cosd(n) * _cosd(v + w) - _sind(n) * _sind(v + w) * _cosd(i)),
		r * (_sind(n) * _cosd(v + w) + _cosd(n) * _sind(v + w) * _cosd(i)),
		r * _sind(v + w) * _sind(i))


def _sun(d: float) -> Tuple[float, float, float]:
	"""The Sun's geocentric ecliptic longitude, distance (AU), and its
	mean longitude."""
	_n, _i, w, _a, e, m = _elements("Sun", d)
	ecc = _kepler(_rev(m), e)
	xv = _cosd(ecc) - e
	yv = math.sqrt(1.0 - e * e) * _sind(ecc)
	v = math.atan2(yv, xv) / DEG
	return _rev(v + w), math.hypot(xv, yv), _rev(m + w)


def _perturbed(body: str, d: float, lon: float, lat: float, r: float) -> Tuple[float, float, float]:
	mj = _rev(_elements("Jupiter", d)[5])
	ms = _rev(_elements("Saturn", d)[5])
	mu = _rev(_elements("Uranus", d)[5])
	if body == "Jupiter":
		lon += (-0.332 * _sind(2 * mj - 5 * ms - 67.6) - 0.056 * _sind(2 * mj - 2 * ms + 21)
			+ 0.042 * _sind(3 * mj - 5 * ms + 21) - 0.036 * _sind(mj - 2 * ms) + 0.022 * _cosd(mj - ms)
			+ 0.023 * _sind(2 * mj - 3 * ms + 52) - 0.016 * _sind(mj - 5 * ms - 69))
	elif body == "Saturn":
		lon += (0.812 * _sind(2 * mj - 5 * ms - 67.6) - 0.229 * _cosd(2 * mj - 4 * ms - 2)
			+ 0.119 * _sind(mj - 2 * ms - 3) + 0.046 * _sind(2 * mj - 6 * ms - 69) + 0.014 * _sind(mj - 3 * ms + 32))
		lat += -0.020 * _cosd(2 * mj - 4 * ms - 2) + 0.018 * _sind(2 * mj - 6 * ms - 49)
	elif body == "Uranus":
		lon += 0.040 * _sind(ms - 2 * mu + 6) + 0.035 * _sind(ms - 3 * mu + 33) - 0.015 * _sind(mj - mu + 20)
	return lon, lat, r


def _moon(d: float) -> Tuple[float, float, float]:
	"""The Moon's geocentric ecliptic longitude, latitude (degrees) and
	distance (Earth radii)."""
	x, y, z = _orbit("Moon", d)
	lon = _rev(math.atan2(y, x) / DEG)
	lat = math.atan2(z, math.hypot(x, y)) / DEG
	r = math.sqrt(x * x + y * y + z * z)
	nm, _i, wm, _a, _e, mm = _elements("Moon", d)
	ms = _rev(_elements("Sun", d)[5])
	ws = _elements("Sun", d)[2]
	mm = _rev(mm)
	ls = ms + ws
	lm = mm + wm + nm
	dd = lm - ls
	f = lm - nm
	lon += (-1.274 * _sind(mm - 2 * dd) + 0.658 * _sind(2 * dd) - 0.186 * _sind(ms)
		- 0.059 * _sind(2 * mm - 2 * dd) - 0.057 * _sind(mm - 2 * dd + ms) + 0.053 * _sind(mm + 2 * dd)
		+ 0.046 * _sind(2 * dd - ms) + 0.041 * _sind(mm - ms) - 0.035 * _sind(dd) - 0.031 * _sind(mm + ms)
		- 0.015 * _sind(2 * f - 2 * dd) + 0.011 * _sind(mm - 4 * dd))
	lat += (-0.173 * _sind(f - 2 * dd) - 0.055 * _sind(mm - f - 2 * dd) - 0.046 * _sind(mm + f - 2 * dd)
		+ 0.033 * _sind(f + 2 * dd) + 0.017 * _sind(2 * mm + f))
	r += -0.58 * _cosd(mm - 2 * dd) - 0.46 * _cosd(2 * dd)
	return _rev(lon), lat, r


def _to_equatorial(lon: float, lat: float, d: float) -> Tuple[float, float]:
	ecl = 23.4393 - 3.563e-7 * d
	x = _cosd(lon) * _cosd(lat)
	y = _sind(lon) * _cosd(lat)
	z = _sind(lat)
	ye = y * _cosd(ecl) - z * _sind(ecl)
	ze = y * _sind(ecl) + z * _cosd(ecl)
	return _rev(math.atan2(ye, x) / DEG), math.atan2(ze, math.hypot(x, ye)) / DEG


def _magnitude(body: str, r: float, big_r: float, phase: float) -> Optional[float]:
	if r <= 0 or big_r <= 0:
		return None
	base = 5.0 * math.log10(r * big_r)
	if body == "Mercury":
		return -0.36 + base + 0.027 * phase + 2.2e-13 * phase ** 6
	if body == "Venus":
		return -4.34 + base + 0.013 * phase + 4.2e-7 * phase ** 3
	if body == "Mars":
		return -1.51 + base + 0.016 * phase
	if body == "Jupiter":
		return -9.25 + base + 0.014 * phase
	if body == "Saturn":
		return -9.0 + base + 0.044 * phase
	if body == "Uranus":
		return -7.15 + base + 0.001 * phase
	if body == "Neptune":
		return -6.90 + base + 0.001 * phase
	return None


def position_of_date(body: str, when: datetime, site: Optional[Tuple[float, float]] = None) -> Dict[str, float]:
	"""RA/Dec of date (degrees) and more for one body. site (lat, lon):
	the Moon as seen from there."""
	d = _day(when)
	sun_lon, sun_r, _sun_l = _sun(d)
	xs, ys = sun_r * _cosd(sun_lon), sun_r * _sind(sun_lon)
	out: Dict[str, float] = {}
	if body == "Sun":
		ra, dec = _to_equatorial(sun_lon, 0.0, d)
		out.update(ra=ra, dec=dec, distance_au=sun_r, mag=-26.74)
		return out
	if body == "Moon":
		lon, lat, r = _moon(d)
		ra, dec = _to_equatorial(lon, lat, d)
		elong = math.acos(max(-1.0, min(1.0, _cosd(lon - sun_lon) * _cosd(lat)))) / DEG
		phase = 180.0 - elong
		out.update(ra=ra, dec=dec, distance_er=r, illumination=(1.0 + _cosd(phase)) / 2.0, elongation=elong,
			mag=-12.7 + 0.026 * abs(phase) + 4.0e-9 * phase ** 4)
		if site is not None:
			out["ra"], out["dec"] = _topocentric(ra, dec, r, site, when)
		return out
	xh, yh, zh = _orbit(body, d)
	lon = math.atan2(yh, xh) / DEG
	lat = math.atan2(zh, math.hypot(xh, yh)) / DEG
	r = math.sqrt(xh * xh + yh * yh + zh * zh)
	lon, lat, r = _perturbed(body, d, lon, lat, r)
	xh = r * _cosd(lon) * _cosd(lat)
	yh = r * _sind(lon) * _cosd(lat)
	zh = r * _sind(lat)
	xg, yg, zg = xh + xs, yh + ys, zh
	big_r = math.sqrt(xg * xg + yg * yg + zg * zg)
	glon = math.atan2(yg, xg) / DEG
	glat = math.atan2(zg, math.hypot(xg, yg)) / DEG
	ra, dec = _to_equatorial(glon, glat, d)
	cos_phase = (r * r + big_r * big_r - sun_r * sun_r) / (2.0 * r * big_r)
	phase = math.acos(max(-1.0, min(1.0, cos_phase))) / DEG
	out.update(ra=ra, dec=dec, distance_au=big_r, phase_angle=phase, mag=_magnitude(body, r, big_r, phase))
	return out


def _topocentric(ra: float, dec: float, r_er: float, site: Tuple[float, float], when: datetime) -> Tuple[float, float]:
	from utils.sky_view import lst_degrees

	lat, lon = site
	mpar = math.asin(min(1.0, 1.0 / r_er)) / DEG
	gclat = lat - 0.1924 * _sind(2 * lat)
	rho = 0.99833 + 0.00167 * _cosd(2 * lat)
	ha = lst_degrees(when, lon) - ra
	g = math.atan2(math.tan(gclat * DEG), _cosd(ha)) / DEG
	top_ra = ra - mpar * rho * _cosd(gclat) * _sind(ha) / max(1e-9, _cosd(dec))
	if abs(_sind(g)) < 1e-9:
		top_dec = dec - mpar * rho * _sind(-dec) * _cosd(ha)
	else:
		top_dec = dec - mpar * rho * _sind(gclat) * _sind(g - dec) / _sind(g)
	return _rev(top_ra), top_dec


def position(body: str, when: datetime, site: Optional[Tuple[float, float]] = None) -> Tuple[float, float]:
	"""J2000 RA/Dec of a body (topocentric for the Moon with a site)."""
	from utils.astrometry import date_to_j2000

	p = position_of_date(body, when, site)
	return date_to_j2000(p["ra"], p["dec"], when)


def rates(body: str, when: datetime, site: Optional[Tuple[float, float]] = None,
		step_s: float = 600.0) -> Tuple[float, float]:
	"""How fast a body moves against the stars: (dRA/dt, dDec/dt) of date,
	degrees per second."""
	a = position_of_date(body, when - timedelta(seconds=step_s / 2), site)
	b = position_of_date(body, when + timedelta(seconds=step_s / 2), site)
	dra = ((b["ra"] - a["ra"] + 180.0) % 360.0) - 180.0
	return dra / step_s, (b["dec"] - a["dec"]) / step_s


def find(name: str) -> Optional[str]:
	"""The body's proper name for a name typed or sent ('moon', 'MARS')."""
	key = str(name or "").strip().lower()
	for body in BODIES:
		if body.lower() == key:
			return body
	return None


def positions(when: datetime, site: Optional[Tuple[float, float]] = None) -> List[Dict[str, object]]:
	"""Every body as the sky view and the go-to lists want them: name, J2000
	ra and dec, mag; the Moon's illuminated fraction."""
	from utils.astrometry import date_to_j2000

	out: List[Dict[str, object]] = []
	for body in BODIES:
		p = position_of_date(body, when, site if body == "Moon" else None)
		ra, dec = date_to_j2000(p["ra"], p["dec"], when)
		entry: Dict[str, object] = {"name": body, "ra": ra, "dec": dec, "mag": p.get("mag"), "type": "planet",
			"source": "local"}
		if body == "Moon":
			entry["moon_illumination_fraction"] = round(p["illumination"], 3)
		out.append(entry)
	return out
