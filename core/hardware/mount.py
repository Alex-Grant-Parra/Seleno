"""Equatorial mount geometry and slew routes.

The mount has two axes, measured in degrees and not wrapped (so the cable
wrap is known):

* `ra`: the RA axis. 0 is the meridian; it turns with hour angle, and
  sidereal tracking turns it forward (motor1 forward).
* `dec`: the Dec axis. From -90 to 90 it reads declination directly. Past
  either pole (90 or -90) the telescope is on the other side of the mount,
  "flipped": it then points at declination 180 - dec, twelve hours of hour
  angle round.

So every direction in the sky can be reached two ways (normal and flipped),
each in several turns of the axes.

South of the equator the polar axis points at the south celestial pole.
The maths runs in the "mount frame", which is the sky as a northern mount
would see it: (hour angle, declination) mirrored to (-ha, -dec). The
public functions take and return real sky coordinates and mirror at the
boundary when config.south is set; sidereal tracking then turns the RA
motor backwards (tracking_direction()).

A pointing model (core/hardware/pointing.py) can say where the telescope
really points for given axes. Pass it as `model` and the axis solutions
are refined so the telescope lands on the target, not where the ideal
geometry says it should be. plan_route() picks the quickest that
stays inside the axis limits and keeps clear of the Sun and of any dead
zones (Keepout: utils/dead_zones.py). When the straight move (both axes
together) would pass too close, it curves round them.
The firmware runs the route as one smooth path ({"cmd":"path"}), so the
route can be over the pole (towards the zenith, or under towards the
nadir) whenever that's quicker.

Pure maths: no I/O. core/hardware/tracking.py does the motors and state.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

DEG = math.pi / 180.0
Point = Tuple[float, float]  # (ra axis, dec axis), degrees
# axes -> where the telescope really points, (ha, dec) in the mount frame
Model = Callable[[Point], Tuple[float, float]]


@dataclass
class MountConfig:
	ra_gear: float = 360.0
	dec_gear: float = 144.0
	steps_per_rev: float = 1600.0     # motor logical steps per motor turn
	slew_sps: float = 1200.0
	ra_limits: Tuple[float, float] = (-270.0, 270.0)
	dec_limits: Tuple[float, float] = (-180.0, 180.0)
	pole_crossing: bool = True        # may use the flipped side
	south: bool = False               # polar axis at the south celestial pole

	def motor_steps(self, d_ra: float, d_dec: float) -> Tuple[float, float]:
		"""Axis degrees to motor1/motor2 logical steps."""
		return (d_ra * self.ra_gear * self.steps_per_rev / 360.0,
			d_dec * self.dec_gear * self.steps_per_rev / 360.0)

	def axis_degrees(self, steps1: float, steps2: float) -> Tuple[float, float]:
		return (steps1 * 360.0 / (self.ra_gear * self.steps_per_rev),
			steps2 * 360.0 / (self.dec_gear * self.steps_per_rev))

	def seconds(self, a: Point, b: Point) -> float:
		"""Time for a straight move (both axes together, the slower-to-finish
		one at slew speed)."""
		s1, s2 = self.motor_steps(b[0] - a[0], b[1] - a[1])
		return max(abs(s1), abs(s2)) / self.slew_sps

	def within(self, p: Point, slack: float = 0.0) -> bool:
		return (self.ra_limits[0] - slack <= p[0] <= self.ra_limits[1] + slack
			and self.dec_limits[0] - slack <= p[1] <= self.dec_limits[1] + slack)


def wrap180(deg: float) -> float:
	return (deg + 180.0) % 360.0 - 180.0


# --- axes and sky -------------------------------------------------------------

def is_flipped(dec_axis: float) -> bool:
	return abs(wrap180(dec_axis)) > 90.0


def mount_frame(sky: Tuple[float, float], config: Optional[MountConfig]) -> Tuple[float, float]:
	"""Real (ha, dec) to the mount frame and back (the mirror is its own
	inverse)."""
	if config is not None and config.south:
		return wrap180(-sky[0]), -sky[1]
	return sky


def tracking_direction(config: Optional[MountConfig]) -> int:
	"""+1: sidereal tracking turns the RA motor forward; -1 in the south."""
	return -1 if config is not None and config.south else 1


def _frame_from_axes(ra_axis: float, dec_axis: float) -> Tuple[float, float]:
	d = wrap180(dec_axis)
	if -90.0 <= d <= 90.0:
		return wrap180(ra_axis), d
	return wrap180(ra_axis + 180.0), (180.0 - d) if d > 0 else (-180.0 - d)


def sky_from_axes(ra_axis: float, dec_axis: float, config: Optional[MountConfig] = None) -> Tuple[float, float]:
	"""Axis angles to (hour angle, declination), degrees (ideal geometry;
	the pointing model, if any, is applied by the caller)."""
	return mount_frame(_frame_from_axes(ra_axis, dec_axis), config)


def axis_rates(ha_rate: float, dec_rate: float, dec_axis: float, config: Optional[MountConfig]) -> Tuple[float, float]:
	"""Sky rates (deg/s of hour angle and declination) to axis rates (deg/s
	of the RA and Dec axes) on the side the mount is on."""
	m = -1.0 if config is not None and config.south else 1.0
	da = m * ha_rate
	dd = m * dec_rate
	return da, (-dd if is_flipped(dec_axis) else dd)


def _refine(goal: Point, target: Tuple[float, float], model: Model, rounds: int = 4) -> Point:
	"""Nudge an axis position until the model says it points at target
	(mount frame). The model's errors are small, so a few rounds do."""
	a, d = goal
	for _ in range(rounds):
		ha, dec = model((a, d))
		dha, ddec = wrap180(target[0] - ha), target[1] - dec
		if abs(dha) < 1e-6 and abs(ddec) < 1e-6:
			break
		a += dha
		d += -ddec if is_flipped(d) else ddec
	return a, d


