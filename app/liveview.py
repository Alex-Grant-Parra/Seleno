"""Live-view server: telescopes send frames in, browsers watch.

Telescopes connect to wss://liveview.<domain> (port 8000) and authenticate with
their first message. Proto 2 telescopes (`"proto": 2` in that message) get a
`hello` naming the codec to send, then send JSON control messages and binary
SLV2 frames (H.264 or JPEG) that are relayed to viewers unchanged. Telescopes
without `proto` keep the original path: every message is a JPEG and nothing
is ever sent back to them.

Browsers watch at /watch/<client_id>?ticket=<ticket> on the same server. The
latest JPEG of every telescope also feeds the /liveview/<id> MJPEG endpoint.
"""
import asyncio
import collections
import concurrent.futures
import logging
import os
import re
import struct
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

import ujson as json
import websockets
from flask import Response

from app.WebsocketServer import (
    LIVEVIEW_WS_PORT,
    WS_IP,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    ClientError,
    ClientNotConnected,
    _is_telescope_capable_client,
    _security_log,
    _token_may_command,
    authenticate_token_with_policy,
    check_rate_limit,
    client_manager,
    get_ws_client_ip,
    heartbeat_tasks,
    record_telescope_connection,
    telescope_heartbeat,
)

logger = logging.getLogger(__name__)

lv_event_loop = None

# Binary frame header (proto 2): magic, kind, flags, stream, seq, time (us), length
FRAME_HEADER = struct.Struct(">4sBBHIQI")
FRAME_MAGIC = b"SLV2"
KIND_JPEG = 1
KIND_H264 = 2
FLAG_KEYFRAME = 0x01

POSTER_S = 5                         # JPEG poster interval asked of H.264 telescopes
GOP_CAP_BYTES = 8 * 1024 * 1024      # frames kept since the last keyframe
VIEWER_QUEUE_BYTES = 4 * 1024 * 1024
KEYFRAME_REQUEST_MIN_S = 1.0
MAX_VIEWERS_PER_IP = 8
VIEWER_HELLO_TIMEOUT_S = 10

WATCH_TICKET_SALT = "liveview-watch"
WATCH_TICKET_MAX_AGE = 60

# The stream message proto 1 viewers get: a JPEG stream, with stream id 0
LEGACY_STREAM_TEXT = json.dumps({"type": "stream", "stream": 0, "codec": "jpeg"})


# Latest JPEG per telescope, for /liveview/<id> and save_latest_frame
latest_frames = {}
_frame_seq = {}
_frame_cond = threading.Condition()


def store_frame(client_id, payload):
    with _frame_cond:
        latest_frames[client_id] = payload
        _frame_seq[client_id] = _frame_seq.get(client_id, 0) + 1
        _frame_cond.notify_all()


def clear_frames(client_id):
    with _frame_cond:
        latest_frames.pop(client_id, None)
        _frame_cond.notify_all()


def parse_frame(message):
    # (kind, flags) of a valid SLV2 frame, or None
    if not isinstance(message, (bytes, bytearray)) or len(message) < FRAME_HEADER.size:
        return None
    magic, kind, flags, _stream, _seq, _time_us, length = FRAME_HEADER.unpack_from(message, 0)
    if magic != FRAME_MAGIC or kind not in (KIND_JPEG, KIND_H264):
        return None
    if length != len(message) - FRAME_HEADER.size:
        return None
    return kind, flags


def wrap_jpeg(payload, seq, stream=0):
    # A proto 1 JPEG as an SLV2 frame, so the browser player can show it
    header = FRAME_HEADER.pack(
        FRAME_MAGIC, KIND_JPEG, FLAG_KEYFRAME, stream,
        seq & 0xFFFFFFFF, int(time.time() * 1_000_000), len(payload),
    )
    return header + payload


