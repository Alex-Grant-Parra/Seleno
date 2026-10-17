import asyncio
import concurrent.futures
import hmac
import websockets
import uuid
import ujson as json  # Faster JSON serialization
import time
import os
import threading
import secrets
import logging
import ipaddress
from flask import jsonify, request
from flask_login import current_user

# WebSocket Configuration
commandPort = 4000
LiveViewPort = 8000
WS_IP = os.getenv("WS_IP", "0.0.0.0")  # default to all interfaces
WS_PORT = commandPort
LIVEVIEW_WS_PORT = LiveViewPort
WS_PING_INTERVAL = int(os.getenv("WS_PING_INTERVAL", "20"))
WS_PING_TIMEOUT = int(os.getenv("WS_PING_TIMEOUT", "120"))

sec_logger = logging.getLogger('security')
logger = logging.getLogger(__name__)

# Rate limiting
client_request_counts = {}
REQUEST_LIMIT_PER_MINUTE = 60

# Global variables
pending = {}
clients = []
heartbeat_tasks = {}  # Store heartbeat tasks by client_id
ws_event_loop = None  # Store reference to WebSocket thread's event loop

# Latest status message pushed by each telescope: client_id -> (received_at, message)
latest_status = {}
MAX_STATUS_BYTES = 64 * 1024
STATUS_PARTS = ('tracking', 'camera', 'liveview', 'recording', 'photos', 'uploads', 'clock', 'disk', 'esp32', 'plan')
_status_last_seen_write = {}

# Command timeouts in seconds. Each layer waits a little longer than the one inside it:
# Client.execute waits `timeout`, ClientManager.command `timeout + 2`, the HTTP call `timeout + 4`.
DEFAULT_COMMAND_TIMEOUT = 5.0
MAX_COMMAND_TIMEOUT = 960.0  # a 900 s exposure plus the 30 s capturePhoto margin, and some

# Only token types that may command a telescope other than their own
CROSS_TELESCOPE_TOKEN_TYPES = {'admin', 'developer'}

# Requests from this process's own routes (app/telescopeLink.py) carry this header.
# Behind the Cloudflare tunnel every request arrives from 127.0.0.1, so a loopback
# address alone proves nothing about who sent it.
INTERNAL_COMMAND_HEADER = 'X-Seleno-Internal'
INTERNAL_COMMAND_KEY = secrets.token_urlsafe(32)
_PROXY_HEADERS = ('CF-Connecting-IP', 'CF-Ray', 'X-Forwarded-For', 'X-Forwarded-Host', 'Forwarded', 'X-Real-IP', 'True-Client-IP')

# Blocking work (DB writes, catch-up commands) started from the event loop
background_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='ws-bg')


class ClientError(Exception):
    """The telescope replied with an error, timed out, or is not connected."""


class ClientNotConnected(ClientError):
    """No command socket is open for that telescope."""

_handshake_log_lock = threading.Lock()
_handshake_reject_count = 0
_handshake_last_summary = 0.0
_HANDSHAKE_LOG_WINDOW_SECONDS = 30


def _is_telescope_capable_client(client_type):
    # Developer clients should inherit telescope connectivity behavior.
    normalized = (client_type or '').strip().lower()
    return normalized in {'telescope', 'developer'}


def _log_handshake_reject_summary():
     # Log throttled summary for invalid websocket handshake attempts.
    global _handshake_reject_count, _handshake_last_summary
    now = time.time()

    with _handshake_log_lock:
        _handshake_reject_count += 1
        if now - _handshake_last_summary >= _HANDSHAKE_LOG_WINDOW_SECONDS:
            print(
                f"[WebSocket] Ignored {_handshake_reject_count} invalid handshake request(s) "
                f"in the last {_HANDSHAKE_LOG_WINDOW_SECONDS}s"
            )
            _handshake_reject_count = 0
            _handshake_last_summary = now

# Suppress noisy traceback logs for malformed/non-upgrade websocket probes
class _WebSocketHandshakeNoiseFilter(logging.Filter):
    

    def filter(self, record):
        message = record.getMessage().lower()

        if "opening handshake failed" in message:
            _log_handshake_reject_summary()
            return False

        if record.exc_info:
            exc_text = str(record.exc_info[1]).lower() if record.exc_info[1] else ""
            if (
                "did not receive a valid http request" in exc_text
                or "missing connection header" in exc_text
            ):
                _log_handshake_reject_summary()
                return False

        return True

