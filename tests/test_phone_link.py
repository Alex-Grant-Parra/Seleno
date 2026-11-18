"""The seleno.org app talking straight to the telescope on the hotspot:
offline passes, the API that answers as seleno.org would
(utils/phone_api.py), and the HTTPS and live view servers
(core/networking/phone_link.py) over real TLS on the loopback."""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import ssl
import struct
import time
import unittest
import urllib.request
from unittest import mock

from tests.helpers import ScratchConfig, jpeg

SECRET = "s3cret-for-tests-0123456789abcdef"
ORIGIN = "https://seleno.example"


def make_pass(role="owner", user=42, telescope="Dob", secret=SECRET, exp_in=3600.0, nbf_in=-60.0):
	from core.networking import phone_link

	now = time.time()
	return phone_link.sign(secret, "v1", {"t": telescope, "u": user, "n": "alex", "r": role,
		"nbf": now + nbf_in, "exp": now + exp_in})


def self_signed(folder: str):
	from cryptography import x509
	from cryptography.hazmat.primitives import hashes, serialization
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.x509.oid import NameOID

	key = ec.generate_private_key(ec.SECP256R1())
	name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
	now = datetime.datetime.now(datetime.timezone.utc)
	cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
		.serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
		.not_valid_after(now + datetime.timedelta(days=30))
		.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
		.sign(key, hashes.SHA256()))
	cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
	with open(cert_path, "wb") as f:
		f.write(cert.public_bytes(serialization.Encoding.PEM))
	with open(key_path, "wb") as f:
		f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
			serialization.NoEncryption()))
	return cert_path, key_path


class Passes(unittest.TestCase):
	def test_good_and_bad(self) -> None:
		from core.networking import phone_link

		good = make_pass()
		self.assertEqual(phone_link.verify(SECRET, "v1", good, "Dob")["u"], 42)
		self.assertIsNone(phone_link.verify("other-secret", "v1", good, "Dob"))
		self.assertIsNone(phone_link.verify(SECRET, "v1", good, "Other scope"))
		self.assertIsNone(phone_link.verify(SECRET, "lv", good, "Dob"))
		self.assertIsNone(phone_link.verify(SECRET, "v1", make_pass(exp_in=-1), "Dob"))
		self.assertIsNone(phone_link.verify(SECRET, "v1", make_pass(nbf_in=3600), "Dob"))
		prefix, body, mac = good.split(".")
		tampered = phone_link._b64(json.dumps({"t": "Dob", "u": 1, "r": "owner", "exp": time.time() + 99}).encode())
		self.assertIsNone(phone_link.verify(SECRET, "v1", f"{prefix}.{tampered}.{mac}", "Dob"))
		for junk in ("", "v1", "v1.x.y", "a.b.c.d"):
			self.assertIsNone(phone_link.verify(SECRET, "v1", junk, "Dob"))


