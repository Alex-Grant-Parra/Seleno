"""The Pi side of the front-panel UI: sky view maths and SKY1 encoding, and
utils/panel_ui.py against a fake connection. No hardware or network.

Run from the repo root:  ./venv/bin/python -m unittest esp32.tests.test_panel_ui -v
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import utils.config_state as config_state  # noqa: E402
from utils import sky_view  # noqa: E402
from utils.sky_view import (  # noqa: E402
	PEN_UP,
	SkyOptions,
	ascii_text,
	build_scene,
	lst_degrees,
	radec_to_altaz,
)

_hardware_patches: List[Any] = []


def setUpModule() -> None:
	# No serial ports: the LED manager and tracking would otherwise go looking
	# for a real ESP32 (and find the dev kit).
	from unittest import mock

	from utils.esp32_state import esp32_state

	_hardware_patches[:] = [
		mock.patch.object(esp32_state, "ensure_connection", return_value=None),
		mock.patch("utils.LEDmanager.get_led_manager", return_value=mock.MagicMock()),
	]
	for patch in _hardware_patches:
		patch.start()


def tearDownModule() -> None:
	for patch in _hardware_patches:
		patch.stop()


def sun_at(ra: float, dec: float) -> list:
	"""Patches putting the Sun at (ra, dec) for the Sun guard and for
	tracking's route planner."""
	from unittest import mock

	return [mock.patch("utils.sun_guard.sun_radec", return_value=(ra, dec)),
		mock.patch("core.hardware.tracking.sun_radec", return_value=(ra, dec))]


LAT, LON = 51.4623, -2.5502
WHEN = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)


def decode_sky1(data: bytes) -> Dict[str, Any]:
	"""Parse SKY1 the way ui_sky.cpp does, so the two stay in step."""
	assert data[:4] == b"SKY1"
	off = 4
	(w, h, flags, _r, tx, ty, s_alt, s_az, t_alt, t_az, dist, fov) = struct.unpack_from("<HHBBhhhHhHHH", data, off)
	off += struct.calcsize("<HHBBhhhHhHHH")
	(n,) = struct.unpack_from("<H", data, off)
	off += 2
	stars = [struct.unpack_from("<hhBB", data, off + 6 * i) for i in range(n)]
	off += 6 * n
	(nl,) = struct.unpack_from("<B", data, off)
	off += 1
	layers = []
	for _ in range(nl):
		style, npts = struct.unpack_from("<BH", data, off)
		off += 3
		pts = list(struct.unpack_from(f"<{2 * npts}h", data, off))
		off += 4 * npts
		layers.append((style, pts))
	step, ncols = struct.unpack_from("<BH", data, off)
	off += 3
	horizon = list(struct.unpack_from(f"<{ncols}h", data, off))
	off += 2 * ncols
	(nlab,) = struct.unpack_from("<B", data, off)
	off += 1
	labels = []
	for _ in range(nlab):
		x, y, style, ln = struct.unpack_from("<hhBB", data, off)
		off += 6
		labels.append((x, y, style, data[off:off + ln].decode()))
		off += ln
	(npl,) = struct.unpack_from("<B", data, off)
	off += 1
	planets = []
	for _ in range(npl):
		x, y, kind, r, illum, limb, ln = struct.unpack_from("<hhBBBBB", data, off)
		off += 9
		planets.append((x, y, kind, r, illum, limb, data[off:off + ln].decode()))
		off += ln
	assert off == len(data), f"{len(data) - off} trailing bytes"
	return dict(w=w, h=h, flags=flags, target=(tx, ty), scope=(s_alt, s_az), dist=dist, fov=fov,
		stars=stars, layers=layers, step=step, horizon=horizon, labels=labels, planets=planets)


def radec_at(alt: float, az: float) -> tuple:
	"""RA/Dec of an alt/az at WHEN, for pointing tests."""
	lst = lst_degrees(WHEN, LON)
	m = sky_view.eq_to_horizon_matrix(LAT, lst)
	v = sky_view.altaz_to_xyz(alt, az)
	eq = tuple(m[0][i] * v[0] + m[1][i] * v[1] + m[2][i] * v[2] for i in range(3))
	return math.degrees(math.atan2(eq[2], eq[0])) % 360.0, math.degrees(math.asin(eq[1]))


STARS = [(279.23, 38.78, 0.03, "HD172167"), (297.70, 8.87, 0.77, "HD187642"), (310.36, 45.28, 1.25, "HD197345")]
NAMES = {"HD172167": "Vega", "HD187642": "Altair", "HD197345": "Deneb"}