def axis_solutions(ha: float, dec: float, config: MountConfig, slack: float = 0.0,
		model: Optional[Model] = None) -> List[Point]:
	"""Every axis position inside the limits that points at (ha, dec):
	normal and (if allowed) flipped, each in every turn of the axes."""
	ha, dec = mount_frame((ha, dec), config)
	found = _frame_solutions(ha, dec, config, slack)
	if model is None:
		return found
	return [_refine(p, (ha, dec), model) for p in found]


def _frame_solutions(ha: float, dec: float, config: MountConfig, slack: float = 0.0) -> List[Point]:
	bases = [(ha, dec)]
	if config.pole_crossing:
		bases.append((ha + 180.0, 180.0 - dec))
	found: List[Point] = []
	for base_ra, base_dec in bases:
		for k in range(-2, 3):
			a = wrap180(base_ra) + 360.0 * k
			if not config.ra_limits[0] - slack <= a <= config.ra_limits[1] + slack:
				continue
			for m in range(-2, 3):
				d = wrap180(base_dec) + 360.0 * m
				if config.dec_limits[0] - slack <= d <= config.dec_limits[1] + slack:
					found.append((a, d))
	return found


def nearest_solution(ha: float, dec: float, near: Point, config: MountConfig,
		model: Optional[Model] = None) -> Point:
	"""Of the ways to point at (ha, dec), the one closest to `near` (for a
	sync: the side the mount is on now). Ignores the limits if it must."""
	options = (axis_solutions(ha, dec, config, model=model)
		or axis_solutions(ha, dec, config, slack=1e6, model=model))
	return min(options, key=lambda p: config.seconds(near, p))


def _xyz(ha: float, dec: float) -> Tuple[float, float, float]:
	h, d = ha * DEG, dec * DEG
	return (math.cos(d) * math.cos(h), math.cos(d) * math.sin(h), math.sin(d))


def separation(a: Tuple[float, float], b: Tuple[float, float]) -> float:
	"""Angle between two (ha, dec) directions, degrees."""
	x, y = _xyz(*a), _xyz(*b)
	dot = max(-1.0, min(1.0, x[0] * y[0] + x[1] * y[1] + x[2] * y[2]))
	return math.acos(dot) / DEG


