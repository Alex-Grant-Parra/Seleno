import os
import secrets
from datetime import datetime
from flask import Blueprint, render_template, abort, send_file, send_from_directory, request, jsonify
from werkzeug.utils import secure_filename
from security.token_store import verify_token
from models.client_release import ClientReleaseSubmission

downloads_bp = Blueprint("downloads", __name__)

DOWNLOADS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "downloads"))
RELEASE_CANDIDATE_DIR = ClientReleaseSubmission.candidate_dir()
# Optional extras (schematics, PCB files, etc.). Kept in a subfolder so the
# top-level .zip scan that drives /downloads/latest never sees them.
SCHEMATICS_DIR = os.path.join(DOWNLOADS_DIR, "schematics")


def format_bytes(num_bytes):
    if num_bytes is None:
        return "-"
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def list_zip_versions():
    if not os.path.isdir(DOWNLOADS_DIR):
        return []

    versions = []
    for entry in os.listdir(DOWNLOADS_DIR):
        if not entry.lower().endswith(".zip"):
            continue
        path = os.path.join(DOWNLOADS_DIR, entry)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        version = os.path.splitext(entry)[0]
        versions.append({
            "version": version,
            "filename": entry,
            "path": path,
            "mtime": stat.st_mtime,
            "size_display": format_bytes(stat.st_size),
            "updated": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    versions.sort(key=lambda item: item["mtime"], reverse=True)
    return versions


def resolve_version_file(version):
    versions = list_zip_versions()
    if not versions:
        return None, None

    if version == "latest":
        latest = versions[0]
        return latest["path"], latest["filename"]

    requested = version
    if requested.lower().endswith(".zip"):
        requested = requested[:-4]

    for item in versions:
        if item["version"] == requested:
            return item["path"], item["filename"]

    return None, None


def list_schematic_files():
    if not os.path.isdir(SCHEMATICS_DIR):
        return []

    files = []
    for entry in os.listdir(SCHEMATICS_DIR):
        if entry.startswith("."):
            continue
        path = os.path.join(SCHEMATICS_DIR, entry)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        files.append({
            "filename": entry,
            "mtime": stat.st_mtime,
            "size_display": format_bytes(stat.st_size),
            "updated": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    files.sort(key=lambda item: item["filename"].lower())
    return files


def _extract_api_token_from_request():
    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    header_token = (request.headers.get("X-API-Token") or "").strip()
    if header_token:
        return header_token

    body_token = (request.form.get("token") or "").strip()
    if body_token:
        return body_token

    return None


def _normalize_version(raw_version, fallback_filename):
    base = (raw_version or "").strip()
    if not base:
        base = os.path.splitext(fallback_filename)[0]

    safe = secure_filename(base)
    if not safe:
        return None
    return safe


@downloads_bp.route("/downloads")
def downloads_home():
    versions = list_zip_versions()
    latest = versions[0]["version"] if versions else None
    return render_template("downloads.html", versions=versions, latest=latest)


@downloads_bp.route("/downloads/latest")
def downloads_latest():
    path, filename = resolve_version_file("latest")
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


@downloads_bp.route("/downloads/schematics")
def downloads_schematics():
    return render_template("downloads_schematics.html", files=list_schematic_files())


@downloads_bp.route("/downloads/schematics/<path:filename>")
def downloads_schematic_file(filename):
    if filename.startswith(".") or "/" in filename:
        abort(404)
    return send_from_directory(SCHEMATICS_DIR, filename, as_attachment=True)


@downloads_bp.route("/downloads/<version>")
def downloads_version(version):
    safe_version = secure_filename(version)
    if safe_version != version:
        abort(404)

    path, filename = resolve_version_file(version)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


@downloads_bp.route("/api/developer/releases/upload", methods=["POST"])
def upload_client_release():
    token = _extract_api_token_from_request()
    rec, reason = verify_token(token)
    if not rec:
        return jsonify({"status": "error", "message": f"Unauthorized: {reason or 'invalid token'}"}), 403

    if (rec.client_type or "").strip().lower() != "developer":
        return jsonify({"status": "error", "message": "Developer telescope token required."}), 403

    release_file = request.files.get("release_file") or request.files.get("file")
    if not release_file:
        return jsonify({"status": "error", "message": "Missing release_file upload."}), 400

    original_filename = secure_filename(release_file.filename or "")
    if not original_filename or not original_filename.lower().endswith(".zip"):
        return jsonify({"status": "error", "message": "Only .zip client releases are accepted."}), 400

    version = _normalize_version(request.form.get("version"), original_filename)
    if not version:
        return jsonify({"status": "error", "message": "Invalid version value."}), 400

    os.makedirs(RELEASE_CANDIDATE_DIR, exist_ok=True)
    unique_name = f"{version}__{int(datetime.utcnow().timestamp())}_{secrets.token_hex(4)}.zip"
    save_path = os.path.join(RELEASE_CANDIDATE_DIR, unique_name)
    release_file.save(save_path)

    submission = ClientReleaseSubmission.create_submission(
        version=version,
        original_filename=original_filename,
        stored_filename=unique_name,
        telescope_id=rec.id,
        telescope_name=rec.name,
    )

    return jsonify(
        {
            "status": "success",
            "message": "Release uploaded and queued for admin review.",
            "submission_id": submission.id,
            "version": submission.version,
        }
    ), 201
