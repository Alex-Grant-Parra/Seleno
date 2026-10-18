"""Test harness: the Server's controllers on a stub Flask app with a throwaway
SQLite database. Nothing touches instance/Data.db or the running server:
telescopeLink's HTTP calls are routed into the test client, and fake
telescopes stand in for real command sockets."""
import asyncio
import os
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

from cryptography.fernet import Fernet
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

TMP = tempfile.mkdtemp(prefix="seleno-test-")

from flask import Flask, jsonify
from flask_login import LoginManager

app = Flask("stub", template_folder=os.path.join(REPO, "templates"), static_folder=os.path.join(REPO, "static"))
app.config.update(
    SECRET_KEY="test-secret",
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{TMP}/test.db",
    APP_DOMAIN="example.test",
    TESTING=True,
)
stub = types.ModuleType("Server")
stub.app = app
sys.modules["Server"] = stub

from app.db import db
db.init_app(app)
login_manager = LoginManager(app)

from models.user import User
from models.tables import Telescope as TelescopeRow
from models.booking import TelescopeBooking
from models.photo import Photo, TelescopeUpload
from models.capture_plan import CapturePlan, SavedCapturePlan
from models.album import FrameMeta, StackResult, FeaturedTarget, Project
import models.logging  # noqa: F401  (websocket security log table)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.before_request
def _fresh_login_user():
    # Tests hold one app context open, so g (and Flask-Login's cached user) would
    # otherwise carry over between requests
    from flask import g
    g.pop('_login_user', None)


import controllers.upload as upload_mod
import controllers.telescope_uploads as tus_mod
import controllers.album as album_mod
import controllers.interface as interface_mod
import controllers.star_map as star_map_mod
import controllers.capture as capture_mod
import controllers.album_api as album_api_mod
import controllers.interface_api as interface_api_mod
import controllers.projects_api as projects_api_mod

PHOTOS = os.path.join(TMP, "camera_photos")
upload_mod.BASE_UPLOAD_FOLDER = PHOTOS
tus_mod.BASE_UPLOAD_FOLDER = PHOTOS
tus_mod.PARTIAL_DIR = os.path.join(TMP, "partial")
album_mod.CAMERA_PHOTOS_BASE_DIR = PHOTOS

for bp in (interface_mod.interface_bp, star_map_mod.star_map_bp, album_mod.album_bp,
           capture_mod.capture_bp, tus_mod.telescope_uploads_bp, upload_mod.upload_bp,
           album_api_mod.album_api_bp, interface_api_mod.interface_api_bp, projects_api_mod.projects_api_bp):
    app.register_blueprint(bp)

from app import WebsocketServer as wss
from app import liveview
from app import telescopeLink


@app.route("/sendCommand", methods=["POST"])
def send_command():
    return wss.send_command_handler()


@app.route("/liveview/<client_id>")
def liveview_route(client_id):
    from app.telescope_access import telescope_access_error
    denied = telescope_access_error(client_id)
    if denied:
        return denied
    return liveview.liveview_handler(client_id)


with app.app_context():
    db.create_all()

# The command server's event loop, for fake telescopes
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
wss.ws_event_loop = loop


class FakeTelescope:
    """Stands in for a connected telescope: records calls, replies from `handlers`."""

    def __init__(self, client_id, handlers=None):
        self.client_id = client_id
        self.handlers = handlers or {}
        self.calls = []

    async def execute(self, function_name, args=None, kwargs=None, timeout=5.0):
        self.calls.append((function_name, list(args or []), dict(kwargs or {}), timeout))
        handler = self.handlers.get(function_name)
        if handler is None:
            return {"ok": True}
        if isinstance(handler, Exception):
            raise handler
        return handler(*(args or []), **(kwargs or {})) if callable(handler) else handler

    def last(self, name):
        for call in reversed(self.calls):
            if call[0] == name:
                return call
        return None


def connect_fake(client_id, handlers=None):
    fake = FakeTelescope(client_id, handlers)
    with wss.client_manager._lock:
        wss.client_manager.clients[client_id] = fake
    return fake


def disconnect_fake(client_id):
    wss.client_manager.remove_client(client_id)


client = app.test_client()


class _Resp:
    def __init__(self, r):
        self.text = r.get_data(as_text=True)
        self.status_code = r.status_code


def _fake_post(url, json=None, timeout=None, headers=None):
    # telescopeLink posts to http://localhost:5000/sendCommand: answer it from the test app
    # On its own thread, like the real separate request, so it gets its own app context and g
    _fake_post.calls.append({"json": json, "timeout": timeout})
    out = {}

    def run():
        with app.test_client() as c:
            out["resp"] = _Resp(c.post("/sendCommand", json=json, headers=headers or {}))

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    return out["resp"]


_fake_post.calls = []
telescopeLink.requests.post = _fake_post


def make_user(username, account_type="Standard"):
    user = User(username=username, email=f"{username}@example.test", password="x", AccountType=account_type)
    db.session.add(user)
    db.session.commit()
    return user


def make_telescope(name, owner=None, client_type="telescope", token=None):
    from security.token_store import token_hash
    row = TelescopeRow(telescope_id=name, type=client_type, user_id=owner.id if owner else None,
                       is_approved=True, last_seen=None)
    if token:
        row.token_hash = token_hash(token)
        row.token_prefix = token[:12]
    db.session.add(row)
    db.session.commit()
    return row


def make_booking(telescope, user, start, end, status="reserved"):
    booking = TelescopeBooking(telescope_id=telescope.id, requester_user_id=user.id,
                               owner_user_id=telescope.user_id or user.id,
                               start_utc=start, end_utc=end, status=status)
    db.session.add(booking)
    db.session.commit()
    return booking


def login(c, user):
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def select(c, telescope_name):
    with c.session_transaction() as sess:
        sess["selected_telescope"] = {"telescope_id": telescope_name}