# Attach filter to websockets loggers to reduce noisy handshake tracebacks
def _configure_websocket_logging():
     
    noise_filter = _WebSocketHandshakeNoiseFilter()

    for logger_name in ("websockets.server", "websockets.asyncio.server"):
        ws_logger = logging.getLogger(logger_name)

        if not any(isinstance(existing, _WebSocketHandshakeNoiseFilter) for existing in ws_logger.filters):
            ws_logger.addFilter(noise_filter)


_configure_websocket_logging()

# Validate client token against DB-backed token store
def authenticate_token(token):
    from security.token_store import verify_token
    from Server import app

    with app.app_context():
        rec, reason = verify_token(token)

    return bool(rec)

# Validate token against DB-backed token store
def authenticate_token_with_policy(token, *, client_id=None, client_ip=None, required_scope=None):
    
    from security.token_store import verify_token
    from Server import app

    with app.app_context():
        rec, reason = verify_token(
            token,
        )

    if rec:
        return {
            'ok': True,
            'name': rec.name,
            'client_type': rec.client_type,
            'token_id': rec.id,
            'source': 'db',
        }

    return {'ok': False, 'reason': reason or 'authentication_failed'}

# Best effort security event logging
def _security_log(event, **kwargs):
    
    try:
        payload = {'event': event}
        payload.update(kwargs)
        try:
            from models.logging import WebsocketSecurityLog
            WebsocketSecurityLog.save_event(event, kwargs, level='WARNING')
        except Exception:
            pass
        sec_logger.warning(json.dumps(payload))
    except Exception:
        try:
            print(f"[SECURITY] {event}: {kwargs}")
        except Exception:
            pass

# Resolve client IP for WebSocket connections using forwarded headers when available
def get_ws_client_ip(ws):
    def _ws_headers(ws_obj):
        # Support both websockets APIs:
        # - legacy: ws.request_headers
        # - modern: ws.request.headers
        try:
            legacy = getattr(ws_obj, 'request_headers', None)
            if legacy is not None:
                return legacy
        except Exception:
            pass

        try:
            request_obj = getattr(ws_obj, 'request', None)
            headers = getattr(request_obj, 'headers', None) if request_obj is not None else None
            if headers is not None:
                return headers
        except Exception:
            pass

        return None

    def _get_header_ci(headers_obj, header_name):
        if headers_obj is None:
            return None

        # `websockets` headers should be case-insensitive, but normalize manually
        # to handle version differences and proxy edge-cases reliably.
        try:
            direct = headers_obj.get(header_name)
            if direct:
                return direct
        except Exception:
            pass

        try:
            target = header_name.lower()
            for key, value in headers_obj.items():
                if str(key).lower() == target and value:
                    return value
        except Exception:
            pass

        return None

    def _clean_ip_token(token):
        value = (token or '').strip().strip('"').strip("'")
        if not value:
            return None

        # RFC 7239 Forwarded header values can be like: for=1.2.3.4 or for="[2001:db8::1]:1234"
        if value.lower().startswith('for='):
            value = value[4:].strip().strip('"').strip("'")

        # Forwarded entries often include params: for=1.2.3.4;proto=https
        if ';' in value:
            value = value.split(';', 1)[0].strip().strip('"').strip("'")

        # Bracketed IPv6 with optional port: [2001:db8::1]:443
        if value.startswith('['):
            end = value.find(']')
            if end > 1:
                return value[1:end]

        # IPv4 with port: 203.0.113.9:443
        if value.count(':') == 1 and value.rsplit(':', 1)[1].isdigit():
            return value.rsplit(':', 1)[0]

        return value

    def _collect_ips(raw_value):
        if not raw_value:
            return []
        parts = [part.strip() for part in str(raw_value).split(',') if part.strip()]
        cleaned = []
        for part in parts:
            token = _clean_ip_token(part)
            if token:
                cleaned.append(token)
        return cleaned

    def _pick_best_ip(candidates):
        valid_ips = []
        for candidate in candidates:
            try:
                valid_ips.append(ipaddress.ip_address(candidate))
            except ValueError:
                continue

        for ip_obj in valid_ips:
            if not ip_obj.is_loopback:
                return str(ip_obj)

        if valid_ips:
            return str(valid_ips[0])

        return None

    # Prefer forwarded client IP headers, then fall back to the socket peer address.
    try:
        headers = _ws_headers(ws)
        candidate_ips = []

        for header in ('CF-Connecting-IP', 'True-Client-IP', 'X-Real-IP', 'X-Client-IP', 'X-Forwarded-For', 'Forwarded'):
            candidate_ips.extend(_collect_ips(_get_header_ci(headers, header)))

        selected = _pick_best_ip(candidate_ips)
        if selected:
            return selected
    except Exception:
        pass

    return ws.remote_address[0] if ws.remote_address else "unknown"

