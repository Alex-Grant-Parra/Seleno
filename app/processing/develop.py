"""Developing a linear image (a stack, or one calibrated frame) into a picture.

The steps, in order:
  crop          a box in fractions of the image
  background    a smooth polynomial fitted to the sky and taken away (gradients,
                light pollution) or divided out (vignetting)
  colour        background made neutral grey, then a white balance: from the stars,
                from the image as a whole, or set by hand
  stretch       linear data to display: an automatic screen-transfer stretch, a manual
                black / midtones / white, or arcsinh (keeps star colour)
  contrast, saturation
  noise         colour noise (chroma blur) and detail noise (wavelet thresholding)
  sharpen       wavelet layers, fine to coarse

`resolve()` works out the numbers that depend on the image (background model, colour
factors, stretch levels) from the small preview; `apply()` uses those numbers at any
size, so a preview, a 100% crop and the full-size export all match.
"""
import copy
import math

import numpy as np
from scipy import ndimage

from .frames import LUMA
from .stars import detect_stars, robust_stats

DEFAULTS = {
    "crop": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
    "background": {"enabled": False, "degree": 2, "grid": 16, "mode": "subtract"},
    "colour": {"neutralise": True, "white": "stars", "manual": [1.0, 1.0, 1.0]},
    "stretch": {"mode": "auto", "linked": True, "target": 0.20, "shadows": -2.8,
                "black": 0.0, "mid": 0.5, "white": 1.0, "asinh": 30.0},
    "contrast": 0.0,
    "saturation": 1.0,
    "noise": {"chroma": 0.0, "luma": 0.0},
    "sharpen": {"layers": [1.0, 1.0, 1.0, 1.0]},
}

# A sensible starting point: what Auto sets
AUTO = {
    "background": {"enabled": True, "degree": 2},
    "colour": {"neutralise": True, "white": "stars"},
    "stretch": {"mode": "auto", "linked": True, "target": 0.20, "shadows": -2.8},
    "contrast": 0.15,
    "saturation": 1.3,
    "noise": {"chroma": 0.5, "luma": 0.2},
    "sharpen": {"layers": [1.0, 1.0, 1.0, 1.0]},
}

# Planetary and lunar results are already display-referred: no stretch, more sharpening
AUTO_PLANETARY = {
    "background": {"enabled": False},
    "colour": {"neutralise": False, "white": "average"},
    "stretch": {"mode": "none"},
    "contrast": 0.1,
    "saturation": 1.1,
    "noise": {"chroma": 0.2, "luma": 0.0},
    "sharpen": {"layers": [1.6, 1.4, 1.15, 1.0]},
}

LIMITS = {
    ("background", "degree"): (0, 4),
    ("background", "grid"): (6, 40),
    ("stretch", "target"): (0.02, 0.6),
    ("stretch", "shadows"): (-8.0, 2.0),
    ("stretch", "black"): (0.0, 0.99),
    ("stretch", "mid"): (0.001, 0.999),
    ("stretch", "white"): (0.01, 1.0),
    ("stretch", "asinh"): (1.0, 1000.0),
    ("contrast",): (-0.9, 0.9),
    ("saturation",): (0.0, 3.0),
    ("noise", "chroma"): (0.0, 1.0),
    ("noise", "luma"): (0.0, 1.0),
}


def _deep_merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if key not in out:
            continue
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _clamp(value, lo, hi, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, lo), hi)


