"""The redesigned album's data: the library index, thumbnails, the user's flags and
types, quality numbers measured in the browser, and stacks the browser made.

Stacking and editing run in the browser; the server stores and serves files and
remembers what was decided about them."""
import json
import logging
import math
import os
import shutil
import uuid
from datetime import datetime

from flask import Blueprint, abort, jsonify, request, send_file
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app.album_index import THUMB_SIZES, make_thumbnail, refresh_index
from app.db import db
from controllers.album import (_photo_rows, _safe_photo_path, _write_metadata_to_jpeg, get_user_photos_dir)
from flask_babel import gettext as _

logger = logging.getLogger(__name__)

album_api_bp = Blueprint("album_api", __name__)

STACK_DIR = ".stacks"
STACK_FILES = {
    # name: (mimetype, max bytes)
    "preview.f16": ("application/octet-stream", 80 * 1024 * 1024),
    "auto.jpg": ("image/jpeg", 32 * 1024 * 1024),
    "thumb.jpg": ("image/jpeg", 4 * 1024 * 1024),
    "coverage.u8": ("application/octet-stream", 16 * 1024 * 1024),
}
MASTER_NAME = "master.fits"
MAX_MASTER_BYTES = 2 * 1024 ** 3
MAX_CHUNK_BYTES = 48 * 1024 * 1024
DISK_RESERVE_BYTES = 2 * 1024 ** 3
SAVE_IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "tif": "image/tiff", "fits": "application/fits"}
MAX_SAVE_IMAGE_BYTES = 120 * 1024 * 1024
MAX_METRICS_BYTES = 4096
THUMB_CACHE_S = 30 * 24 * 3600


def _owner_id():
    return int(current_user.get_id())


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _private_cache(resp, seconds):
    resp.cache_control.no_cache = None
    resp.cache_control.public = False
    resp.cache_control.private = True
    resp.cache_control.max_age = seconds
    return resp


# --- the library -----------------------------------------------------------

def _frame_entry(name, row, upload, telescopes, version):
    frame_type = row.frame_type or (upload.frame_type if upload else None)
    entry = {
        "name": name,
        "kind": row.kind,
        "raw": row.raw_filename,
        "url": f"/album/photo/{name}",
        "raw_url": f"/album/photo/{row.raw_filename}" if row.raw_filename else None,
        "thumb": f"/album/thumb/{name}?size=sm&v={version}",
        "preview": f"/album/thumb/{name}?size=md&v={version}",
        "captured_at": _iso(row.captured_at),
        "exposure_s": row.exposure_s if row.exposure_s is not None else (upload.exposure_s if upload else None),
        "iso": row.iso or (upload.iso if upload else None),
        "camera": row.camera,
        "width": row.width,
        "height": row.height,
        "bytes": row.file_size,
        "target": row.target or (upload.target if upload else None),
        "notes": row.notes,
        "frame_type": frame_type,
        "frame_type_set": bool(row.frame_type),
        "flag": row.flag,
        "rating": row.rating,
        "metrics": row.metrics,
        "metrics_source": row.metrics_source,
    }
    if upload is not None:
        entry.update({
            "plan_id": upload.plan_id,
            "step": upload.step,
            "frame": upload.frame,
            "label": upload.label,
            "ra": upload.ra,
            "dec": upload.dec,
            "telescope": telescopes.get(upload.telescope_id),
            "uploaded_at": _iso(upload.uploaded_at),
        })
    return entry


@album_api_bp.route("/album/api/library")
@login_required
def album_library():
    """Every frame the user has, with what the album knows about it."""
    from models.capture_plan import CapturePlan
    from models.tables import Telescope as TelescopeRow
    from models.album import StackResult

    user_dir = get_user_photos_dir()
    index = refresh_index(_owner_id(), user_dir)
    uploads = _photo_rows()
    telescope_ids = {u.telescope_id for u in uploads.values() if u.telescope_id}
    telescopes = {}
    if telescope_ids:
        telescopes = {t.id: t.telescope_id for t in TelescopeRow.query.filter(TelescopeRow.id.in_(telescope_ids))}
    frames = []
    for name, row in index.items():
        version = int(row.file_mtime or 0)
        frames.append(_frame_entry(name, row, uploads.get(name), telescopes, version))
    frames.sort(key=lambda f: f["captured_at"] or "", reverse=True)

    plan_ids = {f.get("plan_id") for f in frames if f.get("plan_id")}
    plans = {}
    if plan_ids:
        for plan in CapturePlan.query.filter(CapturePlan.id.in_(plan_ids), CapturePlan.owner_id == _owner_id()):
            plans[plan.id] = {"id": plan.id, "name": plan.name, "state": plan.state,
                              "started_at": _iso(plan.started_at), "steps": plan.plan.get("steps", [])}
    stacks = StackResult.query.filter_by(owner_id=_owner_id(), state="ready").count()
    return jsonify({"status": "success", "frames": frames, "plans": plans, "stacks": stacks})


