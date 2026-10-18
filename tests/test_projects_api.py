"""Projects: storing the running totals of a multi-night integration."""
import os
import unittest

from harness import app, login, make_user, PHOTOS

FITS = b"SIMPLE  =                    T" + b" " * 50


class ProjectsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.owner = make_user("projectowner")
        cls.other = make_user("projectother")
        cls.dir = os.path.join(PHOTOS, str(cls.owner.id))
        os.makedirs(cls.dir, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def as_user(self, user):
        c = app.test_client()
        login(c, user)
        return c

    def upload_all(self, c, pid, tag=b"1"):
        sizes = {}
        for name in ("sum.fits", "wsum.fits", "coverage.fits", "limits.fits"):
            body = FITS + tag * 100
            half = len(body) // 2
            self.assertEqual(c.put(f"/album/api/projects/{pid}/upload/{name}?offset=0", data=body[:half]).status_code, 200)
            r = c.put(f"/album/api/projects/{pid}/upload/{name}?offset={half}", data=body[half:])
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["received"], len(body))
            sizes[name] = len(body)
        return sizes

    def test_nights_checkpoints_and_rebuild(self):
        c = self.as_user(self.owner)
        r = c.post("/album/api/projects", json={"name": "M31 deep", "target": "M31", "width": 2817, "height": 1876,
                                                "settings": {"debayer": "superpixel"}, "reference": {"name": "a.cr2", "xy": [[1, 2]]}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        pid = r.get_json()["project"]["id"]

        # Not a FITS, wrong offsets, unknown files
        self.assertEqual(c.put(f"/album/api/projects/{pid}/upload/sum.fits?offset=0", data=b"nope").status_code, 400)
        self.assertEqual(c.put(f"/album/api/projects/{pid}/upload/evil.sh?offset=0", data=FITS).status_code, 404)
        c.put(f"/album/api/projects/{pid}/upload/sum.fits?offset=0", data=FITS)
        r = c.put(f"/album/api/projects/{pid}/upload/sum.fits?offset=5", data=b"x")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["received"], len(FITS))

        # A commit needs every file
        r = c.post(f"/album/api/projects/{pid}/commit", json={"mode": "night", "frames": ["a.cr2"], "exposure_s": 60})
        self.assertEqual(r.status_code, 400)

        sizes = self.upload_all(c, pid)
        r = c.post(f"/album/api/projects/{pid}/commit", json={"mode": "night", "frames": ["a.cr2", "b.cr2"], "considered": ["a.cr2", "b.cr2", "blurred.cr2"], "exposure_s": 120,
                                                              "night": "2026-10-18", "files": sizes})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        p = r.get_json()["project"]
        self.assertEqual((p["generation"], p["frames_total"], p["exposure_total_s"]), (1, 2, 120))
        self.assertEqual(p["seen"], ["a.cr2", "b.cr2", "blurred.cr2"])
        r = c.get(f"/album/projects/{pid}/current/sum.fits", headers={"Range": "bytes=0-8"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.data, b"SIMPLE  =")

        # A second night: names already in are counted once
        self.upload_all(c, pid, b"2")
        r = c.post(f"/album/api/projects/{pid}/commit", json={"mode": "night", "frames": ["b.cr2", "c.cr2"], "exposure_s": 60, "night": "2026-10-19"})
        p = r.get_json()["project"]
        self.assertEqual((p["generation"], p["frames_total"], p["exposure_total_s"]), (2, 3, 180))
        self.assertEqual(len(p["nights"]), 2)
        self.assertTrue(c.get(f"/album/projects/{pid}/current/sum.fits").data.endswith(b"2" * 100))

        # A rebuild: a checkpoint, then the finish
        self.upload_all(c, pid, b"3")
        r = c.post(f"/album/api/projects/{pid}/commit", json={"mode": "checkpoint", "done": ["a.cr2", "x.cr2"], "accepted": ["a.cr2"], "total": 3, "settings": {"method": "sigma"}})
        p = r.get_json()["project"]
        self.assertEqual(p["rebuilding"]["done"], ["a.cr2", "x.cr2"])
        self.assertEqual(p["rebuilding"]["accepted"], ["a.cr2"])
        self.assertEqual(p["generation"], 2)                       # the live totals are untouched
        self.assertTrue(c.get(f"/album/projects/{pid}/rebuild/sum.fits").data.endswith(b"3" * 100))
        self.assertTrue(c.get(f"/album/projects/{pid}/current/sum.fits").data.endswith(b"2" * 100))
        self.upload_all(c, pid, b"4")
        r = c.post(f"/album/api/projects/{pid}/commit", json={"mode": "rebuilt", "frames": ["a.cr2", "b.cr2", "c.cr2"],
                                                              "exposure_s": 180, "settings": {"method": "sigma"}})
        p = r.get_json()["project"]
        self.assertIsNone(p["rebuilding"])
        self.assertEqual((p["generation"], p["frames_total"], p["settings"]), (3, 3, {"method": "sigma"}))
        self.assertEqual(c.get(f"/album/projects/{pid}/rebuild/sum.fits").status_code, 404)

        # Only the owner sees it; deleting removes the files
        other = self.as_user(self.other)
        self.assertEqual(other.get(f"/album/api/projects/{pid}").status_code, 404)
        self.assertEqual(other.get(f"/album/projects/{pid}/current/sum.fits").status_code, 404)
        self.assertEqual([x["name"] for x in c.get("/album/api/projects").get_json()["projects"]], ["M31 deep"])
        self.assertEqual(c.delete(f"/album/api/projects/{pid}").status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".projects", pid)))


if __name__ == "__main__":
    unittest.main()
