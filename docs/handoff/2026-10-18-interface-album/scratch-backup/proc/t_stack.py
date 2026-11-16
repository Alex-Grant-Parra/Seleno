"""End-to-end stack test on synthetic Bayer data and JPEGs."""
import json, os, shutil, sys, time, resource
import numpy as np
sys.path.insert(0, '/home/alex/Server'); sys.path.insert(0, os.path.dirname(__file__))
from synth import star_catalogue, render, transformed
from app.processing import frames, stack, fitsio, stars, develop
from app.processing.worker import Progress

OUT = sys.argv[1]
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)
W, H = 1200, 800
PATTERN = np.array([[1, 2], [0, 1]])        # G B / R G
rng = np.random.default_rng(7)
x, y, f = star_catalogue(n=900, w=W, h=H, seed=3)
colour = rng.uniform(0.6, 1.4, (len(x), 3)); colour[:, 1] = 1
yy, xx = np.mgrid[0:H, 0:W]
vignette = (1 - 0.35 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)).astype(np.float32)
hot_y, hot_x = rng.integers(0, H, 60), rng.integers(0, W, 60)
bias_pattern = rng.normal(0, 0.0004, (H, W)).astype(np.float32)
dark_current = np.full((H, W), 0.002, np.float32); dark_current[hot_y, hot_x] += rng.uniform(0.05, 0.3, 60)
nebula = 0.03 * np.exp(-(((xx - 640) / 140) ** 2 + ((yy - 380) / 90) ** 2))

def scene(dx, dy, rot, fwhm=3.0, trail=False, seed=0):
    X, Y = transformed(x, y, dx, dy, rot, W / 2, H / 2)
    chans = []
    for c in range(3):
        img = render(X, Y, f * 3 * colour[:, c], W, H, fwhm=fwhm, sky=0.0, noise=0.0, seed=seed)
        img += nebula.astype(np.float32) * (1.0, 0.5, 0.7)[c] + 0.04 + 0.02 * xx / W   # sky + gradient
        if trail:
            img[np.abs((yy - 0.4 * xx) - 150) < 1.5] += 0.4
        chans.append(img)
    return np.stack(chans, -1)

def mosaic_of(rgb):
    m = np.zeros(rgb.shape[:2], np.float32)
    for dy in (0, 1):
        for dx in (0, 1):
            m[dy::2, dx::2] = rgb[dy::2, dx::2, PATTERN[dy, dx]]
    return m

def raw_frame(kind, i, **kw):
    noise = 0.003
    if kind == 'bias':
        m = bias_pattern + rng.normal(0, noise / 2, (H, W))
    elif kind == 'dark':
        m = bias_pattern + dark_current + rng.normal(0, noise, (H, W))
    elif kind == 'flat':
        m = bias_pattern + 0.45 * vignette * (1, 1.1, 0.9)[0] + rng.normal(0, 0.004, (H, W))
    else:
        m = mosaic_of(scene(**kw)) * vignette + bias_pattern + dark_current + rng.normal(0, noise, (H, W))
    return np.clip(m, -0.01, 1.0).astype(np.float32)

# Fake RAW files: .dng named, holding a numpy mosaic
real_read_raw = frames.read_raw
def fake_read_raw(path):
    data = np.load(path, allow_pickle=False)
    return frames.Mosaic(data.copy(), PATTERN, np.ones(3, np.float32), np.ones(3, np.float32), np.eye(3, dtype=np.float32))
frames.read_raw = fake_read_raw

def save_raw(name, data):
    p = os.path.join(OUT, name)
    with open(p, 'wb') as fh:
        np.save(fh, data)
    return {'path': p, 'name': name, 'exposure_s': 30.0}

lights = []
offsets = [(0, 0, 0), (12.3, -7.1, 0.3), (-20.5, 9.9, -0.4), (5.2, 15.7, 0.8), (-8.8, -12.2, 0.0), (30.1, 3.3, -0.6),
           (-3.3, -25.0, 0.5), (17.7, 21.1, 0.2)]
for i, (dx, dy, rot) in enumerate(offsets):
    lights.append(save_raw(f'L{i:02d}.dng', raw_frame('light', i, dx=dx, dy=dy, rot=rot, seed=i)))
lights.append(save_raw('L_trail.dng', raw_frame('light', 20, dx=4, dy=4, rot=0.1, trail=True, seed=20)))
lights.append(save_raw('L_blur.dng', raw_frame('light', 21, dx=-4, dy=2, rot=0.0, fwhm=7.0, seed=21)))
darks = [save_raw(f'D{i}.dng', raw_frame('dark', i)) for i in range(8)]
flats = [save_raw(f'F{i}.dng', raw_frame('flat', i)) for i in range(8)]
bias = [save_raw(f'B{i}.dng', raw_frame('bias', i)) for i in range(10)]