@album_api_bp.route("/album/thumb/<filename>")
@login_required
def album_thumb(filename):
    if not _safe_photo_path(filename):
        abort(404)
    size = request.args.get("size", "sm")
    if size not in THUMB_SIZES:
        size = "sm"
    path = make_thumbnail(get_user_photos_dir(), filename, size)
    if not path:
        abort(404)
    return _private_cache(send_file(path, mimetype="image/jpeg"), THUMB_CACHE_S)


def _index_rows(names):
    from models.album import FrameMeta
    names = [n for n in names if isinstance(n, str)]
    if not names:
        return {}
    rows = FrameMeta.query.filter(FrameMeta.owner_id == _owner_id(), FrameMeta.filename.in_(names)).all()
    return {row.filename: row for row in rows}


def _clean_text(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


@album_api_bp.route("/album/api/frames", methods=["POST"])
@login_required
def album_set_frames():
    """Set flag, rating, frame type, target or notes on some frames:
    {"files": [...], "set": {"flag": "pick"|"reject"|null, "rating": 0-5,
    "frame_type": "light"|... |null, "target": "...", "notes": "..."}}"""
    from models.album import FLAGS, USER_FRAME_TYPES

    data = _json_body()
    files = data.get("files")
    changes = data.get("set")
    if not isinstance(files, list) or not files or not isinstance(changes, dict):
        return jsonify({"status": "error", "message": "files and set are required"}), 400
    if len(files) > 5000:
        return jsonify({"status": "error", "message": _("Too many files at once")}), 400

    if "flag" in changes and changes["flag"] not in FLAGS + (None,):
        return jsonify({"status": "error", "message": "flag must be pick, reject or null"}), 400
    if "rating" in changes:
        try:
            rating = None if changes["rating"] in (None, "") else int(changes["rating"])
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "rating must be 0 to 5"}), 400
        if rating is not None and not 0 <= rating <= 5:
            return jsonify({"status": "error", "message": "rating must be 0 to 5"}), 400
        changes["rating"] = rating
    if "frame_type" in changes and changes["frame_type"] not in USER_FRAME_TYPES + (None,):
        return jsonify({"status": "error", "message": _('frame_type must be one of %(types)s', types=', '.join(USER_FRAME_TYPES))}), 400

    refresh_index(_owner_id(), get_user_photos_dir())
    rows = _index_rows(files)
    missing = [f for f in files if f not in rows]
    updated = []
    for name, row in rows.items():
        for key in ("flag", "rating", "frame_type"):
            if key in changes:
                setattr(row, key, changes[key])
        if "target" in changes or "notes" in changes:
            target = _clean_text(changes.get("target", row.target), 255)
            notes = _clean_text(changes.get("notes", row.notes), 4000)
            if row.kind == "jpeg":
                path = _safe_photo_path(name)
                try:
                    _write_metadata_to_jpeg(path, target or "", notes or "")
                    st = os.stat(path)
                    row.file_size, row.file_mtime = st.st_size, st.st_mtime
                except Exception:
                    logger.exception("Couldn't write the target and notes into %s", name)
                    return jsonify({"status": "error", "message": _("Couldn't save the target and notes into %(name)s", name=name)}), 500
            row.target, row.notes = target, notes
        updated.append(name)
    db.session.commit()
    return jsonify({"status": "success", "updated": updated, "missing": missing})


_METRIC_KEYS = {"stars", "fwhm", "hfr", "ecc", "snr", "background", "noise", "background_pct",
                "background_display", "noise_pct", "clipped_pct", "saturated_stars", "width",
                "height", "fwhm_arcsec", "pixel_scale", "seconds", "source", "version"}


