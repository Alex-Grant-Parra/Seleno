"""The seleno.org app on a phone, talking straight to this telescope.

On the offline hotspot (core/networking/hotspot.py) phones have no
seleno.org. The app is installed from seleno.org, its pages are cached on the
phone, and its service worker sends the requests that are about the telescope
here instead (utils/phone_api.py answers them as seleno.org would). An https
page can only call https, so this serves HTTPS at the telescope's own name,
<slug>.tel.<domain>, which points at the hotspot's address, with a real
certificate seleno.org got for it (utils/phone_credentials.py fetches both
while online):

    https://<name>:8443/          the API (utils/phone_api.py), and /hello
    wss://<name>:8444/watch/<id>  live view, the relay's viewer protocol
                                  (static/js/liveview-player.js on the server)

Who may: every request carries an offline pass (X-Seleno-Pass) that
seleno.org signed for that user and this telescope, with the secret only the
two of them know: v1.<payload>.<HMAC-SHA256>, payload {"t": telescope id,
"u": user id, "n": username, "r": owner | shared | booking, "nbf", "exp"}.
The Wi-Fi password keeps strangers off the network; the pass decides what
they may do. CORS allows the app's own origins only; the private network
header lets Chrome's local network checks through.

Runs in offline mode with the hotspot on, once the credentials are here
(or hotspot.tls_cert / tls_key in the config, for testing).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.server
import json
import os
import socketserver
import ssl
import struct
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional, Set

from utils.log import get_logger

log = get_logger("phone_link")

API_PORT = 8443
LIVEVIEW_PORT = 8444
PASS_HEADER = "X-Seleno-Pass"
TICKET_S = 120
MAX_BODY = 1024 * 1024
FRAME_HEADER = struct.Struct(">4sBBHIQI")
FRAME_MAGIC = b"SLV2"
KIND_JPEG = 1
FLAG_KEYFRAME = 0x01

state: Dict[str, str] = {"state": "off", "detail": ""}
_lock = threading.RLock()
_api: Optional["_Server"] = None
_live: Optional["_LiveView"] = None
_running_with: Optional[tuple] = None


# --- credentials and passes ----------------------------------------------------------------

def credentials() -> Optional[Dict[str, Any]]:
	"""{telescope_id, host, secret, origins, cert, key}: from seleno.org
	(utils/phone_credentials.py), or a test cert in the hotspot config."""
	from utils import phone_credentials
	from utils.config_state import get_section

	creds = phone_credentials.load()
	s = get_section("hotspot")
	if s.get("tls_cert") and s.get("tls_key"):
		creds = dict(creds or {}, cert=s["tls_cert"], key=s["tls_key"])
		creds.setdefault("host", "localhost")
	if not creds or not creds.get("secret") or not creds.get("cert") or not creds.get("key"):
		return None
	if not (os.path.exists(creds["cert"]) and os.path.exists(creds["key"])):
		return None
	return creds


def _b64(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
	return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, prefix: str, payload: Dict[str, Any]) -> str:
	body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
	mac = hmac.new(secret.encode(), f"{prefix}.{body}".encode(), hashlib.sha256).digest()
	return f"{prefix}.{body}.{_b64(mac)}"


def verify(secret: str, prefix: str, token: str, telescope_id: Optional[str] = None,
		now: Optional[float] = None) -> Optional[Dict[str, Any]]:
	"""The payload of a good, current token signed with the secret; else None."""
	try:
		got_prefix, body, mac = str(token or "").split(".")
	except ValueError:
		return None
	if got_prefix != prefix:
		return None
	expected = hmac.new(secret.encode(), f"{prefix}.{body}".encode(), hashlib.sha256).digest()
	try:
		if not hmac.compare_digest(expected, _unb64(mac)):
			return None
		claims = json.loads(_unb64(body))
	except (ValueError, TypeError):
		return None
	if not isinstance(claims, dict):
		return None
	now = time.time() if now is None else now
	if float(claims.get("exp", 0)) < now or float(claims.get("nbf", 0)) > now + 300:
		return None
	if telescope_id is not None and claims.get("t") != telescope_id:
		return None
	return claims


def check_pass(token: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
	from utils import clock

	# The Pi's clock may be off offline (no battery): its corrected time
	return verify(creds["secret"], "v1", token, creds.get("telescope_id"), now=clock.now().timestamp())


def make_ticket(creds: Dict[str, Any], claims: Dict[str, Any]) -> str:
	from utils import clock

	now = clock.now().timestamp()
	return sign(creds["secret"], "lv", {"t": creds.get("telescope_id"), "u": claims.get("u"), "exp": now + TICKET_S})


def check_ticket(token: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
	from utils import clock

	return verify(creds["secret"], "lv", token, creds.get("telescope_id"), now=clock.now().timestamp())


def origin_allowed(origin: str, creds: Dict[str, Any]) -> bool:
	return bool(origin) and origin.rstrip("/") in {o.rstrip("/") for o in creds.get("origins") or []}


# --- the API ----------------------------------------------------------------------------------

class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
	daemon_threads = True
	allow_reuse_address = True
	creds: Dict[str, Any] = {}
	live_port = LIVEVIEW_PORT


class _Handler(http.server.BaseHTTPRequestHandler):
	server_version = "SELENO"
	protocol_version = "HTTP/1.1"
	timeout = 30  # an idle kept-alive connection

	def log_message(self, fmt: str, *args: Any) -> None:
		log.debug("%s %s", self.address_string(), fmt % args)

	@property
	def creds(self) -> Dict[str, Any]:
		return self.server.creds  # type: ignore[attr-defined]

	def _cors(self) -> Dict[str, str]:
		origin = self.headers.get("Origin", "")
		headers = {"Vary": "Origin"}
		if origin_allowed(origin, self.creds):
			headers.update({
				"Access-Control-Allow-Origin": origin,
				"Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges, Content-Disposition",
			})
		return headers

	def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
		self.send_response(status)
		for key, value in {**self._cors(), "Content-Type": content_type, "Content-Length": str(len(body)),
				"Cache-Control": "no-store", **(headers or {})}.items():
			self.send_header(key, value)
		self.end_headers()
		if self.command != "HEAD":
			self.wfile.write(body)

	def _json(self, data: Any, status: int = 200) -> None:
		self._send(status, json.dumps(data, default=str).encode(), "application/json")

	def _file(self, path: str, content_type: str, headers: Dict[str, str]) -> None:
		size = os.path.getsize(path)
		start, end, status = 0, size - 1, 200
		rng = self.headers.get("Range", "")
		if rng.startswith("bytes=") and size:
			first, _, last = rng[6:].split(",")[0].partition("-")
			try:
				if first:
					start, end = int(first), min(size - 1, int(last) if last else size - 1)
				else:
					start, end = max(0, size - int(last)), size - 1
				status = 206 if start <= end else 416
			except ValueError:
				start, end = 0, size - 1
		if status == 416:
			self._send(416, b"", content_type, {"Content-Range": f"bytes */{size}"})
			return
		length = end - start + 1 if size else 0
		self.send_response(status)
		extra = {"Content-Range": f"bytes {start}-{end}/{size}"} if status == 206 else {}
		for key, value in {**self._cors(), "Content-Type": content_type, "Content-Length": str(length),
				"Accept-Ranges": "bytes", **headers, **extra}.items():
			self.send_header(key, value)
		self.end_headers()
		if self.command == "HEAD":
			return
		with open(path, "rb") as f:
			f.seek(start)
			left = length
			while left > 0:
				chunk = f.read(min(256 * 1024, left))
				if not chunk:
					break
				self.wfile.write(chunk)
				left -= len(chunk)

	def do_OPTIONS(self) -> None:
		origin = self.headers.get("Origin", "")
		if not origin_allowed(origin, self.creds):
			self._send(403, b"", "text/plain")
			return
		self.send_response(204)
		for key, value in {**self._cors(),
				"Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS",
				"Access-Control-Allow-Headers": self.headers.get("Access-Control-Request-Headers")
					or f"Content-Type, {PASS_HEADER}, X-CSRFToken, X-CSRF-Token, Range",
				"Access-Control-Allow-Private-Network": "true",
				"Access-Control-Max-Age": "600",
				"Content-Length": "0"}.items():
			self.send_header(key, value)
		self.end_headers()

	def _route(self) -> None:
		from utils import phone_api

		parsed = urllib.parse.urlparse(self.path)
		path = parsed.path
		query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
		length = min(int(self.headers.get("Content-Length", 0) or 0), MAX_BODY)
		raw = self.rfile.read(length) if length else b""
		if path == "/hello":
			from core.networking import hotspot

			self._json({"service": "seleno-telescope", "version": 1, "telescope": self.creds.get("telescope_id"),
				"name": hotspot.name(), "paths": phone_api.paths()})
			return
		claims = check_pass(self.headers.get(PASS_HEADER, ""), self.creds)
		if claims is None:
			self._json({"status": "error", "message": "Open the Seleno app online once, to get a pass for this telescope"},
				401)
			return
		if path.rstrip("/") == "/interface/liveview_ticket":
			host = (self.headers.get("Host") or self.creds.get("host") or "localhost").rsplit(":", 1)[0]
			ticket = make_ticket(self.creds, claims)
			name = urllib.parse.quote(phone_api.telescope_name(), safe="")
			self._json({"status": "success",
				"url": f"wss://{host}:{self.server.live_port}/watch/{name}?ticket={urllib.parse.quote(ticket, safe='')}"})
			return
		body: Dict[str, Any] = {}
		if raw and "json" in (self.headers.get("Content-Type") or ""):
			try:
				parsed_body = json.loads(raw)
				body = parsed_body if isinstance(parsed_body, dict) else {}
			except ValueError:
				self._json({"status": "error", "message": "Bad JSON"}, 400)
				return
		try:
			reply = phone_api.handle(self.command, path, query, body, claims)
		except Exception as exc:
			log.exception("%s %s failed", self.command, path)
			self._json({"status": "error", "message": str(exc)[:120]}, 500)
			return
		if reply.file is not None:
			self._file(reply.file, reply.content_type, reply.headers)
		elif reply.body is not None:
			self._send(reply.status, reply.body, reply.content_type, reply.headers)
		else:
			self._json(reply.data, reply.status)

	def _safe_route(self) -> None:
		try:
			self._route()
		except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
			pass

	do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _safe_route


# --- live view ------------------------------------------------------------------------------

class _LiveView:
	"""wss://<name>:8444/watch/<id>?ticket=... as the relay serves viewers:
	a hello from the page, {"type": "telescope", "connected": true}, then
	SLV2-framed JPEGs from the camera's live view stream."""

	def __init__(self, creds: Dict[str, Any], context: ssl.SSLContext, port: int) -> None:
		self.creds = creds
		self.context = context
		self.port = port
		self.viewers: Set[Any] = set()
		self.loop: Optional[asyncio.AbstractEventLoop] = None
		self.thread = threading.Thread(target=self._run, name="phone-liveview", daemon=True)
		self.started = threading.Event()
		self.error: Optional[str] = None
		self._stop: Optional[asyncio.Event] = None
		self.seq = 0

	def start(self) -> None:
		self.thread.start()
		self.started.wait(5)
		if self.error:
			raise OSError(self.error)

	def stop(self) -> None:
		if self.loop is not None and self._stop is not None:
			self.loop.call_soon_threadsafe(self._stop.set)
		self.thread.join(5)

	def _run(self) -> None:
		self.loop = asyncio.new_event_loop()
		try:
			self.loop.run_until_complete(self._main())
		except Exception as exc:
			self.error = str(exc)
			self.started.set()

	async def _main(self) -> None:
		import websockets

		self._stop = asyncio.Event()
		async with websockets.serve(self._viewer, "0.0.0.0", self.port, ssl=self.context, max_size=64 * 1024,
				ping_interval=20, ping_timeout=30, compression=None):
			self.started.set()
			pump = asyncio.create_task(self._pump())
			await self._stop.wait()
			pump.cancel()

	def _set_viewers(self) -> None:
		try:
			from core.camera import stream as stream_module

			stream_module.get_live_stream().set_viewers(len(self.viewers))
		except Exception:
			pass

	async def _viewer(self, ws: Any) -> None:
		path = getattr(getattr(ws, "request", None), "path", "") or getattr(ws, "path", "")
		parsed = urllib.parse.urlparse(path)
		origin = (ws.request.headers.get("Origin") if getattr(ws, "request", None) else "") or ""
		if not origin_allowed(origin, self.creds):
			await ws.close(4003, "Origin not allowed")
			return
		ticket = (urllib.parse.parse_qs(parsed.query).get("ticket") or [""])[0]
		if not parsed.path.startswith("/watch/") or check_ticket(ticket, self.creds) is None:
			await ws.close(4001, "Invalid or expired ticket")
			return
		try:
			await asyncio.wait_for(ws.recv(), timeout=10)  # the hello
		except Exception:
			await ws.close(4004, "Expected hello")
			return
		await ws.send(json.dumps({"type": "telescope", "connected": True}))
		self.viewers.add(ws)
		self._set_viewers()
		try:
			async for _message in ws:
				pass  # keyframe requests: every frame is a JPEG keyframe
		except Exception:
			pass
		finally:
			self.viewers.discard(ws)
			self._set_viewers()

	async def _pump(self) -> None:
		from core.camera import stream as stream_module

		while True:
			stream = stream_module.get_live_stream()
			if not self.viewers or not stream.enabled:
				await asyncio.sleep(0.3)
				continue
			frame = await asyncio.to_thread(stream.next_frame, 1.0)
			if frame is None:
				continue
			self.seq += 1
			message = FRAME_HEADER.pack(FRAME_MAGIC, KIND_JPEG, FLAG_KEYFRAME, 0, self.seq & 0xFFFFFFFF,
				int(time.time() * 1_000_000), len(frame)) + bytes(frame)
			started = time.monotonic()
			for ws in list(self.viewers):
				try:
					await asyncio.wait_for(ws.send(message), timeout=10)
				except Exception:
					self.viewers.discard(ws)
			stream.note_sent(len(frame), time.monotonic() - started)


