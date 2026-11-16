import base64
import copy
import hashlib
import io
import json
import os
import time
import unittest
import zipfile
from datetime import datetime, timedelta

from PIL import Image

from harness import (app, db, wss, tus_mod, connect_fake, disconnect_fake, make_user, make_telescope,
                     make_booking, login, select, Photo, TelescopeUpload, CapturePlan, PHOTOS)
from app import capture_plans

TUS = {"Tus-Resumable": "1.0.0"}

DOC_PLAN = {
    "name": "Moon then M42",
    "on_error": "skip_step",
    "upload": "jpeg_first",
    "park_at_end": False,
    "steps": [
        {"type": "goto", "target": "Moon"},
        {"type": "bracket", "exposures_s": [0.001, 0.004, 0.016], "iso": 100, "repeat": 3},
        {"type": "video", "duration_s": 60, "quality": "original"},
        {"type": "goto", "name": "M42", "ra": 83.82, "dec": -5.39, "centre": True},
        {"type": "light", "count": 20, "exposure_s": 30, "iso": 800, "dither_every": 5, "dither_arcmin": 2},
        {"type": "dark", "count": 20, "exposure_s": 30, "iso": 800},
        {"type": "bias", "count": 30},
    ],
}


def now():
    return datetime.utcnow()


def jpeg_bytes(color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def b64(text):
    return base64.b64encode(str(text).encode()).decode()


def sha1_header(data):
    return "sha1 " + base64.b64encode(hashlib.sha1(data).digest()).decode()


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
        cls.tname = f"{tag}-scope"
        cls.token = f"{tag}-token-0123456789"
        cls.scope = make_telescope(cls.tname, owner=cls.owner, token=cls.token)
        cls.booking = make_booking(cls.scope, cls.booker, now() - timedelta(hours=1), now() + timedelta(hours=2))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def as_user(self, user):
        c = app.test_client()
        login(c, user)
        select(c, self.tname)
        return c


class Uploads(Base):
    def auth(self, extra=None):
        return {**TUS, "Authorization": f"Bearer {self.token}", **(extra or {})}

    def create(self, data, owner, filename="IMG_0412.JPG", kind="jpeg", meta=None, captured=None):
        c = app.test_client()
        metadata = ",".join([
            f"filename {b64(filename)}",
            f"owner {b64(owner.id)}",
            f"sha256 {b64(hashlib.sha256(data).hexdigest())}",
            f"kind {b64(kind)}",
            f"captured {b64(captured or now().isoformat() + 'Z')}",
            f"meta {b64(json.dumps(meta or {}))}",
        ])
        return c.post("/api/telescope/uploads", headers=self.auth({"Upload-Length": str(len(data)), "Upload-Metadata": metadata}))

    def patch(self, location, chunk, offset, checksum=None):
        c = app.test_client()
        headers = self.auth({"Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream",
                             "Upload-Checksum": checksum or sha1_header(chunk)})
        return c.patch(location, data=chunk, headers=headers)

    def test_options(self):
        r = app.test_client().options("/api/telescope/uploads")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["Tus-Version"], "1.0.0")
        self.assertIn("checksum", r.headers["Tus-Extension"])
        self.assertEqual(r.headers["Tus-Checksum-Algorithm"], "sha1")
        self.assertEqual(r.headers["Tus-Resumable"], "1.0.0")

    def test_full_upload_resume_and_dedupe(self):
        data = jpeg_bytes()
        meta = {"target": "M42", "ra": 83.82, "dec": -5.39, "site": {"lat": 51.5, "lon": -0.1},
                "exposure_s": 30, "iso": 800, "frame_type": "light", "plan": "plan_x", "step": 4, "frame": 0, "pair": "p1"}
        r = self.create(data, self.booker, meta=meta)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        location = r.headers["Location"]
        self.assertTrue(location.startswith("/api/telescope/uploads/"))
        self.assertIn("Upload-Expires", r.headers)

        half = len(data) // 2
        self.assertEqual(self.patch(location, data[:half], 0).status_code, 204)
        # a resend at the wrong offset
        r = self.patch(location, data[half:], 0)
        self.assertEqual(r.status_code, 409)
        # a corrupted chunk
        r = self.patch(location, data[half:], half, checksum=sha1_header(b"other"))
        self.assertEqual(r.status_code, 460)
        # after a cut-out: ask how much arrived
        r = app.test_client().head(location, headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(int(r.headers["Upload-Offset"]), half)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        r = self.patch(location, data[half:], half)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(int(r.headers["Upload-Offset"]), len(data))

        photo = Photo.query.filter_by(owner_id=self.booker.id).one()
        self.assertEqual((photo.frame_type, photo.target, photo.step, photo.pair_id, photo.site_lat), ("light", "M42", 4, "p1", 51.5))
        self.assertTrue(os.path.exists(os.path.join(PHOTOS, str(self.booker.id), "IMG_0412.JPG")))
        # the target shows in the album via the JPEG's description
        with Image.open(photo.path) as img:
            self.assertEqual(img.getexif().get(270), "M42")

        # a retry of the same photo keeps the one copy
        r = self.create(data, self.booker)
        location2 = r.headers["Location"]
        self.assertEqual(self.patch(location2, data, 0).status_code, 204)
        self.assertEqual(Photo.query.filter_by(owner_id=self.booker.id).count(), 1)

        # a different file with the same name gets a suffix
        other = jpeg_bytes((200, 0, 0))
        r = self.create(other, self.booker)
        self.assertEqual(self.patch(r.headers["Location"], other, 0).status_code, 204)
        names = sorted(p.filename for p in Photo.query.filter_by(owner_id=self.booker.id))
        self.assertEqual(names, ["IMG_0412.JPG", "IMG_0412_1.JPG"])

    def test_owner_check(self):
        data = jpeg_bytes()
        self.assertEqual(self.create(data, self.stranger).status_code, 403)
        # the booker, for a photo captured outside the booking
        old = (now() - timedelta(days=2)).isoformat() + "Z"
        self.assertEqual(self.create(data, self.booker, captured=old).status_code, 403)
        self.assertEqual(self.create(data, self.owner, filename="o.jpg").status_code, 201)
        self.assertEqual(self.create(data, self.admin, filename="a.jpg").status_code, 201)

    def test_auth_and_validation(self):
        c = app.test_client()
        self.assertEqual(c.post("/api/telescope/uploads", headers={**TUS, "Upload-Length": "1"}).status_code, 401)
        self.assertEqual(c.post("/api/telescope/uploads", headers={"Authorization": f"Bearer {self.token}", "Upload-Length": "1"}).status_code, 412)
        self.assertEqual(self.create(b"x", self.owner, filename="evil.exe").status_code, 400)
        self.assertEqual(self.create(jpeg_bytes(), self.owner, filename="frame.CR3", kind="raw").status_code, 201)

    def test_expired_and_delete(self):
        data = jpeg_bytes()
        location = self.create(data, self.owner, filename="exp.jpg").headers["Location"]
        upload_id = location.rsplit("/", 1)[1]
        row = db.session.get(TelescopeUpload, upload_id)
        row.expires_at = now() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(app.test_client().head(location, headers=self.auth()).status_code, 410)

        location = self.create(data, self.owner, filename="del.jpg").headers["Location"]
        self.assertEqual(app.test_client().delete(location, headers=self.auth()).status_code, 204)
        self.assertEqual(app.test_client().head(location, headers=self.auth()).status_code, 404)

    def test_cleanup(self):
        data = jpeg_bytes()
        location = self.create(data, self.owner, filename="stale.jpg").headers["Location"]
        upload_id = location.rsplit("/", 1)[1]
        row = db.session.get(TelescopeUpload, upload_id)
        row.updated_at = now() - timedelta(days=8)
        db.session.commit()
        self.assertGreaterEqual(tus_mod.cleanup_stale_uploads(), 1)
        self.assertIsNone(db.session.get(TelescopeUpload, upload_id))
        self.assertFalse(os.path.exists(tus_mod._part_path(upload_id)))


class Plans(Base):
    def test_doc_plan_validates_and_estimates(self):
        self.assertEqual(capture_plans.validate_plan(DOC_PLAN), [])
        est = capture_plans.estimate_plan(DOC_PLAN, overhead_s=3.0)
        # 9 bracket + 1 video + 20 lights + 20 darks + 30 bias
        self.assertEqual(est["frames"], 80)
        bracket = 3 * (0.001 + 0.004 + 0.016 + 9)
        lights = 20 * 33 + 4 * capture_plans.DITHER_S
        expected = 45 + bracket + 63 + 105 + lights + 20 * 33 + 30 * 3
        self.assertAlmostEqual(est["seconds"], round(expected, 1), places=1)

    def test_validation_errors(self):
        bad = copy.deepcopy(DOC_PLAN)
        bad["steps"].append({"type": "light", "count": 5, "exposure_s": 1200})
        bad["steps"].append({"type": "wait"})
        bad["steps"].append({"type": "video", "duration_s": 900})
        bad["steps"].append({"type": "goto", "ra": 10})
        bad["steps"].append({"type": "warp"})
        bad["steps"].append({"type": "flat", "count": 3, "exposure_s": "sometimes"})
        bad["steps"].append({"type": "light", "count": 1, "colour": "blue"})
        errors = capture_plans.validate_plan(bad)
        joined = " | ".join(errors)
        for fragment in ("exposure_s must be at most 900", "exactly one of until", "duration_s must be at most 600",
                         "needs both ra and dec", "unknown type: warp", "must be one of auto", "unknown field: colour"):
            self.assertIn(fragment, joined)
        self.assertEqual(capture_plans.validate_plan({**DOC_PLAN, "steps": [{"type": "flat", "count": 3, "exposure_s": "auto"}]}), [])
        too_many = {"name": "x", "steps": [{"type": "bias", "count": 2000}, {"type": "bias", "count": 1}]}
        self.assertIn("limit is 2000", " ".join(capture_plans.validate_plan(too_many)))
        self.assertIn("at most 50", " ".join(capture_plans.validate_plan({"name": "x", "steps": [{"type": "bias", "count": 1}] * 51})))

    def test_camera_choices(self):
        errors = capture_plans.check_camera_settings(DOC_PLAN, {"iso": ["100", "200", "400"]})
        self.assertEqual(errors, ["Step 5: the camera has no iso 800", "Step 6: the camera has no iso 800"])

    def test_booking_rules(self):
        plan, context, errors = capture_plans.prepare_plan(self.booker, self.scope, DOC_PLAN)
        self.assertEqual(errors, [])
        self.assertEqual(plan["owner"], str(self.booker.id))
        self.assertEqual(context["end_by"], self.booking.end_utc)
        self.assertTrue(plan["id"].startswith("plan_" + now().strftime("%Y%m%d") + "_"))
        # the telescope owner starting during someone's booking: photos still go to the booker
        plan, context, errors = capture_plans.prepare_plan(self.owner, self.scope, DOC_PLAN)
        self.assertEqual(plan["owner"], str(self.booker.id))
        _, _, errors = capture_plans.prepare_plan(self.stranger, self.scope, DOC_PLAN)
        self.assertTrue(errors)
        # a plan that runs past the booking's end
        long_plan = {"name": "long", "steps": [{"type": "light", "count": 300, "exposure_s": 60}]}
        _, _, errors = capture_plans.prepare_plan(self.booker, self.scope, long_plan)
        self.assertIn("must end by", errors[0])

    def test_start_progress_control_and_watchdog(self):
        fake = connect_fake(self.tname, {
            "getCameraChoices": {"iso": ["100", "800"], "shutterSpeed": []},
            "startCapturePlan": lambda plan: {"status": "started", "plan": plan["id"]},
            "pauseCapturePlan": "paused",
        })
        try:
            c = self.as_user(self.booker)
            r = c.post("/interface/capture/start", json={"plan": DOC_PLAN})
            body = r.get_json()
            self.assertEqual(body["status"], "success", body)
            plan_id = body["plan"]["id"]
            sent = fake.last("startCapturePlan")
            self.assertEqual(sent[1][0]["id"], plan_id)
            self.assertEqual(sent[3], 10.0)

            # a second plan while one runs
            self.assertEqual(c.post("/interface/capture/start", json={"plan": DOC_PLAN}).status_code, 409)

            # progress from a status message
            status = {"type": "status", "photos": {"running": True, "plan": {"id": plan_id, "state": "waiting",
                                                                            "waiting": "cover", "step": 5, "frames_done": 30}}}
            wss._store_status(self.tname, self.tname, json.dumps(status), status)
            time.sleep(0.3)
            db.session.expire_all()
            row = db.session.get(CapturePlan, plan_id)
            self.assertEqual((row.state, row.frames_done), ("waiting", 30))
            self.assertEqual(row.last_status["waiting"], "cover")

            # pause, by a stranger and by the booker
            self.assertEqual(self.as_user(self.stranger).post("/interface/capture/control", json={"action": "pause"}).status_code, 403)
            r = c.post("/interface/capture/control", json={"action": "pause"})
            self.assertEqual(r.get_json()["plan"]["state"], "paused")

            # another telescope can't move this plan along
            capture_plans.record_plan_status("someone-else", {"id": plan_id, "state": "done"})
            db.session.expire_all()
            self.assertEqual(db.session.get(CapturePlan, plan_id).state, "paused")

            # the booking ends: the watchdog stops the plan
            booking = db.session.get(type(self.booking), self.booking.id)
            row = db.session.get(CapturePlan, plan_id)
            row.end_by = now() - timedelta(seconds=1)
            db.session.commit()
            capture_plans.stop_overdue_plans()
            self.assertIsNotNone(fake.last("stopCapturePlan"))
            db.session.expire_all()
            self.assertIsNotNone(db.session.get(CapturePlan, plan_id).stop_requested_at)

            # done
            capture_plans.record_plan_status(self.tname, {"id": plan_id, "state": "done"})
            db.session.expire_all()
            row = db.session.get(CapturePlan, plan_id)
            self.assertEqual(row.state, "done")
            self.assertIsNotNone(row.finished_at)
        finally:
            disconnect_fake(self.tname)

    def test_saved_plans_and_estimate_route(self):
        c = self.as_user(self.booker)
        r = c.post("/interface/capture/estimate", json={"plan": DOC_PLAN})
        self.assertEqual(r.get_json()["errors"], [])
        self.assertEqual(r.get_json()["estimate"]["frames"], 80)
        r = c.post("/interface/capture/saved", json={"plan": DOC_PLAN})
        saved_id = r.get_json()["plan"]["id"]
        self.assertEqual(len(c.get("/interface/capture/saved").get_json()["plans"]), 1)
        self.assertEqual(self.as_user(self.stranger).delete(f"/interface/capture/saved/{saved_id}").status_code, 404)
        self.assertEqual(c.delete(f"/interface/capture/saved/{saved_id}").status_code, 200)
        presets = c.get("/interface/capture/presets").get_json()["presets"]
        for preset in presets:
            self.assertEqual(capture_plans.validate_plan(preset["plan"]), [], preset["key"])


class AlbumPlans(Base):
    def test_plan_groups_and_zip(self):
        user_dir = os.path.join(PHOTOS, str(self.owner.id))
        os.makedirs(user_dir, exist_ok=True)
        plan = CapturePlan(id="plan_album_1", owner_id=self.owner.id, telescope_id=self.scope.id, name="Album plan",
                           plan_json=json.dumps({"steps": [{"type": "goto"}, {"type": "light"}, {"type": "dark"}]}),
                           state="done", started_at=now())
        db.session.add(plan)
        files = [("L1.JPG", "jpeg", "light", 1, "pair1"), ("L1.CR3", "raw", "light", 1, "pair1"),
                 ("D1.JPG", "jpeg", "dark", 2, None), ("other.jpg", "jpeg", None, None, None)]
        for name, kind, frame_type, step, pair in files:
            path = os.path.join(user_dir, name)
            with open(path, "wb") as f:
                f.write(jpeg_bytes())
            if frame_type:
                db.session.add(Photo(owner_id=self.owner.id, telescope_id=self.scope.id, filename=name, path=path,
                                     sha256=name.ljust(64, "0"), bytes=10, kind=kind, frame_type=frame_type,
                                     plan_id="plan_album_1", step=step, frame=0, pair_id=pair))
        db.session.commit()

        c = self.as_user(self.owner)
        body = c.get("/album/plans").get_json()
        plan_view = body["plans"][0]
        self.assertEqual(plan_view["name"], "Album plan")
        self.assertEqual([s["type"] for s in plan_view["steps"]], ["light"])
        self.assertEqual([s["type"] for s in plan_view["calibration"]], ["dark"])
        self.assertEqual(plan_view["steps"][0]["raw"], 1)

        lights = c.get("/album/photos?frame_type=light").get_json()
        self.assertEqual([p["name"] for p in lights], ["L1.JPG"])

        r = c.get("/album/plan/plan_album_1/download")
        names = sorted(zipfile.ZipFile(io.BytesIO(r.data)).namelist())
        self.assertEqual(names, ["Album_plan/calibration/dark/D1.JPG", "Album_plan/step_02_light/L1.CR3",
                                 "Album_plan/step_02_light/L1.JPG"])
        self.assertEqual(self.as_user(self.stranger).get("/album/plan/plan_album_1/download").status_code, 404)

        # deleting the JPEG takes its paired RAW and both rows with it
        r = c.post("/album/photo/delete", json={"files": ["L1.JPG"]})
        self.assertEqual(sorted(r.get_json()["deleted"]), ["L1.CR3", "L1.JPG"])
        self.assertEqual(Photo.query.filter_by(owner_id=self.owner.id, pair_id="pair1").count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
