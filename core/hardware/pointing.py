"""Pointing model: where the telescope really points for given axis angles.

Without encoders, the axis angles are dead reckoning from the motor steps,
and the ideal geometry (core/hardware/mount.py) ignores how the mount was
set up. Each alignment star (Align on star, or a plate solve) is a point:
the axes as they were, and where the telescope was really pointing. The
model is fitted to them; it has six terms, used as the points allow:

	ih, id   index offsets of the RA and Dec axes (1+ points)
	alt, az  the polar axis's misalignment: how far the mount's pole is
	         above (alt > 0) and east (az > 0) of the true pole (3+ points
	         spread in hour angle)
	ch       cone error: the tube not square to the Dec axis (4+ points;
	         both sides of the mount help)
	np       the two axes not square to each other (6+ points)

Angles are degrees. Everything here is in the mount frame (the northern
sky as the mount sees it; core/hardware/mount.py mirrors the south).

The pole misalignment is applied as an exact rotation of the sky, so its
fitted values read straight off as "move the pole down 12 arcmin and west
5 arcmin" (polar_report()). Pure maths: no I/O.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

DEG = math.pi / 180.0
Point = Tuple[float, float]
TERMS = ("ih", "id", "alt", "az", "ch", "np")
# How many points before each term is fitted (fewer: held at 0).
_TERMS_FOR_POINTS = {1: ("ih", "id"), 2: ("ih", "id"), 3: ("ih", "id", "alt", "az"),
	4: ("ih", "id", "alt", "az", "ch"), 5: ("ih", "id", "alt", "az", "ch")}
_SEC_LIMIT = 85.0  # cone and non-perpendicularity blow up at the pole


def _wrap180(deg: float) -> float:
	return (deg + 180.0) % 360.0 - 180.0


def _ideal(a: float, d: float) -> Tuple[float, float, bool]:
	"""Axes to mount-frame (ha, dec) and whether flipped (as mount.py)."""
	dw = _wrap180(d)
	if -90.0 <= dw <= 90.0:
		return _wrap180(a), dw, False
	return _wrap180(a + 180.0), (180.0 - dw) if dw > 0 else (-180.0 - dw), True


def _xyz(ha: float, dec: float) -> Tuple[float, float, float]:
	h, d = ha * DEG, dec * DEG
	return (math.cos(d) * math.cos(h), math.cos(d) * math.sin(h), math.sin(d))


def _sky(v: Sequence[float]) -> Tuple[float, float]:
	n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
	return math.atan2(v[1], v[0]) / DEG, math.asin(max(-1.0, min(1.0, v[2] / n))) / DEG


def _rotate_pole(v: Tuple[float, float, float], alt: float, az: float) -> Tuple[float, float, float]:
	"""The mount's frame to the true sky, when its pole is alt degrees
	towards the zenith (+x, the meridian) and az degrees east (-y, since y
	is towards hour angle +90, the west) of the true pole: rotate +z onto
	the mount's pole (Rodrigues)."""
	px, py = math.sin(alt * DEG), -math.sin(az * DEG)
	pz = math.sqrt(max(0.0, 1.0 - px * px - py * py))
	# axis = z x p, angle = acos(pz)
	kx, ky = -py, px
	s = math.sqrt(kx * kx + ky * ky)
	if s < 1e-15:
		return v
	kx, ky = kx / s, ky / s
	angle = math.atan2(s, pz)
	c, sn = math.cos(angle), math.sin(angle)
	# k x v with k = (kx, ky, 0)
	cx, cy, cz = ky * v[2], -kx * v[2], kx * v[1] - ky * v[0]
	dot = kx * v[0] + ky * v[1]
	return (v[0] * c + cx * sn + kx * dot * (1 - c),
		v[1] * c + cy * sn + ky * dot * (1 - c),
		v[2] * c + cz * sn)


@dataclass
class PointingModel:
	ih: float = 0.0
	id: float = 0.0
	alt: float = 0.0
	az: float = 0.0
	ch: float = 0.0
	np: float = 0.0
	points: int = 0
	rms_arcmin: float = 0.0

	def sky(self, axes: Point) -> Tuple[float, float]:
		"""Where axes really point: (ha, dec) in the mount frame."""
		a, d = axes
		# The index offsets: what the axes read is off by ih and id (on the
		# flipped side the Dec offset reverses by itself: dec = 180 - axis).
		ha, dec, flipped = _ideal(a + self.ih, d + self.id)
		side = -1.0 if flipped else 1.0
		dc = max(-_SEC_LIMIT, min(_SEC_LIMIT, dec)) * DEG
		ha += side * (self.ch / math.cos(dc) + self.np * math.tan(dc))
		if self.alt or self.az:
			ha, dec = _sky(_rotate_pole(_xyz(ha, dec), self.alt, self.az))
		return _wrap180(ha), dec

	def is_identity(self) -> bool:
		return not any(getattr(self, t) for t in TERMS)

	def to_dict(self) -> Dict[str, float]:
		return asdict(self)

	@classmethod
	def from_dict(cls, data: Optional[Dict[str, float]]) -> "PointingModel":
		data = data or {}
		return cls(**{k: float(data.get(k, 0.0)) for k in TERMS},
			points=int(data.get("points", 0)), rms_arcmin=float(data.get("rms_arcmin", 0.0)))


