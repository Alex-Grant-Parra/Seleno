"""Stacking: calibrate, measure, align and combine light frames into one linear image.

  1. Master bias, dark, dark flat and flat frames, each combined with rejection.
  2. Every light calibrated (RAW: on the Bayer mosaic, where it's exact):
       light - dark (or - bias), / normalised flat, hot pixels fixed.
  3. A first pass finds and measures each light's stars; the sharpest, roundest frame
     becomes the reference and every frame is matched to it. Frames that won't match,
     or are clearly worse than the rest, are left out (and say why).
  4. A second pass debayers, colours and warps each frame onto the reference and
     stores it on disk (float16), so memory stays bounded whatever the frame count.
  5. Strip by strip, the frames are normalised and combined with rejection.

The result is a 32-bit linear FITS plus a small preview for the editor.
"""
import json
import math
import os
import shutil
import time

import numpy as np
from scipy import ndimage

from . import develop, fitsio, frames, integrate, register, stars

FRAME_TYPES = ("light", "dark", "flat", "darkflat", "bias")

DEFAULT_SETTINGS = {
    "debayer": "bilinear",          # bilinear (full size) | superpixel (half size)
    "colour": "camera",             # camera (white balance + colour matrix) | wb (white balance only) | none
    "wb": "daylight",               # daylight | as_shot
    "calibration_method": "winsorized",
    "dark_scaling": True,           # scale darks with the bias when exposures differ
    "hot_pixels": True,             # fix hot pixels found in the master dark
    "cosmetic": True,               # fix stray single hot pixels in each light
    "reference": "auto",            # auto | a light's name
    "transform": "similarity",      # similarity | affine
    "star_sigma": 5.0,
    "interpolation": "bicubic",     # bilinear | bicubic
    "method": "winsorized",
    "kappa_low": 3.0,
    "kappa_high": 3.0,
    "iterations": 3,
    "normalise": True,
    "weighting": "noise",           # noise | none
    "reject_auto": True,            # leave out frames far worse than the night's typical
    "reject_fwhm": None,            # px, or None
    "reject_ecc": None,
    "reject_stars": None,
    "crop_common": True,
    "keep_masters": False,
}

PREVIEW_SIDE = 1600


class StackError(Exception):
    pass


class DiskStack:
    """N frames of (C, H, W) float16 in one file, written and read with plain file I/O
    (not a memory map), so the frames never sit in the worker's memory."""

    def __init__(self, path, n, channels, height, width):
        self.path, self.n, self.c, self.h, self.w = path, n, channels, height, width
        self.row_bytes = width * 2
        self.plane_bytes = height * self.row_bytes
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        os.ftruncate(self.fd, n * channels * self.plane_bytes)

    def _offset(self, k, c, row):
        return (k * self.c + c) * self.plane_bytes + row * self.row_bytes

    def write_rows(self, k, c, row, data):
        os.pwrite(self.fd, np.ascontiguousarray(data, dtype=np.float16).tobytes(), self._offset(k, c, row))

    def read_strip(self, c, row, rows):
        rows = min(rows, self.h - row)
        out = np.empty((self.n, rows, self.w), dtype=np.float32)
        size = rows * self.row_bytes
        for k in range(self.n):
            buf = os.pread(self.fd, size, self._offset(k, c, row))
            out[k] = np.frombuffer(buf, dtype=np.float16).reshape(rows, self.w)
        return out

    def close(self, remove=True):
        os.close(self.fd)
        if remove and os.path.exists(self.path):
            os.remove(self.path)


