import os
import re
from flask import Blueprint, render_template, request, send_file, jsonify, abort
from werkzeug.utils import secure_filename
import zipfile
import io
import tempfile
from datetime import datetime
from flask_login import login_required, current_user
import logging
from PIL import Image, ExifTags

album_bp = Blueprint("album", __name__, template_folder="../templates")

CAMERA_PHOTOS_BASE_DIR = os.path.join(os.path.dirname(__file__), "../camera_photos")
EXIF_IMAGE_DESCRIPTION_TAG = 270
EXIF_USER_COMMENT_TAG = 37510


def _format_file_size(size_bytes):
    if size_bytes is None:
        return "Unknown"
    try:
        size_bytes = float(size_bytes)
    except Exception:
        return "Unknown"
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if size_bytes < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size_bytes)} {unit}"
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0


def _format_exif_number(value, suffix=""):
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, tuple) and len(value) == 2 and value[1]:
            value = value[0] / value[1]
        value = float(value)
        if suffix == "sec":
            if value > 0 and value < 1:
                denominator = round(1 / value)
                return f"1/{denominator} sec"
            return f"{value:.1f} sec"
        if suffix == "f":
            formatted = f"f/{value:.1f}".rstrip("0").rstrip(".")
            return formatted
        if suffix == "mm":
            return f"{value:.0f} mm" if abs(value - round(value)) < 0.05 else f"{value:.1f} mm"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def _decode_user_comment(value):
    if isinstance(value, bytes):
        if value.startswith(b"UNICODE\x00"):
            return value[8:].decode("utf-16-be", errors="replace").rstrip("\x00")
        if value.startswith(b"ASCII\x00\x00\x00"):
            return value[8:].decode("utf-8", errors="replace").rstrip("\x00")
        try:
            return value.decode("utf-8", errors="replace").rstrip("\x00")
        except Exception:
            return value.decode("latin-1", errors="replace").rstrip("\x00")
    if isinstance(value, str):
        return value
    return ""


def _encode_user_comment(value):
    text = (value or "").strip()
    if not text:
        return None
    return b"UNICODE\x00" + text.encode("utf-16-be", errors="replace")


def _safe_photo_path(filename):
    user_dir = get_user_photos_dir()
    if not user_dir or not filename:
        return None

    safe_name = secure_filename(filename)
    if safe_name != filename:
        return None

    path = os.path.abspath(os.path.join(user_dir, safe_name))
    try:
        if os.path.commonpath([os.path.abspath(user_dir), path]) != os.path.abspath(user_dir):
            return None
    except Exception:
        return None

    if not os.path.isfile(path):
        return None
    return path


def _build_metadata_sections(path, filename):
    with Image.open(path) as img:
        exif = img.getexif() or {}
        size_bytes = os.path.getsize(path)
        modified = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        width, height = img.size

        def exif_value(tag_id, default=""):
            value = exif.get(tag_id)
            if value is None:
                return default
            if tag_id == EXIF_USER_COMMENT_TAG:
                return _decode_user_comment(value)
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").rstrip("\x00")
            return value

        target = exif_value(EXIF_IMAGE_DESCRIPTION_TAG, "")
        notes = exif_value(EXIF_USER_COMMENT_TAG, "")

        make = exif_value(271, "")
        model = exif_value(272, "")
        software = exif_value(305, "")
        lens_model = exif_value(42036, "")
        datetime_original = exif_value(36867, "")
        exposure = _format_exif_number(exif_value(33434, ""), "sec")
        aperture = _format_exif_number(exif_value(33437, ""), "f")
        iso = exif_value(34855, "")
        focal_length = _format_exif_number(exif_value(37386, ""), "mm")

        sections = [
            {
                "title": "File",
                "items": [
                    {"label": "Filename", "value": filename},
                    {"label": "Size", "value": _format_file_size(size_bytes)},
                    {"label": "Modified", "value": modified},
                    {"label": "Format", "value": img.format or "JPEG"},
                ],
            },
            {
                "title": "Image",
                "items": [
                    {"label": "Dimensions", "value": f"{width} x {height}"},
                    {"label": "Mode", "value": img.mode or "Unknown"},
                    {"label": "Target", "value": target or ""},
                    {"label": "Notes", "value": notes or ""},
                ],
            },
            {
                "title": "Camera",
                "items": [
                    {"label": "Make", "value": make or ""},
                    {"label": "Model", "value": model or ""},
                    {"label": "Lens", "value": lens_model or ""},
                    {"label": "Software", "value": software or ""},
                ],
            },
            {
                "title": "Exposure",
                "items": [
                    {"label": "Date Taken", "value": datetime_original or ""},
                    {"label": "Exposure", "value": exposure or ""},
                    {"label": "Aperture", "value": aperture or ""},
                    {"label": "ISO", "value": str(iso) if iso not in (None, "") else ""},
                    {"label": "Focal Length", "value": focal_length or ""},
                ],
            },
        ]

        return {
            "name": filename,
            "url": f"/album/photo/{filename}",
            "target": target or "",
            "notes": notes or "",
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "modified": modified,
            "sections": sections,
        }