def viewer_count() -> int:
	return len(_live.viewers) if _live is not None else 0


# --- running it -----------------------------------------------------------------------------

def wanted() -> bool:
	from core.networking import hotspot

	return hotspot.wanted()


def _context(creds: Dict[str, Any]) -> ssl.SSLContext:
	context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
	context.minimum_version = ssl.TLSVersion.TLSv1_2
	context.load_cert_chain(creds["cert"], creds["key"])
	return context


def _stop_locked() -> None:
	global _api, _live, _running_with
	if _api is not None:
		_api.shutdown()
		_api.server_close()
		_api = None
	if _live is not None:
		_live.stop()
		_live = None
	_running_with = None


def apply(api_port: int = API_PORT, live_port: int = LIVEVIEW_PORT) -> None:
	"""Start, restart (new credentials) or stop the servers to match."""
	global _api, _live, _running_with
	with _lock:
		if not wanted():
			if _api is not None:
				_stop_locked()
				log.info("Phone link off")
			state.update(state="off", detail="")
			return
		creds = credentials()
		if creds is None:
			if _api is not None:
				_stop_locked()
			state.update(state="no credentials", detail="online once, to get them from seleno.org")
			return
		key = (json.dumps(creds, sort_keys=True), os.path.getmtime(creds["cert"]), api_port, live_port)
		if _api is not None and key == _running_with:
			return
		_stop_locked()
		try:
			context = _context(creds)
			server = _Server(("0.0.0.0", api_port), _Handler)
			server.creds = creds
			server.live_port = live_port
			# The handshake happens in each connection's own thread (on its
			# first read), so a slow phone doesn't hold up the others
			server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
			live = _LiveView(creds, context, live_port)
			live.start()
		except (OSError, ssl.SSLError) as exc:
			state.update(state="failed", detail=str(exc)[:60])
			log.warning("Phone link didn't start: %s", exc)
			return
		threading.Thread(target=server.serve_forever, name="phone-api", daemon=True).start()
		_api, _live, _running_with = server, live, key
		state.update(state="on", detail=creds.get("host", ""))
		log.info("Phone link on: https://%s:%d (live view :%d)", creds.get("host"), api_port, live_port)


def stop() -> None:
	with _lock:
		_stop_locked()
		state.update(state="off", detail="")


def status_text() -> str:
	return f"{state['state']}: {state['detail']}"[:40] if state.get("detail") and state["state"] != "on" else state["state"]
