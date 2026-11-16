"""Where telescope photos are stored and which file types they may be.

Telescopes upload through the resumable API in controllers/telescope_uploads.py.
"""
import os

BASE_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "..", "camera_photos")
os.makedirs(BASE_UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp',
    'cr2', 'cr3', 'nef', 'nrw', 'arw', 'orf', 'rw2', 'raf', 'pef', 'srw', 'dng', 'heic', 'fits',
    # Video steps in capture plans (SER: uncompressed planetary video)
    'mp4', 'avi', 'ser',
}


def allowed_file(filename: str) -> bool:
    if not filename or '.' not in filename:
        return False
    return filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
