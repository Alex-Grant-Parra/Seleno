"""Reference outputs from app/processing (Python) for the JS engine's tests.

    python tests/processing/oracle.py <out dir> [<extra sys.path entry, e.g. a pylib with scipy>]

Writes small synthetic inputs and what the Python code makes of them: arrays as
little-endian float32 files, everything else in manifest.json. tests/processing/
core.test.mjs reads them. Takes a few seconds.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1]
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
if len(sys.argv) > 2:
    sys.path.insert(0, sys.argv[2])

import numpy as np                       # noqa: E402
from scipy import ndimage                # noqa: E402

from app.processing import develop, frames, integrate, register, stars   # noqa: E402
from synth import render, star_catalogue, transformed                     # noqa: E402

os.makedirs(OUT, exist_ok=True)
manifest = {}


def save(name, arr):
    arr = np.ascontiguousarray(arr, dtype="<f4")
    arr.tofile(os.path.join(OUT, name + ".f32"))
    return {"file": name + ".f32", "shape": list(arr.shape)}


def star_dict(st, n=None):
    keys = ("x", "y", "flux", "peak", "snr", "hfr", "fwhm", "ecc")
    out = {k: [float(v) for v in st[k][:n]] for k in keys}
    out["saturated"] = [bool(v) for v in st["saturated"][:n]]
    out["count"] = int(st["x"].size)
    out["background"] = float(st["background"])
    out["noise"] = float(st["noise"])
    return out


W, H = 480, 320
x, y, f = star_catalogue(n=160, w=W, h=H, seed=3)

# Basics
img = render(x, y, f, W, H, fwhm=3.0, sky=0.05, gradient=0.02, noise=0.004, seed=1)
manifest["basics"] = {
    "img": save("basics_img", img),
    "robust": list(stars.robust_stats(img[::3, ::3])),
    "background": save("basics_bg", stars.background_map(img)),
    "gauss": save("basics_gauss", ndimage.gaussian_filter(img, 1.0)),
    "maxf": save("basics_maxf", ndimage.maximum_filter(img, size=5)),
}

# Stars at several sizes and shapes
manifest["stars"] = []
for i, (fwhm, ratio) in enumerate([(2.0, 1.0), (3.0, 1.0), (5.0, 1.0), (3.0, 1.6)]):
    im = render(x, y, f, W, H, fwhm=fwhm, ecc_axis_ratio=ratio, angle=0.4, hot=10, saturate=0.9, seed=10 + i)
    st = stars.detect_stars(im, saturation=0.89)
    manifest["stars"].append({
        "img": save(f"stars_{i}", im), "fwhm": fwhm, "ratio": ratio,
        "result": star_dict(st), "summary": stars.summarise(st),
    })

# Registration: shift and rotation, and a meridian flip
ref = render(x, y, f, W, H, seed=2)
X, Y = transformed(x, y, 17.3, -9.8, 1.7, W / 2, H / 2)
tgt = render(X, Y, f, W, H, seed=3)
sr, stg = stars.detect_stars(ref), stars.detect_stars(tgt)
m, n, rms = register.match_stars(np.c_[sr["x"], sr["y"]], np.c_[stg["x"], stg["y"]])
X2, Y2 = transformed(x, y, 5, 3, 180, W / 2, H / 2)
tgt2 = render(X2, Y2, f, W, H, seed=4)
s2 = stars.detect_stars(tgt2)
m2, n2, rms2 = register.match_stars(np.c_[sr["x"], sr["y"]], np.c_[s2["x"], s2["y"]])
manifest["register"] = {
    "ref_xy": np.c_[sr["x"], sr["y"]].tolist(),
    "tgt_xy": np.c_[stg["x"], stg["y"]].tolist(),
    "flip_xy": np.c_[s2["x"], s2["y"]].tolist(),
    "m": m.tolist(), "matched": n, "rms": rms,
    "m_flip": m2.tolist(),
    "truth": {"dx": 17.3, "dy": -9.8, "rot": 1.7, "cx": W / 2, "cy": H / 2},
    "scaled": register.scale_transform(m, 2.0, 0.5).tolist(),
}
pc_ref = render(x, y, f, 128, 96, fwhm=4, seed=5)
pc_img = ndimage.shift(pc_ref, (2.3, -3.6), order=3, mode="nearest")
dy, dx, strength = register.phase_correlate(pc_ref, pc_img)
manifest["phase"] = {"ref": save("phase_ref", pc_ref), "img": save("phase_img", pc_img), "dy": dy, "dx": dx}

# Integration with a trail, missing data and outliers
rng = np.random.default_rng(5)
N = 12
base = (np.full((N, 40, 60), 0.1, np.float32) + rng.normal(0, 0.01, (N, 40, 60))).astype(np.float32)
base[3, 20, :] += 0.5
base[5, :6, :20] = np.nan
base[7, 30:, 40:] -= 0.3
weights = rng.uniform(0.5, 1.0, N)
manifest["integrate"] = {"stack": save("int_stack", base), "weights": weights.tolist(), "results": {}}
for method in integrate.METHODS:
    for w in (None, weights):
        res, cov, rl, rh = integrate.combine(base, method, 3.0, 3.0, 3, w)
        key = f"{method}_{'w' if w is not None else 'plain'}"
        manifest["integrate"]["results"][key] = {
            "result": save(f"int_{key}", res), "rej_low": int(rl.sum()), "rej_high": int(rh.sum()),
            "coverage_min": int(cov.min()), "coverage_sum": int(cov.sum()),
        }

# Debayering a small mosaic
mos = rng.uniform(0, 1, (12, 16)).astype(np.float32)
pattern = [[1, 2], [0, 1]]
manifest["debayer"] = {
    "mosaic": save("deb_mosaic", mos), "pattern": pattern,
    "bilinear": save("deb_bilinear", frames.debayer(mos, pattern, "bilinear", strip_rows=4)),
    "superpixel": save("deb_super", frames.debayer(mos, pattern, "superpixel")),
    "luminance": save("deb_lum", frames.mosaic_luminance(mos)),
}
cam_xyz = np.array([[4716, 603, -830], [-7798, 15474, 2480], [-1496, 1937, 6651]]) / 10000.0
manifest["colour"] = {"cam_xyz": cam_xyz.tolist(), "cam_to_srgb": frames.cam_to_srgb_matrix(cam_xyz).tolist()}

# Developing: a small linear colour image with a gradient, nebula and stars
yy, xx = np.mgrid[0:H, 0:W]
rgb = np.stack([render(x, y, f * k, W, H, fwhm=2.5, sky=s, gradient=g, noise=0.002, seed=20 + c)
                for c, (k, s, g) in enumerate([(1.2, 0.03, 0.01), (1.0, 0.04, 0.015), (0.8, 0.05, 0.005)])], -1)
rgb += (0.02 * np.exp(-(((xx - 200) / 60) ** 2 + ((yy - 150) / 40) ** 2)))[..., None] * np.array([1.0, 0.4, 0.6])
rgb = rgb.astype(np.float32)
manifest["develop"] = {"img": save("dev_img", rgb), "cases": []}
cases = {
    "auto": develop.auto_params(),
    "manual": develop.clean_params({"stretch": {"mode": "manual", "black": 0.03, "mid": 0.2, "white": 0.9},
                                    "colour": {"neutralise": False, "white": "average"}, "contrast": -0.2}),
    "asinh": develop.clean_params({"stretch": {"mode": "arcsinh", "asinh": 50}, "background": {"enabled": True, "degree": 1, "mode": "divide"},
                                   "colour": {"white": "manual", "manual": [1.1, 1.0, 0.9]}}),
    "none": develop.clean_params({"stretch": {"mode": "none"}, "colour": {"white": "none", "neutralise": False}, "sharpen": {"layers": [1.5, 1.2, 1, 1]}}),
}
for name, params in cases.items():
    resolved = develop.resolve(rgb, params)
    out = develop.apply(rgb, resolved)
    r = {k: v for k, v in resolved.items() if k not in ("params", "background_model")}
    if resolved.get("background_model"):
        r["background_model"] = {k: v for k, v in resolved["background_model"].items() if k != "samples"}
    manifest["develop"]["cases"].append({"name": name, "params": params, "resolved": json.loads(json.dumps(r, default=float)),
                                         "out": save(f"dev_{name}", out)})
layers, residual = develop.wavelet_layers(rgb[..., 1], 4)
manifest["develop"]["wavelet0"] = save("dev_wav0", layers[0])
manifest["develop"]["wavelet_res"] = save("dev_wavres", residual)

with open(os.path.join(OUT, "manifest.json"), "w") as fh:
    json.dump(manifest, fh)
print(f"oracle written to {OUT}")
