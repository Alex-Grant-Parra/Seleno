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


class PanelUITests(unittest.TestCase):
	def setUp(self) -> None:
		# Never touch the real config files.
		self.tmp = tempfile.TemporaryDirectory()
		self.saved = (config_state.CLIENT_PROFILE_FILE, config_state.STATIC_STATE_FILE,
			config_state.RUNTIME_STATE_FILE, config_state._STATE_READY)
		config_state.CLIENT_PROFILE_FILE = os.path.join(self.tmp.name, "client_profile.json")
		config_state.STATIC_STATE_FILE = os.path.join(self.tmp.name, "client_config.json")
		config_state.RUNTIME_STATE_FILE = os.path.join(self.tmp.name, "runtime_state.json")
		config_state._STATE_READY = False
		config_state.save_client_profile({"client_config": {"client_id": "scope", "base_url": "seleno.org",
			"api_token": "0123456789abcdef"}})
		from utils import panel_ui

		self.panel_ui = panel_ui
		self.ui = panel_ui.PanelUI()
		self.conn = FakeConn()
		self.conn.reply = lambda action, fields: (
			{"width": 128, "height": 160, "sky_w": 128, "sky_h": 136} if action in ("hello", "status") else {})
		self.ui.attach(self.conn)
		self.assertTrue(self.ui.attached.wait(3))

	def tearDown(self) -> None:
		(config_state.CLIENT_PROFILE_FILE, config_state.STATIC_STATE_FILE,
			config_state.RUNTIME_STATE_FILE, config_state._STATE_READY) = self.saved
		self.tmp.cleanup()

	def test_hello_sends_the_pi_tabs_with_values(self) -> None:
		hello = self.conn.actions("hello")[0]
		self.assertEqual(hello["device"], "scope")
		panels = {p["id"]: p for p in hello["panels"]}
		self.assertEqual({"scope", "sky", "setup", "pi"}, set(panels))
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


if __name__ == "__main__":
	unittest.main()
