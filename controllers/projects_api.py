"""Projects: integrations built up over many nights in the browser.

The browser stacks each night's frames into running totals (weighted sums, weights,
coverage and rejection limits) and uploads them as FITS files. Uploads go to staging/
in chunks the browser can resume; a commit then makes them the project's totals
(current/), or a rebuild's checkpoint (rebuild/), in one rename, so a half-finished
upload never replaces good totals. The server only stores and serves; no pixels are
processed here."""
import json
import os
import shutil
import uuid
from datetime import datetime

from flask import Blueprint, abort, jsonify, request, send_file
from flask_login import current_user, login_required

from app.db import db
from controllers.album import get_user_photos_dir

projects_api_bp = Blueprint("projects_api", __name__)

PROJECT_DIR = ".projects"
TOTALS_FILES = ("sum.fits", "wsum.fits", "coverage.fits", "limits.fits")
AREAS = ("staging", "current", "rebuild")
MAX_FILE_BYTES = 2 * 1024 ** 3
MAX_CHUNK_BYTES = 48 * 1024 * 1024
DISK_RESERVE_BYTES = 2 * 1024 ** 3
MAX_FRAMES = 200000


def _owner_id():
    return int(current_user.get_id())


def _project_dir(project_id, area=None):
    base = os.path.join(get_user_photos_dir(), PROJECT_DIR, project_id)
    return os.path.join(base, area) if area else base