def clean_settings(settings):
    s = dict(DEFAULT_SETTINGS)
    for key, value in (settings or {}).items():
        if key in s:
            s[key] = value
    s["debayer"] = s["debayer"] if s["debayer"] in ("bilinear", "superpixel") else "bilinear"
    s["colour"] = s["colour"] if s["colour"] in ("camera", "wb", "none") else "camera"
    s["wb"] = s["wb"] if s["wb"] in ("daylight", "as_shot") else "daylight"
    for key in ("calibration_method", "method"):
        s[key] = s[key] if s[key] in integrate.METHODS else "winsorized"
    s["transform"] = s["transform"] if s["transform"] in ("similarity", "affine") else "similarity"
    s["interpolation"] = s["interpolation"] if s["interpolation"] in ("bilinear", "bicubic") else "bicubic"
    s["weighting"] = s["weighting"] if s["weighting"] in ("noise", "none") else "noise"
    for key, lo, hi, default in (("star_sigma", 2.0, 50.0, 5.0), ("kappa_low", 0.5, 10.0, 3.0),
                                 ("kappa_high", 0.5, 10.0, 3.0), ("iterations", 1, 10, 3)):
        try:
            s[key] = min(max(float(s[key]), lo), hi)
        except (TypeError, ValueError):
            s[key] = default
    s["iterations"] = int(s["iterations"])
    for key in ("reject_fwhm", "reject_ecc", "reject_stars"):
        try:
            s[key] = float(s[key]) if s[key] not in (None, "") else None
        except (TypeError, ValueError):
            s[key] = None
    for key in ("dark_scaling", "hot_pixels", "cosmetic", "normalise", "reject_auto", "crop_common", "keep_masters"):
        s[key] = bool(s[key])
    return s


# --- loading ----------------------------------------------------------------

class Loaded:
    """A frame ready for calibration: data is the mosaic (H, W) for RAW, RGB (H, W, 3)
    for JPEG."""

    def __init__(self, data, mosaic=None):
        self.data = data
        self.mosaic = mosaic

    @property
    def is_raw(self):
        return self.mosaic is not None


def load(path):
    kind = frames.kind_of(path)
    if kind == "raw":
        m = frames.read_raw(path)
        return Loaded(m.data, m)
    if kind == "jpeg":
        return Loaded(frames.read_jpeg(path, linear=True))
    raise StackError(f"{os.path.basename(path)} isn't a photo that can be stacked")


# --- masters ----------------------------------------------------------------

def _combine_to_master(entries, tmp_dir, label, method, budget, progress, subtract=None, normalise=False,
                       pattern=None, per_channel=True):
    """Combine frames of one type into a master (float32). Flats are divided by their
    own level (per Bayer colour, or per RGB channel) before combining."""
    if not entries:
        return None, {}
    first = load(entries[0]["path"])
    shape = first.data.shape
    n = len(entries)
    path = os.path.join(tmp_dir, f"{label}.f16")
    rows_total = shape[0]
    row_len = int(np.prod(shape[1:]))
    disk = DiskStack(path, n, 1, rows_total, row_len)
    exposures = []
    for i, entry in enumerate(entries):
        progress.sub(i / n, f"Reading {label} frames ({i + 1} of {n})")
        frame = first if i == 0 else load(entry["path"])
        data = frame.data
        if data.shape != shape:
            raise StackError(f"{entry['name']} is a different size from the other {label} frames")
        data = data.astype(np.float32, copy=True)
        if subtract is not None:
            data -= subtract
        if normalise:
            data = _normalise_flat(data, pattern if first.is_raw else None, per_channel)
        disk.write_rows(i, 0, 0, data.reshape(rows_total, row_len))
        exposures.append(entry.get("exposure_s"))
        del frame, data
    progress.sub(1.0, f"Combining {n} {label} frames")
    master = np.empty(shape, dtype=np.float32)
    rows = integrate.strip_rows_for(n, row_len, budget)
    out2d = master.reshape(rows_total, row_len)
    for start in range(0, rows_total, rows):
        strip = disk.read_strip(0, start, rows)
        res, *_ = integrate.combine(strip, method if n >= 3 else "average")
        out2d[start:start + rows] = res
    disk.close()
    known = [e for e in exposures if e]
    info = {"frames": n, "exposure_s": float(np.median(known)) if known else None}
    return master, info


def _normalise_flat(data, pattern, per_channel):
    if pattern is not None and per_channel:
        for dy in (0, 1):
            for dx in (0, 1):
                plane = data[dy::2, dx::2]
                level = float(np.median(plane[::4, ::4]))
                if level > 0:
                    plane /= level
        return data
    if data.ndim == 3 and per_channel:
        for c in range(data.shape[2]):
            level = float(np.median(data[::4, ::4, c]))
            if level > 0:
                data[..., c] /= level
        return data
    level = float(np.median(data[::4, ::4]))
    return data / level if level > 0 else data


