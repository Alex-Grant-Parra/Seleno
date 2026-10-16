"""The front panel's sky view: what the telescope points at, drawn like the
server's star map (Seleno static/js/star_map), for the 1.8" LCD.

The maths is the star map's: sidereal time, RA/Dec -> Alt/Az, a
horizon-level camera (turn = azimuth, tilt = altitude) and the
stereographic projection, here centred on the telescope. The result is a
SKY1 scene (see esp32-firmware/PROTOCOL.md): stars, constellation figures,
grids, the ecliptic, labels and planets already in screen pixels, which the
ESP32 draws under its own overlays (reticle, lock rings, radar, text strip).
"""
from __future__ import annotations

import math
import struct
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEG = math.pi / 180.0
PEN_UP = -32768

# Layer styles and label styles (match ui_sky.cpp)
LAYER_CONSTELLATION, LAYER_EQ_GRID, LAYER_EQUATOR, LAYER_HZ_GRID, LAYER_HORIZON, LAYER_ECLIPTIC = range(6)
LABEL_STAR, LABEL_CONSTELLATION, LABEL_CARDINAL, LABEL_EQ, LABEL_HZ, LABEL_PLANET = range(6)

PLANET_KINDS = {
	"sun": 0, "moon": 1, "mercury": 2, "venus": 3, "mars": 4,
	"jupiter": 5, "saturn": 6, "uranus": 7, "neptune": 8, "pluto": 9,
}

# Field widths the panel steps through with LEFT/RIGHT (degrees).
FOV_STEPS = [10.0, 20.0, 40.0, 70.0, 100.0, 140.0]

Vec = Tuple[float, float, float]
Mat = Tuple[Vec, Vec, Vec]


@dataclass
class SkyOptions:
	fov_deg: float = 40.0
	horizon_grid: bool = True
	equatorial_grid: bool = False
	ecliptic: bool = True
	constellations: bool = True
	constellation_names: bool = True
	star_names: bool = True
	planets: bool = True
	below_horizon: bool = True
	max_star_labels: int = 6


# --- star map maths (astronomy.js) ---------------------------------------------

def julian_date(when: datetime) -> float:
	when = when.astimezone(timezone.utc)
	year, month = when.year, when.month
	day = when.day + (when.hour + (when.minute + (when.second + when.microsecond / 1e6) / 60.0) / 60.0) / 24.0
	if month <= 2:
		year -= 1
		month += 12
	a = year // 100
	b = 2 - a + a // 4
	return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5


def gmst_degrees(when: datetime) -> float:
	jd = julian_date(when)
	t = (jd - 2451545.0) / 36525.0
	gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t * t * t / 38710000.0
	return gmst % 360.0


def lst_degrees(when: datetime, longitude_deg: float) -> float:
	return (gmst_degrees(when) + longitude_deg) % 360.0


def radec_to_xyz(ra_deg: float, dec_deg: float) -> Vec:
	ra, dec = ra_deg * DEG, dec_deg * DEG
	return (math.cos(dec) * math.cos(ra), math.sin(dec), math.cos(dec) * math.sin(ra))


def altaz_to_xyz(alt_deg: float, az_deg: float) -> Vec:
	alt, az = alt_deg * DEG, az_deg * DEG
	return (math.cos(alt) * math.sin(az), math.sin(alt), math.cos(alt) * math.cos(az))


def xyz_to_altaz(v: Vec) -> Tuple[float, float]:
	alt = math.asin(max(-1.0, min(1.0, v[1]))) / DEG
	az = math.atan2(v[0], v[2]) / DEG
	return alt, az % 360.0


def eq_to_horizon_matrix(lat_deg: float, lst_deg: float) -> Mat:
	# buildEqToViewMatrix's M: equatorial [cosd cosa, sind, cosd sina] ->
	# horizon (x east, y up, z north)
	p, t = lat_deg * DEG, lst_deg * DEG
	sp, cp, st, ct = math.sin(p), math.cos(p), math.sin(t), math.cos(t)
	return ((-st, 0.0, ct), (cp * ct, sp, cp * st), (-sp * ct, cp, -sp * st))


def view_matrix(rot_x: float, rot_y: float) -> Mat:
	# viewMatrixFromAngles: turn by rot_y, then tilt by rot_x. With rot_y = az
	# and rot_x = alt, that alt/az lands at the centre, horizon level.
	sx, cx, sy, cy = math.sin(rot_x), math.cos(rot_x), math.sin(rot_y), math.cos(rot_y)
	return ((cy, 0.0, -sy), (-sx * sy, cx, -sx * cy), (cx * sy, sx, cx * cy))