# Viewer counts: viewer sockets plus open MJPEG streams, told to each telescope
class ViewerCounts:
    SETTLE_S = 2.0
    RETRY_S = 3.0
    MAX_RETRIES = 5

    def __init__(self):
        self._lock = threading.Lock()
        self._sockets = collections.Counter()
        self._mjpeg = collections.Counter()
        self._sent = {}      # client_id -> count the telescope was last told
        self._timers = {}
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='lv-viewers')

    def count(self, client_id):
        with self._lock:
            return self._sockets[client_id] + self._mjpeg[client_id]

    def mjpeg_count(self, client_id):
        with self._lock:
            return self._mjpeg[client_id]

    def change(self, client_id, sockets=0, mjpeg=0):
        with self._lock:
            self._sockets[client_id] = max(0, self._sockets[client_id] + sockets)
            self._mjpeg[client_id] = max(0, self._mjpeg[client_id] + mjpeg)
        if mjpeg and lv_event_loop is not None:
            # MJPEG readers can't decode H.264, so they take part in the codec rule
            asyncio.run_coroutine_threadsafe(_update_codec(client_id), lv_event_loop)
        self._schedule(client_id)

    def telescope_connected(self, client_id):
        # A telescope that was never told assumes someone is watching, so tell it now
        with self._lock:
            self._sent.pop(client_id, None)
        self._schedule(client_id)

    def _schedule(self, client_id):
        with self._lock:
            old = self._timers.pop(client_id, None)
            if old:
                old.cancel()
            timer = threading.Timer(self.SETTLE_S, self._flush, args=(client_id,))
            timer.daemon = True
            self._timers[client_id] = timer
        timer.start()

    def _flush(self, client_id):
        with self._lock:
            self._timers.pop(client_id, None)
            count = self._sockets[client_id] + self._mjpeg[client_id]
            if self._sent.get(client_id) == count:
                return
        self._pool.submit(self._tell, client_id, count, 0)

    def _tell(self, client_id, count, attempt):
        try:
            sent = None
            if lv_event_loop is not None:
                sent = asyncio.run_coroutine_threadsafe(_send_viewers(client_id, count), lv_event_loop).result(timeout=5)
            if sent is None:
                # Proto 1 telescopes take the count as a command (it blocks for up to 5 s)
                client_manager.command(client_id, "liveViewViewers", [count], timeout=5)
        except ClientNotConnected:
            if attempt < self.MAX_RETRIES:
                timer = threading.Timer(self.RETRY_S, lambda: self._pool.submit(self._tell, client_id, self.count(client_id), attempt + 1))
                timer.daemon = True
                timer.start()
            return
        except ClientError as e:
            # An older client without the command: don't keep asking
            logger.info("liveViewViewers not accepted by %s: %s", client_id, e)
        except Exception:
            logger.exception("Failed to send viewer count to %s", client_id)
            return

        with self._lock:
            self._sent[client_id] = count
            changed = (self._sockets[client_id] + self._mjpeg[client_id]) != count
        if changed:
            self._schedule(client_id)


viewer_counts = ViewerCounts()


class Viewer:
    """One browser watching. Frames queue here and a sender task writes them out."""

    def __init__(self, ws, h264):
        self.ws = ws
        self.h264 = h264
        self.queue = collections.deque()
        self.queued_bytes = 0
        self.wake = asyncio.Event()
        self.skip_h264 = False

    def offer(self, message, kind=None, key=False):
        size = len(message)
        if kind == KIND_H264:
            if not self.h264:
                return
            # After a drop, skip to the next keyframe: never drop part of a GOP and carry on
            if self.skip_h264 and not key:
                return
            if self.queued_bytes + size > VIEWER_QUEUE_BYTES:
                self.skip_h264 = True
                return
            self.skip_h264 = False
        elif kind == KIND_JPEG:
            if self.queued_bytes + size > VIEWER_QUEUE_BYTES:
                return
        self.queue.append(message)
        self.queued_bytes += size
        self.wake.set()

    async def run(self):
        while True:
            if not self.queue:
                self.wake.clear()
                await self.wake.wait()
                continue
            message = self.queue.popleft()
            self.queued_bytes -= len(message)
            await self.ws.send(message)