@dataclass
class AlignmentPoint:
	axes: Point             # dead-reckoned axes when it was taken
	sky: Tuple[float, float]  # where it really pointed: (ha, dec), mount frame
	source: str = "star"    # "star" (by eye) or "solve"
	name: str = ""

	def to_dict(self) -> Dict[str, object]:
		return {"axes": list(self.axes), "sky": list(self.sky), "source": self.source, "name": self.name}

	@classmethod
	def from_dict(cls, data: Dict[str, object]) -> "AlignmentPoint":
		axes = data.get("axes") or (0.0, 0.0)
		sky = data.get("sky") or (0.0, 0.0)
		return cls((float(axes[0]), float(axes[1])), (float(sky[0]), float(sky[1])),
			str(data.get("source", "star")), str(data.get("name", "")))


def _sep(a: Tuple[float, float], b: Tuple[float, float]) -> float:
	x, y = _xyz(*a), _xyz(*b)
	dot = max(-1.0, min(1.0, x[0] * y[0] + x[1] * y[1] + x[2] * y[2]))
	return math.acos(dot) / DEG


def _residuals(model: PointingModel, points: Sequence[AlignmentPoint]) -> List[float]:
	out: List[float] = []
	for p in points:
		ha, dec = model.sky(p.axes)
		out.append(_wrap180(ha - p.sky[0]) * math.cos(p.sky[1] * DEG))
		out.append(dec - p.sky[1])
	return out


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
	"""Least squares by the normal equations with a little damping (small
	systems; no numpy needed)."""
	n = len(a[0])
	ata = [[sum(row[i] * row[j] for row in a) for j in range(n)] for i in range(n)]
	atb = [sum(row[i] * bi for row, bi in zip(a, b)) for i in range(n)]
	for i in range(n):
		ata[i][i] += 1e-9 + 1e-6 * ata[i][i]
	# Gaussian elimination with partial pivoting.
	m = [ata[i] + [atb[i]] for i in range(n)]
	for col in range(n):
		piv = max(range(col, n), key=lambda r: abs(m[r][col]))
		m[col], m[piv] = m[piv], m[col]
		if abs(m[col][col]) < 1e-15:
			continue
		for r in range(n):
			if r != col:
				f = m[r][col] / m[col][col]
				for c in range(col, n + 1):
					m[r][c] -= f * m[col][c]
	return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-15 else 0.0 for i in range(n)]


def fit(points: Sequence[AlignmentPoint], terms: Optional[Sequence[str]] = None) -> PointingModel:
	"""The model that best explains the points (Gauss-Newton). Only the
	terms the number of points can support are fitted."""
	model = PointingModel(points=len(points))
	if not points:
		return model
	if terms is None:
		terms = _TERMS_FOR_POINTS.get(len(points), TERMS)
	for _ in range(8):
		r0 = _residuals(model, points)
		jac: List[List[float]] = [[0.0] * len(terms) for _ in r0]
		for j, term in enumerate(terms):
			step = 1e-4
			setattr(model, term, getattr(model, term) + step)
			r1 = _residuals(model, points)
			setattr(model, term, getattr(model, term) - step)
			for i in range(len(r0)):
				jac[i][j] = (r1[i] - r0[i]) / step
		delta = _solve(jac, [-r for r in r0])
		for term, dv in zip(terms, delta):
			setattr(model, term, getattr(model, term) + dv)
		if max(abs(dv) for dv in delta) < 1e-7:
			break
	res = _residuals(model, points)
	model.rms_arcmin = math.sqrt(sum(r * r for r in res) / max(1, len(points))) * 60.0
	return model


def polar_report(model: PointingModel) -> Optional[str]:
	"""How to move the polar axis, once the pole terms are fitted (the
	mount frame is the northern one: south of the equator "up" is still
	towards the zenith)."""
	if model.points < 3 or (not model.alt and not model.az):
		return None
	up = -model.alt * 60.0
	east = -model.az * 60.0
	total = math.hypot(up, east)
	parts = []
	if abs(up) >= 0.5:
		parts.append(f"{'up' if up > 0 else 'down'} {abs(up):.0f}'")
	if abs(east) >= 0.5:
		parts.append(f"{'east' if east > 0 else 'west'} {abs(east):.0f}'")
	if not parts:
		return f"Polar axis within {max(total, 0.1):.1f}' of the pole"
	return f"Move the polar axis {' and '.join(parts)} ({total:.0f}' off)"


@dataclass
class Alignment:
	"""The points taken this session and the model fitted to them."""
	points: List[AlignmentPoint] = field(default_factory=list)
	model: PointingModel = field(default_factory=PointingModel)
	max_points: int = 12

	def add(self, point: AlignmentPoint) -> PointingModel:
		self.points.append(point)
		self.points = self.points[-self.max_points:]
		self.model = fit(self.points)
		return self.model

	def clear(self) -> None:
		self.points = []
		self.model = PointingModel()

	def to_dict(self) -> Dict[str, object]:
		return {"points": [p.to_dict() for p in self.points], "model": self.model.to_dict()}

	@classmethod
	def from_dict(cls, data: Optional[Dict[str, object]]) -> "Alignment":
		data = data or {}
		points = [AlignmentPoint.from_dict(p) for p in data.get("points", []) if isinstance(p, dict)]
		alignment = cls(points=points)
		alignment.model = fit(points) if points else PointingModel()
		return alignment
