import sys, time, numpy as np
sys.path.insert(0, '/home/alex/Server'); sys.path.insert(0, sys.argv[1])
from synth import *
from app.processing import stars, register, integrate

w, h = 1200, 800
x, y, f = star_catalogue()
for fwhm, ratio in [(2.0, 1.0), (3.0, 1.0), (5.0, 1.0), (3.0, 1.6)]:
    img = render(x, y, f, w, h, fwhm=fwhm, ecc_axis_ratio=ratio, angle=0.4, hot=30, saturate=0.9)
    t = time.time(); st = stars.detect_stars(img, saturation=0.89); dt = time.time() - t
    s = stars.summarise(st)
    true_ecc = np.sqrt(1 - 1 / ratio**2)
    print(f"fwhm {fwhm} ratio {ratio}: found {s['stars']} fwhm {s['fwhm']:.2f} hfr {s['hfr']:.2f} ecc {s['ecc']:.2f} (true {true_ecc:.2f}) bg {s['background']:.4f} noise {s['noise']:.4f} sat {s['saturated_stars']} {dt*1000:.0f} ms")

# Registration: known shift + rotation
ref = render(x, y, f, w, h, seed=2)
X, Y = transformed(x, y, 37.3, -21.8, 1.7, w / 2, h / 2)
tgt = render(X, Y, f, w, h, seed=3)
sr = stars.detect_stars(ref); stg = stars.detect_stars(tgt)
t = time.time(); m, n, rms = register.match_stars(np.c_[sr['x'], sr['y']], np.c_[stg['x'], stg['y']]); dt = time.time() - t
pts = np.array([[100, 100], [600, 400], [1100, 700]], float)
tx, ty = transformed(pts[:, 0], pts[:, 1], 37.3, -21.8, 1.7, w / 2, h / 2)
err = np.hypot(*(register.apply_transform(m, pts) - np.c_[tx, ty]).T)
print(f"register: matched {n} rms {rms:.3f}px  max error {err.max():.3f}px rot {register.transform_rotation_deg(m):.3f} scale {register.transform_scale(m):.5f} {dt*1000:.0f} ms")

# 180 degree rotation (meridian flip)
X2, Y2 = transformed(x, y, 5, 3, 180, w / 2, h / 2)
tgt2 = render(X2, Y2, f, w, h, seed=4); s2 = stars.detect_stars(tgt2)
m2, n2, rms2 = register.match_stars(np.c_[sr['x'], sr['y']], np.c_[s2['x'], s2['y']])
print(f"flip: matched {n2} rms {rms2:.3f} rot {register.transform_rotation_deg(m2):.2f}")

# Integration: sigma clipping removes a satellite trail, noise drops by ~sqrt(N)
rng = np.random.default_rng(5)
N = 16
base = np.full((N, 100, 300), 0.1, np.float32) + rng.normal(0, 0.01, (N, 100, 300)).astype(np.float32)
base[3, 50, :] += 0.5      # trail in one frame
base[5, :10, :40] = np.nan  # missing corner
for method in integrate.METHODS:
    t = time.time(); res, cov, rl, rh = integrate.combine(base, method); dt = time.time() - t
    print(f"{method:10s} trail row {res[50].mean():.4f}  noise {np.nanstd(res[60:]):.5f} (1 frame 0.01, ideal {0.01/np.sqrt(N):.5f})  cov min {cov.min()} rejected hi {rh.sum()} {dt*1000:.0f} ms")
