"""Reading frames for processing.

RAW files (through LibRaw) give the sensor's Bayer mosaic: linear data, black level
removed, scaled so 1.0 is the white level. Calibration (bias, darks, flats) happens on
the mosaic, before debayering, which is where it is exact.

JPEGs are decoded and, by default, turned back into roughly linear light with the
inverse sRGB curve. A camera JPEG has its own tone curve and noise reduction, so this
is an approximation: stacking JPEGs works, calibrating them only roughly.
"""
import os

import numpy as np
from PIL import Image
from scipy import ndimage

RAW_EXTENSIONS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
                  ".orf", ".rw2", ".raf", ".pef", ".srw", ".erf", ".kdc", ".mrw", ".3fr"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}

# sRGB (D65) to XYZ, as dcraw and LibRaw use it
XYZ_FROM_SRGB = np.array([[0.412453, 0.357580, 0.180423],
                          [0.212671, 0.715160, 0.072169],
                          [0.019334, 0.119193, 0.950227]])

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

Image.MAX_IMAGE_PIXELS = 400_000_000


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTENSIONS:
        return "raw"
    if ext in JPEG_EXTENSIONS:
        return "jpeg"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def _srgb_lut():
    v = np.arange(256, dtype=np.float64) / 255.0
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    return lin.astype(np.float32)


SRGB_TO_LINEAR_LUT = _srgb_lut()