job = os.path.join(OUT, 'job'); os.makedirs(job)
spec = {'kind': 'stack', 'output_dir': os.path.join(job, 'out'), 'tmp_dir': os.path.join(job, 'tmp'), 'memory_mb': 300,
        'frames': {'light': lights, 'dark': darks, 'flat': flats, 'bias': bias}, 'settings': {}}
t = time.time()
result = stack.run(spec, Progress(os.path.join(job, 'progress.json')))
print(f"RAW stack: {time.time() - t:.1f} s, used {result['frames_used']}/{result['frames_total']}, ref {result['reference']}")
for fr in result['frames']:
    if not fr['accepted']:
        print('   left out', fr['name'], '-', fr['reason'])
print('   masters', result['masters'], 'warnings', result['warnings'], 'crop', result['crop'])
hdr, data = fitsio.open_fits(os.path.join(spec['output_dir'], 'master.fits'))
print('   fits', hdr.get('NCOMBINE'), data.shape, 'files', sorted(os.listdir(spec['output_dir'])))
g = np.array(data[1])
ref_off = offsets[int(result['reference'][1:3])] if result['reference'].startswith('L0') else (0, 0, 0)
truth = scene(*ref_off, seed=0)[..., 1]
c = result['crop']; y0, y1, x0, x1 = develop.crop_box(H, W, c)
gi, ti = g[y0 + 10:y1 - 10, x0 + 10:x1 - 10], truth[y0 + 10:y1 - 10, x0 + 10:x1 - 10]
scale = np.median(gi) / np.median(ti)          # flats keep the median level, not the absolute one
diff = gi - ti * scale
print(f"   level scale {scale:.3f}; residual robust sd {stars.robust_stats(diff)[1]:.4f} (one frame ~{0.003/0.88:.4f}, ideal /sqrt(8) {0.003/0.88/np.sqrt(8):.4f})")
st_res = stars.summarise(stars.detect_stars(g[y0:y1, x0:x1]))
print(f"   stacked FWHM {st_res['fwhm']:.2f} ecc {st_res['ecc']:.2f} stars {st_res['stars']}")
full_diff = g - truth * scale
inside = (hot_y > 20) & (hot_y < H - 20) & (hot_x > 60) & (hot_x < W - 20)
print(f"   at the 60 planted hot pixels: max excess {np.nanmax(full_diff[hot_y[inside], hot_x[inside]]):.4f} (raw hot pixels add 0.05-0.3)")
# Trail kept in but rejected pixel by pixel
spec3 = dict(spec, output_dir=os.path.join(job, 'out3'), tmp_dir=os.path.join(job, 'tmp3'), settings={'reject_auto': False})
r3 = stack.run(spec3, Progress(os.path.join(job, 'p3.json')))
_, d3 = fitsio.open_fits(os.path.join(spec3['output_dir'], 'master.fits'))
g3 = np.array(d3[1])
line = np.abs((yy - 0.4 * xx) - 150 - (ref_off[1] - 4) + 0.4 * (ref_off[0] - 4)) < 1.0
print(f"   with the trail frame kept in: used {r3['frames_used']}, trail pixels excess {np.nanmedian((g3 - truth * scale)[line & (xx > 100)]):+.4f}, rejected high {r3['stats']['rejected_high_pct']}%")
print('   peak RSS MB', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)

# JPEG stack
from PIL import Image
jl = []
for i, (dx, dy, rot) in enumerate(offsets[:6]):
    rgb = scene(dx, dy, rot, seed=i) + rng.normal(0, 0.003, (H, W, 3))
    p = os.path.join(OUT, f'J{i}.jpg')
    Image.fromarray((frames.linear_to_srgb(np.clip(rgb, 0, 1)) * 255).astype(np.uint8)).save(p, quality=95)
    jl.append({'path': p, 'name': f'J{i}.jpg', 'exposure_s': 30})
job2 = os.path.join(OUT, 'job2'); os.makedirs(job2)
spec2 = {'kind': 'stack', 'output_dir': os.path.join(job2, 'out'), 'tmp_dir': os.path.join(job2, 'tmp'), 'memory_mb': 300,
         'frames': {'light': jl}, 'settings': {'interpolation': 'bilinear', 'method': 'sigma'}}
t = time.time(); r2 = stack.run(spec2, Progress(os.path.join(job2, 'progress.json')))
print(f"JPEG stack: {time.time() - t:.1f} s, used {r2['frames_used']}/{r2['frames_total']}, crop {r2['crop']}, warnings {r2['warnings']}")