class Channel:
    """One telescope's live view: its socket, the stream so far, and its viewers.
    Only touched from the live-view event loop."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.telescope = None
        self.proto = 0
        self.codecs = ('jpeg',)
        self.codec = None
        self.stream_text = None
        self.gop = []
        self.gop_bytes = 0
        self.poster = None          # latest JPEG frame, as sent to viewers
        self.viewers = set()
        self.jpeg_seq = 0
        self.last_keyframe_request = 0.0
        self.bad_frame_logged = 0.0

    def reset_stream(self):
        self.stream_text = None
        self.gop = []
        self.gop_bytes = 0
        self.poster = None

    def wanted_codec(self):
        # H.264 only while every viewer can decode it
        if 'h264' not in self.codecs:
            return 'jpeg'
        if any(not viewer.h264 for viewer in self.viewers):
            return 'jpeg'
        if viewer_counts.mjpeg_count(self.client_id) > 0:
            return 'jpeg'
        return 'h264'

    async def send_telescope(self, payload):
        ws = self.telescope
        if ws is None or self.proto < 2:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except websockets.exceptions.ConnectionClosed:
            return False

    async def update_codec(self):
        if self.proto < 2 or self.telescope is None:
            return
        wanted = self.wanted_codec()
        if wanted != self.codec:
            self.codec = wanted
            await self.send_telescope({"type": "codec", "codec": wanted})

    async def request_keyframe(self):
        now = time.monotonic()
        if now - self.last_keyframe_request < KEYFRAME_REQUEST_MIN_S:
            return
        self.last_keyframe_request = now
        await self.send_telescope({"type": "keyframe"})

    def broadcast(self, message, kind=None, key=False):
        for viewer in list(self.viewers):
            viewer.offer(message, kind, key)

    def log_bad_frame(self, reason):
        now = time.monotonic()
        if now - self.bad_frame_logged >= 60:
            self.bad_frame_logged = now
            print(f"[LiveView] Dropped bad frame from {self.client_id}: {reason}")


_channels = {}


def _channel(client_id):
    channel = _channels.get(client_id)
    if channel is None:
        channel = _channels[client_id] = Channel(client_id)
    return channel


def _drop_channel_if_idle(channel):
    if channel.telescope is None and not channel.viewers and _channels.get(channel.client_id) is channel:
        _channels.pop(channel.client_id, None)


async def _send_viewers(client_id, count):
    # True/False when sent (or failed) over a proto 2 socket; None means use the command
    channel = _channels.get(client_id)
    if channel is None or channel.telescope is None or channel.proto < 2:
        return None
    return await channel.send_telescope({"type": "viewers", "count": count})


async def _update_codec(client_id):
    channel = _channels.get(client_id)
    if channel is not None:
        await channel.update_codec()


def _ws_header(ws, name):
    for headers in (getattr(ws, 'request_headers', None), getattr(getattr(ws, 'request', None), 'headers', None)):
        if headers is not None:
            try:
                value = headers.get(name)
                if value:
                    return value
            except Exception:
                pass
    return None


def _request_path(ws):
    request_obj = getattr(ws, 'request', None)
    return getattr(request_obj, 'path', None) or getattr(ws, 'path', None) or '/'


# Routing: /watch/<client_id> is a browser, anything else is a telescope
async def handle_liveview_socket(ws):
    parts = urlsplit(_request_path(ws))
    if parts.path.startswith('/watch/'):
        client_id = unquote(parts.path[len('/watch/'):])
        query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
        await _handle_viewer(ws, client_id, query)
    else:
        await _handle_telescope(ws)


async def _handle_telescope(ws):
    client_ip = get_ws_client_ip(ws)

    # Rate limiting check
    if not check_rate_limit(client_ip):
        print(f"[SECURITY] LiveView rate limit exceeded for {client_ip}")
        await ws.close(code=4008, reason='Rate limit exceeded')
        return

    try:
        # First message should be authentication
        auth_message = await asyncio.wait_for(ws.recv(), timeout=10)
        auth_data = json.loads(auth_message)

        token = auth_data.get('token')
        client_id = auth_data.get('client_id')

        auth_result = authenticate_token_with_policy(
            token,
            client_id=client_id,
            client_ip=client_ip,
        )

        if not token or not auth_result.get('ok'):
            reason = auth_result.get('reason', 'authentication_failed')
            _security_log('ws_liveview_auth_failed', ip=client_ip, client_id=client_id, reason=reason)
            await ws.close(code=4001, reason='Authentication failed')
            return

        if not client_id:
            print(f"[SECURITY] LiveView no client_id provided from {client_ip}")
            await ws.close(code=4002, reason='Client ID required')
            return

        # One telescope's token can't stream as, or replace the stream of, another telescope
        if not _token_may_command(auth_result, client_id):
            _security_log('ws_liveview_client_id_mismatch', ip=client_ip, client_id=client_id, token_name=auth_result.get('name'))
            await ws.close(code=4001, reason='Token does not match client ID')
            return

    except asyncio.TimeoutError:
        print(f"[SECURITY] LiveView authentication timeout for {client_ip}")
        await ws.close(code=4003, reason='Authentication timeout')
        return
    except (ValueError, TypeError, AttributeError):
        print(f"[SECURITY] LiveView invalid JSON from {client_ip}")
        await ws.close(code=4004, reason='Invalid JSON')
        return
    except Exception as e:
        print(f"[SECURITY] LiveView authentication error for {client_ip}: {e}")
        await ws.close(code=4000, reason='Authentication error')
        return

    # Authentication successful
    client_type = auth_result.get('client_type', 'unknown')
    client_name = auth_result.get('name', client_id)
    proto = auth_data.get('proto')
    proto = proto if isinstance(proto, int) and not isinstance(proto, bool) else 1
    print(f"[LiveView] {client_name} ({client_type}) connected from {client_ip} (proto {min(proto, 2)})")
    _security_log('ws_liveview_connected', ip=client_ip, client_id=client_id, client_name=client_name, client_type=client_type, token_source=auth_result.get('source'))

    # Handle telescope database operations
    if _is_telescope_capable_client(client_type):
        record_telescope_connection(client_name, client_type, client_ip, token)

    # Start heartbeat task for telescopes
    liveview_client_id = f"{client_id}_liveview"
    if _is_telescope_capable_client(client_type):
        heartbeat_tasks[liveview_client_id] = asyncio.create_task(
            telescope_heartbeat(liveview_client_id, f"{client_name} (LiveView)", client_name, ws)
        )

    channel = _channel(client_id)
    previous = channel.telescope
    channel.telescope = ws
    channel.reset_stream()
    if previous is not None and previous is not ws:
        asyncio.ensure_future(previous.close(code=4012, reason='Replaced by a new live-view connection'))

    if proto >= 2:
        channel.proto = 2
        codecs = [c for c in (auth_data.get('codecs') or []) if c in ('h264', 'jpeg')]
        channel.codecs = tuple(codecs) or ('jpeg',)
        channel.codec = channel.wanted_codec()
        await ws.send(json.dumps({"type": "hello", "proto": 2, "codec": channel.codec, "poster_s": POSTER_S}))
    else:
        # Today's client never reads this socket: send it nothing
        channel.proto = 1
        channel.codecs = ('jpeg',)
        channel.codec = 'jpeg'
        channel.stream_text = LEGACY_STREAM_TEXT
        channel.broadcast(LEGACY_STREAM_TEXT)

    channel.broadcast(json.dumps({"type": "telescope", "connected": True}))
    viewer_counts.telescope_connected(client_id)

    try:
        async for message in ws:
            if channel.telescope is not ws:
                break
            if channel.proto >= 2:
                await _on_proto2_message(channel, message)
            else:
                _on_legacy_frame(channel, message)
    except websockets.exceptions.ConnectionClosed as e:
        if "payload length" in str(e) and "max_size" in str(e):
            print(f"[LiveView] Frame too large from {client_name}")
    except Exception as e:
        print(f"[LiveView] Error in connection: {e}")
    finally:
        print(f"[LiveView] {client_name} disconnected from live view.")
        task = heartbeat_tasks.pop(liveview_client_id, None)
        if task:
            task.cancel()
        if channel.telescope is ws:
            channel.telescope = None
            channel.proto = 0
            channel.reset_stream()
            clear_frames(client_id)
            channel.broadcast(json.dumps({"type": "telescope", "connected": False}))
        _drop_channel_if_idle(channel)
        _security_log('ws_liveview_disconnected', ip=client_ip, client_id=client_id)


def _on_legacy_frame(channel, message):
    # Proto 1: every message is a JPEG
    if isinstance(message, str):
        return
    payload = bytes(message)
    store_frame(channel.client_id, payload)
    frame = wrap_jpeg(payload, channel.jpeg_seq)
    channel.jpeg_seq += 1
    channel.poster = frame
    channel.broadcast(frame, KIND_JPEG, True)


async def _on_proto2_message(channel, message):
    if isinstance(message, str):
        try:
            data = json.loads(message)
        except ValueError:
            channel.log_bad_frame("invalid JSON")
            return
        if isinstance(data, dict) and data.get('type') == 'stream':
            channel.stream_text = message
            channel.gop = []
            channel.gop_bytes = 0
            channel.broadcast(message)
        return

    parsed = parse_frame(message)
    if parsed is None:
        channel.log_bad_frame("bad magic, kind or length")
        return
    kind, flags = parsed
    key = bool(flags & FLAG_KEYFRAME)

    if kind == KIND_H264:
        if key:
            channel.gop = [message]
            channel.gop_bytes = len(message)
        elif not channel.gop or channel.gop_bytes + len(message) > GOP_CAP_BYTES:
            # No keyframe yet, or the GOP outgrew the cache: drop and start again
            channel.gop = []
            channel.gop_bytes = 0
            await channel.request_keyframe()
            return
        else:
            channel.gop.append(message)
            channel.gop_bytes += len(message)
    else:
        store_frame(channel.client_id, bytes(message[FRAME_HEADER.size:]))
        channel.poster = message
        key = True

    channel.broadcast(message, kind, key)


# Viewer sockets
_viewer_ip_counts = collections.Counter()


def _ticket_serializer():
    from itsdangerous import URLSafeTimedSerializer
    from Server import app
    return URLSafeTimedSerializer(app.secret_key, salt=WATCH_TICKET_SALT)


def make_watch_ticket(client_id, user_id):
    return _ticket_serializer().dumps({"cid": client_id, "uid": user_id})


def verify_watch_ticket(ticket):
    from itsdangerous import BadSignature
    if not ticket:
        return None
    try:
        claims = _ticket_serializer().loads(ticket, max_age=WATCH_TICKET_MAX_AGE)
    except BadSignature:
        return None
    return claims if isinstance(claims, dict) else None


def _app_domain():
    from Server import app
    return (app.config.get("APP_DOMAIN") or "seleno.org").strip().lower()


def watch_url(client_id, ticket):
    # LIVEVIEW_PUBLIC_URL overrides the address for local testing, e.g. ws://localhost:8000
    base = os.getenv("LIVEVIEW_PUBLIC_URL") or f"wss://liveview.{_app_domain()}"
    return f"{base.rstrip('/')}/watch/{quote(str(client_id), safe='')}?ticket={quote(ticket, safe='')}"


def allowed_origins():
    domain = _app_domain()
    origins = {f"https://{domain}", f"https://www.{domain}"}
    extra = os.getenv("LIVEVIEW_ALLOWED_ORIGINS", "")
    origins.update(o.strip().rstrip('/') for o in extra.split(',') if o.strip())
    return origins


async def _handle_viewer(ws, client_id, query):
    client_ip = get_ws_client_ip(ws)

    # Viewers have their own allowance, apart from the telescopes'
    if not check_rate_limit(f"viewer:{client_ip}") or _viewer_ip_counts[client_ip] >= MAX_VIEWERS_PER_IP:
        await ws.close(code=4008, reason='Too many viewer connections')
        return

    origin = (_ws_header(ws, 'Origin') or '').rstrip('/')
    if origin not in allowed_origins():
        _security_log('ws_viewer_bad_origin', ip=client_ip, client_id=client_id, origin=origin)
        await ws.close(code=4003, reason='Origin not allowed')
        return

    claims = verify_watch_ticket(query.get('ticket'))
    if not claims or claims.get('cid') != client_id:
        _security_log('ws_viewer_bad_ticket', ip=client_ip, client_id=client_id)
        await ws.close(code=4001, reason='Invalid or expired ticket')
        return

    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=VIEWER_HELLO_TIMEOUT_S))
    except (asyncio.TimeoutError, ValueError, TypeError, websockets.exceptions.ConnectionClosed):
        await ws.close(code=4004, reason='Expected hello')
        return
    decode = hello.get('decode') if isinstance(hello, dict) and hello.get('type') == 'hello' else None
    viewer = Viewer(ws, h264=isinstance(decode, list) and 'h264' in decode)

    _viewer_ip_counts[client_ip] += 1
    channel = _channel(client_id)

    # Catch up first: the stream, the latest JPEG, then the frames since the keyframe
    viewer.offer(json.dumps({"type": "telescope", "connected": channel.telescope is not None}))
    if channel.stream_text:
        viewer.offer(channel.stream_text)
    if channel.poster:
        viewer.offer(channel.poster, KIND_JPEG, True)
    for index, frame in enumerate(channel.gop):
        viewer.offer(frame, KIND_H264, index == 0)

    channel.viewers.add(viewer)
    viewer_counts.change(client_id, sockets=1)
    sender = asyncio.create_task(viewer.run())
    try:
        await channel.update_codec()
        async for message in ws:
            if not isinstance(message, str):
                continue
            try:
                data = json.loads(message)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get('type') == 'keyframe':
                # The browser's decoder failed: ask the telescope, at most once a second
                await channel.request_keyframe()
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        sender.cancel()
        channel.viewers.discard(viewer)
        _viewer_ip_counts[client_ip] -= 1
        if _viewer_ip_counts[client_ip] <= 0:
            del _viewer_ip_counts[client_ip]
        viewer_counts.change(client_id, sockets=-1)
        await channel.update_codec()
        _drop_channel_if_idle(channel)


async def _close_channel(client_id, reason):
    channel = _channels.get(client_id)
    if channel is None:
        return False
    closed = False
    if channel.telescope is not None:
        await channel.telescope.close(code=4011, reason=reason)
        closed = True
    for viewer in list(channel.viewers):
        await viewer.ws.close(code=4011, reason=reason)
        closed = True
    return closed


def disconnect_liveview(client_id, reason='admin_disconnect'):
    # Close a telescope's live-view socket and its viewers, from any thread
    if lv_event_loop is None:
        return False
    try:
        closed = asyncio.run_coroutine_threadsafe(_close_channel(client_id, reason), lv_event_loop).result(timeout=5)
    except Exception:
        return False
    if closed:
        _security_log('ws_liveview_programmatic_disconnect', client_id=client_id, reason=reason)
    return closed


# Saving liveview frames
def save_latest_frame(client_id):
    frame = latest_frames.get(client_id)
    if frame:
        try:
            tmp_dir = os.path.realpath(tempfile.gettempdir())
            safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', str(client_id))[:128] or 'client'
            file_path = os.path.realpath(os.path.join(tmp_dir, f"{safe_id}_latest.jpg"))
            if os.path.dirname(file_path) != tmp_dir:
                return
            with open(file_path, "wb") as f:
                f.write(frame)
        except Exception as e:
            print(f"[DEBUG] Failed to save frame for {client_id}: {e}")


MJPEG_RESEND_S = 5.0     # resend the last frame this often, so a closed browser is noticed
MJPEG_IDLE_END_S = 15.0  # end the response after this long without a new frame (live view off)


# Handler for /liveview/<client_id> route
def liveview_handler(client_id):
    def generate():
        viewer_counts.change(client_id, mjpeg=1)
        try:
            last_seq = None
            last_sent = last_new = time.monotonic()
            last_save = 0.0
            while True:
                # Sleep until a new frame, the next resend, or the idle cut-off
                now = time.monotonic()
                wait = max(0.05, min(MJPEG_RESEND_S - (now - last_sent), MJPEG_IDLE_END_S - (now - last_new)))
                with _frame_cond:
                    _frame_cond.wait_for(
                        lambda: _frame_seq.get(client_id) != last_seq and client_id in latest_frames,
                        timeout=wait,
                    )
                    seq = _frame_seq.get(client_id)
                    frame = latest_frames.get(client_id)

                now = time.monotonic()
                if frame is not None and seq != last_seq:
                    last_seq = seq
                    last_new = now
                elif now - last_new >= MJPEG_IDLE_END_S:
                    break
                elif frame is None or now - last_sent < MJPEG_RESEND_S:
                    continue

                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
                last_sent = now
                if now - last_save > 5:
                    save_latest_frame(client_id)
                    last_save = now
        finally:
            viewer_counts.change(client_id, mjpeg=-1)

    return Response(
        generate(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-store'},
    )


# Start a separate WebSocket server for live view frames
def start_liveview_ws_server():
    global lv_event_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    lv_event_loop = loop

    async def run_server():
        try:
            async with websockets.serve(
                handle_liveview_socket,
                WS_IP,
                LIVEVIEW_WS_PORT,
                max_size=2*1024*1024,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
                # Frames are already compressed; deflate would only burn CPU
                compression=None,
            ):
                await asyncio.Future()
        except Exception as e:
            # Single write so concurrent thread output doesn't interleave on one line
            print(f"[LiveView] WebSocket server failed to start: {e}\n", end="", flush=True)
    try:
        loop.run_until_complete(run_server())
        loop.run_forever()
    except Exception as e:
        print(f"[LiveView] Event loop error: {e}")