class Api(ScratchConfig):
	owner = {"u": 42, "n": "alex", "r": "owner"}
	shared = {"u": 7, "n": "sam", "r": "shared"}

	def setUp(self) -> None:
		super().setUp()
		self.in_scratch_dir()
		self.calls = []
		self.answers = {}

		def fake(name, *args, **kwargs):
			self.calls.append((name, args, kwargs))
			answer = self.answers.get(name, {"ok": True})
			return answer(*args, **kwargs) if callable(answer) else answer
		from utils import phone_api

		self.patch = mock.patch.object(phone_api, "call", side_effect=fake)
		self.patch.start()

	def tearDown(self) -> None:
		self.patch.stop()
		super().tearDown()

	def handle(self, method, path, body=None, claims=None, query=None):
		from utils import phone_api

		return phone_api.handle(method, path, query or {}, body or {}, claims or self.owner)

	def test_the_only_telescope(self) -> None:
		data = self.handle("GET", "/interface/get_telescopes").data
		self.assertEqual([t["telescope_id"] for t in data["telescopes"]], ["test"])
		self.assertTrue(data["telescopes"][0]["online"])
		data = self.handle("POST", "/interface/select_telescope", {"telescope_id": "test"}).data
		self.assertEqual(data["access"], {"control": True, "owner": True, "shared": False})
		data = self.handle("GET", "/interface/get_selected_telescope", claims=self.shared).data
		self.assertEqual(data["access"], {"control": True, "owner": False, "shared": True})

	def test_commands_as_the_server_sends_them(self) -> None:
		r = self.handle("POST", "/interface/command", {"action": "park"})
		self.assertEqual(r.data, {"status": "success", "result": {"ok": True}, "job": "park"})
		self.assertEqual(self.calls[-1][0], "park")
		self.handle("POST", "/interface/command", {"action": "set_tracking_rate", "params": {"rate": "lunar"}})
		self.assertEqual(self.calls[-1][:2], ("setTrackingRate", ("lunar", None)))
		r = self.handle("POST", "/interface/command", {"action": "set_tracking_rate", "params": {"rate": "warp"}})
		self.assertEqual(r.status, 400)
		r = self.handle("POST", "/interface/command", {"action": "set_clock"}, claims=self.shared)
		self.assertEqual(r.status, 403)
		self.answers["park"] = {"error": "Parked already"}
		r = self.handle("POST", "/interface/command", {"action": "park"})
		self.assertEqual(r.data["status"], "error")
		self.assertEqual(r.data["message"], "Parked already")
		self.answers["getMountStatus"] = {"error": "no ESP32", "parked": False}
		r = self.handle("POST", "/interface/command", {"action": "mount_status"})
		self.assertEqual(r.data["status"], "success")  # plain: data with an error field is still data

	def test_motors_photos_and_tracking(self) -> None:
		self.handle("POST", "/interface/motor_command", {"command": "move_steps", "args": ["200", 400, True],
			"motor_id": "motor2"})
		self.assertEqual(self.calls[-1], ("espMoveSteps", (200, 400.0, True), {"motor_id": "motor2"}))
		self.handle("POST", "/interface/take_photo", {"exposure_s": 30, "frame_type": "test"})
		self.assertEqual(self.calls[-1], ("capturePhoto", ("42", 30.0), {"frame_type": "test"}))
		self.assertEqual(self.handle("POST", "/interface/take_photo", {"exposure_s": 901}).status, 400)
		r = self.handle("POST", "/track_star", {"name": "Vega", "ra": 279.2, "dec": 38.8, "mag": 0.0})
		self.assertEqual(r.data["status"], "tracking")
		self.assertEqual(self.calls[-1][:2], ("trackCoordinates", ("Vega", 279.2, 38.8, 0.0)))
		self.handle("POST", "/track_star", {"name": "Jupiter", "ra": 1, "dec": 2})
		self.assertEqual(self.calls[-1][:2], ("trackObject", ("Jupiter",)))
		self.assertTrue(self.handle("GET", "/get_tracking_status").data["tracking"])
		self.assertFalse(self.handle("GET", "/get_tracking_status", claims=self.shared).data["tracking"])
		self.answers["getCurrentCoordinates"] = {"current_right_ascension": 10.5, "current_declination": -3}
		self.assertEqual(self.handle("GET", "/api/telescope_position").data["ra"], 10.5)

	def test_sky_targets_and_search(self) -> None:
		from utils import offline_pack

		offline_pack.save({"featured_targets": [
			{"designation": "M42", "name": "Orion Nebula", "track_name": "Orion Nebula", "kind": "Nebula",
				"ra": 83.82, "dec": -5.39},
			{"designation": "jupiter", "name": "Jupiter", "track_name": "Jupiter", "kind": "Planet"}],
			"objects": [["NGC0224", "Andromeda Galaxy", 10.68, 41.27, 3.4, ["Andromeda Galaxy", "M31"]],
				["NGC 7000", "North America Nebula", 314.7, 44.3, 4.0]]})
		sky = self.handle("GET", "/interface/api/sky", query={"target": "M42"}).data
		self.assertEqual(sky["status"], "success")
		self.assertIn("moon", sky)
		self.assertEqual(sky["target"]["name"], "M42")
		targets = self.handle("GET", "/interface/api/targets").data["targets"]
		self.assertEqual({t["designation"] for t in targets}, {"M42", "jupiter"})
		self.assertTrue(all("verdict" in t for t in targets))
		found = self.handle("POST", "/interface/search_object", {"searchValue": "andromeda galaxy"}).data
		self.assertEqual(found["data"]["Name"], "NGC0224")
		self.assertEqual(self.handle("POST", "/interface/search_object", {"searchValue": "M 31"}).data["data"]["Name"],
			"NGC0224")
		self.assertEqual(self.handle("POST", "/interface/search_object", {"searchValue": "ngc7000"}).data["data"]["RA"], 314.7)
		self.assertEqual(self.handle("POST", "/interface/search_object", {"searchValue": "saturn"}).data["data"]["Name"],
			"Saturn")
		self.assertEqual(self.handle("POST", "/interface/search_object", {"searchValue": "zzzz"}).data["status"], "error")

	def test_album_from_the_pis_photos(self) -> None:
		from utils import photos

		night = os.path.join(photos.local_dir(), "2026-11-18")
		os.makedirs(night)
		with open(os.path.join(night, "m42_1.jpg"), "wb") as f:
			f.write(jpeg(800, 500))
		with open(os.path.join(night, "m42_1.cr2"), "wb") as f:
			f.write(b"II*\x00" + b"\x00" * 100)
		photos.record(night, ["m42_1.jpg", "m42_1.cr2"], {"target": "M42", "exposure_s": 30, "frame_type": "light"})
		frames = self.handle("GET", "/album/api/library").data["frames"]
		self.assertEqual(len(frames), 1)  # the RAW goes with its JPEG
		frame = frames[0]
		self.assertEqual(frame["target"], "M42")
		self.assertTrue(frame["raw"].endswith("m42_1.cr2"))
		thumb = self.handle("GET", frame["thumb"].split("?")[0], query={"size": "sm"})
		self.assertEqual(thumb.content_type, "image/jpeg")
		self.assertTrue(os.path.exists(thumb.file))
		self.assertTrue(self.handle("GET", frame["url"]).file.endswith("m42_1.jpg"))
		self.assertEqual(self.handle("GET", "/album/photo/..~..~config~client_config.json").status, 404)
		latest = self.handle("GET", "/interface/api/latest_captures", query={"limit": "5"}).data["frames"]
		self.assertEqual(len(latest), 1)

	def test_what_needs_seleno_org(self) -> None:
		self.assertEqual(self.handle("POST", "/interface/capture/start", {}).status, 503)
		self.assertEqual(self.handle("GET", "/interface/capture/presets").data["presets"], [])
		self.assertEqual(self.handle("POST", "/album/api/stacks", {}).status, 503)
		self.assertEqual(self.handle("GET", "/somewhere/else").status, 404)

	def test_settings_are_the_owners(self) -> None:
		self.answers["getSettings"] = {"sections": []}
		self.assertEqual(self.handle("GET", "/interface/telescope_settings/data").data["settings"], {"sections": []})
		self.assertEqual(self.handle("GET", "/interface/telescope_settings/data", claims=self.shared).status, 403)


