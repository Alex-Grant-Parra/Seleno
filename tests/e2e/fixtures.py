"""Synthetic test data for the end-to-end tests: star fields (as JPEGs, and as an H.264
focus sweep for live view), a tiny TIFF-based RAW with an embedded preview, and a
small video. Nothing here comes from real photos."""
import io
import math
import os
import struct
import subprocess

import numpy as np
from PIL import Image


def star_field(width, height, seed=1, fwhm=2.6, ecc=0.0, angle_deg=30.0, stars=260, sky=0.10,
               gradient=0.04, noise=0.006, nebula=True, brightness=1.0, field=None, shift=(0.0, 0.0)):
    """A linear star field (H, W, 3) float32 in 0..1."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    img = np.full((height, width, 3), sky, dtype=np.float32)
    img += gradient * (xx / width)[..., None] * np.array([1.0, 0.9, 0.7], dtype=np.float32)
    if nebula:
        cx, cy = width * 0.55, height * 0.45
        r2 = ((xx - cx) / (width * 0.18)) ** 2 + ((yy - cy) / (height * 0.15)) ** 2
        glow = np.exp(-r2) * 0.08 * brightness
        img += glow[..., None] * np.array([1.0, 0.35, 0.55], dtype=np.float32)
    # The sky: the same stars in every frame of a field (field=...), dithered by shift
    rng2 = np.random.default_rng((seed if field is None else field) + 1000)
    xs = rng2.uniform(8, width - 8, stars) + shift[0]
    ys = rng2.uniform(8, height - 8, stars) + shift[1]
    flux = (rng2.pareto(1.6, stars) + 0.25) * 0.05 * brightness
    colours = rng2.uniform(0.8, 1.2, (stars, 3)).astype(np.float32)
    sigma = fwhm / 2.3548
    # Elongation along `angle` (tracking error or trailing)
    major = sigma / math.sqrt(max(1e-3, 1 - ecc ** 2)) if ecc else sigma
    theta = math.radians(angle_deg)
    c, s = math.cos(theta), math.sin(theta)
    r = int(max(4, 4 * major))
    for x0, y0, f, col in zip(xs, ys, flux, colours):
        ix, iy = int(x0), int(y0)
        x_lo, x_hi = max(0, ix - r), min(width, ix + r + 1)
        y_lo, y_hi = max(0, iy - r), min(height, iy + r + 1)
        dx = xx[y_lo:y_hi, x_lo:x_hi] - x0
        dy = yy[y_lo:y_hi, x_lo:x_hi] - y0
        u = dx * c + dy * s
        v = -dx * s + dy * c
        g = np.exp(-0.5 * ((u / major) ** 2 + (v / sigma) ** 2))
        img[y_lo:y_hi, x_lo:x_hi] += (f * g)[..., None] * col
    img += rng.normal(0, noise, img.shape).astype(np.float32)
    return np.clip(img, 0, 1)


def to_srgb8(linear):
    v = np.clip(linear, 0, 1)
    srgb = np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055)
    return (np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def jpeg_bytes(rgb8, exif=None, quality=90):
    img = Image.fromarray(rgb8)
    kwargs = {"quality": quality}
    if exif:
        e = Image.Exif()
        for key, value in exif.items():
            e[key] = value
        kwargs["exif"] = e.tobytes()
    buf = io.BytesIO()
    img.save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def fake_raw(preview_jpeg, model="Canon EOS 5D Mark II", date="2026:10:18 21:09:12", exposure=(30, 1), iso=800):
    """A tiny TIFF-based RAW: IFD0 holds the JPEG preview, plus an EXIF IFD."""
    model_b = model.encode() + b"\x00"
    date_b = date.encode() + b"\x00"
    ifd0_off = 8
    entries0 = 5
    exif_off = ifd0_off + 2 + entries0 * 12 + 4
    exif_entries = 3
    data_off = exif_off + 2 + exif_entries * 12 + 4
    model_off = data_off
    date_off = model_off + len(model_b)
    expo_off = date_off + len(date_b)
    jpeg_off = expo_off + 8
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd0_off))
    out += struct.pack("<H", entries0)
    out += struct.pack("<HHII", 259, 3, 1, 6)
    out += struct.pack("<HHII", 272, 2, len(model_b), model_off)
    out += struct.pack("<HHII", 273, 4, 1, jpeg_off)
    out += struct.pack("<HHII", 279, 4, 1, len(preview_jpeg))
    out += struct.pack("<HHII", 34665, 4, 1, exif_off)
    out += struct.pack("<I", 0)
    out += struct.pack("<H", exif_entries)
    out += struct.pack("<HHII", 33434, 5, 1, expo_off)
    out += struct.pack("<HHII", 34855, 3, 1, iso)
    out += struct.pack("<HHII", 36867, 2, len(date_b), date_off)
    out += struct.pack("<I", 0)
    out += model_b + date_b + struct.pack("<II", *exposure) + preview_jpeg
    return bytes(out)


def focus_sweep_h264(path, width=640, height=360, frames=80, fps=10):
    """H.264 of a star field going through focus: blurred, sharp, blurred again."""
    if os.path.exists(path):
        return path
    proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                             "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
                             "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1", "-bf", "0", "-g", "20",
                             "-pix_fmt", "yuv420p", "-x264-params", "aud=1:repeat-headers=1", "-f", "h264", path],
                            stdin=subprocess.PIPE)
    for i in range(frames):
        phase = i / (frames - 1)
        fwhm = 2.2 + 9.0 * abs(phase - 0.55) ** 1.2        # sharpest a little after half way
        field = star_field(width, height, seed=7, fwhm=fwhm, stars=60, sky=0.03, gradient=0.0,
                           noise=0.004, nebula=False, brightness=2.5)
        proc.stdin.write(to_srgb8(field).tobytes())
    proc.stdin.close()
    proc.wait()
    return path


def small_video(path):
    if os.path.exists(path):
        return path
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                    "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", path], check=True)
    return path


def ser_planet(path, frames=60, width=200, height=150, seed=7):
    """A SER video of a banded planet: 8-bit RGB, each frame blurred by a different
    amount of "seeing" and drifting a little (for lucky imaging)."""
    rng = np.random.default_rng(seed)
    header = bytearray(178)
    header[0:14] = b"LUCAM-RECORDER"
    struct.pack_into("<iiiiiii", header, 14, 0, 100, 0, width, height, 8, frames)
    out = [bytes(header)]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    for i in range(frames):
        cx, cy = width / 2 + rng.uniform(-4, 4), height / 2 + rng.uniform(-4, 4)
        r = np.hypot(xx - cx, yy - cy)
        disc = (r < 36).astype(np.float32) * (0.55 + 0.25 * np.sin((yy - cy) * 0.5))
        blur = 1 if i % 4 == 0 else int(rng.integers(3, 7))
        k = np.ones(blur, np.float32) / blur
        img = np.apply_along_axis(lambda v: np.convolve(v, k, "same"), 1, disc)
        img = np.apply_along_axis(lambda v: np.convolve(v, k, "same"), 0, img)
        img = np.clip(img + rng.normal(0, 0.01, img.shape), 0, 1)
        rgb = np.stack([img, img * 0.9, img * 0.75], -1)
        out.append((rgb * 255 + 0.5).astype(np.uint8).tobytes())
    with open(path, "wb") as f:
        f.write(b"".join(out))
    return path


def float16_preview(linear):
    """The editor's preview format: float16 RGB, little-endian, row-major."""
    return np.ascontiguousarray(linear.astype("<f2")).tobytes()