def closest_to(route: Sequence[Point], sky: Tuple[float, float], step_deg: float = 0.5,
		config: Optional[MountConfig] = None) -> float:
	"""Closest a route comes to a sky direction (ha, dec), checked every
	step_deg of axis travel."""
	sky = mount_frame(sky, config)
	target = _xyz(*sky)
	best = 180.0
	for a, b in zip(route, route[1:]):
		n = max(1, int(math.ceil(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / step_deg)))
		for i in range(n + 1):
			t = i / n
			p = _xyz(*_frame_from_axes(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
			dot = max(-1.0, min(1.0, p[0] * target[0] + p[1] * target[1] + p[2] * target[2]))
			best = min(best, math.acos(dot) / DEG)
	if len(route) == 1:
		best = separation(_frame_from_axes(*route[0]), sky)
	return best


@dataclass
class Keepout:
	"""Sky a route mustn't enter (a dead zone). Both take and give real
	(ha, dec), not the mount frame."""
	name: str
	inside: Callable[[Tuple[float, float]], bool]
	# padded outlines (ha, dec) of convex-ish pieces to go round; pieces
	# that can't be gone round (round a pole) are left out, only checked
	outlines: Callable[[float], List[List[Tuple[float, float]]]]


def zone_hit(route: Sequence[Point], keepouts: Sequence[Keepout], config: Optional[MountConfig] = None,
		step_deg: float = 0.5) -> Optional[str]:
	"""The name of the first keep-out the route goes into, or None. Leaving
	one the route starts in is fine; going back in isn't."""
	if not keepouts:
		return None
	samples: List[Tuple[float, float]] = []
	for a, b in zip(route, route[1:]):
		n = max(1, int(math.ceil(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / step_deg)))
		start = 0 if not samples else 1
		for i in range(start, n + 1):
			t = i / n
			samples.append(sky_from_axes(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, config))
	if len(route) == 1:
		samples.append(sky_from_axes(*route[0], config))
	for k in keepouts:
		leaving = k.inside(samples[0])  # still in the stretch it started in
		for sky in samples[1:]:
			if not k.inside(sky):
				leaving = False
			elif not leaving:
				return k.name
	return None


# --- routes ---------------------------------------------------------------------

@dataclass
class Route:
	points: List[Point]           # axis positions, the start first
	seconds: float                # at slew speed
	sun_deg: float                # closest it comes to the Sun (180 without one)
	detour: bool = False          # curves round the Sun or a dead zone
	notes: List[str] = field(default_factory=list)
	zone: Optional[str] = None    # a dead zone it goes into (never run one)

	@property
	def end(self) -> Point:
		return self.points[-1]

	@property
	def flips(self) -> bool:
		return is_flipped(self.points[0][1]) != is_flipped(self.points[-1][1])


def _route_seconds(points: Sequence[Point], config: MountConfig) -> float:
	return sum(config.seconds(a, b) for a, b in zip(points, points[1:]))


def _circle(center: Tuple[float, float], radius: float, step_deg: float) -> List[Tuple[float, float]]:
	"""Points `radius` from (ha, dec) all the way round, as (ha, dec)."""
	lat1, lon1, d = center[1] * DEG, center[0] * DEG, radius * DEG
	out = []
	for i in range(int(round(360.0 / step_deg))):
		theta = i * step_deg * DEG
		lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(theta))
		lon2 = lon1 + math.atan2(math.sin(theta) * math.sin(d) * math.cos(lat1),
			math.cos(d) - math.sin(lat1) * math.sin(lat2))
		out.append((lon2 / DEG, lat2 / DEG))
	return out


def _hull(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
	"""Convex hull, counter-clockwise (monotone chain)."""
	pts = sorted(set(points))
	if len(pts) <= 2:
		return pts

	def cross(o, a, b):
		return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

	lower: List[Tuple[float, float]] = []
	for p in pts:
		while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
			lower.pop()
		lower.append(p)
	upper: List[Tuple[float, float]] = []
	for p in reversed(pts):
		while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
			upper.pop()
		upper.append(p)
	return lower[:-1] + upper[:-1]


def _inside(poly: Sequence[Tuple[float, float]], p: Tuple[float, float]) -> bool:
	"""Strictly inside a counter-clockwise convex polygon."""
	n = len(poly)
	for i in range(n):
		a, b = poly[i], poly[(i + 1) % n]
		if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) <= 1e-9:
			return False
	return True


def _crosses(poly: Sequence[Tuple[float, float]], p: Tuple[float, float], q: Tuple[float, float]) -> bool:
	"""Whether segment pq goes through a convex polygon's inside (running
	along its edges or touching a corner doesn't count)."""
	# Clip the segment to the polygon (Cyrus-Beck); it crosses if a piece of
	# positive length is left strictly inside.
	t0, t1 = 0.0, 1.0
	dx, dy = q[0] - p[0], q[1] - p[1]
	n = len(poly)
	for i in range(n):
		a, b = poly[i], poly[(i + 1) % n]
		ex, ey = b[0] - a[0], b[1] - a[1]
		# Inside is to the left of each edge: cross(e, x - a) > 0.
		num = ex * (p[1] - a[1]) - ey * (p[0] - a[0])
		den = ex * dy - ey * dx
		if abs(den) < 1e-12:
			if num <= 1e-9:
				return False
			continue
		t = -num / den
		if den > 0:
			t0 = max(t0, t)
		else:
			t1 = min(t1, t)
		if t1 - t0 <= 1e-6:
			return False
	return t1 - t0 > 1e-6


class _Planner:
	"""Shortest routes round the Sun's keep-out and the dead zones, in the
	axis plane scaled to motor turns (so a straight line there is a move of
	both axes together, and its length is the motors' work)."""

	def __init__(self, config: MountConfig, sun: Optional[Tuple[float, float]], keep_out: float,
			keepouts: Sequence[Keepout] = ()) -> None:
		self.config = config
		self.sun = sun if keep_out > 0.0 else None  # mount frame
		self.keep_out = keep_out
		self.keepouts = list(keepouts)
		self.sx = config.ra_gear
		self.sy = config.dec_gear

	def scaled(self, p: Point) -> Tuple[float, float]:
		return p[0] * self.sx, p[1] * self.sy

	def unscaled(self, q: Tuple[float, float]) -> Point:
		return q[0] / self.sx, q[1] / self.sy

	def clear(self, points: Sequence[Point]) -> bool:
		if self.sun is not None and closest_to(points, self.sun) < self.keep_out:
			return False
		return zone_hit(points, self.keepouts, self.config) is None

	def _placed(self, center: Tuple[float, float], outline: Sequence[Tuple[float, float]],
			a: Point, b: Point) -> List[List[Tuple[float, float]]]:
		"""An outline round center (both mount-frame (ha, dec)) as convex
		polygons in the scaled axis plane: one per way of pointing at it
		(normal or flipped, each turn) near the move from a to b."""
		offsets = [(wrap180(ha - center[0]), dec - center[1]) for ha, dec in outline]
		reach_ra = max(abs(da) for da, _dd in offsets) + 2.0
		reach_dec = max(abs(dd) for _da, dd in offsets)
		lo_ra, hi_ra = min(a[0], b[0]) - reach_ra - 30.0, max(a[0], b[0]) + reach_ra + 30.0
		lo_dec, hi_dec = min(a[1], b[1]) - reach_dec - 30.0, max(a[1], b[1]) + reach_dec + 30.0
		polys = []
		for flipped in (False, True):
			base_ra = center[0] + (180.0 if flipped else 0.0)
			base_dec = (180.0 - center[1]) if flipped else center[1]
			for k in range(-2, 3):
				ca = wrap180(base_ra) + 360.0 * k
				if not lo_ra <= ca <= hi_ra:
					continue
				for m in range(-2, 3):
					cd = wrap180(base_dec) + 360.0 * m
					if not lo_dec <= cd <= hi_dec:
						continue
					polys.append(_hull([self.scaled((ca + da, cd + (-dd if flipped else dd))) for da, dd in offsets]))
		return polys

	def obstacles(self, a: Point, b: Point, radius: float, zone_pad: float = 1.0) -> List[List[Tuple[float, float]]]:
		"""The Sun's keep-out (radius degrees) and the dead zones (padded
		by zone_pad) as convex polygons in the scaled axis plane."""
		polys = []
		if self.sun is not None:
			polys.extend(self._placed(self.sun, _circle(self.sun, radius, 15.0), a, b))
		for keepout in self.keepouts:
			for outline in keepout.outlines(zone_pad):
				framed = [mount_frame(p, self.config) for p in outline]
				x = sum(_xyz(*p)[0] for p in framed)
				y = sum(_xyz(*p)[1] for p in framed)
				z = sum(_xyz(*p)[2] for p in framed)
				center = (math.atan2(y, x) / DEG, math.atan2(z, math.hypot(x, y)) / DEG)
				polys.extend(self._placed(center, framed, a, b))
		return polys

	def route(self, start: Point, goal: Point) -> Optional[List[Point]]:
		"""Straight if that keeps clear, else the shortest way round; None if
		there's no clear way inside the limits."""
		straight = [start, goal]
		if self.clear(straight):
			return straight
		for sun_pad, zone_pad in ((1.5, 3.0), (3.0, 1.0), (5.0, 0.3)):
			found = self._around(start, goal, self.keep_out + sun_pad, zone_pad)
			if found is not None and self.clear(found):
				return found
		return None

	def _around(self, start: Point, goal: Point, radius: float, zone_pad: float = 1.0) -> Optional[List[Point]]:
		polys = self.obstacles(start, goal, radius, zone_pad)
		s, g = self.scaled(start), self.scaled(goal)
		# A start or goal inside an outline can't go round it; leave that
		# one out (its closeness is then down to the start or goal).
		polys = [p for p in polys if len(p) >= 3 and not _inside(p, s) and not _inside(p, g)]
		nodes: List[Tuple[float, float]] = [s, g]
		owner: List[Tuple[int, int]] = [(-1, -1), (-1, -1)]
		for pi, poly in enumerate(polys):
			for vi, v in enumerate(poly):
				if self.config.within(self.unscaled(v)) and not any(_inside(q, v) for q in polys if q is not poly):
					nodes.append(v)
					owner.append((pi, vi))
		seen: Dict[Tuple[int, int], bool] = {}

		def visible(i: int, j: int) -> bool:
			key = (min(i, j), max(i, j))
			if key not in seen:
				oi, oj = owner[i], owner[j]
				if oi[0] >= 0 and oi[0] == oj[0]:
					n = len(polys[oi[0]])
					# Along the outline only (any other chord cuts through it),
					# and not through another outline.
					seen[key] = ((oi[1] - oj[1]) % n in (1, n - 1)
						and not any(_crosses(poly, nodes[i], nodes[j]) for k, poly in enumerate(polys) if k != oi[0]))
				else:
					seen[key] = not any(_crosses(poly, nodes[i], nodes[j]) for poly in polys)
			return seen[key]

		def cost(i: int, j: int) -> float:
			return max(abs(nodes[i][0] - nodes[j][0]), abs(nodes[i][1] - nodes[j][1]))

		best: Dict[int, float] = {0: 0.0}
		prev: Dict[int, int] = {}
		queue = [(0.0, 0)]
		while queue:
			d, i = heapq.heappop(queue)
			if i == 1:
				break
			if d > best.get(i, math.inf):
				continue
			for j in range(len(nodes)):
				if j == i or not visible(i, j):
					continue
				nd = d + cost(i, j)
				if nd < best.get(j, math.inf) - 1e-9:
					best[j] = nd
					prev[j] = i
					heapq.heappush(queue, (nd, j))
		if 1 not in best:
			return None
		path = [1]
		while path[-1] != 0:
			path.append(prev[path[-1]])
		return [start] + [self.unscaled(nodes[i]) for i in reversed(path[1:-1])] + [goal]


def _blocked_note(sun: bool, keepouts: Sequence[Keepout]) -> str:
	what = " or ".join(w for w, on in (("the Sun", sun), ("the exclusion zones", bool(keepouts))) if on)
	return f"no way round {what} inside the axis limits"


def plan_route(start: Point, target: Tuple[float, float], config: MountConfig,
		sun: Optional[Tuple[float, float]] = None, keep_out: float = 0.0,
		model: Optional[Model] = None, keepouts: Sequence[Keepout] = ()) -> Route:
	"""The quickest route from the axis position `start` to point at
	target (ha, dec), over the pole if that's quicker and allowed, never
	closer than keep_out degrees to the Sun (ha, dec) and never into a
	keep-out (a dead zone) when there's a way round. If nothing keeps
	clear, the quickest route anyway: its sun_deg says how close it gets,
	and its zone names a dead zone it goes into (such a route must not be
	run)."""
	goals = axis_solutions(target[0], target[1], config, model=model)
	if not goals:
		goals = [nearest_solution(target[0], target[1], start, config, model=model)]
	goals.sort(key=lambda g: config.seconds(start, g))
	use_sun = sun is not None and keep_out > 0.0
	if not use_sun and not keepouts:
		goal = goals[0]
		return Route([start, goal], config.seconds(start, goal), 180.0)
	sun_m = mount_frame(sun, config) if sun is not None else None  # the planner works in the mount frame
	planner = _Planner(config, sun_m if use_sun else None, keep_out if use_sun else 0.0, keepouts)

	def sun_deg(points: Sequence[Point]) -> float:
		return closest_to(points, sun_m) if sun_m is not None else 180.0

	best: Optional[Route] = None
	for goal in goals:
		if best is not None and config.seconds(start, goal) >= best.seconds:
			break  # even straight it's slower than what we have
		points = planner.route(start, goal)
		if points is None:
			continue
		seconds = _route_seconds(points, config)
		if best is None or seconds < best.seconds - 1e-6:
			best = Route(points, seconds, sun_deg(points), detour=len(points) > 2)
	if best is not None:
		return best
	goal = goals[0]
	route = Route([start, goal], config.seconds(start, goal), sun_deg([start, goal]),
		zone=zone_hit([start, goal], keepouts, config))
	route.notes.append(_blocked_note(use_sun, keepouts))
	return route


def route_steps(route: Route, config: MountConfig) -> List[Tuple[int, int]]:
	"""The route as {"cmd":"path"} points: motor1/motor2 logical steps from
	the start."""
	start = route.points[0]
	out: List[Tuple[int, int]] = []
	for p in route.points[1:]:
		s1, s2 = config.motor_steps(p[0] - start[0], p[1] - start[1])
		out.append((int(round(s1)), int(round(s2))))
	return out


def plan_axes_route(start: Point, goal: Point, config: MountConfig,
		sun: Optional[Tuple[float, float]] = None, keep_out: float = 0.0,
		keepouts: Sequence[Keepout] = ()) -> Route:
	"""A route between two axis positions (parking): round the Sun's
	keep-out and the dead zones when the straight way passes them. sun is
	real (ha, dec). Its zone names a dead zone it goes into anyway."""
	use_sun = sun is not None and keep_out > 0.0
	if not use_sun and not keepouts:
		return Route([start, goal], config.seconds(start, goal), 180.0)
	sun_m = mount_frame(sun, config) if sun is not None else None
	points = _Planner(config, sun_m if use_sun else None, keep_out if use_sun else 0.0, keepouts).route(start, goal)
	note = []
	if points is None:
		points = [start, goal]
		note.append(_blocked_note(use_sun, keepouts))
	route = Route(points, _route_seconds(points, config), closest_to(points, sun_m) if sun_m is not None else 180.0,
		detour=len(points) > 2, zone=zone_hit(points, keepouts, config))
	route.notes.extend(note)
	return route
