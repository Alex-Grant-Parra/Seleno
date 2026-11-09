"""The world map's local weather cache (app/weather_cache.py) and the
/map/weather/ endpoint that serves it, against a fake Open-Meteo built from
small synthetic .om files. No network; the cache database is a temp file."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

import harness  # noqa: F401  (sets WEATHER_CACHE_DB to a temp folder)
import numpy as np
import omfiles
from flask import Flask

from app import weather_cache as wc
from controllers.map import map_bp

TMP = tempfile.mkdtemp(prefix="weather-cache-test-")
NOW = datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)


def make_om(path, seed, first_hour=False):
    """An .om file shaped like Open-Meteo's: a root group of 2D variables, ours
    plus some we don't use. Noise doesn't compress, so each spans many blocks.
    Like the real thing, a run's first hour has no rain (it's the past hour's total)."""
    rng = np.random.default_rng(seed)
    writer = omfiles.OmFileWriter.at_path(path)
    children = []
    names = ["cloud_cover", "rain", "snow_depth", "temperature_2m", "wind_gusts_10m", "pressure_msl"]
    if first_hour:
        names.remove("rain")
    for name in names:
        data = rng.random((360, 720), dtype=np.float32) * 100
        children.append(writer.write_array(data, chunks=[32, 32], scale_factor=1000.0, name=name))
    root = writer.write_group("", children)
    writer.close(root)
    with open(path, "rb") as f:
        return f.read()


