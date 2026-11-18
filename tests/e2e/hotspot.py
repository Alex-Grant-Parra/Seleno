"""The seleno.org app with a telescope on its own Wi-Fi, end to end in headless
Chrome (app/offline_access.py, static/js/offline-link.js, static/js/star_map_sw.js).

    E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/hotspot.py

The e2e server (tests/e2e/server.py) stands in for seleno.org; a fake
telescope stands in for the Pi's phone link (SelenoClient
core/networking/phone_link.py) on https://127.0.0.1:8443 with a self-signed
certificate, reached by its offline name through Chrome's host resolver. It
checks the offline pass the app sends, like the Pi does, and records what it's
asked. Then:

1. the interface, online: the service worker installs, the app gets its passes;
2. SelenoLink.connectNearby(): the app finds the telescope and switches to it; the
   interface lists just that telescope and its commands go to it;
3. the star map: its data still comes from seleno.org, tracking goes to the
   telescope;
4. seleno.org goes away (the server stops): the interface and album open from
   the app's cache and still talk to the telescope.

Takes about half a minute. Screenshots go to tests/e2e/out/hotspot-*.png."""
import asyncio
import datetime
import hashlib
import hmac
import base64
import http.server
import json
import os
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["OFFLINE_TLS_SUFFIX"] = "tel.e2e.test"
os.environ["OFFLINE_APP_ORIGINS"] = "http://127.0.0.1:18090"
os.environ["CLOUDFLARE_API_TOKEN"] = ""

import run  # noqa: E402  (the Chrome driver)

FAKE_NAME = "Field Scope"
requests_seen = []


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def self_signed(folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.tel.e2e.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=7))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.tel.e2e.test")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


