"""The real interface and album pages on a stub server for end-to-end tests: a
throwaway database, synthetic photos, a fake telescope on the command socket (it
"uploads" a photo when asked to take one) and a fake telescope streaming an H.264
focus sweep into the live-view server.

    venv/bin/python tests/e2e/server.py            # serve on http://127.0.0.1:18090
    then open /e2e-login?next=/interface/        (logs in as the test owner)

run.py imports this and drives it with headless Chrome."""
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import struct
import sys
import threading
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
WEB_PORT, LV_PORT = 18090, 18766
os.environ["LIVEVIEW_PUBLIC_URL"] = f"ws://127.0.0.1:{LV_PORT}"
os.environ["LIVEVIEW_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{WEB_PORT}"

import harness  # noqa: E402
from harness import app, db, wss, liveview, connect_fake, make_user, make_telescope, Photo, PHOTOS, TMP  # noqa: E402
from flask import jsonify, redirect, request, session  # noqa: E402
from flask_wtf import CSRFProtect  # noqa: E402

import fixtures  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False
CSRFProtect(app)   # for csrf_token() in base.html

# Every blueprint base.html links to
for name in sorted(os.listdir(os.path.join(harness.REPO, "controllers"))):
    if name.endswith(".py") and name != "__init__.py":
        module = importlib.import_module(f"controllers.{name[:-3]}")
        bp = getattr(module, f"{name[:-3]}_bp", None)
        if bp is not None and bp.name not in app.blueprints:
            app.register_blueprint(bp)
from models.user import user_bp  # noqa: E402
if user_bp.name not in app.blueprints:
    app.register_blueprint(user_bp)
import models.trusted_device, models.client_release, models.payment, models.contact, models.contact_thread  # noqa: E402,F401
from models.album import FrameMeta, StackResult, ensure_featured_targets  # noqa: E402
from models.capture_plan import CapturePlan  # noqa: E402
from models.tables import NGCtable  # noqa: E402

DATA = os.path.join(TMP, "e2e-data")
os.makedirs(DATA, exist_ok=True)

# The night the test photos were taken: yesterday evening, local time
NIGHT = (datetime.now() - timedelta(hours=12)).replace(hour=21, minute=0, second=0, microsecond=0)
if NIGHT > datetime.now():
    NIGHT -= timedelta(days=1)
NIGHT_UTC = NIGHT + (datetime.utcnow() - datetime.now())
EARLIER = NIGHT_UTC - timedelta(days=8)

NGC_ROWS = [
    ("NGC1976", 83.82, -5.39, 4.0, "M42", 42, "Orion Nebula, Great Orion Nebula"),
    ("NGC224", 10.68, 41.27, 3.4, "M31", 31, "Andromeda Galaxy"),
    ("NGC6205", 250.42, 36.46, 5.8, "M13", 13, "Great Hercules Cluster"),
    ("NGC6853", 299.90, 22.72, 7.5, "M27", 27, "Dumbbell Nebula"),
    ("NGC6720", 283.40, 33.03, 8.8, "M57", 57, "Ring Nebula"),
    ("NGC5194", 202.47, 47.20, 8.4, "M51", 51, "Whirlpool Galaxy"),
    ("NGC598", 23.46, 30.66, 5.7, "M33", 33, "Triangulum Galaxy"),
    ("NGC7000", 314.68, 44.53, 4.0, None, None, "North America Nebula"),
]


def add_photo(owner_id, telescope, name, data, kind="jpeg", frame_type=None, captured=None, plan_id=None, step=None,
              frame=None, pair=None, target=None, exposure_s=30, iso="800"):
    folder = os.path.join(PHOTOS, str(owner_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    row = Photo(owner_id=owner_id, telescope_id=telescope.id, filename=name, path=path,
                sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), kind=kind, frame_type=frame_type,
                captured_at=captured, uploaded_at=(captured or datetime.utcnow()) + timedelta(seconds=40),
                plan_id=plan_id, step=step, frame=frame, pair_id=pair, target=target, exposure_s=exposure_s, iso=iso)
    db.session.add(row)
    return row


def build_fixtures():
    """Users, a telescope, a night of photos (with a blurred and a trailed frame), its
    darks and bias, an older night with flats, a RAW on its own, a video and a stack."""
    owner = make_user("e2e_owner")
    scope = make_telescope("e2e-scope", owner=owner, token="e2e-token-0123456789")
    scope.latitude, scope.longitude = 51.48, -0.01
    for name, ra, dec, mag, messier, number, common in NGC_ROWS:
        db.session.add(NGCtable(Name=name, RA=ra, DEC=dec, V_Mag=mag, Messier=messier, M=number, Common_names=common))
    db.session.commit()
    ensure_featured_targets()

    plan = CapturePlan(id="plan_e2e_m42", owner_id=owner.id, telescope_id=scope.id, name="M42 with calibration",
                       plan_json=json.dumps({"steps": [{"type": "goto", "name": "M42"}, {"type": "light", "count": 12},
                                                       {"type": "dark", "count": 4}, {"type": "bias", "count": 4}]}),
                       state="done", started_at=NIGHT_UTC)
    db.session.add(plan)
    metrics = {}
    for i in range(12):
        fwhm, ecc, stars = 2.7 + 0.05 * (i % 3), 0.0, 260
        if i == 4:
            fwhm = 5.0          # blurred (wind, or focus slipped)
        if i == 8:
            ecc = 0.75          # trailed
        field = fixtures.star_field(900, 600, seed=100 + i, fwhm=fwhm, ecc=ecc, stars=stars)
        name = f"IMG_{401 + i:04d}.JPG"
        captured = NIGHT_UTC + timedelta(minutes=i)
        add_photo(owner.id, scope, name, fixtures.jpeg_bytes(fixtures.to_srgb8(field), exif={272: "Canon EOS 5D Mark II", 270: "M42"}),
                  frame_type="light", captured=captured, plan_id=plan.id, step=1, frame=i, pair=f"p{i}", target="M42")
        if i % 2 == 0:
            add_photo(owner.id, scope, f"IMG_{401 + i:04d}.CR2", b"RAW" * 100, kind="raw", frame_type="light",
                      captured=captured, plan_id=plan.id, step=1, frame=i, pair=f"p{i}", target="M42")
        measured_ecc = 0.78 if i == 8 else 0.30 + 0.01 * (i % 4)
        metrics[name] = {"stars": 120 if i == 8 else 610 - 3 * i, "fwhm": round(fwhm * (1.6 if i == 8 else 1.0), 2),
                         "hfr": round(fwhm * 0.58, 2), "ecc": measured_ecc,
                         "snr": 38.0 - i * 0.2, "background_pct": 17.1 + 0.1 * i, "noise_pct": 0.41,
                         "clipped_pct": 0.02, "background_display": 0.18, "source": "jpeg"}
    dark = fixtures.to_srgb8(fixtures.star_field(900, 600, seed=1, stars=0, sky=0.02, gradient=0, nebula=False))
    for i in range(4):
        add_photo(owner.id, scope, f"IMG_{420 + i:04d}.JPG", fixtures.jpeg_bytes(dark), frame_type="dark",
                  captured=NIGHT_UTC + timedelta(minutes=30 + i), plan_id=plan.id, step=2, frame=i, target="M42")
        add_photo(owner.id, scope, f"IMG_{430 + i:04d}.JPG", fixtures.jpeg_bytes(dark), frame_type="bias",
                  captured=NIGHT_UTC + timedelta(minutes=40 + i), plan_id=plan.id, step=3, frame=i, target="M42",
                  exposure_s=1 / 4000)
    flat = fixtures.to_srgb8(fixtures.star_field(900, 600, seed=2, stars=0, sky=0.45, gradient=0.1, nebula=False))
    for i in range(3):
        add_photo(owner.id, scope, f"FLAT_{i:04d}.JPG", fixtures.jpeg_bytes(flat), frame_type="flat",
                  captured=EARLIER + timedelta(minutes=i), exposure_s=0.5)
    db.session.commit()

    folder = os.path.join(PHOTOS, str(owner.id))
    # An old upload with no record (named by date, with its RAW), a RAW on its own, a video
    old = EARLIER - timedelta(days=3)
    legacy = fixtures.jpeg_bytes(fixtures.to_srgb8(fixtures.star_field(900, 600, seed=55)))
    stamp = (old + (datetime.now() - datetime.utcnow())).strftime("%Y%m%d%H%M%S")
    with open(os.path.join(folder, f"photo{stamp}.jpg"), "wb") as f:
        f.write(legacy)
    with open(os.path.join(folder, f"photo{stamp}.cr2"), "wb") as f:
        f.write(b"not really a raw")
    with open(os.path.join(folder, "IMG_0900.CR2"), "wb") as f:
        f.write(fixtures.fake_raw(fixtures.jpeg_bytes(fixtures.to_srgb8(fixtures.star_field(600, 400, seed=9)))))
    shutil.copy(fixtures.small_video(os.path.join(DATA, "jupiter.mp4")), os.path.join(folder, "jupiter.mp4"))

    # The browser measures quality; here the numbers are put in as if it had
    from app.album_index import refresh_index
    index = refresh_index(owner.id, folder)
    for name, values in metrics.items():
        index[name].metrics_json = json.dumps(values)
        index[name].metrics_source = "jpeg"
    db.session.commit()

    # A saved stack
    stack = StackResult(id="e2e0" * 8, owner_id=owner.id, name="M42 earlier", target="M42", kind="stack", state="ready",
                        width=900, height=600, preview_width=300, preview_height=200, frames_used=10, frames_total=12,
                        total_exposure_s=300, linear=True,
                        settings_json=json.dumps({"method": "winsorized", "debayer": "bilinear"}),
                        report_json=json.dumps({"reference": "IMG_0402.JPG", "crop": {"x0": 0.01, "y0": 0.01, "x1": 0.99, "y1": 0.99},
                                                "frames": [{"name": "IMG_0405.JPG", "accepted": False, "reason": "stars blurred (FWHM 5.0 px, typical 2.7)"}],
                                                "masters": {"dark": {"frames": 4}, "bias": {"frames": 4}}, "warnings": []}),
                        master_bytes=None, master_received=0, created_at=datetime.utcnow() - timedelta(days=1))
    db.session.add(stack)
    db.session.commit()
    stack_dir = os.path.join(folder, ".stacks", stack.id)
    os.makedirs(stack_dir, exist_ok=True)
    stacked = fixtures.star_field(900, 600, seed=100, noise=0.002)
    with open(os.path.join(stack_dir, "auto.jpg"), "wb") as f:
        f.write(fixtures.jpeg_bytes(fixtures.to_srgb8(stacked)))
    with open(os.path.join(stack_dir, "thumb.jpg"), "wb") as f:
        f.write(fixtures.jpeg_bytes(fixtures.to_srgb8(stacked[::3, ::3])))
    with open(os.path.join(stack_dir, "preview.f16"), "wb") as f:
        f.write(fixtures.float16_preview(stacked[::3, ::3]))
    return owner, scope


with app.app_context():
    db.create_all()
    OWNER, SCOPE = build_fixtures()
    OWNER_ID, SCOPE_PK = OWNER.id, SCOPE.id

results = {}
results_ready = threading.Event()
shots_taken = []


@app.route("/e2e-login")
def e2e_login():
    session["_user_id"] = str(OWNER_ID)
    session["_fresh"] = True
    session["selected_telescope"] = {"telescope_id": "e2e-scope"}
    return redirect(request.args.get("next") or "/interface/")


@app.route("/e2e-result", methods=["POST"])
def e2e_result():
    results.update(request.get_json(silent=True) or {})
    results_ready.set()
    return jsonify({"ok": True})


def upload_test_shot(owner_id, exposure_s):
    """What the Pi does after a capture: send the JPEG (here, after a moment)."""
    time.sleep(1.2)
    with app.app_context():
        scope = db.session.get(harness.TelescopeRow, SCOPE_PK)
        n = Photo.query.filter_by(owner_id=owner_id).count()
        field = fixtures.star_field(900, 600, seed=300 + n, fwhm=2.8, sky=0.08 + 0.004 * float(exposure_s or 30))
        add_photo(owner_id, scope, f"IMG_{1000 + n:04d}.JPG", fixtures.jpeg_bytes(fixtures.to_srgb8(field)),
                  frame_type="light", captured=datetime.utcnow(), exposure_s=exposure_s)
        db.session.commit()


def capture_photo(current_id, exposure_s=None):
    threading.Thread(target=upload_test_shot, args=(int(current_id), exposure_s), daemon=True).start()
    return {"ok": True, "file": "IMG_test.JPG"}


plans_started = {}
fake = connect_fake("e2e-scope", {
    "getCameraChoices": {
        "shutterSpeed": ["1/4000", "1/1000", "1/250", "1/60", "1/4", "1", "30", "Bulb"],
        "iso": ["100", "200", "400", "800", "1600", "3200"],
        "aperture": ["f/5.6", "f/8"], "whiteBalance": ["Auto", "Daylight"],
        "imageFormat": ["Large Fine JPEG", "RAW", "RAW + Large Fine JPEG"],
        "exposureCompensation": ["-1", "0", "+1"],
        "current": {"shutterSpeed": "30", "iso": "800", "aperture": "f/8", "whiteBalance": "Daylight",
                    "imageFormat": "RAW + Large Fine JPEG", "exposureCompensation": "0"},
    },
    "setCameraSetting": {"ok": True},
    "capturePhoto": capture_photo,
    "startLiveView": {"ok": True},
    "stopLiveView": {"ok": True},
    "getCameraStatus": {"battery": "74%", "card_free": 22_000_000_000},
    "getPlanets": [{"name": "Moon", "alt_deg": 12}, {"name": "Jupiter", "alt_deg": 52}, {"name": "Saturn", "alt_deg": 31}],
    "espEstopStatus": {"estop": False},
    "getUploadQueue": {"waiting": 6, "waiting_raw": 18, "failed": 0, "sent": 140},
    "park": {"started": "park"},
    "centreTarget": {"started": "centre"},
    "getJobStatus": {"running": False, "result": {"pixel_scale": 1.98, "field_w_deg": 3.08, "field_h_deg": 2.05,
                                                  "rotation_deg": 91.4, "offset_arcmin": 0.4}},
    "trackCoordinates": {"ok": True},
    "trackObject": {"ok": True, "ra": 120.0, "dec": 20.0},
    "getLiveViewStats": {"codec": "h264", "width": 640, "height": 360, "fps": 10.0, "kbps": 1800},
    "startCapturePlan": lambda plan: plans_started.setdefault(plan["id"], plan) and {"status": "started", "plan": plan["id"]},
    "getCapturePlanStatus": lambda plan_id=None: {"id": plan_id, "state": "running", "step": 1, "frame": 3, "time_left_s": 2400},
    "pauseCapturePlan": "paused",
    "uploadPending": {"ok": True, "queued": 6},
})

STATUS = {"type": "status",
          "tracking": {"state": "tracking", "target": "M42", "parked": False, "rate": "sidereal"},
          "camera": {"connected": True, "model": "Canon EOS 5D Mark II", "battery": 74},
          "liveview": {"enabled": False},
          "uploads": {"waiting": 6, "waiting_raw": 18, "failed": 0},
          "clock": {"trusted": True, "source": "gps", "offset_s": 0.02},
          "disk": 41_000_000_000,
          "esp32": {"connected": True, "estop": False}}
wss._store_status("e2e-scope", "e2e-scope", json.dumps(STATUS), STATUS)


def refresh_status():
    # The Pi sends its status every 15 s; keep the stub's fresh
    while True:
        wss._store_status("e2e-scope", "e2e-scope", json.dumps(STATUS), STATUS)
        time.sleep(5)


# --- the fake telescope's live view ---------------------------------------------------

HEADER = struct.Struct(">4sBBHIQI")


def access_units(data):
    starts, i = [], 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    nals = []
    for k, s in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else len(data)
        nals.append(data[s:end].rstrip(b"\x00"))
    units, current = [], []
    for nal in nals:
        kind = nal[0] & 0x1F
        if kind == 9:
            if current:
                units.append(current)
            current = []
        elif kind != 6:
            current.append(nal)
    if current:
        units.append(current)
    return [(any((n[0] & 0x1F) == 5 for n in unit), b"".join(b"\x00\x00\x00\x01" + n for n in unit)) for unit in units]


def slv2(kind, key, seq, payload, stream=1):
    return HEADER.pack(b"SLV2", kind, 1 if key else 0, stream, seq, int(time.time() * 1e6), len(payload)) + payload


async def fake_liveview_telescope(stop):
    import websockets
    units = access_units(open(fixtures.focus_sweep_h264(os.path.join(DATA, "focus.h264")), "rb").read())
    async with websockets.connect(f"ws://127.0.0.1:{LV_PORT}") as ws:
        await ws.send(json.dumps({"token": "e2e-token-0123456789", "client_id": "e2e-scope", "proto": 2, "codecs": ["h264", "jpeg"]}))
        await ws.recv()

        async def reader():
            async for _ in ws:
                pass

        asyncio.ensure_future(reader())
        await ws.send(json.dumps({"type": "stream", "stream": 1, "codec": "avc1.42E01F", "width": 640, "height": 360, "fps": 10, "gop_s": 2}))
        seq = 0
        while not stop.is_set():
            for key, payload in units:
                await ws.send(slv2(2, key, seq, payload))
                seq += 1
                await asyncio.sleep(0.1)
                if stop.is_set():
                    break


def start(port=WEB_PORT):
    from werkzeug.serving import make_server
    liveview.LIVEVIEW_WS_PORT = LV_PORT
    liveview.WS_IP = "127.0.0.1"
    threading.Thread(target=liveview.start_liveview_ws_server, daemon=True).start()
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=refresh_status, daemon=True).start()
    time.sleep(0.5)
    stop = threading.Event()
    threading.Thread(target=lambda: asyncio.run(fake_liveview_telescope(stop)), daemon=True).start()
    return server, stop


if __name__ == "__main__":
    start()
    print(f"Serving on http://127.0.0.1:{WEB_PORT}/e2e-login?next=/interface/ (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
