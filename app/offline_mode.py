"""Offline mode: this server running on a telescope's Raspberry Pi, for phones
on the telescope's own Wi-Fi hotspot, with no internet.

The telescope client (SelenoClient: core/networking/offline_server.py) starts
the server like this when the telescope is in offline mode with its hotspot
on, and connects to it over the loopback like it would to seleno.org:

    SELENO_OFFLINE=1                  this mode
    SELENO_OFFLINE_TELESCOPE=<name>   the telescope (also the hotspot's name)
    SELENO_OFFLINE_TOKEN=<token>      the token the client connects with

Then:
* there is one account (the observer), and every visitor is logged in as it:
  the hotspot's Wi-Fi password is the way in;
* there is one telescope, owned by that account and selected for everyone.
  Renaming it on the telescope renames the row, so the album keeps its photos;
* only the pages that work without the internet are served (the interface,
  star map, album and the telescope's settings); the others redirect to the
  interface, and their APIs answer 404.
"""
import logging
import os
import secrets
from datetime import datetime, timezone

from flask import jsonify, redirect, request, session

logger = logging.getLogger(__name__)

OBSERVER_USERNAME = "observer"
OBSERVER_EMAIL = "observer@telescope.local"
OBSERVER_ID = 1
DEFAULT_TELESCOPE = "SELENO"

# Path prefixes served in offline mode; the rest redirect to the interface
ALLOWED_PREFIXES = (
    "/interface", "/album", "/StarMap", "/api/", "/star_info/", "/track_star", "/get_tracking_status",
    "/stop_tracking", "/sw.js", "/offline", "/telescopes/", "/i18n", "/lang/", "/user/", "/process",
    "/static/", "/sendCommand", "/liveview/", "/security/csrf-token", "/favicon.ico", "/errors",
)
# The site's online-only APIs under /api/
BLOCKED_PREFIXES = ("/api/client_release", "/api/releases")


def enabled():
    return os.getenv("SELENO_OFFLINE", "").strip().lower() in ("1", "true", "yes")


def telescope_name():
    name = (os.getenv("SELENO_OFFLINE_TELESCOPE") or "").strip()
    return name[:64] or DEFAULT_TELESCOPE


def _token():
    return (os.getenv("SELENO_OFFLINE_TOKEN") or "").strip()


def path_allowed(path):
    if path == "/":
        return False
    if any(path.startswith(p) for p in BLOCKED_PREFIXES):
        return False
    return any(path == p.rstrip("/") or path.startswith(p) for p in ALLOWED_PREFIXES)


def observer():
    """The one account, made the first time."""
    from app.db import db
    from models.user import User

    user = User.query.filter_by(username=OBSERVER_USERNAME).first()
    if user is None:
        import bcrypt

        user = User(username=OBSERVER_USERNAME, password=bcrypt.hashpw(secrets.token_bytes(24), bcrypt.gensalt()).decode())
        # Id 1 when it's free (a fresh offline database): the telescope files the
        # photos it takes from its own panel under it (SelenoClient
        # core/networking/offline_server.OBSERVER_ID)
        if db.session.get(User, OBSERVER_ID) is None:
            user.id = OBSERVER_ID
        user.set_email(OBSERVER_EMAIL)
        db.session.add(user)
        db.session.commit()
    return user


def telescope_row():
    """The one telescope, made or renamed to SELENO_OFFLINE_TELESCOPE, with the
    client's token."""
    from app.db import db
    from models.tables import Telescope
    from security.token_store import token_hash

    user = observer()
    name = telescope_name()
    row = Telescope.query.filter_by(telescope_id=name).first()
    if row is None:
        # Renamed on the telescope: keep the row (and the photos filed under it)
        row = Telescope.query.filter_by(user_id=user.id).order_by(Telescope.id).first()
    if row is None:
        row = Telescope(telescope_id=name, type="telescope", last_seen=None)
        db.session.add(row)
    row.telescope_id = name
    row.type = "telescope"
    row.user_id = user.id
    row.is_approved = True
    row.is_disabled = False
    token = _token()
    if token:
        digest = token_hash(token)
        if row.token_hash != digest:
            row.token_hash = digest
            row.token_prefix = token[:8]
            row.token_created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    # Any other telescope a copied database brought along can't connect here
    for other in Telescope.query.filter(Telescope.id != row.id).all():
        if not other.is_disabled:
            other.is_disabled = True
    db.session.commit()
    return row


def bootstrap(app):
    with app.app_context():
        row = telescope_row()
        logger.info("Offline mode: telescope %s, observer account %s", row.telescope_id, OBSERVER_USERNAME)
        print(f"Offline mode: serving {row.telescope_id} to phones on its hotspot")


def _selected(name):
    from models.tables import Telescope

    return {
        "telescope_id": name,
        "ip_address": None,
        "type": "telescope",
        "last_seen": None,
        "online": Telescope.is_telescope_online(name),
    }


def gate():
    """Before each request in offline mode: only the offline pages, and the
    visitor is the observer with this telescope selected."""
    if not enabled():
        return None
    from flask_login import current_user, login_user

    path = request.path or "/"
    if not path_allowed(path):
        if request.method == "GET" and "application/json" not in (request.headers.get("Accept") or ""):
            return redirect("/interface/")
        return jsonify({"status": "error", "message": "Not available on the telescope's hotspot"}), 404
    if path.startswith("/static/") or path == "/sendCommand":
        return None
    if not getattr(current_user, "is_authenticated", False):
        login_user(observer(), remember=True)
    name = telescope_name()
    if (session.get("selected_telescope") or {}).get("telescope_id") != name:
        session["selected_telescope"] = _selected(name)
    return None


def template_context():
    if enabled():
        return {"offline_mode": True, "offline_telescope": telescope_name()}
    return {"offline_mode": False, "offline_telescope": None}


def setup(app):
    """Wire offline mode into the app (after the blueprints and the database).
    The hooks do nothing unless SELENO_OFFLINE is set."""
    app.context_processor(template_context)
    app.before_request(gate)
    if enabled():
        bootstrap(app)


def watch_base(host_header):
    """The live-view socket's address as the phone reached this server."""
    from app.WebsocketServer import LIVEVIEW_WS_PORT

    host = (host_header or "").strip()
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return f"ws://{host or 'localhost'}:{LIVEVIEW_WS_PORT}"


def origin_allowed(origin):
    """Viewer sockets in offline mode: pages served over plain HTTP from this
    Pi (its hotspot address, its LAN address or <hostname>.local)."""
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(origin or "")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

