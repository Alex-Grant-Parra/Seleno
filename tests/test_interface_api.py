"""The interface page's sky, targets and latest-captures endpoints, and app.sky."""
import os
import unittest
from datetime import datetime, timedelta

from harness import app, db, wss, connect_fake, make_user, make_telescope, login, select, PHOTOS, Photo, FeaturedTarget
from app import sky
from models.album import ensure_featured_targets


class SkyMathTests(unittest.TestCase):
    def test_london_october_night(self):
        start, end = sky.night_window(51.48, 0.0, datetime(2026, 10, 18, 15))
        # Astronomical twilight ends about 18:50 UTC and starts again about 04:40 UTC
        self.assertLess(abs((start - datetime(2026, 10, 18, 18, 51)).total_seconds()), 600)
        self.assertLess(abs((end - datetime(2026, 10, 19, 4, 39)).total_seconds()), 600)

    def test_no_astronomical_night_at_midsummer(self):
        self.assertEqual(sky.night_window(51.48, 0.0, datetime(2026, 6, 21, 12)), (None, None))

    def test_m42_from_london(self):
        info = sky.target_tonight(83.82, -5.39, 51.48, 0.0, datetime(2026, 10, 18, 22))
        self.assertAlmostEqual(info["transit_alt"], 33.13, places=1)
        self.assertLess(abs((info["transit"] - datetime(2026, 10, 19, 3, 45)).total_seconds()), 600)
        self.assertTrue(info["rising"])
        self.assertGreater(info["minutes_above"], 100)

    def test_moon_phase(self):
        fraction, waxing, name = sky.moon_phase(sky.julian_day(datetime(2026, 10, 18, 22)))
        self.assertAlmostEqual(fraction, 0.523, places=2)
        self.assertTrue(waxing)
        self.assertEqual(name, "First quarter")


class InterfaceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        ensure_featured_targets()
        cls.owner = make_user("ifowner")
        cls.stranger = make_user("ifstranger")
        cls.scope = make_telescope("if-scope", owner=cls.owner)
        cls.nosite = make_telescope("if-nosite", owner=cls.owner)
        cls.uploaded = make_telescope("if-uploaded", owner=cls.owner)
        cls.scope.latitude, cls.scope.longitude = 51.4769, -0.0005
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def client(self, user, scope="if-scope"):
        c = app.test_client()
        login(c, user)
        select(c, scope)
        return c

    def test_sky_needs_access_and_a_site(self):
        self.assertEqual(self.client(self.stranger).get("/interface/api/sky").status_code, 403)
        body = self.client(self.owner, "if-nosite").get("/interface/api/sky").get_json()
        self.assertIsNone(body["site"])
        self.assertIn("isn't known", body["message"])

    def test_sky_with_a_target(self):
        c = self.client(self.owner)
        body = c.get("/interface/api/sky?target=Moon").get_json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["site"], {"lat": 51.48, "lon": -0.0, "source": "telescope"})
        self.assertIn(body["sun"]["state"], ("day", "civil twilight", "nautical twilight", "astronomical twilight",
                                              "astronomical night"))
        self.assertEqual(body["target"]["name"], "Moon")
        self.assertLess(body["target"]["moon_separation"], 1.0)
        body = c.get("/interface/api/sky?ra=83.82&dec=-5.39").get_json()
        self.assertEqual(body["target"]["ra"], 83.82)
        self.assertIn(body["target"]["verdict"][0], ("ok", "warn", "bad"))
        self.assertEqual(c.get("/interface/api/sky?target=Nowhere%20Nebula").status_code, 404)

    def test_site_from_uploads(self):
        db.session.add(Photo(owner_id=self.owner.id, telescope_id=self.uploaded.id, filename="s.jpg", path="/x",
                             sha256="s" * 64, kind="jpeg", site_lat=40.0, site_lon=-3.7,
                             uploaded_at=datetime.utcnow() - timedelta(days=1)))
        db.session.commit()
        body = self.client(self.owner, "if-uploaded").get("/interface/api/sky").get_json()
        self.assertEqual(body["site"]["source"], "upload")
        self.assertEqual(body["site"]["lat"], 40.0)

    def test_targets(self):
        body = self.client(self.owner).get("/interface/api/targets").get_json()
        names = [t["designation"] for t in body["targets"]]
        # The test database has no catalogue, so only the Moon and the planets resolve
        self.assertIn("Moon", names)
        self.assertIn("Jupiter", names)
        moon = next(t for t in body["targets"] if t["designation"] == "Moon")
        self.assertEqual(moon["preset"], "moon")
        self.assertIn("alt", moon)
        self.assertIn(moon["verdict"][0], ("ok", "warn", "bad"))
        self.assertEqual(ensure_featured_targets(), 0)      # seeding twice adds nothing

    def test_latest_captures(self):
        c = self.client(self.owner)
        before = datetime.utcnow() - timedelta(seconds=5)
        for i, (name, kind, ftype, pair) in enumerate((("T1.JPG", "jpeg", "test", "pt1"), ("T1.CR2", "raw", "test", "pt1"),
                                                      ("L1.JPG", "jpeg", "light", None))):
            db.session.add(Photo(owner_id=self.owner.id, telescope_id=self.scope.id, filename=name, path="/x",
                                 sha256=f"{name}".ljust(64, "0"), kind=kind, frame_type=ftype, pair_id=pair,
                                 uploaded_at=before + timedelta(seconds=i + 1), exposure_s=30, iso="800"))
        db.session.commit()
        body = c.get("/interface/api/latest_captures").get_json()
        self.assertEqual([f["name"] for f in body["frames"]], ["L1.JPG", "T1.JPG"])
        test = body["frames"][1]
        self.assertTrue(test["has_raw"])
        self.assertEqual(test["frame_type"], "test")
        self.assertTrue(test["thumb"].startswith("/album/thumb/T1.JPG"))
        since = (before + timedelta(seconds=2)).isoformat() + "Z"
        body = c.get(f"/interface/api/latest_captures?since={since}").get_json()
        self.assertEqual([f["name"] for f in body["frames"]], ["L1.JPG"])
        self.assertEqual(c.get("/interface/api/latest_captures?since=yesterday").status_code, 400)
        self.assertEqual(self.client(self.stranger).get("/interface/api/latest_captures").status_code, 403)


class CapabilityTests(unittest.TestCase):
    """New things go to the Pi only when its status says it takes them."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.owner = make_user("capowner")
        make_telescope("cap-scope", owner=cls.owner)
        cls.fake = connect_fake("cap-scope", {"capturePhoto": {"ok": True}})

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def shoot(self):
        c = app.test_client()
        login(c, self.owner)
        r = c.post("/interface/take_photo", json={"telescope_id": "cap-scope", "shutter": "1/100", "frame_type": "test"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.fake.last("capturePhoto")

    def test_frame_type_only_for_capable_clients(self):
        wss._store_status("cap-scope", "cap-scope", "{}", {"type": "status"})
        self.assertEqual(self.shoot()[2], {})
        status = {"type": "status", "capabilities": ["capture_frame_type", 5, "x" * 100], "site": {"lat": 51.5, "lon": 0.1}}
        wss._store_status("cap-scope", "cap-scope", "{}", status)
        _, stored = wss.get_latest_status("cap-scope")
        self.assertEqual(stored["capabilities"], ["capture_frame_type", "x" * 64])
        self.assertEqual(stored["site"], {"lat": 51.5, "lon": 0.1})
        self.assertEqual(self.shoot()[2], {"frame_type": "test"})
        c = app.test_client()
        login(c, self.owner)
        r = c.post("/interface/take_photo", json={"telescope_id": "cap-scope", "frame_type": "dark"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=1)
