"""The astronomy clock, the runtime store, the upload queue and GPS."""
from __future__ import annotations

import json
import os
import time


def read_json(path):
	with open(path) as f:
		return json.load(f)
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.helpers import ScratchConfig, wait_for

import utils.config_state as config_state
from utils import clock


class ClockTests(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		clock._loaded = True
		clock._offset_s, clock._source = 0.0, "system"

	def tearDown(self) -> None:
		clock._offset_s, clock._source = 0.0, "system"
		super().tearDown()

	def test_an_offset_counts_only_while_the_system_clock_isnt_synced(self) -> None:
		later = datetime.now(timezone.utc) + timedelta(hours=2)
		with mock.patch.object(clock, "system_synced", return_value=False):
			clock.set_time(later, "manual")
			self.assertAlmostEqual((clock.now() - later).total_seconds(), 0.0, delta=1.0)
			self.assertTrue(clock.trusted())
			self.assertEqual(clock.short_status(), "set by hand")
		with mock.patch.object(clock, "system_synced", return_value=True):
			self.assertLess(abs((clock.now() - datetime.now(timezone.utc)).total_seconds()), 1.0)
			self.assertEqual(clock.short_status(), "synced")
		with mock.patch.object(clock, "system_synced", return_value=False):
			clock.clear()
			self.assertFalse(clock.trusted())
			self.assertEqual(clock.short_status(), "NOT SET")

	def test_parse(self) -> None:
		self.assertEqual(clock.parse_utc("2026-10-16 21:30"), datetime(2026, 10, 16, 21, 30, tzinfo=timezone.utc))
		self.assertEqual(clock.parse_utc("2026-10-16T21:30:05Z").second, 5)
		self.assertEqual(clock.parse_utc("21:30").hour, 21)
		self.assertEqual((clock.parse_utc("10-17 01:05").day, clock.parse_utc("10-17 01:05").minute), (17, 5))
		with self.assertRaises(ValueError):
			clock.parse_utc("tomorrow")

	def test_kernel_sync_state_is_readable(self) -> None:
		clock._synced_cache = (None, -1e9)
		self.assertIn(clock.system_synced(), (True, False, None))


class RuntimeStoreTests(ScratchConfig):
	def test_writes_are_batched(self) -> None:
		store = config_state.runtime_store()
		path = config_state.RUNTIME_STATE_FILE
		before = os.path.getmtime(path)
		store.update({"axis_ra_deg": 1.0}, when="soon")
		store.update({"axis_ra_deg": 2.0}, when="soon")
		store.update({"current_hour_angle": 5.0}, when="never")
		self.assertEqual(read_json(path).get("axis_ra_deg"), None)  # not yet
		store.flush()
		saved = read_json(path)
		self.assertEqual((saved["axis_ra_deg"], saved["current_hour_angle"]), (2.0, 5.0))
		self.assertEqual(store.get("axis_ra_deg"), 2.0)
		store.update({"target_declination": 7.0}, when="now")
		self.assertEqual(read_json(path)["target_declination"], 7.0)
		self.assertGreaterEqual(os.path.getmtime(path), before)

	def test_a_soon_write_lands_by_itself(self) -> None:
		store = config_state.runtime_store()
		with mock.patch.object(config_state.RuntimeStore, "FLUSH_DELAY_S", 0.1):
			store._timer = None
			store.update({"axis_dec_deg": 3.0}, when="soon")
			self.assertTrue(wait_for(lambda: read_json(config_state.RUNTIME_STATE_FILE).get("axis_dec_deg") == 3.0))

	def test_stale_temp_files_go(self) -> None:
		stale = config_state.RUNTIME_STATE_FILE + ".1.2.tmp"
		open(stale, "w").close()
		os.utime(stale, (time.time() - 3600, time.time() - 3600))
		fresh = config_state.RUNTIME_STATE_FILE + ".3.4.tmp"
		open(fresh, "w").close()
		self.assertEqual(config_state.remove_stale_temp_files(), 1)
		self.assertFalse(os.path.exists(stale))
		self.assertTrue(os.path.exists(fresh))

	def test_no_built_in_server(self) -> None:
		self.assertEqual(config_state._normalize_base_url(""), "")
		with self.assertRaises(ValueError):
			config_state.build_service_urls("")
		self.assertEqual(config_state.build_service_urls("seleno.org")["server_uri"], "wss://ws.seleno.org/")


class TelescopeStateTests(ScratchConfig):
	def test_coordinates_round_trip_through_the_axes(self) -> None:
		from utils import telescope_state as ts

		self.assertIsNone(ts.get_telescope_coords())  # nothing known yet
		ts.set_telescope_coords(83.82, -5.39, source="test")
		back = ts.get_telescope_coords()
		self.assertAlmostEqual(back["right_ascension"], 83.82, places=3)
		self.assertAlmostEqual(back["declination"], -5.39, places=3)
		ts.set_parked(True)
		self.assertTrue(ts.get_parked())

	def test_an_alignment_point_corrects_the_position(self) -> None:
		from utils import telescope_state as ts

		ts.set_telescope_coords(83.82, -5.39, source="test")
		ts.add_alignment_point(84.32, -5.09, name="test")  # it was really pointing here
		back = ts.get_telescope_coords()
		self.assertAlmostEqual(back["right_ascension"], 84.32, places=2)
		self.assertAlmostEqual(back["declination"], -5.09, places=2)
		self.assertEqual(len(ts.get_alignment().points), 1)
		ts.clear_alignment()
		self.assertAlmostEqual(ts.get_telescope_coords()["right_ascension"], 83.82, places=2)

	def test_the_hemisphere(self) -> None:
		from utils import telescope_state as ts
		from utils.config_state import save_location, save_section

		self.assertEqual(ts.hemisphere(), "north")
		save_location({"latitude": -33.9, "longitude": 18.4})
		self.assertEqual(ts.hemisphere(), "south")
		self.assertTrue(ts.mount_config().south)
		save_section("mount", {"hemisphere": "north"})
		self.assertEqual(ts.hemisphere(), "north")


class UploadQueueTests(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		from utils.uploads import UploadQueue

		self.sent = []
		self.failure = None
		self.online = True

		def sender(path, name, meta):
			if self.failure:
				raise self.failure
			self.sent.append(name)
		self.queue = UploadQueue(folder=self.dir, sender=sender, online=lambda: self.online, autostart=False)
		self.files = {}
		for name in ("1_a.jpg", "1_a.cr2", "1_b.jpg", "x.cr2"):
			path = os.path.join(self.dir, name)
			open(path, "wb").write(b"x")
			self.files[name] = path

	def drain(self) -> None:
		for _ in range(10):
			self.queue.run_once()

	def test_jpegs_go_before_raws(self) -> None:
		self.queue.add(self.files["1_a.cr2"], "1")
		self.queue.add(self.files["1_a.jpg"], "1")
		self.queue.add(self.files["1_b.jpg"], "1")
		self.drain()
		self.assertEqual(self.sent, ["1_a.jpg", "1_b.jpg", "1_a.cr2"])
		self.assertEqual(self.queue.status()["waiting"], 0)

	def test_names_carry_the_owner_and_bad_owners_are_refused(self) -> None:
		self.queue.add(self.files["x.cr2"], "42")
		self.assertEqual(self.queue.add(self.files["x.cr2"], "bob"), "No account id to upload under")
		self.drain()
		self.assertEqual(self.sent, ["42_x.cr2"])

	def test_raws_on_request_or_never(self) -> None:
		from utils.config_state import save_section

		save_section("upload", {"raw": "on_request"})
		self.queue.add(self.files["1_a.cr2"], "1")
		self.drain()
		self.assertEqual((self.sent, self.queue.status()["held"]), ([], 1))
		self.assertEqual(self.queue.release("raw"), 1)
		self.drain()
		self.assertEqual(self.sent, ["1_a.cr2"])
		save_section("upload", {"raw": "never"})
		self.assertEqual(self.queue.add(self.files["x.cr2"], "1"), "RAW uploads are off")

	def test_failures_wait_and_refusals_give_up(self) -> None:
		from utils.uploads import UploadRefused

		self.queue.add(self.files["1_a.jpg"], "1")
		self.failure = RuntimeError("network down")
		self.queue.run_once()
		entry = self.queue._load()[0]
		self.assertEqual(entry["tries"], 1)
		self.assertGreater(entry["next_try"], time.time())
		self.assertFalse(self.queue.run_once())  # not due yet
		self.failure = UploadRefused("400: bad name")
		for _ in range(3):
			entry["next_try"] = 0
			self.queue.run_once()
		self.assertEqual(self.queue.status()["failed"], 1)
		self.failure = None
		self.assertEqual(self.queue.retry_failed(), 1)
		self.queue.run_once()
		self.assertEqual(self.sent, ["1_a.jpg"])

	def test_nothing_goes_offline_and_the_queue_survives_a_restart(self) -> None:
		from utils.uploads import UploadQueue

		self.online = False
		self.queue.add(self.files["1_a.jpg"], "1")
		self.drain()
		self.assertEqual(self.sent, [])
		again = UploadQueue(folder=self.dir, sender=lambda p, n, m: self.sent.append(n), online=lambda: True, autostart=False)
		again.run_once()
		self.assertEqual(self.sent, ["1_a.jpg"])

	def test_a_folder_of_offline_photos_is_queued_once(self) -> None:
		night = os.path.join(self.dir, "offline", "2026-10-16")
		os.makedirs(night)
		for name in ("Vega_1.jpg", "Vega_1.cr2", "photos.jsonl"):
			open(os.path.join(night, name), "wb").write(b"x")
		self.assertEqual(self.queue.queue_folder(os.path.join(self.dir, "offline"), "7"), 2)
		self.assertEqual(self.queue.queue_folder(os.path.join(self.dir, "offline"), "7"), 0)
		self.drain()
		self.assertEqual(sorted(self.sent), ["7_Vega_1.cr2", "7_Vega_1.jpg"])


class GpsTests(ScratchConfig):
	profile = {"client_config": {"client_id": "t", "base_url": "", "api_token": "", "offline": True}, "location": {}}

	def test_a_fix_sets_the_site_and_the_clock(self) -> None:
		from utils import gps
		from utils.config_state import get_location

		gps._last_clock_set = 0.0
		clock._loaded = True
		stamp = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
		with mock.patch.object(clock, "system_synced", return_value=False), \
				mock.patch.object(clock, "set_time") as set_time:
			gps.handle_report({"class": "TPV", "mode": 3, "lat": 51.5, "lon": -0.1, "alt": 20, "time": stamp})
			set_time.assert_called_once()
		self.assertEqual(get_location()["source"], "hardware_gps")
		self.assertTrue(gps.has_fix())
		gps.handle_report({"class": "TPV", "mode": 1})  # no fix: ignored
		self.assertAlmostEqual(gps.last_fix()["latitude"], 51.5)

	def test_a_typed_site_stays(self) -> None:
		from utils import gps
		from utils.config_state import get_location, save_location

		save_location({"latitude": 10.0, "longitude": 10.0, "source": "manual"})
		gps.handle_report({"class": "TPV", "mode": 2, "lat": 51.5, "lon": -0.1})
		self.assertEqual(get_location()["latitude"], 10.0)


if __name__ == "__main__":
	unittest.main()
