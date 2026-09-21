import os
import traceback
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
import traceback

upload_bp = Blueprint("upload", __name__)

BASE_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "..", "camera_photos")
os.makedirs(BASE_UPLOAD_FOLDER, exist_ok=True)  # Ensure the base folder exists

# Allowed file extensions for upload
ALLOWED_EXTENSIONS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp',
    'cr2', 'nef', 'arw', 'orf', 'rw2', 'dng', 'heic', 'fits'
}

def allowed_file(filename: str) -> bool:
    try:
        if not filename or '.' not in filename:
            return False
        ext = filename.rsplit('.', 1)[1].lower()
        return ext in ALLOWED_EXTENSIONS
    except Exception:
        return False

@upload_bp.route('/upload', methods=['POST'])
def upload_file():
    print("\n=== [DEBUG] Upload request received ===")
    try:
        print(f"Method: {request.method} Path: {request.path}")
        print(f"Content-Type: {request.headers.get('Content-Type')} (mimetype={request.mimetype})")
        print(f"Content-Length: {request.headers.get('Content-Length')}")
        print(f"Origin: {request.headers.get('Origin')}  Referer: {request.headers.get('Referer')}")
        print(f"Has X-CSRFToken: {bool(request.headers.get('X-CSRFToken') or request.headers.get('X-CSRF-Token'))}")
        print(f"Cookies: {list(request.cookies.keys())}")
        print(f"Files Received: {list(request.files.keys())}")
        print(f"Form fields: {list(request.form.keys())}")
    except Exception as e:
        print(f"[WARN] Failed to print request diagnostics: {e}")

    # Ensure multipart form
    if not (request.mimetype or '').startswith('multipart/'):
        print("[ERROR] Request is not multipart/form-data")
        return jsonify({"status": "error", "message": "Content-Type must be multipart/form-data"}), 400

    if not request.files:
        print("[ERROR] No files in request.")
        return jsonify({"status": "error", "message": "No files provided"}), 400

    saved_files = []
    for file_key, file in request.files.items():
        original_name = file.filename or ''
        print(f"[DEBUG] Processing file: {original_name}")

        if original_name.strip() == '':
            print("[ERROR] Empty filename detected.")
            return jsonify({"status": "error", "message": "Empty filename provided"}), 400

        # Expected format: "<user_id>_<rest-of-filename>"
        parts = original_name.split("_", 1)
        if len(parts) < 2 or not parts[0].isdigit():
            print("[ERROR] Invalid filename format. Expected '<user_id>_<name.ext>'")
            return jsonify({"status": "error", "message": "Invalid filename format"}), 400

        user_id = parts[0]
        remainder = parts[1]

        # Sanitize the remainder of the filename
        safe_name = secure_filename(remainder)
        if not safe_name or '.' not in safe_name:
            print(f"[ERROR] Invalid sanitized filename from remainder='{remainder}' -> safe='{safe_name}'")
            return jsonify({"status": "error", "message": "Invalid filename after sanitization"}), 400

        # Enforce allowed extensions
        if not allowed_file(safe_name):
            ext = safe_name.rsplit('.', 1)[1].lower() if '.' in safe_name else ''
            print(f"[ERROR] Disallowed file extension: .{ext}")
            return jsonify({"status": "error", "message": f"File type '.{ext}' is not allowed"}), 400

        # Create user folder and save
        user_upload_folder = os.path.join(BASE_UPLOAD_FOLDER, user_id)
        os.makedirs(user_upload_folder, exist_ok=True)

        save_path = os.path.join(user_upload_folder, safe_name)
        try:
            file.save(save_path)
            print(f"[DEBUG] Saved '{safe_name}' to '{user_upload_folder}'")

            # Verify the file was saved
            if not os.path.exists(save_path):
                print(f"[ERROR] File '{safe_name}' was not saved properly.")
                return jsonify({"status": "error", "message": f"Failed to save '{safe_name}'"}), 500

            saved_files.append(safe_name)
        except Exception as e:
            print(f"[ERROR] Failed to save '{safe_name}': {e}")
            traceback.print_exc()
            return jsonify({"status": "error", "message": f"Error saving '{safe_name}'"}), 500

    print("[DEBUG] Upload successful")
    return jsonify({"status": "success", "message": f"Files uploaded to '{user_upload_folder}': {saved_files}"}), 200
