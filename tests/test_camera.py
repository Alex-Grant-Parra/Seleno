"""The camera side: JPEG splitting, the AVI writer, the camera service,
the gphoto2 command backend (against a fake gphoto2) and the live view
policy. No camera needed."""
from __future__ import annotations

import os
import random
import stat
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import ScratchConfig, jpeg, wait_for

from core.camera.avi import MjpegAviWriter
from core.camera.backends import CameraError, CliBackend, FakeBackend
from core.camera.jpeg import JpegFramer, jpeg_size
from core.camera.service import CameraService
from core.camera.stream import LiveStream, StreamSettings, encode


def with_thumbnail(data: bytes, thumb: bytes) -> bytes:
	payload = b"Exif\x00\x00" + thumb
	return data[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + data[2:]


class JpegFramerTests(unittest.TestCase):
	def test_frames_with_thumbnails_and_garbage_in_any_chunks(self) -> None:
		thumb = jpeg(40, 30)
		frames = [with_thumbnail(jpeg(320, 200, 1), thumb), jpeg(64, 48, 2, progressive=True),
			with_thumbnail(jpeg(100, 80, 3), thumb)]
		stream = b"noise" + frames[0] + frames[1] + b"\x00\xffjunk" + frames[2]
		rng = random.Random(4)
		for _ in range(30):
			framer, out, i = JpegFramer(), [], 0
			while i < len(stream):
				k = rng.randint(1, 4000)
				out += framer.feed(stream[i:i + k])
				i += k
			self.assertEqual(out, frames)

	def test_a_cut_off_frame_is_dropped_not_joined(self) -> None:
		a, b = jpeg(80, 60, 1), jpeg(80, 60, 2)
		framer = JpegFramer()
		out = framer.feed(a[: len(a) // 2] + b)
		self.assertEqual(out, [b])
		self.assertEqual(framer.dropped, 1)

	def test_too_big_is_given_up(self) -> None:
		framer = JpegFramer(max_frame_bytes=1000)
		self.assertEqual(framer.feed(jpeg(320, 200)), [])
		self.assertGreaterEqual(framer.dropped, 1)
		small = jpeg(16, 16, quality=20)
		self.assertLess(len(small), 1000)
		self.assertEqual(framer.feed(small), [small])

	def test_size(self) -> None:
		self.assertEqual(jpeg_size(jpeg(321, 123)), (321, 123))
		self.assertEqual(jpeg_size(with_thumbnail(jpeg(200, 100), jpeg(40, 30))), (200, 100))
		self.assertIsNone(jpeg_size(b"not a jpeg"))


def read_avi(path: str):
	with open(path, "rb") as f:
		data = f.read()
	assert data[:4] == b"RIFF" and data[8:12] == b"AVI "
	riff_size = struct.unpack_from("<I", data, 4)[0]
	total_frames = struct.unpack_from("<I", data, 48)[0]
	width, height = struct.unpack_from("<II", data, 64)
	movi = data.find(b"movi")
	idx = data.find(b"idx1", movi)
	entries = struct.unpack_from("<I", data, idx + 4)[0] // 16
	frames = []
	for i in range(entries):
		ckid, flags, offset, size = struct.unpack_from("<4sIII", data, idx + 8 + 16 * i)
		start = movi + offset + 8
		frames.append(data[start:start + size])
	return riff_size, len(data), total_frames, (width, height), frames


class AviTests(unittest.TestCase):
	def test_frames_round_trip(self) -> None:
		frames = [jpeg(160, 120, seed) for seed in range(5)]
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "t.avi")
			writer = MjpegAviWriter(path, fps=10)
			for frame in frames:
				writer.add(frame)
			writer.close()
			riff_size, length, total, size, back = read_avi(path)
		self.assertEqual(riff_size, length - 8)
		self.assertEqual(total, 5)
		self.assertEqual(size, (160, 120))
		self.assertEqual(back, frames)


class CameraServiceTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = tempfile.TemporaryDirectory()
		self.backend = FakeBackend(width=320, height=200, fps=30)
		self.service = CameraService(backend=self.backend)
		self.service.start()
		self.assertTrue(wait_for(lambda: self.service.connected))
		with mock.patch("core.camera.service.CameraService._keep_awake"):
			pass

	def tearDown(self) -> None:
		self.service.stop()
		self.tmp.cleanup()

	def frames(self, n: int, timeout: float = 5.0) -> int:
		seq, got = self.service.frames.latest()[0], 0
		deadline = time.monotonic() + timeout
		while got < n and time.monotonic() < deadline:
			new, _f, _a = self.service.frames.wait_newer(seq, 0.5)
			if new != seq:
				got += 1
				seq = new
		return got

	def test_live_view_runs_while_held(self) -> None:
		self.service.acquire_preview("a")
		self.service.acquire_preview("b")
		self.assertEqual(self.frames(5), 5)
		self.service.release_preview("a")
		self.assertEqual(self.frames(3), 3)  # b still holds it
		self.service.release_preview("b")
		self.assertTrue(wait_for(lambda: not self.service.previewing))

	def test_a_photo_pauses_live_view_and_it_carries_on(self) -> None:
		self.service.acquire_preview("a")
		self.assertEqual(self.frames(2), 2)
		names = self.service.capture(self.tmp.name, "shot")
		self.assertEqual(sorted(names), ["shot.cr2", "shot.jpg"])
		self.assertEqual(self.frames(3), 3)

	def test_settings(self) -> None:
		self.assertEqual(self.service.setting_name("shutterspeed"), "shutterspeed")
		self.assertEqual(self.service.setting_name("zoom"), "eoszoom")
		self.assertIsNone(self.service.setting_name("nothing"))
		self.service.set_setting("iso", "1600")
		self.assertEqual(self.service.get_setting("iso")["value"], "1600")
		with self.assertRaises(CameraError):
			self.service.set_setting("iso", "99")

	def test_keep_awake_turns_auto_power_off_off_and_back(self) -> None:
		self.service.acquire_preview("a")
		self.assertTrue(wait_for(lambda: self.backend.config["autopoweroff"]["value"] == "0"))
		self.service.release_preview("a")
		self.assertTrue(wait_for(lambda: self.backend.config["autopoweroff"]["value"] == "30"))

	def test_failures_back_off_and_recover(self) -> None:
		self.service.acquire_preview("a")
		self.assertEqual(self.frames(1), 1)
		self.backend.fail_next_frames = 2
		self.assertEqual(self.frames(3, timeout=8.0), 3)
		self.assertGreaterEqual(self.service.stats["restarts"], 1)
		self.assertTrue(self.service.previewing)

	def test_unplugged_and_back(self) -> None:
		self.service.acquire_preview("a")
		self.assertEqual(self.frames(1), 1)
		self.backend.present = False
		self.assertTrue(wait_for(lambda: not self.service.connected, timeout=10))
		with self.assertRaises(CameraError):
			self.service.capture(self.tmp.name, "x")
		self.backend.present = True
		self.assertTrue(wait_for(lambda: self.service.connected, timeout=10))
		self.assertEqual(self.frames(2, timeout=8), 2)


FAKE_GPHOTO2 = r'''#!{python}
"""Just enough of gphoto2 for the command-line backend."""
import os, sys, time, io
args = sys.argv[1:]
if "--auto-detect" in args:
    print("Model                          Port")
    print("----------------------------------------------------------")
    print("Canon EOS Test                 usb:001,004")
elif "--capture-movie" in args:
    from PIL import Image
    for i in range(1000):
        b = io.BytesIO(); Image.new("RGB", (64, 48), (i % 255, 0, 0)).save(b, "JPEG")
        sys.stdout.buffer.write(b.getvalue()); sys.stdout.buffer.flush(); time.sleep(0.02)
elif "--capture-image-and-download" in args:
    pattern = args[args.index("--filename") + 1]
    for ext in ("jpg", "cr2"):
        path = pattern.replace("%C", ext)
        open(path, "wb").write(b"x")
        print("Saving file as " + path)
elif "--get-config" in args:
    name = args[args.index("--get-config") + 1]
    if name != "iso":
        print("*** Error: no such setting", file=sys.stderr); sys.exit(1)
    print("Label: ISO Speed\nType: RADIO\nCurrent: 800\nChoice: 0 100\nChoice: 1 800\nChoice: 2 Auto ISO\nEND")
elif "--set-config-value" in args or "--set-config" in args:
    pass
elif "--storage-info" in args:
    print("[Storage 0]\ntotalcapacity=30000000\nfree=12000000\nfreeimages=900")
'''


class CliBackendTests(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = tempfile.TemporaryDirectory()
		self.binary = os.path.join(self.tmp.name, "gphoto2")
		with open(self.binary, "w") as f:
			f.write(FAKE_GPHOTO2.replace("{python}", sys.executable))
		os.chmod(self.binary, os.stat(self.binary).st_mode | stat.S_IEXEC)
		self.backend = CliBackend(self.binary)

	def tearDown(self) -> None:
		self.backend.close()
		self.tmp.cleanup()

	def test_detect_capture_and_settings(self) -> None:
		self.assertEqual(self.backend.detect(), "Canon EOS Test")
		self.backend.open()
		names = self.backend.capture(self.tmp.name, "pic")
		self.assertEqual(names, ["pic.jpg", "pic.cr2"])
		info = self.backend.get_config("iso")
		self.assertEqual((info["value"], info["choices"]), ("800", ["100", "800", "Auto ISO"]))
		with self.assertRaises(CameraError):
			self.backend.get_config("nothing")
		self.assertEqual(self.backend.storage()["free_images"], 900)

	def test_live_view_frames_through_the_service(self) -> None:
		service = CameraService(backend=self.backend)
		service.start()
		try:
			self.assertTrue(wait_for(lambda: service.connected))
			service.acquire_preview("t")
			self.assertTrue(wait_for(lambda: service.stats["frames"] >= 5))
			_seq, frame, _at = service.frames.latest()
			self.assertEqual(jpeg_size(frame), (64, 48))
			names = service.capture(self.tmp.name, "during")  # stops the movie, then starts it again
			self.assertEqual(len(names), 2)
			before = service.stats["frames"]
			self.assertTrue(wait_for(lambda: service.stats["frames"] > before + 3))
		finally:
			service.stop()


class StreamTests(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		self.backend = FakeBackend(width=640, height=400, fps=40)
		self.service = CameraService(backend=self.backend)
		self.service.start()
		self.assertTrue(wait_for(lambda: self.service.connected))
		self.moving = False
		self.stream = LiveStream(service=self.service, activity=lambda: self.moving)

	def tearDown(self) -> None:
		self.stream.stop()
		self.service.stop()
		super().tearDown()

	def collect(self, seconds: float):
		out = []
		deadline = time.monotonic() + seconds
		while time.monotonic() < deadline:
			frame = self.stream.next_frame(0.2)
			if frame:
				out.append(frame)
		return out

	def test_presets_shrink_the_frames(self) -> None:
		original = self.backend.render()
		sizes = {level: len(encode(original, level)) for level in ("original", "high", "medium", "low")}
		self.assertEqual(sizes["original"], len(original))
		self.assertLess(sizes["medium"], sizes["high"])
		self.assertLess(sizes["low"], sizes["medium"])
		self.assertEqual(jpeg_size(encode(original, "medium")), (320, 200))
		self.assertEqual(jpeg_size(encode(original, "low")), (160, 100))

	def test_a_still_view_is_sent_slowly_and_a_moving_one_fast(self) -> None:
		self.backend.fps = 40
		self.backend.render = lambda drift=0.0, r=self.backend.render: r(0.0)  # nothing changes
		self.stream.settings = StreamSettings(quality="low", max_fps=10, still_fps=2, keyframe_s=1.0)
		self.stream.start()
		self.stream.settings = StreamSettings(quality="low", max_fps=10, still_fps=2, keyframe_s=1.0)
		self.collect(3.2)  # a new picture counts as a change: full rate for 3 s
		still = self.collect(3.0)
		self.assertLessEqual(len(still), 5)  # a keyframe a second, plus the first
		self.assertGreater(self.stream.counters["skipped_same"], 0)
		self.moving = True
		moving = self.collect(2.0)
		self.assertGreaterEqual(len(moving), 12)

	def test_a_slow_link_lowers_the_quality_and_a_quick_one_raises_it(self) -> None:
		self.stream.settings = StreamSettings(quality="high", max_fps=10)
		self.assertEqual(self.stream.level(), "high")
		for _ in range(3):
			self.stream.note_sent(50000, 1.5)
		self.assertIn(self.stream.level(), ("medium", "low", "lowest"))
		lowered = self.stream.level()
		with mock.patch("core.camera.stream.time.monotonic", side_effect=[1000.0 + i for i in range(100)]):
			for _ in range(40):
				self.stream.note_sent(5000, 0.001)
		self.assertNotEqual(self.stream.level(), lowered)

	def test_nobody_watching_pauses_and_lets_the_camera_rest(self) -> None:
		self.stream.start()
		self.stream.settings = StreamSettings(viewer_grace_s=0.2)
		self.assertTrue(wait_for(lambda: self.service.previewing))
		self.stream.set_viewers(0)
		time.sleep(0.3)
		self.stream.next_frame(0.1)
		self.assertTrue(self.stream.paused)
		self.assertTrue(wait_for(lambda: not self.service.previewing))
		self.stream.set_viewers(2)
		self.assertFalse(self.stream.paused)
		self.assertTrue(wait_for(lambda: self.service.previewing))

	def test_idle_stop(self) -> None:
		from utils import liveview_state
		from core.camera import stream as stream_module

		with mock.patch.object(stream_module, "_stream", self.stream):
			liveview_state.save_liveview_state(True)
			self.stream.settings = StreamSettings(idle_stop_min=0.005)  # 0.3 s
			stopped = []
			self.stream.on_auto_stop = stopped.append
			time.sleep(0.4)
			self.stream.next_frame(0.1)
			self.assertFalse(self.stream.enabled)
			self.assertTrue(stopped)

	def test_focus_mode_sends_a_full_size_crop(self) -> None:
		self.stream.start()
		self.stream.settings = StreamSettings(quality="low", max_fps=10)
		self.stream.set_focus(True)
		frame = None
		deadline = time.monotonic() + 5
		while frame is None and time.monotonic() < deadline:
			frame = self.stream.next_frame(0.5)
		self.assertEqual(jpeg_size(frame), (320, 320))
		self.assertIsNotNone(self.stream.focus_result.get("hfr"))


class FocusTests(unittest.TestCase):
	def test_hfr_gets_bigger_out_of_focus(self) -> None:
		from core.camera.focus import analyse

		backend = FakeBackend(width=480, height=320)
		sharp = analyse(backend.render())
		backend.blur = 2.5
		soft = analyse(backend.render())
		self.assertIsNotNone(sharp["hfr"])
		self.assertGreater(soft["hfr"], sharp["hfr"])
		self.assertGreater(sharp["sharpness"], soft["sharpness"])


class RecorderTests(ScratchConfig):
	def test_video_and_snapshot(self) -> None:
		from core.camera.recorder import Recorder, snapshot

		self.in_scratch_dir()
		service = CameraService(backend=FakeBackend(width=160, height=100, fps=30))
		service.start()
		try:
			self.assertTrue(wait_for(lambda: service.connected))
			recorder = Recorder(service=service)
			recorder.start("video")
			self.assertTrue(wait_for(lambda: recorder.frames >= 10))
			recorder.stop()
			_riff, _len, total, size, frames = read_avi(recorder.path)
			self.assertEqual((total, size), (recorder.frames, (160, 100)))
			self.assertFalse(service.previewing and "recording" in service.preview_holders())
			service.acquire_preview("t")
			self.assertTrue(wait_for(lambda: service.frames.latest()[1] is not None))
			path = snapshot(service)
			self.assertTrue(os.path.exists(path))
		finally:
			service.stop()


if __name__ == "__main__":
	unittest.main()