def check_rate_limit(client_ip):
    # Rate limiting check for WebSocket connections
    # Uses sliding window to count requests per minute
    from security.config import RATE_LIMITS, RATE_LIMIT_CONFIG
    
    now = time.time()
    tracking_window = RATE_LIMIT_CONFIG.get('tracking_window', 60)
    limit = RATE_LIMITS.get('default', 120)
    
    # Initialize tracking for this IP if not present
    if client_ip not in client_request_counts:
        client_request_counts[client_ip] = []
    
    # Remove old entries outside the tracking window
    client_request_counts[client_ip] = [
        ts for ts in client_request_counts[client_ip]
        if now - ts < tracking_window
    ]
    
    # Check if limit exceeded
    if len(client_request_counts[client_ip]) >= limit:
        return False  # Rate limit exceeded
    
    # Record this request
    client_request_counts[client_ip].append(now)
    return True  # OK to proceed


# Periodic cleanup of old rate limit tracking data to prevent memory leaks
def cleanup_rate_limit_tracking():
    # Periodically clean up old entries in rate limit tracking
    from security.config import RATE_LIMIT_CONFIG
    
    while True:
        try:
            cleanup_interval = RATE_LIMIT_CONFIG.get('cleanup_interval', 300)
            time.sleep(cleanup_interval)
            
            now = time.time()
            tracking_window = RATE_LIMIT_CONFIG.get('tracking_window', 60)
            
            # Remove IPs with no recent requests
            to_remove = []
            for client_ip, timestamps in client_request_counts.items():
                active_requests = [ts for ts in timestamps if now - ts < tracking_window]
                if not active_requests:
                    to_remove.append(client_ip)
                else:
                    client_request_counts[client_ip] = active_requests
            
            for client_ip in to_remove:
                del client_request_counts[client_ip]
            
            if to_remove:
                print(f"[Rate Limiting] Cleaned up {len(to_remove)} idle IPs from tracking")
        except Exception as e:
            print(f"[Rate Limiting] Cleanup error: {e}")

# Generate a secure token for new clients
def generate_token():
    return secrets.token_urlsafe(32)

# Main client class representing a connected WebSocket client
class Client:
    def __init__(self, client_id, ws):
        self.client_id = client_id
        self.ws = ws

    async def execute(self, function_name, args=None, kwargs=None, timeout=DEFAULT_COMMAND_TIMEOUT):
        message_id = str(uuid.uuid4())
        message = json.dumps({
            "type": "call",
            "function": function_name,
            "args": args or [],
            "kwargs": kwargs or {},
            "id": message_id
        })

        future = asyncio.get_running_loop().create_future()
        pending[message_id] = future
        try:
            await self.ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            pending.pop(message_id, None)
            raise ClientError("Telescope disconnected")

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            pending.pop(message_id, None)
            raise ClientError(f"Telescope did not reply to {function_name} within {timeout:g} s")

        if "result" in response:
            return response.get("result")
        raise ClientError(str(response.get("error") or f"{function_name} failed on the telescope"))

class ClientManager:
    def __init__(self):
        self.clients = {}
        self._lock = threading.Lock()

    def add_client(self, client_id, ws):
        with self._lock:
            self.clients[client_id] = Client(client_id, ws)

    # Atomically add client only if not currently connected
    def try_add_client(self, client_id, ws):
        
        with self._lock:
            if client_id in self.clients:
                return False
            self.clients[client_id] = Client(client_id, ws)
            return True

    def remove_client(self, client_id):
        with self._lock:
            self.clients.pop(client_id, None)

    def command(self, client_id, function_name, args=None, kwargs=None, timeout=DEFAULT_COMMAND_TIMEOUT):
        # Send command to client using threadsafe async call; raises ClientError
        client = self.get_client(client_id)
        if client is None:
            raise ClientNotConnected(f"Telescope '{client_id}' is not connected")

        if ws_event_loop is None:
            raise ClientError("Command server is not running")

        # Use run_coroutine_threadsafe to safely execute async code from another thread
        coroutine = client.execute(function_name, args, kwargs, timeout=timeout)
        future = asyncio.run_coroutine_threadsafe(coroutine, ws_event_loop)

        try:
            return future.result(timeout=timeout + 2)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ClientError(f"Telescope did not reply to {function_name} within {timeout:g} s")

    def get_client(self, client_id):
        with self._lock:
            return self.clients.get(client_id)