def hot_pixel_map(master_dark, pattern, k=8.0):
    """Pixels far brighter than the rest of the master dark (by Bayer colour)."""
    hot = np.zeros(master_dark.shape[:2], dtype=bool)
    if master_dark.ndim == 3:
        lum = master_dark.mean(axis=2)
        med, sd = stars.robust_stats(lum[::3, ::3])
        return lum > med + k * max(sd, 1e-7)
    for dy in (0, 1):
        for dx in (0, 1):
            plane = master_dark[dy::2, dx::2]
            med, sd = stars.robust_stats(plane[::3, ::3])
            hot[dy::2, dx::2] = plane > med + k * max(sd, 1e-7)
    return hot


def fix_pixels(data, mask, bayer):
    """Replace masked pixels with the mean of their same-colour neighbours."""
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return data
    step = 2 if bayer else 1
    h, w = data.shape[:2]
    total = np.zeros((ys.size,) + data.shape[2:], dtype=np.float32)
    count = np.zeros(ys.size, dtype=np.float32)
    for dy, dx in ((-step, 0), (step, 0), (0, -step), (0, step)):
        yy, xx = ys + dy, xs + dx
        ok = (yy >= 0) & (yy < h) & (xx >= 0) & (xx < w)
        ok[ok] &= ~mask[yy[ok], xx[ok]]
        total[ok] += data[yy[ok], xx[ok]]
        count[ok] += 1
    have = count > 0
    shape = (-1,) + (1,) * (data.ndim - 2)
    data[ys[have], xs[have]] = total[have] / count[have].reshape(shape)
    return data


_RING = np.ones((3, 3), dtype=bool)
_RING[1, 1] = False


def cosmetic_mask(mosaic, noise, k=10.0, strip_rows=512):
    """Single hot pixels: far above the sky while every neighbour (any colour) stays near
    it. Stars, even tight ones, light up their neighbours too, so they're left alone."""
    h, w = mosaic.shape
    mask = np.zeros((h, w), dtype=bool)
    for start in range(0, h, strip_rows):
        lo, hi = max(0, start - 1), min(h, start + strip_rows + 1)
        chunk = mosaic[lo:hi]
        local = ndimage.median_filter(chunk, footprint=_RING, mode="mirror")
        nb_max = ndimage.maximum_filter(chunk, footprint=_RING, mode="mirror")
        excess = chunk - local
        hit = (excess > k * noise) & ((nb_max - local) < 0.15 * excess)
        mask[start:min(h, start + strip_rows)] = hit[start - lo:start - lo + min(strip_rows, h - start)]
    return mask


class Calibrator:
    def __init__(self, bias=None, dark=None, dark_exposure=None, flat=None, hot=None, pattern=None,
                 dark_scaling=True, cosmetic=True):
        self.bias, self.dark, self.dark_exposure = bias, dark, dark_exposure
        self.flat, self.hot, self.pattern = flat, hot, pattern
        self.dark_scaling, self.cosmetic = dark_scaling, cosmetic
        if flat is not None:
            self.flat = np.maximum(flat, 0.05)

    def __call__(self, loaded, exposure=None):
        data = loaded.data.astype(np.float32, copy=True)
        if self.dark is not None:
            if (self.dark_scaling and self.bias is not None and exposure and self.dark_exposure
                    and abs(exposure - self.dark_exposure) / self.dark_exposure > 0.02):
                data -= self.bias
                data -= (self.dark - self.bias) * np.float32(exposure / self.dark_exposure)
            else:
                data -= self.dark
        elif self.bias is not None:
            data -= self.bias
        if self.flat is not None:
            data /= self.flat
        bayer = loaded.is_raw
        if self.hot is not None:
            data = fix_pixels(data, self.hot, bayer)
        if self.cosmetic and bayer:
            _, noise = stars.robust_stats(data[::7, ::7])
            data = fix_pixels(data, cosmetic_mask(data, max(noise, 1e-6)), bayer)
        return data


