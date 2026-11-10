"""Dead zones: parts of the sky the telescope must never point at.

Some directions are off limits: the camera would hit the pier or crush
a motor, or it would look into somebody's window. Each zone is a box in
altitude and azimuth, fixed to the ground:

config dead_zones:
	zones   [{"name": "Next door", "az": [80, 120], "alt": [-90, 35],
	          "reason": "privacy", "locked": false}, ...]

* az is [from, to], clockwise from north through east; from > to wraps
  through north ([350, 10] is 20 deg wide), and [0, 360] is all round.
* alt is [lowest, highest], -90 to 90.
* reason is "privacy", "physical" or "other"; it only changes the wording.
* locked zones were set on the Pi: the server sees them but can't change
  or remove them (setDeadZones keeps them).

What they do (core/hardware/tracking.py, utils/motor_guard.py):
* a go-to into a zone is refused, from the server and the panel alike;
* slews curve round zones (core/hardware/mount.py, like the Sun), and are
  refused when there's no way round;
* tracking stops when the target drifts into one as the sky turns;
* raw motor moves are refused into a zone, and continuous ones stopped at
  its edge.
Moving out of a zone the telescope is already in is always allowed.

The server shows them on its star map (the status push's dead_zones part).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REASONS = ("privacy", "physical", "other")
MAX_ZONES = 32
PIECE_DEG = 30.0  # slews go round each zone in pieces at most this big
ROUTE_PADS = (3.0, 1.0, 0.3)  # how far out slews try to keep, degrees


@dataclass(frozen=True)
class Zone:
	name: str
	az_from: float
	az_to: float
	alt_lo: float
	alt_hi: float
	reason: str = "other"
	locked: bool = False

	@property
	def az_width(self) -> float:
		if self.az_to - self.az_from >= 360.0:
			return 360.0
		width = (self.az_to - self.az_from) % 360.0
		return 360.0 if width == 0.0 and self.az_to != self.az_from else width

	def contains(self, alt: float, az: float, pad: float = 0.0) -> bool:
		if not self.alt_lo - pad <= alt <= self.alt_hi + pad:
			return False
		if self.az_width >= 360.0:
			return True
		# pad degrees of sky are more degrees of azimuth up high
		pad_az = pad / max(0.1, math.cos(math.radians(min(89.0, abs(alt)))))
		if self.az_width + 2 * pad_az >= 360.0:
			return True
		return (az - (self.az_from - pad_az)) % 360.0 <= self.az_width + 2 * pad_az

	def to_dict(self) -> Dict[str, Any]:
		return {"name": self.name, "az": [self.az_from, self.az_to], "alt": [self.alt_lo, self.alt_hi],
			"reason": self.reason, "locked": self.locked}

	def describe(self) -> str:
		return f"the exclusion zone '{self.name}'" if self.name else "an exclusion zone"


def _number(value: Any, what: str) -> float:
	try:
		number = float(value)
	except (TypeError, ValueError):
		raise ValueError(f"{what} must be a number")
	if math.isnan(number) or math.isinf(number):
		raise ValueError(f"{what} must be a number")
	return number


def parse_zone(raw: Any, index: int = 0) -> Zone:
	"""One zone from its dict; ValueError says what's wrong."""
	where = f"Zone {index + 1}"
	if not isinstance(raw, dict):
		raise ValueError(f"{where} must be an object with az and alt")
	az, alt = raw.get("az"), raw.get("alt")
	if not isinstance(az, (list, tuple)) or len(az) != 2:
		raise ValueError(f"{where}: az must be [from, to]")
	if not isinstance(alt, (list, tuple)) or len(alt) != 2:
		raise ValueError(f"{where}: alt must be [lowest, highest]")
	az_from, az_to = _number(az[0], f"{where} az"), _number(az[1], f"{where} az")
	lo, hi = sorted((_number(alt[0], f"{where} alt"), _number(alt[1], f"{where} alt")))
	if lo < -90.0 or hi > 90.0:
		raise ValueError(f"{where}: alt must be from -90 to 90")
	if hi - lo < 0.1:
		raise ValueError(f"{where}: alt is empty")
	if az_to - az_from >= 360.0 or (az_from == 0.0 and az_to == 360.0):
		az_from, az_to = 0.0, 360.0  # all round
	else:
		az_from, az_to = az_from % 360.0, az_to % 360.0
		if az_from == az_to:
			raise ValueError(f"{where}: az is empty (use [0, 360] for all round)")
	reason = str(raw.get("reason") or "other").strip().lower()
	if reason not in REASONS:
		reason = "other"
	name = " ".join(str(raw.get("name") or "").split())[:40]
	return Zone(name, round(az_from, 2), round(az_to, 2), round(lo, 2), round(hi, 2), reason,
		bool(raw.get("locked", False)))


def parse_zones(raw: Any) -> List[Zone]:
	if raw is None:
		return []
	if not isinstance(raw, (list, tuple)):
		raise ValueError("zones must be a list")
	if len(raw) > MAX_ZONES:
		raise ValueError(f"At most {MAX_ZONES} exclusion zones")
	return [parse_zone(z, i) for i, z in enumerate(raw)]