# Global client manager instance
client_manager = ClientManager()


def record_telescope_connection(client_name, client_type, client_ip, token):
    # Add or update the telescope's row when it connects either socket
    try:
        from models.tables import Telescope
        from Server import app
        with app.app_context():
            from app.db import db
            # Check if telescope exists in database by name
            existing = Telescope.get_telescope_by_id(client_name)

            if not existing:
                legacy = db.session.query(Telescope).filter_by(telescope_id=token).first()
                if legacy:
                    legacy.telescope_id = client_name
                    legacy.type = client_type
                    if client_ip and client_ip != 'unknown':
                        legacy.ip_address = client_ip
                    legacy.last_seen = time.time()
                    db.session.commit()
                    print(f"[DB] Migrated telescope ID to '{client_name}'")
                else:
                    # Auto-add telescope to database
                    result = Telescope.add_telescope(
                        telescope_id=client_name,
                        telescope_type=client_type,
                        ip_address=client_ip if client_ip != 'unknown' else None,
                        last_seen=time.time()
                    )
                    if result['status'] == 'success':
                        print(f"[DB] Auto-added telescope '{client_name}' to database (ID: {result.get('id')})")
                    else:
                        print(f"[DB] Failed to auto-add telescope: {result['message']}")
            else:
                # Update existing telescope
                Telescope.update_last_seen(client_name)
                if client_ip and client_ip != 'unknown':
                    Telescope.update_ip_address(client_name, client_ip)
                record = db.session.query(Telescope).filter_by(telescope_id=client_name).first()
                if record and record.type != client_type:
                    record.type = client_type
                    db.session.commit()
                return True
    except Exception as e:
        print(f"[WARNING] Could not update telescope database: {e}")
    return False

async def telescope_heartbeat(client_id, client_name, telescope_id, ws):
    # Periodic heartbeat every 30 seconds
    while True:
        try:
            await asyncio.sleep(30)  # Wait 30 seconds between pings

            try:
                pong_waiter = ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=10)

                # Update last_seen in database on successful ping/pong
                from models.tables import Telescope
                from Server import app
                with app.app_context():
                    result = Telescope.update_last_seen(telescope_id)
                    if result.get('status') == 'success':
                        # Only log occasionally to avoid spam (every 5 minutes)
                        if int(time.time()) % 300 < 30:  # Log once every 10 pings
                            print(f"[Heartbeat] {client_name} alive (last_seen updated)")

            except websockets.exceptions.ConnectionClosed:
                print(f"[Heartbeat] {client_name} connection closed, stopping heartbeat")
                break
            except Exception as e:
                print(f"[Heartbeat] Error pinging {client_name}: {e}")
                break
                
        except asyncio.CancelledError:
            print(f"[Heartbeat] Stopped for {client_name}")
            break
        except Exception as e:
            print(f"[Heartbeat] Unexpected error for {client_name}: {e}")
            break

