"""The server link (command lanes, ids, backoff), the server's commands,
the raw-motor guard, the local page, plate solving, tracking with a fake
mount, and the LED batching."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
import time
import unittest
import urllib.request
from typing import Any, Dict, List, Optional
from unittest import mock

from tests.helpers import ScratchConfig, wait_for

from core.camera.backends import FakeBackend
from core.camera.service import CameraService, set_camera_service

LAT, LON = 51.4623, -2.5502


# --- command lanes ---------------------------------------------------------------------

class FakeWS:
	def __init__(self, messages: List[Dict[str, Any]], hold_open: float = 2.0) -> None:
		self.messages = [json.dumps(m) for m in messages]
		self.sent: List[Dict[str, Any]] = []
		self.sent_at: List[float] = []
		self.hold_open = hold_open
		self.close_code = None

	async def send(self, text: str) -> None:
		self.sent.append(json.loads(text))
		self.sent_at.append(time.monotonic())

	def __aiter__(self):
		return self._iter()

	async def _iter(self):
		for message in self.messages:
			yield message
		await asyncio.sleep(self.hold_open)  # let the lanes finish


class CommandLaneTests(unittest.TestCase):
	def test_lanes(self) -> None:
		from core.networking.websocket import lane_for

		self.assertEqual(lane_for("stopTracking"), "priority")
		self.assertEqual(lane_for("espEstop"), "priority")
		self.assertEqual(lane_for("capturePhoto"), "camera")
		self.assertEqual(lane_for("getCameraChoices"), "camera")
		self.assertEqual(lane_for("getMountStatus"), "query")
		self.assertEqual(lane_for("espStatus"), "query")
		self.assertEqual(lane_for("trackCoordinates"), "motion")
		self.assertEqual(lane_for("espMoveSteps"), "motion")

	def test_a_slow_photo_doesnt_hold_up_a_stop_and_every_reply_has_its_id(self) -> None:
		from core.networking import websocket
		from utils import handlers

		def slow(currentid):
			time.sleep(1.0)
			return "photo"

		def broken():
			raise RuntimeError("it broke")
		fake_map = dict(handlers.function_map, capturePhoto=slow, stopTracking=lambda: "stopped",
			getMountStatus=broken)
		ws = FakeWS([
			{"type": "call", "function": "capturePhoto", "args": [1], "kwargs": {}, "id": "a"},
			{"type": "call", "function": "stopTracking", "args": [], "kwargs": {}, "id": "b"},
			{"type": "call", "function": "getMountStatus", "args": [], "kwargs": {}, "id": "c"},
			{"type": "call", "function": "noSuchThing", "args": [], "kwargs": {}, "id": "d"},
		])
		with mock.patch.object(handlers, "function_map", fake_map), mock.patch.object(websocket, "API_TOKEN", "t"), \
				mock.patch.object(websocket, "CLIENT_ID", "c"):
			asyncio.run(websocket.handle_server(ws))
		replies = {m.get("id"): m for m in ws.sent if "id" in m}
		self.assertEqual(replies["a"]["result"], "photo")
		self.assertEqual(replies["b"]["result"], "stopped")
		self.assertIn("it broke", replies["c"]["error"])
		self.assertIn("not found", replies["d"]["error"])
		order = [m.get("id") for m in ws.sent if "id" in m]
		self.assertLess(order.index("b"), order.index("a"))  # the stop didn't wait
		self.assertEqual(ws.sent[0], {"token": "t", "client_id": "c"})  # authenticated first

	def test_backoff_grows_and_resets(self) -> None:
		from core.networking.websocket import ReconnectBackoff

		b = ReconnectBackoff()
		delays = [b.after(1.0) for _ in range(8)]
		self.assertTrue(2.0 <= delays[0] <= 2.5)
		self.assertTrue(all(later >= earlier * 0.8 for earlier, later in zip(delays, delays[1:])))
		self.assertLessEqual(max(delays), 60.0 * 1.25)
		self.assertTrue(2.0 <= b.after(600.0) <= 2.5)  # a connection that lasted


# --- the server's commands -------------------------------------------------------------

class HandlerTests(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		self.in_scratch_dir()
		self.backend = FakeBackend(width=160, height=100)
		self.service = CameraService(backend=self.backend).start()
		set_camera_service(self.service)
		self.assertTrue(wait_for(lambda: self.service.connected))

	def tearDown(self) -> None:
		self.service.stop()
		set_camera_service(None)
		super().tearDown()

	def test_camera_choices_and_settings_the_web_page_uses(self) -> None:
		from utils import handlers

		choices = handlers.get_camera_choices()
		self.assertIn("1/30", choices["shutterSpeed"])
		self.assertIn("800", choices["iso"])
		self.assertEqual(choices["current"]["iso"], "800")
		handlers.setCameraSetting("/main/imgsettings/iso", "1600")
		self.assertEqual(self.backend.config["iso"]["value"], "1600")

	def test_capture_queues_the_upload_and_returns(self) -> None:
		from utils import handlers, uploads

		queue = uploads.UploadQueue(folder=self.dir, sender=lambda *a: None, online=lambda: False, autostart=False)
		with mock.patch.object(handlers, "get_upload_queue", return_value=queue):
			result = handlers.capturePhoto("42")
		self.assertEqual(result["status"], "captured")
		self.assertEqual(result["queued"], 2)
		self.assertTrue(all(name.startswith("42_photo_") for name in result["files"]))
		self.assertEqual(queue.status()["waiting"], 2)

	def test_live_view_options_are_checked(self) -> None:
		from utils import handlers

		self.assertIn("error", handlers.setLiveViewOptions(quality="ultra"))
		self.assertIn("error", handlers.setLiveViewOptions(colour="red"))
		self.assertEqual(handlers.setLiveViewOptions(quality="low", max_fps=5)["liveview"]["quality"], "low")

	def test_clock_and_horizon(self) -> None:
		from utils import clock, handlers

		self.assertIn("error", handlers.setClock("whenever"))
		with mock.patch.object(clock, "system_synced", return_value=False):
			result = handlers.setClock("2030-01-01 00:00")
		self.assertGreater(result["offset_s"], 1e8)
		clock.clear()
		self.assertEqual(handlers.setHorizon(min_alt_deg=12, profile=[[0, 5], [180, 20]])["min_alt_deg"], 12.0)

	def test_planets(self) -> None:
		from utils import handlers

		names = [p["name"] for p in handlers.getPlanets()["planets"]]
		self.assertEqual(names[:3], ["Sun", "Moon", "Mercury"])


# --- the raw-motor guard -----------------------------------------------------------------

class MotorGuardTests(ScratchConfig):
	def check(self, **kwargs):
		from core.hardware import mount
		from utils import motor_guard

		config = mount.MountConfig(ra_limits=(-90.0, 90.0), dec_limits=(-90.0, 90.0))
		context = (config, self.axes, self.sun)
		with mock.patch.object(motor_guard, "_context", return_value=context):
			return motor_guard.check_raw_move(**kwargs)

	def test_limits_and_the_sun(self) -> None:
		from core.hardware import mount

		config = mount.MountConfig()
		self.axes, self.sun = (80.0, 0.0), (-60.0, 0.0)
		ten_deg = config.motor_steps(10.0, 0.0)[0]
		self.assertIn("limit", self.check(motor_id="motor1", steps=ten_deg * 2))
		self.assertIsNone(self.check(motor_id="motor1", steps=-ten_deg))
		self.axes, self.sun = (0.0, 0.0), (-30.0, 0.0)
		self.assertIn("Sun", self.check(motor_id="motor1", steps=-ten_deg * 2))  # to 10 deg from it
		self.axes = (-25.0, 0.0)  # already close: moving away is fine
		self.assertIsNone(self.check(motor_id="motor1", steps=ten_deg))
		self.assertIn("Sun", self.check(motor_id="motor1", continuous=True, forward=False))

	def test_the_policy(self) -> None:
		from utils.config_state import save_section

		self.axes, self.sun = (80.0, 0.0), (-60.0, 0.0)
		save_section("server", {"raw_motor_commands": "off"})
		self.assertIn("off", self.check(motor_id="motor1", steps=1))
		save_section("server", {"raw_motor_commands": "unrestricted"})
		self.assertIsNone(self.check(motor_id="motor1", steps=10 ** 9))


# --- the local page -------------------------------------------------------------------------

def free_port() -> int:
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


class LocalPageTests(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		self.in_scratch_dir()
		self.service = CameraService(backend=FakeBackend(width=160, height=100, fps=20)).start()
		set_camera_service(self.service)
		from core.networking import local_page

		self.page = local_page
		self.port = free_port()
		local_page.start(self.port)
		self.base = f"http://127.0.0.1:{self.port}"

	def tearDown(self) -> None:
		self.page.stop()
		self.service.stop()
		set_camera_service(None)
		super().tearDown()

	def request(self, path: str, data: Optional[bytes] = None, cookie: str = "", json_body: bool = False):
		req = urllib.request.Request(self.base + path, data=data)
		if cookie:
			req.add_header("Cookie", cookie)
		if json_body:
			req.add_header("Content-Type", "application/json")
		opener = urllib.request.build_opener(NoRedirect)
		try:
			with opener.open(req, timeout=5) as resp:
				return resp.status, resp.headers, resp.read()
		except urllib.error.HTTPError as err:
			return err.code, err.headers, err.read()

	def test_the_code_guards_everything(self) -> None:
		status, _h, _b = self.request("/api/status")
		self.assertEqual(status, 401)
		status, _h, _b = self.request("/login", b"code=nope")
		self.assertEqual(status, 403)
		status, headers, _b = self.request("/login", f"code={self.page.code()}".encode())
		self.assertEqual(status, 303)
		cookie = headers["Set-Cookie"].split(";")[0]
		status, _h, body = self.request("/api/status", cookie=cookie)
		self.assertEqual(status, 200)
		self.assertTrue(json.loads(body)["camera"]["connected"])
		status, _h, _b = self.request("/files/..%2F..%2Fetc%2Fpasswd", cookie=cookie)
		self.assertEqual(status, 404)
		status, _h, body = self.request("/api/photo", b"{}", cookie=cookie, json_body=True)
		self.assertIn("Saved", json.loads(body)["status"])
		status, _h, body = self.request("/api/files", cookie=cookie)
		self.assertTrue(json.loads(body)["files"])

	def test_too_many_wrong_codes(self) -> None:
		self.page._failed.clear()
		for _ in range(5):
			self.request("/login", b"code=000000" if self.page.code() != "000000" else b"code=111111")
		status, _h, _b = self.request("/login", f"code={self.page.code()}".encode())
		self.assertEqual(status, 429)
		self.page._failed.clear()


class NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, *args, **kwargs):
		return None


# --- plate solving ---------------------------------------------------------------------------

FAKE_ASTAP = r'''#!{python}
import sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
with open(out + ".ini", "w") as f:
    if "fail" in args[args.index("-f") + 1]:
        f.write("PLTSOLVD=F\nERROR=No solution found\n")
    else:
        # The reference pixel 100 px right of the centre of a 400x300 picture, 1"/px.
        f.write("PLTSOLVD=T\nCRPIX1=300.5\nCRPIX2=150.5\nCRVAL1=10.0\nCRVAL2=20.0\n"
                "CD1_1=-0.000277777778\nCD1_2=0\nCD2_1=0\nCD2_2=0.000277777778\nCROTA2=0.0\n")
'''


class SolverTests(ScratchConfig):
	def test_reads_the_answer_and_finds_the_centre(self) -> None:
		from PIL import Image

		from core.astrometry import solver
		from utils.config_state import save_section
		from utils.sky_view import angular_distance

		astap = os.path.join(self.dir, "astap_cli")
		with open(astap, "w") as f:
			f.write(FAKE_ASTAP.replace("{python}", sys.executable))
		os.chmod(astap, os.stat(astap).st_mode | stat.S_IEXEC)
		save_section("solver", {"path": astap})
		picture = os.path.join(self.dir, "pic.png")
		Image.new("L", (400, 300)).save(picture)
		result = solver.solve(picture, 10.0, 20.0)
		# The centre is 100 px (100") left of the reference pixel: east by 100".
		self.assertLess(abs(angular_distance(result.ra, result.dec, 10.0, 20.0) * 3600 - 100.0), 1.0)
		self.assertGreater(result.ra, 10.0)
		self.assertAlmostEqual(result.scale_arcsec, 1.0, places=3)
		Image.new("L", (400, 300)).save(os.path.join(self.dir, "fail.png"))
		with self.assertRaises(solver.SolveError):
			solver.solve(os.path.join(self.dir, "fail.png"), 10.0, 20.0)

	@unittest.skipUnless(os.path.exists(os.path.expanduser("~/astap/astap_cli")), "ASTAP isn't installed")
	def test_real_astap_on_a_rendered_star_field(self) -> None:
		import math

		import numpy as np
		from PIL import Image

		from core.astrometry import solver
		from utils.sky_view import angular_distance

		cache = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "sky_cache.json")
		if not os.path.exists(cache):
			self.skipTest("no star catalogue cached")
		with open(cache) as f:
			stars = json.load(f)["stars"]
		ra0, dec0, scale, width, height = 280.0, 40.0, 90.0, 1500, 1000
		d = math.pi / 180
		img = np.full((height, width), 10.0)
		yy, xx = np.mgrid[-6:7, -6:7]
		psf = np.exp(-(xx ** 2 + yy ** 2) / (2 * 1.4 ** 2))
		for ra, dec, mag, _id in stars:
			a, b, a0, b0 = ra * d, dec * d, ra0 * d, dec0 * d
			cosc = math.sin(b0) * math.sin(b) + math.cos(b0) * math.cos(b) * math.cos(a - a0)
			if cosc <= 0.2:
				continue
			xi = math.cos(b) * math.sin(a - a0) / cosc / d * 3600 / scale
			eta = (math.cos(b0) * math.sin(b) - math.sin(b0) * math.cos(b) * math.cos(a - a0)) / cosc / d * 3600 / scale
			ix, iy = int(round(width / 2 - xi)), int(round(height / 2 - eta))
			if 6 <= ix < width - 7 and 6 <= iy < height - 7:
				img[iy - 6:iy + 7, ix - 6:ix + 7] += psf * 200 * 10 ** (-0.4 * (mag - 1.0))
		picture = os.path.join(self.dir, "wide.png")
		Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(picture)
		result = solver.solve(picture, 285.0, 35.0, fov_deg=25.0, radius_deg=30.0)
		self.assertLess(angular_distance(result.ra, result.dec, ra0, dec0) * 60, 3.0)


# --- tracking with a fake mount ----------------------------------------------------------------

class FakeMount:
	"""An ESP32 that runs paths at once and remembers continuous moves."""

	def __init__(self) -> None:
		self.pos = {"motor1": 0, "motor2": 0}
		self.paths: List[List[tuple]] = []
		self.speed = {"motor1": 0.0, "motor2": 0.0}
		self.continuous: Dict[str, bool] = {}
		self.stops: List[str] = []
		self.motor_info: Dict[str, Any] = {}

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
		if cmd == "set_speed":
			self.speed[motor] = float(payload.get("sps", 0.0))
		if cmd == "start_continuous":
			self.continuous[motor] = bool(payload.get("forward"))
		if cmd == "stop":
			self.stops.append(motor)
			self.continuous.pop(motor, None)
		if cmd == "list_motors":
			return self.list_motors()
		return {}


class TrackingTests(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		import core.hardware.tracking as tracking
		from utils.config_state import save_location
		from utils.esp32_state import esp32_state

		self.tracking = tracking
		self.mount = FakeMount()
		save_location({"latitude": LAT, "longitude": LON, "source": "manual"})
		self.patches = [
			mock.patch.object(esp32_state, "ensure_connection", return_value=self.mount),
			mock.patch.object(tracking, "_motors_initialized", False),
			mock.patch.object(tracking, "_last_motor_steps", {"motor1": 0, "motor2": 0}),
			mock.patch("utils.LEDmanager.get_led_manager", return_value=mock.MagicMock()),
			mock.patch.object(tracking, "get_led_manager", return_value=mock.MagicMock()),
			mock.patch("utils.sun_guard._panel", return_value=None),
		]
		for p in self.patches:
			p.start()

	def tearDown(self) -> None:
		self.tracking.stop_tracking()
		for p in self.patches:
			p.stop()
		super().tearDown()

	def radec(self, ha: float, dec: float):
		from utils import clock
		from utils.sky_view import lst_degrees

		return (lst_degrees(clock.now(), LON) - ha) % 360.0, dec

	def sun_far(self):
		ra, dec = self.radec(180.0, -20.0)
		return [mock.patch("utils.sun_guard.sun_radec", return_value=(ra, dec)),
			mock.patch("core.hardware.tracking.sun_radec", return_value=(ra, dec))]

	def test_server_go_tos_below_the_horizon_are_refused(self) -> None:
		from utils.telescope_state import set_mount_axes

		set_mount_axes(0.0, 90.0, source="test", when="now")
		low = self.radec(0.0, -60.0)  # under the southern horizon from 51 N
		with self.sun_far()[0], self.sun_far()[1]:
			refused = self.tracking.trackCoordinates("Low", low[0], low[1], 1.0)
			self.assertIn("horizon", refused["error"])
			allowed = self.tracking.trackCoordinates("Low", low[0], low[1], 1.0, allow_below_horizon=True)
		self.assertEqual(allowed["status"], "slewing")

	def test_it_locks_and_tracks_at_the_sidereal_rate(self) -> None:
		from utils.telescope_state import set_mount_axes

		set_mount_axes(0.0, 90.0, source="test", when="now")
		target = self.radec(-30.0, 30.0)
		with self.sun_far()[0], self.sun_far()[1]:
			result = self.tracking.trackCoordinates("Star", target[0], target[1], 1.0)
			self.assertEqual(result["status"], "slewing")
			self.assertTrue(wait_for(lambda: self.tracking.is_sidereal_tracking(), timeout=10))
		self.assertTrue(self.mount.continuous.get("motor1"))  # forward in the north
		expected = 360.0 * 1600 / 86164.0905  # one turn of the axis a sidereal day, 360:1
		self.assertAlmostEqual(self.mount.speed["motor1"], expected, delta=expected * 0.11)

	def test_the_moon_is_followed_on_both_axes(self) -> None:
		from utils import clock, ephemeris
		from utils.telescope_state import set_mount_axes

		ra, dec = ephemeris.position("Moon", clock.now(), (LAT, LON))
		set_mount_axes(0.0, 90.0, source="test", when="now")
		with self.sun_far()[0], self.sun_far()[1]:
			result = self.tracking.trackCoordinates("Moon", ra, dec, -10, allow_below_horizon=True)
			self.assertEqual(result["status"], "slewing")
			self.assertEqual(self.tracking._target_object.get("body"), "Moon")
			self.assertTrue(wait_for(lambda: self.tracking.is_sidereal_tracking(), timeout=10))
		rate = self.tracking.current_rate()
		self.assertEqual(rate["rate"], "moon")
		self.assertLess(rate["ha_arcsec_per_s"], 15.0)  # slower than the stars

	def test_south_of_the_equator_the_ra_motor_turns_backwards(self) -> None:
		from utils.config_state import save_location
		from utils.telescope_state import set_mount_axes

		save_location({"latitude": -33.9, "longitude": 18.4, "source": "manual"})
		set_mount_axes(0.0, 90.0, source="test", when="now")
		from utils import clock
		from utils.sky_view import lst_degrees

		ra = (lst_degrees(clock.now(), 18.4) + 20.0) % 360.0  # 20 deg east of the meridian
		sun = ((ra + 180.0) % 360.0, 20.0)
		with mock.patch("utils.sun_guard.sun_radec", return_value=sun), \
				mock.patch("core.hardware.tracking.sun_radec", return_value=sun):
			self.tracking.trackCoordinates("Southern", ra, -50.0, 1.0)
			self.assertTrue(wait_for(lambda: self.tracking.is_sidereal_tracking(), timeout=10))
		self.assertFalse(self.mount.continuous.get("motor1"))

	def test_park_then_go_tos_wait_for_unpark(self) -> None:
		from utils.telescope_state import get_mount_axes, get_parked, set_mount_axes

		set_mount_axes(40.0, 20.0, source="test", when="now")
		with self.sun_far()[0], self.sun_far()[1]:
			self.assertEqual(self.tracking.park()["status"], "Parked")
			self.assertTrue(get_parked())
			axes = get_mount_axes()
			self.assertAlmostEqual(axes[0], 0.0, places=2)
			self.assertAlmostEqual(axes[1], 90.0, places=2)
			target = self.radec(-30.0, 30.0)
			self.assertIn("parked", self.tracking.trackCoordinates("x", target[0], target[1], 1.0)["error"])
			self.tracking.unpark()
			self.assertEqual(self.tracking.trackCoordinates("x", target[0], target[1], 1.0)["status"], "slewing")

	def test_rates_can_be_chosen(self) -> None:
		self.assertIn("error", self.tracking.set_rate("warp"))
		self.tracking.set_rate("lunar")
		ha_rate, _dec, name = self.tracking.sky_rates({"ra": 10.0, "dec": 10.0})
		self.assertEqual(name, "lunar")
		self.assertAlmostEqual(ha_rate * 3600, 14.685, places=3)


# --- LEDs ------------------------------------------------------------------------------------------

class LedTests(unittest.TestCase):
	def test_one_command_for_the_changes(self) -> None:
		from utils.LEDmanager import LEDManager

		sent: List[Dict[str, Any]] = []
		conn = mock.MagicMock()
		conn.send.side_effect = lambda payload, **k: sent.append(payload) or {}
		leds = mock.MagicMock()
		manager = LEDManager()
		with mock.patch("utils.LEDmanager.esp32_state.ensure_connection", return_value=conn), \
				mock.patch("utils.LEDmanager.ESP32LED", return_value=leds):
			start = time.monotonic()
			manager.set_tracking(True)
			manager.set_command_busy(True)
			manager.set_command_busy(True)  # nothing changed: nothing sent
			took = time.monotonic() - start
		batches = [p for p in sent if "leds" in p]
		self.assertEqual(len(batches), 1)
		self.assertEqual(batches[0]["leds"]["green"], "on")
		self.assertEqual(batches[0]["leds"]["blue"], "on")
		leds.blue.blink.assert_called_once_with(interval_ms=1400)  # one LED: the plain command
		self.assertLess(took, 0.2)  # no gaps

	def test_older_firmware_gets_one_at_a_time(self) -> None:
		from utils.LEDmanager import LEDManager

		conn = mock.MagicMock()
		conn.send.side_effect = RuntimeError("ESP32 error: Missing LED mode")
		leds = mock.MagicMock()
		manager = LEDManager()
		with mock.patch("utils.LEDmanager.esp32_state.ensure_connection", return_value=conn), \
				mock.patch("utils.LEDmanager.ESP32LED", return_value=leds):
			manager.set_tracking(True)
		self.assertFalse(manager._batch_ok)
		leds.green.on.assert_called_once()


if __name__ == "__main__":
	unittest.main()
