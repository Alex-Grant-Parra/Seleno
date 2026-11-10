"""What the Seleno server asked of the client (client-requests-v0.8):
capabilities and the new status parts, frame metadata, test shots, live
view JPEG options, asking for some RAWs, automatic flats, the dither
settle, centring after a slew and lossless SER video. No camera needed."""
from __future__ import annotations

import io
import os
import re
import struct
import threading
import time
from unittest import mock

from tests.helpers import ScratchConfig, wait_for

from core.camera.backends import FakeBackend, parse_shutter
from core.camera.service import CameraService, set_camera_service


class CameraTestCase(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		self.in_scratch_dir()
		self.backend = FakeBackend(width=320, height=200)
		self.service = CameraService(backend=self.backend).start()
		set_camera_service(self.service)
		self.assertTrue(wait_for(lambda: self.service.connected))
		from utils import handlers

		handlers._camera_info_cache.update(at=0.0, value={})

	def tearDown(self) -> None:
		self.service.stop()
		set_camera_service(None)
		super().tearDown()

	def queue(self, **kwargs):
		from utils import uploads

		return uploads.UploadQueue(folder=self.dir, sender=lambda *a: None, online=lambda: False,
			autostart=False, **kwargs)


class StatusTests(CameraTestCase):
	def test_capabilities_site_solve_and_camera_numbers(self) -> None:
		from utils import client_status, plate_solve

		plate_solve._last_solve = None
		status = client_status.collect()
		self.assertEqual(status["capabilities"],
			["capture_frame_type", "upload_pending_filter", "liveview_jpeg_options", "dead_zones"])
		self.assertEqual(status["site"], {"lat": 51.4623, "lon": -2.5502})
		self.assertEqual(status["dead_zones"], {"zones": []})
		self.assertIsNone(status["solve"])
		camera = status["camera"]
		self.assertEqual(camera["model"], "Fake Camera")
		self.assertEqual(camera["battery"], 75)
		self.assertEqual(camera["card_free_bytes"], 12000 * 1024 * 1024)
		self.assertIsNone(camera["temperature_c"])  # the fake camera has none, like most Canons

	def test_a_solve_is_kept_for_the_status(self) -> None:
		from core.astrometry.solver import SolveResult
		from utils import plate_solve

		plate_solve._note_solve(SolveResult(83.81, -5.4, 1.98, 91.4, 5616, 3744, 3.0))
		solve = plate_solve.last_solve()
		self.assertRegex(solve["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
		self.assertEqual((solve["ra"], solve["dec"], solve["pixel_scale"], solve["rotation_deg"]),
			(83.81, -5.4, 1.98, 91.4))
		self.assertAlmostEqual(solve["field_w_deg"], 3.089, places=3)
		self.assertAlmostEqual(solve["field_h_deg"], 2.059, places=3)
		plate_solve._last_solve = None

	def test_numbers_from_camera_text(self) -> None:
		from utils.handlers import _number

		self.assertEqual(_number("75%"), 75.0)
		self.assertEqual(_number("18.5 C"), 18.5)
		self.assertEqual(_number("Full"), 100.0)
		self.assertIsNone(_number("n/a"))
		self.assertAlmostEqual(parse_shutter("1/250"), 0.004)
		self.assertEqual(parse_shutter('2.5"'), 2.5)
		self.assertIsNone(parse_shutter("bulb"))


class CaptureTests(CameraTestCase):
	def test_upload_meta_and_utc_times_with_ms(self) -> None:
		from utils import handlers
		from utils.config_state import save_section

		save_section("camera", {"focal_mm": 650})
		queue = self.queue()
		with mock.patch.object(handlers, "get_upload_queue", return_value=queue):
			result = handlers.capturePhoto("42", frame_type="light")
		self.assertEqual(result["status"], "captured")
		entries = queue._load()
		self.assertEqual(len(entries), 2)
		meta = entries[0]["meta"]
		self.assertEqual(meta["frame_type"], "light")
		self.assertRegex(meta["captured"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
		self.assertEqual(meta["camera"], "Fake Camera")
		self.assertEqual(meta["iso"], 800)
		self.assertAlmostEqual(meta["exposure_s"], 1 / 30)
		self.assertEqual(meta["focal_mm"], 650)
		self.assertEqual(meta["site"], [51.4623, -2.5502])
		self.assertNotIn("solve", meta)
		self.assertEqual(meta["pair"], result["pair"])
		self.assertEqual(entries[1]["meta"]["pair"], result["pair"])

	def test_a_test_shot_jpeg_jumps_the_queue(self) -> None:
		from utils import handlers

		queue = self.queue()
		with mock.patch.object(handlers, "get_upload_queue", return_value=queue):
			handlers.capturePhoto("42")
			time.sleep(1.1)  # a different name
			result = handlers.capturePhoto("42", frame_type="test")
			self.assertIn("error", handlers.capturePhoto("42", frame_type="selfie"))
		self.assertEqual(result["frame_type"], "test")
		first = queue._next()
		self.assertTrue(first["path"].endswith(".jpg"))
		self.assertEqual(first["meta"]["frame_type"], "test")
		self.assertIn("_test_", first["path"])
		raw = next(e for e in queue._load() if e["meta"]["frame_type"] == "test" and e["kind"] == "raw")
		self.assertFalse(raw.get("priority"))  # its RAW waits its turn


class UploadPendingTests(CameraTestCase):
	static = {"upload": {"raw": "on_request"}}

	def test_some_raws_not_all(self) -> None:
		from utils import handlers

		queue = self.queue()
		for n in range(3):
			queue.add_many([f"/p/42_IMG_041{n}.JPG", f"/p/42_IMG_041{n}.CR2"], "42", {"pair": f"p41{n}"})
		with mock.patch.object(handlers, "get_upload_queue", return_value=queue):
			reply = handlers.uploadPending("raw", files=["42_IMG_0410.JPG", "IMG_0419.JPG"], pairs=["p410", "p419"])
			self.assertEqual(reply, {"queued": 1, "missing": ["IMG_0419.JPG"]})
			reply = handlers.uploadPending("raw", pairs=["p412"])
			self.assertEqual(reply, {"queued": 1, "missing": []})
			self.assertIn("error", handlers.uploadPending("raw", files="IMG_0410.JPG"))
		states = {os.path.basename(e["path"]): e["state"] for e in queue._load() if e["kind"] == "raw"}
		self.assertEqual(states, {"42_IMG_0410.CR2": "waiting", "42_IMG_0411.CR2": "held", "42_IMG_0412.CR2": "waiting"})
		# Asked-for RAWs go ahead of the JPEGs still waiting.
		self.assertTrue(queue._next()["path"].endswith("0410.CR2"))
		with mock.patch.object(handlers, "get_upload_queue", return_value=queue):
			self.assertEqual(handlers.uploadPending("raw")["released"], 1)

	def test_offline_uploads_carry_their_records(self) -> None:
		from utils import photos

		folder = os.path.join(self.dir, "offline", "2026-10-18")
		os.makedirs(folder)
		for name in ("M42_1.jpg", "M42_1.cr2"):
			with open(os.path.join(folder, name), "wb") as f:
				f.write(b"x")
		photos.record(folder, ["M42_1.jpg", "M42_1.cr2"], {"frame_type": "light", "pair": "M42_1", "iso": 800})
		queue = self.queue()
		self.assertEqual(queue.queue_folder(os.path.join(self.dir, "offline"), "42"), 2)
		meta = queue._load()[0]["meta"]
		self.assertEqual((meta["pair"], meta["iso"], meta["offline"]), ("M42_1", 800, True))


class LiveViewOptionTests(CameraTestCase):
	def test_jpeg_options(self) -> None:
		from core.camera.stream import StreamSettings, encode
		from utils import handlers
		from tests.helpers import jpeg

		reply = handlers.setLiveViewOptions(codec="jpeg", jpeg_quality=92, max_fps=2)
		self.assertEqual((reply["liveview"]["jpeg_quality"], reply["liveview"]["max_fps"]), (92, 2.0))
		self.assertEqual(StreamSettings.load().jpeg_quality, 92)
		self.assertIn("error", handlers.setLiveViewOptions(jpeg_quality=99))
		self.assertIn("error", handlers.setLiveViewOptions(max_fps=31))
		self.assertIn("error", handlers.setLiveViewOptions(max_fps=0.1))
		self.assertIn("error", handlers.setLiveViewOptions(codec="h264"))
		from PIL import Image

		frame = jpeg(320, 200)
		self.assertEqual(Image.open(io.BytesIO(encode(frame, "medium", jpeg_quality=92))).size, (320, 200))
		self.assertEqual(Image.open(io.BytesIO(encode(frame, "medium"))).size, (160, 100))
		self.assertIsNone(handlers.setLiveViewOptions(jpeg_quality=None)["liveview"]["jpeg_quality"])
		self.assertIsNone(StreamSettings.load().jpeg_quality)


class FakeFlatCamera:
	"""A shutter setting and a light: test shots are JPEGs whose level is
	exposure x brightness (linear), through the sRGB curve."""

	def __init__(self, brightness: float) -> None:
		self.brightness = brightness
		self.value = "1/30"
		self.choices = ["bulb", "30", "15", "8", "4", "2", "1", "1/2", "1/4", "1/8", "1/15", "1/30", "1/60",
			"1/125", "1/250", "1/500", "1/1000", "1/2000", "1/4000"]
		self.shots = []

	def get_setting(self, name, max_age_s=0.0):
		return {"value": self.value, "choices": self.choices}

	def set_setting(self, name, value):
		self.value = value

	def shoot(self, folder: str, bulb_s) -> list:
		from PIL import Image

		seconds = bulb_s or parse_shutter(self.value)
		level = min(1.0, seconds * self.brightness)
		srgb = 12.92 * level if level <= 0.0031308 else 1.055 * level ** (1 / 2.4) - 0.055
		path = os.path.join(folder, f"flat_{len(self.shots)}.jpg")
		Image.new("L", (64, 48), int(round(srgb * 255))).convert("RGB").save(path, quality=95)
		self.shots.append((seconds, level))
		return [path]


class FlatTests(ScratchConfig):
	def test_finds_half_full_and_follows_the_light(self) -> None:
		from utils.flats import AutoFlats

		camera = FakeFlatCamera(brightness=8.0)  # 1/15 s is about half full
		out = os.path.join(self.dir, "flats")
		os.makedirs(out)
		flats = AutoFlats(lambda bulb: camera.shoot(out, bulb), service=camera,
			test=lambda folder, bulb: camera.shoot(folder, bulb))
		flats.next_frame()
		self.assertLessEqual(flats.tests, 4)
		self.assertTrue(0.35 <= camera.shots[-1][1] <= 0.65, camera.shots)
		camera.brightness = 3.0  # twilight: darker
		flats.next_frame()
		flats.next_frame()
		self.assertTrue(0.35 <= camera.shots[-1][1] <= 0.65, camera.shots)
		flats.finish()
		self.assertEqual(camera.value, "1/30")  # put back

	def test_only_flats_take_auto(self) -> None:
		from utils.photos import SequencePlan

		self.assertIsNone(SequencePlan("flat", 20, "auto").check())
		self.assertIn("flats", SequencePlan("light", 20, "auto").check())
		self.assertIn("settle", SequencePlan("light", 5, 30.0, dither_every=1, dither_settle_s=200).check())


class DitherSettleTests(ScratchConfig):
	def test_waits_after_each_dither(self) -> None:
		from core.hardware import tracking
		from utils.photos import PhotoRun, SequencePlan

		run = PhotoRun()
		run.plan = SequencePlan("light", 3, 1.0, dither_every=1, dither_settle_s=0.3)
		with mock.patch.object(tracking, "is_sidereal_tracking", return_value=True), \
				mock.patch.object(tracking, "dither", return_value=True) as dither:
			started = time.monotonic()
			run._dither(1)
			self.assertGreaterEqual(time.monotonic() - started, 0.3)
		dither.assert_called_once_with(2.0)


class CentreTests(ScratchConfig):
	def test_centring_waits_for_the_slew(self) -> None:
		from core.hardware import tracking
		from utils import plate_solve

		saved = tracking._panel_state
		tracking._panel_state = "slewing"
		threading.Timer(0.3, lambda: setattr(tracking, "_panel_state", "tracking")).start()
		try:
			started = time.monotonic()
			plate_solve.wait_for_slew(timeout_s=5.0)
			self.assertGreaterEqual(time.monotonic() - started, 0.25)
			tracking._panel_state = "slewing"
			with self.assertRaises(plate_solve.SolveJobError):
				plate_solve.wait_for_slew(timeout_s=0.3)
		finally:
			tracking._panel_state = saved


class SerTests(CameraTestCase):
	def test_ser_header_frames_and_times(self) -> None:
		from core.camera.ser import HEADER_BYTES, SerWriter

		path = os.path.join(self.dir, "t.ser")
		writer = SerWriter(path, 4, 2, "RGB")
		for n in range(3):
			writer.add(bytes([n]) * 24)
		writer.close()
		with open(path, "rb") as f:
			data = f.read()
		self.assertEqual(data[:14], b"LUCAM-RECORDER")
		_lu, colour, _le, w, h, depth, frames = struct.unpack("<7i", data[14:42])
		self.assertEqual((colour, w, h, depth, frames), (100, 4, 2, 8, 3))
		self.assertEqual(len(data), HEADER_BYTES + 3 * 24 + 3 * 8)
		self.assertEqual(data[HEADER_BYTES + 24], 1)
		times = struct.unpack("<3q", data[-24:])
		self.assertTrue(times[0] <= times[1] <= times[2])
		self.assertGreater(times[0], 6.3e17)  # after 2000 in SER's ticks

	def test_lossless_recording_of_the_planet_crop(self) -> None:
		from core.camera.recorder import Recorder, planet_crop
		import numpy as np

		frame = np.zeros((200, 320, 3), dtype=np.uint8)
		frame[40:60, 250:270] = 230  # the planet
		self.assertEqual(planet_crop(frame, 100, 80), (210, 10, 100, 80))
		self.assertEqual(planet_crop(frame, 640, 480), (0, 0, 320, 200))

		queue = self.queue()
		recorder = Recorder(self.service)
		with mock.patch("utils.uploads.get_upload_queue", return_value=queue):
			recorder.start("video", quality="lossless", owner="42", crop_size=(64, 48))
			self.assertTrue(wait_for(lambda: recorder.frames >= 5, timeout=10.0))
			recorder.stop()
		self.assertTrue(recorder.path.endswith(".ser"))
		with open(recorder.path, "rb") as f:
			data = f.read(42)
		_lu, colour, _le, w, h, depth, frames = struct.unpack("<7i", data[14:42])
		self.assertEqual((colour, w, h, depth), (100, 64, 48, 8))
		self.assertGreaterEqual(frames, 5)
		self.assertEqual(os.path.getsize(recorder.path), 178 + frames * (64 * 48 * 3 + 8))
		entry = queue._load()[0]
		self.assertEqual(entry["kind"], "video")
		meta = entry["meta"]
		self.assertEqual(meta["frame_type"], "video")
		self.assertEqual((meta["crop"]["width"], meta["crop"]["height"]), (64, 48))
		self.assertTrue(0 <= meta["crop"]["x"] <= 1 and 0 <= meta["crop"]["y"] <= 1)
		self.assertGreater(meta["fps"], 0)
		self.assertTrue(re.match(r"^\d{4}-.*\.\d{3}Z$", meta["captured"]))
		with self.assertRaises(ValueError):
			recorder.start("timelapse", quality="lossless")
