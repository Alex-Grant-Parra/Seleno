"""Ephemeris, precession, refraction, the pointing model, polar alignment,
the southern mount, backlash and the horizon. Reference positions are
from JPL's DE421 via Skyfield (geocentric astrometric J2000)."""
from __future__ import annotations

import math
import random
import unittest
from datetime import datetime, timezone

from tests.helpers import ScratchConfig

from core.hardware import mount
from core.hardware.pointing import AlignmentPoint, PointingModel, fit, polar_report
from utils import astrometry, ephemeris, horizon
from utils.sky_view import angular_distance

WHEN = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)
SITE = (51.4623, -2.5502)
DE421 = {
	"Sun": (201.4543, -9.0090), "Moon": (274.6018, -27.4831), "Mercury": (224.2189, -20.1050),
	"Venus": (209.6313, -19.9751), "Mars": (133.1336, 18.9066), "Jupiter": (144.4538, 14.8231),
	"Saturn": (10.2150, 1.4510), "Uranus": (62.8621, 20.9393), "Neptune": (2.4492, -0.4853),
}
DE421_TOPO_MOON = (273.9769, -28.1560)
DEG = math.pi / 180


class EphemerisTests(unittest.TestCase):
	def test_against_de421(self) -> None:
		for body, (ra, dec) in DE421.items():
			mine = ephemeris.position(body, WHEN)
			self.assertLess(angular_distance(mine[0], mine[1], ra, dec) * 60, 2.0, body)

	def test_the_moon_from_the_site(self) -> None:
		mine = ephemeris.position("Moon", WHEN, SITE)
		self.assertLess(angular_distance(mine[0], mine[1], *DE421_TOPO_MOON) * 60, 5.0)
		geo = ephemeris.position("Moon", WHEN)
		self.assertGreater(angular_distance(*mine, *geo), 0.3)  # the parallax is real

	def test_names_and_rates(self) -> None:
		self.assertEqual(ephemeris.find("mOoN"), "Moon")
		self.assertIsNone(ephemeris.find("Vega"))
		dra, ddec = ephemeris.rates("Moon", WHEN)
		self.assertTrue(0.3 / 3600 < dra < 0.8 / 3600)  # about half an arcsecond a second, eastwards
		self.assertLess(abs(ddec), 0.3 / 3600)
		moon = [p for p in ephemeris.positions(WHEN, SITE) if p["name"] == "Moon"][0]
		self.assertTrue(0.0 <= moon["moon_illumination_fraction"] <= 1.0)


class AstrometryTests(unittest.TestCase):
	def test_precession_of_vega(self) -> None:
		# DE421/Skyfield apparent place of date (with nutation and aberration, ~20").
		ra, dec = astrometry.j2000_to_date(279.23473, 38.78369, WHEN)
		self.assertLess(angular_distance(ra, dec, 279.46077, 38.80588) * 60, 1.0)
		back = astrometry.date_to_j2000(ra, dec, WHEN)
		self.assertLess(angular_distance(*back, 279.23473, 38.78369) * 3600, 0.01)

	def test_refraction(self) -> None:
		self.assertAlmostEqual(astrometry.refraction_deg(0.0) * 60, 29.0, delta=1.5)
		self.assertAlmostEqual(astrometry.refraction_deg(10.0) * 60, 5.3, delta=0.3)
		self.assertAlmostEqual(astrometry.refraction_deg(45.0) * 60, 1.0, delta=0.1)
		self.assertEqual(astrometry.refraction_deg(-5.0), 0.0)

	def test_apparent_and_back(self) -> None:
		for ra, dec in ((10.0, 20.0), (200.0, -10.0), (300.0, 80.0)):
			lst = 150.0
			ha, d = astrometry.j2000_to_apparent_hadec(ra, dec, lst, SITE[0], WHEN)
			back = astrometry.apparent_hadec_to_j2000(ha, d, lst, SITE[0], WHEN)
			self.assertLess(angular_distance(*back, ra, dec) * 3600, 0.5)


class PointingTests(unittest.TestCase):
	TRUTH = PointingModel(ih=0.8, id=-0.5, alt=0.4, az=-0.25, ch=0.12, np=0.05)

	def points(self, n: int, seed: int = 3):
		rng = random.Random(seed)
		out = []
		for i in range(n):
			a, d = rng.uniform(-80, 80), rng.uniform(-30, 70)
			if i % 2:
				d = 180 - d
			out.append(AlignmentPoint((a, d), self.TRUTH.sky((a, d))))
		return out

	def test_one_point_is_an_offset_and_six_find_everything(self) -> None:
		one = fit(self.points(1))
		self.assertEqual((one.alt, one.az, one.ch), (0.0, 0.0, 0.0))
		self.assertAlmostEqual(one.rms_arcmin, 0.0, places=6)
		model = fit(self.points(8))
		for term in ("ih", "id", "alt", "az", "ch", "np"):
			self.assertAlmostEqual(getattr(model, term), getattr(self.TRUTH, term), places=3, msg=term)
		self.assertIn("down 24'", polar_report(model))
		self.assertIn("east 15'", polar_report(model))

	def test_the_model_steers_the_axes(self) -> None:
		config = mount.MountConfig()
		target = (30.0, 40.0)
		axes = mount.nearest_solution(*target, (0.0, 0.0), config, model=self.TRUTH.sky)
		self.assertLess(mount.separation(self.TRUTH.sky(axes), target) * 3600, 1.0)