async def handle_client(ws):
    client_ip = get_ws_client_ip(ws)
    
    # Rate limiting check
    if not check_rate_limit(client_ip):
        print(f"[SECURITY] Rate limit exceeded for {client_ip}")
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
            _security_log('ws_command_auth_failed', ip=client_ip, client_id=client_id, reason=reason)
            await ws.close(code=4001, reason='Authentication failed')
            return
            
        if not client_id:
            print(f"[SECURITY] No client_id provided from {client_ip}")
            await ws.close(code=4002, reason='Client ID required')
            return
            
    except asyncio.TimeoutError:
        print(f"[SECURITY] Authentication timeout for {client_ip}")
        await ws.close(code=4003, reason='Authentication timeout')
        return
    except json.JSONDecodeError:
        print(f"[SECURITY] Invalid JSON from {client_ip}")
        await ws.close(code=4004, reason='Invalid JSON')
        return
    except Exception as e:
        print(f"[SECURITY] Authentication error for {client_ip}: {e}")
        await ws.close(code=4000, reason='Authentication error')
        return
    
    # Authentication successful; refuse silent replacement of existing client_id
    if not client_manager.try_add_client(client_id, ws):
        _security_log('ws_command_client_id_already_connected', ip=client_ip, client_id=client_id)
        await ws.close(code=4009, reason='Client ID already connected')
        return

    client_type = auth_result.get('client_type', 'unknown')
    client_name = auth_result.get('name', client_id)
    print(f"[+] {client_name} ({client_type}) connected from {client_ip}")
    _security_log('ws_command_connected', ip=client_ip, client_id=client_id, client_name=client_name, client_type=client_type, token_source=auth_result.get('source'))
    
    # Handle telescope database operations
    if _is_telescope_capable_client(client_type):
        if record_telescope_connection(client_name, client_type, client_ip, token):
            print(f"[DB] Updated telescope '{client_name}' in database")

    # Start heartbeat task for telescopes
    heartbeat_task = None
    if _is_telescope_capable_client(client_type):
        heartbeat_task = asyncio.create_task(
            telescope_heartbeat(client_id, client_name, client_name, ws)
        )
        heartbeat_tasks[client_id] = heartbeat_task

        # Catch up on any capture plan that ran while the telescope was offline
        background_pool.submit(_catch_up_capture_plan, client_id, client_name)

    try:
        async for message in ws:
            # Rate limit each telescope's messages separately, not per IP, so several
            # telescopes behind one home router don't share one allowance
            if not check_rate_limit(f"client:{client_id}"):
                print(f"[SECURITY] Rate limit exceeded for {client_id} from {client_ip}")
                break

            data = json.loads(message)
            if not isinstance(data, dict):
                continue
            msg_id = data.get("id")
            if msg_id in pending:
                future = pending.pop(msg_id)
                if not future.done():
                    future.set_result(data)
            elif data.get("type") == "status":
                _store_status(client_id, client_name, message, data)
            else:
                print(f"[{client_id}] -> {data}")
    except websockets.exceptions.ConnectionClosed:
        print(f"[-] {client_name} disconnected")
    except json.JSONDecodeError:
        print(f"[SECURITY] Invalid JSON from {client_id}")
    except Exception as e:
        print(f"[ERROR] Error handling {client_id}: {e}")
    finally:
        # Cancel heartbeat task if it exists
        if client_id in heartbeat_tasks:
            heartbeat_tasks[client_id].cancel()
            del heartbeat_tasks[client_id]
        client_manager.remove_client(client_id)
        _security_log('ws_command_disconnected', ip=client_ip, client_id=client_id)

# Status messages pushed by telescopes (type "status"), stored rather than printed
_status_drop_log = {}


def _clean_status(data):
    # Keep known parts that are objects or plain values; a malformed part becomes an error part
    cleaned = {}
    for key, value in data.items():
        if key in STATUS_PARTS:
            if value is None or isinstance(value, (dict, str, int, float, bool)):
                cleaned[key] = value
            else:
                cleaned[key] = {"error": "malformed status part"}
        elif value is None or isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
    return cleaned


def _store_status(client_id, client_name, raw_message, data):
    if len(raw_message) > MAX_STATUS_BYTES:
        now = time.time()
        if now - _status_drop_log.get(client_id, 0) > 60:
            _status_drop_log[client_id] = now
            print(f"[Status] Dropped {len(raw_message)} byte status message from {client_id} (limit {MAX_STATUS_BYTES})")
        return

    status = _clean_status(data)
    received_at = time.time()
    latest_status[client_id] = (received_at, status)
    background_pool.submit(_apply_status, client_name, status, received_at)


def _apply_status(client_name, status, received_at):
    # Database side of a status message: last_seen, and capture plan progress
    try:
        from Server import app
        with app.app_context():
            if received_at - _status_last_seen_write.get(client_name, 0) >= 15:
                _status_last_seen_write[client_name] = received_at
                from models.tables import Telescope
                Telescope.update_last_seen(client_name, received_at)

            from app.capture_plans import plan_status_from_status, record_plan_status
            plan_status = plan_status_from_status(status)
            if plan_status:
                record_plan_status(client_name, plan_status)
    except Exception:
        logger.exception("Failed to apply status message from %s", client_name)


def get_latest_status(client_id):
    # (age in seconds, status message) for a telescope, or (None, None)
    entry = latest_status.get(client_id)
    if not entry:
        return None, None
    received_at, status = entry
    return max(0.0, time.time() - received_at), status