@album_api_bp.route("/album/api/metrics", methods=["POST"])
@login_required
def album_metrics():
    """Quality numbers the browser measured: {"metrics": {filename: {...}}}."""
    data = _json_body()
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return jsonify({"status": "error", "message": "metrics is required"}), 400
    rows = _index_rows(list(metrics))
    saved = []
    now = datetime.utcnow()
    for name, values in metrics.items():
        row = rows.get(name)
        if row is None or not isinstance(values, dict):
            continue
        clean = {}
        for key, value in values.items():
            if key not in _METRIC_KEYS:
                continue
            if isinstance(value, bool) or value is None:
                clean[key] = value
            elif isinstance(value, (int, float)):
                if math.isfinite(value):
                    clean[key] = value
            elif isinstance(value, str):
                clean[key] = value[:32]
        text = json.dumps(clean)
        if len(text) > MAX_METRICS_BYTES:
            continue
        row.metrics_json = text
        row.metrics_source = clean.get("source") if clean.get("source") in ("raw", "jpeg") else None
        row.measured_at = now
        saved.append(name)
    db.session.commit()
    return jsonify({"status": "success", "saved": saved})


@album_api_bp.route("/album/api/request_raws", methods=["POST"])
@login_required
def album_request_raws():
    """Ask the telescopes that took these frames to upload their RAWs."""
    from app.telescopeLink import Telescope, result_error
    from app.telescope_access import user_can_control
    from models.tables import Telescope as TelescopeRow

    files = _json_body().get("files")
    if not isinstance(files, list) or not files:
        return jsonify({"status": "error", "message": "files is required"}), 400
    uploads = _photo_rows()
    by_telescope = {}
    skipped = []
    for name in files:
        upload = uploads.get(name) if isinstance(name, str) else None
        if upload is None or not upload.telescope_id:
            skipped.append(name)
            continue
        by_telescope.setdefault(upload.telescope_id, []).append(upload)
    results = []
    for telescope_pk, rows in by_telescope.items():
        scope = db.session.get(TelescopeRow, telescope_pk)
        if scope is None or not user_can_control(current_user, scope):
            results.append({"telescope": scope.telescope_id if scope else None, "status": "error",
                            "message": _("You can't control this telescope now")})
            continue
        t = Telescope(scope.telescope_id)
        pairs = sorted({r.pair_id for r in rows if r.pair_id})
        result = t.uploads.upload_pending("raw", files=[r.filename for r in rows], pairs=pairs)
        error = result_error(result)
        results.append({"telescope": scope.telescope_id, "status": "error" if error else "success",
                        "message": error, "frames": len(rows)})
    return jsonify({"status": "success", "results": results, "skipped": skipped})


# --- stacks ------------------------------------------------------------------

def _stack_dir(stack_id):
    return os.path.join(get_user_photos_dir(), STACK_DIR, stack_id)


def _own_stack(stack_id):
    from models.album import StackResult
    if not stack_id or len(stack_id) != 32 or not all(c in "0123456789abcdef" for c in stack_id):
        abort(404)
    row = StackResult.query.filter_by(id=stack_id, owner_id=_owner_id()).first()
    if row is None:
        abort(404)
    return row


def _json_field(value, limit=512 * 1024):
    if value is None:
        return None
    text = json.dumps(value)
    if len(text) > limit:
        raise ValueError("too large")
    return text


def _positive_int(value, limit):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= limit else None


@album_api_bp.route("/album/api/stacks", methods=["GET"])
@login_required
def stacks_list():
    from models.album import StackResult
    rows = StackResult.query.filter_by(owner_id=_owner_id()).order_by(StackResult.created_at.desc()).all()
    return jsonify({"status": "success", "stacks": [row.to_dict() for row in rows]})