class SkyViewTests(unittest.TestCase):
	def test_polaris_altitude_is_the_latitude(self) -> None:
		alt, _ = radec_to_altaz(37.95, 89.26, lst_degrees(WHEN, LON), LAT)
		self.assertAlmostEqual(alt, LAT, delta=0.8)

	def test_scope_direction_lands_in_the_middle(self) -> None:
		ra, dec = radec_at(40.0, 200.0)
		scene = build_scene(128, 136, STARS, NAMES, [], [], (ra, dec), (ra, dec), LAT, LON, WHEN)
		self.assertEqual(scene.target, (64, 68))
		self.assertAlmostEqual(scene.scope_altaz[0], 40.0, places=3)
		self.assertAlmostEqual(scene.distance_deg, 0.0, places=6)

	def test_field_width_spans_the_screen(self) -> None:
		ra, dec = radec_at(30.0, 180.0)
		east_edge = radec_at(30.0, 180.0 - 20.0 / math.cos(math.radians(30.0)))
		scene = build_scene(128, 136, [], {}, [], [], (ra, dec), east_edge, LAT, LON, WHEN,
			SkyOptions(fov_deg=40.0, horizon_grid=False, below_horizon=False))
		# 20 deg to the side of a 40 deg field: near an edge, level with the centre
		self.assertLess(abs(abs(scene.target[0] - 64) - 64), 6)

	def test_scene_round_trips_and_looks_sane(self) -> None:
		ra, dec = radec_at(45.0, 270.0)
		planets = [{"name": "Moon", "ra": ra + 2, "dec": dec, "moon_illumination_fraction": 0.3},
			{"name": "Sun", "ra": ra + 120, "dec": dec - 10, "mag": -26.7}]
		figures = [{"id": "Tst", "name": "Testus", "anchor": [ra - 3, dec + 3],
			"lines": [[[ra - 5, dec + 5], [ra + 5, dec + 5]]]}]
		data = build_scene(128, 136, STARS, NAMES, figures, planets, (ra, dec), (ra + 1, dec), LAT, LON, WHEN,
			SkyOptions(equatorial_grid=True)).encode()
		d = decode_sky1(data)
		self.assertEqual((d["w"], d["h"]), (128, 136))
		self.assertEqual(d["flags"] & 1, 1)
		self.assertTrue(d["layers"], "grids and the figure are drawn")
		self.assertEqual(len(d["horizon"]), 64)
		moon = [p for p in d["planets"] if p[6] == "Moon"]
		self.assertEqual(len(moon), 1)
		self.assertEqual(moon[0][4], 30)  # illumination %
		for style, pts in d["layers"]:
			self.assertEqual(len(pts) % 2, 0)
			for x, y in zip(pts[::2], pts[1::2]):
				if x != PEN_UP:
					self.assertTrue(-200 < x < 330 and -200 < y < 340, (style, x, y))

	def test_horizon_tint(self) -> None:
		level = build_scene(128, 136, [], {}, [], [], radec_at(0.0, 180.0), None, LAT, LON, WHEN)
		self.assertTrue(all(60 <= y <= 76 for y in level.horizon), level.horizon)
		high = build_scene(128, 136, [], {}, [], [], radec_at(60.0, 180.0), None, LAT, LON, WHEN)
		self.assertTrue(all(y == 136 for y in high.horizon))

	def test_labels_keep_clear_of_the_reticle_and_keepouts(self) -> None:
		ra, dec = 279.23, 38.78  # Vega in the middle
		scene = build_scene(128, 136, STARS, NAMES, [], [], (ra, dec), None, LAT, LON, WHEN,
			SkyOptions(fov_deg=70.0), keepouts=[(0, 95, 41, 41)])
		for x, y, style, text in scene.labels:
			grid = sky_view._LabelGrid(128, 136, [(0, 95, 41, 41)])
			left = x if style != sky_view.LABEL_CONSTELLATION else x - 3 * len(text)
			self.assertTrue(grid.clear_of_reticle(left, text, y), (x, y, text))
		self.assertNotIn("Vega", [lab[3] for lab in scene.labels])

	def test_labels_never_overlap(self) -> None:
		from utils.sky_data import SkyData

		data = SkyData()
		data._load_cache()  # the cached catalogue only: no network in tests
		if not data.ready():
			self.skipTest("no cached sky data")
		stars, names, figures, _planets = data.snapshot()
		for alt, az in ((60, 200), (30, 90), (75, 0), (20, 300)):
			for fov in (20.0, 40.0, 70.0):
				ra, dec = radec_at(alt, az)
				scene = build_scene(128, 136, stars, names, figures, [], (ra, dec), None, LAT, LON, WHEN,
					SkyOptions(fov_deg=fov, max_star_labels=12))
				boxes = []
				for x, y, style, text in scene.labels:
					left = x - 3 * len(text) if style in (sky_view.LABEL_CONSTELLATION, sky_view.LABEL_CARDINAL) else x
					boxes.append((left, y - 4, left + 6 * len(text), y + 4, text))
				for i, a in enumerate(boxes):
					for b in boxes[i + 1:]:
						self.assertFalse(a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1],
							f"{a[4]} overlaps {b[4]} at alt {alt} az {az} fov {fov}")

	def test_ascii_names(self) -> None:
		self.assertEqual(ascii_text("Boötes", 40), b"Bootes")


class FakeConn:
	"""Just enough of ESP32Connection for PanelUI."""

	def __init__(self) -> None:
		self.calls: List[Dict[str, Any]] = []
		self.listeners: List[tuple] = []
		self.scenes: List[bytes] = []
		self.reply: Callable[[str, Dict[str, Any]], Dict[str, Any]] = lambda action, fields: {}

	def ui(self, action: str, timeout: Optional[float] = None, **fields: Any) -> Dict[str, Any]:
		self.calls.append({"action": action, **fields})
		return self.reply(action, fields)

	def ui_sky(self, scene: bytes) -> Dict[str, Any]:
		self.scenes.append(scene)
		return {}

	def add_event_listener(self, callback, event=None) -> int:
		self.listeners.append((event, callback))
		return len(self.listeners)

	def emit(self, event: Dict[str, Any]) -> None:
		for name, callback in list(self.listeners):
			if name is None or name == event.get("event"):
				callback(event)

	def actions(self, name: str) -> List[Dict[str, Any]]:
		return [c for c in self.calls if c["action"] == name]


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.02)
	return False


