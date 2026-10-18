"""Resumable photo uploads from telescopes, using tus 1.0 (https://tus.io/protocols/resumable-upload).

A cut-out costs only the chunk in flight: the telescope asks how much arrived
(HEAD) and carries on from there. Requests carry the telescope's API token
(`Authorization: Bearer <token>`), the same one it uses on its sockets, and
the photo's owner must own the telescope or hold an approved booking that
covers the capture time.
"""
import base64
import binascii
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from flask import Blueprint, Response, request
from werkzeug.utils import secure_filename

from app.db import db
from controllers.upload import ALLOWED_EXTENSIONS, BASE_UPLOAD_FOLDER

logger = logging.getLogger(__name__)

telescope_uploads_bp = Blueprint("telescope_uploads", __name__, url_prefix="/api/telescope/uploads")

TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation,checksum,termination,expiration"
TUS_MAX_SIZE = int(os.getenv("TUS_MAX_UPLOAD_BYTES", str(4 * 1024 ** 3)))
MAX_CHUNK_BYTES = 32 * 1024 * 1024   # the client sends 4 MB
MAX_META_BYTES = 16 * 1024
UPLOAD_TTL = timedelta(days=7)
CHECKSUM_MISMATCH = 460

PARTIAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "uploads", "partial")
KINDS = ("jpeg", "raw", "video")
RAW_EXTENSIONS = {"cr2", "cr3", "nef", "nrw", "arw", "orf", "rw2", "dng", "raf", "pef", "srw"}
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "ser"}
FRAME_TYPES = ("light", "dark", "flat", "darkflat", "bias", "snapshot", "bracket", "timelapse", "video", "test")

_locks_guard = threading.Lock()
_upload_locks = {}


def _upload_lock(upload_id):
    with _locks_guard:
        lock = _upload_locks.get(upload_id)
        if lock is None:
            lock = _upload_locks[upload_id] = threading.Lock()
        return lock


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _http_date(value):
    return format_datetime(value.replace(tzinfo=timezone.utc), usegmt=True)


def _reply(status, message=None, headers=None):
    resp = Response(message or "", status=status, mimetype="text/plain")
    for key, value in (headers or {}).items():
        resp.headers[key] = str(value)
    return resp


@telescope_uploads_bp.after_request
def _tus_headers(resp):
    resp.headers["Tus-Resumable"] = TUS_VERSION
    return resp


def _part_path(upload_id):
    return os.path.join(PARTIAL_DIR, f"{upload_id}.part")