def _write_metadata_to_jpeg(path, target, notes):
    temp_dir = os.path.dirname(path)
    original_mode = None
    try:
        original_mode = os.stat(path).st_mode
    except Exception:
        original_mode = None

    with tempfile.NamedTemporaryFile(delete=False, dir=temp_dir, suffix=".jpg") as temp_file:
        temp_path = temp_file.name

    try:
        with Image.open(path) as img:
            exif = img.getexif() or Image.Exif()

            target_text = (target or "").strip()
            if target_text:
                exif[EXIF_IMAGE_DESCRIPTION_TAG] = target_text
            elif EXIF_IMAGE_DESCRIPTION_TAG in exif:
                del exif[EXIF_IMAGE_DESCRIPTION_TAG]

            user_comment = _encode_user_comment(notes)
            if user_comment:
                exif[EXIF_USER_COMMENT_TAG] = user_comment
            elif EXIF_USER_COMMENT_TAG in exif:
                del exif[EXIF_USER_COMMENT_TAG]

            save_kwargs = {"format": "JPEG", "exif": exif.tobytes()}
            if img.format == "JPEG":
                save_kwargs.update({"quality": "keep", "subsampling": "keep"})
            if img.info.get("icc_profile"):
                save_kwargs["icc_profile"] = img.info.get("icc_profile")

            img.save(temp_path, **save_kwargs)

        os.replace(temp_path, path)
        if original_mode is not None:
            try:
                os.chmod(path, original_mode)
            except Exception:
                pass
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise

def get_user_photos_dir():
    if not current_user.is_authenticated:
        return None
    user_dir = os.path.join(CAMERA_PHOTOS_BASE_DIR, str(current_user.get_id()))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def get_jpeg_files():
    user_dir = get_user_photos_dir()
    if not user_dir:
        return []
    files = []
    for fname in os.listdir(user_dir):
        if fname.lower().endswith(".jpg") or fname.lower().endswith(".jpeg"):
            # Extract date from filename
            # photo_YYYYMMDD_HHMMSS.jpg
            date = None
            display_str = "Unknown"
            try:
                name_no_ext = os.path.splitext(fname)[0]
                m = re.match(r"^photo_(\d{8})_(\d{6})$", name_no_ext)
                if m:
                    ymd, hms = m.groups()
                    date = datetime.strptime(ymd + hms, "%Y%m%d%H%M%S")
                else:
                    m2 = re.match(r"^photo(\d{8})(\d{6})?$", name_no_ext)
                    if m2:
                        ymd, hms = m2.groups()
                        if hms:
                            date = datetime.strptime(ymd + hms, "%Y%m%d%H%M%S")
                        else:
                            date = datetime.strptime(ymd, "%Y%m%d")
                if date:
                    display_str = date.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                date = None
                display_str = "Unknown"
            files.append({
                "name": fname,
                "date": display_str,
                "datetime": date.timestamp() if date else 0,
                "url": f"/album/photo/{fname}"
            })
    return files


@album_bp.route("/album/photo/<filename>/metadata", methods=["GET", "POST"])
@login_required
def album_photo_metadata(filename):
    path = _safe_photo_path(filename)
    if not path:
        abort(404)

    if request.method == "GET":
        try:
            return jsonify({"status": "success", "photo": _build_metadata_sections(path, filename)})
        except Exception as e:
            logging.exception("Failed to read metadata for %s: %s", filename, e)
            return jsonify({"status": "error", "message": "Failed to read image metadata"}), 500

    data = request.get_json(silent=True) or {}
    target = data.get("target", "")
    notes = data.get("notes", "")

    try:
        _write_metadata_to_jpeg(path, target, notes)
        return jsonify({"status": "success", "photo": _build_metadata_sections(path, filename)})
    except Exception as e:
        logging.exception("Failed to save metadata for %s: %s", filename, e)
        return jsonify({"status": "error", "message": "Failed to save image metadata"}), 500