def _own(project_id):
    from models.album import Project
    if not project_id or len(project_id) != 32 or not all(c in "0123456789abcdef" for c in project_id):
        abort(404)
    row = Project.query.filter_by(id=project_id, owner_id=_owner_id()).first()
    if row is None:
        abort(404)
    return row


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _text(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _int(value, lo, hi):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if lo <= number <= hi else None


def _dump(value, limit):
    text = json.dumps(value)
    if len(text) > limit:
        raise ValueError("too large")
    return text


def _sizes(folder):
    if not os.path.isdir(folder):
        return {}
    return {name: os.path.getsize(os.path.join(folder, name)) for name in TOTALS_FILES
            if os.path.isfile(os.path.join(folder, name))}


def _folder_bytes(folder):
    total = 0
    for root, _, files in os.walk(folder):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    return total


@projects_api_bp.route("/album/api/projects", methods=["GET"])
@login_required
def projects_list():
    from models.album import Project
    rows = Project.query.filter_by(owner_id=_owner_id()).order_by(Project.updated_at.desc()).all()
    return jsonify({"status": "success", "projects": [row.to_dict() for row in rows]})


@projects_api_bp.route("/album/api/projects", methods=["POST"])
@login_required
def projects_create():
    from models.album import Project
    data = _body()
    width = _int(data.get("width"), 1, 100000)
    height = _int(data.get("height"), 1, 100000)
    if not width or not height:
        return jsonify({"status": "error", "message": "width and height are required"}), 400
    try:
        row = Project(
            id=uuid.uuid4().hex, owner_id=_owner_id(),
            name=_text(data.get("name"), 255) or "Project",
            target=_text(data.get("target"), 255),
            source="jpeg" if data.get("source") == "jpeg" else "raw",
            width=width, height=height,
            settings_json=_dump(data.get("settings") or {}, 64 * 1024),
            reference_json=_dump(data.get("reference"), 512 * 1024) if data.get("reference") else None,
            frames_json="[]", seen_json="[]", nights_json="[]", frames_total=0, exposure_total_s=0.0, generation=0,
        )
    except ValueError:
        return jsonify({"status": "error", "message": "The project's description is too large"}), 400
    os.makedirs(_project_dir(row.id), exist_ok=True)
    db.session.add(row)
    db.session.commit()
    return jsonify({"status": "success", "project": row.to_dict(full=True)})


@projects_api_bp.route("/album/api/projects/<project_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def projects_one(project_id):
    row = _own(project_id)
    if request.method == "GET":
        out = row.to_dict(full=True)
        out["staged"] = _sizes(_project_dir(row.id, "staging"))
        return jsonify({"status": "success", "project": out})
    if request.method == "DELETE":
        shutil.rmtree(_project_dir(row.id), ignore_errors=True)
        db.session.delete(row)
        db.session.commit()
        return jsonify({"status": "success"})
    data = _body()
    if "name" in data:
        row.name = _text(data.get("name"), 255) or row.name
    if "target" in data:
        row.target = _text(data.get("target"), 255)
    if "stack_id" in data:
        sid = data.get("stack_id")
        row.stack_id = sid if isinstance(sid, str) and len(sid) == 32 else None
    db.session.commit()
    return jsonify({"status": "success", "project": row.to_dict(full=True)})


@projects_api_bp.route("/album/api/projects/<project_id>/upload/<name>", methods=["PUT"])
@login_required
def projects_upload(project_id, name):
    """A chunk of a totals file into staging/, at ?offset= (what the server has; a new
    upload starts at 0 and replaces any earlier staged file)."""
    row = _own(project_id)
    if name not in TOTALS_FILES:
        abort(404)
    offset = _int(request.args.get("offset"), 0, MAX_FILE_BYTES)
    if offset is None:
        return jsonify({"status": "error", "message": "offset is required"}), 400
    folder = _project_dir(row.id, "staging")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    have = os.path.getsize(path) if os.path.isfile(path) else 0
    if offset and offset != have:
        return jsonify({"status": "error", "message": "Wrong offset", "received": have}), 409
    if (request.content_length or 0) > MAX_CHUNK_BYTES:
        return jsonify({"status": "error", "message": "Chunk too large"}), 413
    data = request.get_data(cache=False)
    if not data or offset + len(data) > MAX_FILE_BYTES:
        return jsonify({"status": "error", "message": "Chunk is empty or the file is too large"}), 400
    if offset == 0:
        if not data.startswith(b"SIMPLE  ="):
            return jsonify({"status": "error", "message": f"{name} isn't a FITS file"}), 400
        if shutil.disk_usage(get_user_photos_dir()).free < DISK_RESERVE_BYTES + len(data):
            return jsonify({"status": "error", "message": "The server is short of disk space"}), 507
    with open(path, "r+b" if offset else "wb") as f:
        f.seek(offset)
        f.write(data)
    return jsonify({"status": "success", "received": offset + len(data)})


def _swap_in(row, area, expected):
    """Make staging/ the given area, after checking every file arrived whole."""
    staging = _project_dir(row.id, "staging")
    sizes = _sizes(staging)
    missing = [n for n in TOTALS_FILES if n not in sizes]
    wrong = [n for n, size in (expected or {}).items() if n in sizes and sizes[n] != size]
    if missing or wrong:
        return f"Still missing or incomplete: {', '.join(missing + wrong)}"
    target = _project_dir(row.id, area)
    old = target + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(target):
        os.replace(target, old)
    os.replace(staging, target)
    shutil.rmtree(old, ignore_errors=True)
    return None


@projects_api_bp.route("/album/api/projects/<project_id>/commit", methods=["POST"])
@login_required
def projects_commit(project_id):
    """The staged totals become the project's. mode:
      night       a night's frames were added: frames (names), considered (every name
                  looked at, so frames left out aren't judged again), exposure_s, night
      checkpoint  a rebuild's progress: done (names considered), accepted (names in the
                  totals so far), total, exposure_s, settings
      rebuilt     a rebuild finished: frames (all names), exposure_s, settings"""
    row = _own(project_id)
    data = _body()
    mode = data.get("mode")
    if mode not in ("night", "checkpoint", "rebuilt"):
        return jsonify({"status": "error", "message": "mode must be night, checkpoint or rebuilt"}), 400
    names = data.get("frames") if mode != "checkpoint" else data.get("done")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or len(names) > MAX_FRAMES:
        return jsonify({"status": "error", "message": "frames must be a list of names"}), 400
    names = [n[:255] for n in names]
    exposure = data.get("exposure_s")
    exposure = float(exposure) if isinstance(exposure, (int, float)) and exposure >= 0 else 0.0
    expected = data.get("files") if isinstance(data.get("files"), dict) else None
    try:
        if mode == "checkpoint":
            accepted = data.get("accepted") if isinstance(data.get("accepted"), list) else []
            accepted = [str(n)[:255] for n in accepted[:MAX_FRAMES]]
            rebuild = {"done": names, "accepted": accepted, "total": _int(data.get("total"), 0, MAX_FRAMES) or len(names),
                       "settings": data.get("settings") or {}, "exposure_s": exposure,
                       "updated_at": datetime.utcnow().isoformat() + "Z"}
            rebuild_json = _dump(rebuild, 8 * 1024 * 1024)
        else:
            frames = names if mode == "rebuilt" else list(dict.fromkeys(json.loads(row.frames_json or "[]") + names))
            frames_json = _dump(frames, 8 * 1024 * 1024)
            considered = data.get("considered") if isinstance(data.get("considered"), list) else []
            considered = [str(n)[:255] for n in considered[:MAX_FRAMES]]
            seen = considered if mode == "rebuilt" else json.loads(row.seen_json or "[]") + considered
            seen_json = _dump(list(dict.fromkeys(seen + frames)), 8 * 1024 * 1024)
            reference = data.get("reference")
            reference_json = _dump(reference, 512 * 1024) if reference else None
            settings_json = _dump(data["settings"], 64 * 1024) if mode == "rebuilt" and isinstance(data.get("settings"), dict) else None
    except ValueError:
        return jsonify({"status": "error", "message": "Too much to record"}), 400

    error = _swap_in(row, "rebuild" if mode == "checkpoint" else "current", expected)
    if error:
        return jsonify({"status": "error", "message": error}), 400
    if mode == "checkpoint":
        row.rebuild_json = rebuild_json
    else:
        row.frames_json = frames_json
        row.seen_json = seen_json
        if reference_json:
            row.reference_json = reference_json
        if mode == "night":
            nights = json.loads(row.nights_json or "[]")
            nights.append({"night": _text(data.get("night"), 32), "frames": len(names), "exposure_s": exposure,
                           "added_at": datetime.utcnow().isoformat() + "Z"})
            row.nights_json = json.dumps(nights[-1000:])
            row.exposure_total_s = (row.exposure_total_s or 0) + exposure
        else:
            row.exposure_total_s = exposure
            if settings_json:
                row.settings_json = settings_json
            row.rebuild_json = None
            shutil.rmtree(_project_dir(row.id, "rebuild"), ignore_errors=True)
        row.frames_total = len(json.loads(frames_json))
        row.generation = (row.generation or 0) + 1
    row.bytes_total = _folder_bytes(_project_dir(row.id))
    db.session.commit()
    return jsonify({"status": "success", "project": row.to_dict(full=True)})


@projects_api_bp.route("/album/api/projects/<project_id>/rebuild", methods=["DELETE"])
@login_required
def projects_rebuild_cancel(project_id):
    row = _own(project_id)
    row.rebuild_json = None
    shutil.rmtree(_project_dir(row.id, "rebuild"), ignore_errors=True)
    shutil.rmtree(_project_dir(row.id, "staging"), ignore_errors=True)
    row.bytes_total = _folder_bytes(_project_dir(row.id))
    db.session.commit()
    return jsonify({"status": "success", "project": row.to_dict(full=True)})


@projects_api_bp.route("/album/projects/<project_id>/<area>/<name>")
@login_required
def projects_file(project_id, area, name):
    row = _own(project_id)
    if area not in ("current", "rebuild") or name not in TOTALS_FILES:
        abort(404)
    path = os.path.join(_project_dir(row.id, area), name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="application/fits", conditional=True)
