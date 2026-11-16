import time
import unittest
from datetime import datetime, timedelta

from harness import (app, client, db, wss, telescopeLink, connect_fake, disconnect_fake, make_user,
                     make_telescope, make_booking, login, select, _fake_post)


def now():
    return datetime.utcnow()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        tag = cls.__name__.lower()
        cls.admin = make_user(f"{tag}_admin", "Administrator")
        cls.owner = make_user(f"{tag}_owner")
        cls.booker = make_user(f"{tag}_booker")
        cls.stranger = make_user(f"{tag}_stranger")
        cls.limited = make_user(f"{tag}_limited", "Limited")
        cls.tname = f"{tag}-scope"
        cls.token = f"{tag}-token-0123456789"
        cls.scope = make_telescope(cls.tname, owner=cls.owner, token=cls.token)
        cls.other = make_telescope(f"{tag}-other", owner=cls.owner, token=f"{tag}-other-token-0123")
        cls.dev = make_telescope(f"{tag}-dev", client_type="developer", token=f"{tag}-dev-token-012345")
        make_booking(cls.scope, cls.booker, now() - timedelta(hours=1), now() + timedelta(hours=1))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def as_user(self, user):
        c = app.test_client()
        login(c, user)
        select(c, self.tname)
        return c


class SendCommandAuth(Base):
    def setUp(self):
        self.fake = connect_fake(self.tname, {"ping": "pong"})

    def tearDown(self):
        disconnect_fake(self.tname)

    def post(self, headers=None, client_id=None, **body):
        return client.post("/sendCommand", json={"client_id": client_id or self.tname, "command": "ping", **body},
                           headers=headers or {})

    def test_tunnel_request_from_loopback_is_refused(self):
        # Cloudflare's tunnel connects from 127.0.0.1 but adds its headers
        r = self.post(headers={"CF-Connecting-IP": "203.0.113.5", "CF-Ray": "abc"})
        self.assertEqual(r.status_code, 403)
        r = self.post()  # plain loopback without the internal key
        self.assertEqual(r.status_code, 403)

    def test_internal_key_is_accepted(self):
        r = self.post(headers={wss.INTERNAL_COMMAND_HEADER: wss.INTERNAL_COMMAND_KEY})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["result"], "pong")

    def test_internal_key_with_proxy_headers_is_refused(self):
        r = self.post(headers={wss.INTERNAL_COMMAND_HEADER: wss.INTERNAL_COMMAND_KEY, "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(r.status_code, 403)

    def test_token_scope(self):
        own = self.post(headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(own.status_code, 200)
        other_token = f"{type(self).__name__.lower()}-other-token-0123"
        foreign = self.post(headers={"Authorization": f"Bearer {other_token}"})
        self.assertEqual(foreign.status_code, 403)
        dev_token = f"{type(self).__name__.lower()}-dev-token-012345"
        dev = self.post(headers={"Authorization": f"Bearer {dev_token}"})
        self.assertEqual(dev.status_code, 200)

    def test_error_reply_is_reported(self):
        self.fake.handlers["boom"] = wss.ClientError("camera not connected")
        r = client.post("/sendCommand", json={"client_id": self.tname, "command": "boom"},
                        headers={wss.INTERNAL_COMMAND_HEADER: wss.INTERNAL_COMMAND_KEY})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json(), {"status": "error", "error": "camera not connected"})

    def test_not_connected(self):
        r = client.post("/sendCommand", json={"client_id": "nobody", "command": "ping"},
                        headers={wss.INTERNAL_COMMAND_HEADER: wss.INTERNAL_COMMAND_KEY})
        self.assertEqual(r.status_code, 502)
        self.assertIn("not connected", r.get_json()["error"])

    def test_timeout_passes_through(self):
        r = client.post("/sendCommand", json={"client_id": self.tname, "command": "ping", "timeout": 42},
                        headers={wss.INTERNAL_COMMAND_HEADER: wss.INTERNAL_COMMAND_KEY})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.last("ping")[3], 42.0)


class ExecuteErrors(unittest.TestCase):
    def test_execute_raises_on_error_reply(self):
        # Client.execute with a real future: an error reply raises ClientError, not returns an Exception
        import asyncio

        class WS:
            def __init__(self):
                self.sent = []

            async def send(self, message):
                import ujson
                msg = ujson.loads(message)
                self.sent.append(msg)
                fut = wss.pending.pop(msg["id"])
                fut.set_result({"id": msg["id"], "error": "below the horizon"})

        c = wss.Client("x", WS())
        with self.assertRaises(wss.ClientError) as cm:
            asyncio.run(c.execute("trackObject", ["Moon"], timeout=1))
        self.assertEqual(str(cm.exception), "below the horizon")

    def test_execute_timeout(self):
        import asyncio

        class WS:
            async def send(self, message):
                pass

        c = wss.Client("x", WS())
        with self.assertRaises(wss.ClientError):
            asyncio.run(c.execute("slow", timeout=0.05))


class Access(Base):
    def setUp(self):
        self.fake = connect_fake(self.tname, {
            "espEstopStatus": {"estop": False},
            "espClearEstop": {"cleared": True},
            "getCameraChoices": {"shutterSpeed": ["1/250", "30", "bulb"], "iso": ["100", "800"]},
        })

    def tearDown(self):
        disconnect_fake(self.tname)

    def command(self, user, action, **params):
        return self.as_user(user).post("/interface/command", json={"action": action, "params": params})

    def test_control_levels(self):
        self.assertEqual(self.command(self.stranger, "estop_status").status_code, 403)
        self.assertEqual(self.command(self.limited, "estop_status").status_code, 403)
        for user in (self.booker, self.owner, self.admin):
            r = self.command(user, "estop_status")
            self.assertEqual(r.status_code, 200, (user.username, r.get_json()))
            self.assertEqual(r.get_json()["result"], {"estop": False})

    def test_owner_only_commands(self):
        self.assertEqual(self.command(self.booker, "clear_estop").status_code, 403)
        self.assertEqual(self.command(self.owner, "clear_estop").status_code, 200)
        self.assertEqual(self.command(self.admin, "update_client").status_code, 200)
        self.assertEqual(self.command(self.booker, "update_client").status_code, 403)
        self.assertEqual(self.command(self.booker, "set_clock").status_code, 403)

    def test_expired_booking_has_no_access(self):
        late = make_user(f"{type(self).__name__.lower()}_late")
        make_booking(self.scope, late, now() - timedelta(hours=3), now() - timedelta(hours=2))
        self.assertEqual(self.command(late, "estop_status").status_code, 403)
        pending = make_user(f"{type(self).__name__.lower()}_pending")
        make_booking(self.scope, pending, now() - timedelta(hours=1), now() + timedelta(hours=1), status="pending")
        self.assertEqual(self.command(pending, "estop_status").status_code, 403)

    def test_existing_routes_are_checked(self):
        c = self.as_user(self.stranger)
        self.assertEqual(c.get("/interface/get_camera_choices").status_code, 403)
        self.assertEqual(c.post("/interface/start_live_view", json={}).status_code, 403)
        self.assertEqual(c.post("/interface/take_photo", json={}).status_code, 403)
        self.assertEqual(c.post("/interface/motor_command", json={"command": "stop"}).status_code, 403)
        self.assertEqual(c.post("/track_star", json={"name": "Vega", "ra": 279.2, "dec": 38.8}).status_code, 403)
        self.assertEqual(c.get(f"/liveview/{self.tname}").status_code, 403)
        self.assertEqual(c.get("/interface/liveview_ticket").status_code, 403)
        self.assertEqual(c.post("/interface/remove_telescope", json={"telescope_id": self.tname}).status_code, 403)
        self.assertEqual(c.post("/interface/add_telescope", json={"telescope_id": "squat"}).status_code, 403)
        self.assertEqual(c.post("/interface/update_telescope_heartbeat", json={"telescope_id": self.tname}).status_code, 403)
        ok = self.as_user(self.booker).get("/interface/get_camera_choices")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("iso", ok.get_json())

    def test_bad_params(self):
        r = self.command(self.owner, "set_tracking_rate", rate="warp")
        self.assertEqual(r.status_code, 400)
        r = self.command(self.owner, "set_tracking_rate", rate="custom", custom_arcsec_per_s="15.2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.last("setTrackingRate")[1], ["custom", 15.2])
        r = self.command(self.owner, "set_tracking_rate", rate="lunar")
        self.assertEqual(self.fake.last("setTrackingRate")[1], ["lunar"])
        self.assertEqual(self.command(self.owner, "nonsense").status_code, 400)


class FixFirst(Base):
    def setUp(self):
        self.fake = connect_fake(self.tname)

    def tearDown(self):
        disconnect_fake(self.tname)

    def test_capture_photo_sends_a_list(self):
        c = self.as_user(self.booker)
        r = c.post("/interface/take_photo", json={"shutter": "1/250"})
        self.assertEqual(r.get_json()["status"], "success", r.get_json())
        name, args, kwargs, timeout = self.fake.last("capturePhoto")
        self.assertEqual(args, [str(self.booker.id)])
        self.assertAlmostEqual(timeout, 30.004, places=3)
        r = c.post("/interface/take_photo", json={"exposure_s": 120})
        name, args, kwargs, timeout = self.fake.last("capturePhoto")
        self.assertEqual(args, [str(self.booker.id), 120.0])
        self.assertEqual(timeout, 150.0)
        # the HTTP call waits longer than the command server
        self.assertEqual(_fake_post.calls[-1]["timeout"], 154.0)

    def test_refusal_is_not_success(self):
        self.fake.handlers["capturePhoto"] = {"error": "Camera busy"}
        r = self.as_user(self.booker).post("/interface/take_photo", json={})
        self.assertEqual(r.get_json()["status"], "error")
        self.assertEqual(r.get_json()["message"], "Camera busy")

        self.fake.handlers["trackCoordinates"] = {"error": "Target is below the horizon"}
        r = self.as_user(self.booker).post("/track_star", json={"name": "Vega", "ra": 279.2, "dec": 38.8, "mag": 0})
        self.assertEqual(r.get_json()["status"], "error")
        self.assertEqual(r.get_json()["message"], "Target is below the horizon")

    def test_moon_goes_by_name(self):
        r = self.as_user(self.booker).post("/track_star", json={"name": "Moon", "ra": 10.0, "dec": 5.0, "mag": -12})
        self.assertEqual(r.get_json()["status"], "tracking", r.get_json())
        self.assertEqual(self.fake.last("trackObject")[1], ["Moon"])
        self.assertIsNone(self.fake.last("trackCoordinates"))

    def test_camera_settings(self):
        r = self.as_user(self.booker).post("/interface/update_camera", json={"aperture": "f/8"})
        self.assertEqual(r.get_json()["changed"], ["aperture"])
        self.assertEqual(self.fake.last("setCameraSetting")[1], ["/main/capturesettings/aperture", "f/8"])

    def test_raw_extensions(self):
        from controllers.upload import allowed_file
        for ext in ("cr3", "raf", "pef", "srw", "nrw", "mp4", "avi"):
            self.assertTrue(allowed_file(f"x.{ext}"), ext)


class StatusMessages(Base):
    def test_status_is_stored_and_served(self):
        message = '{"type": "status", "tracking": {"state": "tracking", "target": "M42"}, "disk": 12345, "bad": [1,2]}'
        import ujson
        wss._store_status(self.tname, self.tname, message, ujson.loads(message))
        time.sleep(0.3)
        r = self.as_user(self.booker).get("/interface/telescope_status")
        body = r.get_json()
        self.assertEqual(body["telescope_status"]["tracking"]["target"], "M42")
        self.assertEqual(body["telescope_status"]["disk"], 12345)
        self.assertNotIn("bad", body["telescope_status"])
        self.assertLess(body["age_s"], 5)
        self.assertEqual(body["access"], {"control": True, "owner": False})
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(type(self.scope), self.scope.id).last_seen)

    def test_oversized_status_is_dropped(self):
        big = '{"type": "status", "x": "' + "a" * (70 * 1024) + '"}'
        wss.latest_status.pop("big-one", None)
        wss._store_status("big-one", "big-one", big, {"type": "status"})
        self.assertNotIn("big-one", wss.latest_status)


if __name__ == "__main__":
    unittest.main(verbosity=1)
