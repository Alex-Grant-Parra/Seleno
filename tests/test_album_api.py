"""The album's library index, thumbnails, flags and metrics, and saving stacks."""
import io
import json
import os
import struct
import time
import unittest
from datetime import datetime

from PIL import Image

from harness import (app, db, make_user, make_telescope, login, PHOTOS, Photo, FrameMeta, StackResult)


def jpeg_file(path, size=(320, 200), color=(30, 40, 60), exif=None):
    img = Image.new("RGB", size, color)
    kwargs = {}
    if exif:
        e = Image.Exif()
        for key, value in exif.items():
            e[key] = value
        kwargs["exif"] = e.tobytes()
    img.save(path, "JPEG", **kwargs)


def fake_cr2(path, preview_size=(400, 260)):
    """A tiny TIFF-based RAW: IFD0 holds a baseline JPEG preview and an EXIF IFD."""
    buf = io.BytesIO()
    Image.new("RGB", preview_size, (90, 10, 10)).save(buf, "JPEG")
    jpeg = buf.getvalue()
    model = b"Canon EOS 5D Mark II\x00"
    date = b"2026:10:18 21:09:12\x00"
    entries0 = 5
    ifd0_off = 8
    ifd0_size = 2 + entries0 * 12 + 4
    exif_off = ifd0_off + ifd0_size
    exif_entries = 3
    exif_size = 2 + exif_entries * 12 + 4
    data_off = exif_off + exif_size
    model_off = data_off
    date_off = model_off + len(model)
    expo_off = date_off + len(date)
    jpeg_off = expo_off + 8
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd0_off))
    out += struct.pack("<H", entries0)
    out += struct.pack("<HHII", 259, 3, 1, 6)                       # compression: JPEG
    out += struct.pack("<HHII", 272, 2, len(model), model_off)       # model
    out += struct.pack("<HHII", 273, 4, 1, jpeg_off)                 # strip offset
    out += struct.pack("<HHII", 279, 4, 1, len(jpeg))                # strip bytes
    out += struct.pack("<HHII", 34665, 4, 1, exif_off)               # EXIF IFD
    out += struct.pack("<I", 0)
    out += struct.pack("<H", exif_entries)
    out += struct.pack("<HHII", 33434, 5, 1, expo_off)               # exposure 30/1
    out += struct.pack("<HHII", 34855, 3, 1, 800)                    # ISO
    out += struct.pack("<HHII", 36867, 2, len(date), date_off)       # DateTimeOriginal
    out += struct.pack("<I", 0)
    out += model + date + struct.pack("<II", 30, 1) + jpeg
    with open(path, "wb") as f:
        f.write(out)


class AlbumApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.owner = make_user("albumowner")
        cls.other = make_user("albumother")
        cls.scope = make_telescope("album-scope", owner=cls.owner)
        cls.dir = os.path.join(PHOTOS, str(cls.owner.id))
        os.makedirs(cls.dir, exist_ok=True)
        # A legacy pair (named by date), an uploaded light with a RAW, an uploaded dark,
        # and a RAW on its own
        jpeg_file(os.path.join(cls.dir, "photo20261018210000.jpg"))
        with open(os.path.join(cls.dir, "photo20261018210000.cr2"), "wb") as f:
            f.write(b"not really a raw")
        jpeg_file(os.path.join(cls.dir, "IMG_0412.JPG"), exif={272: "Canon EOS 5D Mark II", 270: "M42"})
        fake_cr2(os.path.join(cls.dir, "IMG_0412_raw.CR2"))
        jpeg_file(os.path.join(cls.dir, "IMG_0500.JPG"), color=(5, 5, 5))
        fake_cr2(os.path.join(cls.dir, "IMG_0600.CR2"))
        for name, kind, frame_type, pair in (("IMG_0412.JPG", "jpeg", "light", "p412"),
                                             ("IMG_0412_raw.CR2", "raw", "light", "p412"),
                                             ("IMG_0500.JPG", "jpeg", "dark", None)):
            db.session.add(Photo(owner_id=cls.owner.id, telescope_id=cls.scope.id, filename=name,
                                 path=os.path.join(cls.dir, name), sha256=name.ljust(64, "0"), bytes=1, kind=kind,
                                 frame_type=frame_type, plan_id="plan_x", step=1, frame=0, pair_id=pair,
                                 captured_at=datetime(2026, 10, 18, 21, 9, 12), exposure_s=30, iso="800"))
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def as_user(self, user):
        c = app.test_client()
        login(c, user)
        return c

    def library(self, c):
        r = c.get("/album/api/library")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return {f["name"]: f for f in r.get_json()["frames"]}

    def test_library_pairs_and_reads_exif(self):
        c = self.as_user(self.owner)
        frames = self.library(c)
        self.assertEqual(sorted(frames), ["IMG_0412.JPG", "IMG_0500.JPG", "IMG_0600.CR2", "photo20261018210000.jpg"])
        legacy = frames["photo20261018210000.jpg"]
        self.assertEqual(legacy["raw"], "photo20261018210000.cr2")
        self.assertEqual(legacy["captured_at"], "2026-10-18T21:00:00Z")
        self.assertIsNone(legacy["frame_type"])
        light = frames["IMG_0412.JPG"]
        self.assertEqual(light["raw"], "IMG_0412_raw.CR2")        # paired by the upload's pair id
        self.assertEqual(light["frame_type"], "light")
        self.assertEqual(light["target"], "M42")
        self.assertEqual(light["camera"], "Canon EOS 5D Mark II")
        self.assertEqual(light["telescope"], "album-scope")
        self.assertEqual(light["plan_id"], "plan_x")
        raw = frames["IMG_0600.CR2"]
        self.assertEqual(raw["kind"], "raw")
        self.assertEqual(raw["exposure_s"], 30)
        self.assertEqual(raw["iso"], "800")
        self.assertEqual(raw["captured_at"], "2026-10-18T21:09:12Z")
        self.assertEqual((raw["width"], raw["height"]), (400, 260))
        # Another user sees none of it
        self.assertEqual(self.library(self.as_user(self.other)), {})

    def test_thumbnails(self):
        c = self.as_user(self.owner)
        self.library(c)
        r = c.get("/album/thumb/IMG_0600.CR2?size=sm")
        self.assertEqual(r.status_code, 200)
        self.assertIn("private", r.headers["Cache-Control"])
        img = Image.open(io.BytesIO(r.data))
        self.assertEqual(img.size, (360, 234))
        r = c.get("/album/thumb/IMG_0412.JPG?size=md")
        self.assertEqual(Image.open(io.BytesIO(r.data)).size, (320, 200))   # never enlarged
        self.assertEqual(c.get("/album/thumb/photo20261018210000.cr2").status_code, 404)   # no preview inside
        self.assertEqual(c.get("/album/thumb/..%2Fx.jpg").status_code, 404)
        self.assertEqual(self.as_user(self.other).get("/album/thumb/IMG_0412.JPG").status_code, 404)

    def test_flags_types_and_notes(self):
        c = self.as_user(self.owner)
        self.library(c)
        r = c.post("/album/api/frames", json={"files": ["IMG_0412.JPG", "IMG_0600.CR2", "nope.jpg"],
                                              "set": {"flag": "reject", "rating": 2, "frame_type": "flat"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["missing"], ["nope.jpg"])
        r = c.post("/album/api/frames", json={"files": ["IMG_0600.CR2"], "set": {"target": "M31", "notes": "hazy"}})
        self.assertEqual(r.status_code, 200)
        r = c.post("/album/api/frames", json={"files": ["IMG_0412.JPG"], "set": {"notes": "Slight haze after 21:30"}})
        self.assertEqual(r.status_code, 200)
        frames = self.library(c)
        self.assertEqual(frames["IMG_0412.JPG"]["flag"], "reject")
        self.assertEqual(frames["IMG_0412.JPG"]["frame_type"], "flat")
        self.assertTrue(frames["IMG_0412.JPG"]["frame_type_set"])
        self.assertEqual(frames["IMG_0412.JPG"]["notes"], "Slight haze after 21:30")
        self.assertEqual(frames["IMG_0412.JPG"]["target"], "M42")       # untouched, still in the EXIF
        self.assertEqual(frames["IMG_0600.CR2"]["target"], "M31")       # kept in the index for a RAW
        # The JPEG's notes went into the file, where the old metadata view reads them
        meta = c.get("/album/photo/IMG_0412.JPG/metadata").get_json()["photo"]
        self.assertEqual(meta["notes"], "Slight haze after 21:30")
        # Touching the RAW file (a rescan) doesn't lose what the user set
        os.utime(os.path.join(self.dir, "IMG_0600.CR2"), (time.time() + 5, time.time() + 5))
        self.assertEqual(self.library(c)["IMG_0600.CR2"]["target"], "M31")
        # Bad values
        self.assertEqual(c.post("/album/api/frames", json={"files": ["IMG_0600.CR2"], "set": {"flag": "maybe"}}).status_code, 400)
        self.assertEqual(c.post("/album/api/frames", json={"files": ["IMG_0600.CR2"], "set": {"rating": 9}}).status_code, 400)
        self.assertEqual(c.post("/album/api/frames", json={"files": ["IMG_0600.CR2"], "set": {"frame_type": "x"}}).status_code, 400)
        # Put things back for the other tests
        c.post("/album/api/frames", json={"files": ["IMG_0412.JPG", "IMG_0600.CR2"],
                                          "set": {"flag": None, "rating": None, "frame_type": None}})

    def test_metrics(self):
        c = self.as_user(self.owner)
        self.library(c)
        metrics = {"IMG_0412.JPG": {"stars": 612, "fwhm": 3.05, "hfr": 1.77, "ecc": 0.31, "source": "jpeg",
                                    "junk": "dropped", "background": float("nan")},
                   "unknown.jpg": {"stars": 1}}
        r = c.post("/album/api/metrics", data=json.dumps(metrics).replace("NaN", "null").join(['{"metrics": ', "}"]),
                   content_type="application/json")
        self.assertEqual(r.get_json()["saved"], ["IMG_0412.JPG"])
        frame = self.library(c)["IMG_0412.JPG"]
        self.assertEqual(frame["metrics"]["stars"], 612)
        self.assertNotIn("junk", frame["metrics"])
        self.assertEqual(frame["metrics_source"], "jpeg")

    def test_stack_upload_round_trip(self):
        c = self.as_user(self.owner)
        master = b"SIMPLE  =                    T" + b" " * 50 + b"\0" * (2880 * 3 - 80)
        preview = b"\x00\x3c" * (4 * 3 * 3)    # 4 x 3 pixels, 3 channels of float16 1.0
        r = c.post("/album/api/stacks", json={"name": "M42 18 Oct", "target": "M42", "width": 5616, "height": 3744,
                                               "preview_width": 4, "preview_height": 3, "frames_used": 38,
                                               "frames_total": 40, "total_exposure_s": 1140,
                                               "master_bytes": len(master), "settings": {"method": "winsorized"},
                                               "report": {"frames": []}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        stack = r.get_json()["stack"]
        sid = stack["id"]
        self.assertEqual(stack["state"], "uploading")
        # Not finished until every file is there
        self.assertEqual(c.post(f"/album/api/stacks/{sid}/finish").status_code, 400)
        buf = io.BytesIO()
        Image.new("RGB", (40, 30)).save(buf, "JPEG")
        jpeg = buf.getvalue()
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/file/auto.jpg", data=jpeg).status_code, 200)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/file/thumb.jpg", data=jpeg).status_code, 200)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/file/thumb.jpg", data=b"nope").status_code, 400)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/file/preview.f16", data=preview[:-2]).status_code, 400)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/file/preview.f16", data=preview).status_code, 200)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/file/evil.sh", data=b"x").status_code, 404)
        # The master in two chunks; a wrong offset is refused and says where to carry on
        half = len(master) // 2
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/master?offset=0", data=master[:half]).status_code, 200)
        r = c.put(f"/album/api/stacks/{sid}/master?offset=0", data=master[half:])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["received"], half)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/master?offset={half}", data=master[half:] + b"x").status_code, 400)
        self.assertEqual(c.put(f"/album/api/stacks/{sid}/master?offset={half}", data=master[half:]).status_code, 200)
        r = c.post(f"/album/api/stacks/{sid}/finish")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["stack"]["state"], "ready")
        self.assertTrue(r.get_json()["stack"]["has_master"])
        # Served back, with ranges for the master
        r = c.get(f"/album/stacks/{sid}/master.fits", headers={"Range": "bytes=0-8"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.data, b"SIMPLE  =")
        self.assertEqual(c.get(f"/album/stacks/{sid}/preview.f16").data, preview)
        # Edits saved, then the stack listed, then deleted
        r = c.patch(f"/album/api/stacks/{sid}", json={"name": "M42 final", "edit": {"stretch": {"mode": "auto"}}})
        self.assertTrue(r.get_json()["stack"]["edited"])
        full = c.get(f"/album/api/stacks/{sid}").get_json()["stack"]
        self.assertEqual(full["edit"], {"stretch": {"mode": "auto"}})
        self.assertEqual(full["settings"], {"method": "winsorized"})
        self.assertEqual([s["name"] for s in c.get("/album/api/stacks").get_json()["stacks"]], ["M42 final"])
        other = self.as_user(self.other)
        self.assertEqual(other.get(f"/album/api/stacks/{sid}").status_code, 404)
        self.assertEqual(other.get(f"/album/stacks/{sid}/auto.jpg").status_code, 404)
        self.assertEqual(c.delete(f"/album/api/stacks/{sid}").status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".stacks", sid)))
        self.assertIsNone(db.session.get(StackResult, sid))
        # The stacks folder never shows up as a frame
        self.assertNotIn(".stacks", self.library(c))

    def test_download_zip_by_form_post(self):
        import zipfile
        c = self.as_user(self.owner)
        r = c.post("/album/download", data={"files_json": json.dumps(["IMG_0412.JPG", "IMG_0600.CR2"]), "name": "M42 night"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("M42_night.zip", r.headers["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        self.assertEqual(sorted(zf.namelist()), ["IMG_0412.JPG", "IMG_0412_raw.CR2", "IMG_0600.CR2"])
        self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist()))
        self.assertEqual(c.post("/album/download", data={"files_json": "not json"}).status_code, 400)

    def test_save_image_into_album(self):
        c = self.as_user(self.owner)
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (200, 100, 50)).save(buf, "JPEG")
        r = c.post("/album/api/save_image", data={"file": (io.BytesIO(buf.getvalue()), "x.jpg"), "format": "jpg",
                                                   "name": "M42 18 Oct", "target": "M42"},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        name = r.get_json()["name"]
        self.assertEqual(name, "M42_18_Oct.jpg")
        frame = self.library(c)[name]
        self.assertEqual(frame["frame_type"], "processed")
        self.assertEqual(frame["target"], "M42")
        r = c.post("/album/api/save_image", data={"file": (io.BytesIO(b"GIF89a"), "x.jpg"), "format": "jpg"},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        c.post("/album/photo/delete", json={"files": [name]})
        self.assertNotIn(name, self.library(c))
        self.assertIsNone(FrameMeta.query.filter_by(owner_id=self.owner.id, filename=name).first())


if __name__ == "__main__":
    unittest.main(verbosity=1)