class Servers(ScratchConfig):
	"""The real HTTPS and WSS servers, on the loopback, with a self-signed cert."""

	def setUp(self) -> None:
		super().setUp()
		self.in_scratch_dir()
		from core.networking import phone_link

		cert, key = self_signed(self.dir)
		self.creds = {"telescope_id": "Dob", "host": "localhost", "secret": SECRET, "origins": [ORIGIN],
			"cert": cert, "key": key}
		self.patches = [mock.patch.object(phone_link, "credentials", return_value=self.creds),
			mock.patch.object(phone_link, "wanted", return_value=True)]
		for p in self.patches:
			p.start()
		self.api_port, self.live_port = _free_port(), _free_port()
		phone_link.apply(self.api_port, self.live_port)
		self.assertEqual(phone_link.state["state"], "on", phone_link.state)
		self.tls = ssl.create_default_context(cafile=cert)

	def tearDown(self) -> None:
		from core.networking import phone_link

		phone_link.stop()
		for p in self.patches:
			p.stop()
		super().tearDown()

	def request(self, path, method="GET", headers=None, body=None):
		req = urllib.request.Request(f"https://localhost:{self.api_port}{path}", method=method, data=body,
			headers=headers or {})
		try:
			with urllib.request.urlopen(req, context=self.tls, timeout=10) as resp:
				return resp.status, dict(resp.headers), resp.read()
		except urllib.error.HTTPError as exc:
			return exc.code, dict(exc.headers), exc.read()

	def test_hello_cors_and_passes(self) -> None:
		status, _h, body = self.request("/hello")
		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body)["telescope"], "Dob")
		status, headers, _b = self.request("/interface/get_telescopes", "OPTIONS", {"Origin": ORIGIN,
			"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-seleno-pass",
			"Access-Control-Request-Private-Network": "true"})
		self.assertEqual(status, 204)
		self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
		self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")
		status, _h, _b = self.request("/interface/get_telescopes", "OPTIONS", {"Origin": "https://evil.example"})
		self.assertEqual(status, 403)
		status, _h, _b = self.request("/interface/get_telescopes", headers={"Origin": ORIGIN})
		self.assertEqual(status, 401)
		status, headers, body = self.request("/interface/get_telescopes", headers={"Origin": ORIGIN,
			"X-Seleno-Pass": make_pass()})
		self.assertEqual(status, 200)
		self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
		self.assertEqual(len(json.loads(body)["telescopes"]), 1)
		status, _h, _b = self.request("/interface/get_telescopes", headers={"X-Seleno-Pass": make_pass(telescope="Other")})
		self.assertEqual(status, 401)

	def test_files_by_range(self) -> None:
		from utils import photos

		os.makedirs(photos.local_dir())
		data = os.urandom(5000)
		with open(os.path.join(photos.local_dir(), "clip.avi"), "wb") as f:
			f.write(data)
		status, headers, body = self.request("/album/photo/offline~clip.avi",
			headers={"X-Seleno-Pass": make_pass(), "Range": "bytes=100-199"})
		self.assertEqual(status, 206)
		self.assertEqual(body, data[100:200])
		self.assertEqual(headers["Content-Range"], "bytes 100-199/5000")

	def test_live_view_reaches_the_viewer(self) -> None:
		import websockets

		status, _h, body = self.request("/interface/liveview_ticket", headers={"X-Seleno-Pass": make_pass()})
		url = json.loads(body)["url"]
		self.assertTrue(url.startswith(f"wss://localhost:{self.live_port}/watch/test?ticket="), url)

		class FakeStream:
			enabled = True
			viewers = None

			def set_viewers(self, n):
				self.viewers = n

			def next_frame(self, timeout):
				time.sleep(0.05)
				return jpeg(64, 48)

			def note_sent(self, size, seconds):
				pass
		stream = FakeStream()

		async def watch(origin):
			async with websockets.connect(url, ssl=self.tls, additional_headers={"Origin": origin}) as ws:
				await ws.send(json.dumps({"type": "hello", "decode": ["jpeg"]}))
				hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
				frame = await asyncio.wait_for(ws.recv(), 5)
				return hello, frame

		with mock.patch("core.camera.stream.get_live_stream", return_value=stream):
			hello, frame = asyncio.run(watch(ORIGIN))
			self.assertEqual(hello, {"type": "telescope", "connected": True})
			magic, kind = struct.unpack_from(">4sB", frame)
			self.assertEqual((magic, kind), (b"SLV2", 1))
			self.assertEqual(frame[24:26], b"\xff\xd8")
			with self.assertRaises(Exception):
				asyncio.run(watch("https://evil.example"))


def _free_port() -> int:
	import socket

	s = socket.socket()
	s.bind(("127.0.0.1", 0))
	port = s.getsockname()[1]
	s.close()
	return port


if __name__ == "__main__":
	unittest.main()