@album_bp.route("/album/photo/delete", methods=["POST"])
@login_required
def album_photo_delete():
    data = request.get_json() or {}
    files = []
    if isinstance(data.get('files'), list):
        files = data.get('files')
    elif isinstance(data.get('filenames'), list):
        files = data.get('filenames')
    elif data.get('filename'):
        files = [data.get('filename')]

    if not files:
        return jsonify({"status": "error", "message": "No filename(s) provided"}), 400

    user_dir = get_user_photos_dir()
    if not user_dir:
        return jsonify({"status": "error", "message": "Not authenticated"}), 403

    results = {"deleted": [], "failed": []}

    for filename in files:
        if not filename:
            continue
        safe_name = secure_filename(filename)
        if safe_name != filename:
            logging.warning("Attempted unsafe filename delete: %s -> %s", filename, safe_name)
            results["failed"].append({"file": filename, "reason": "invalid filename"})
            continue

        target_jpeg = os.path.join(user_dir, safe_name)
        # Protect against path traversal (ensure target is inside user_dir)
        try:
            if not os.path.commonpath([os.path.abspath(user_dir)]) == os.path.commonpath([os.path.abspath(user_dir), os.path.abspath(target_jpeg)]) or not os.path.isfile(target_jpeg):
                results["failed"].append({"file": filename, "reason": "not found"})
                continue
        except Exception:
            results["failed"].append({"file": filename, "reason": "invalid path"})
            continue

        try:
            os.remove(target_jpeg)
            results["deleted"].append(os.path.basename(target_jpeg))
        except Exception as e:
            logging.exception("Failed to delete jpeg %s: %s", target_jpeg, e)
            results["failed"].append({"file": filename, "reason": "delete_failed"})
            continue

        # also try to delete raw .cr2 if exists
        cr2_path = os.path.splitext(target_jpeg)[0] + ".cr2"
        if os.path.isfile(cr2_path):
            try:
                os.remove(cr2_path)
                results["deleted"].append(os.path.basename(cr2_path))
            except Exception:
                logging.exception("Failed to delete raw file %s", cr2_path)
                # non-fatal; record but continue
                results["failed"].append({"file": os.path.basename(cr2_path), "reason": "raw_delete_failed"})

    status_code = 200 if len(results.get("failed", [])) == 0 else 207
    return jsonify({"status": "partial_success" if status_code == 207 else "success", **results}), status_code


@album_bp.route("/album")
@login_required
def album():
    sort = request.args.get("sort", "date")
    files = get_jpeg_files()
    if sort == "date":
        files.sort(key=lambda x: x["datetime"], reverse=True)
    return render_template("album.html", photos=files)

@album_bp.route("/album/photos")
@login_required
def album_photos():
    files = get_jpeg_files()
    files.sort(key=lambda x: x["datetime"], reverse=True)
    return jsonify(files)

@album_bp.route("/album/photo/<filename>")
@login_required
def album_photo(filename):
    path = _safe_photo_path(filename)
    if not path:
        abort(404)
    return send_file(path)

@album_bp.route("/album/download", methods=["POST"])
@login_required
def album_download():
    data = request.json
    files = data.get("files", [])
    if not files:
        abort(400)
    abs_files = []
    for fname in files:
        if not isinstance(fname, str):
            continue
        # Add the jpeg if it exists
        jpeg_path = _safe_photo_path(fname)
        if jpeg_path:
            abs_files.append(jpeg_path)
        # Add the raw file if it exists
        cr2_path = _safe_photo_path(os.path.splitext(fname)[0] + ".cr2")
        if cr2_path:
            abs_files.append(cr2_path)
    # Always zip 
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in abs_files:
            zf.write(f, os.path.basename(f))
    mem_zip.seek(0)
    if len(files) == 1:
        zip_name = f"{os.path.splitext(files[0])[0]}.zip"
    else:
        zip_name = "photos.zip"
    return send_file(
        mem_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name
    )