def _catch_up_capture_plan(client_id, client_name):
    # After a reconnect, ask once for the state of a plan that was running
    time.sleep(3)
    try:
        from Server import app
        from app.capture_plans import catch_up_after_reconnect
        with app.app_context():
            catch_up_after_reconnect(client_id, client_name)
    except Exception:
        logger.exception("Capture plan catch-up failed for %s", client_name)

# Extract API token from header or JSON body
def _extract_api_token_from_request():
    auth_header = (request.headers.get('Authorization') or '').strip()
    if auth_header.lower().startswith('bearer '):
        return auth_header[7:].strip()

    header_token = (request.headers.get('X-API-Token') or '').strip()
    if header_token:
        return header_token

    try:
        data = request.get_json(silent=True) or {}
        body_token = (data.get('token') or '').strip()
        if body_token:
            return body_token
    except Exception:
        pass

    return None

# Requests from this process's own routes: loopback, no proxy headers, and the per-process key
def _is_internal_request():
    try:
        host = (request.remote_addr or '').strip()
        if host not in ('127.0.0.1', '::1', 'localhost'):
            return False
        # The Cloudflare tunnel also connects from localhost, but adds these headers
        if any(request.headers.get(name) for name in _PROXY_HEADERS):
            return False
        presented = request.headers.get(INTERNAL_COMMAND_HEADER) or ''
        return hmac.compare_digest(presented, INTERNAL_COMMAND_KEY)
    except Exception:
        return False


def _token_may_command(auth_result, client_id):
    # A telescope's token drives only that telescope; admin and developer tokens drive any
    if (auth_result.get('client_type') or '').strip().lower() in CROSS_TELESCOPE_TOKEN_TYPES:
        return True
    return bool(client_id) and auth_result.get('name') == client_id


# Auth handler
def _authorize_send_command(client_id):
    try:
        if current_user and getattr(current_user, 'is_authenticated', False) and getattr(current_user, 'is_admin', False):
            return True, 'admin'
    except Exception:
        pass

    if _is_internal_request():
        return True, 'internal'

    token = _extract_api_token_from_request()
    if token:
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        auth_result = authenticate_token_with_policy(
            token,
            client_id=client_id,
            client_ip=(client_ip.split(',')[0].strip() if client_ip else 'unknown'),
        )
        if auth_result.get('ok'):
            if _token_may_command(auth_result, client_id):
                return True, auth_result.get('source', 'token')
            return False, 'token_scope'

    return False, 'forbidden'

# Start WebSocket Server in Background
def start_ws_server():
    global ws_event_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ws_event_loop = loop  # Store reference for thread-safe operations
    async def run_server():
        try:
            async with websockets.serve(
                handle_client,
                WS_IP,
                WS_PORT,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT
            ):
                await asyncio.Future()
        except Exception as e:
            # Single write so concurrent thread output doesn't interleave on one line
            print(f"[CommandWS] WebSocket server failed to start: {e}\n", end="", flush=True)
    try:
        loop.run_until_complete(run_server())
        loop.run_forever()
    except Exception as e:
        print(f"[CommandWS] Event loop error: {e}")

# Start both websocket servers in daemon threads
def start_websocket_servers():
    
    from socket import gethostname
    from app.liveview import start_liveview_ws_server
    
    # Start rate limit cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_rate_limit_tracking, daemon=True)
    cleanup_thread.start()
    print("[Rate Limiting] Started cleanup thread for tracking data")
    
    threading.Thread(target=start_ws_server, daemon=True).start()
    print(f"Starting websocket command server on {gethostname()} at port: {commandPort}")

    threading.Thread(target=start_liveview_ws_server, daemon=True).start()
    print(f"Starting liveView server on {gethostname()} at port: {LiveViewPort}")


def _request_timeout(value):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_COMMAND_TIMEOUT
    return min(max(timeout, 1.0), MAX_COMMAND_TIMEOUT)

