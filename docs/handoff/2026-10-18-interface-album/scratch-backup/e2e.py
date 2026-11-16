"""End-to-end check of the interface page in headless Chrome.

The real interface page, served by the test harness (throwaway DB, fake command
socket telescope), with a fake proto 2 telescope streaming real H.264 (made by
ffmpeg) into the live-view server. A script injected into the page drives it
and posts what it saw to /e2e-result."""
import asyncio
import importlib
import json
import os
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_PORT, LV_PORT = 18090, 18766
os.environ["LIVEVIEW_PUBLIC_URL"] = f"ws://127.0.0.1:{LV_PORT}"
os.environ["LIVEVIEW_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{WEB_PORT}"

import harness
from harness import app, db, wss, liveview, connect_fake, make_user, make_telescope
from flask import request, redirect, session, jsonify, Response
from flask_wtf import CSRFProtect

app.config["WTF_CSRF_ENABLED"] = False
CSRFProtect(app)  # for csrf_token() in base.html

# Every blueprint base.html links to
for name in sorted(os.listdir(os.path.join(harness.REPO, "controllers"))):
    if name.endswith(".py") and name != "__init__.py":
        module = importlib.import_module(f"controllers.{name[:-3]}")
        bp = getattr(module, f"{name[:-3]}_bp", None)
        if bp is not None and bp.name not in app.blueprints:
            app.register_blueprint(bp)
from models.user import user_bp
if user_bp.name not in app.blueprints:
    app.register_blueprint(user_bp)

import models.trusted_device, models.client_release, models.payment, models.contact, models.contact_thread  # noqa: F401
with app.app_context():
    db.create_all()
    owner = make_user("e2e_owner")
    scope = make_telescope("e2e-scope", owner=owner, token="e2e-token-0123456789")
    OWNER_ID = owner.id

result_box = {}
result_ready = threading.Event()


@app.route("/e2e-login")
def e2e_login():
    session["_user_id"] = str(OWNER_ID)
    session["_fresh"] = True
    session["selected_telescope"] = {"telescope_id": "e2e-scope"}
    return redirect("/interface/?e2e=1")


@app.route("/e2e.js")
def e2e_js():
    return Response(open(os.path.join(HERE, os.environ.get("E2E_SCRIPT", "e2e_page.js"))).read(), mimetype="application/javascript")


@app.route("/e2e-result", methods=["POST"])
def e2e_result():
    result_box.update(request.get_json(silent=True) or {})
    result_ready.set()
    return jsonify({"ok": True})


@app.after_request
def inject(resp):
    if request.path == "/interface/" and request.args.get("e2e") and resp.mimetype == "text/html":
        html = resp.get_data(as_text=True).replace("</body>", '<script src="/e2e.js"></script></body>')
        resp.set_data(html)
    return resp


# The fake telescope's command socket
plans = {}
fake = connect_fake("e2e-scope", {
    "getCameraChoices": {
        "shutterSpeed": ["1/250", "1/60", "30", "Bulb"], "iso": ["100", "400", "800", "1600"],
        "aperture": ["f/5.6", "f/8"], "whiteBalance": ["Auto", "Daylight"],
        "imageFormat": ["JPEG", "RAW", "RAW + JPEG"], "exposureCompensation": ["-1", "0", "+1"],
        "current": {"shutterSpeed": "30", "iso": "800", "aperture": "f/8", "whiteBalance": "Auto",
                    "imageFormat": "RAW + JPEG", "exposureCompensation": "0"},
    },
    "startLiveView": {"ok": True},
    "stopLiveView": {"ok": True},
    "getCameraStatus": {"battery": "75%", "card_free": 12_000_000_000},
    "getPlanets": ["Moon", "Jupiter", "Saturn"],
    "espEstopStatus": {"estop": False},
    "getUploadQueue": {"waiting": 2, "waiting_raw": 5, "failed": 0, "sent": 40},
    "park": {"started": "park"},
    "getJobStatus": {"running": False, "result": "parked"},
    "startCapturePlan": lambda plan: plans.setdefault(plan["id"], plan) and {"status": "started", "plan": plan["id"]},
    "pauseCapturePlan": "paused",
    "getCapturePlanStatus": lambda plan_id=None: {"id": plan_id, "state": "running", "step": 0},
    "liveViewViewers": None,
})

status = {"type": "status",
          "tracking": {"state": "tracking", "target": "M42", "parked": False, "rate": "sidereal"},
          "camera": {"connected": True, "model": "Canon EOS 600D"},
          "liveview": {"enabled": False},
          "uploads": {"waiting": 2, "failed": 0},
          "clock": {"trusted": True, "source": "gps", "offset_s": 0.02},
          "disk": 25_000_000_000,
          "esp32": {"connected": True, "estop": False}}
wss._store_status("e2e-scope", "e2e-scope", json.dumps(status), status)


# Real H.264: Constrained Baseline, no B-frames, Annex B, a keyframe every 2 s
H264 = os.path.join(HERE, "e2e.h264")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10", "-t", "6",
                "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1", "-bf", "0", "-g", "20",
                "-pix_fmt", "yuv420p", "-x264-params", "aud=1:repeat-headers=1", "-f", "h264", H264], check=True)


