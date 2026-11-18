"""The offline hotspot: its settings, which server the client talks to, the
phone's settings page and resumable uploads.

Nothing here touches the Wi-Fi, sudo or the network beyond the loopback."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import threading
import unittest
from unittest import mock

from tests.helpers import ScratchConfig

import utils.config_state as config_state


class HotspotSettings(ScratchConfig):
	def test_names_and_passwords(self) -> None:
		from core.networking import hotspot

		self.assertIsNone(hotspot.validate_name("Garden Dob"))
		self.assertIsNotNone(hotspot.validate_name(""))
		self.assertIsNotNone(hotspot.validate_name("x" * 33))
		self.assertIsNotNone(hotspot.validate_name("bad\nname"))
		self.assertIsNone(hotspot.validate_password("12345678"))
		self.assertIsNotNone(hotspot.validate_password("1234567"))
		self.assertIsNotNone(hotspot.validate_password("é" * 9))

	def test_reading_saves_nothing(self) -> None:
		from core.networking import hotspot

		before = config_state.load_static_state().get("hotspot")
		self.assertEqual(hotspot.name(), "test")  # the device name
		self.assertEqual(hotspot.password(), "")
		self.assertEqual(config_state.load_static_state().get("hotspot"), before)

	def test_credentials_are_made_once(self) -> None:
		from core.networking import hotspot

		hotspot.ensure_credentials()
		first = hotspot.password()
		self.assertIsNone(hotspot.validate_password(first))
		config_state.save_client_config({"client_id": "Renamed"})
		hotspot.ensure_credentials()
		self.assertEqual(hotspot.password(), first)
		self.assertEqual(hotspot.name(), "test")  # kept, not the new device name

	def test_set_name_restarts_it(self) -> None:
		from core.networking import hotspot

		with mock.patch.object(hotspot, "apply") as apply:
			self.assertIsNotNone(hotspot.set_name("no/slashes"))
			apply.assert_not_called()
			self.assertIsNone(hotspot.set_name("Field Scope", delay_s=2.0))
			apply.assert_called_once_with(delay_s=2.0)
		self.assertEqual(hotspot.name(), "Field Scope")

	def test_without_the_helper_wifi_is_left_alone(self) -> None:
		from core.networking import hotspot

		with mock.patch.object(hotspot, "helper_installed", return_value=False), \
				mock.patch.object(hotspot.subprocess, "run") as run:
			hotspot._up()
		run.assert_not_called()
		self.assertEqual(hotspot.state["state"], "not set up")
		hotspot.state.update(state="off", detail="")

	def test_helper_gets_the_password_on_stdin(self) -> None:
		from core.networking import hotspot

		done = mock.Mock(returncode=0, stdout="{}", stderr="")
		with mock.patch.object(hotspot, "helper_installed", return_value=True), \
				mock.patch.object(hotspot.subprocess, "run", return_value=done) as run:
			hotspot._up()
		args, kwargs = run.call_args
		self.assertEqual(args[0], ["sudo", "-n", hotspot.HELPER, "up"])
		sent = json.loads(kwargs["input"])
		self.assertEqual(sent["ssid"], "test")
		self.assertEqual(sent["password"], hotspot.password())
		self.assertNotIn(sent["password"], " ".join(args[0]))
		self.assertEqual(hotspot.state["state"], "on")
		with mock.patch.object(hotspot.subprocess, "run", return_value=done):
			hotspot._down()
		self.assertEqual(hotspot.state["state"], "off")

	def test_the_telescopes_names_are_answered_on_the_hotspot(self) -> None:
		from core.networking import hotspot
		from utils import phone_credentials

		# Not the Pi's own credentials (config/phone_link/), if it has them
		with mock.patch.object(phone_credentials, "load", return_value=None):
			self.assertEqual(hotspot.dns_names(), [])  # no server set
			config_state.save_client_config({"base_url": "https://seleno.example"})
			self.assertEqual(hotspot.dns_names(), ["tel.seleno.example"])
		with mock.patch.object(phone_credentials, "load", return_value={"host": "dob-3.tel.other.example"}):
			self.assertEqual(hotspot.dns_names(), ["tel.other.example"])

	def test_the_helper_checks_what_it_is_given(self) -> None:
		import importlib.machinery
		import importlib.util

		path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "seleno-hotspot")
		loader = importlib.machinery.SourceFileLoader("seleno_hotspot_helper", path)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		helper = importlib.util.module_from_spec(spec)
		loader.exec_module(helper)  # defines functions only: main() needs root and argv
		good = {"ssid": "Dob", "password": "12345678", "dns_names": ["tel.seleno.org"]}
		self.assertEqual(helper.validate(good)["dns_names"], ["tel.seleno.org"])
		for bad in ({"dns_names": ["x;rm -rf /"]}, {"dns_names": ["seleno"]}, {"dns_names": "tel.seleno.org"},
				{"ssid": "bad\nname"}, {"password": "short"}, {"address": "8.8.8.8"}, {"interface": "eth0"}):
			with self.assertRaises(SystemExit):
				helper.validate(dict(good, **bad))


class HotspotFile(ScratchConfig):
	"""config/hotspot.json: the on/off, name and password, changeable by hand."""

	def read(self) -> dict:
		with open(config_state.hotspot_file()) as f:
			return json.load(f)

	def test_made_at_startup_with_the_name_and_password(self) -> None:
		from core.networking import hotspot

		self.assertEqual(os.path.dirname(config_state.hotspot_file()), self.dir)
		with mock.patch.object(hotspot, "apply"), mock.patch.object(hotspot.threading, "Thread"):
			hotspot.start()
		saved = self.read()
		self.assertEqual(set(saved), {"offline", "name", "password"})
		self.assertTrue(saved["offline"])  # moved over from client_profile.json
		self.assertNotIn("offline", config_state.load_client_profile()["client_config"])
		self.assertEqual(saved["name"], "test")
		self.assertIsNone(hotspot.validate_password(saved["password"]))
		self.assertEqual(os.stat(config_state.hotspot_file()).st_mode & 0o077, 0)  # it holds the password
		self.assertNotIn("password", config_state.load_static_state().get("hotspot", {}))

	def test_setters_save_there(self) -> None:
		from core.networking import hotspot

		with mock.patch.object(hotspot, "apply"):
			hotspot.set_name("Field Scope")
			hotspot.set_password("starry-night")
			config_state.set_offline_mode(False)
		self.assertEqual(self.read(), {"offline": False, "name": "Field Scope", "password": "starry-night"})
		self.assertFalse(hotspot.wanted())
		self.assertFalse(hotspot.check_file())  # its own saves aren't changes by hand

	def test_old_settings_move_over(self) -> None:
		state = config_state.load_static_state()
		state["hotspot"] = dict(state.get("hotspot", {}), name="Old Name", password="old-password", channel=11)
		config_state.save_static_state(state)
		self.assertEqual(config_state.get_section("hotspot")["name"], "Old Name")
		config_state.save_section("hotspot", {"country": "FR"})
		self.assertEqual(self.read()["name"], "Old Name")
		self.assertEqual(self.read()["password"], "old-password")
		section = config_state.load_static_state()["hotspot"]
		self.assertNotIn("name", section)
		self.assertEqual((section["channel"], section["country"]), (11, "FR"))

	def test_a_change_by_hand_is_applied(self) -> None:
		from core.networking import hotspot

		with mock.patch.object(hotspot, "apply"), mock.patch.object(hotspot.threading, "Thread"):
			hotspot.start()
		saved = self.read()
		saved.update(name="By Hand", offline="off")
		with open(config_state.hotspot_file(), "w") as f:
			json.dump(saved, f)
		with mock.patch.object(hotspot, "apply") as apply:
			self.assertTrue(hotspot.check_file())
			apply.assert_called_once_with()
			self.assertFalse(hotspot.check_file())
		self.assertEqual(hotspot.name(), "By Hand")
		self.assertFalse(config_state.is_offline_mode())
		# A password that won't do is replaced, and saved
		saved = self.read()
		saved["password"] = "short"
		with open(config_state.hotspot_file(), "w") as f:
			json.dump(saved, f)
		with mock.patch.object(hotspot, "apply"):
			self.assertTrue(hotspot.check_file())
		self.assertIsNone(hotspot.validate_password(self.read()["password"]))
		self.assertEqual(hotspot.password(), self.read()["password"])


class ServerLink(ScratchConfig):
	profile = {"client_config": {"client_id": "Dob", "base_url": "https://seleno.example/", "api_token": "tok-123456",
		"offline": False}}

	def test_online_is_the_panel_server(self) -> None:
		from utils import server_link

		link = server_link.current()
		self.assertEqual(link["kind"], "server")
		self.assertEqual(link["client_id"], "Dob")
		self.assertEqual(link["server_uri"], "wss://ws.seleno.example/")

	def test_offline_there_is_no_server(self) -> None:
		from utils import server_link

		config_state.set_offline_mode(True)
		self.assertIsNone(server_link.current())
		# The panel's server stays known, for fetching the phone link's credentials
		self.assertEqual(server_link.online_link()["kind"], "server")


class RemoteSettings(ScratchConfig):
	def test_describes_the_panel_settings(self) -> None:
		from utils import remote_settings

		described = remote_settings.describe()
		keys = {item["key"]: item for s in described["sections"] for item in s["items"]}
		for key in ("hotspot.name", "hotspot.password", "site", "clock", "horizon.min_alt_deg", "tracking.rate",
				"liveview.quality", "upload.raw"):
			self.assertIn(key, keys)
		self.assertEqual(keys["tracking.rate"]["type"], "choice")
		self.assertNotIn("token", json.dumps(described))
		json.dumps(described)  # it goes over the socket

	def test_changes_and_refusals(self) -> None:
		from utils import remote_settings

		result = remote_settings.apply({"horizon.min_alt_deg": 12, "upload.raw": "never",
			"tracking.auto_rate": False, "horizon.min_alt_deg_typo": 1, "mount.hemisphere": "east"})
		self.assertEqual(sorted(result["applied"]), ["horizon.min_alt_deg", "tracking.auto_rate", "upload.raw"])
		self.assertIn("horizon.min_alt_deg_typo", result["errors"])
		self.assertIn("mount.hemisphere", result["errors"])
		self.assertEqual(config_state.get_section("horizon")["min_alt_deg"], 12.0)
		self.assertEqual(config_state.get_section("upload")["raw"], "never")
		self.assertFalse(config_state.get_section("tracking")["auto_rate"])
		self.assertIn("sections", result["settings"])
		self.assertIn("error", remote_settings.apply({}))

	def test_site_and_clock(self) -> None:
		from utils import clock, remote_settings

		with mock.patch("utils.location.set_manual_location") as set_location, \
				mock.patch.object(clock, "set_time", return_value=0.0) as set_time:
			result = remote_settings.apply({"site": "40.5, -3.7", "clock": "2026-11-18T21:30:00.123Z"})
		self.assertEqual(result["errors"], {})
		set_location.assert_called_once_with(40.5, -3.7)
		when = set_time.call_args[0][0]
		self.assertEqual((when.hour, when.minute, when.utcoffset().total_seconds()), (21, 30, 0))
		self.assertIn("clock", remote_settings.apply({"clock": "soon"})["errors"])

	def test_hotspot_changes_say_to_rejoin(self) -> None:
		from core.networking import hotspot
		from utils import remote_settings

		config_state.set_offline_mode(True)
		with mock.patch.object(hotspot, "apply") as apply:
			result = remote_settings.apply({"hotspot.password": "new-password-1"})
		self.assertEqual(result["applied"], ["hotspot.password"])
		self.assertIn("join it again", result["notice"])
		apply.assert_called_once_with(delay_s=hotspot.APPLY_DELAY_S)
		self.assertEqual(hotspot.password(), "new-password-1")


# --- uploads --------------------------------------------------------------------------------

class FakeTus(http.server.BaseHTTPRequestHandler):
	"""Just enough of the server's /api/telescope/uploads."""
	uploads: dict = {}
	finished: dict = {}
	fail_next_patch = False

	def log_message(self, *args) -> None:
		pass

	def _reply(self, status: int, headers: dict = None) -> None:
		self.send_response(status)
		for k, v in (headers or {}).items():
			self.send_header(k, str(v))
		self.send_header("Content-Length", "0")
		self.end_headers()

	def do_POST(self) -> None:
		assert self.headers["Authorization"] == "Bearer tok-123456"
		import base64

		meta = {}
		for pair in self.headers["Upload-Metadata"].split(","):
			k, _, v = pair.partition(" ")
			meta[k] = base64.b64decode(v).decode()
		upload_id = str(len(self.uploads) + 1)
		self.uploads[upload_id] = {"length": int(self.headers["Upload-Length"]), "data": b"", "meta": meta}
		self._reply(201, {"Location": f"/api/telescope/uploads/{upload_id}"})

	def do_HEAD(self) -> None:
		upload = self.uploads[self.path.rsplit("/", 1)[1]]
		self._reply(200, {"Upload-Offset": len(upload["data"]), "Upload-Length": upload["length"]})

	def do_PATCH(self) -> None:
		upload = self.uploads[self.path.rsplit("/", 1)[1]]
		body = self.rfile.read(int(self.headers["Content-Length"]))
		if int(self.headers["Upload-Offset"]) != len(upload["data"]):
			self._reply(409, {"Upload-Offset": len(upload["data"])})
			return
		if FakeTus.fail_next_patch:
			FakeTus.fail_next_patch = False
			upload["data"] += body[: len(body) // 2]  # half arrived, then the link dropped
			self._reply(500)
			return
		upload["data"] += body
		if len(upload["data"]) == upload["length"]:
			self.finished[upload["meta"]["filename"]] = upload
		self._reply(204, {"Upload-Offset": len(upload["data"])})


class Uploads(ScratchConfig):
	def setUp(self) -> None:
		super().setUp()
		self.in_scratch_dir()
		FakeTus.uploads, FakeTus.finished = {}, {}
		self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTus)
		threading.Thread(target=self.server.serve_forever, daemon=True).start()
		port = self.server.server_address[1]
		config_state.save_client_config({"client_id": "Dob", "api_token": "tok-123456",
			"base_url": f"http://127.0.0.1:{port}/", "offline": False})

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		super().tearDown()

	def test_resumable_upload_carries_on_after_a_cut(self) -> None:
		from utils import uploads

		data = os.urandom(uploads.CHUNK_BYTES + 1000)
		path = os.path.join(self.dir, "IMG_1.CR2")
		with open(path, "wb") as f:
			f.write(data)
		FakeTus.fail_next_patch = True
		with self.assertRaises(RuntimeError):
			uploads._default_sender(path, "7_IMG_1.CR2", {"captured": "2026-11-18T21:30:00.000Z", "exposure_s": 30})
		uploads._default_sender(path, "7_IMG_1.CR2", {"captured": "2026-11-18T21:30:00.000Z"})
		self.assertEqual(len(FakeTus.uploads), 1)  # carried on, not started again
		done = FakeTus.finished["7_IMG_1.CR2"]
		self.assertEqual(done["data"], data)
		self.assertEqual(done["meta"]["owner"], "7")
		self.assertEqual(done["meta"]["kind"], "raw")
		self.assertEqual(done["meta"]["sha256"], hashlib.sha256(data).hexdigest())

	def test_each_file_goes_only_to_its_own_server(self) -> None:
		from utils.uploads import UploadQueue

		link = {"now": "hotspot"}
		sent = []
		queue = UploadQueue(folder=self.dir, sender=lambda p, n, m: sent.append(n), online=lambda: True,
			autostart=False, link=lambda: link["now"])
		path = os.path.join(self.dir, "a.jpg")
		open(path, "wb").close()
		queue.add_many([path], "1")
		link["now"] = "server"
		self.assertFalse(queue.run_once())  # the hotspot's photo doesn't go to seleno.org
		queue.add_many([path], "42")       # "Upload offline", back online
		self.assertTrue(queue.run_once())
		self.assertEqual(sent, ["42_a.jpg"])
		link["now"] = "hotspot"
		self.assertTrue(queue.run_once())
		self.assertEqual(sent, ["42_a.jpg", "1_a.jpg"])


if __name__ == "__main__":
	unittest.main()
