"""Offline mode (app/offline_mode.py): this server on a telescope's Pi, for
phones on its hotspot. One telescope and one account; only the offline pages;
the telescope's settings page; the live view address; and the bundle
telescopes download it as (controllers/offline_bundle.py)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import app, client, connect_fake, disconnect_fake, login, make_telescope, make_user, select

from app import liveview, offline_mode
from models.tables import Telescope as TelescopeRow
from models.user import User

NAME = "Field Scope"
TOKEN = "offline-token-0123456789abcdefghijklmnop"


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SELENO_OFFLINE": "1", "SELENO_OFFLINE_TELESCOPE": NAME,
                                                "SELENO_OFFLINE_TOKEN": TOKEN})
        self.env.start()
        offline_mode.bootstrap(app)
        self.client = app.test_client()

    def tearDown(self):
        self.env.stop()
        disconnect_fake(NAME)


class Bootstrap(OfflineCase):
    def test_one_telescope_owned_by_the_observer(self):
        from security.token_store import verify_token

        with app.app_context():
            observer = User.query.filter_by(username=offline_mode.OBSERVER_USERNAME).one()
            row = TelescopeRow.query.filter_by(telescope_id=NAME).one()
            self.assertEqual(row.user_id, observer.id)
            self.assertEqual(row.type, "telescope")
            self.assertTrue(row.is_approved)
            rec, _reason = verify_token(TOKEN)
            self.assertEqual(rec.id, row.id)

    def test_renaming_keeps_the_row(self):
        with app.app_context():
            before = TelescopeRow.query.filter_by(telescope_id=NAME).one().id
        with mock.patch.dict(os.environ, {"SELENO_OFFLINE_TELESCOPE": "Renamed Scope"}):
            offline_mode.bootstrap(app)
            with app.app_context():
                self.assertEqual(TelescopeRow.query.filter_by(telescope_id="Renamed Scope").one().id, before)
                self.assertIsNone(TelescopeRow.query.filter_by(telescope_id=NAME).first())
        offline_mode.bootstrap(app)
        with app.app_context():
            self.assertEqual(TelescopeRow.query.filter_by(telescope_id=NAME).one().id, before)
            self.assertEqual(User.query.filter_by(username=offline_mode.OBSERVER_USERNAME).count(), 1)


class Gate(OfflineCase):
    def test_every_visitor_is_the_observer_with_the_telescope_selected(self):
        r = self.client.get("/interface/get_selected_telescope")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["telescope"]["telescope_id"], NAME)
        self.assertTrue(data["access"]["owner"])

    def test_only_the_offline_pages(self):
        r = self.client.get("/store", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/interface/"))
        r = self.client.post("/payments/checkout", json={})
        self.assertEqual(r.status_code, 404)
        for path in ("/", "/home", "/login", "/admin", "/book", "/profile", "/contact"):
            self.assertFalse(offline_mode.path_allowed(path), path)
        for path in ("/interface/", "/StarMap", "/album", "/album/api/library", "/api/stars_bin", "/star_info/Vega",
                     "/track_star", "/static/style.css", "/sendCommand", "/liveview/x", "/telescopes/3/zones",
                     "/security/csrf-token"):
            self.assertTrue(offline_mode.path_allowed(path), path)

    def test_settings_page_shows_the_telescope_not_the_site(self):
        # The harness has no CSRFProtect, which base.html's meta tag needs
        app.jinja_env.globals.setdefault("csrf_token", lambda: "test-csrf")
        r = self.client.get("/interface/telescope_settings/")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn(NAME, page)
        self.assertIn("telescope-settings.js", page)
        self.assertNotIn("/logout", page)
        self.assertNotIn("/store", page)

    def test_off_when_not_asked_for(self):
        self.env.stop()
        try:
            self.assertFalse(offline_mode.enabled())
            r = app.test_client().get("/interface/get_selected_telescope")
            self.assertIsNone(r.get_json()["telescope"])
        finally:
            self.env.start()


class TelescopeSettings(OfflineCase):
    def test_read_and_save_through_the_telescope(self):
        described = {"sections": [{"id": "site", "title": "Site", "items": [
            {"key": "horizon.min_alt_deg", "label": "Lowest altitude", "type": "number", "value": 5}]}]}
        fake = connect_fake(NAME, {
            "getSettings": described,
            "setSettings": lambda changes: {"applied": list(changes), "errors": {}, "notice": "", "settings": described},
        })
        r = self.client.get("/interface/telescope_settings/data")
        self.assertEqual(r.get_json()["settings"], described)
        r = self.client.post("/interface/telescope_settings/data", json={"changes": {"horizon.min_alt_deg": 10}})
        self.assertEqual(r.get_json()["result"]["applied"], ["horizon.min_alt_deg"])
        self.assertEqual(fake.last("setSettings")[1], [{"horizon.min_alt_deg": 10}])
        r = self.client.post("/interface/telescope_settings/data", json={"changes": {}})
        self.assertEqual(r.status_code, 400)

    def test_an_older_telescope_says_so(self):
        connect_fake(NAME, {"getSettings": {"error": "Function 'getSettings' not found"}})
        r = self.client.get("/interface/telescope_settings/data")
        self.assertIn("too old", r.get_json()["message"])


class OnlineOwnersOnly(unittest.TestCase):
    def test_someone_elses_telescope(self):
        with app.app_context():
            owner = make_user("ts-owner")
            other = make_user("ts-other")
            make_telescope("ts-scope", owner=owner)
            c = app.test_client()
            login(c, other)
            select(c, "ts-scope")
            r = c.get("/interface/telescope_settings/data")
            self.assertEqual(r.status_code, 403)
            login(c, owner)
            connect_fake("ts-scope", {"getSettings": {"sections": []}})
            try:
                self.assertEqual(c.get("/interface/telescope_settings/data").get_json()["status"], "success")
            finally:
                disconnect_fake("ts-scope")


class LiveView(OfflineCase):
    def test_watch_address_is_the_one_the_phone_used(self):
        with app.test_request_context("/", headers={"Host": "10.42.0.1"}):
            url = liveview.watch_url(NAME, "tick")
        self.assertTrue(url.startswith("ws://10.42.0.1:8000/watch/Field%20Scope?ticket="), url)
        with app.test_request_context("/", headers={"Host": "seleno-pi.local:5000"}):
            self.assertTrue(liveview.watch_url(NAME, "t").startswith("ws://seleno-pi.local:8000/"))

    def test_viewer_origins(self):
        for origin in ("http://10.42.0.1", "http://192.168.0.55:5000", "http://seleno-pi.local", "http://localhost"):
            self.assertTrue(offline_mode.origin_allowed(origin), origin)
        for origin in ("https://evil.example", "http://8.8.8.8", "", "null"):
            self.assertFalse(offline_mode.origin_allowed(origin), origin)


class Bundle(unittest.TestCase):
    def setUp(self):
        import controllers.offline_bundle as bundle_mod

        self.mod = bundle_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.dir_patch = mock.patch.object(bundle_mod, "BUNDLE_DIR", self.tmp.name)
        self.dir_patch.start()
        with app.app_context():
            if TelescopeRow.query.filter_by(telescope_id="bundle-scope").first() is None:
                make_telescope("bundle-scope", token="bundle-token-0123456789")
            if TelescopeRow.query.filter_by(telescope_id="bundle-observer").first() is None:
                make_telescope("bundle-observer", client_type="observer", token="observer-token-0123456789")

    def tearDown(self):
        self.dir_patch.stop()
        self.tmp.cleanup()

    def get(self, token, **headers):
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = client.get("/api/telescope/offline_bundle", headers=headers)
        r.get_data()
        r.close()
        return r

    def test_download(self):
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.get("wrong-token-0123456789").status_code, 401)
        self.assertEqual(self.get("observer-token-0123456789").status_code, 403)
        self.assertEqual(self.get("bundle-token-0123456789").status_code, 404)
        with open(os.path.join(self.tmp.name, self.mod.BUNDLE_NAME), "wb") as f:
            f.write(b"tarball")
        with open(os.path.join(self.tmp.name, self.mod.VERSION_NAME), "w") as f:
            json.dump({"version": "v7", "sha256": "abc"}, f)
        r = self.get("bundle-token-0123456789")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"tarball")
        self.assertEqual(r.headers["X-Seleno-Bundle-Version"], "v7")
        self.assertEqual(self.get("bundle-token-0123456789", **{"If-None-Match": '"v7"'}).status_code, 304)
        self.assertEqual(self.get("bundle-token-0123456789", **{"If-None-Match": '"v6"'}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