# --- measuring and matching ------------------------------------------------------

def luminance_half(data, is_raw):
    """A half-size monochrome image for finding stars: 2x2 binned mosaic, or binned RGB."""
    if is_raw:
        return frames.mosaic_luminance(data)
    lum = data @ frames.LUMA
    h, w = (lum.shape[0] // 2) * 2, (lum.shape[1] // 2) * 2
    return lum[:h, :w].reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3)).astype(np.float32)


def measure(lum, star_sigma):
    peak = float(lum.max()) if lum.size else 0
    sat = 0.97 * peak if peak > 0 else None
    st = stars.detect_stars(lum, k=star_sigma, max_stars=800, saturation=sat)
    summary = stars.summarise(st, pixel_scale=2.0)
    return st, summary


def pick_reference(measured, wanted="auto"):
    names = [m["name"] for m in measured]
    if wanted and wanted != "auto" and wanted in names:
        return names.index(wanted)
    counts = np.array([m["metrics"]["stars"] for m in measured], dtype=float)
    fwhm = np.array([m["metrics"]["fwhm"] or np.inf for m in measured], dtype=float)
    ecc = np.array([m["metrics"]["ecc"] if m["metrics"]["ecc"] is not None else 1 for m in measured], dtype=float)
    ok = counts >= 0.7 * np.median(counts)
    ok &= ecc <= np.median(ecc) + 0.1
    if not ok.any():
        ok = counts == counts.max()
    score = np.where(ok, fwhm, np.inf)
    return int(np.argmin(score))


def quality_rejections(measured, settings):
    """{index: reason} for frames to leave out on quality."""
    out = {}
    metrics = [m["metrics"] for m in measured]
    fwhm = np.array([m["fwhm"] or np.nan for m in metrics])
    ecc = np.array([m["ecc"] if m["ecc"] is not None else np.nan for m in metrics])
    count = np.array([m["stars"] for m in metrics], dtype=float)
    if settings["reject_auto"] and len(measured) >= 5:
        f_med, f_sd = stars.robust_stats(fwhm)
        e_med, e_sd = stars.robust_stats(ecc)
        c_med = float(np.median(count))
        for i in range(len(measured)):
            if np.isfinite(fwhm[i]) and fwhm[i] > f_med + max(3 * f_sd, 0.15 * f_med):
                out[i] = f"stars blurred (FWHM {fwhm[i]:.2f} px, typical {f_med:.2f})"
            elif np.isfinite(ecc[i]) and ecc[i] > max(0.6, e_med + 3 * e_sd):
                out[i] = f"stars stretched (eccentricity {ecc[i]:.2f})"
            elif count[i] < 0.5 * c_med:
                out[i] = f"few stars ({int(count[i])}, typical {int(c_med)}): cloud or haze?"
    for i in range(len(measured)):
        if i in out:
            continue
        if settings["reject_fwhm"] and np.isfinite(fwhm[i]) and fwhm[i] > settings["reject_fwhm"]:
            out[i] = f"FWHM {fwhm[i]:.2f} px over the {settings['reject_fwhm']:g} px limit"
        elif settings["reject_ecc"] and np.isfinite(ecc[i]) and ecc[i] > settings["reject_ecc"]:
            out[i] = f"eccentricity {ecc[i]:.2f} over the {settings['reject_ecc']:g} limit"
        elif settings["reject_stars"] and count[i] < settings["reject_stars"]:
            out[i] = f"{int(count[i])} stars, under the {int(settings['reject_stars'])} limit"
    return out


# --- colour and warping -----------------------------------------------------------

def to_rgb(data, loaded, settings):
    if not loaded.is_raw:
        return data
    rgb = frames.debayer(data, loaded.mosaic.pattern, settings["debayer"])
    if settings["colour"] != "none":
        wb = loaded.mosaic.wb if settings["wb"] == "daylight" else loaded.mosaic.wb_as_shot
        matrix = loaded.mosaic.cam_to_srgb if settings["colour"] == "camera" else None
        frames.apply_colour(rgb, wb, matrix)
    return rgb


