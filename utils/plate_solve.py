"""Plate-solving jobs (core/astrometry/solver.py does the solving).

* solve_and_sync: where is it really pointing? Add that to the session's
  alignment (core/hardware/pointing.py). While tracking, it then corrects
  onto the target.
* centre_target: solve, correct, solve again, until the target is within
  solver.centre_tolerance_arcmin (three rounds at most).
* polar_align: the three-point method. Solve, turn the RA axis, solve,
  turn, solve: the three places lie on a circle round the mount's polar
  axis, which gives how far it is from the celestial pole, as up/down and
  east/west turns of the mount's adjusters.
* drift_check: while tracking, solve the live view now and then and
  correct (config solver.drift_check_min).

Pictures come from live view when it's running (no shutter, nothing
saved) or a photo otherwise; the photo's JPEG is solved (RAW alone can't
be). Solves also become alignment points.
"""
from __future__ import annotations

import math
import os
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.log import get_logger

log = get_logger("plate_solve")
DEG = math.pi / 180.0
POLAR_STEP_DEG = 30.0


class SolveJobError(RuntimeError):
	pass


def _solver():
	from core.astrometry import solver

	if not solver.available():
		raise SolveJobError("No plate solver installed (ASTAP in ~/astap)")
	return solver


def _picture(source: str = "auto") -> Tuple[str, bool]:
	"""(path, temporary) of a picture to solve."""
	from core.camera.service import get_camera_service

	service = get_camera_service()
	if not service.is_available():
		raise SolveJobError("Camera not connected")
	_seq, frame, at = service.frames.latest()
	fresh = frame is not None and time.monotonic() - at < 5.0
	if source == "liveview" or (source == "auto" and fresh and service.previewing):
		if not fresh:
			raise SolveJobError("No live view frame")
		handle, path = tempfile.mkstemp(prefix="solve-", suffix=".jpg")
		with os.fdopen(handle, "wb") as f:
			f.write(frame)
		return path, True
	from utils import photos

	folder = os.path.join(photos.base_dir(), "solves", os.path.basename(photos.night_folder()))
	paths = photos.capture_to(folder, f"solve_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
	jpegs = [p for p in paths if p.lower().endswith((".jpg", ".jpeg"))]
	if not jpegs:
		raise SolveJobError("The camera saved no JPEG to solve: set it to RAW + JPEG or JPEG")
	return jpegs[0], False


def solve_now(source: str = "auto"):
	"""Solve where the telescope points now; hinted with where it thinks
	it is."""
	solver = _solver()
	from utils.telescope_state import get_telescope_coords

	coords = get_telescope_coords() or {}
	path, temporary = _picture(source)
	try:
		return solver.solve(path, coords.get("right_ascension"), coords.get("declination"))
	except solver.SolveError as exc:
		raise SolveJobError(str(exc)) from exc
	finally:
		if temporary:
			try:
				os.remove(path)
			except OSError:
				pass


def _separation(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
	from utils.sky_view import angular_distance

	return angular_distance(ra1, dec1, ra2, dec2)


def _record(result, name: str = "plate solve") -> None:
	"""Add a solve to the alignment without stopping anything."""
	from core.hardware import tracking
	from utils.telescope_state import add_alignment_point

	tracking._update_current_coords_from_motors()
	if add_alignment_point(result.ra, result.dec, name=name, source="solve") is None:
		from utils.telescope_state import set_telescope_coords

		set_telescope_coords(result.ra, result.dec, source="solve")


def solve_and_sync(source: str = "auto") -> Dict[str, Any]:
	from core.hardware import tracking
	from utils.telescope_state import get_telescope_coords

	before = get_telescope_coords() or {}
	result = solve_now(source)
	_record(result)
	out = result.to_dict()
	if before:
		out["was_off_arcmin"] = round(60.0 * _separation(result.ra, result.dec,
			before["right_ascension"], before["declination"]), 1)
	if tracking.is_sidereal_tracking():
		out["recentred"] = tracking.recentre()
	return out


def _tolerance_deg() -> float:
	try:
		from utils.config_state import get_section

		return float(get_section("solver").get("centre_tolerance_arcmin", 2.0)) / 60.0
	except Exception:
		return 2.0 / 60.0


def centre_target(rounds: int = 3, source: str = "auto") -> Dict[str, Any]:
	from core.hardware import tracking

	target = tracking._target_object
	if target is None or not tracking._tracking_active:
		raise SolveJobError("Not tracking anything")
	history: List[float] = []
	for _ in range(max(1, rounds)):
		result = solve_now(source)
		aim = _target_j2000(target)
		error = _separation(result.ra, result.dec, aim[0], aim[1])
		history.append(round(error * 60.0, 2))
		if error <= _tolerance_deg():
			return {"centred": True, "error_arcmin": history[-1], "rounds": len(history), "history": history}
		_record(result, name=str(target.get("name", "")))
		if not tracking.recentre():
			break
	return {"centred": False, "error_arcmin": history[-1], "rounds": len(history), "history": history}


def _target_j2000(target: Dict[str, Any]) -> Tuple[float, float]:
	from utils import clock, ephemeris

	off_ra, off_dec = target.get("offset", (0.0, 0.0))
	body = target.get("body")
	if body:
		from core.hardware.tracking import _site

		site = _site()
		ra, dec = ephemeris.position(body, clock.now(), site if body == "Moon" else None)
	else:
		ra, dec = float(target["ra"]), float(target["dec"])
	return ra + off_ra, dec + off_dec


def drift_check() -> Optional[Dict[str, Any]]:
	"""While tracking with live view on: solve, and correct if it's off."""
	from core.hardware import tracking

	if not tracking.is_sidereal_tracking() or tracking._target_object is None:
		return None
	result = solve_now("liveview")
	aim = _target_j2000(tracking._target_object)
	error = _separation(result.ra, result.dec, aim[0], aim[1])
	log.info("Drift check: %.1f arcmin off", error * 60.0)
	if error > _tolerance_deg():
		_record(result, name="drift check")
		tracking.recentre()
	return {"error_arcmin": round(error * 60.0, 2)}


# --- polar alignment ----------------------------------------------------------------

def _hadec_vector(ra_j2000: float, dec_j2000: float, when) -> Tuple[float, float, float]:
	"""The direction in the Earth's frame (apparent hour angle, of date)."""
	from utils.telescope_state import to_mount_sky

	ha, dec = to_mount_sky(ra_j2000, dec_j2000, when)
	ha *= DEG
	d = dec * DEG
	return (math.cos(d) * math.cos(ha), math.cos(d) * math.sin(ha), math.sin(d))


def pole_from_points(vectors: List[Tuple[float, float, float]], south: bool = False) -> Tuple[float, float, float]:
	"""The axis of the circle through three directions (the mount's polar
	axis), pointing at the visible pole."""
	a, b, c = vectors
	u = [b[i] - a[i] for i in range(3)]
	v = [c[i] - b[i] for i in range(3)]
	n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
	norm = math.sqrt(sum(x * x for x in n))
	if norm < 1e-9:
		raise SolveJobError("The three solves were too close together to find the axis")
	n = tuple(x / norm for x in n)
	if (n[2] < 0) != south:
		n = tuple(-x for x in n)
	return n


def pole_error(pole: Tuple[float, float, float], lat: float) -> Tuple[float, float]:
	"""How far the mount's pole is from the true one: (up, east) in
	arcminutes on the sky, for the altitude and azimuth adjusters."""
	from utils.astrometry import hadec_to_altaz

	ha = math.atan2(pole[1], pole[0]) / DEG
	dec = math.asin(max(-1.0, min(1.0, pole[2]))) / DEG
	alt, az = hadec_to_altaz(ha, dec, lat)
	south = lat < 0
	true_alt, true_az = abs(lat), (180.0 if south else 0.0)
	up = (alt - true_alt) * 60.0
	daz = ((az - true_az + 180.0) % 360.0) - 180.0
	east = daz * math.cos(true_alt * DEG) * 60.0  # azimuth grows towards the east
	return up, east


def polar_report(up_arcmin: float, east_arcmin: float) -> str:
	parts = []
	if abs(up_arcmin) >= 0.5:
		parts.append(f"{'lower' if up_arcmin > 0 else 'raise'} the altitude {abs(up_arcmin):.0f}'")
	if abs(east_arcmin) >= 0.5:
		parts.append(f"turn the azimuth {'west' if east_arcmin > 0 else 'east'} {abs(east_arcmin):.0f}'")
	total = math.hypot(up_arcmin, east_arcmin)
	if not parts:
		return f"Polar alignment within {max(total, 0.1):.1f}'"
	return f"{total:.0f}' off: " + ", ".join(parts)


def polar_align(step_deg: float = POLAR_STEP_DEG, source: str = "auto", progress=None) -> Dict[str, Any]:
	"""Three solves POLAR_STEP_DEG of RA apart (RA turns twice: keep the
	path clear). Tracking stops for it. progress(text) is told each step."""
	from core.hardware import tracking
	from utils import clock

	site = tracking._site()
	if site is None:
		raise SolveJobError("No site set")
	_solver()
	tracking.stop_tracking()
	config = tracking.mount_config()
	direction = 1.0
	axes0 = tracking.get_mount_axes()
	if axes0 is not None and not config.within((axes0[0] + 2 * step_deg, axes0[1])):
		direction = -1.0  # turn the other way to stay inside the limits
	vectors = []
	solves = []
	for k in range(3):
		if progress:
			progress(f"Solving {k + 1} of 3")
		result = solve_now(source)
		vectors.append(_hadec_vector(result.ra, result.dec, clock.now()))
		solves.append(result.to_dict())
		_record(result, name=f"polar {k + 1}")
		if k < 2:
			if progress:
				progress(f"Turning RA {step_deg:.0f} deg")
			_turn_ra(direction * step_deg, config)
	pole = pole_from_points(vectors, south=site[0] < 0)
	up, east = pole_error(pole, site[0])
	report = polar_report(up, east)
	log.info("Polar alignment: %s", report)
	return {"up_arcmin": round(up, 1), "east_arcmin": round(east, 1), "report": report, "solves": solves}


def _turn_ra(degrees: float, config) -> None:
	from core.hardware import tracking

	start = tracking.get_mount_axes() or (0.0, 0.0)
	result = tracking.move_to_axes((start[0] + degrees, start[1]))
	if "error" in result:
		raise SolveJobError(result["error"])
	time.sleep(1.0)  # let it settle before the next picture