class PanelUIBase(unittest.TestCase):
	profile: Dict[str, Any] = {"client_config": {"client_id": "scope", "base_url": "seleno.org",
		"api_token": "0123456789abcdef"}}

	def setUp(self) -> None:
		# Never touch the real config files.
		self.tmp = tempfile.TemporaryDirectory()
		self.saved = (config_state.CLIENT_PROFILE_FILE, config_state.STATIC_STATE_FILE,
			config_state.RUNTIME_STATE_FILE, config_state._STATE_READY)
		# Write all three files before switching to them: threads left over
		# from earlier tests still read the config, and a read that finds a
		# file missing rebuilds it (a blank profile).
		files = {
			"CLIENT_PROFILE_FILE": ("client_profile.json",
				config_state._merge_defaults(config_state._default_client_profile(), self.profile)),
			"STATIC_STATE_FILE": ("client_config.json", config_state._default_static_state()),
			"RUNTIME_STATE_FILE": ("runtime_state.json", config_state._default_runtime_state()),
		}
		for attr, (name, content) in files.items():
			path = os.path.join(self.tmp.name, name)
			config_state._save_json(path, content)
			setattr(config_state, attr, path)
		config_state._STATE_READY = True
		from utils import panel_ui, telescope_state

		telescope_state._STATE_CACHE = None

		self.panel_ui = panel_ui
		self.ui = panel_ui.PanelUI()
		from unittest import mock

		self._ui_patch = mock.patch.object(panel_ui, "get_panel_ui", return_value=self.ui)
		self._ui_patch.start()
		self.conn = FakeConn()
		self.conn.reply = lambda action, fields: (
			{"width": 128, "height": 160, "sky_w": 128, "sky_h": 136} if action in ("hello", "status") else {})
		self.ui.attach(self.conn)
		self.assertTrue(self.ui.attached.wait(3))

	def tearDown(self) -> None:
		self._ui_patch.stop()
		(config_state.CLIENT_PROFILE_FILE, config_state.STATIC_STATE_FILE,
			config_state.RUNTIME_STATE_FILE, config_state._STATE_READY) = self.saved
		self.tmp.cleanup()

	def answer(self, title: str, **reply: Any) -> Dict[str, Any]:
		"""Wait for the next unanswered prompt, check its title and answer it."""
		answered = getattr(self, "_answered", 0)
		self.assertTrue(wait_for(lambda: len(self.conn.actions("prompt")) > answered),
			f"no prompt after {[p['title'] for p in self.conn.actions('prompt')]}")
		prompt = self.conn.actions("prompt")[answered]
		self._answered = answered + 1
		self.assertEqual(prompt["title"], title)
		if prompt["kind"] != "busy":
			self.conn.emit({"event": "ui", "type": "prompt", "prompt": prompt["prompt"], **reply})
		return prompt


class PanelUITests(PanelUIBase):
	def test_hello_sends_the_pi_tabs_with_values(self) -> None:
		hello = self.conn.actions("hello")[0]
		self.assertEqual(hello["device"], "scope")
		panels = {p["id"]: p for p in hello["panels"]}
		self.assertEqual({"scope", "camera", "sky", "setup", "pi"}, set(panels))
		setup = {i["id"]: i for i in panels["setup"]["items"]}
		self.assertEqual(setup["base_url"]["value"], "seleno.org")
		self.assertTrue(setup["api_token"]["secret"])
		json.dumps(hello)  # all of it must be JSON

	def test_a_change_runs_the_setter_and_confirms_the_value(self) -> None:
		self.conn.emit({"event": "ui", "type": "change", "panel": "sky", "item": "ecliptic", "value": False})
		self.assertTrue(wait_for(lambda: not self.ui.sky_options.ecliptic))
		self.assertTrue(wait_for(lambda: any(c.get("values") == {"sky.ecliptic": False} for c in self.conn.actions("set"))))
		saved = config_state.load_static_state()["sky_view"]
		self.assertFalse(saved["ecliptic"])

	def test_a_bad_setting_is_refused_and_snaps_back(self) -> None:
		self.conn.emit({"event": "ui", "type": "change", "panel": "setup", "item": "api_token", "value": "short"})
		self.assertTrue(wait_for(lambda: any(c["action"] == "toast" and "short" in c["text"] for c in self.conn.calls)))
		self.assertEqual(config_state.get_client_config()["api_token"], "0123456789abcdef")
		self.assertTrue(wait_for(lambda: any(
			c.get("values") == {"setup.api_token": "0123456789abcdef"} for c in self.conn.actions("set"))))

	def test_a_good_setting_is_saved(self) -> None:
		self.conn.emit({"event": "ui", "type": "change", "panel": "setup", "item": "client_id", "value": "Big Dob"})
		self.assertTrue(wait_for(lambda: config_state.get_client_config()["client_id"] == "Big Dob"))

	def test_panel_stop_stops_tracking_once_and_acknowledges(self) -> None:
		import core.hardware.tracking as tracking

		stops = []
		original = tracking.stop_tracking
		tracking.stop_tracking = lambda *a, **k: (stops.append(1), time.sleep(0.2))
		try:
			for _ in range(3):  # the event, hello and the heartbeat may all report it
				self.conn.emit({"event": "ui", "type": "stop", "reason": "return_double"})
			self.assertTrue(wait_for(lambda: len(self.conn.actions("ack_stop")) >= 1))
			time.sleep(0.3)
		finally:
			tracking.stop_tracking = original
		self.assertEqual(len(stops), 1)
		self.assertEqual(len(self.conn.actions("ack_stop")), 1)

	def test_prompts_return_the_answer_or_raise_when_cancelled(self) -> None:
		answers: List[Any] = []

		def ask() -> None:
			try:
				answers.append(self.ui.prompt("text", "Name", value="x")["value"])
			except self.panel_ui.PromptCancelled:
				answers.append("cancelled")

		for reply in ({"value": "Orion"}, {"cancelled": True}):
			t = threading.Thread(target=ask)
			t.start()
			self.assertTrue(wait_for(lambda: len(self.conn.actions("prompt")) > len(answers)))
			prompt_id = self.conn.actions("prompt")[-1]["prompt"]
			self.conn.emit({"event": "ui", "type": "prompt", "prompt": prompt_id, **reply})
			t.join(2)
		self.assertEqual(answers, ["Orion", "cancelled"])

	def test_zoom_steps_the_field(self) -> None:
		self.ui.sky_options.fov_deg = 40.0
		self.conn.emit({"event": "ui", "type": "sky", "zoom": 1})
		self.assertTrue(wait_for(lambda: self.ui.sky_options.fov_deg == 20.0))
		self.conn.emit({"event": "ui", "type": "sky", "zoom": -1})
		self.assertTrue(wait_for(lambda: self.ui.sky_options.fov_deg == 40.0))

	def test_tracking_updates_reach_the_panel(self) -> None:
		self.ui.tracking_update(state="slewing", target="M31", ra=10.68, dec=41.27)
		self.assertTrue(wait_for(lambda: any(c.get("target") == "M31" for c in self.conn.actions("tracking"))))
		self.ui.tracking_update(state="idle")
		self.assertTrue(wait_for(lambda: self.conn.actions("tracking")[-1] == {"action": "tracking", "state": "idle"}))


