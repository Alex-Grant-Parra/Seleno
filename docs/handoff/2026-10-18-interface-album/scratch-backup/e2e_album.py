import io, json, os, subprocess, sys, threading, time
os.environ["E2E_SCRIPT"] = "e2e_album.js"
import e2e
from e2e import app, db
from flask import request, redirect, session
from harness import Photo, CapturePlan, PHOTOS
from PIL import Image
from datetime import datetime

@app.route("/e2e-album-login")
def album_login():
    session["_user_id"] = str(e2e.OWNER_ID); session["_fresh"] = True
    return redirect("/album?e2e=1")

@app.after_request
def inject_album(resp):
    if request.path == "/album" and request.args.get("e2e") and resp.mimetype == "text/html":
        resp.set_data(resp.get_data(as_text=True).replace("</body>", '<script src="/e2e.js"></script></body>'))
    return resp

with app.app_context():
    d = os.path.join(PHOTOS, str(e2e.OWNER_ID)); os.makedirs(d, exist_ok=True)
    db.session.add(CapturePlan(id="plan_e2e_album", owner_id=e2e.OWNER_ID, telescope_id=1, name="Orion night",
        plan_json=json.dumps({"steps": [{"type": "goto"}, {"type": "light"}, {"type": "dark"}]}), state="done", started_at=datetime.utcnow()))
    for name, ft, step, color in [("IMG_0001.JPG", "light", 1, (200, 50, 50)), ("IMG_0002.JPG", "dark", 2, (5, 5, 5)), ("photo_20261017_220000.jpg", None, None, (50, 50, 200))]:
        p = os.path.join(d, name); buf = io.BytesIO(); Image.new("RGB", (120, 80), color).save(buf, "JPEG"); open(p, "wb").write(buf.getvalue())
        if ft:
            db.session.add(Photo(owner_id=e2e.OWNER_ID, filename=name, path=p, sha256=name.ljust(64, "0"), bytes=1000, kind="jpeg",
                                 frame_type=ft, plan_id="plan_e2e_album", step=step, frame=0, captured_at=datetime(2026, 10, 17, 21, 30)))
    db.session.commit()

from werkzeug.serving import make_server
server = make_server("127.0.0.1", e2e.WEB_PORT, app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
chrome = subprocess.Popen([os.path.join(e2e.HERE, "chrome.sh"), f"http://127.0.0.1:{e2e.WEB_PORT}/e2e-album-login"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = e2e.result_ready.wait(40); chrome.kill()
print(json.dumps(e2e.result_box if ok else {"timeout": True}, indent=2))