def access_units(data):
    # Split Annex B into access units at AUDs; keep SPS, PPS and slices with 4-byte start codes
    nals, i = [], 0
    starts = []
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else len(data)
        nal = data[s:end].rstrip(b"\x00")
        nals.append(nal)
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
    out = []
    for unit in units:
        key = any((n[0] & 0x1F) == 5 for n in unit)
        out.append((key, b"".join(b"\x00\x00\x00\x01" + n for n in unit)))
    return out


AUS = access_units(open(H264, "rb").read())
HEADER = struct.Struct(">4sBBHIQI")


def slv2(kind, key, seq, payload, stream=1):
    return HEADER.pack(b"SLV2", kind, 1 if key else 0, stream, seq, int(time.time() * 1e6), len(payload)) + payload


def poster_jpeg():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (40, 80, 160)).save(buf, "JPEG")
    return buf.getvalue()


telescope_log = []


async def fake_liveview_telescope(stop):
    import websockets
    async with websockets.connect(f"ws://127.0.0.1:{LV_PORT}") as ws:
        await ws.send(json.dumps({"token": "e2e-token-0123456789", "client_id": "e2e-scope", "proto": 2, "codecs": ["h264", "jpeg"]}))
        telescope_log.append(json.loads(await ws.recv()))

        async def reader():
            async for message in ws:
                telescope_log.append(json.loads(message))

        asyncio.ensure_future(reader())
        await ws.send(json.dumps({"type": "stream", "stream": 1, "codec": "avc1.42E01F", "width": 640, "height": 360, "fps": 10, "gop_s": 2}))
        seq, last_poster = 0, 0
        poster = poster_jpeg()
        while not stop.is_set():
            for key, payload in AUS:
                if time.time() - last_poster >= 5:
                    await ws.send(slv2(1, True, 0, poster))
                    last_poster = time.time()
                await ws.send(slv2(2, key, seq, payload))
                seq += 1
                await asyncio.sleep(0.1)
                if stop.is_set():
                    break


def main():
    from werkzeug.serving import make_server
    liveview.LIVEVIEW_WS_PORT = LV_PORT
    liveview.WS_IP = "127.0.0.1"
    threading.Thread(target=liveview.start_liveview_ws_server, daemon=True).start()
    server = make_server("127.0.0.1", WEB_PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)

    stop = threading.Event()
    threading.Thread(target=lambda: asyncio.run(fake_liveview_telescope(stop)), daemon=True).start()

    chrome = subprocess.Popen([os.path.join(HERE, "chrome.sh"), "--window-size=1600,1000", f"http://127.0.0.1:{WEB_PORT}/e2e-login"],
                              stdout=subprocess.DEVNULL, stderr=open(os.path.join(HERE, "chrome.log"), "w"))
    ok = result_ready.wait(75)
    chrome.kill()
    stop.set()
    print(json.dumps(result_box if ok else {"timeout": True}, indent=2, default=str))
    print("telescope saw:", telescope_log[:8])
    print("commands:", sorted({c[0] for c in fake.calls}))
    print("plans sent:", [p["name"] for p in plans.values()])


if __name__ == "__main__":
    main()