class ManualControlTests(unittest.TestCase):
	def test_ra_and_dec_in_the_usual_forms(self) -> None:
		from utils.manual_control import format_dec, format_ra, parse_dec, parse_ra

		for text in ("5 35 17", "5:35:17", "5h35m17s", "5.58806", "83.8208d"):
			self.assertAlmostEqual(parse_ra(text), 83.8208, places=3, msg=text)
		for text in ("-5 23 28", "-5:23:28", "-5d23m28s", "-5.39111"):
			self.assertAlmostEqual(parse_dec(text), -5.39111, places=4, msg=text)
		self.assertAlmostEqual(parse_dec("-0 30"), -0.5)
		self.assertEqual(format_ra(parse_ra("5 35 17")), "5h35m17s")
		self.assertEqual(format_dec(parse_dec("-5 23 28")), "-5d23m28s")
		self.assertEqual(format_ra(359.9999), "0h00m00s")
		for bad in ("24", "5 61", "", "abc", "-1", "400d"):
			with self.assertRaises(ValueError, msg=bad):
				parse_ra(bad)
		for bad in ("91", "5 -3", "", "x"):
			with self.assertRaises(ValueError, msg=bad):
				parse_dec(bad)

	def test_site(self) -> None:
		from utils.manual_control import parse_site

		self.assertEqual(parse_site("51.48, -2.55"), (51.48, -2.55))
		self.assertEqual(parse_site("33.9S 18.4E"), (-33.9, 18.4))
		for bad in ("51", "2.5W 51N", "95, 0", "0, 200"):
			with self.assertRaises(ValueError, msg=bad):
				parse_site(bad)

	def test_stars_up_are_named_bright_and_above_the_horizon(self) -> None:
		from utils.manual_control import stars_up

		ra_up, dec_up = radec_at(60, 180)
		ra_low, dec_low = radec_at(5, 90)
		ra_down, dec_down = radec_at(-30, 0)
		stars = [(ra_down, dec_down, 0.0, "A"), (ra_up, dec_up, 1.0, "B"), (ra_low, dec_low, 1.5, "C"),
			(ra_up, dec_up, 2.0, "D"), (ra_up + 1, dec_up, 2.5, "E")]
		names = {"A": "Below", "B": "High", "C": "Low", "E": "Bo\u00f6tes star"}
		found = stars_up(stars, names, LAT, LON, WHEN)
		self.assertEqual([t.name for t in found], ["High", "Bootes star"])
		self.assertAlmostEqual(found[0].alt, 60, places=3)

	def test_the_sun(self) -> None:
		# March equinox 2026: 14:46 UTC on the 20th; June solstice: 08:24 on the 21st.
		ra, dec = sky_view.sun_radec(datetime(2026, 3, 20, 14, 46, tzinfo=timezone.utc))
		self.assertLess(min(ra, 360 - ra), 0.1)
		self.assertLess(abs(dec), 0.05)
		_ra, dec = sky_view.sun_radec(datetime(2026, 6, 21, 8, 24, tzinfo=timezone.utc))
		self.assertAlmostEqual(dec, 23.44, delta=0.02)

	def test_a_night_of_photos_shares_a_folder(self) -> None:
		from utils import photos

		evening = photos.night_folder(datetime(2026, 10, 16, 21, 0))
		after_midnight = photos.night_folder(datetime(2026, 10, 17, 3, 30))
		self.assertEqual(evening, after_midnight)
		self.assertTrue(evening.endswith("2026-10-16"))

	def test_a_photo_run_takes_each_shot_and_can_stop(self) -> None:
		from utils.photos import PhotoRun

		taken: List[int] = []
		messages: List[str] = []

		def capture() -> List[str]:
			taken.append(1)
			time.sleep(0.05)
			return [f"/tmp/IMG_{len(taken)}.JPG"]

		run = PhotoRun(capture)
		run.start(3, messages.append)
		self.assertTrue(wait_for(lambda: not run.running))
		self.assertEqual((len(taken), messages), (3, ["Saved 3 of 3 photos"]))
		taken.clear()
		messages.clear()
		run.start(50, messages.append)
		self.assertTrue(wait_for(lambda: len(taken) >= 1))
		run.stop()
		self.assertTrue(wait_for(lambda: not run.running))
		self.assertLess(len(taken), 50)
		taken.clear()
		run.start(1, messages.append)
		self.assertTrue(wait_for(lambda: not run.running))
		self.assertEqual(messages[-1], "Saved IMG_1.JPG")


class FakeSkyData:
	def __init__(self, stars, names) -> None:
		self.stars, self.names = stars, names

	def refresh(self, force: bool = False) -> bool:
		return True

	def ready(self) -> bool:
		return bool(self.stars)

	def snapshot(self):
		return self.stars, self.names, [], []