def clean_params(params, planetary=False):
    """Defaults filled in and every number clamped to a sane range. Unknown keys dropped."""
    p = _deep_merge(DEFAULTS, params or {})
    if planetary and not params:
        p = _deep_merge(p, AUTO_PLANETARY)
    for path, (lo, hi) in LIMITS.items():
        node = p
        for key in path[:-1]:
            node = node[key]
        default = DEFAULTS
        for key in path:
            default = default[key]
        node[path[-1]] = _clamp(node[path[-1]], lo, hi, default)
    p["background"]["degree"] = int(p["background"]["degree"])
    p["background"]["grid"] = int(p["background"]["grid"])
    p["background"]["enabled"] = bool(p["background"]["enabled"])
    if p["background"]["mode"] not in ("subtract", "divide"):
        p["background"]["mode"] = "subtract"
    if p["colour"]["white"] not in ("stars", "average", "manual", "none"):
        p["colour"]["white"] = "stars"
    p["colour"]["neutralise"] = bool(p["colour"]["neutralise"])
    manual = p["colour"].get("manual") or [1, 1, 1]
    p["colour"]["manual"] = [_clamp(v, 0.1, 10.0, 1.0) for v in (list(manual) + [1, 1, 1])[:3]]
    if p["stretch"]["mode"] not in ("auto", "manual", "arcsinh", "none"):
        p["stretch"]["mode"] = "auto"
    p["stretch"]["linked"] = bool(p["stretch"]["linked"])
    layers = p["sharpen"].get("layers") or [1, 1, 1, 1]
    p["sharpen"]["layers"] = [_clamp(v, 0.0, 4.0, 1.0) for v in (list(layers) + [1, 1, 1, 1])[:4]]
    c = p["crop"]
    x0, x1 = sorted((_clamp(c.get("x0"), 0, 1, 0.0), _clamp(c.get("x1"), 0, 1, 1.0)))
    y0, y1 = sorted((_clamp(c.get("y0"), 0, 1, 0.0), _clamp(c.get("y1"), 0, 1, 1.0)))
    if x1 - x0 < 0.02:
        x0, x1 = 0.0, 1.0
    if y1 - y0 < 0.02:
        y0, y1 = 0.0, 1.0
    p["crop"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    return p


def auto_params(planetary=False):
    return clean_params(_deep_merge(DEFAULTS, AUTO_PLANETARY if planetary else AUTO))


def crop_box(height, width, crop):
    y0 = int(round(crop["y0"] * height))
    y1 = max(y0 + 1, int(round(crop["y1"] * height)))
    x0 = int(round(crop["x0"] * width))
    x1 = max(x0 + 1, int(round(crop["x1"] * width)))
    return y0, y1, x0, x1


# --- background ------------------------------------------------------------

def _poly_terms(degree):
    return [(i, j) for total in range(degree + 1) for i in range(total + 1) for j in [total - i]]


def _design(u, v, degree):
    return np.stack([(u ** i) * (v ** j) for i, j in _poly_terms(degree)], axis=-1)


def fit_background(img, degree=2, grid=16):
    """Per-channel polynomial over the image, in coordinates u, v from -1 to 1 across
    the (cropped) image. Sample boxes that hold stars or nebula are rejected by
    iterating the fit and dropping boxes well above it."""
    h, w, channels = img.shape
    nx = grid
    ny = max(4, int(round(grid * h / w)))
    bh, bw = h / ny, w / nx
    samples = np.zeros((ny, nx, channels), dtype=np.float64)
    for i in range(ny):
        for j in range(nx):
            box = img[int(i * bh):int((i + 1) * bh), int(j * bw):int((j + 1) * bw)]
            if box.size == 0:
                continue
            samples[i, j] = np.nanmedian(box.reshape(-1, channels)[::3], axis=0)
    vv, uu = np.meshgrid((np.arange(ny) + 0.5) / ny * 2 - 1, (np.arange(nx) + 0.5) / nx * 2 - 1, indexing="ij")
    u, v = uu.ravel(), vv.ravel()
    s = samples.reshape(-1, channels)
    ok = np.all(np.isfinite(s), axis=1)
    lum = s @ LUMA[:channels] / LUMA[:channels].sum() if channels == 3 else s[:, 0]

    # Start from the darker half: nebulae and galaxy cores are bright
    ok &= lum <= np.nanpercentile(lum[ok], 75) if ok.any() else ok
    degree = int(max(0, min(degree, 4)))
    coeffs = None
    for _ in range(6):
        if ok.sum() < len(_poly_terms(degree)) + 2:
            degree = max(0, degree - 1)
            if ok.sum() < len(_poly_terms(degree)) + 1:
                break
        a = _design(u[ok], v[ok], degree)
        coeffs, *_ = np.linalg.lstsq(a, s[ok], rcond=None)
        model = _design(u, v, degree) @ coeffs
        model_l = model @ LUMA[:channels] / LUMA[:channels].sum() if channels == 3 else model[:, 0]
        resid = lum - model_l
        _, sigma = robust_stats(resid[ok])
        sigma = max(sigma, 1e-6)
        new_ok = np.all(np.isfinite(s), axis=1) & (resid < 2.0 * sigma) & (resid > -4.0 * sigma)
        if np.array_equal(new_ok, ok):
            break
        ok = new_ok
    if coeffs is None:
        coeffs = np.zeros((1, channels))
        coeffs[0] = np.nanmedian(s, axis=0)
        degree = 0
    model = _design(u, v, degree) @ coeffs
    levels = np.median(model, axis=0)
    return {
        "degree": degree,
        "coeffs": np.asarray(coeffs).T.tolist(),      # channels x terms
        "levels": levels.tolist(),                    # each channel's typical sky level
        "samples": [[float(uu.ravel()[k]), float(vv.ravel()[k]), bool(ok[k])] for k in range(len(u))],
    }


def eval_background(model, y_coords, x_coords):
    """The model at pixel rows y_coords and columns x_coords, both already in -1..1."""
    degree = model["degree"]
    coeffs = np.asarray(model["coeffs"], dtype=np.float64)
    vv, uu = np.meshgrid(y_coords, x_coords, indexing="ij")
    terms = _design(uu.astype(np.float64), vv.astype(np.float64), degree)
    return (terms @ coeffs.T).astype(np.float32)


def _norm_coords(start, count, total):
    return ((np.arange(start, start + count) + 0.5) / total * 2.0 - 1.0).astype(np.float64)


# --- colour ---------------------------------------------------------------

def star_white_balance(img, background):
    """Factors making the median star white, from the colour of unsaturated stars
    measured in apertures. None if too few stars."""
    lum = img @ LUMA
    stars = detect_stars(lum, k=8.0, max_stars=400)
    keep = (~stars["saturated"]) & (stars["snr"] > 15)
    xs, ys = stars["x"][keep], stars["y"][keep]
    if xs.size < 12:
        return None, 0
    h, w, _ = img.shape
    r = 4
    off = np.arange(-r, r + 1)
    oy, ox = np.meshgrid(off, off, indexing="ij")
    disk = np.hypot(oy, ox) <= r - 1
    ring = np.hypot(oy, ox) > r - 0.5
    iy = np.clip(np.round(ys).astype(int), r, h - r - 1)
    ix = np.clip(np.round(xs).astype(int), r, w - r - 1)
    win = img[iy[:, None, None] + oy[None], ix[:, None, None] + ox[None]]     # N, S, S, 3
    if np.nanmax(win) >= 0.98 * np.nanmax(img):
        peak_ok = win.reshape(len(ix), -1, 3).max(axis=1).max(axis=1) < 0.95 * np.nanmax(img)
    else:
        peak_ok = np.ones(len(ix), dtype=bool)
    sky = np.median(win[:, ring], axis=1)                                     # N, 3
    flux = (win[:, disk] - sky[:, None, :]).sum(axis=1)                        # N, 3
    good = peak_ok & np.all(flux > 0, axis=1)
    if good.sum() < 12:
        return None, int(good.sum())
    flux = flux[good]
    g = flux[:, 1]
    factors = np.array([np.median(g / flux[:, 0]), 1.0, np.median(g / flux[:, 2])])
    if not np.all(np.isfinite(factors)) or np.any(factors <= 0):
        return None, int(good.sum())
    return np.clip(factors, 0.2, 5.0), int(good.sum())


def average_white_balance(img, background):
    """Grey-world on the parts of the image clearly above the sky."""
    sig = img - np.asarray(background, dtype=np.float32)
    lum = sig @ LUMA
    _, noise = robust_stats(lum[::4, ::4])
    mask = lum > 5 * max(noise, 1e-6)
    if mask.sum() < 50:
        mask = lum > np.percentile(lum, 90)
    sums = np.array([np.clip(sig[..., c][mask], 0, None).sum() for c in range(3)])
    if np.any(sums <= 0):
        return np.ones(3)
    return np.clip(sums[1] / sums, 0.2, 5.0)


# --- stretch --------------------------------------------------------------

def mtf(m, x):
    """The midtones transfer function: 0 -> 0, m -> 0.5, 1 -> 1."""
    x = np.asarray(x, dtype=np.float32)
    m = np.float32(m)
    with np.errstate(divide="ignore", invalid="ignore"):
        y = ((m - 1) * x) / ((2 * m - 1) * x - m)
    return np.where(x <= 0, 0.0, np.where(x >= 1, 1.0, y)).astype(np.float32)


def auto_stretch_levels(img, target=0.2, shadows=-2.8, linked=True):
    """Black and midtones per channel so the sky median lands at `target`."""
    channels = img.shape[-1]
    sub = img[::4, ::4].reshape(-1, channels)
    stats = [robust_stats(sub[:, c]) for c in range(channels)]
    if linked:
        med = float(np.mean([s[0] for s in stats]))
        mad = float(np.mean([s[1] for s in stats])) / 1.4826
        stats = [(med, mad * 1.4826)] * channels
    blacks, mids = [], []
    for med, sd in stats:
        mad = sd / 1.4826
        black = min(max(med + shadows * mad, 0.0), 0.99)
        x = (med - black) / max(1.0 - black, 1e-6)
        x = min(max(x, 1e-6), 0.999)
        mid = float(mtf(target, x))
        blacks.append(black)
        mids.append(min(max(mid, 1e-4), 0.9999))
    return blacks, mids


def _contrast_curve(x, amount):
    if not amount:
        return x
    return np.clip(x - amount * np.sin(2 * np.pi * x) / (2 * np.pi), 0, 1)


# --- detail ---------------------------------------------------------------

_B3 = np.array([1, 4, 6, 4, 1], dtype=np.float32) / 16.0


def wavelet_layers(img2d, n_layers=4):
    """Starlet (a trous B3 spline) decomposition: detail layers fine to coarse, plus the
    smooth residual. Layers plus residual add back up to the image."""
    layers = []
    current = img2d.astype(np.float32)
    for j in range(n_layers):
        step = 2 ** j
        kernel = np.zeros(4 * step + 1, dtype=np.float32)
        kernel[::step] = _B3
        smooth = ndimage.convolve1d(current, kernel, axis=0, mode="mirror")
        smooth = ndimage.convolve1d(smooth, kernel, axis=1, mode="mirror")
        layers.append(current - smooth)
        current = smooth
    return layers, current


def _luma(rgb):
    return rgb @ LUMA


# --- resolve and apply ----------------------------------------------------

def resolve(img, params, planetary=False):
    """The image-dependent numbers for `params`, measured on `img` (the preview, already
    linear float32 HxWx3). Returns a dict for apply()."""
    p = clean_params(params, planetary=planetary)
    h, w, _ = img.shape
    y0, y1, x0, x1 = crop_box(h, w, p["crop"])
    work = np.ascontiguousarray(img[y0:y1, x0:x1], dtype=np.float32)
    work = np.nan_to_num(work, nan=0.0)
    resolved = {"params": p, "planetary": planetary}

    bg = p["background"]
    model = None
    if bg["enabled"]:
        model = fit_background(work, bg["degree"], bg["grid"])
        resolved["background_model"] = model
        work = _apply_background(work, model, bg["mode"], 0, 0, work.shape[0], work.shape[1], p["colour"]["neutralise"])

    # Sky level per channel after the background step
    sub = work[::4, ::4].reshape(-1, 3)
    sky = np.array([robust_stats(sub[:, c])[0] for c in range(3)], dtype=np.float32)
    col = p["colour"]
    offsets = np.zeros(3, dtype=np.float32)
    if col["neutralise"]:
        target = float(sky.mean())
        offsets = (target - sky).astype(np.float32)
        sky = np.full(3, target, dtype=np.float32)
    factors = np.ones(3)
    stars_used = 0
    if col["white"] == "stars":
        f, stars_used = star_white_balance(work + offsets, sky)
        factors = f if f is not None else average_white_balance(work + offsets, sky)
        resolved["white_source"] = "stars" if f is not None else "average"
    elif col["white"] == "average":
        factors = average_white_balance(work + offsets, sky)
        resolved["white_source"] = "average"
    elif col["white"] == "manual":
        factors = np.asarray(col["manual"], dtype=np.float64)
        resolved["white_source"] = "manual"
    factors = np.asarray(factors, dtype=np.float64)
    factors = factors / factors[1]
    resolved.update(offsets=offsets.tolist(), sky=sky.tolist(), factors=factors.tolist(), stars_used=stars_used)
    work = _apply_colour(work, offsets, sky, factors)

    st = p["stretch"]
    if st["mode"] == "auto":
        blacks, mids = auto_stretch_levels(work, st["target"], st["shadows"], st["linked"])
        resolved["levels"] = {"black": blacks, "mid": mids, "white": [1.0] * 3}
    elif st["mode"] == "manual":
        resolved["levels"] = {"black": [st["black"]] * 3, "mid": [st["mid"]] * 3, "white": [st["white"]] * 3}
    elif st["mode"] == "arcsinh":
        lum = _luma(work)
        med, sd = robust_stats(lum[::4, ::4])
        black = max(med + st["shadows"] * sd / 1.4826, 0.0)
        hi = float(np.percentile(lum[::4, ::4], 99.95))
        resolved["asinh"] = {"black": black, "beta": st["asinh"], "white": max(hi, black + 1e-4)}
    else:
        peak = 1.0 if planetary else float(np.percentile(np.clip(work[::4, ::4], 0, None), 99.99))
        resolved["peak"] = max(peak, 1.0)
    return resolved


def _apply_background(img, model, mode, row0, col0, crop_h, crop_w, neutral):
    rows, cols = img.shape[0], img.shape[1]
    bgm = eval_background(model, _norm_coords(row0, rows, crop_h), _norm_coords(col0, cols, crop_w))
    levels = np.asarray(model["levels"], dtype=np.float32)
    if neutral:
        levels = np.full(3, float(levels.mean()), dtype=np.float32)
    if mode == "divide":
        return (img / np.maximum(bgm / np.maximum(np.asarray(model["levels"], np.float32), 1e-6), 1e-3)).astype(np.float32)
    return (img - bgm + levels).astype(np.float32)


def _apply_colour(img, offsets, sky, factors):
    offsets = np.asarray(offsets, dtype=np.float32)
    sky = np.asarray(sky, dtype=np.float32)
    factors = np.asarray(factors, dtype=np.float32)
    # Shift each channel's sky to the common level, then scale the signal above it
    return ((img + offsets - sky) * factors + sky).astype(np.float32)


def apply(img, resolved, scale=1.0, region=None):
    """Develop `img` (linear HxWx3 float32) with resolved numbers. `scale` is the size
    of this image relative to full size (for blur radii). `region` (row0, col0, crop_h,
    crop_w) places a piece of the cropped image when only part of it is given; without
    it, `img` is the whole uncropped image and the crop is applied here."""
    p = resolved["params"]
    img = np.nan_to_num(np.asarray(img, dtype=np.float32), nan=0.0)
    if region is None:
        h, w, _ = img.shape
        y0, y1, x0, x1 = crop_box(h, w, p["crop"])
        img = img[y0:y1, x0:x1]
        region = (0, 0, img.shape[0], img.shape[1])
    row0, col0, crop_h, crop_w = region

    if resolved.get("background_model"):
        img = _apply_background(img, resolved["background_model"], p["background"]["mode"], row0, col0, crop_h, crop_w,
                                p["colour"]["neutralise"])
    img = _apply_colour(img, resolved["offsets"], resolved["sky"], resolved["factors"])

    st = p["stretch"]
    if st["mode"] in ("auto", "manual") and resolved.get("levels"):
        lv = resolved["levels"]
        out = np.empty_like(img)
        for c in range(3):
            black, mid, white = lv["black"][c], lv["mid"][c], lv["white"][c]
            x = (img[..., c] - black) / max(white - black, 1e-6)
            out[..., c] = mtf(mid, np.clip(x, 0, 1))
        img = out
    elif st["mode"] == "arcsinh" and resolved.get("asinh"):
        a = resolved["asinh"]
        lum = _luma(img)
        x = np.clip((lum - a["black"]) / max(a["white"] - a["black"], 1e-6), 0, None)
        beta = a["beta"]
        y = np.arcsinh(beta * x) / np.arcsinh(beta)
        with np.errstate(divide="ignore", invalid="ignore"):
            k = np.where(lum - a["black"] > 1e-9, y / np.maximum(lum - a["black"], 1e-9), 0.0)
        img = np.clip((img - a["black"]) * k[..., None], 0, None)
        # Colour kept, brightness capped without shifting hue
        peak = img.max(axis=-1, keepdims=True)
        img = np.where(peak > 1, img / np.maximum(peak, 1e-9), img)
    else:
        img = np.clip(img, 0, None) / np.float32(resolved.get("peak", 1.0))

    img = np.clip(img, 0, 1)
    if p["contrast"]:
        img = _contrast_curve(img, p["contrast"])

    lum = _luma(img)
    if p["saturation"] != 1.0:
        img = lum[..., None] + (img - lum[..., None]) * p["saturation"]

    nz = p["noise"]
    if nz["chroma"] > 0:
        chroma = img - lum[..., None]
        sigma = max(0.5, 4.0 * nz["chroma"] * scale ** 0.5)
        blurred = np.stack([ndimage.gaussian_filter(chroma[..., c], sigma) for c in range(3)], axis=-1)
        img = lum[..., None] + blurred
    gains = p["sharpen"]["layers"]
    if nz["luma"] > 0 or any(abs(g - 1.0) > 1e-3 for g in gains):
        lum = _luma(img)
        layers, residual = wavelet_layers(lum, 4)
        new = residual.copy()
        for j, layer in enumerate(layers):
            if nz["luma"] > 0 and j < 2:
                _, sd = robust_stats(layer[::3, ::3])
                thr = nz["luma"] * (3.0 if j == 0 else 1.5) * sd
                layer = np.sign(layer) * np.maximum(np.abs(layer) - thr, 0)
            new += layer * gains[j]
        img = img + (new - lum)[..., None]
    return np.clip(img, 0, 1).astype(np.float32)


def apply_strips(read_rows, height, width, resolved, write_rows, strip_rows=512, margin=64):
    """Develop a big image a strip at a time: read_rows(r0, r1) gives cropped linear
    rows, write_rows(r0, r1, rgb) takes developed ones. The margin keeps blurs and
    sharpening seamless across strips."""
    for start in range(0, height, strip_rows):
        lo = max(0, start - margin)
        hi = min(height, start + strip_rows + margin)
        rows = read_rows(lo, hi)
        out = apply(rows, resolved, scale=1.0, region=(lo, 0, height, width))
        a = start - lo
        b = a + min(strip_rows, height - start)
        write_rows(start, start + (b - a), out[a:b])


def to_uint8(rgb):
    return (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def to_uint16(rgb):
    return (np.clip(rgb, 0, 1) * 65535 + 0.5).astype(np.uint16)


def preview_jpeg(img, resolved=None, params=None, max_side=None, quality=90, planetary=False):
    """JPEG bytes of a developed linear image (resolving params on it if needed)."""
    import io
    from PIL import Image

    if resolved is None:
        resolved = resolve(img, params or auto_params(planetary), planetary=planetary)
    out = apply(img, resolved, scale=1.0)
    pil = Image.fromarray(to_uint8(out))
    if max_side and max(pil.size) > max_side:
        pil.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def downsample(img, max_side):
    """A smaller copy by block averaging (no aliasing), then a final resize."""
    h, w = img.shape[:2]
    factor = int(max(1, math.floor(max(h, w) / max_side)))
    if factor > 1:
        hh, ww = (h // factor) * factor, (w // factor) * factor
        shape = (hh // factor, factor, ww // factor, factor) + img.shape[2:]
        img = np.nanmean(img[:hh, :ww].reshape(shape), axis=(1, 3))
    return np.asarray(img, dtype=np.float32)