@album_api_bp.route("/album/api/stacks", methods=["POST"])
@login_required
def stacks_create():
    """Start saving a stack: its description now, its files next."""
    from models.album import StackResult

    data = _json_body()
    name = _clean_text(data.get("name"), 255) or "Stack"
    width = _positive_int(data.get("width"), 100000)
    height = _positive_int(data.get("height"), 100000)
    if not width or not height:
        return jsonify({"status": "error", "message": "width and height are required"}), 400
    master_bytes = data.get("master_bytes")
    if master_bytes not in (None, 0):
        master_bytes = _positive_int(master_bytes, MAX_MASTER_BYTES)
        if master_bytes is None:
            return jsonify({"status": "error", "message": _("The result is too big to keep on the server")}), 400
    user_dir = get_user_photos_dir()
    free = shutil.disk_usage(user_dir).free
    if (master_bytes or 0) + DISK_RESERVE_BYTES > free:
        return jsonify({"status": "error", "message": _("The server is short of disk space; download the result instead")}), 507
    kind = data.get("kind") if data.get("kind") in ("stack", "planetary", "single") else "stack"
    try:
        row = StackResult(
            id=uuid.uuid4().hex,
            owner_id=_owner_id(),
            name=name,
            target=_clean_text(data.get("target"), 255),
            kind=kind,
            state="uploading",
            width=width,
            height=height,
            preview_width=_positive_int(data.get("preview_width"), 20000),
            preview_height=_positive_int(data.get("preview_height"), 20000),
            frames_used=_positive_int(data.get("frames_used"), 100000),
            frames_total=_positive_int(data.get("frames_total"), 100000),
            total_exposure_s=float(data["total_exposure_s"]) if isinstance(data.get("total_exposure_s"), (int, float)) else None,
            linear=bool(data.get("linear", True)),
            settings_json=_json_field(data.get("settings")),
            report_json=_json_field(data.get("report"), 4 * 1024 * 1024),
            edit_json=_json_field(data.get("edit")),
            master_bytes=master_bytes or None,
            master_received=0,
        )
    except ValueError:
        return jsonify({"status": "error", "message": _("The stack's description is too large")}), 400
    os.makedirs(_stack_dir(row.id), exist_ok=True)
    db.session.add(row)
    db.session.commit()
    return jsonify({"status": "success", "stack": row.to_dict()})


@album_api_bp.route("/album/api/stacks/<stack_id>/file/<name>", methods=["PUT"])
@login_required
def stacks_put_file(stack_id, name):
    row = _own_stack(stack_id)
    spec = STACK_FILES.get(name)
    if spec is None:
        abort(404)
    _unused, limit = spec
    if (request.content_length or 0) > limit:
        return jsonify({"status": "error", "message": _('%(name)s is too large', name=name)}), 413
    data = request.get_data(cache=False)
    if len(data) > limit or not data:
        return jsonify({"status": "error", "message": _('%(name)s is empty or too large', name=name)}), 400
    if name.endswith(".jpg") and data[:3] != b"\xff\xd8\xff":
        return jsonify({"status": "error", "message": _("%(name)s isn't a JPEG", name=name)}), 400
    if name == "preview.f16" and row.preview_width and row.preview_height:
        if len(data) != row.preview_width * row.preview_height * 3 * 2:
            return jsonify({"status": "error", "message": "preview.f16 doesn't match the preview size"}), 400
    path = os.path.join(_stack_dir(row.id), name)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"status": "success", "bytes": len(data)})


@album_api_bp.route("/album/api/stacks/<stack_id>/master", methods=["PUT"])
@login_required
def stacks_put_master(stack_id):
    """One chunk of master.fits, at ?offset= (which must be what the server has)."""
    row = _own_stack(stack_id)
    if not row.master_bytes:
        return jsonify({"status": "error", "message": _("This stack has no master file")}), 400
    try:
        offset = int(request.args.get("offset", ""))
    except ValueError:
        return jsonify({"status": "error", "message": "offset is required"}), 400
    if offset != row.master_received:
        return jsonify({"status": "error", "message": "Wrong offset", "received": row.master_received}), 409
    if (request.content_length or 0) > MAX_CHUNK_BYTES:
        return jsonify({"status": "error", "message": "Chunk too large"}), 413
    data = request.get_data(cache=False)
    if not data or offset + len(data) > row.master_bytes:
        return jsonify({"status": "error", "message": "Chunk is empty or runs past the end"}), 400
    if offset == 0 and not data.startswith(b"SIMPLE  ="):
        return jsonify({"status": "error", "message": "master.fits isn't a FITS file"}), 400
    path = os.path.join(_stack_dir(row.id), MASTER_NAME)
    with open(path, "r+b" if offset else "wb") as f:
        f.seek(offset)
        f.write(data)
    row.master_received = offset + len(data)
    db.session.commit()
    return jsonify({"status": "success", "received": row.master_received, "total": row.master_bytes})