# Send command
def send_command_handler():
    data = request.get_json(silent=True) or {}
    client_id = data.get('client_id')
    command = data.get('command')
    args = data.get('args', [])
    kwargs = data.get('kwargs', {})  # Extract kwargs from request
    timeout = _request_timeout(data.get('timeout'))

    if not client_id or not command:
        return jsonify({"status": "error", "error": "client_id and command are required"}), 400

    allowed, auth_source = _authorize_send_command(client_id)
    if not allowed:
        _security_log(
            'send_command_denied',
            client_id=client_id,
            command=command,
            reason=auth_source,
            ip=request.headers.get('X-Forwarded-For', request.remote_addr),
        )
        return jsonify({"status": "error", "error": "Unauthorized"}), 403

    _security_log(
        'send_command_allowed',
        auth_source=auth_source,
        client_id=client_id,
        command=command,
        ip=request.headers.get('X-Forwarded-For', request.remote_addr),
    )

    try:
        result = client_manager.command(client_id, command, args, kwargs, timeout=timeout)
        return jsonify({"status": "success", "result": result})
    except ClientError as e:
        return jsonify({"status": "error", "error": str(e)}), 502
    except Exception:
        logger.exception("send_command failed for client %s", client_id)
        return jsonify({"status": "error", "error": "Command failed"}), 500

# Client disconnect handler for admin interface
def admin_disconnect_ws_client_handler(client_id):
    
    try:
        if not current_user or not getattr(current_user, 'is_authenticated', False) or not getattr(current_user, 'is_admin', False):
            return jsonify({'status': 'error', 'error': 'Admin access required'}), 403
    except Exception:
        return jsonify({'status': 'error', 'error': 'Admin access required'}), 403

    existing = client_manager.get_client(client_id)
    if not existing:
        return jsonify({'status': 'error', 'error': 'Client not connected'}), 404

    global ws_event_loop
    if ws_event_loop is None:
        return jsonify({'status': 'error', 'error': 'WebSocket event loop not initialized'}), 500

    try:
        fut = asyncio.run_coroutine_threadsafe(
            existing.ws.close(code=4010, reason='Admin override disconnect'),
            ws_event_loop,
        )
        fut.result(timeout=5)
        client_manager.remove_client(client_id)
        _security_log('ws_command_admin_disconnect', client_id=client_id, by=getattr(current_user, 'id', None))
        return jsonify({'status': 'success', 'message': f"Disconnected '{client_id}'"})
    except Exception:
        logger.exception("Admin disconnect failed for client %s", client_id)
        return jsonify({'status': 'error', 'error': 'Failed to disconnect client'}), 500


def disconnect_client_by_id(client_id, reason='admin_disconnect'):
    """
    Programmatic helper to disconnect a connected websocket client by client_id.
    `<client_id>_liveview` closes that telescope's live-view socket and its viewers.
    Returns True if a client was disconnected, False otherwise.
    """
    if str(client_id).endswith('_liveview'):
        try:
            from app.liveview import disconnect_liveview
            return disconnect_liveview(str(client_id)[:-len('_liveview')], reason=reason)
        except Exception:
            return False

    try:
        existing = client_manager.get_client(client_id)
        if not existing:
            return False

        global ws_event_loop
        if ws_event_loop is None:
            return False

        fut = asyncio.run_coroutine_threadsafe(
            existing.ws.close(code=4011, reason=reason),
            ws_event_loop,
        )
        fut.result(timeout=5)
        client_manager.remove_client(client_id)
        _security_log('ws_command_programmatic_disconnect', client_id=client_id, reason=reason)
        return True
    except Exception:
        return False
    
# Handler for /client/register route
def register_client_handler():
    print("Client requesting client ID")
    client_id = str(uuid.uuid4())
    
    # Generate a secure token for the client
    token = generate_token()
    
    clients.append(client_id)
    
    print(f"[+] New client registered: {client_id}")
    print(f"[SECURITY] Generated token {token[:6]}... for client {client_id}")
    
    return jsonify({
        "client_id": client_id,
        "token": token,
        "message": "Store this token securely - you'll need it to connect via WebSocket"
    })

# Handler for /admin/add_token route - for manually adding authorized tokens
def add_api_token_handler():
   
    data = request.get_json()
    
    token = data.get('token') or generate_token()
    client_type = data.get('client_type', 'observer')  # telescope, observer
    name = data.get('name', 'Unknown Client')

    # Persist token to DB-backed telescope token store
    try:
        from security.token_store import upsert_raw_token_record
        from Server import app

        with app.app_context():
            upsert_raw_token_record(
                token,
                name=name,
                client_type=client_type,
            )
    except Exception as e:
        print(f"[WARNING] Failed to persist API token to DB: {e}")
    
    print(f"[ADMIN] Added API token for {name} ({client_type})")
    
    return jsonify({
        "token": token,
        "client_type": client_type,
        "name": name,
        "message": "Token added successfully"
    })