def mat_mul(a: Mat, b: Mat) -> Mat:
	return tuple(
		tuple(a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j] for j in range(3)) for i in range(3)
	)  # type: ignore[return-value]


def mat_vec(m: Mat, v: Vec) -> Vec:
	return (
		m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
		m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
		m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
	)


def radec_to_altaz(ra_deg: float, dec_deg: float, lst_deg: float, lat_deg: float) -> Tuple[float, float]:
	return xyz_to_altaz(mat_vec(eq_to_horizon_matrix(lat_deg, lst_deg), radec_to_xyz(ra_deg, dec_deg)))


def ecliptic_to_equatorial(lam_deg: float, beta_deg: float) -> Tuple[float, float]:
	eps = 23.4392911 * DEG
	lam, bet = lam_deg * DEG, beta_deg * DEG
	dec = math.asin(math.sin(bet) * math.cos(eps) + math.cos(bet) * math.sin(eps) * math.sin(lam))
	ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(bet) * math.sin(eps), math.cos(lam))
	return (ra / DEG) % 360.0, dec / DEG


def sun_radec(when: datetime) -> Tuple[float, float]:
	# Low-precision solar position (Astronomical Almanac, ~0.01 deg), so the
	# Sun can be found without the server.
	n = julian_date(when) - 2451545.0
	mean_lon = 280.460 + 0.9856474 * n
	g = (357.528 + 0.9856003 * n) * DEG
	return ecliptic_to_equatorial(mean_lon + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g), 0.0)