@album_api_bp.route("/album/api/stacks/<stack_id>/finish", methods=["POST"])
@login_required
def stacks_finish(stack_id):
    row = _own_stack(stack_id)
    folder = _stack_dir(row.id)
    missing = [name for name in ("auto.jpg", "thumb.jpg") if not os.path.isfile(os.path.join(folder, name))]
    if row.linear and not os.path.isfile(os.path.join(folder, "preview.f16")):
        missing.append("preview.f16")
    if row.master_bytes and row.master_received < row.master_bytes:
        missing.append(MASTER_NAME)
    if missing:
        return jsonify({"status": "error", "message": _('Still missing: %(missing)s', missing=', '.join(missing))}), 400
    row.state = "ready"
    row.bytes_total = sum(os.path.getsize(os.path.join(folder, n)) for n in os.listdir(folder)
                          if os.path.isfile(os.path.join(folder, n)))
    db.session.commit()
    return jsonify({"status": "success", "stack": row.to_dict()})


@album_api_bp.route("/album/api/stacks/<stack_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def stacks_one(stack_id):
    row = _own_stack(stack_id)
    if request.method == "GET":
        return jsonify({"status": "success", "stack": row.to_dict(full=True)})
    if request.method == "DELETE":
        shutil.rmtree(_stack_dir(row.id), ignore_errors=True)
        db.session.delete(row)
        db.session.commit()
        return jsonify({"status": "success"})
    data = _json_body()
    if "name" in data:
        row.name = _clean_text(data.get("name"), 255) or row.name
    if "target" in data:
        row.target = _clean_text(data.get("target"), 255)
    if "edit" in data:
        try:
            row.edit_json = _json_field(data.get("edit"))
        except ValueError:
            return jsonify({"status": "error", "message": _("The edit settings are too large")}), 400
    db.session.commit()
    return jsonify({"status": "success", "stack": row.to_dict()})


@album_api_bp.route("/album/stacks/<stack_id>/<name>")
@login_required
def stacks_file(stack_id, name):
    row = _own_stack(stack_id)
    if name != MASTER_NAME and name not in STACK_FILES:
        abort(404)
    path = os.path.join(_stack_dir(row.id), name)
    if not os.path.isfile(path):
        abort(404)
    mimetype = "application/fits" if name == MASTER_NAME else STACK_FILES[name][0]
    download = request.args.get("download") == "1"
    resp = send_file(path, mimetype=mimetype, conditional=True, as_attachment=download,
                     download_name=f"{secure_filename(row.name) or 'stack'}_{name}" if download else None)
    return _private_cache(resp, 3600) if name in ("thumb.jpg", "auto.jpg") else resp


@album_api_bp.route("/album/api/save_image", methods=["POST"])
@login_required
def album_save_image():
    """Keep an image the browser made (an edited stack, say) in the album as a new photo."""
    from models.album import FrameMeta

    upload = request.files.get("file")
    if upload is None:
        return jsonify({"status": "error", "message": "No file"}), 400
    fmt = request.form.get("format", "jpg")
    if fmt not in SAVE_IMAGE_TYPES:
        return jsonify({"status": "error", "message": "format must be jpg, png, tif or fits"}), 400
    data = upload.read(MAX_SAVE_IMAGE_BYTES + 1)
    if len(data) > MAX_SAVE_IMAGE_BYTES or not data:
        return jsonify({"status": "error", "message": _("The image is empty or too large")}), 400
    magic = {"jpg": (b"\xff\xd8\xff",), "png": (b"\x89PNG",), "tif": (b"II*\x00", b"MM\x00*"), "fits": (b"SIMPLE  =",)}
    if not data.startswith(magic[fmt]):
        return jsonify({"status": "error", "message": _("That isn't a %(format)s file", format=fmt.upper())}), 400
    stem = secure_filename(request.form.get("name") or "") or "edited"
    stem = stem[:80]
    user_dir = get_user_photos_dir()
    filename = f"{stem}.{fmt}"
    n = 1
    while os.path.exists(os.path.join(user_dir, filename)):
        n += 1
        filename = f"{stem}_{n}.{fmt}"
    path = os.path.join(user_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    target = _clean_text(request.form.get("target"), 255)
    if fmt == "jpg" and target:
        try:
            _write_metadata_to_jpeg(path, target, request.form.get("notes") or "")
        except Exception:
            logger.exception("Couldn't write the target into %s", filename)
    refresh_index(_owner_id(), user_dir)
    row = FrameMeta.query.filter_by(owner_id=_owner_id(), filename=filename).first()
    if row is not None:
        row.frame_type = "processed"
        if fmt != "jpg":
            row.target = target
        db.session.commit()
    return jsonify({"status": "success", "name": filename, "url": f"/album/photo/{filename}"})