def _authenticated_telescope():
    # (telescope row, None) for a valid telescope token, else (None, error reply)
    from security.token_store import verify_token

    if request.headers.get("Tus-Resumable") != TUS_VERSION:
        return None, _reply(412, "Unsupported tus version", {"Tus-Version": TUS_VERSION})

    auth = (request.headers.get("Authorization") or "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return None, _reply(401, "API token required")
    rec, reason = verify_token(token)
    if not rec:
        return None, _reply(401, f"Token refused: {reason}")
    if (rec.client_type or "").strip().lower() not in ("telescope", "developer"):
        return None, _reply(403, "Only telescope tokens can upload")
    return rec, None


def _parse_metadata(header):
    # Upload-Metadata: comma-separated "key base64(value)" pairs
    meta = {}
    for pair in (header or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, encoded = pair.partition(" ")
        meta[key] = base64.b64decode(encoded.strip(), validate=True).decode("utf-8") if encoded.strip() else ""
    return meta


def _parse_captured(text):
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _owner_may_receive(owner_id, telescope, captured):
    # The telescope's owner, an admin, or someone whose approved booking covers the capture
    from app.telescope_access import approved_booking
    from models.user import User

    user = db.session.get(User, owner_id)
    if user is None:
        return False
    if telescope.user_id is not None and telescope.user_id == owner_id:
        return True
    if user.is_admin:
        return True
    return approved_booking(telescope, at=captured, user_id=owner_id) is not None


def _expires_headers(row):
    return {"Upload-Expires": _http_date(row.expires_at)}


@telescope_uploads_bp.route("", methods=["OPTIONS"])
def tus_options():
    return _reply(204, headers={
        "Tus-Version": TUS_VERSION,
        "Tus-Extension": TUS_EXTENSIONS,
        "Tus-Max-Size": TUS_MAX_SIZE,
        "Tus-Checksum-Algorithm": "sha1",
    })


@telescope_uploads_bp.route("", methods=["POST"])
def tus_create():
    from models.photo import TelescopeUpload

    telescope, error = _authenticated_telescope()
    if error:
        return error

    try:
        length = int(request.headers.get("Upload-Length", ""))
    except ValueError:
        return _reply(400, "Upload-Length is required")
    if length < 0:
        return _reply(400, "Upload-Length must not be negative")
    if length > TUS_MAX_SIZE:
        return _reply(413, f"Uploads are limited to {TUS_MAX_SIZE} bytes")

    try:
        meta = _parse_metadata(request.headers.get("Upload-Metadata"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return _reply(400, "Upload-Metadata is not valid base64")

    filename = secure_filename(meta.get("filename") or "")
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if not filename or ext not in ALLOWED_EXTENSIONS:
        return _reply(400, f"File type '.{ext}' is not allowed")

    try:
        owner_id = int(meta.get("owner") or "")
    except ValueError:
        return _reply(400, "owner must be an account id")

    sha256 = (meta.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        return _reply(400, "sha256 must be the file's hex SHA-256")

    kind = (meta.get("kind") or "").strip().lower()
    if not kind:
        kind = "raw" if ext in RAW_EXTENSIONS else "video" if ext in VIDEO_EXTENSIONS else "jpeg"
    if kind not in KINDS:
        return _reply(400, "kind must be jpeg, raw or video")

    captured = _parse_captured(meta.get("captured"))
    if meta.get("captured") and captured is None:
        return _reply(400, "captured must be an ISO 8601 UTC time")

    extra_text = meta.get("meta") or "{}"
    if len(extra_text) > MAX_META_BYTES:
        return _reply(400, "meta is too large")
    try:
        extra = json.loads(extra_text)
    except ValueError:
        return _reply(400, "meta must be JSON")
    if not isinstance(extra, dict):
        return _reply(400, "meta must be a JSON object")

    if not _owner_may_receive(owner_id, telescope, captured or _now()):
        return _reply(403, "That account can't receive photos from this telescope")

    now = _now()
    row = TelescopeUpload(
        id=uuid.uuid4().hex,
        telescope_id=telescope.id,
        owner_id=owner_id,
        upload_length=length,
        upload_offset=0,
        metadata_json=json.dumps({
            "filename": filename,
            "sha256": sha256,
            "kind": kind,
            "captured": captured.isoformat() if captured else None,
            "meta": extra,
        }),
        created_at=now,
        updated_at=now,
        expires_at=now + UPLOAD_TTL,
    )
    os.makedirs(PARTIAL_DIR, exist_ok=True)
    open(_part_path(row.id), "wb").close()
    db.session.add(row)
    db.session.commit()

    if length == 0:
        failure = _finish_upload(row)
        if failure:
            return failure

    return _reply(201, headers={"Location": f"{telescope_uploads_bp.url_prefix}/{row.id}", **_expires_headers(row)})


def _get_upload(upload_id, telescope):
    # (row, None), or (None, 404/410 reply)
    from models.photo import TelescopeUpload

    row = db.session.get(TelescopeUpload, upload_id)
    if row is None or row.telescope_id != telescope.id:
        return None, _reply(404, "No such upload")
    if row.completed_at is None:
        if row.expires_at <= _now() or not os.path.exists(_part_path(row.id)):
            _discard(row)
            return None, _reply(410, "That upload expired")
    return row, None


@telescope_uploads_bp.route("/<upload_id>", methods=["HEAD"])
def tus_head(upload_id):
    telescope, error = _authenticated_telescope()
    if error:
        return error
    row, error = _get_upload(upload_id, telescope)
    if error:
        return error
    offset = row.upload_length if row.completed_at else row.upload_offset
    return _reply(200, headers={
        "Upload-Offset": offset,
        "Upload-Length": row.upload_length,
        "Cache-Control": "no-store",
        **_expires_headers(row),
    })


@telescope_uploads_bp.route("/<upload_id>", methods=["PATCH"])
def tus_patch(upload_id):
    telescope, error = _authenticated_telescope()
    if error:
        return error

    if (request.mimetype or "") != "application/offset+octet-stream":
        return _reply(415, "Content-Type must be application/offset+octet-stream")
    try:
        offset = int(request.headers.get("Upload-Offset", ""))
    except ValueError:
        return _reply(400, "Upload-Offset is required")

    checksum = (request.headers.get("Upload-Checksum") or "").strip()
    expected_sha1 = None
    if checksum:
        algorithm, _, encoded = checksum.partition(" ")
        if algorithm.lower() != "sha1":
            return _reply(400, "Only sha1 checksums are supported")
        try:
            expected_sha1 = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return _reply(400, "Upload-Checksum is not valid base64")

    with _upload_lock(upload_id):
        row, error = _get_upload(upload_id, telescope)
        if error:
            return error
        if row.completed_at:
            if offset == row.upload_length:
                return _reply(204, headers={"Upload-Offset": row.upload_length, **_expires_headers(row)})
            return _reply(409, "Upload already complete", {"Upload-Offset": row.upload_length})
        if offset != row.upload_offset:
            return _reply(409, "Upload-Offset doesn't match", {"Upload-Offset": row.upload_offset})

        data = request.get_data(cache=False)
        if len(data) > MAX_CHUNK_BYTES:
            return _reply(413, f"Chunks are limited to {MAX_CHUNK_BYTES} bytes")
        if offset + len(data) > row.upload_length:
            return _reply(400, "The chunk runs past Upload-Length")
        if expected_sha1 is not None and hashlib.sha1(data).digest() != expected_sha1:
            return _reply(CHECKSUM_MISMATCH, "Checksum mismatch")

        # Write at the offset rather than appending, so a chunk written before a
        # crash but never recorded is simply written again
        with open(_part_path(row.id), "r+b") as part:
            part.seek(offset)
            part.write(data)
            part.flush()
            os.fsync(part.fileno())

        now = _now()
        row.upload_offset = offset + len(data)
        row.updated_at = now
        row.expires_at = now + UPLOAD_TTL
        db.session.commit()

        if row.upload_offset == row.upload_length:
            failure = _finish_upload(row)
            if failure:
                return failure

        return _reply(204, headers={"Upload-Offset": row.upload_offset, **_expires_headers(row)})


@telescope_uploads_bp.route("/<upload_id>", methods=["DELETE"])
def tus_delete(upload_id):
    telescope, error = _authenticated_telescope()
    if error:
        return error
    with _upload_lock(upload_id):
        row, error = _get_upload(upload_id, telescope)
        if error:
            return error
        _discard(row)
    return _reply(204)


def _discard(row):
    try:
        os.remove(_part_path(row.id))
    except FileNotFoundError:
        pass
    db.session.delete(row)
    db.session.commit()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _as_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _site(meta):
    site = meta.get("site")
    if isinstance(site, dict):
        return _as_float(site.get("lat", site.get("latitude"))), _as_float(site.get("lon", site.get("longitude")))
    if isinstance(site, (list, tuple)) and len(site) >= 2:
        return _as_float(site[0]), _as_float(site[1])
    return None, None


def _target_name(meta):
    target = meta.get("target")
    if isinstance(target, dict):
        target = target.get("name")
    return str(target)[:255] if target else None


def _finish_upload(row):
    """Check the whole file, file it under its owner and record the photo.
    Returns an error reply, or None once the upload is complete."""
    from models.photo import Photo

    info = row.details
    meta = info.get("meta") or {}
    part = _part_path(row.id)

    digest = _file_sha256(part)
    if digest != info.get("sha256"):
        # Every chunk matched its checksum but the file doesn't: start it again
        with open(part, "r+b") as f:
            f.truncate(0)
        row.upload_offset = 0
        db.session.commit()
        return _reply(CHECKSUM_MISMATCH, "The file's SHA-256 doesn't match; upload it again")

    now = _now()
    existing = Photo.query.filter_by(owner_id=row.owner_id, sha256=digest).first()
    if existing is not None:
        # A retry of a photo we already have: keep the one copy
        os.remove(part)
        row.completed_at = now
        row.photo_id = existing.id
        db.session.commit()
        return None

    owner_dir = os.path.join(BASE_UPLOAD_FOLDER, str(row.owner_id))
    os.makedirs(owner_dir, exist_ok=True)
    filename = info["filename"]
    stem, dot, ext = filename.rpartition(".")
    dest = os.path.join(owner_dir, filename)
    suffix = 0
    while os.path.exists(dest):
        if _file_sha256(dest) == digest:
            break  # the same file arrived some other way (the old /upload route)
        suffix += 1
        filename = f"{stem}_{suffix}{dot}{ext}"
        dest = os.path.join(owner_dir, filename)

    if os.path.exists(dest):
        os.remove(part)
    else:
        shutil.move(part, dest)

    lat, lon = _site(meta)
    frame_type = meta.get("frame_type") if meta.get("frame_type") in FRAME_TYPES else None
    photo = Photo(
        owner_id=row.owner_id,
        telescope_id=row.telescope_id,
        filename=filename,
        path=os.path.abspath(dest),
        sha256=digest,
        bytes=row.upload_length,
        kind=info.get("kind") or "jpeg",
        frame_type=frame_type,
        label=str(meta["label"])[:255] if meta.get("label") else None,
        captured_at=datetime.fromisoformat(info["captured"]) if info.get("captured") else None,
        uploaded_at=now,
        exposure_s=_as_float(meta.get("exposure_s")),
        iso=str(meta["iso"])[:32] if meta.get("iso") is not None else None,
        target=_target_name(meta),
        ra=_as_float(meta.get("ra")),
        dec=_as_float(meta.get("dec")),
        site_lat=lat,
        site_lon=lon,
        plan_id=str(meta["plan"])[:64] if meta.get("plan") else None,
        step=_as_int(meta.get("step")),
        frame=_as_int(meta.get("frame")),
        pair_id=str(meta["pair"])[:64] if meta.get("pair") else None,
        meta_json=json.dumps(meta) if meta else None,
    )
    db.session.add(photo)
    db.session.flush()
    row.completed_at = now
    row.photo_id = photo.id
    db.session.commit()

    # Show the target in the album, which reads it from the JPEG's description
    if photo.kind == "jpeg" and photo.target:
        try:
            from controllers.album import write_target_keeping_notes
            write_target_keeping_notes(dest, photo.target)
        except Exception:
            logger.exception("Could not write the target into %s", dest)
    return None


def cleanup_stale_uploads():
    """Delete parts untouched for UPLOAD_TTL, and old records of finished uploads."""
    from models.photo import TelescopeUpload

    now = _now()
    removed = 0
    stale = TelescopeUpload.query.filter(
        TelescopeUpload.completed_at.is_(None), TelescopeUpload.updated_at < now - UPLOAD_TTL
    ).all()
    for row in stale:
        try:
            os.remove(_part_path(row.id))
        except FileNotFoundError:
            pass
        db.session.delete(row)
        removed += 1
    TelescopeUpload.query.filter(
        TelescopeUpload.completed_at.isnot(None), TelescopeUpload.completed_at < now - timedelta(days=30)
    ).delete(synchronize_session=False)
    db.session.commit()

    # Parts with no row at all (a crash between creating the file and the row)
    if os.path.isdir(PARTIAL_DIR):
        cutoff = time.time() - UPLOAD_TTL.total_seconds()
        known = {row.id for row in TelescopeUpload.query.with_entities(TelescopeUpload.id)}
        for name in os.listdir(PARTIAL_DIR):
            path = os.path.join(PARTIAL_DIR, name)
            if name.endswith(".part") and name[:-5] not in known and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
    return removed


def start_upload_cleanup(app, interval_s=24 * 3600):
    # Daily job, first run a minute after startup
    def run():
        time.sleep(60)
        while True:
            try:
                with app.app_context():
                    removed = cleanup_stale_uploads()
                if removed:
                    print(f"[Uploads] Removed {removed} stale partial upload(s)")
            except Exception:
                logger.exception("Partial upload cleanup failed")
            time.sleep(interval_s)

    threading.Thread(target=run, name="upload-cleanup", daemon=True).start()
