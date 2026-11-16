"""The album's index of a user's files, and their thumbnails.

Every file in camera_photos/<owner_id>/ becomes a frame: a JPEG (with its RAW, when
there is one), a RAW on its own, a video, or another image. What a file's EXIF says
is cached in frame_meta and read again only when the file changes, so listing a big
album costs a directory scan, not thousands of file opens.

Thumbnails are made on first request (Pillow's draft mode decodes JPEGs at 1/2 to
1/8 size, so even a 20 MP JPEG takes milliseconds) and cached in .thumbs/. A RAW on
its own is shown by the JPEG preview the camera embedded in it.
"""
import io
import logging
import os
import struct
import subprocess
from datetime import datetime

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

RAW_EXTENSIONS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
                  ".orf", ".rw2", ".raf", ".pef", ".srw", ".erf", ".kdc", ".mrw", ".3fr"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
IMAGE_EXTENSIONS = {".png", ".tif", ".tiff", ".webp", ".gif", ".bmp", ".fits", ".fit"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".ser"}

THUMB_DIR = ".thumbs"
THUMB_SIZES = {"sm": 360, "md": 1600}

# EXIF tags
TAG_DESCRIPTION = 270
TAG_USER_COMMENT = 37510
TAG_MAKE = 271
TAG_MODEL = 272
TAG_EXIF_IFD = 34665
TAG_EXPOSURE = 33434
TAG_ISO = 34855
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME = 306
TAG_JPEG_OFFSET = 513
TAG_JPEG_LENGTH = 514
TAG_STRIP_OFFSETS = 273
TAG_STRIP_BYTES = 279
TAG_COMPRESSION = 259
TAG_SUBIFDS = 330


