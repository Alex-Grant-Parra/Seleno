"""Synthetic star fields for testing the processing code."""
import numpy as np

def star_catalogue(n=400, w=1200, h=800, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.uniform(-100, w + 100, n); y = rng.uniform(-100, h + 100, n)
    flux = 10 ** rng.uniform(1.5, 4.0, n)   # total flux in arbitrary units
    return x, y, flux

def render(x, y, flux, w, h, fwhm=3.0, ecc_axis_ratio=1.0, angle=0.0, sky=0.05, gradient=0.0,
           noise=0.004, seed=1, saturate=None, hot=0):
    rng = np.random.default_rng(seed)
    img = np.full((h, w), sky, np.float64)
    if gradient:
        img += gradient * (np.arange(w)[None, :] / w)
    s = fwhm / 2.3548
    sx, sy = s * np.sqrt(ecc_axis_ratio), s / np.sqrt(ecc_axis_ratio)
    ca, sa = np.cos(angle), np.sin(angle)
    r = int(np.ceil(5 * max(sx, sy)))
    for xi, yi, fi in zip(x, y, flux):
        x0, y0 = int(round(xi)), int(round(yi))
        if x0 < -r or y0 < -r or x0 >= w + r or y0 >= h + r: continue
        ys = np.arange(max(0, y0 - r), min(h, y0 + r + 1)); xs = np.arange(max(0, x0 - r), min(w, x0 + r + 1))
        if not len(ys) or not len(xs): continue
        X, Y = np.meshgrid(xs - xi, ys - yi)
        u = ca * X + sa * Y; v = -sa * X + ca * Y
        g = np.exp(-0.5 * (u * u / (sx * sx) + v * v / (sy * sy)))
        img[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1] += fi * 1e-4 * g / (2 * np.pi * sx * sy)
    img += rng.normal(0, noise, img.shape)
    if hot:
        hy = rng.integers(0, h, hot); hx = rng.integers(0, w, hot); img[hy, hx] += 0.5
    if saturate is not None:
        img = np.minimum(img, saturate)
    return img.astype(np.float32)

def transformed(x, y, dx, dy, rot_deg, cx, cy):
    t = np.radians(rot_deg); c, s = np.cos(t), np.sin(t)
    X = c * (x - cx) - s * (y - cy) + cx + dx
    Y = s * (x - cx) + c * (y - cy) + cy + dy
    return X, Y