class FakeBoard:
	def __init__(self, estop: bool = False) -> None:
		self.estop = estop
		self.cleared = 0

	def estop_status(self) -> Dict[str, Any]:
		return {"active": self.estop}

	def clear_estop(self) -> Dict[str, Any]:
		self.cleared += 1
		self.estop = False
		return {}

	def send(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
		return {}  # LED commands


class ManualFlowTests(PanelUIBase):
	def setUp(self) -> None:
		super().setUp()
		from unittest import mock

		import core.hardware.tracking as tracking
		from utils.esp32_state import esp32_state

		config_state.save_location({"latitude": LAT, "longitude": LON})
		from utils.telescope_state import set_telescope_coords

		set_telescope_coords(37.95, 89.26, source="manual_reset")  # at Polaris: go-tos there don't slew
		self.board = FakeBoard()
		self.goes: List[tuple] = []
		self.syncs: List[tuple] = []
		self.patches = [
			*sun_at(201.0, -9.0),
			mock.patch.object(esp32_state, "get_connection", return_value=self.board),
			mock.patch.object(tracking, "trackCoordinates", side_effect=lambda *a, **k: self.goes.append(a)),
			mock.patch.object(tracking, "sync_position", side_effect=lambda *a: self.syncs.append(a)),
		]
		for p in self.patches:
			p.start()

	def tearDown(self) -> None:
		for p in self.patches:
			p.stop()
		super().tearDown()

	def act(self, panel: str, item: str) -> None:
		self.conn.emit({"event": "ui", "type": "action", "panel": panel, "item": item})

	def test_go_to_typed_coordinates(self) -> None:
		self.act("scope", "goto")
		self.answer("RA", value="25")
		self.answer("RA", dismissed=True)  # "Hours go 0 to 24"
		self.answer("RA", value="2 31 49")
		self.answer("Dec", value="89 15 51")
		go = self.answer("Go there?", value=True)
		self.assertIn("RA 2h31m49s", go["text"])
		self.assertTrue(wait_for(lambda: self.goes))
		name, ra, dec, mag = self.goes[0]
		self.assertEqual(name, "2h31 +89")
		self.assertAlmostEqual(ra, 37.954, places=3)
		self.assertAlmostEqual(dec, 89.264, places=3)

	def test_backing_out_goes_nowhere(self) -> None:
		self.act("scope", "goto")
		self.answer("RA", cancelled=True)
		time.sleep(0.2)
		self.act("scope", "goto")  # the first flow has ended, so this one runs
		self.answer("RA", cancelled=True)
		time.sleep(0.2)
		self.assertEqual(self.goes, [])

	def test_the_emergency_stop_is_offered_for_release(self) -> None:
		self.board.estop = True
		self.act("scope", "goto")
		self.answer("RA", value="2 31 49")
		self.answer("Dec", value="89 15")
		self.answer("Go there?", value=True)
		self.answer("Emergency stop", value=True)
		self.assertTrue(wait_for(lambda: self.goes))
		self.assertEqual(self.board.cleared, 1)

	def test_near_the_sun_needs_the_code(self) -> None:

		# Polaris is always up from LAT; put the Sun 9 deg from it.
		with sun_at(37.95, 80.0)[0], sun_at(37.95, 80.0)[1]:
			self.board.estop = True
			self.act("scope", "goto")
			self.answer("RA", value="2 31 49")
			self.answer("Dec", value="89 15 51")
			self.answer("Go there?", value=True)
			danger = self.answer("DANGER: SUN", value=True)
			self.assertEqual(danger["style"], "danger")
			self.assertIn("9 deg from the Sun", danger["text"])
			code = self.answer("Enter the code", value=True)
			self.assertEqual(code["kind"], "code")
			self.answer("Emergency stop", value=True)  # asked after the Sun, not before
			self.assertTrue(wait_for(lambda: self.goes))

	def test_backing_out_of_the_sun_warning_goes_nowhere(self) -> None:

		with sun_at(37.95, 64.0)[0], sun_at(37.95, 64.0)[1]:
			self.board.estop = True
			self.act("scope", "goto")
			self.answer("RA", value="2 31 49")
			self.answer("Dec", value="89 15 51")
			self.answer("Go there?", value=True)
			self.assertEqual(self.answer("Sun warning", value=False)["style"], "warn")
			time.sleep(0.3)
		self.assertEqual((self.goes, self.board.cleared), ([], 0))
		self.assertEqual(len(self.conn.actions("prompt")), 4)  # no E-stop prompt

	def test_go_to_a_star_from_the_list(self) -> None:
		from unittest import mock

		# Polaris: always up from LAT, whenever the test runs
		polaris = (37.95, 89.26, 1.97, "HD8890")
		data = FakeSkyData([polaris], {"HD8890": "Polaris"})
		with mock.patch("utils.sky_data.get_sky_data", return_value=data):
			self.act("scope", "star")
			choice = self.answer("Go to star", value=0)
			self.assertEqual(choice["options"], ["Polaris"])
			self.assertTrue(wait_for(lambda: self.goes))
		self.assertEqual(self.goes[0], ("Polaris", 37.95, 89.26, 1.97))

	def test_align_on_a_star_sets_the_position_then_tracks(self) -> None:
		from unittest import mock

		data = FakeSkyData([(37.95, 89.26, 1.97, "HD8890")], {"HD8890": "Polaris"})
		with mock.patch("utils.sky_data.get_sky_data", return_value=data):
			self.act("scope", "align")
			self.answer("Pointing at?", value=0)
			self.answer("Align", value=True)
			self.assertTrue(wait_for(lambda: self.goes))
		self.assertEqual(self.syncs, [(37.95, 89.26)])
		self.assertEqual(self.goes[0][0], "Polaris")

	def test_no_site_no_go(self) -> None:
		config_state.save_location({})
		self.act("scope", "goto")
		self.answer("Site needed", dismissed=True)
		time.sleep(0.2)
		self.assertEqual(self.goes, [])

	def test_typing_the_site(self) -> None:
		from utils.location import get_current_location

		self.conn.emit({"event": "ui", "type": "change", "panel": "scope", "item": "location", "value": "33.9S 18.4E"})
		self.assertTrue(wait_for(lambda: (get_current_location() or {}).get("latitude") == -33.9))
		self.assertEqual(get_current_location()["source"], "manual")
		self.assertTrue(wait_for(lambda: any(
			c.get("values") == {"scope.location": "-33.90, 18.40"} for c in self.conn.actions("set"))))

	def test_take_photos_starts_then_stops_a_run(self) -> None:
		taken: List[int] = []
		release = threading.Event()

		def capture() -> List[str]:
			taken.append(1)
			release.wait(2)
			return ["/tmp/IMG_1.JPG"]

		self.ui.photo_run._capture = capture
		self.conn.emit({"event": "ui", "type": "change", "panel": "camera", "item": "shots", "value": 3})
		self.assertTrue(wait_for(lambda: config_state.load_static_state().get("photos", {}).get("shots") == 3))
		self.act("camera", "shoot")
		self.assertTrue(wait_for(lambda: any(c.get("text") == "Taking 3 photos" for c in self.conn.actions("toast"))))
		self.act("camera", "shoot")
		self.assertTrue(wait_for(lambda: any(
			c.get("text") == "Stopping after this photo" for c in self.conn.actions("toast"))))
		release.set()
		self.assertTrue(wait_for(lambda: not self.ui.photo_run.running))
		self.assertEqual(len(taken), 1)


class OfflineModeTests(PanelUIBase):
	def setUp(self) -> None:
		super().setUp()
		from unittest import mock

		from core.networking import websocket

		self.reconnects: List[int] = []
		self.applied: List[dict] = []
		self.patches = [
			mock.patch.object(websocket, "request_reconnect", side_effect=lambda: self.reconnects.append(1)),
			mock.patch.object(websocket, "apply_client_config", side_effect=self.applied.append),
		]
		for p in self.patches:
			p.start()

	def tearDown(self) -> None:
		for p in self.patches:
			p.stop()
		super().tearDown()

	def test_the_toggle_goes_offline_and_back(self) -> None:
		from core.networking import websocket

		hello = self.conn.actions("hello")[0]
		camera = {i["id"]: i for p in hello["panels"] if p["id"] == "camera" for i in p["items"]}
		self.assertTrue(camera["liveview"]["visible"])

		self.conn.emit({"event": "ui", "type": "change", "panel": "setup", "item": "offline", "value": True})
		self.assertTrue(wait_for(config_state.is_offline_mode))
		self.assertTrue(wait_for(lambda: self.reconnects))
		self.assertFalse(websocket.server_enabled())
		self.assertTrue(wait_for(lambda: any(c.get("item") == "liveview" and c.get("fields") == {"visible": False}
			for c in self.conn.actions("set"))))

		self.conn.emit({"event": "ui", "type": "change", "panel": "setup", "item": "offline", "value": False})
		self.assertTrue(wait_for(lambda: not config_state.is_offline_mode()))
		self.assertTrue(wait_for(lambda: self.applied))
		self.assertEqual(self.applied[0]["base_url"], "https://seleno.org/")
		self.assertTrue(wait_for(lambda: any(c.get("item") == "liveview" and c.get("fields") == {"visible": True}
			for c in self.conn.actions("set"))))


class FirstRunTests(PanelUIBase):
	profile = {"client_config": {"client_id": "", "base_url": "", "api_token": ""}}

	def test_choosing_offline_at_the_welcome(self) -> None:
		welcome = self.answer("Welcome to SELENO", value=False)
		self.assertEqual((welcome["yes"], welcome["no"]), ("Connect", "Offline"))
		self.answer("Offline mode", dismissed=True)
		self.assertTrue(config_state.is_offline_mode())

	def test_backing_out_offers_offline(self) -> None:
		self.answer("Welcome to SELENO", cancelled=True)
		self.answer("Use offline?", value=False)  # back
		self.answer("Welcome to SELENO", value=True)
		self.answer("Device name", cancelled=True)
		self.answer("Use offline?", value=True)
		self.answer("Offline mode", dismissed=True)
		self.assertTrue(config_state.is_offline_mode())


class OfflineStartTests(PanelUIBase):
	profile = {"client_config": {"client_id": "", "base_url": "", "api_token": "", "offline": True}}

	def test_no_setup_when_already_offline(self) -> None:
		time.sleep(0.3)
		self.assertEqual(self.conn.actions("prompt"), [])


class SunGuardTests(PanelUIBase):
	SUN = (100.0, 0.0)

	def setUp(self) -> None:
		super().setUp()
		self.sun_patches = sun_at(*self.SUN)
		for p in self.sun_patches:
			p.start()

	def tearDown(self) -> None:
		for p in self.sun_patches:
			p.stop()
		super().tearDown()

	def confirm_in_thread(self, ra: float, dec: float, start=None, approval=None) -> List[tuple]:
		from utils import sun_guard

		results: List[tuple] = []
		threading.Thread(target=lambda: results.append(
			sun_guard.confirm_goto("Test", ra, dec, start=start, approval=approval)), daemon=True).start()
		return results

	def test_levels_by_distance(self) -> None:
		from utils import sun_guard

		for ra, level in ((150.0, "clear"), (125.0, "warn"), (112.0, "danger"), (100.5, "danger")):
			c = sun_guard.check("x", ra, 0.0, start=(ra, 0.0))
			self.assertEqual(c.level, level, ra)
			self.assertFalse(c.on_the_way)

	def test_moving_away_from_the_sun_isnt_getting_close(self) -> None:
		from utils import sun_guard

		c = sun_guard.SunCheck("x", target_deg=60.0, path_deg=10.0, start_deg=10.0)
		self.assertEqual((c.level, c.on_the_way), ("clear", False))
		c = sun_guard.SunCheck("x", target_deg=60.0, path_deg=5.0, start_deg=40.0)
		self.assertEqual((c.level, c.on_the_way), ("danger", True))
		self.assertIn("The slew to x passes 5 deg", c.where())
		self.assertEqual(sun_guard.keep_out_deg(80.0, 60.0), 22.0)
		self.assertEqual(sun_guard.keep_out_deg(10.0, 60.0), 9.5)

	def test_a_warning_needs_go_on(self) -> None:
		from utils import sun_guard

		results = self.confirm_in_thread(125.0, 0.0, start=(125.0, 0.0))
		prompt = self.answer("Sun warning", value=True)
		self.assertEqual((prompt["kind"], prompt["style"], prompt["no"]), ("confirm", "warn", "Cancel"))
		self.assertIn("blinds", prompt["text"])
		self.assertTrue(wait_for(lambda: results))
		allowed, _reason, approval = results[0]
		self.assertTrue(allowed)
		self.assertIsInstance(approval, sun_guard.SunApproval)
		results = self.confirm_in_thread(126.0, 0.0, start=(126.0, 0.0))
		self.answer("Sun warning", value=False)
		self.assertTrue(wait_for(lambda: results))
		self.assertEqual(results[0], (False, sun_guard.CANCELLED, None))

	def test_danger_needs_the_notice_and_the_code(self) -> None:
		results = self.confirm_in_thread(110.0, 0.0, start=(110.0, 0.0))
		notice = self.answer("DANGER: SUN", value=True)
		self.assertIn("eyepieces", notice["text"])
		code = self.answer("Enter the code", value=True)
		self.assertEqual(code["kind"], "code")
		self.assertGreater(code["timeout_ms"], 0)
		self.assertTrue(wait_for(lambda: results))
		allowed, _reason, approval = results[0]
		self.assertTrue(allowed)
		# Handing the yes on doesn't ask again, for that target only.
		again = self.confirm_in_thread(110.0, 0.0, start=(110.0, 0.0), approval=approval)
		self.assertTrue(wait_for(lambda: again))
		self.assertTrue(again[0][0])
		other = self.confirm_in_thread(111.0, 0.0, start=(111.0, 0.0), approval=approval)
		self.answer("DANGER: SUN", value=False)
		self.assertTrue(wait_for(lambda: other))
		self.assertFalse(other[0][0])
		# Something that only looks like one (as a server request could send) counts for nothing.
		fake = self.confirm_in_thread(110.0, 0.0, start=(110.0, 0.0), approval={"ra": 110.0, "dec": 0.0})
		self.answer("DANGER: SUN", value=False)
		self.assertTrue(wait_for(lambda: fake))
		self.assertFalse(fake[0][0])

	def test_a_cancelled_code_refuses(self) -> None:
		results = self.confirm_in_thread(110.0, 0.0, start=(110.0, 0.0))
		self.answer("DANGER: SUN", value=True)
		self.answer("Enter the code", cancelled=True)
		self.assertTrue(wait_for(lambda: results))
		self.assertFalse(results[0][0])

	def test_without_a_panel(self) -> None:
		from unittest import mock

		from utils import sun_guard

		with mock.patch.object(self.ui, "has_panel", return_value=False):
			self.assertEqual(sun_guard.confirm_goto("x", 125.0, 0.0, start=(125.0, 0.0)), (True, "", None))
			allowed, reason, _ = sun_guard.confirm_goto("x", 110.0, 0.0, start=(110.0, 0.0))
		self.assertFalse(allowed)
		self.assertIn("front panel", reason)
		self.assertEqual(self.conn.actions("prompt"), [])

	def test_track_coordinates_asks_before_stopping_anything(self) -> None:
		from unittest import mock

		import core.hardware.tracking as tracking

		config_state.save_location({"latitude": LAT, "longitude": LON})
		with mock.patch.object(self.ui, "has_panel", return_value=False), \
				mock.patch.object(tracking, "stop_tracking") as stop, \
				mock.patch.object(tracking, "_tracking_active", True):
			result = tracking.trackCoordinates("Near", 105.0, 0.0, 1.0)
			# A server can't pass an approval in: kwargs from JSON aren't one.
			forged = tracking.trackCoordinates("Near", 105.0, 0.0, 1.0, sun_approval={"ra": 105.0})
		self.assertIn("front panel", result["error"])
		self.assertIn("front panel", forged["error"])
		stop.assert_not_called()


class MountTests(unittest.TestCase):
	def setUp(self) -> None:
		from core.hardware import mount

		self.m = mount
		self.config = mount.MountConfig()

	def test_axes_and_sky_round_trip(self) -> None:
		for ha in range(-180, 180, 23):
			for dec in (-89.0, -45.0, 0.0, 30.0, 89.5):
				solutions = self.m.axis_solutions(float(ha), dec, self.config)
				self.assertTrue(any(not self.m.is_flipped(d) for _a, d in solutions))
				self.assertTrue(any(self.m.is_flipped(d) for _a, d in solutions))
				for a, d in solutions:
					h2, d2 = self.m.sky_from_axes(a, d)
					self.assertAlmostEqual(self.m.wrap180(h2 - ha), 0.0, places=9)
					self.assertAlmostEqual(d2, dec, places=9)
					self.assertTrue(self.config.within((a, d)))

	def test_over_the_pole_when_quicker(self) -> None:
		route = self.m.plan_route((-90.0, 80.0), (90.0, 80.0), self.config)
		self.assertTrue(route.flips)
		self.assertEqual(route.end, (-90.0, 100.0))  # only the Dec axis turns, through the pole
		self.assertLess(route.seconds, 15.0)
		stay = self.m.plan_route((-90.0, 80.0), (90.0, 80.0), self.m.MountConfig(pole_crossing=False))
		self.assertFalse(stay.flips)
		self.assertGreater(stay.seconds, 200.0)
		# Under the south pole too, when that's the quick way.
		under = self.m.plan_route((-60.0, 0.0), (60.0, 0.0), self.config)
		self.assertTrue(under.flips)
		self.assertLess(under.seconds, self.config.seconds((-60.0, 0.0), (60.0, 0.0)))

	def test_curves_round_the_sun(self) -> None:
		import math

		config = self.m.MountConfig(pole_crossing=False, dec_limits=(-90.0, 90.0))
		sun = (0.0, 0.0)
		straight = [(-60.0, 0.0), (60.0, 0.0)]
		self.assertLess(self.m.closest_to(straight, sun), 1.0)
		route = self.m.plan_route((-60.0, 0.0), (60.0, 0.0), config, sun=sun, keep_out=22.0)
		self.assertTrue(route.detour)
		self.assertGreaterEqual(route.sun_deg, 22.0)
		self.assertGreaterEqual(self.m.closest_to(route.points, sun, step_deg=0.1), 22.0)
		# Smooth: small turns at every corner (the firmware keeps speed through them).
		scaled = [(a * config.ra_gear, d * config.dec_gear) for a, d in route.points]
		for p, q, r in zip(scaled, scaled[1:], scaled[2:]):
			turn = math.atan2(r[1] - q[1], r[0] - q[0]) - math.atan2(q[1] - p[1], q[0] - p[0])
			self.assertLess(abs((turn + math.pi) % (2 * math.pi) - math.pi), math.radians(15))
		# The RA axis sets the pace, so going round costs no time here.
		self.assertAlmostEqual(route.seconds, config.seconds((-60.0, 0.0), (60.0, 0.0)), places=6)
		for start, end, s in (((-50.0, 23.0), (50.0, 23.0), (0.0, 23.4)), ((-30.0, -20.0), (40.0, 10.0), (5.0, -5.0))):
			r = self.m.plan_route(start, end, config, sun=s, keep_out=22.0)
			self.assertGreaterEqual(self.m.closest_to(r.points, s, step_deg=0.1), 22.0, (start, end))

	def test_limits_and_no_way_round(self) -> None:
		config = self.m.MountConfig(pole_crossing=False, ra_limits=(-70.0, 70.0), dec_limits=(-15.0, 15.0))
		for p in self.m.plan_route((-60.0, 0.0), (60.0, 0.0), config).points:
			self.assertTrue(config.within(p))
		# Boxed in: the keep-out fills the gap, so it goes straight and says so.
		route = self.m.plan_route((-60.0, 0.0), (60.0, 0.0), config, sun=(0.0, 0.0), keep_out=22.0)
		self.assertLess(route.sun_deg, 22.0)
		self.assertTrue(route.notes)

	def test_motor_steps(self) -> None:
		route = self.m.Route([(10.0, 5.0), (11.0, 4.0)], 0.0, 180.0)
		self.assertEqual(self.m.route_steps(route, self.config), [(1600, -640)])  # 360:1 and 144:1


class FakeMount:
	"""An ESP32 that runs paths at once: just enough for tracking.py."""

	def __init__(self) -> None:
		self.pos = {"motor1": 0, "motor2": 0}
		self.paths: List[List[tuple]] = []
		self.motor_info: Dict[str, Any] = {}
		self.continuous: List[str] = []

	def list_motors(self) -> Dict[str, Any]:
		return {"motors": ["motor1", "motor2"]}

	def path(self, points, speed_sps) -> Dict[str, Any]:
		self.paths.append([tuple(p) for p in points])
		self.pos["motor1"] += points[-1][0]
		self.pos["motor2"] += points[-1][1]
		return {"eta_s": 1.0}

	def send(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
		cmd, motor = payload.get("cmd"), payload.get("motor")
		if cmd in ("get_position", "status"):
			return {"position": self.pos[motor], "steps_per_rev": 1600, "moving": False, "enabled": True}
		if cmd == "reset_position":
			self.pos[motor] = 0
		if cmd == "start_continuous":
			self.continuous.append(motor)
		if cmd == "list_motors":
			return self.list_motors()
		return {}


class TrackingRouteTests(PanelUIBase):
	def setUp(self) -> None:
		super().setUp()
		from unittest import mock

		import core.hardware.tracking as tracking
		from utils.esp32_state import esp32_state

		self.tracking = tracking
		self.mount = FakeMount()
		config_state.save_location({"latitude": LAT, "longitude": LON})
		self.patches = [
			mock.patch.object(esp32_state, "ensure_connection", return_value=self.mount),
			mock.patch.object(tracking, "_motors_initialized", False),
			mock.patch.object(tracking, "_last_motor_steps", {"motor1": 0, "motor2": 0}),
		]
		for p in self.patches:
			p.start()

	def tearDown(self) -> None:
		self.tracking.stop_tracking()
		for p in self.patches:
			p.stop()
		super().tearDown()

	def at(self, ha: float, dec: float) -> tuple:
		"""RA of an hour angle now."""
		lst = lst_degrees(datetime.now(timezone.utc), LON)
		return (lst - ha) % 360.0, dec

	def route_sky(self, path) -> List[tuple]:
		"""A sent path back to (ha, dec) along the way, from the start axes."""
		from core.hardware import mount

		config = self.tracking.mount_config()
		out = []
		for s1, s2 in path:
			da, dd = config.axis_degrees(s1, s2)
			out.append(mount.sky_from_axes(self.start[0] + da, self.start[1] + dd))
		return out

	def test_a_goto_curves_round_the_sun(self) -> None:
		from unittest import mock

		from core.hardware import mount
		from utils.telescope_state import get_mount_axes, set_mount_axes

		set_mount_axes(-60.0, 0.0, source="test")
		self.start = get_mount_axes()
		sun = self.at(0.0, 0.0)
		target = self.at(60.0, 0.0)
		with mock.patch("utils.config_state.get_mount_config", return_value={
				"ra_axis_limits": [-270.0, 270.0], "dec_axis_limits": [-90.0, 90.0], "pole_crossing": False}), \
				sun_at(*sun)[0], sun_at(*sun)[1]:
			result = self.tracking.trackCoordinates("Far side", target[0], target[1], 1.0)
		self.assertTrue(result["round_the_sun"], result)
		self.assertGreaterEqual(result["closest_to_sun_deg"], 22.0)
		path = self.mount.paths[0]
		self.assertGreater(len(path), 2)
		# The axes end where the route did, pointing at the target (the
		# tracking loop reads the motors; the route aimed a little ahead).
		self.assertTrue(wait_for(lambda: mount.separation(
			mount.sky_from_axes(*get_mount_axes()), (60.0, 0.0)) < 1.0, timeout=5.0))

	def test_a_goto_goes_over_the_pole_when_quicker(self) -> None:
		from utils.telescope_state import get_mount_axes, set_mount_axes

		set_mount_axes(-90.0, 80.0, source="test")
		target = self.at(90.0, 80.0)
		with sun_at(*self.at(0.0, -80.0))[0], sun_at(*self.at(0.0, -80.0))[1]:
			result = self.tracking.trackCoordinates("Over", target[0], target[1], 1.0)
		self.assertTrue(result["over_the_pole"], result)
		self.assertLess(result["seconds"], 20.0)
		self.assertTrue(wait_for(lambda: get_mount_axes()[1] > 90.0, timeout=5.0))  # on the far side now

	def test_the_watchdog_stops_tracking_near_the_sun(self) -> None:
		from utils.telescope_state import set_mount_axes

		set_mount_axes(10.0, 0.0, source="test")
		sun = self.at(0.0, 0.0)
		with sun_at(*sun)[0], sun_at(*sun)[1]:
			self.tracking._target_object = {"ra": self.at(10.0, 0.0)[0], "dec": 0.0, "sun_floor": 19.5}
			problem = self.tracking._sun_and_limits_problem()
			self.assertIn("from the Sun", problem)
			self.tracking._target_object["sun_floor"] = 9.0  # approved this close
			self.assertIsNone(self.tracking._sun_and_limits_problem())
		self.tracking._target_object = None


if __name__ == "__main__":
	unittest.main()
