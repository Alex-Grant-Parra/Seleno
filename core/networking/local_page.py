"""A page on the local network: live view, photos and status, no server.

For a phone at the telescope, offline or not: http://<the Pi>:8080/
(config local_page.port). It asks for the six-digit code shown on the
front panel (Setup > Local page); the code changes every time the client
starts. Off unless config local_page.enabled (the panel's toggle).

What it offers:
	/                   the page (dark red, for night eyes)
	/stream.mjpg        live view (MJPEG; holds live view open while watched)
	/snapshot.jpg       the newest frame
	/api/status         utils/client_status.collect() and the focus numbers
	/api/photo          POST: take a photo (saved on the Pi)
	/api/snapshot       POST: save the current frame
	/api/record         POST {"on": true|false, "kind": "video"|"timelapse"}
	/api/stop           POST: stop tracking and the motors
	/api/files          recent photos, snapshots and recordings
	/files/<path>       one of them, from the photos folder only

It's plain HTTP on the local network: the code keeps out other people on
the same Wi-Fi, not someone listening to it. Nothing that moves the
telescope towards anything is here; go-tos stay on the panel and the
server, where the Sun guard asks.
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional

from utils.log import get_logger

log = get_logger("local_page")

HOLDER = "local page"
COOKIE = "seleno_session"
MAX_FAILED_PER_MIN = 5

_server: Optional["_Server"] = None
_code = f"{secrets.randbelow(1_000_000):06d}"
_sessions: Dict[str, float] = {}
_failed: list = []
_viewers = 0
_viewers_lock = threading.Lock()


def code() -> str:
	return _code


def address() -> str:
	from utils import pi_system

	port = _settings().get("port", 8080)
	return f"http://{pi_system.ip_address()}:{port}/"


def _settings() -> Dict[str, Any]:
	try:
		from utils.config_state import get_section

		return get_section("local_page")
	except Exception:
		return {"enabled": False, "port": 8080}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SELENO</title><style>
:root{--bg:#120000;--fg:#ff6b5a;--dim:#8a2a20;--line:#3a0a06}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
main{max-width:960px;margin:0 auto;padding:12px 16px}
img{width:100%;background:#000;border:1px solid var(--line);border-radius:6px;filter:sepia(1) hue-rotate(-50deg) saturate(3)}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
button{flex:1 1 120px;padding:12px;border-radius:6px;border:1px solid var(--dim);background:#1e0300;color:var(--fg);font-size:15px}
#status{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:13px;color:var(--fg)}
a{color:var(--fg)}
</style></head><body><main>
<h1 style="font-size:18px;margin:4px 0">SELENO</h1>
<img id="view" alt="Live view (start it with the button)">
<div class="row">
<button onclick="toggleView()" id="viewBtn">Live view</button>
<button onclick="post('/api/photo')">Photo</button>
<button onclick="post('/api/snapshot')">Snapshot</button>
<button onclick="record()" id="recBtn">Record</button>
<button onclick="if(confirm('Stop tracking and the motors?'))post('/api/stop')">Stop</button>
</div>
<div id="msg"></div><div id="status">...</div>
<p><a href="/api/files">Files on the Pi</a></p>
</main><script>
let viewing=false,recording=false;
function toggleView(){viewing=!viewing;document.getElementById('view').src=viewing?'/stream.mjpg?'+Date.now():'';
document.getElementById('viewBtn').textContent=viewing?'Stop live view':'Live view';}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify(body||{})});const j=await r.json().catch(()=>({}));
document.getElementById('msg').textContent=j.error||j.status||'';return j;}
async function record(){const j=await post('/api/record',{on:!recording});if(!j.error){recording=!recording;
document.getElementById('recBtn').textContent=recording?'Stop recording':'Record';}}
async function tick(){try{const s=await (await fetch('/api/status')).json();const t=s.tracking||{},c=s.camera||{},f=s.focus||{};
recording=!!(s.recording||{}).recording;document.getElementById('recBtn').textContent=recording?'Stop recording':'Record';
document.getElementById('status').textContent=
`Telescope: ${t.state||'?'} ${t.target||''}\\nRA ${(t.ra??0).toFixed(3)}  Dec ${(t.dec??0).toFixed(3)}\\n`+
`Camera: ${c.connected?(c.model||'connected'):'not found'}  ${c.camera_fps?c.camera_fps+' fps':''}\\n`+
`Focus: HFR ${f.hfr??'-'} px  sharpness ${f.sharpness??'-'}\\nClock: ${(s.clock||{}).state||'?'}  Disk: ${s.disk||'?'}`;}catch(e){}}
setInterval(tick,2000);tick();
</script></body></html>"""