def srgb_to_linear(values):
    v = np.asarray(values, dtype=np.float32)
    return np.where(v <= 0.04045, v / 12.92, ((np.maximum(v, 0) + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(values):
    v = np.clip(np.asarray(values, dtype=np.float32), 0, None)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055).astype(np.float32)


class Mosaic:
    """A RAW frame: the Bayer mosaic and what's needed to turn it into colour."""

    def __init__(self, data, pattern, wb, wb_as_shot, cam_to_srgb, camera=""):
        self.data = data                  # float32 (H, W), 0 = black level, 1 = white level
        self.pattern = pattern            # 2x2 ints: 0 red, 1 green, 2 blue
        self.wb = wb                      # daylight white balance, green = 1
        self.wb_as_shot = wb_as_shot      # the camera's white balance for this shot
        self.cam_to_srgb = cam_to_srgb    # 3x3, camera RGB (white balanced) to linear sRGB
        self.camera = camera

    @property
    def shape(self):
        return self.data.shape


def _normalised_wb(values):
    values = [float(v) for v in list(values)[:3]]
    if len(values) < 3 or min(values) <= 0 or values[1] <= 0:
        return None
    return np.array([values[0] / values[1], 1.0, values[2] / values[1]], dtype=np.float32)


def cam_to_srgb_matrix(cam_xyz):
    cam_xyz = np.asarray(cam_xyz, dtype=np.float64)[:3, :3]
    if not np.any(cam_xyz):
        return np.eye(3, dtype=np.float32)
    cam_rgb = cam_xyz @ XYZ_FROM_SRGB
    sums = cam_rgb.sum(axis=1, keepdims=True)
    if np.any(np.abs(sums) < 1e-9):
        return np.eye(3, dtype=np.float32)
    cam_rgb /= sums
    try:
        return np.linalg.inv(cam_rgb).astype(np.float32)
    except np.linalg.LinAlgError:
        return np.eye(3, dtype=np.float32)


def read_raw(path):
    import rawpy

    with rawpy.imread(path) as raw:
        desc = raw.color_desc.decode("ascii", errors="replace")
        if any(ch not in "RGB" for ch in desc[:4]):
            raise ValueError(f"{os.path.basename(path)}: only RGB Bayer sensors are supported")
        colours = np.array(raw.raw_colors_visible[:2, :2])
        pattern = np.array([["RGB".index(desc[c]) for c in row] for row in colours])
        if sorted(pattern.ravel().tolist()) != [0, 1, 1, 2]:
            raise ValueError(f"{os.path.basename(path)}: not a Bayer sensor")

        data = raw.raw_image_visible.astype(np.float32)
        blacks = np.array(raw.black_level_per_channel, dtype=np.float32)
        for dy in (0, 1):
            for dx in (0, 1):
                data[dy::2, dx::2] -= blacks[colours[dy, dx]]
        span = float(raw.white_level) - float(blacks.mean())
        data *= np.float32(1.0 / max(span, 1.0))

        wb = _normalised_wb(raw.daylight_whitebalance)
        as_shot = _normalised_wb(raw.camera_whitebalance)
        if wb is None:
            wb = as_shot if as_shot is not None else np.ones(3, dtype=np.float32)
        if as_shot is None:
            as_shot = wb
        matrix = cam_to_srgb_matrix(raw.rgb_xyz_matrix)
    return Mosaic(data, pattern, wb, as_shot, matrix)


def mosaic_luminance(data, out_dtype=np.float32):
    """2x2 binned mosaic: a half-size monochrome image, good for finding stars."""
    h, w = (data.shape[0] // 2) * 2, (data.shape[1] // 2) * 2
    d = data[:h, :w]
    return ((d[0::2, 0::2] + d[0::2, 1::2] + d[1::2, 0::2] + d[1::2, 1::2]) * 0.25).astype(out_dtype)


def _channel_mask(shape, pattern, channel):
    mask = np.zeros(shape, dtype=bool)
    for dy in (0, 1):
        for dx in (0, 1):
            if pattern[dy][dx] == channel:
                mask[dy::2, dx::2] = True
    return mask


_BILINEAR = np.array([[0.25, 0.5, 0.25], [0.5, 1.0, 0.5], [0.25, 0.5, 0.25]], dtype=np.float32)


def debayer(data, pattern, method="bilinear", strip_rows=512):
    """Camera RGB from a Bayer mosaic.

    superpixel: each 2x2 cell becomes one pixel (half size, no interpolation at all).
    bilinear: full size, each missing colour averaged from its neighbours.
    """
    h, w = data.shape
    if method == "superpixel":
        h2, w2 = h // 2, w // 2
        out = np.zeros((h2, w2, 3), dtype=np.float32)
        counts = np.zeros(3, dtype=np.float32)
        for dy in (0, 1):
            for dx in (0, 1):
                c = pattern[dy][dx]
                out[..., c] += data[dy:2 * h2:2, dx:2 * w2:2]
                counts[c] += 1
        out /= counts
        return out

    out = np.empty((h, w, 3), dtype=np.float32)
    # Strips (with a 2-row margin, kept even so the pattern lines up) bound the memory
    strip_rows -= strip_rows % 2
    for start in range(0, h, strip_rows):
        lo = max(0, start - 2)
        hi = min(h, start + strip_rows + 2)
        chunk = data[lo:hi]
        offset_y = lo % 2
        local_pattern = [[pattern[(dy + offset_y) % 2][dx] for dx in (0, 1)] for dy in (0, 1)]
        for c in range(3):
            mask = _channel_mask(chunk.shape, local_pattern, c)
            known = np.where(mask, chunk, 0).astype(np.float32)
            num = ndimage.convolve(known, _BILINEAR, mode="mirror")
            den = ndimage.convolve(mask.astype(np.float32), _BILINEAR, mode="mirror")
            channel = num / np.maximum(den, 1e-6)
            channel[mask] = chunk[mask]
            out[start:min(h, start + strip_rows), :, c] = channel[start - lo:start - lo + min(strip_rows, h - start)]
    return out


def apply_colour(rgb, wb=None, matrix=None, strip_rows=256):
    """White balance then the camera-to-sRGB matrix, in place, a strip at a time."""
    wb = None if wb is None else np.asarray(wb, dtype=np.float32)
    mt = None if matrix is None else np.asarray(matrix, dtype=np.float32).T
    for start in range(0, rgb.shape[0], strip_rows):
        strip = rgb[start:start + strip_rows]
        if wb is not None:
            strip *= wb
        if mt is not None:
            strip[...] = strip @ mt
    return rgb


def read_jpeg(path, linear=True, reduce=1):
    with Image.open(path) as img:
        if reduce > 1:
            img.draft("RGB", (img.width // reduce, img.height // reduce))
        img = img.convert("RGB")
        arr = np.asarray(img)
    if linear:
        return SRGB_TO_LINEAR_LUT[arr]
    return arr.astype(np.float32) / 255.0


def read_jpeg_luminance(path, reduce=2, linear=True):
    """Fast luminance of a JPEG: the decoder only does the luma channel, at 1/reduce size."""
    with Image.open(path) as img:
        if reduce > 1:
            img.draft("L", (img.width // reduce, img.height // reduce))
        img = img.convert("L")
        arr = np.asarray(img)
    if linear:
        return SRGB_TO_LINEAR_LUT[arr]
    return arr.astype(np.float32) / 255.0


def read_frame_rgb(path, debayer_method="bilinear", colour="camera", linear_jpeg=True):
    """Any still frame as a float32 (H, W, 3) linear RGB image (for single images that
    need no calibration)."""
    kind = kind_of(path)
    if kind == "raw":
        mosaic = read_raw(path)
        rgb = debayer(mosaic.data, mosaic.pattern, debayer_method)
        if colour != "none":
            apply_colour(rgb, mosaic.wb, mosaic.cam_to_srgb if colour == "camera" else None)
        return rgb
    if kind == "jpeg":
        return read_jpeg(path, linear=linear_jpeg)
    raise ValueError(f"Can't read {os.path.basename(path)} as an image")


def embedded_preview(path):
    """A RAW file's embedded JPEG preview as a PIL image, or None."""
    try:
        import io
        import rawpy
        with rawpy.imread(path) as raw:
            thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            return Image.open(io.BytesIO(thumb.data))
        return Image.fromarray(thumb.data)
    except Exception:
        return None