class FakeOpenMeteo:
    """Stands in for S3: latest.json plus the .om files of whatever runs exist."""

    def __init__(self):
        self.files = {}
        self.latest = None
        self.range_calls = 0
        self.head_calls = 0

    def publish(self, reference_time, hours, seed):
        run = wc.run_dir(reference_time)
        start = wc._parse_time(reference_time)
        valid = [datetime.fromtimestamp(start.timestamp() + 3600 * h, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                 for h in range(hours)]
        for n, t in enumerate(valid):
            path = wc.file_path(run, t)
            self.files[path] = make_om(os.path.join(TMP, f"{seed}-{n}.om"), seed * 100 + n, first_hour=n == 0)
        self.latest = json.dumps({"completed": True, "reference_time": reference_time,
                                  "valid_times": valid, "variables": list(wc.VARIABLES)})
        return run, valid

    def get(self, path, headers=None, method="GET"):
        class R:
            pass
        r = R()
        r.headers = {}
        if path.endswith("latest.json"):
            r.status_code, r.text, r.content = 200, self.latest, self.latest.encode()
            return r
        if path not in self.files:
            raise wc.NotFound(path)
        data = self.files[path]
        r.status_code = 200
        r.headers = {"Content-Length": str(len(data)), "ETag": f'"{hash(path) & 0xffff:x}"',
                     "Last-Modified": "Mon, 09 Nov 2026 12:00:00 GMT"}
        if method == "HEAD":
            self.head_calls += 1
            r.content = b""
            return r
        first, last = headers["Range"][6:].split("-")
        self.range_calls += 1
        r.status_code, r.content = 206, data[int(first):int(last) + 1]
        return r


class WeatherCacheTest(unittest.TestCase):
    def setUp(self):
        wc.DB_PATH = os.path.join(TMP, f"cache-{self._testMethodName}.db")
        wc._upstream_latest.update(at=0.0, text=None)
        wc.CLIENT_BLOCK = 4 * wc.BLOCK  # smaller than the real 512 KB to suit the small test files
        self.fake = FakeOpenMeteo()
        self._orig = wc._upstream_get
        wc._upstream_get = self.fake.get
        app = Flask("weather-test")
        app.register_blueprint(map_bp)
        self.client = app.test_client()

    def tearDown(self):
        wc._upstream_get = self._orig

    def test_refresh_caches_the_run_and_serves_it_without_upstream(self):
        run, valid = self.fake.publish("2026-11-09T12:00:00Z", hours=4, seed=1)
        self.assertEqual(wc.refresh(now=NOW), "ready")
        self.assertEqual(wc.refresh(now=NOW), "ready")  # a second pass has nothing to do
        conn = wc._db()
        self.assertEqual(conn.execute("SELECT count(*) FROM files WHERE warmed = 2").fetchone()[0], 4)

        # Only our variables' blocks are stored, not whole files
        stored = conn.execute("SELECT sum(length(data)) FROM blocks").fetchone()[0]
        total = sum(len(d) for d in self.fake.files.values())
        self.assertLess(stored, total * 0.75, (stored, total))

        # The browser's reads (via omfiles, as the JS library would) need nothing from upstream
        self.fake.range_calls = self.fake.head_calls = 0
        path = wc.file_path(run, valid[2])
        reader = omfiles.OmFileReader.from_fsspec(wc._om_filesystem(path, len(self.fake.files[path])), path)
        cached = reader.get_child_by_name("temperature_2m")[...]
        reader.close()
        self.assertEqual((self.fake.range_calls, self.fake.head_calls), (0, 0))
        direct = omfiles.OmFileReader.from_path(os.path.join(TMP, "1-2.om"))
        np.testing.assert_array_equal(cached, direct.get_child_by_name("temperature_2m")[...])
        direct.close()

        # The map reads in CLIENT_BLOCK pieces; every piece around our data is cached whole
        size = len(self.fake.files[path])
        held = {r[0] for r in conn.execute("SELECT idx FROM blocks WHERE path = ?", (path,))}
        per = wc.CLIENT_BLOCK // wc.BLOCK
        for window in sorted({i // per for i in held}):
            start = window * wc.CLIENT_BLOCK
            self.assertEqual(wc.read_range(path, start, min(wc.CLIENT_BLOCK, size - start)),
                             self.fake.files[path][start:start + wc.CLIENT_BLOCK])
        self.assertEqual(self.fake.range_calls, 0)

        # Hours are cached current hour first: 13:00 before 12:00
        self.assertEqual(wc._hours_in_order(valid, NOW)[:2], [valid[1], valid[2]])
        self.assertEqual(wc._hours_in_order(valid, NOW)[-1], valid[0])

    def test_ready_run_from_older_cache_is_topped_up(self):
        run, valid = self.fake.publish("2026-11-09T12:00:00Z", hours=2, seed=7)
        wc.refresh(now=NOW)
        conn = wc._db()
        conn.execute("UPDATE files SET warmed = 1")
        wc.CLIENT_BLOCK = 8 * wc.BLOCK
        self.assertEqual(wc.refresh(now=NOW), "ready")
        self.assertEqual(conn.execute("SELECT count(*) FROM files WHERE warmed = 2").fetchone()[0], 2)

    def test_endpoint_head_range_and_latest(self):
        run, valid = self.fake.publish("2026-11-09T12:00:00Z", hours=2, seed=2)
        wc.refresh(now=NOW)
        path = wc.file_path(run, valid[0])
        data = self.fake.files[path]
        url = f"/map/weather/data_spatial/{path}"

        latest = self.client.get("/map/weather/data_spatial/dwd_icon/latest.json")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(json.loads(latest.data)["reference_time"], "2026-11-09T12:00:00Z")
        self.assertEqual(latest.headers["Access-Control-Allow-Origin"], "*")

        head = self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["Content-Length"], str(len(data)))
        self.assertTrue(head.headers["ETag"])

        for start, end in [(0, 99), (70000, 140000), (len(data) - 500, len(data) - 1)]:
            r = self.client.get(url, headers={"Range": f"bytes={start}-{end}"})
            self.assertEqual(r.status_code, 206)
            self.assertEqual(r.data, data[start:end + 1])
            self.assertEqual(r.headers["Content-Range"], f"bytes {start}-{end}/{len(data)}")
            self.assertIn("immutable", r.headers["Cache-Control"])

        options = self.client.options(url)
        self.assertEqual(options.status_code, 204)
        self.assertIn("Range", options.headers["Access-Control-Allow-Headers"])

        self.assertEqual(self.client.get(url).status_code, 416)  # no Range
        self.assertEqual(self.client.get("/map/weather/data_spatial/other_model/latest.json").status_code, 404)
        self.assertEqual(self.client.get("/map/weather/data_spatial/dwd_icon/../../etc/passwd").status_code, 404)
        self.assertEqual(self.client.get(f"/map/weather/data_spatial/{wc.DOMAIN}/2026/11/09/1200Z/nope.om",
                                         headers={"Range": "bytes=0-9"}).status_code, 404)

    def test_new_run_replaces_old_after_grace(self):
        old_run, _ = self.fake.publish("2026-11-09T06:00:00Z", hours=2, seed=3)
        wc.refresh(now=NOW)
        new_run, _ = self.fake.publish("2026-11-09T12:00:00Z", hours=2, seed=4)
        wc._upstream_latest.update(at=0.0, text=None)
        wc.refresh(now=NOW)
        conn = wc._db()
        runs = {r: s for r, s in conn.execute("SELECT run, state FROM runs")}
        self.assertEqual(runs, {old_run: "ready", new_run: "ready"})  # old kept for open pages
        self.assertEqual(json.loads(wc.latest_meta())["reference_time"], "2026-11-09T12:00:00Z")

        wc.cleanup(now=datetime.fromtimestamp(time.time() + wc.OLD_RUN_GRACE_S + 1, timezone.utc))
        self.assertEqual([r[0] for r in conn.execute("SELECT run FROM runs")], [new_run])
        self.assertEqual(conn.execute("SELECT count(*) FROM files WHERE run = ?", (old_run,)).fetchone()[0], 0)

    def test_serves_upstream_until_a_run_is_cached(self):
        run, valid = self.fake.publish("2026-11-09T12:00:00Z", hours=1, seed=5)
        # No refresh yet: latest.json comes from upstream and reads still work
        self.assertEqual(json.loads(wc.latest_meta())["reference_time"], "2026-11-09T12:00:00Z")
        path = wc.file_path(run, valid[0])
        self.assertEqual(wc.read_range(path, 1000, 5000), self.fake.files[path][1000:6000])

    def test_full_cache_passes_through_without_storing(self):
        run, valid = self.fake.publish("2026-11-09T12:00:00Z", hours=1, seed=6)
        wc.latest_meta()  # registers the run
        saved = wc.MAX_CACHE_BYTES
        wc.MAX_CACHE_BYTES = 0
        try:
            path = wc.file_path(run, valid[0])
            self.assertEqual(wc.read_range(path, 0, 200000), self.fake.files[path][:200000])
            self.assertEqual(wc._db().execute("SELECT count(*) FROM blocks").fetchone()[0], 0)
            self.assertEqual(wc.refresh(now=NOW), "downloading")  # won't mark a run ready it couldn't store
        finally:
            wc.MAX_CACHE_BYTES = saved


if __name__ == "__main__":
    unittest.main()