LOGIN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SELENO</title>
<style>body{margin:0;background:#120000;color:#ff6b5a;font:16px system-ui,sans-serif}
form{max-width:320px;margin:20vh auto;padding:0 16px}input,button{width:100%;box-sizing:border-box;
padding:12px;margin:6px 0;border-radius:6px;border:1px solid #8a2a20;background:#1e0300;color:#ff6b5a;font-size:18px}
</style></head><body><form method="post" action="/login"><p>The code is on the telescope's
front panel, under Setup &gt; Local page.</p><input name="code" inputmode="numeric" autocomplete="off"
placeholder="Code"><button>Open</button><p>{message}</p></form></body></html>"""


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
	daemon_threads = True
	allow_reuse_address = True


class _Handler(http.server.BaseHTTPRequestHandler):
	server_version = "SELENO"

	def log_message(self, fmt: str, *args: Any) -> None:
		log.debug("%s %s", self.address_string(), fmt % args)

	# --- helpers --------------------------------------------------------------

	def _session(self) -> Optional[str]:
		cookies = self.headers.get("Cookie", "")
		for part in cookies.split(";"):
			name, _, value = part.strip().partition("=")
			if name == COOKIE and value in _sessions:
				return value
		return None

	def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		for key, value in (headers or {}).items():
			self.send_header(key, value)
		self.end_headers()
		self.wfile.write(body)

	def _json(self, data: Any, status: int = 200) -> None:
		self._send(status, json.dumps(data, default=str).encode(), "application/json")

	def _body(self) -> Dict[str, Any]:
		length = min(int(self.headers.get("Content-Length", 0) or 0), 65536)
		raw = self.rfile.read(length) if length else b""
		if self.headers.get("Content-Type", "").startswith("application/json"):
			try:
				data = json.loads(raw or b"{}")
				return data if isinstance(data, dict) else {}
			except ValueError:
				return {}
		return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

	# --- routes -----------------------------------------------------------------

	def do_GET(self) -> None:
		path = urllib.parse.urlparse(self.path).path
		if self._session() is None:
			if path == "/":
				self._send(200, LOGIN.replace("{message}", "").encode(), "text/html; charset=utf-8")
			else:
				self._json({"error": "Log in first"}, 401)
			return
		try:
			if path == "/":
				self._send(200, PAGE.encode(), "text/html; charset=utf-8")
			elif path == "/stream.mjpg":
				self._stream()
			elif path == "/snapshot.jpg":
				frame = _latest_frame()
				if frame is None:
					self._json({"error": "No live view frame"}, 404)
				else:
					self._send(200, frame, "image/jpeg")
			elif path == "/api/status":
				self._json(_status())
			elif path == "/api/files":
				self._json(_files())
			elif path.startswith("/files/"):
				self._file(urllib.parse.unquote(path[len("/files/"):]))
			else:
				self._json({"error": "Not found"}, 404)
		except (BrokenPipeError, ConnectionResetError):
			pass

	def do_POST(self) -> None:
		path = urllib.parse.urlparse(self.path).path
		if path == "/login":
			self._login()
			return
		if self._session() is None:
			self._json({"error": "Log in first"}, 401)
			return
		body = self._body()
		try:
			if path == "/api/photo":
				from utils import photos

				names = photos.take_photo()
				self._json({"status": f"Saved {os.path.basename(names[-1])}"})
			elif path == "/api/snapshot":
				from core.camera.recorder import snapshot

				self._json({"status": f"Saved {os.path.basename(snapshot())}"})
			elif path == "/api/record":
				from core.camera.recorder import get_recorder

				recorder = get_recorder()
				if body.get("on"):
					self._json({"status": recorder.start(str(body.get("kind", "video")), float(body.get("interval_s", 10)))})
				else:
					self._json({"status": recorder.stop()})
			elif path == "/api/stop":
				from core.hardware.tracking import stop_tracking

				stop_tracking()
				self._json({"status": "Stopped"})
			else:
				self._json({"error": "Not found"}, 404)
		except Exception as exc:
			self._json({"error": str(exc)[:120]}, 400)

	def _login(self) -> None:
		now = time.monotonic()
		_failed[:] = [t for t in _failed if now - t < 60.0]
		if len(_failed) >= MAX_FAILED_PER_MIN:
			self._send(429, LOGIN.replace("{message}", "Too many tries: wait a minute.").encode(), "text/html; charset=utf-8")
			return
		given = str(self._body().get("code", "")).strip()
		if not secrets.compare_digest(given, _code):
			_failed.append(now)
			self._send(403, LOGIN.replace("{message}", "That's not the code.").encode(), "text/html; charset=utf-8")
			return
		token = secrets.token_urlsafe(24)
		_sessions[token] = now
		self.send_response(303)
		self.send_header("Location", "/")
		self.send_header("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")
		self.send_header("Content-Length", "0")
		self.end_headers()

	def _stream(self) -> None:
		global _viewers
		from core.camera.service import get_camera_service

		service = get_camera_service()
		with _viewers_lock:
			_viewers += 1
			service.acquire_preview(HOLDER)
		self.send_response(200)
		self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		seq = 0
		last = 0.0
		try:
			while True:
				new_seq, frame, _at = service.frames.wait_newer(seq, 2.0)
				if new_seq == seq or frame is None:
					continue
				seq = new_seq
				if time.monotonic() - last < 0.1:
					continue  # 10 fps is plenty on a phone
				last = time.monotonic()
				self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
					+ str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
		except (BrokenPipeError, ConnectionResetError, OSError):
			pass
		finally:
			with _viewers_lock:
				_viewers -= 1
				if _viewers <= 0:
					_viewers = 0
					service.release_preview(HOLDER)

	def _file(self, relative: str) -> None:
		from utils import photos

		base = os.path.realpath(photos.base_dir())
		path = os.path.realpath(os.path.join(base, relative))
		if not path.startswith(base + os.sep) or not os.path.isfile(path):
			self._json({"error": "Not found"}, 404)
			return
		size = os.path.getsize(path)
		ext = os.path.splitext(path)[1].lower()
		content_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".avi": "video/x-msvideo",
			".png": "image/png", ".json": "application/json", ".jsonl": "text/plain"}.get(ext, "application/octet-stream")
		self.send_response(200)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(size))
		self.send_header("Content-Disposition", f'inline; filename="{os.path.basename(path)}"')
		self.end_headers()
		with open(path, "rb") as f:
			while True:
				chunk = f.read(256 * 1024)
				if not chunk:
					break
				self.wfile.write(chunk)


def _latest_frame() -> Optional[bytes]:
	from core.camera.service import get_camera_service

	_seq, frame, at = get_camera_service().frames.latest()
	return frame if frame is not None and time.monotonic() - at < 10.0 else None


def _status() -> Dict[str, Any]:
	from utils.client_status import collect

	status = collect()
	frame = _latest_frame()
	if frame is not None:
		try:
			from core.camera.focus import analyse

			result = analyse(frame)
			status["focus"] = {"hfr": result.get("hfr"), "sharpness": result.get("sharpness"), "peak": result.get("peak")}
		except Exception:
			pass
	return status


def _files(limit: int = 60) -> Dict[str, Any]:
	from utils import photos

	base = photos.base_dir()
	found = []
	for root, _dirs, files in os.walk(base):
		for name in files:
			if name.startswith(".") or name.endswith((".json", ".tmp")):
				continue
			path = os.path.join(root, name)
			try:
				found.append((os.path.getmtime(path), os.path.relpath(path, base), os.path.getsize(path)))
			except OSError:
				continue
	found.sort(reverse=True)
	return {"files": [{"path": rel, "url": "/files/" + urllib.parse.quote(rel), "bytes": size,
		"time": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))} for mtime, rel, size in found[:limit]]}


def start(port: Optional[int] = None) -> str:
	"""Start serving (if it isn't already); returns the address."""
	global _server
	if _server is None:
		port = int(port or _settings().get("port", 8080))
		_server = _Server(("0.0.0.0", port), _Handler)
		threading.Thread(target=_server.serve_forever, name="local-page", daemon=True).start()
		log.info("Local page on port %d, code %s (also on the front panel)", port, _code)
	return address()


def stop() -> None:
	global _server
	if _server is not None:
		_server.shutdown()
		_server.server_close()
		_server = None
		_sessions.clear()


def running() -> bool:
	return _server is not None


def start_if_enabled() -> None:
	if _settings().get("enabled"):
		start()


def set_enabled(on: bool) -> Optional[str]:
	from utils.config_state import save_section

	save_section("local_page", {"enabled": bool(on)})
	if on:
		try:
			start()
		except OSError as exc:
			return f"Couldn't start: {exc}"[:60]
		return f"Code {_code}"
	stop()
	return "Local page off"
