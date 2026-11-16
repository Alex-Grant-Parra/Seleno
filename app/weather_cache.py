"""Local cache of the Open-Meteo weather map data.

The world map's weather layers (rain, wind, temperature) are drawn in the
browser from Open-Meteo's .om files. Each file covers one forecast hour, is
about 170 MB and holds 120-odd variables; the browser reads only the parts it
needs with HTTP range requests. Straight from Amazon S3 that takes several
slow round trips per layer, so the map was slow to show weather.

This module keeps the parts the map needs on our own server:

* A background thread checks Open-Meteo every few minutes. When a new model
  run is out it reads our three variables from every forecast hour, which
  pulls exactly the 64 KB blocks they live in into a local SQLite database
  (about 5 MB per hour, roughly 600 MB per run). The current hour goes first.
* Once a run is fully cached it becomes the one we serve; the previous run is
  dropped after a grace period, so pages that are already open keep working.
* /map/weather/... (controllers/map.py) answers the browser from the cache.
  Anything missing is fetched from S3 once and kept, or just passed through if
  the cache is over its size budget or the disk is getting full.

The cache lives in its own database (instance/weather_cache.db), not Data.db:
it rewrites hundreds of MB every few hours and shouldn't bloat or lock the
main database.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

SOURCE = os.getenv("WEATHER_CACHE_SOURCE", "https://openmeteo.s3.amazonaws.com/data_spatial").rstrip("/")
DOMAIN = "dwd_icon"
# Keep in sync with weatherVariables in static/js/map.js
VARIABLES = ("rain", "temperature_2m", "wind_gusts_10m")

BLOCK = 64 * 1024                   # how the cache stores data
# The map reads in pieces this size (WEATHER_BLOCK_BYTES in static/js/map.js),
# so warming caches whole pieces around each variable. A multiple of BLOCK.
CLIENT_BLOCK = 512 * 1024
CHECK_EVERY_S = 600                 # how often to look for a new model run
OLD_RUN_GRACE_S = 1800              # keep the previous run this long after switching
MAX_CACHE_BYTES = 3 * 1024 ** 3     # stop storing new blocks past this
MIN_FREE_BYTES = 2 * 1024 ** 3      # ...or when the disk has less than this free
MAX_RANGE_BYTES = 32 * 1024 ** 2    # biggest single range we'll serve
WARM_WORKERS = 3
TIMEOUT = (10, 60)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("WEATHER_CACHE_DB") or os.path.join(_REPO, "instance", "weather_cache.db")

_FILE_RE = re.compile(r"^(?P<domain>[a-z0-9_]+)/(?P<run>\d{4}/\d{2}/\d{2}/\d{4}Z)/\d{4}-\d{2}-\d{2}T\d{4}\.om$")

FileInfo = namedtuple("FileInfo", "size etag last_modified")


class NotFound(Exception):
    pass


class UpstreamError(Exception):
    pass


# --- Storage -------------------------------------------------------------

_local = threading.local()


def _db():
    """This thread's connection to the cache database (made on first use)."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == DB_PATH:
        return conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 30000")
    # auto_vacuum only takes effect before the first table exists
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run TEXT PRIMARY KEY,        -- e.g. 2026/11/09/1200Z
            meta TEXT NOT NULL,          -- the run's latest.json
            state TEXT NOT NULL,         -- downloading | ready
            created REAL NOT NULL,
            ready_at REAL
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,       -- e.g. dwd_icon/2026/11/09/1200Z/2026-11-09T2200.om
            run TEXT NOT NULL,
            size INTEGER NOT NULL,
            etag TEXT,
            last_modified TEXT,
            warmed INTEGER NOT NULL DEFAULT 0  -- 2 once fully cached (see warm_file)
        );
        CREATE INDEX IF NOT EXISTS files_run ON files(run);
        CREATE TABLE IF NOT EXISTS blocks (
            path TEXT NOT NULL,
            idx INTEGER NOT NULL,        -- byte offset / BLOCK
            data BLOB NOT NULL,
            PRIMARY KEY (path, idx)
        );
    """)
    _local.conn = conn
    _local.path = DB_PATH
    return conn


def _session():
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        _local.session = session
    return session


def _cache_bytes():
    try:
        total = 0
        for suffix in ("", "-wal"):
            if os.path.exists(DB_PATH + suffix):
                total += os.path.getsize(DB_PATH + suffix)
        return total
    except OSError:
        return 0


def _room_to_store():
    try:
        free = shutil.disk_usage(os.path.dirname(DB_PATH)).free
    except OSError:
        return False
    return free > MIN_FREE_BYTES and _cache_bytes() < MAX_CACHE_BYTES


def _kept_runs(conn):
    return {row[0] for row in conn.execute("SELECT run FROM runs")}


# --- Upstream ------------------------------------------------------------

def _upstream_get(path, headers=None, method="GET"):
    try:
        response = _session().request(method, f"{SOURCE}/{path}", headers=headers or {}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise UpstreamError(str(exc)) from exc
    if response.status_code in (403, 404):
        raise NotFound(path)
    if response.status_code >= 400:
        raise UpstreamError(f"{method} {path}: HTTP {response.status_code}")
    return response


def _upstream_info(path):
    response = _upstream_get(path, method="HEAD")
    size = response.headers.get("Content-Length")
    if not size:
        raise UpstreamError(f"no size for {path}")
    return FileInfo(int(size), response.headers.get("ETag"), response.headers.get("Last-Modified"))


def _upstream_range(path, start, end):
    """Bytes start..end inclusive."""
    data = _upstream_get(path, headers={"Range": f"bytes={start}-{end}"}).content
    if len(data) != end - start + 1:
        raise UpstreamError(f"short read of {path}: wanted {end - start + 1} bytes, got {len(data)}")
    return data


# --- Model runs ----------------------------------------------------------

def run_dir(reference_time):
    """'2026-11-09T12:00:00Z' -> '2026/11/09/1200Z' (the run's folder on S3)."""
    t = _parse_time(reference_time)
    return t.strftime("%Y/%m/%d/%H%MZ")


def file_path(run, valid_time):
    """The .om file for one forecast hour of a run."""
    return f"{DOMAIN}/{run}/{_parse_time(valid_time).strftime('%Y-%m-%dT%H%M')}.om"


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _register_run(meta_text):
    meta = json.loads(meta_text)
    run = run_dir(meta["reference_time"])
    conn = _db()
    conn.execute("INSERT OR IGNORE INTO runs (run, meta, state, created) VALUES (?, ?, 'downloading', ?)",
                 (run, meta_text, time.time()))
    return run, meta


_upstream_latest = {"at": 0.0, "text": None}
_upstream_latest_lock = threading.Lock()


def _fetch_upstream_latest(max_age_s=60):
    with _upstream_latest_lock:
        if _upstream_latest["text"] and time.time() - _upstream_latest["at"] < max_age_s:
            return _upstream_latest["text"]
    text = _upstream_get(f"{DOMAIN}/latest.json").text
    json.loads(text)  # must be valid
    with _upstream_latest_lock:
        _upstream_latest.update(at=time.time(), text=text)
    return text


def latest_meta():
    """latest.json for the newest fully cached run, or Open-Meteo's own until one is ready."""
    row = _db().execute("SELECT meta FROM runs WHERE state = 'ready' ORDER BY run DESC LIMIT 1").fetchone()
    if row:
        return row[0]
    text = _fetch_upstream_latest()
    _register_run(text)
    return text


# --- Reading -------------------------------------------------------------

def _check_path(path):
    match = _FILE_RE.match(path)
    if not match or match.group("domain") != DOMAIN:
        raise NotFound(path)
    return match.group("run")


def file_info(path):
    """Size, ETag and Last-Modified of an .om file (what the browser asks with HEAD)."""
    run = _check_path(path)
    conn = _db()
    row = conn.execute("SELECT size, etag, last_modified FROM files WHERE path = ?", (path,)).fetchone()
    if row:
        return FileInfo(*row)
    info = _upstream_info(path)
    if run in _kept_runs(conn):
        conn.execute("INSERT OR IGNORE INTO files (path, run, size, etag, last_modified) VALUES (?, ?, ?, ?, ?)",
                     (path, run, info.size, info.etag, info.last_modified))
    return info


def read_range(path, start, length):
    """Bytes [start, start + length) of an .om file, from the cache where possible."""
    run = _check_path(path)
    info = file_info(path)
    if start < 0 or length <= 0 or start + length > info.size:
        raise ValueError("range outside the file")
    if length > MAX_RANGE_BYTES:
        raise ValueError("range too large")

    first, last = start // BLOCK, (start + length - 1) // BLOCK
    conn = _db()
    blocks = dict(conn.execute("SELECT idx, data FROM blocks WHERE path = ? AND idx BETWEEN ? AND ?",
                               (path, first, last)))
    missing = [i for i in range(first, last + 1) if i not in blocks]
    if missing:
        store = run in _kept_runs(conn) and _room_to_store()
        fetched = []
        for group in _consecutive(missing):
            lo = group[0] * BLOCK
            hi = min(info.size, (group[-1] + 1) * BLOCK) - 1
            data = _upstream_range(path, lo, hi)
            for n, idx in enumerate(group):
                piece = data[n * BLOCK:(n + 1) * BLOCK]
                blocks[idx] = piece
                fetched.append((path, idx, piece))
        if store and fetched:
            conn.executemany("INSERT OR IGNORE INTO blocks (path, idx, data) VALUES (?, ?, ?)", fetched)

    joined = b"".join(blocks[i] for i in range(first, last + 1))
    offset = start - first * BLOCK
    return joined[offset:offset + length]


def _consecutive(indexes):
    group = []
    for idx in indexes:
        if group and idx != group[-1] + 1:
            yield group
            group = []
        group.append(idx)
    if group:
        yield group


# --- Warming -------------------------------------------------------------

def _om_filesystem(path, size):
    """A read-only fsspec filesystem over one cached .om file, for the omfiles reader."""
    import fsspec
    from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

    class CachedFile(AbstractBufferedFile):
        def _fetch_range(self, start, end):
            end = min(end, size)
            return read_range(path, start, end - start) if end > start else b""

    class CachedFS(AbstractFileSystem):
        protocol = "weathercache"
        cachable = False

        def info(self, p, **kwargs):
            return {"name": p, "size": size, "type": "file"}

        def _open(self, p, mode="rb", **kwargs):
            # cache_type "none": every read goes to read_range, which has its own cache
            return CachedFile(self, p, mode=mode, size=size, block_size=BLOCK, cache_type="none")

    return CachedFS()


def warm_file(path):
    """Read our variables from one file so every block they use lands in the cache."""
    import omfiles

    info = file_info(path)
    reader = omfiles.OmFileReader.from_fsspec(_om_filesystem(path, info.size), path)
    try:
        for name in VARIABLES:
            try:
                variable = reader.get_child_by_name(name)
            except ValueError:
                variable = None
            if variable is None:
                # Normal for the first hour of a run: rain is the past hour's total
                continue
            variable[...]  # reading the whole field pulls in all of its blocks
    finally:
        reader.close()
    _fill_client_blocks(path, info.size)
    if not _room_to_store():
        # Blocks were passed through rather than stored, so this hour isn't cached
        raise UpstreamError("weather cache is full or the disk is low on space")
    # 2 = cached in whole CLIENT_BLOCK pieces (1 was the older 64 KB-only warm)
    _db().execute("UPDATE files SET warmed = 2 WHERE path = ?", (path,))


def _fill_client_blocks(path, size):
    """Cache the whole CLIENT_BLOCK-sized pieces around what we already hold,
    so the map's bigger reads never fall through to S3."""
    per = CLIENT_BLOCK // BLOCK
    held = [row[0] for row in _db().execute("SELECT idx FROM blocks WHERE path = ?", (path,))]
    for window in sorted({idx // per for idx in held}):
        start = window * CLIENT_BLOCK
        read_range(path, start, min(CLIENT_BLOCK, size - start))


def _hours_in_order(valid_times, now=None):
    """Forecast hours ordered current hour first, then forwards, then the earlier ones."""
    now = now or datetime.now(timezone.utc)
    parsed = [(_parse_time(t), t) for t in valid_times]
    upcoming = [t for when, t in parsed if when >= now.replace(minute=0, second=0, microsecond=0)]
    earlier = [t for when, t in parsed if when < now.replace(minute=0, second=0, microsecond=0)]
    return upcoming + list(reversed(earlier))


def refresh(now=None):
    """One pass: register the newest run, cache it, switch to it, drop old runs.
    Returns the run's state."""
    run, meta = _register_run(_fetch_upstream_latest(max_age_s=0))
    conn = _db()
    state = conn.execute("SELECT state FROM runs WHERE run = ?", (run,)).fetchone()[0]
    warmed = {row[0] for row in conn.execute("SELECT path FROM files WHERE run = ? AND warmed = 2", (run,))}
    paths = [file_path(run, t) for t in _hours_in_order(meta.get("valid_times", []), now)]
    todo = [p for p in paths if p not in warmed]
    failures = []
    if todo:  # a new run, or a ready one cached before CLIENT_BLOCK existed
        if not _room_to_store():
            logger.warning("weather cache: not enough disk space to cache run %s", run)
            cleanup(now)
            return state

        def warm(path):
            try:
                warm_file(path)
            except Exception as exc:  # retried on the next pass
                failures.append((path, exc))

        started = time.time()
        with ThreadPoolExecutor(max_workers=WARM_WORKERS, thread_name_prefix="weather-warm") as pool:
            list(pool.map(warm, todo))
        if failures:
            logger.warning("weather cache: %d of %d hours of run %s failed, e.g. %s: %s",
                           len(failures), len(paths), run, failures[0][0], failures[0][1])
        else:
            logger.info("weather cache: run %s cached (%d hours in %.0f s)", run, len(todo), time.time() - started)
    if not failures and state != "ready":
        conn.execute("UPDATE runs SET state = 'ready', ready_at = ? WHERE run = ?", (time.time(), run))
        state = "ready"
    cleanup(now)
    return state


def cleanup(now=None):
    """Drop runs we no longer need: older runs once the newest ready one has been
    out for the grace period, and half-downloaded runs that a newer run replaced."""
    conn = _db()
    runs = conn.execute("SELECT run, state, ready_at FROM runs ORDER BY run DESC").fetchall()
    if not runs:
        return
    now_ts = now.timestamp() if now else time.time()
    newest_ready = next((r for r in runs if r[1] == "ready"), None)
    keep = {runs[0][0]}  # the newest run, whatever its state
    if newest_ready:
        keep.add(newest_ready[0])
        if now_ts - (newest_ready[2] or 0) < OLD_RUN_GRACE_S:
            # Just switched: pages opened before the switch still use the previous one
            previous = next((r for r in runs if r[1] == "ready" and r[0] < newest_ready[0]), None)
            if previous:
                keep.add(previous[0])
    drop = [r[0] for r in runs if r[0] not in keep]
    stray = [row[0] for row in conn.execute("SELECT DISTINCT run FROM files")
             if row[0] not in keep and row[0] not in drop]
    for run in drop + stray:
        conn.execute("DELETE FROM blocks WHERE path IN (SELECT path FROM files WHERE run = ?)", (run,))
        conn.execute("DELETE FROM files WHERE run = ?", (run,))
        conn.execute("DELETE FROM runs WHERE run = ?", (run,))
    if drop or stray:
        conn.execute("PRAGMA incremental_vacuum")
        logger.info("weather cache: dropped runs %s", ", ".join(drop + stray))


def start_background(interval_s=CHECK_EVERY_S):
    """Keep the cache filled whether or not anyone is looking at the map.
    WEATHER_CACHE=off turns it off."""
    if os.getenv("WEATHER_CACHE", "on").lower() in ("0", "off", "false", "no"):
        return None

    def run():
        while True:
            try:
                refresh()
            except Exception:
                logger.exception("weather cache refresh failed")
            time.sleep(interval_s)

    thread = threading.Thread(target=run, name="weather-cache", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    # One pass by hand: venv/bin/python -m app.weather_cache
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("state:", refresh())