def zones() -> List[Zone]:
	"""The configured zones; bad entries are skipped (and logged)."""
	try:
		from utils.config_state import get_section

		raw = get_section("dead_zones").get("zones") or []
	except Exception:
		return []
	out = []
	for i, z in enumerate(raw if isinstance(raw, list) else []):
		try:
			out.append(parse_zone(z, i))
		except ValueError as exc:
			from utils.log import get_logger

			get_logger("dead_zones").warning("Skipping a dead zone in config: %s", exc)
	return out


def save(new: Sequence[Zone]) -> List[Zone]:
	"""Replace the zones the server may change; locked ones stay."""
	from utils.config_state import save_section

	kept = [z for z in zones() if z.locked]
	unlocked = [Zone(z.name, z.az_from, z.az_to, z.alt_lo, z.alt_hi, z.reason, False) for z in new]
	if len(kept) + len(unlocked) > MAX_ZONES:
		raise ValueError(f"At most {MAX_ZONES} exclusion zones")
	save_section("dead_zones", {"zones": [z.to_dict() for z in kept + unlocked]})
	return zones()


def zone_at(alt: float, az: float, among: Optional[Iterable[Zone]] = None) -> Optional[Zone]:
	for z in zones() if among is None else among:
		if z.contains(alt, az % 360.0):
			return z
	return None


def zone_at_hadec(ha: float, dec: float, lat: float, among: Optional[Iterable[Zone]] = None) -> Optional[Zone]:
	from utils.astrometry import hadec_to_altaz

	alt, az = hadec_to_altaz(ha, dec, lat)
	return zone_at(alt, az, among)


def status_part() -> Dict[str, Any]:
	"""For the status push and getDeadZones."""
	return {"zones": [z.to_dict() for z in zones()]}


# --- for the route planner -------------------------------------------------------------

def _pieces(z: Zone) -> List[Tuple[float, float, float, float]]:
	"""The zone cut into (az_from, az_width, alt_lo, alt_hi) pieces no
	bigger than PIECE_DEG: each maps to a nearly convex patch of the axis
	plane, so its convex hull hugs it."""
	width = z.az_width
	n_az = max(1, int(math.ceil(width / PIECE_DEG)))
	n_alt = max(1, int(math.ceil((z.alt_hi - z.alt_lo) / PIECE_DEG)))
	out = []
	for i in range(n_az):
		for j in range(n_alt):
			out.append((z.az_from + width * i / n_az, width / n_az,
				z.alt_lo + (z.alt_hi - z.alt_lo) * j / n_alt, z.alt_lo + (z.alt_hi - z.alt_lo) * (j + 1) / n_alt))
	return out


def _piece_outline(piece: Tuple[float, float, float, float], pad: float, lat: float) -> Optional[List[Tuple[float, float]]]:
	"""A piece's padded edge as (ha, dec), or None when it holds a pole
	(no outline goes round that: those pieces are only checked)."""
	from utils.astrometry import altaz_to_hadec

	az0, width, lo, hi = piece
	lo, hi = max(-90.0, lo - pad), min(90.0, hi + pad)
	mid = max(abs(lo), abs(hi)) if lo * hi > 0 else 0.0
	pad_az = pad / max(0.1, math.cos(math.radians(min(89.0, mid))))
	a0, a1 = az0 - pad_az, az0 + width + pad_az
	if a1 - a0 >= 360.0 or hi >= 89.9 or lo <= -89.9:
		return None
	pole_alt, pole_az = (abs(lat), 0.0 if lat >= 0 else 180.0)
	for alt, az in ((pole_alt, pole_az), (-pole_alt, (pole_az + 180.0) % 360.0)):
		if lo <= alt <= hi and (az - a0) % 360.0 <= a1 - a0:
			return None
	steps = 4
	edge: List[Tuple[float, float]] = []
	for k in range(steps + 1):
		edge.append((lo, a0 + (a1 - a0) * k / steps))
	for k in range(1, steps + 1):
		edge.append((lo + (hi - lo) * k / steps, a1))
	for k in range(1, steps + 1):
		edge.append((hi, a1 - (a1 - a0) * k / steps))
	for k in range(1, steps):
		edge.append((hi - (hi - lo) * k / steps, a0))
	return [altaz_to_hadec(alt, az % 360.0, lat) for alt, az in edge]


def keepouts(lat: float, among: Optional[Iterable[Zone]] = None) -> List[Any]:
	"""The zones as core.hardware.mount.Keepout, for the route planner and
	the route checks, at a site's latitude."""
	from core.hardware.mount import Keepout

	out = []
	for z in zones() if among is None else among:
		def inside(hadec: Tuple[float, float], z: Zone = z) -> bool:
			from utils.astrometry import hadec_to_altaz

			alt, az = hadec_to_altaz(hadec[0], hadec[1], lat)
			return z.contains(alt, az)

		def outlines(pad: float, z: Zone = z) -> List[List[Tuple[float, float]]]:
			found = (_piece_outline(p, pad, lat) for p in _pieces(z))
			return [o for o in found if o]

		out.append(Keepout(z.describe(), inside, outlines))
	return out