def fake_telescope(server_module, cert, key):
    """An HTTPS telescope answering like the Pi's phone link, for the paths the
    pages use here."""
    from app import offline_access
    from harness import TelescopeRow, app, db

    def secret():
        with app.app_context():
            row = db.session.get(TelescopeRow, server_module.SCOPE_PK)
            return offline_access.record_for(row).secret

    status = dict(server_module.STATUS)
    paths = {"exact": ["/interface/get_telescopes", "/interface/get_selected_telescope", "/interface/select_telescope",
                       "/interface/telescope_status", "/interface/command", "/interface/get_camera_choices",
                       "/interface/api/sky", "/interface/api/targets", "/interface/api/latest_captures",
                       "/interface/liveview_ticket", "/track_star", "/stop_tracking", "/get_tracking_status",
                       "/api/telescope_position", "/api/telescope_dead_zones", "/album/api/library",
                       "/interface/search_object", "/user/night_mode"],
             "prefixes": ["/album/thumb/", "/album/photo/", "/interface/capture/", "/album/api/"]}
    entry = {"id": 1, "telescope_id": FAKE_NAME, "ip_address": None, "type": "telescope", "last_seen": time.time(),
             "online": True}
    access = {"control": True, "owner": True, "shared": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def cors(self):
            origin = self.headers.get("Origin", "")
            if origin == "http://127.0.0.1:18090":
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

        def reply(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", self.headers.get("Access-Control-Request-Headers", "*"))
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route()

        def route(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            path = self.path.split("?")[0]
            if path == "/hello":
                return self.reply({"service": "seleno-telescope", "version": 1, "telescope": "e2e-scope",
                                   "name": FAKE_NAME, "paths": paths})
            token = self.headers.get("X-Seleno-Pass", "")
            try:
                prefix, payload, mac = token.split(".")
                good = hmac.compare_digest(hmac.new(secret().encode(), f"{prefix}.{payload}".encode(),
                                                    hashlib.sha256).digest(), unb64(mac))
                claims = json.loads(unb64(payload)) if good else None
            except ValueError:
                claims = None
            requests_seen.append((self.command, path, body, bool(claims)))
            if not claims or claims.get("t") != "e2e-scope":
                return self.reply({"status": "error", "message": "No pass"}, 401)
            path = path.rstrip("/")
            if path == "/interface/get_telescopes":
                return self.reply({"status": "success", "telescopes": [entry]})
            if path in ("/interface/get_selected_telescope", "/interface/select_telescope"):
                return self.reply({"status": "success", "telescope": entry, "access": access})
            if path == "/interface/telescope_status":
                return self.reply({"status": "success", "telescope_id": FAKE_NAME, "age_s": 0, "telescope_status": status,
                                   "viewers": 0, "plan": None, "access": access, "booking": None,
                                   "next_booking_start": None})
            if path == "/interface/get_camera_choices":
                return self.reply({"iso": ["100", "800"], "shutterSpeed": ["1/100", "30"], "current": {"iso": "800"}})
            if path == "/interface/command":
                return self.reply({"status": "success", "result": {"ok": True}})
            if path == "/track_star":
                return self.reply({"status": "tracking", "ra": body.get("ra"), "dec": body.get("dec"),
                                   "telescope_id": FAKE_NAME, "result": {"ok": True}, "redirect": True})
            if path == "/album/api/library":
                return self.reply({"status": "success", "frames": [], "plans": {}, "stacks": 0})
            if path == "/interface/api/latest_captures":
                return self.reply({"status": "success", "frames": [], "now": None})
            if path == "/interface/api/targets":
                return self.reply({"status": "success", "site": None, "night": None, "moon": None, "targets": []})
            if path.startswith("/interface/capture"):
                return self.reply({"status": "success", "presets": [], "plans": []})
            return self.reply({"status": "error", "message": "Not here"})

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd = Server(("127.0.0.1", 8443), Handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def scenario(page, base, web):
    R = {"checks": {}, "failures": []}

    def check(name, ok, detail=None):
        R["checks"][name] = bool(ok) if detail is None else detail
        if not ok:
            R["failures"].append(f"{name}: {detail}")

    async def goto(path):
        page.loaded.clear()
        await page.send("Page.navigate", {"url": base + path})
        await asyncio.wait_for(page.loaded.wait(), 30)

    async def until(expr, ms=10000):
        return await page.evaluate(f"(async () => {{ const t = Date.now(); while (Date.now() - t < {ms}) {{"
                                   f" try {{ if (await ({expr})) return true; }} catch (e) {{}}"
                                   f" await new Promise(r => setTimeout(r, 100)); }} return false; }})()", timeout=ms / 1000 + 10)

    async def until_reloads(expr, ms=15000):
        """until(), carrying on across the page reloading itself (it does when
        it connects to a telescope)."""
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            try:
                if await until(expr, 1000):
                    return True
            except RuntimeError as exc:
                if "navigated" not in str(exc) and "context" not in str(exc).lower():
                    raise
                await asyncio.sleep(0.3)
        return False

    async def shot(name):
        await asyncio.sleep(0.5)
        await page.screenshot(os.path.join(run.OUT, f"hotspot-{name}.png"))

    # 1. Online: the worker installs, the app gets its passes
    await goto("/e2e-login?next=/interface/")
    check("worker", await until("navigator.serviceWorker.ready.then(() => true)"))
    await goto("/interface/")
    check("controlled", await until("!!navigator.serviceWorker.controller"))
    check("passes", await until("SelenoLink.passes().length === 1"),
          await page.evaluate("JSON.stringify(SelenoLink.passes().map(p => [p.telescope_id, p.host, p.role]))"))
    check("online list", await until("[...document.querySelectorAll('#telescopeSelect option')].some(o => o.value === 'e2e-scope')"))
    await goto("/album")       # stored for later
    await goto("/StarMap")
    check("star map stored", await until("caches.open('seleno-starmap-pages-v2').then(c => c.match('/StarMap')).then(Boolean)", 15000))
    await goto("/interface/")
    check("no nearby button", await page.evaluate("document.getElementById('offlineLinkConnect') === null"))

    # 2. Telescope nearby
    page.loaded.clear()
    await page.evaluate("SelenoLink.connectNearby(), true")
    await asyncio.wait_for(page.loaded.wait(), 20)
    check("connected", await until("SelenoLink.ready.then(s => s && s.name === 'Field Scope')"),
          await page.evaluate("JSON.stringify(SelenoLink.state)"))
    check("only the telescope", await until(
        "(() => { const v = [...document.querySelectorAll('#telescopeSelect option')].map(o => o.value).filter(Boolean);"
        " return v.length === 1 && v[0] === 'Field Scope'; })()"),
          await page.evaluate("[...document.querySelectorAll('#telescopeSelect option')].map(o => o.textContent).join(' | ')"))
    check("badge", await page.evaluate("!document.getElementById('offlineLinkBadge').classList.contains('d-none')"))
    await page.evaluate("fetch('/interface/command', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                        " body: JSON.stringify({action: 'park'})}).then(r => r.json())")
    check("command to the telescope", any(p == "/interface/command" and b.get("action") == "park" and ok
                                          for _m, p, b, ok in requests_seen))
    check("status from the telescope", any(p.startswith("/interface/telescope_status") for _m, p, _b, _ok in requests_seen))
    await shot("interface")

    # 3. The star map: its data from the app, tracking to the telescope
    await goto("/StarMap")
    await until("document.readyState === 'complete'")
    result = await page.evaluate("fetch('/track_star', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                                 " body: JSON.stringify({name: 'Vega', ra: 279.23, dec: 38.78, mag: 0.03})}).then(r => r.json())")
    check("tracking to the telescope", result.get("status") == "tracking" and any(p == "/track_star" for _m, p, _b, _ok in requests_seen), result)
    check("star data not from the telescope", not any(p.startswith("/api/stars") for _m, p, _b, _ok in requests_seen))
    await shot("star-map")

    # 4. seleno.org gone: the app's cached pages, still talking to the telescope
    web.shutdown()
    before = len(requests_seen)
    await goto("/interface/")
    check("interface offline", await until("document.getElementById('telescopeSelect') !== null"))
    check("telescope offline", await until(
        "[...document.querySelectorAll('#telescopeSelect option')].some(o => o.value === 'Field Scope')"))
    check("asked the telescope offline", len(requests_seen) > before)
    await shot("interface-offline")
    await goto("/album")
    check("album offline", await until("document.body && document.body.innerText.length > 50"))
    check("album from the telescope", await until("true") and any(p == "/album/api/library" for _m, p, _b, _ok in requests_seen))
    await shot("album-offline")
    # The installed app opens at "/" (not stored): offline it goes on to the
    # interface, which finds the telescope by itself, as on a first visit
    await page.evaluate("""(async () => {
        const s = JSON.parse(localStorage.getItem('seleno.offline.v1')); s.last = null;
        localStorage.setItem('seleno.offline.v1', JSON.stringify(s));
        const c = await caches.open('seleno-telescope-v1'); await c.delete('/__seleno_telescope__');
        navigator.serviceWorker.controller.postMessage({type: 'seleno-telescope', action: 'none'});
    })()""")
    await goto("/")
    check("app start goes to the interface offline", await until_reloads("location.pathname === '/interface/'"))
    check("found the telescope from the start page", await until_reloads(
        "[...document.querySelectorAll('#telescopeSelect option')].some(o => o.value === 'Field Scope')"))
    await shot("start-offline")
    check("every telescope request had a pass", all(ok for _m, _p, _b, ok in requests_seen), [r for r in requests_seen if not r[3]])
    return R


async def main():
    import websockets

    import server

    os.makedirs(run.OUT, exist_ok=True)
    folder = tempfile.mkdtemp(prefix="e2e-hotspot-")
    cert, key = self_signed(folder)
    web, stop = server.start()
    telescope = fake_telescope(server, cert, key)
    profile = tempfile.mkdtemp(prefix="e2e-chrome-")
    chrome = subprocess.Popen([run.CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                               "--no-default-browser-check", f"--user-data-dir={profile}",
                               f"--remote-debugging-port={run.DEBUG_PORT}", "--window-size=1500,1000",
                               "--host-resolver-rules=MAP *.tel.e2e.test 127.0.0.1", "--ignore-certificate-errors",
                               "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        targets = None
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{run.DEBUG_PORT}/json"))
                break
            except Exception:
                time.sleep(0.1)
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=200_000_000) as ws:
            page = run.Page(ws)
            reader = asyncio.ensure_future(page.reader())
            for domain in ("Page", "Runtime", "Log"):
                await page.send(f"{domain}.enable")
            await page.send("Emulation.setDeviceMetricsOverride", {"width": 1500, "height": 1000, "deviceScaleFactor": 1,
                                                                   "mobile": False})
            started = time.time()
            try:
                result = await scenario(page, f"http://127.0.0.1:{server.WEB_PORT}", web)
            except Exception as error:
                result = {"failures": [f"scenario crashed: {error!r}"]}
            result["console_errors"] = [e for e in page.errors if "favicon" not in str(e)]
            result["seconds"] = round(time.time() - started, 1)
            reader.cancel()
    finally:
        chrome.kill()
        stop.set()
        telescope.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(folder, ignore_errors=True)
    print(json.dumps(result, indent=1, default=str))
    print("the telescope got:", sorted({p for _m, p, _b, _ok in requests_seen}))
    return 1 if result.get("failures") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