def angular_distance(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
	a, b = radec_to_xyz(ra1, dec1), radec_to_xyz(ra2, dec2)
	dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
	return math.acos(dot) / DEG


# --- star sizes (core.js / render.js, scaled for a 128 px screen) ---------------

def star_mag_limit(fov_deg: float, data_limit: float = 5.0) -> float:
	# The star map's magnitude/zoom link (4.0 at zoom 1, ~140 deg wide, plus
	# 1.25 mag per zoom doubling on small screens), half a magnitude brighter
	# for the LCD and capped at what the catalogue holds.
	return min(data_limit, 3.5 + 1.25 * math.log2(140.0 / max(1.0, fov_deg)))


def star_radius_px(mag: float, mag_limit: float) -> float:
	full = max(0.8, 2.5 * 1.75 ** ((3.0 - mag) * 0.6)) * 0.42
	t = (mag_limit - mag) / 1.5
	t = 0.0 if t <= 0 else (1.0 if t >= 1 else t * t * (3 - 2 * t))
	return 0.35 + (max(full, 0.35) - 0.35) * t


def star_level(mag: float, mag_limit: float) -> int:
	t = max(0.0, min(1.0, 1.0 - (mag + 1.5) / (mag_limit + 2.5)))
	return int(round(255 * (0.3 + 0.7 * t)))


# --- projection ---------------------------------------------------------------

class Projector:
	"""Equatorial or horizon directions -> LCD pixels, centred on (alt, az)."""

	def __init__(self, width: int, height: int, lat_deg: float, lst_deg: float,
			center_alt: float, center_az: float, fov_deg: float) -> None:
		self.w, self.h = width, height
		self.horizon_to_view = view_matrix(center_alt * DEG, center_az * DEG)
		self.eq_to_view = mat_mul(self.horizon_to_view, eq_to_horizon_matrix(lat_deg, lst_deg))
		# Stereographic: a point theta from the centre lands k2 * tan(theta/2)
		# out, so the field's half width sets k2.
		self.k2 = (width / 2.0) / math.tan(fov_deg * DEG / 4.0)
		self.px_per_deg = self.k2 * DEG / 2.0
		self.margin = 24

	def eq(self, ra_deg: float, dec_deg: float) -> Vec:
		return mat_vec(self.eq_to_view, radec_to_xyz(ra_deg, dec_deg))

	def eq_xyz(self, v: Vec) -> Vec:
		return mat_vec(self.eq_to_view, v)

	def hz(self, alt_deg: float, az_deg: float) -> Vec:
		return mat_vec(self.horizon_to_view, altaz_to_xyz(alt_deg, az_deg))

	def screen(self, v: Vec) -> Optional[Tuple[float, float]]:
		if v[2] <= 0.0:
			return None
		s = self.k2 / (1.0 + v[2])
		return self.w / 2.0 + v[0] * s, self.h / 2.0 - v[1] * s

	def inside(self, p: Optional[Tuple[float, float]], margin: Optional[float] = None) -> bool:
		if p is None:
			return False
		m = self.margin if margin is None else margin
		return -m <= p[0] <= self.w + m and -m <= p[1] <= self.h + m

	def altitude_at(self, px: float, py: float) -> float:
		# unproject() then view -> horizon (the transpose); returns sin(alt)
		x = (px - self.w / 2.0) / self.k2
		y = -(py - self.h / 2.0) / self.k2
		p2 = x * x + y * y
		v = (2 * x / (1 + p2), 2 * y / (1 + p2), (1 - p2) / (1 + p2))
		r = self.horizon_to_view
		return r[0][1] * v[0] + r[1][1] * v[1] + r[2][1] * v[2]

	def trace(self, vectors: Iterable[Vec]) -> List[int]:
		"""A sampled curve as LCD points with pen-ups wherever it leaves the
		screen or passes behind the viewer."""
		out: List[int] = []
		prev: Optional[Tuple[float, float]] = None
		pen_down = False
		for v in vectors:
			p = self.screen(v) if v[2] > 0.02 else None
			if p is None:
				if pen_down:
					out += [PEN_UP, PEN_UP]
				pen_down = False
				prev = None
				continue
			if self.inside(p) or self.inside(prev):
				if not pen_down and prev is not None:
					out += [_clamp16(prev[0]), _clamp16(prev[1])]
				out += [_clamp16(p[0]), _clamp16(p[1])]
				pen_down = True
			elif pen_down:
				out += [PEN_UP, PEN_UP]
				pen_down = False
			prev = p
		return out


def _clamp16(value: float) -> int:
	return int(max(-30000, min(30000, round(value))))


def _great_circle(a: Vec, b: Vec, step_deg: float) -> List[Vec]:
	dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
	omega = math.acos(dot)
	if omega < 1e-6:
		return [a, b]
	steps = max(2, min(48, math.ceil(omega / DEG / step_deg)))
	so = math.sin(omega)
	pts = []
	for i in range(steps + 1):
		t = i / steps
		s1, s2 = math.sin((1 - t) * omega) / so, math.sin(t * omega) / so
		pts.append((s1 * a[0] + s2 * b[0], s1 * a[1] + s2 * b[1], s1 * a[2] + s2 * b[2]))
	return pts


# --- the scene ----------------------------------------------------------------

@dataclass
class SkyScene:
	width: int
	height: int
	stars: List[Tuple[int, int, int, int]]
	layers: List[Tuple[int, List[int]]]
	horizon_step: int
	horizon: List[int]
	labels: List[Tuple[int, int, int, str]]
	planets: List[Tuple[int, int, int, int, int, int, str]]
	target: Optional[Tuple[int, int]]
	scope_altaz: Tuple[float, float]
	target_altaz: Tuple[float, float]
	distance_deg: Optional[float]
	fov_deg: float

	def encode(self) -> bytes:
		"""SKY1 bytes (PROTOCOL.md, "Sky scene")."""
		out = bytearray(b"SKY1")
		flags = 1 if self.target is not None else 0
		tx, ty = self.target if self.target is not None else (0, 0)
		dist = 0xFFFF if self.distance_deg is None else min(0xFFFE, int(round(self.distance_deg * 100)))
		out += struct.pack(
			"<HHBBhhhHhHHH", self.width, self.height, flags, 0, tx, ty,
			_tenths(self.scope_altaz[0]), _tenths(self.scope_altaz[1] % 360.0),
			_tenths(self.target_altaz[0]), _tenths(self.target_altaz[1] % 360.0),
			dist, int(round(self.fov_deg * 10)),
		)
		out += struct.pack("<H", len(self.stars))
		for x, y, r4, level in self.stars:
			out += struct.pack("<hhBB", x, y, r4, level)
		out += struct.pack("<B", len(self.layers))
		for style, pts in self.layers:
			out += struct.pack("<BH", style, len(pts) // 2)
			out += struct.pack(f"<{len(pts)}h", *pts)
		out += struct.pack("<BH", self.horizon_step, len(self.horizon))
		out += struct.pack(f"<{len(self.horizon)}h", *self.horizon)
		out += struct.pack("<B", len(self.labels))
		for x, y, style, text in self.labels:
			raw = ascii_text(text, 40)
			out += struct.pack("<hhBB", x, y, style, len(raw)) + raw
		out += struct.pack("<B", len(self.planets))
		for x, y, kind, r, illum, limb, name in self.planets:
			raw = ascii_text(name, 20)
			out += struct.pack("<hhBBBBB", x, y, kind, r, illum, limb, len(raw)) + raw
		return bytes(out)


def _tenths(value: float) -> int:
	return int(round(value * 10))


def ascii_text(text: str, limit: int) -> bytes:
	"""The LCD fonts are ASCII: "Boötes" becomes "Bootes"."""
	plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore")
	return plain[:limit]


class _LabelGrid:
	"""Where labels have gone, so they don't pile up, plus a keep-out circle
	for the panel's reticle in the middle of the screen."""

	CHAR_W = 6  # the ESP32 draws labels in its 6x8 font
	RETICLE_R = 18

	def __init__(self, width: int, height: int, keepouts: Sequence[Tuple[int, int, int, int]] = ()) -> None:
		self.cx, self.cy = width / 2.0, height / 2.0
		self.keepouts = list(keepouts)  # (x, y, w, h) the panel draws over
		self.taken: List[Tuple[float, float, float, float]] = []  # placed labels, padded

	def clear_of_reticle(self, left: float, text: str, y: float) -> bool:
		right = left + self.CHAR_W * len(text)
		for kx, ky, kw, kh in self.keepouts:
			if left < kx + kw and right > kx and y - 4 < ky + kh and y + 4 > ky:
				return False
		nx = min(max(self.cx, left), right)
		ny = min(max(self.cy, y - 4), y + 4)
		return math.hypot(nx - self.cx, ny - self.cy) > self.RETICLE_R

	def place_beside(self, x: float, y: float, r: float, text: str, width: int, height: int) -> Optional[float]:
		"""Left edge for a label right of a point, or left of it when that
		side is blocked; None if neither fits."""
		if y < 5 or y > height - 5:
			return None
		gap = max(3.0, r + 2)
		for lx in (x + gap, x - gap - self.CHAR_W * len(text)):
			if lx >= 0 and lx + self.CHAR_W * len(text) <= width and self.clear_of_reticle(lx, text, y):
				if self.claim(lx, y, text):
					return lx
		return None

	def claim(self, left: float, y: float, text: str) -> bool:
		"""Take the box of a label drawn from left, centred on y, unless it
		would touch one already placed."""
		box = (left - 2, y - 5, left + self.CHAR_W * len(text) + 2, y + 5)
		for other in self.taken:
			if box[0] < other[2] and box[2] > other[0] and box[1] < other[3] and box[3] > other[1]:
				return False
		self.taken.append(box)
		return True


def build_scene(
	width: int,
	height: int,
	stars: Sequence[Tuple[float, float, float, str]],
	names: Dict[str, str],
	constellations: Sequence[dict],
	planets: Sequence[dict],
	scope_radec: Tuple[float, float],
	target_radec: Optional[Tuple[float, float]],
	lat_deg: float,
	lon_deg: float,
	when: Optional[datetime] = None,
	options: Optional[SkyOptions] = None,
	keepouts: Sequence[Tuple[int, int, int, int]] = (),
) -> SkyScene:
	"""Project everything for a width x height view centred on the scope.
	keepouts are screen rectangles (x, y, w, h) the panel draws its own
	things on (the radar inset), kept free of labels."""
	opt = options or SkyOptions()
	when = when or datetime.now(timezone.utc)
	lst = lst_degrees(when, lon_deg)
	scope_alt, scope_az = radec_to_altaz(scope_radec[0], scope_radec[1], lst, lat_deg)
	proj = Projector(width, height, lat_deg, lst, scope_alt, scope_az, opt.fov_deg)
	sample = max(0.5, min(3.0, opt.fov_deg / 30.0))  # curve sampling, degrees
	layers: List[Tuple[int, List[int]]] = []
	labels: List[Tuple[int, int, int, str]] = []
	grid = _LabelGrid(width, height, keepouts)

	def add_layer(style: int, pts: List[int]) -> None:
		if pts:
			layers.append((style, pts))

	# Grids, coarser when the field is wide (the star map always uses 10 deg).
	spacing = 10 if opt.fov_deg >= 30 else (5 if opt.fov_deg >= 12 else 2)
	if opt.horizon_grid:
		pts: List[int] = []
		for alt in range(-90 + spacing, 90, spacing):
			if alt == 0:
				continue
			pts += proj.trace(proj.hz(alt, az * sample) for az in range(int(360 / sample) + 1))
			pts += [PEN_UP, PEN_UP]
		for az in range(0, 360, spacing):
			pts += proj.trace(proj.hz(-90 + i * sample, az) for i in range(int(180 / sample) + 1))
			pts += [PEN_UP, PEN_UP]
		add_layer(LAYER_HZ_GRID, pts)
		for az, name in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
			p = proj.screen(proj.hz(3, az))
			if proj.inside(p, 0) and grid.clear_of_reticle(p[0] - 3, name, p[1]) and grid.claim(p[0] - 3, p[1], name):
				labels.append((_clamp16(p[0]), _clamp16(p[1]), LABEL_CARDINAL, name))
	if opt.equatorial_grid:
		pts = []
		for dec in range(-90 + spacing, 90, spacing):
			if dec == 0:
				continue
			pts += proj.trace(proj.eq(ra * sample, dec) for ra in range(int(360 / sample) + 1))
			pts += [PEN_UP, PEN_UP]
		for ra in range(0, 360, spacing):
			ra_deg = (lst + ra) % 360.0  # hour-angle lines, as the star map draws them
			pts += proj.trace(proj.eq(ra_deg, -90 + i * sample) for i in range(int(180 / sample) + 1))
			pts += [PEN_UP, PEN_UP]
		add_layer(LAYER_EQ_GRID, pts)
		add_layer(LAYER_EQUATOR, proj.trace(proj.eq(ra * sample, 0) for ra in range(int(360 / sample) + 1)))
	if opt.horizon_grid or opt.below_horizon:
		add_layer(LAYER_HORIZON, [] if opt.below_horizon else
			proj.trace(proj.hz(0, az * sample) for az in range(int(360 / sample) + 1)))
	if opt.ecliptic:
		def ecliptic_points() -> Iterable[Vec]:
			lam = 0.0
			while lam <= 360.0:
				ra, dec = ecliptic_to_equatorial(lam, 0.0)
				yield proj.eq(ra, dec)
				lam += sample
		add_layer(LAYER_ECLIPTIC, proj.trace(ecliptic_points()))

	if opt.constellations:
		pts = []
		for figure in constellations:
			for line in figure.get("lines", []):
				for (ra1, dec1), (ra2, dec2) in zip(line, line[1:]):
					a, b = proj.eq(ra1, dec1), proj.eq(ra2, dec2)
					if a[2] <= 0 and b[2] <= 0:
						continue
					pts += proj.trace(_great_circle(a, b, sample))
					pts += [PEN_UP, PEN_UP]
		add_layer(LAYER_CONSTELLATION, pts)

	mag_limit = star_mag_limit(opt.fov_deg)
	star_out: List[Tuple[int, int, int, int]] = []
	named: List[Tuple[float, float, float, str]] = []
	for ra, dec, mag, ident in stars:
		if mag > mag_limit:
			break  # brightest first
		p = proj.screen(proj.eq(ra, dec))
		if not proj.inside(p, 2):
			continue
		r4 = min(255, int(round(star_radius_px(mag, mag_limit) * 4)))
		star_out.append((_clamp16(p[0]), _clamp16(p[1]), r4, star_level(mag, mag_limit)))
		if opt.star_names and ident in names:
			named.append((p[0], p[1], star_radius_px(mag, mag_limit), names[ident]))

	planet_out = []
	if opt.planets:
		sun_v = None
		for obj in planets:
			if str(obj.get("name", "")).lower() == "sun":
				sun_v = proj.eq(float(obj["ra"]), float(obj["dec"]))
		for obj in planets:
			name = str(obj.get("name", ""))
			kind = PLANET_KINDS.get(name.lower(), 10)
			mag = obj.get("mag")
			if kind > 1 and mag is not None and float(mag) > mag_limit:
				continue  # the star map hides planets past the limit
			v = proj.eq(float(obj["ra"]), float(obj["dec"]))
			p = proj.screen(v)
			if not proj.inside(p, 6):
				continue
			if kind <= 1:
				r = max(4, int(round(0.26 * proj.px_per_deg)))
			else:
				r = 2 if mag is not None and float(mag) < 1.5 else 1
			illum, limb = 255, 0
			if kind == 1:
				illum = int(round(100 * float(obj.get("moon_illumination_fraction", 1.0))))
				if sun_v is not None:
					limb = _screen_angle_towards(proj, v, sun_v)
			planet_out.append((_clamp16(p[0]), _clamp16(p[1]), kind, min(255, r), illum, limb, name))
			lx = grid.place_beside(p[0], p[1], r, name, width, height)
			if lx is not None:
				labels.append((_clamp16(lx), _clamp16(p[1]), LABEL_PLANET, name))

	if opt.constellation_names and opt.constellations:
		for figure in constellations:
			anchor = figure.get("anchor")
			if not anchor:
				continue
			p = proj.screen(proj.eq(anchor[0], anchor[1]))
			name = str(figure.get("name") or figure.get("id") or "")
			if (proj.inside(p, -4) and name and grid.clear_of_reticle(p[0] - 3 * len(name), name, p[1])
					and grid.claim(p[0] - 3 * len(name), p[1], name)):
				labels.append((_clamp16(p[0]), _clamp16(p[1]), LABEL_CONSTELLATION, name))
	shown = 0
	for x, y, r, name in named:
		if shown >= opt.max_star_labels:
			break
		lx = grid.place_beside(x, y, r, name, width, height)
		if lx is None:
			continue
		labels.append((_clamp16(lx), _clamp16(y), LABEL_STAR, name))
		shown += 1

	horizon_step, horizon = 0, []
	if opt.below_horizon:
		horizon_step = 2
		for col in range(0, width, horizon_step):
			x = col + horizon_step / 2.0
			if proj.altitude_at(x, 0) < 0:
				horizon.append(0)
				continue
			if proj.altitude_at(x, height - 1) >= 0:
				horizon.append(height)
				continue
			lo, hi = 0, height - 1  # alt >= 0 at lo, < 0 at hi
			while hi - lo > 1:
				mid = (lo + hi) // 2
				if proj.altitude_at(x, mid) >= 0:
					lo = mid
				else:
					hi = mid
			horizon.append(hi)

	target_px = None
	target_altaz = (0.0, 0.0)
	distance = None
	if target_radec is not None:
		target_altaz = radec_to_altaz(target_radec[0], target_radec[1], lst, lat_deg)
		v = proj.eq(target_radec[0], target_radec[1])
		p = proj.screen(v) if v[2] > 0.05 else None
		if p is None:
			# Behind the viewer: far off in its direction, for the edge arrow.
			n = math.hypot(v[0], v[1]) or 1.0
			p = (width / 2.0 + v[0] / n * 5000, height / 2.0 - v[1] / n * 5000)
		target_px = (_clamp16(p[0]), _clamp16(p[1]))
		distance = angular_distance(scope_radec[0], scope_radec[1], target_radec[0], target_radec[1])

	return SkyScene(
		width=width,
		height=height,
		stars=star_out,
		layers=layers,
		horizon_step=horizon_step,
		horizon=horizon,
		labels=labels,
		planets=planet_out,
		target=target_px,
		scope_altaz=(scope_alt, scope_az),
		target_altaz=target_altaz,
		distance_deg=distance,
		fov_deg=opt.fov_deg,
	)


def _screen_angle_towards(proj: Projector, v: Vec, toward: Vec) -> int:
	"""Screen direction from v toward another direction, 0-255 for 0-360 deg
	(0 = right, counter-clockwise): the Moon's bright limb faces the Sun."""
	dot = v[0] * toward[0] + v[1] * toward[1] + v[2] * toward[2]
	t = (toward[0] - dot * v[0], toward[1] - dot * v[1], toward[2] - dot * v[2])
	n = math.sqrt(t[0] ** 2 + t[1] ** 2 + t[2] ** 2) or 1.0
	step = 0.01
	w = (v[0] + t[0] / n * step, v[1] + t[1] / n * step, v[2] + t[2] / n * step)
	a, b = proj.screen(v), proj.screen(w)
	if a is None or b is None:
		return 0
	angle = math.atan2(-(b[1] - a[1]), b[0] - a[0])
	return int(round((angle % (2 * math.pi)) / (2 * math.pi) * 256)) % 256
