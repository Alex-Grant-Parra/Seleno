"""Full-size (5634 x 3752 mosaic) stack through the real worker process; reports peak memory."""
import json, os, resource, shutil, subprocess, sys, time
import numpy as np
sys.path.insert(0, '/home/alex/Server')
OUT = sys.argv[1]; PYLIB = sys.argv[2]
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)
H, W = 3752, 5634
rng = np.random.default_rng(1)
n = 2500
sx, sy = rng.uniform(0, W, n), rng.uniform(0, H, n)
flux = 10 ** rng.uniform(-1.5, 1.0, n)
off = np.arange(-8, 9); OY, OX = np.meshgrid(off, off, indexing='ij')

def frame(kind, dx=0.0, dy=0.0):
    m = rng.normal(0, 0.003, (H, W)).astype(np.float32)
    if kind == 'light':
        m += 0.05
        sig = 1.3
        for x, y, f in zip(sx + dx, sy + dy, flux):
            xi, yi = int(x), int(y)
            if 8 <= xi < W - 8 and 8 <= yi < H - 8:
                m[yi - 8:yi + 9, xi - 8:xi + 9] += f * 0.05 * np.exp(-((OX - (x - xi)) ** 2 + (OY - (y - yi)) ** 2) / (2 * sig * sig))
    if kind in ('light', 'dark'):
        m[::997, ::1009] += 0.2
    return m

def save(name, data):
    p = os.path.join(OUT, name)
    with open(p, 'wb') as fh:
        np.save(fh, data)
    return {'path': p, 'name': name, 'exposure_s': 30.0}

t = time.time()
lights = [save(f'L{i}.dng', frame('light', dx=i * 7.3, dy=-i * 4.1)) for i in range(int(os.environ.get('NL', 6)))]
darks = [save(f'D{i}.dng', frame('dark')) for i in range(4)]
bias = [save(f'B{i}.dng', frame('bias')) for i in range(4)]
print(f'generated in {time.time() - t:.0f} s', flush=True)

# A sitecustomize on the path makes the worker read these as RAW mosaics
shim = os.path.join(OUT, 'shim'); os.makedirs(shim)
with open(os.path.join(shim, 'sitecustomize.py'), 'w') as fh:
    fh.write('''
import numpy as np
from app.processing import frames
def _fake(path):
    return frames.Mosaic(np.load(path).astype(np.float32), np.array([[1, 2], [0, 1]]), np.ones(3, np.float32), np.ones(3, np.float32), np.eye(3, dtype=np.float32))
frames.read_raw = _fake
''')
job = os.path.join(OUT, 'job'); os.makedirs(job)
spec = {'kind': 'stack', 'output_dir': os.path.join(job, 'out'), 'tmp_dir': os.path.join(job, 'tmp'), 'memory_mb': 900,
        'frames': {'light': lights, 'dark': darks, 'bias': bias}, 'settings': {}}
json.dump(spec, open(os.path.join(job, 'spec.json'), 'w'))
env = dict(os.environ, PYTHONPATH=f'{shim}:/home/alex/Server:{PYLIB}', OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
t = time.time()
proc = subprocess.Popen([sys.executable, '-m', 'app.processing.worker', job], cwd='/home/alex/Server', env=env)
peaks = {'RssAnon': (0, ''), 'RssFile': (0, ''), 'VmRSS': (0, '')}
while proc.poll() is None:
    try:
        st = dict(l.split(':', 1) for l in open(f'/proc/{proc.pid}/status').read().splitlines() if ':' in l)
        stage = json.load(open(os.path.join(job, 'progress.json'))).get('message', '')
        for k in list(peaks):
            v = int(st[k].split()[0]) // 1024
            if v > peaks[k][0]:
                peaks[k] = (v, stage)
        key = 'anon:' + stage.split('(')[0].strip()
        v = int(st['RssAnon'].split()[0]) // 1024
        if v > peaks.get(key, (0, ''))[0]:
            peaks[key] = (v, '')
    except Exception:
        pass
    time.sleep(0.2)
rc = proc.returncode
print('peaks MB (and stage):', peaks)
peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
r = json.load(open(os.path.join(job, 'result.json')))
print(f'worker exit {rc} in {time.time() - t:.0f} s, peak RSS {peak:.0f} MB, ok={r.get("ok")} error={r.get("error")}')
print('used', r.get('frames_used'), 'size', r.get('width'), r.get('height'), 'stats', r.get('stats'), 'tmp left:', os.path.exists(spec['tmp_dir']))
print('outputs', {f: os.path.getsize(os.path.join(spec['output_dir'], f)) // 1024 for f in os.listdir(spec['output_dir'])})
