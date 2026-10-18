// Synthetic RAW-like frames for the pipeline tests (the JS twin of synth.py and
// t_stack.py's scene): a star field with colours, a nebula, a sky gradient, vignetting,
// a bias pattern, dark current with hot pixels, and noise, on a GBRG Bayer mosaic.

export function rng(seed) {
    let s = seed >>> 0;
    const next = () => {
        s = (s + 0x6d2b79f5) | 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    next.normal = () => {
        const u = Math.max(next(), 1e-12), v = next();
        return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
    };
    return next;
}

export const PATTERN = [[1, 2], [0, 1]];       // G B / R G

export function makeScene({ width = 320, height = 240, stars = 220, seed = 3 } = {}) {
    const r = rng(seed);
    const list = [];
    for (let i = 0; i < stars; i++) {
        const c = [0.6 + 0.8 * r(), 1, 0.6 + 0.8 * r()];
        list.push({ x: -40 + (width + 80) * r(), y: -40 + (height + 80) * r(), flux: 10 ** (1.5 + 2.5 * r()), c });
    }
    const hot = Array.from({ length: 30 }, () => [Math.floor(r() * height), Math.floor(r() * width), 0.05 + 0.25 * r()]);
    const bias = new Float32Array(width * height).map(() => 0.0004 * r.normal());
    return { width, height, stars: list, hot, bias };
}

function render(scene, { dx = 0, dy = 0, rot = 0, fwhm = 3, trail = false, seed = 1, kind = 'light' }) {
    const { width: w, height: h } = scene;
    const r = rng(seed * 7919 + 13);
    const out = new Float32Array(w * h);
    const vignette = (x, y) => 1 - 0.35 * (((x - w / 2) / (w / 2)) ** 2 + ((y - h / 2) / (h / 2)) ** 2);
    const darkAt = new Map(scene.hot.map(([y, x, v]) => [y * w + x, v]));
    if (kind === 'light') {
        const s = fwhm / 2.3548;
        const t = rot * Math.PI / 180, ct = Math.cos(t), st = Math.sin(t);
        const rgb = [0, 1, 2].map(() => new Float32Array(w * h));
        for (const star of scene.stars) {
            const X = ct * (star.x - w / 2) - st * (star.y - h / 2) + w / 2 + dx;
            const Y = st * (star.x - w / 2) + ct * (star.y - h / 2) + h / 2 + dy;
            const rad = Math.ceil(5 * s);
            for (let y = Math.max(0, Math.round(Y) - rad); y <= Math.min(h - 1, Math.round(Y) + rad); y++) {
                for (let x = Math.max(0, Math.round(X) - rad); x <= Math.min(w - 1, Math.round(X) + rad); x++) {
                    const g = Math.exp(-0.5 * ((x - X) ** 2 + (y - Y) ** 2) / (s * s)) * star.flux * 3e-4 / (2 * Math.PI * s * s);
                    for (let c = 0; c < 3; c++) rgb[c][y * w + x] += g * star.c[c];
                }
            }
        }
        for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
            const i = y * w + x;
            const neb = 0.03 * Math.exp(-(((x - 0.55 * w) / (0.12 * w)) ** 2 + ((y - 0.45 * h) / (0.11 * h)) ** 2));
            const c = PATTERN[y & 1][x & 1];
            let v = rgb[c][i] + neb * [1, 0.5, 0.7][c] + 0.04 + 0.02 * x / w;
            if (trail && Math.abs((y - 0.4 * x) - 0.45 * h) < 1.5) v += 0.4;
            out[i] = v * vignette(x, y) + scene.bias[i] + 0.002 + (darkAt.get(i) || 0) + 0.003 * r.normal();
        }
    } else {
        for (let i = 0; i < w * h; i++) {
            const y = Math.floor(i / w), x = i - y * w;
            if (kind === 'bias') out[i] = scene.bias[i] + 0.0015 * r.normal();
            else if (kind === 'dark') out[i] = scene.bias[i] + 0.002 + (darkAt.get(i) || 0) + 0.003 * r.normal();
            else out[i] = scene.bias[i] + 0.45 * vignette(x, y) + 0.004 * r.normal();
        }
    }
    return out;
}

// env.loadFrame for entries with url "synth:<kind>:<index>[:json options]"
export function synthLoader(scene, lightOptions = {}) {
    const cache = new Map();
    return async (entry) => {
        const [, kind, idx] = entry.url.split(':');
        const key = entry.url;
        if (!cache.has(key)) {
            const opts = kind === 'light' ? { seed: Number(idx) + 1, ...(lightOptions[idx] || {}), kind } : { seed: 1000 + Number(idx) + kind.length * 100, kind };
            cache.set(key, render(scene, opts));
        }
        return {
            planes: [Float32Array.from(cache.get(key))], width: scene.width, height: scene.height, pattern: PATTERN, isRaw: true,
            wb: [1, 1, 1], wbAsShot: [1, 1, 1], camToSrgb: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], camera: 'Synthetic'
        };
    };
}

export function entries(kind, n, exposure = 60) {
    return Array.from({ length: n }, (_, i) => ({ name: `${kind}_${i}.cr2`, url: `synth:${kind}:${i}`, kind: 'raw', exposure_s: exposure }));
}