def output_transform(m_half, settings, is_raw):
    """The half-size star transform for the output image's pixels."""
    if is_raw and settings["debayer"] == "superpixel":
        return np.asarray(m_half, dtype=np.float64)
    return register.scale_transform(m_half, 2.0, 0.5)


def warp_channel(channel, m, out_shape, order):
    matrix, offset = register.to_ndimage(m)
    if order > 1:
        coeffs = ndimage.spline_filter(channel, order=order, output=np.float32, mode="mirror")
        out = ndimage.affine_transform(coeffs, matrix, offset=offset, output_shape=out_shape, order=order,
                                       mode="constant", cval=np.nan, prefilter=False, output=np.float32)
    else:
        out = ndimage.affine_transform(channel, matrix, offset=offset, output_shape=out_shape, order=1,
                                       mode="constant", cval=np.nan, output=np.float32)
    return out


def common_area(coverage, needed):
    """The box (fractions) of rows and columns fully covered by `needed` frames."""
    full = coverage >= needed
    h, w = full.shape
    if not full.any():
        return {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}
    rows = np.nonzero(full.mean(axis=1) >= 0.995)[0]
    cols = np.nonzero(full.mean(axis=0) >= 0.995)[0]
    if rows.size == 0 or cols.size == 0:
        rows, cols = np.nonzero(full.any(axis=1))[0], np.nonzero(full.any(axis=0))[0]
    return {"x0": float(cols[0] / w), "y0": float(rows[0] / h), "x1": float((cols[-1] + 1) / w), "y1": float((rows[-1] + 1) / h)}


