"""Plate solving: where in the sky a picture was taken, from its stars.

Runs ASTAP's command-line solver (astap_cli, ARM64 build) with a star
database beside it (D05 for fields under 20 degrees, W08 for wider). The
solver is found from config solver.path, else ~/astap/astap_cli, else the
PATH; without it, available() is False and everything that would solve
says so.

A hint (where the telescope thinks it points) makes it quick: the search
spirals out from there up to solver.search_radius_deg. The field size is
learnt from the first solve (solver.fov_deg = 0) and kept for the next.

The answer is the picture's centre in J2000, the scale and the rotation.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, Optional

from utils.log import get_logger

log = get_logger("solver")
DEG = math.pi / 180.0


class SolveError(RuntimeError):
	pass


@dataclass
class SolveResult:
	ra: float               # J2000, degrees, at the picture's centre
	dec: float
	scale_arcsec: float     # per pixel
	rotation_deg: float
	width: int
	height: int
	seconds: float

	@property
	def fov_deg(self) -> float:
		"""The picture's height in degrees (what ASTAP's -fov means)."""
		return self.height * self.scale_arcsec / 3600.0

	def to_dict(self) -> Dict[str, float]:
		return {"ra": round(self.ra, 5), "dec": round(self.dec, 5), "scale_arcsec": round(self.scale_arcsec, 3),
			"rotation_deg": round(self.rotation_deg, 2), "fov_deg": round(self.fov_deg, 3),
			"seconds": round(self.seconds, 1)}


def _settings() -> Dict[str, object]:
	try:
		from utils.config_state import get_section

		return get_section("solver")
	except Exception:
		return {}


def find_astap() -> Optional[str]:
	configured = str(_settings().get("path") or "").strip()
	candidates = [configured] if configured else []
	candidates += [os.path.expanduser("~/astap/astap_cli"), shutil.which("astap_cli") or "", shutil.which("astap") or ""]
	for path in candidates:
		if path and os.path.isfile(path) and os.access(path, os.X_OK):
			return path
	return None


def available() -> bool:
	return find_astap() is not None


def _databases(folder: str) -> Dict[str, bool]:
	try:
		names = os.listdir(folder)
	except OSError:
		return {}
	return {prefix: any(n.lower().startswith(prefix + "_") for n in names) for prefix in ("d05", "d20", "d50", "d80", "w08", "v50", "g05")}


def _read_ini(path: str) -> Dict[str, str]:
	out: Dict[str, str] = {}
	try:
		with open(path, errors="replace") as f:
			for line in f:
				key, sep, value = line.strip().partition("=")
				if sep:
					out[key.strip().upper()] = value.strip()
	except OSError:
		pass
	return out


def _tan_inverse(ra0: float, dec0: float, xi: float, eta: float):
	"""Gnomonic: offsets (degrees on the tangent plane) to RA/Dec."""
	x, y = xi * DEG, eta * DEG
	a0, d0 = ra0 * DEG, dec0 * DEG
	denom = math.cos(d0) - y * math.sin(d0)
	ra = a0 + math.atan2(x, denom)
	dec = math.atan2(math.sin(d0) + y * math.cos(d0), math.hypot(x, denom))
	return (ra / DEG) % 360.0, dec / DEG


def _image_size(path: str):
	from PIL import Image

	with Image.open(path) as im:
		return im.size


def solve(image_path: str, ra_hint: Optional[float] = None, dec_hint: Optional[float] = None,
		fov_deg: Optional[float] = None, radius_deg: Optional[float] = None,
		timeout_s: Optional[float] = None) -> SolveResult:
	"""Solve one picture (JPEG, PNG, TIFF or FITS). Hints are J2000 degrees."""
	astap = find_astap()
	if astap is None:
		raise SolveError("No plate solver: install ASTAP (astap_cli) in ~/astap")
	settings = _settings()
	fov = float(fov_deg if fov_deg is not None else settings.get("fov_deg", 0.0) or 0.0)
	radius = float(radius_deg if radius_deg is not None else settings.get("search_radius_deg", 15.0) or 15.0)
	timeout = float(timeout_s if timeout_s is not None else settings.get("timeout_s", 60.0) or 60.0)
	width, height = _image_size(image_path)
	folder = os.path.dirname(astap)
	dbs = _databases(folder)
	with tempfile.TemporaryDirectory(prefix="solve-") as work:
		out_base = os.path.join(work, "solution")
		args = [astap, "-f", image_path, "-o", out_base, "-r", f"{radius:.1f}", "-fov", f"{fov:.3f}", "-z", "0"]
		if any(dbs.values()):
			args += ["-d", folder]
			if fov >= 20.0 and dbs.get("w08"):
				args += ["-D", "w08"]
			elif 0 < fov < 20.0 and dbs.get("d05"):
				args += ["-D", "d05"]
		if ra_hint is not None and dec_hint is not None:
			args += ["-ra", f"{(ra_hint % 360.0) / 15.0:.5f}", "-spd", f"{dec_hint + 90.0:.5f}"]
		else:
			args[args.index("-r") + 1] = "180"  # no idea where: the whole sky (slow)
		started = time.monotonic()
		try:
			subprocess.run(args, capture_output=True, text=True, timeout=timeout)
		except subprocess.TimeoutExpired as exc:
			raise SolveError(f"No solution within {timeout:.0f} s") from exc
		except OSError as exc:
			raise SolveError(f"Couldn't run ASTAP: {exc}") from exc
		seconds = time.monotonic() - started
		ini = _read_ini(out_base + ".ini")
	if ini.get("PLTSOLVD", "F").upper() != "T":
		reason = ini.get("ERROR") or ini.get("WARNING") or "no match"
		raise SolveError(f"Not solved: {reason}"[:120])
	try:
		ra0, dec0 = float(ini["CRVAL1"]), float(ini["CRVAL2"])
		crpix1 = float(ini.get("CRPIX1", (width + 1) / 2.0))
		crpix2 = float(ini.get("CRPIX2", (height + 1) / 2.0))
		cd11 = float(ini.get("CD1_1", ini.get("CDELT1", 0.0)))
		cd12 = float(ini.get("CD1_2", 0.0))
		cd21 = float(ini.get("CD2_1", 0.0))
		cd22 = float(ini.get("CD2_2", ini.get("CDELT2", 0.0)))
	except (KeyError, ValueError) as exc:
		raise SolveError("The solver's answer couldn't be read") from exc
	# The picture's centre (FITS pixels count from 1).
	dx, dy = (width + 1) / 2.0 - crpix1, (height + 1) / 2.0 - crpix2
	xi, eta = cd11 * dx + cd12 * dy, cd21 * dx + cd22 * dy
	ra, dec = _tan_inverse(ra0, dec0, xi, eta)
	scale = math.sqrt(abs(cd11 * cd22 - cd12 * cd21)) * 3600.0
	rotation = float(ini.get("CROTA2", math.degrees(math.atan2(cd21, cd22)) if cd22 or cd21 else 0.0))
	result = SolveResult(ra, dec, scale, rotation, width, height, seconds)
	log.info("Solved in %.1f s: RA %.4f, Dec %.4f, %.2f arcsec/px", seconds, ra, dec, scale)
	if not settings.get("fov_deg"):
		try:
			from utils.config_state import save_section

			save_section("solver", {"fov_deg": round(result.fov_deg, 3)})
		except Exception:
			pass
	return result