def kind_of(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in JPEG_EXTENSIONS:
        return "jpeg"
    if ext in RAW_EXTENSIONS:
        return "raw"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def _ratio(value):
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return value[0] / value[1] if value[1] else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _exif_datetime(text):
    if isinstance(text, bytes):
        text = text.decode("ascii", "replace")
    try:
        return datetime.strptime(str(text).strip().rstrip("\x00")[:19], "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip().rstrip("\x00").strip() if value not in (None, "") else None


# --- a small TIFF reader, enough for RAW files' EXIF and their embedded JPEG ---

class _Tiff:
    TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}

    def __init__(self, f):
        self.f = f
        head = f.read(8)
        if head[:2] == b"II":
            self.e = "<"
        elif head[:2] == b"MM":
            self.e = ">"
        else:
            raise ValueError("not a TIFF-based file")
        self.first = struct.unpack(self.e + "I", head[4:8])[0]

    def ifd(self, offset):
        """{tag: value} and the next IFD's offset. Values are ints, tuples or bytes."""
        f, e = self.f, self.e
        f.seek(offset)
        count = struct.unpack(e + "H", f.read(2))[0]
        if count > 1000:
            raise ValueError("implausible IFD")
        entries = [struct.unpack(e + "HHII", f.read(12)) for _ in range(count)]
        next_offset = struct.unpack(e + "I", f.read(4))[0]
        tags = {}
        for tag, typ, n, raw in entries:
            size = self.TYPE_SIZES.get(typ, 1) * n
            if size > 4:
                if size > 1 << 20:
                    continue
                f.seek(raw)
                data = f.read(size)
            else:
                data = struct.pack(e + "I", raw)[:size]
            tags[tag] = self._decode(typ, n, data)
        return tags, next_offset

    def _decode(self, typ, n, data):
        e = self.e
        if typ == 2:
            return data.split(b"\x00", 1)[0].decode("latin-1")
        if typ == 3:
            values = struct.unpack(e + "H" * n, data[:2 * n])
        elif typ in (4, 13):
            values = struct.unpack(e + "I" * n, data[:4 * n])
        elif typ == 5:
            nums = struct.unpack(e + "I" * (2 * n), data[:8 * n])
            values = tuple((nums[2 * i], nums[2 * i + 1]) for i in range(n))
        elif typ == 10:
            nums = struct.unpack(e + "i" * (2 * n), data[:8 * n])
            values = tuple((nums[2 * i], nums[2 * i + 1]) for i in range(n))
        else:
            return data
        return values[0] if n == 1 else values


def _viewable_jpeg(f, offset):
    """Whether the JPEG at `offset` is a baseline or progressive one (a picture), not
    the lossless JPEG some RAW formats keep the sensor data in."""
    f.seek(offset)
    head = f.read(65536)
    if head[:2] != b"\xff\xd8":
        return False
    i = 2
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            return False
        marker = head[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return True
        if 0xC3 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return False
        i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    return False


def _raw_info(path):
    """EXIF fields and the largest embedded JPEG (offset, length) of a TIFF-based RAW."""
    info = {}
    jpegs = []
    with open(path, "rb") as f:
        tiff = _Tiff(f)
        offset, seen = tiff.first, set()
        ifds = []
        while offset and offset not in seen and len(seen) < 8:
            seen.add(offset)
            tags, offset = tiff.ifd(offset)
            ifds.append(tags)
            subs = tags.get(TAG_SUBIFDS)
            for sub in (subs if isinstance(subs, tuple) else (subs,)) if subs else ():
                try:
                    ifds.append(tiff.ifd(sub)[0])
                except Exception:
                    pass
        for tags in ifds:
            if TAG_JPEG_OFFSET in tags and TAG_JPEG_LENGTH in tags:
                off, length = tags[TAG_JPEG_OFFSET], tags[TAG_JPEG_LENGTH]
            elif tags.get(TAG_COMPRESSION) in (6, 7) and TAG_STRIP_OFFSETS in tags:
                off, length = tags[TAG_STRIP_OFFSETS], tags.get(TAG_STRIP_BYTES)
            else:
                continue
            if isinstance(off, int) and isinstance(length, int) and _viewable_jpeg(f, off):
                jpegs.append((length, off))
        first = ifds[0] if ifds else {}
        info["make"] = _text(first.get(TAG_MAKE))
        info["model"] = _text(first.get(TAG_MODEL))
        exif_offset = first.get(TAG_EXIF_IFD)
        if exif_offset:
            exif, _ = tiff.ifd(exif_offset)
            info["exposure_s"] = _ratio(exif.get(TAG_EXPOSURE))
            iso = exif.get(TAG_ISO)
            info["iso"] = str(iso[0] if isinstance(iso, tuple) else iso) if iso else None
            info["captured_at"] = _exif_datetime(exif.get(TAG_DATETIME_ORIGINAL))
        if not info.get("captured_at"):
            info["captured_at"] = _exif_datetime(first.get(TAG_DATETIME))
    if jpegs:
        info["jpeg"] = max(jpegs)[::-1]   # (offset, length) of the biggest
    return info


def embedded_jpeg(path):
    """The camera's JPEG preview inside a RAW, as bytes, or None."""
    try:
        info = _raw_info(path)
    except Exception:
        return None
    if not info.get("jpeg"):
        return None
    offset, length = info["jpeg"]
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    return data if data[:2] == b"\xff\xd8" else None


def read_exif(path, kind):
    """What the file says about itself: captured_at, exposure_s, iso, camera, width,
    height, and for a JPEG its target and notes. Missing values are None."""
    out = {"captured_at": None, "exposure_s": None, "iso": None, "camera": None,
           "width": None, "height": None}
    try:
        if kind == "raw":
            info = _raw_info(path)
            out.update({k: info.get(k) for k in ("captured_at", "exposure_s", "iso")})
            out["camera"] = info.get("model")
            jpeg = embedded_jpeg(path)
            if jpeg:
                with Image.open(io.BytesIO(jpeg)) as img:
                    out["width"], out["height"] = img.size
        elif kind in ("jpeg", "image") and not path.lower().endswith((".fits", ".fit")):
            with Image.open(path) as img:
                out["width"], out["height"] = img.size
                exif = img.getexif()
                if exif:
                    out["camera"] = _text(exif.get(TAG_MODEL))
                    if kind == "jpeg":
                        out["target"] = _text(exif.get(TAG_DESCRIPTION))
                        out["notes"] = _user_comment(exif.get(TAG_USER_COMMENT))
                    sub = exif.get_ifd(TAG_EXIF_IFD) if hasattr(exif, "get_ifd") else {}
                    out["exposure_s"] = _ratio(sub.get(TAG_EXPOSURE))
                    iso = sub.get(TAG_ISO)
                    out["iso"] = str(iso[0] if isinstance(iso, tuple) else iso) if iso else None
                    out["captured_at"] = _exif_datetime(sub.get(TAG_DATETIME_ORIGINAL) or exif.get(TAG_DATETIME))
    except Exception as e:
        logger.info("Couldn't read EXIF from %s: %s", os.path.basename(path), e)
    return out


def _user_comment(value):
    if isinstance(value, bytes):
        if value.startswith(b"UNICODE\x00"):
            return value[8:].decode("utf-16-be", errors="replace").rstrip("\x00").strip() or None
        value = value[8:] if value[:8] in (b"ASCII\x00\x00\x00", b"\x00" * 8) else value
    return _text(value)


def _name_date(filename):
    """photo_20250618_115048.jpg and photo20250618115048.jpg carry their own time."""
    import re
    stem = os.path.splitext(filename)[0]
    for pattern in (r"^photo_(\d{8})_(\d{6})", r"^photo(\d{8})(\d{6})"):
        m = re.match(pattern, stem)
        if m:
            try:
                return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                return None
    return None


def list_frames(user_dir):
    """[(frame filename, raw filename or None)] for the files in user_dir. A RAW whose
    JPEG is there (same name) is folded into it."""
    try:
        names = [n for n in os.listdir(user_dir) if not n.startswith(".") and os.path.isfile(os.path.join(user_dir, n))]
    except FileNotFoundError:
        return []
    by_stem = {}
    for name in names:
        kind = kind_of(name)
        if kind is None:
            continue
        by_stem.setdefault(os.path.splitext(name)[0].lower(), []).append((kind, name))
    frames = []
    for group in by_stem.values():
        jpegs = [n for k, n in group if k == "jpeg"]
        raws = [n for k, n in group if k == "raw"]
        others = [n for k, n in group if k not in ("jpeg", "raw")]
        if jpegs:
            frames.append((jpegs[0], raws[0] if raws else None))
            frames.extend((n, None) for n in jpegs[1:])
            frames.extend((n, None) for n in raws[1:])
        else:
            frames.extend((n, None) for n in raws)
        frames.extend((n, None) for n in others)
    return frames


def refresh_index(owner_id, user_dir):
    """Bring frame_meta up to date with the files on disk. Returns {filename: FrameMeta}
    for the frames (paired RAWs aren't frames of their own)."""
    from app.db import db
    from models.album import FrameMeta
    from models.photo import Photo

    rows = {row.filename: row for row in FrameMeta.query.filter_by(owner_id=owner_id).all()}
    uploads = {row.filename: row for row in Photo.query.filter_by(owner_id=owner_id).all()}
    frames = list_frames(user_dir)
    # A RAW paired by the upload's pair_id (not the same name) is folded in too
    pairs = {}
    for photo in uploads.values():
        if photo.pair_id and photo.kind == "raw":
            pairs.setdefault(photo.pair_id, photo.filename)
    paired_raws = {raw for _, raw in frames if raw}
    for name, raw in list(frames):
        photo = uploads.get(name)
        if raw is None and photo is not None and photo.kind == "jpeg" and photo.pair_id in pairs:
            raw_name = pairs[photo.pair_id]
            if os.path.isfile(os.path.join(user_dir, raw_name)):
                paired_raws.add(raw_name)
                frames[frames.index((name, raw))] = (name, raw_name)
    frames = [(n, r) for n, r in frames if n not in paired_raws]

    seen = set()
    changed = False
    for name, raw in frames:
        seen.add(name)
        path = os.path.join(user_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        row = rows.get(name)
        if row is None:
            row = FrameMeta(owner_id=owner_id, filename=name)
            db.session.add(row)
            rows[name] = row
        if row.raw_filename != raw:
            row.raw_filename = raw
            changed = True
        if row.file_size != st.st_size or row.file_mtime != st.st_mtime:
            kind = kind_of(name)
            exif = read_exif(path, kind)
            if kind == "jpeg":
                exif.setdefault("target", None)
                exif.setdefault("notes", None)
                if raw and not exif.get("exposure_s"):
                    raw_exif = read_exif(os.path.join(user_dir, raw), "raw")
                    exif.update({k: v for k, v in raw_exif.items() if v is not None and not exif.get(k)})
            row.kind = kind
            row.file_size, row.file_mtime = st.st_size, st.st_mtime
            for key, value in exif.items():
                setattr(row, key, value)
            if row.captured_at is None:
                upload = uploads.get(name)
                row.captured_at = (upload.captured_at if upload and upload.captured_at
                                   else _name_date(name) or datetime.utcfromtimestamp(st.st_mtime))
            changed = True
    for name, row in rows.items():
        if name not in seen:
            db.session.delete(row)
            changed = True
    if changed:
        from sqlalchemy.exc import IntegrityError
        try:
            db.session.commit()
        except IntegrityError:
            # Another request indexed the same files at the same moment
            db.session.rollback()
            return {row.filename: row for row in FrameMeta.query.filter_by(owner_id=owner_id).all()
                    if row.filename in seen}
    return {name: rows[name] for name in seen if name in rows}


# --- thumbnails -----------------------------------------------------------------

def thumb_path(user_dir, filename, size):
    return os.path.join(user_dir, THUMB_DIR, f"{filename}.{size}.jpg")


def _video_frame(path):
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1",
                              "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                             capture_output=True, timeout=20, check=True).stdout
        return out or None
    except Exception:
        return None


def make_thumbnail(user_dir, filename, size="sm"):
    """The path of a cached thumbnail (made now if missing or stale), or None when the
    file can't be shown."""
    side = THUMB_SIZES.get(size, THUMB_SIZES["sm"])
    src = os.path.join(user_dir, filename)
    dest = thumb_path(user_dir, filename, size)
    try:
        src_mtime = os.path.getmtime(src)
    except OSError:
        return None
    if os.path.exists(dest) and os.path.getmtime(dest) >= src_mtime:
        return dest
    kind = kind_of(filename)
    source = None
    if kind == "raw":
        data = embedded_jpeg(src)
        source = io.BytesIO(data) if data else None
    elif kind == "video":
        data = _video_frame(src)
        source = io.BytesIO(data) if data else None
    elif kind in ("jpeg", "image"):
        source = src
    if source is None:
        return None
    try:
        with Image.open(source) as img:
            if img.format == "JPEG":
                img.draft("RGB", (side, side))
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((side, side), Image.LANCZOS)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"
            img.save(tmp, "JPEG", quality=84 if size == "sm" else 88)
            os.replace(tmp, dest)
        return dest
    except Exception as e:
        logger.info("Couldn't make a thumbnail of %s: %s", filename, e)
        return None


def forget_thumbnails(user_dir, filename):
    for size in THUMB_SIZES:
        try:
            os.remove(thumb_path(user_dir, filename, size))
        except OSError:
            pass