def _downsample_chw(arr, max_side, rows_per_read=256):
    """(C, H, W) on disk to a small (h, w, C) float32 by block averaging."""
    c, h, w = arr.shape
    factor = int(max(1, math.ceil(max(h, w) / max_side)))
    oh, ow = h // factor, w // factor
    out = np.zeros((oh, ow, c), dtype=np.float32)
    step = max(factor, (rows_per_read // factor) * factor)
    for start in range(0, oh * factor, step):
        end = min(start + step, oh * factor)
        block = np.array(arr[:, start:end, :ow * factor], dtype=np.float32)
        block = block.reshape(c, (end - start) // factor, factor, ow, factor)
        with np.errstate(invalid="ignore"):
            out[start // factor:end // factor] = np.nanmean(block, axis=(2, 4)).transpose(1, 2, 0)
    return np.nan_to_num(out, nan=0.0)


def write_outputs(result_chw, coverage, out_dir, header, crop, planetary=False):
    """master.fits, preview.npy, auto.jpg and thumb.jpg from a (3, H, W) linear result."""
    from PIL import Image

    fitsio.write_fits(os.path.join(out_dir, "master.fits"), result_chw, header)
    preview = _downsample_chw(result_chw, PREVIEW_SIDE)
    np.save(os.path.join(out_dir, "preview.npy"), preview.astype(np.float16))
    if coverage is not None:
        cov = develop.downsample(coverage.astype(np.float32), PREVIEW_SIDE // 4)
        np.save(os.path.join(out_dir, "coverage.npy"), cov.astype(np.float16))
    params = develop.auto_params(planetary)
    if crop:
        params["crop"] = crop
    resolved = develop.resolve(preview, params, planetary=planetary)
    jpeg = develop.preview_jpeg(preview, resolved, quality=90)
    with open(os.path.join(out_dir, "auto.jpg"), "wb") as f:
        f.write(jpeg)
    import io
    thumb = Image.open(io.BytesIO(jpeg))
    thumb.thumbnail((480, 480), Image.LANCZOS)
    thumb.save(os.path.join(out_dir, "thumb.jpg"), "JPEG", quality=85)
    return params


# --- the job --------------------------------------------------------------------

def check_disk(path, needed_bytes):
    free = shutil.disk_usage(path).free
    margin = 1.5 * 1024 ** 3
    if free < needed_bytes + margin:
        raise StackError(
            f"Not enough disk space: stacking needs about {needed_bytes / 1024 ** 3:.1f} GB of temporary space and "
            f"{free / 1024 ** 3:.1f} GB is free (keeping 1.5 GB spare). Try half-size (super-pixel) debayering or fewer frames.")


def run(spec, progress):
    settings = clean_settings(spec.get("settings"))
    groups = {t: list(spec.get("frames", {}).get(t) or []) for t in FRAME_TYPES}
    lights = groups["light"]
    if not lights:
        raise StackError("No light frames to stack")
    out_dir, tmp_dir = spec["output_dir"], spec["tmp_dir"]
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    budget = int(spec.get("memory_mb", 900)) * 1024 * 1024
    warnings = []
    t_start = time.time()

    kinds = {frames.kind_of(e["path"]) for e in lights}
    if len(kinds) != 1 or None in kinds or "video" in kinds:
        raise StackError("Light frames must all be RAW files or all JPEGs")
    light_kind = kinds.pop()
    for t in ("dark", "flat", "darkflat", "bias"):
        mismatched = [e for e in groups[t] if frames.kind_of(e["path"]) != light_kind]
        if mismatched:
            warnings.append(f"Left out {len(mismatched)} {t} frame(s) that aren't {light_kind.upper()} like the lights")
            groups[t] = [e for e in groups[t] if frames.kind_of(e["path"]) == light_kind]
    if light_kind == "jpeg" and any(groups[t] for t in ("dark", "flat", "bias", "darkflat")):
        warnings.append("Calibrating JPEGs is approximate: the camera's tone curve isn't linear. RAW files calibrate exactly.")

    # Masters
    probe = load(lights[0]["path"])
    pattern = probe.mosaic.pattern if probe.is_raw else None
    shape = probe.data.shape
    del probe
    method = settings["calibration_method"]
    stage_weights = {"masters": 0.15, "measure": 0.25, "warp": 0.4, "combine": 0.2}
    n_cal = sum(len(groups[t]) for t in ("bias", "dark", "darkflat", "flat"))

    progress.stage("masters", stage_weights["masters"] if n_cal else 0.0)
    done = 0

    def sub_progress(count):
        start = done / max(n_cal, 1)
        span = count / max(n_cal, 1)
        return progress.child(start, span)

    masters_info = {}
    bias, info = _combine_to_master(groups["bias"], tmp_dir, "bias", method, budget, sub_progress(len(groups["bias"])))
    done += len(groups["bias"])
    if bias is not None:
        masters_info["bias"] = info
    dark, info = _combine_to_master(groups["dark"], tmp_dir, "dark", method, budget, sub_progress(len(groups["dark"])))
    done += len(groups["dark"])
    if dark is not None:
        masters_info["dark"] = info
    darkflat, info = _combine_to_master(groups["darkflat"], tmp_dir, "darkflat", method, budget, sub_progress(len(groups["darkflat"])))
    done += len(groups["darkflat"])
    if darkflat is not None:
        masters_info["darkflat"] = info
    flat_sub = darkflat if darkflat is not None else bias
    flat, info = _combine_to_master(groups["flat"], tmp_dir, "flat", method, budget, sub_progress(len(groups["flat"])),
                                    subtract=flat_sub, normalise=True, pattern=pattern)
    if flat is not None:
        masters_info["flat"] = info
        if flat_sub is None:
            warnings.append("Flats were used without bias or dark flats; add bias frames for exact flat correction")
    for name, master in (("bias", bias), ("dark", dark), ("darkflat", darkflat), ("flat", flat)):
        if master is not None and master.shape != shape:
            raise StackError(f"The master {name} is a different size from the light frames")
    hot = hot_pixel_map(dark, pattern) if (dark is not None and settings["hot_pixels"]) else None
    if hot is not None:
        masters_info["hot_pixels"] = int(hot.sum())
    if settings["keep_masters"]:
        for name, master in (("bias", bias), ("dark", dark), ("flat", flat), ("darkflat", darkflat)):
            if master is not None:
                data = master if master.ndim == 2 else np.moveaxis(master, 2, 0)
                fitsio.write_fits(os.path.join(out_dir, f"master_{name}.fits"), data, {"IMAGETYP": f"Master {name}"})

    calibrate = Calibrator(bias=bias, dark=dark, dark_exposure=(masters_info.get("dark") or {}).get("exposure_s"),
                           flat=flat, hot=hot, pattern=pattern, dark_scaling=settings["dark_scaling"],
                           cosmetic=settings["cosmetic"])

    # Pass 1: measure every light
    progress.stage("measure", stage_weights["measure"])
    measured = []
    for i, entry in enumerate(lights):
        progress.sub(i / len(lights), f"Measuring stars ({i + 1} of {len(lights)})")
        try:
            loaded = load(entry["path"])
            if loaded.data.shape != shape:
                raise StackError("a different size from the other lights")
            data = calibrate(loaded, entry.get("exposure_s"))
            lum = luminance_half(data, loaded.is_raw)
            st, summary = measure(lum, settings["star_sigma"])
            measured.append({"name": entry["name"], "entry": entry, "metrics": summary,
                             "xy": np.c_[st["x"], st["y"]][:300]})
        except Exception as e:      # one bad file shouldn't sink the stack
            measured.append({"name": entry["name"], "entry": entry, "metrics": None, "error": str(e)})

    usable = [m for m in measured if m.get("metrics")]
    if not usable:
        raise StackError("None of the light frames could be read")
    frame_report = {m["name"]: {"name": m["name"], "accepted": False, "metrics": m.get("metrics"),
                                "reason": m.get("error")} for m in measured}

    single = len(usable) == 1
    ref_idx = pick_reference(usable, settings["reference"])
    ref = usable[ref_idx]
    if not single and ref["metrics"]["stars"] < 8:
        raise StackError(f"Only {ref['metrics']['stars']} stars found in the best frame, too few to align. "
                         "For the Moon or a planet, use lucky imaging instead.")
    rejections = quality_rejections(usable, settings) if not single else {}
    rejections.pop(ref_idx, None)

    accepted = []
    for i, m in enumerate(usable):
        report = frame_report[m["name"]]
        if i in rejections:
            report["reason"] = rejections[i]
            continue
        if i == ref_idx:
            m_half = np.array([[1.0, 0, 0], [0, 1.0, 0]])
            report.update(matched=len(m["xy"]), rms=0.0)
        else:
            try:
                m_half, matched, rms = register.match_stars(ref["xy"], m["xy"], transform=settings["transform"])
            except register.RegistrationError as e:
                report["reason"] = f"couldn't align: {e}"
                continue
            report.update(matched=matched, rms=round(rms * 2, 3))
        report["shift_px"] = [round(float(m_half[0, 2]) * 2, 1), round(float(m_half[1, 2]) * 2, 1)]
        report["rotation_deg"] = round(register.transform_rotation_deg(m_half), 3)
        accepted.append((m, m_half))
    if not accepted:
        raise StackError("No frames could be aligned to the reference")

    # Output size
    is_raw = light_kind == "raw"
    if is_raw and settings["debayer"] == "superpixel":
        out_shape = (shape[0] // 2, shape[1] // 2)
    else:
        out_shape = (shape[0], shape[1])
    n = len(accepted)
    frame_bytes = 3 * out_shape[0] * out_shape[1] * 2
    check_disk(tmp_dir, n * frame_bytes + 3 * out_shape[0] * out_shape[1] * 4 * 2)

    # Pass 2: calibrate, debayer, colour, warp, store
    progress.stage("warp", stage_weights["warp"])
    order = 3 if settings["interpolation"] == "bicubic" else 1
    aligned = DiskStack(os.path.join(tmp_dir, "aligned.f16"), n, 3, out_shape[0], out_shape[1])
    locs = np.zeros((n, 3))
    scales = np.ones((n, 3))
    noise = np.ones(n)
    for k, (m, m_half) in enumerate(accepted):
        progress.sub(k / n, f"Aligning frames ({k + 1} of {n})")
        loaded = load(m["entry"]["path"])
        data = calibrate(loaded, m["entry"].get("exposure_s"))
        rgb = to_rgb(data, loaded, settings)
        del data, loaded
        mt = output_transform(m_half, settings, is_raw)
        identity = np.allclose(mt, [[1, 0, 0], [0, 1, 0]])
        for c in range(3):
            channel = np.ascontiguousarray(rgb[..., c])
            warped = channel if identity else warp_channel(channel, mt, out_shape, order)
            sample = warped[::5, ::5]
            sample = sample[np.isfinite(sample)]
            med, sd = stars.robust_stats(sample) if sample.size else (0.0, 1.0)
            locs[k, c], scales[k, c] = med, max(sd, 1e-7)
            for start in range(0, out_shape[0], 512):
                aligned.write_rows(k, c, start, warped[start:start + 512])
            del channel, warped
        noise[k] = max(m["metrics"]["noise"], 1e-7)
        del rgb

    # Combine
    progress.stage("combine", stage_weights["combine"])
    ref_k = next(k for k, (m, _) in enumerate(accepted) if m is ref) if any(m is ref for m, _ in accepted) else 0
    result = np.memmap(os.path.join(tmp_dir, "result.f32"), dtype=np.float32, mode="w+", shape=(3,) + out_shape)
    coverage = np.zeros(out_shape, dtype=np.uint16)
    weights = None
    if settings["weighting"] == "noise" and n >= 3:
        k_green = scales[ref_k, 1] / scales[:, 1] if settings["normalise"] else np.ones(n)
        weights = 1.0 / (noise * k_green) ** 2
        weights = weights / weights.max()
    rejected = [0, 0]
    rows = integrate.strip_rows_for(n, out_shape[1], budget)
    method = settings["method"] if n >= 3 else "average"
    steps = 3 * math.ceil(out_shape[0] / rows)
    step = 0
    for c in range(3):
        for start in range(0, out_shape[0], rows):
            progress.sub(step / steps, f"Combining {n} frames")
            step += 1
            strip = aligned.read_strip(c, start, rows)
            if settings["normalise"] and n > 1:
                integrate.normalise_strip(strip, locs[:, c], scales[:, c], locs[ref_k, c], scales[ref_k, c])
            res, cov, rl, rh = integrate.combine(strip, method, settings["kappa_low"], settings["kappa_high"],
                                                 settings["iterations"], weights)
            result[c, start:start + rows] = res
            if c == 1:
                coverage[start:start + rows] = cov
            rejected[0] += int(rl.sum())
            rejected[1] += int(rh.sum())
            del strip
    result.flush()
    aligned.close()

    for m, _ in accepted:
        frame_report[m["name"]]["accepted"] = True
        frame_report[m["name"]]["reason"] = None
    crop = common_area(coverage, n) if (settings["crop_common"] and n > 1) else None

    total_exposure = sum(float(m["entry"].get("exposure_s") or 0) for m, _ in accepted)
    header = {
        "IMAGETYP": "Master light",
        "NCOMBINE": (n, "frames combined"),
        "EXPTIME": (total_exposure, "total exposure, s") if total_exposure else None,
        "OBJECT": spec.get("target") or None,
        "CREATOR": "Seleno",
        "DATE": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "REFFRAME": ref["name"][:60],
        "REJECT": method,
    }
    progress.stage("output", 0.0)
    progress.sub(0.0, "Writing the result")
    auto = write_outputs(result, coverage, out_dir, header, crop)
    pixels = out_shape[0] * out_shape[1] * 3
    stats = {
        "seconds": round(time.time() - t_start, 1),
        "rejected_low_pct": round(100.0 * rejected[0] / max(pixels * n, 1), 3),
        "rejected_high_pct": round(100.0 * rejected[1] / max(pixels * n, 1), 3),
    }
    del result
    os.remove(os.path.join(tmp_dir, "result.f32"))
    return {
        "kind": "stack" if not single else "single",
        "width": out_shape[1],
        "height": out_shape[0],
        "frames_used": n,
        "frames_total": len(lights),
        "total_exposure_s": total_exposure,
        "reference": ref["name"],
        "frames": list(frame_report.values()),
        "masters": masters_info,
        "settings": settings,
        "crop": crop,
        "auto_params": auto,
        "stats": stats,
        "warnings": warnings,
        "linear": True,
    }