class PolarAlignTests(unittest.TestCase):
	def test_three_points_find_the_mount_pole(self) -> None:
		from utils.plate_solve import pole_error, pole_from_points

		for lat, up, east in ((51.46, 20.0, -12.0), (-33.9, -8.0, 15.0)):
			true_alt, true_az = abs(lat), (180.0 if lat < 0 else 0.0)
			ha, dec = astrometry.altaz_to_hadec(true_alt + up / 60, true_az + east / 60 / math.cos(true_alt * DEG), lat)
			p = (math.cos(dec * DEG) * math.cos(ha * DEG), math.cos(dec * DEG) * math.sin(ha * DEG), math.sin(dec * DEG))

			def turn(v, t):
				c, s = math.cos(t), math.sin(t)
				d = sum(a * b for a, b in zip(p, v))
				cx = (p[1] * v[2] - p[2] * v[1], p[2] * v[0] - p[0] * v[2], p[0] * v[1] - p[1] * v[0])
				return tuple(v[i] * c + cx[i] * s + p[i] * d * (1 - c) for i in range(3))
			v0 = (0.6, 0.3, 0.74 if lat > 0 else -0.74)
			n = math.sqrt(sum(x * x for x in v0))
			v0 = tuple(x / n for x in v0)
			got = pole_error(pole_from_points([turn(v0, math.radians(a)) for a in (0, 30, 60)], south=lat < 0), lat)
			self.assertAlmostEqual(got[0], up, places=2)
			self.assertAlmostEqual(got[1], east, places=2)


class SouthernMountTests(unittest.TestCase):
	def test_mirrored_round_trip_and_directions(self) -> None:
		south = mount.MountConfig(south=True)
		for ha, dec in ((-30.0, -40.0), (100.0, -80.0), (20.0, 10.0)):
			for axes in mount.axis_solutions(ha, dec, south):
				back = mount.sky_from_axes(*axes, south)
				self.assertLess(mount.separation(back, (ha, dec)), 1e-6)
		self.assertEqual(mount.tracking_direction(south), -1)
		self.assertEqual(mount.tracking_direction(mount.MountConfig()), 1)
		# The home position points at the visible pole either way.
		self.assertAlmostEqual(mount.sky_from_axes(0.0, 90.0, south)[1], -90.0)
		self.assertAlmostEqual(mount.sky_from_axes(0.0, 90.0, mount.MountConfig())[1], 90.0)

	def test_axis_rates_follow_the_side_and_hemisphere(self) -> None:
		north, south = mount.MountConfig(), mount.MountConfig(south=True)
		self.assertEqual(mount.axis_rates(1.0, 0.5, 30.0, north), (1.0, 0.5))
		self.assertEqual(mount.axis_rates(1.0, 0.5, 120.0, north), (1.0, -0.5))  # flipped
		self.assertEqual(mount.axis_rates(1.0, 0.5, 30.0, south), (-1.0, -0.5))


class BacklashTests(unittest.TestCase):
	def test_reversals_take_up_the_slack(self) -> None:
		from core.hardware.tracking import with_backlash

		steps = [(100, 50), (200, -20), (150, -40)]
		out, takeup, last = with_backlash(steps, backlash=(10, 5), last_dir={"motor1": 1, "motor2": -1})
		# motor1: forward (same as before), forward, then back: +-10 from the third point.
		# motor2: forward (reversed: +5 from the start), back (reversed again: -5 from point two).
		self.assertEqual(out, [(100, 55), (200, -15 - 5), (150 - 10, -40)])
		self.assertEqual(takeup, {"motor1": -10, "motor2": 0})
		self.assertEqual(last, {"motor1": -1, "motor2": -1})
		plain, none, _ = with_backlash(steps, backlash=(0, 0), last_dir={"motor1": 1, "motor2": -1})
		self.assertEqual((plain, none), (steps, {"motor1": 0, "motor2": 0}))


class HorizonTests(ScratchConfig):
	def test_profile_and_minimum(self) -> None:
		from utils.config_state import save_section

		self.assertEqual(horizon.profile_limit([], 10.0), None)
		profile = [[0, 10], [90, 30], [180, 10]]
		self.assertAlmostEqual(horizon.profile_limit(profile, 45.0), 20.0)
		self.assertAlmostEqual(horizon.profile_limit(profile, 270.0), 10.0)  # 180 -> 360 (= 0)
		save_section("horizon", {"min_alt_deg": 15.0, "profile": profile})
		self.assertEqual(horizon.limit_at(0.0), 15.0)
		self.assertAlmostEqual(horizon.limit_at(90.0), 30.0)


if __name__ == "__main__":
	unittest.main()
