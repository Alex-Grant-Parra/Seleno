"""Telescope Settings (controllers/telescope_settings.py): the telescope's own
panel settings, read and changed through its getSettings and setSettings, by
its owner only."""
import time
import unittest
from types import SimpleNamespace

from harness import app, connect_fake, disconnect_fake, login, make_telescope, make_user, select


class TelescopeSettings(unittest.TestCase):
    def setUp(self):
        suffix = str(time.time_ns())[-8:]
        self.name = f"ts-scope-{suffix}"
        with app.app_context():
            owner = make_user(f"ts-owner-{suffix}")
            other = make_user(f"ts-other-{suffix}")
            make_telescope(self.name, owner=owner)
            # login() needs only the id; the rows themselves stay in this context
            self.owner, self.other = SimpleNamespace(id=owner.id), SimpleNamespace(id=other.id)
        self.client = app.test_client()

    def tearDown(self):
        disconnect_fake(self.name)

    def test_read_and_save_through_the_telescope(self):
        described = {"sections": [{"id": "site", "title": "Site", "items": [
            {"key": "horizon.min_alt_deg", "label": "Lowest altitude", "type": "number", "value": 5}]}]}
        fake = connect_fake(self.name, {
            "getSettings": described,
            "setSettings": lambda changes: {"applied": list(changes), "errors": {}, "notice": "", "settings": described},
        })
        login(self.client, self.owner)
        select(self.client, self.name)
        r = self.client.get("/interface/telescope_settings/data")
        self.assertEqual(r.get_json()["settings"], described)
        r = self.client.post("/interface/telescope_settings/data", json={"changes": {"horizon.min_alt_deg": 10}})
        self.assertEqual(r.get_json()["result"]["applied"], ["horizon.min_alt_deg"])
        self.assertEqual(fake.last("setSettings")[1], [{"horizon.min_alt_deg": 10}])
        r = self.client.post("/interface/telescope_settings/data", json={"changes": {}})
        self.assertEqual(r.status_code, 400)

    def test_an_older_telescope_says_so(self):
        connect_fake(self.name, {"getSettings": {"error": "Function 'getSettings' not found"}})
        login(self.client, self.owner)
        select(self.client, self.name)
        self.assertIn("too old", self.client.get("/interface/telescope_settings/data").get_json()["message"])

    def test_only_the_owner(self):
        connect_fake(self.name, {"getSettings": {"sections": []}})
        login(self.client, self.other)
        select(self.client, self.name)
        self.assertEqual(self.client.get("/interface/telescope_settings/data").status_code, 403)

    def test_the_page(self):
        app.jinja_env.globals.setdefault("csrf_token", lambda: "test-csrf")
        login(self.client, self.owner)
        select(self.client, self.name)
        page = self.client.get("/interface/telescope_settings/").get_data(as_text=True)
        self.assertIn(self.name, page)
        self.assertIn("telescope-settings.js", page)


if __name__ == "__main__":
    unittest.main()
