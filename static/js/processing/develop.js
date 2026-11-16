// Developing a linear image into a picture: crop, background, colour, stretch, contrast,
// saturation, noise reduction and sharpening. A port of app/processing/develop.py (see
// there for the reasoning). Images are {planes: [r, g, b], width, height}.
//
// resolve() measures the image-dependent numbers on the preview; apply() reuses them
// at any size, so the preview and the full-size export match.

import { LUMA } from './colour.js';
import { gaussianFilter, median, mirrorIndex, percentile, robustStats } from './image.js';
import { detectStars } from './stars.js';

export const DEFAULTS = {
    crop: { x0: 0, y0: 0, x1: 1, y1: 1 },
    background: { enabled: false, degree: 2, grid: 16, mode: 'subtract' },
    colour: { neutralise: true, white: 'stars', manual: [1, 1, 1] },
    stretch: { mode: 'auto', linked: true, target: 0.2, shadows: -2.8, black: 0, mid: 0.5, white: 1, asinh: 30 },
    contrast: 0,
    saturation: 1,
    noise: { chroma: 0, luma: 0 },
    sharpen: { layers: [1, 1, 1, 1] }
};

export const AUTO = {
    background: { enabled: true, degree: 2 },
    colour: { neutralise: true, white: 'stars' },
    stretch: { mode: 'auto', linked: true, target: 0.2, shadows: -2.8 },
    contrast: 0.15,
    saturation: 1.3,
    noise: { chroma: 0.5, luma: 0.2 },
    sharpen: { layers: [1, 1, 1, 1] }
};

export const AUTO_PLANETARY = {
    background: { enabled: false },
    colour: { neutralise: false, white: 'average' },
    stretch: { mode: 'none' },
    contrast: 0.1,
    saturation: 1.1,
    noise: { chroma: 0.2, luma: 0 },
    sharpen: { layers: [1.6, 1.4, 1.15, 1] }
};

const LIMITS = [
    [['background', 'degree'], 0, 4],
    [['background', 'grid'], 6, 40],
    [['stretch', 'target'], 0.02, 0.6],
    [['stretch', 'shadows'], -8, 2],
    [['stretch', 'black'], 0, 0.99],
    [['stretch', 'mid'], 0.001, 0.999],
    [['stretch', 'white'], 0.01, 1],
    [['stretch', 'asinh'], 1, 1000],
    [['contrast'], -0.9, 0.9],
    [['saturation'], 0, 3],
    [['noise', 'chroma'], 0, 1],
    [['noise', 'luma'], 0, 1]
];

const clone = (v) => JSON.parse(JSON.stringify(v));

function deepMerge(base, extra) {
    const out = clone(base);
    Object.entries(extra || {}).forEach(([key, value]) => {
        if (!(key in out)) return;
        if (out[key] && typeof out[key] === 'object' && !Array.isArray(out[key]) && value && typeof value === 'object' && !Array.isArray(value)) {
            out[key] = deepMerge(out[key], value);
        } else out[key] = clone(value);
    });
    return out;
}

function clamp(value, lo, hi, fallback) {
    const v = Number(value);
    if (value === null || value === undefined || value === '' || !Number.isFinite(v)) return fallback;
    return Math.min(Math.max(v, lo), hi);
}

// Python's round(): halves go to the even neighbour
export function roundHalfEven(x) {
    const r = Math.round(x);
    return Math.abs(x % 1) === 0.5 && r % 2 ? r - 1 : r;
}

export function cleanParams(params, planetary = false) {
    let p = deepMerge(DEFAULTS, params || {});
    if (planetary && !params) p = deepMerge(p, AUTO_PLANETARY);
    for (const [path, lo, hi] of LIMITS) {
        let node = p, def = DEFAULTS;
        path.slice(0, -1).forEach(k => { node = node[k]; def = def[k]; });
        const key = path[path.length - 1];
        node[key] = clamp(node[key], lo, hi, def[key]);
    }
    const bg = p.background;
    bg.degree = Math.trunc(bg.degree);
    bg.grid = Math.trunc(bg.grid);
    bg.enabled = !!bg.enabled;
    if (!['subtract', 'divide'].includes(bg.mode)) bg.mode = 'subtract';
    if (!['stars', 'average', 'manual', 'none'].includes(p.colour.white)) p.colour.white = 'stars';
    p.colour.neutralise = !!p.colour.neutralise;
    p.colour.manual = [...(p.colour.manual || [1, 1, 1]), 1, 1, 1].slice(0, 3).map(v => clamp(v, 0.1, 10, 1));
    if (!['auto', 'manual', 'arcsinh', 'none'].includes(p.stretch.mode)) p.stretch.mode = 'auto';
    p.stretch.linked = !!p.stretch.linked;
    p.sharpen.layers = [...(p.sharpen.layers || [1, 1, 1, 1]), 1, 1, 1, 1].slice(0, 4).map(v => clamp(v, 0, 4, 1));
    const c = p.crop || {};
    let [x0, x1] = [clamp(c.x0, 0, 1, 0), clamp(c.x1, 0, 1, 1)].sort((a, b) => a - b);
    let [y0, y1] = [clamp(c.y0, 0, 1, 0), clamp(c.y1, 0, 1, 1)].sort((a, b) => a - b);
    if (x1 - x0 < 0.02) { x0 = 0; x1 = 1; }
    if (y1 - y0 < 0.02) { y0 = 0; y1 = 1; }
    p.crop = { x0, y0, x1, y1 };
    return p;
}

export function autoParams(planetary = false) {
    return cleanParams(deepMerge(DEFAULTS, planetary ? AUTO_PLANETARY : AUTO));
}

export function cropBox(height, width, crop) {
    const y0 = Math.trunc(roundHalfEven(crop.y0 * height));
    const y1 = Math.max(y0 + 1, Math.trunc(roundHalfEven(crop.y1 * height)));
    const x0 = Math.trunc(roundHalfEven(crop.x0 * width));
    const x1 = Math.max(x0 + 1, Math.trunc(roundHalfEven(crop.x1 * width)));
    return [y0, y1, x0, x1];
}

export function cropImage(img, box) {
    const [y0, y1, x0, x1] = box;
    const w = x1 - x0, h = y1 - y0;
    const planes = img.planes.map(p => {
        const out = new Float32Array(w * h);
        for (let y = 0; y < h; y++) {
            const src = (y0 + y) * img.width + x0;
            for (let x = 0; x < w; x++) {
                const v = p[src + x];
                out[y * w + x] = v === v ? v : 0;
            }
        }
        return out;
    });
    return { planes, width: w, height: h };
}

// --- background -----------------------------------------------------------------------

function polyTerms(degree) {
    const t = [];
    for (let total = 0; total <= degree; total++) for (let i = 0; i <= total; i++) t.push([i, total - i]);
    return t;
}

function design(u, v, terms) {
    return terms.map(([i, j]) => (u ** i) * (v ** j));
}

// Least squares through the normal equations (a handful of terms, well conditioned on -1..1)
function lstsq(rows, rhs) {
    const m = rows[0].length, k = rhs[0].length;
    const ata = Array.from({ length: m }, () => new Float64Array(m));
    const atb = Array.from({ length: m }, () => new Float64Array(k));
    rows.forEach((row, r) => {
        for (let i = 0; i < m; i++) {
            for (let j = 0; j < m; j++) ata[i][j] += row[i] * row[j];
            for (let c = 0; c < k; c++) atb[i][c] += row[i] * rhs[r][c];
        }
    });
    for (let col = 0; col < m; col++) {
        let p = col;
        for (let i = col + 1; i < m; i++) if (Math.abs(ata[i][col]) > Math.abs(ata[p][col])) p = i;
        [ata[col], ata[p]] = [ata[p], ata[col]];
        [atb[col], atb[p]] = [atb[p], atb[col]];
        const d = ata[col][col] || 1e-12;
        for (let i = 0; i < m; i++) {
            if (i === col) continue;
            const f = ata[i][col] / d;
            if (!f) continue;
            for (let j = col; j < m; j++) ata[i][j] -= f * ata[col][j];
            for (let c = 0; c < k; c++) atb[i][c] -= f * atb[col][c];
        }
    }
    return Array.from({ length: m }, (_, i) => Array.from(atb[i], v => v / (ata[i][i] || 1e-12)));     // terms x channels
}

// Per-channel polynomial in u, v (-1..1 across the image), fitted to the medians of a
// grid of boxes, dropping boxes that hold stars or nebula
export function fitBackground(img, degree = 2, grid = 16) {
    const { planes, width: w, height: h } = img;
    const nx = grid, ny = Math.max(4, roundHalfEven(grid * h / w));
    const bh = h / ny, bw = w / nx;
    const samples = [], us = [], vs = [];
    for (let i = 0; i < ny; i++) for (let j = 0; j < nx; j++) {
        const ya = Math.trunc(i * bh), yb = Math.trunc((i + 1) * bh), xa = Math.trunc(j * bw), xb = Math.trunc((j + 1) * bw);
        const bwid = xb - xa;
        const vals = [[], [], []];
        let k = 0;
        for (let y = ya; y < yb; y++) for (let x = xa; x < xb; x++, k++) {
            if (k % 3) continue;
            for (let c = 0; c < 3; c++) vals[c].push(planes[c][y * w + x]);
        }
        samples.push(bwid > 0 && yb > ya ? vals.map(v => median(v)) : [0, 0, 0]);
        vs.push((i + 0.5) / ny * 2 - 1);
        us.push((j + 0.5) / nx * 2 - 1);
    }
    const lum = samples.map(s => s[0] * LUMA[0] + s[1] * LUMA[1] + s[2] * LUMA[2]);
    const finite = samples.map(s => s.every(Number.isFinite));
    let ok = finite.slice();
    if (ok.some(Boolean)) {
        const p75 = percentile(lum.filter((_, i) => ok[i]), 75);
        ok = ok.map((o, i) => o && lum[i] <= p75);
    }
    degree = Math.max(0, Math.min(Math.trunc(degree), 4));
    let coeffs = null;
    for (let it = 0; it < 6; it++) {
        let count = ok.filter(Boolean).length;
        if (count < polyTerms(degree).length + 2) {
            degree = Math.max(0, degree - 1);
            if (count < polyTerms(degree).length + 1) break;
        }
        const terms = polyTerms(degree);
        const idx = ok.map((o, i) => (o ? i : -1)).filter(i => i >= 0);
        coeffs = lstsq(idx.map(i => design(us[i], vs[i], terms)), idx.map(i => samples[i]));
        const model = samples.map((_, i) => {
            const d = design(us[i], vs[i], terms);
            return [0, 1, 2].map(c => d.reduce((s, t, q) => s + t * coeffs[q][c], 0));
        });
        const resid = model.map((m, i) => lum[i] - (m[0] * LUMA[0] + m[1] * LUMA[1] + m[2] * LUMA[2]));
        const sigma = Math.max(robustStats(resid.filter((_, i) => ok[i]))[1], 1e-6);
        const newOk = finite.map((f, i) => f && resid[i] < 2 * sigma && resid[i] > -4 * sigma);
        if (newOk.every((v, i) => v === ok[i])) break;
        ok = newOk;
    }
    if (!coeffs) {
        degree = 0;
        coeffs = [[0, 1, 2].map(c => median(samples.map(s => s[c])))];
    }
    const terms = polyTerms(degree);
    const levels = [0, 1, 2].map(c => median(samples.map((_, i) => {
        const d = design(us[i], vs[i], terms);
        return d.reduce((s, t, q) => s + t * coeffs[q][c], 0);
    })));
    return { degree, coeffs: [0, 1, 2].map(c => coeffs.map(row => row[c])), levels };
}

// The model at rows [row0, row0 + rows) and columns [col0, col0 + cols) of a cropped
// image cropH x cropW
function evalBackground(model, c, row0, rows, col0, cols, cropH, cropW) {
    const terms = polyTerms(model.degree);
    const co = model.coeffs[c];
    const out = new Float32Array(rows * cols);
    const upow = terms.map(() => null);
    const uc = new Float64Array(cols);
    for (let x = 0; x < cols; x++) uc[x] = (col0 + x + 0.5) / cropW * 2 - 1;
    terms.forEach(([i], q) => { upow[q] = uc.map(u => u ** i); });
    for (let y = 0; y < rows; y++) {
        const v = (row0 + y + 0.5) / cropH * 2 - 1;
        const vp = terms.map(([, j]) => v ** j);
        for (let x = 0; x < cols; x++) {
            let s = 0;
            for (let q = 0; q < terms.length; q++) s += co[q] * upow[q][x] * vp[q];
            out[y * cols + x] = s;
        }
    }
    return out;
}

// The fitted background itself, stretched to 0..1 per channel (to show what's taken away)
export function backgroundImage(model, width, height) {
    const planes = [0, 1, 2].map(c => {
        const p = evalBackground(model, c, 0, height, 0, width, height, width);
        let lo = Infinity, hi = -Infinity;
        for (let i = 0; i < p.length; i++) { lo = Math.min(lo, p[i]); hi = Math.max(hi, p[i]); }
        const k = 1 / Math.max(hi - lo, 1e-9);
        for (let i = 0; i < p.length; i++) p[i] = (p[i] - lo) * k;
        return p;
    });
    return { planes, width, height };
}

function applyBackground(img, model, mode, row0, col0, cropH, cropW, neutral) {
    const { width: w, height: h } = img;
    const levels = model.levels;
    const mean = (levels[0] + levels[1] + levels[2]) / 3;
    const planes = img.planes.map((p, c) => {
        const bgm = evalBackground(model, c, row0, h, col0, w, cropH, cropW);
        const out = new Float32Array(p.length);
        if (mode === 'divide') {
            const lv = Math.max(levels[c], 1e-6);
            for (let i = 0; i < p.length; i++) out[i] = p[i] / Math.max(bgm[i] / lv, 1e-3);
        } else {
            const lv = neutral ? mean : levels[c];
            for (let i = 0; i < p.length; i++) out[i] = p[i] - bgm[i] + lv;
        }
        return out;
    });
    return { planes, width: w, height: h };
}

// --- colour ----------------------------------------------------------------------------

function lumaOf(planes) {
    const [r, g, b] = planes;
    const out = new Float32Array(r.length);
    for (let i = 0; i < r.length; i++) out[i] = r[i] * LUMA[0] + g[i] * LUMA[1] + b[i] * LUMA[2];
    return out;
}

function nanMax(planes) {
    let m = -Infinity;
    planes.forEach(p => { for (let i = 0; i < p.length; i++) if (p[i] > m) m = p[i]; });
    return m;
}

// Factors making the median star white, from apertures on unsaturated stars
export function starWhiteBalance(img) {
    const { planes, width: w, height: h } = img;
    const st = detectStars({ data: lumaOf(planes), width: w, height: h }, { k: 8, maxStars: 400 });
    const keep = st.x.map((_, i) => i).filter(i => !st.saturated[i] && st.snr[i] > 15);
    if (keep.length < 12) return [null, 0];
    const r = 4;
    const peakAll = nanMax(planes);
    const rows = keep.map(i => {
        const iy = Math.min(Math.max(roundHalfEven(st.y[i]), r), h - r - 1);
        const ix = Math.min(Math.max(roundHalfEven(st.x[i]), r), w - r - 1);
        const ring = [[], [], []], disk = [[], [], []];
        let peak = -Infinity;
        for (let dy = -r; dy <= r; dy++) for (let dx = -r; dx <= r; dx++) {
            const d = Math.hypot(dy, dx);
            const p = (iy + dy) * w + ix + dx;
            for (let c = 0; c < 3; c++) {
                const v = planes[c][p];
                if (v > peak) peak = v;
                if (d <= r - 1) disk[c].push(v);
                if (d > r - 0.5) ring[c].push(v);
            }
        }
        const sky = ring.map(v => median(v));
        const flux = disk.map((v, c) => v.reduce((s, x) => s + x - sky[c], 0));
        return { peak, flux };
    });
    const winMax = Math.max(...rows.map(r2 => r2.peak));
    const peakOk = rows.map(r2 => (winMax >= 0.98 * peakAll ? r2.peak < 0.95 * peakAll : true));
    const good = rows.filter((r2, i) => peakOk[i] && r2.flux.every(f => f > 0));
    if (good.length < 12) return [null, good.length];
    const fr = median(good.map(g => g.flux[1] / g.flux[0]));
    const fb = median(good.map(g => g.flux[1] / g.flux[2]));
    if (!(fr > 0 && fb > 0 && Number.isFinite(fr) && Number.isFinite(fb))) return [null, good.length];
    return [[fr, 1, fb].map(v => Math.min(Math.max(v, 0.2), 5)), good.length];
}

// Grey world on the parts of the image clearly above the sky
export function averageWhiteBalance(img, background) {
    const { planes, width: w, height: h } = img;
    const sig = planes.map((p, c) => p.map(v => v - background[c]));
    const lum = lumaOf(sig);
    const sub = [];
    for (let y = 0; y < h; y += 4) for (let x = 0; x < w; x += 4) sub.push(lum[y * w + x]);
    const noise = robustStats(sub)[1];
    let thr = 5 * Math.max(noise, 1e-6);
    let count = 0;
    for (let i = 0; i < lum.length; i++) if (lum[i] > thr) count++;
    if (count < 50) thr = percentile(lum, 90);
    const sums = [0, 0, 0];
    for (let i = 0; i < lum.length; i++) {
        if (!(lum[i] > thr)) continue;
        for (let c = 0; c < 3; c++) sums[c] += Math.max(sig[c][i], 0);
    }
    if (sums.some(s => s <= 0)) return [1, 1, 1];
    return sums.map(s => Math.min(Math.max(sums[1] / s, 0.2), 5));
}

function applyColour(img, offsets, sky, factors) {
    const planes = img.planes.map((p, c) => {
        const out = new Float32Array(p.length);
        const o = offsets[c], s = sky[c], f = factors[c];
        for (let i = 0; i < p.length; i++) out[i] = (p[i] + o - s) * f + s;
        return out;
    });
    return { planes, width: img.width, height: img.height };
}

// --- stretch ----------------------------------------------------------------------------

// The midtones transfer function: 0 -> 0, m -> 0.5, 1 -> 1
export function mtf(m, x) {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    return ((m - 1) * x) / ((2 * m - 1) * x - m);
}

function sub4(p, w, h) {
    const out = [];
    for (let y = 0; y < h; y += 4) for (let x = 0; x < w; x += 4) out.push(p[y * w + x]);
    return out;
}

export function autoStretchLevels(img, target = 0.2, shadows = -2.8, linked = true) {
    let stats = img.planes.map(p => robustStats(sub4(p, img.width, img.height)));
    if (linked) {
        const med = stats.reduce((s, v) => s + v[0], 0) / 3;
        const mad = stats.reduce((s, v) => s + v[1], 0) / 3 / 1.4826;
        stats = [0, 1, 2].map(() => [med, mad * 1.4826]);
    }
    const blacks = [], mids = [];
    for (const [med, sd] of stats) {
        const mad = sd / 1.4826;
        const black = Math.min(Math.max(med + shadows * mad, 0), 0.99);
        let x = (med - black) / Math.max(1 - black, 1e-6);
        x = Math.min(Math.max(x, 1e-6), 0.999);
        blacks.push(black);
        mids.push(Math.min(Math.max(mtf(target, x), 1e-4), 0.9999));
    }
    return [blacks, mids];
}

// --- detail -----------------------------------------------------------------------------

// Starlet (a trous B3 spline) layers, fine to coarse, plus the smooth residual
export function waveletLayers(data, w, h, nLayers = 4) {
    const B3 = [1 / 16, 4 / 16, 6 / 16, 4 / 16, 1 / 16];
    const layers = [];
    let current = Float32Array.from(data);
    const tmp = new Float32Array(w * h);
    for (let j = 0; j < nLayers; j++) {
        const step = 2 ** j;
        const smooth = new Float32Array(w * h);
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                let s = 0;
                for (let k = -2; k <= 2; k++) s += B3[k + 2] * current[mirrorIndex(y + k * step, h) * w + x];
                tmp[y * w + x] = s;
            }
        }
        for (let y = 0; y < h; y++) {
            const row = y * w;
            for (let x = 0; x < w; x++) {
                let s = 0;
                for (let k = -2; k <= 2; k++) s += B3[k + 2] * tmp[row + mirrorIndex(x + k * step, w)];
                smooth[row + x] = s;
            }
        }
        const layer = new Float32Array(w * h);
        for (let i = 0; i < layer.length; i++) layer[i] = current[i] - smooth[i];
        layers.push(layer);
        current = smooth;
    }
    return { layers, residual: current };
}

// --- resolve and apply --------------------------------------------------------------------

export function resolve(img, params, planetary = false) {
    const p = cleanParams(params, planetary);
    let work = cropImage(img, cropBox(img.height, img.width, p.crop));
    const resolved = { params: p, planetary };
    const bg = p.background;
    if (bg.enabled) {
        const model = fitBackground(work, bg.degree, bg.grid);
        resolved.background_model = model;
        work = applyBackground(work, model, bg.mode, 0, 0, work.height, work.width, p.colour.neutralise);
    }
    let sky = work.planes.map(pl => robustStats(sub4(pl, work.width, work.height))[0]);
    const col = p.colour;
    let offsets = [0, 0, 0];
    if (col.neutralise) {
        const target = (sky[0] + sky[1] + sky[2]) / 3;
        offsets = sky.map(s => target - s);
        sky = [target, target, target];
    }
    let factors = [1, 1, 1], starsUsed = 0;
    const shifted = () => applyColour(work, offsets, [0, 0, 0], [1, 1, 1]);
    if (col.white === 'stars') {
        const off = shifted();
        const [f, n] = starWhiteBalance(off);
        starsUsed = n;
        factors = f || averageWhiteBalance(off, sky);
        resolved.white_source = f ? 'stars' : 'average';
    } else if (col.white === 'average') {
        factors = averageWhiteBalance(shifted(), sky);
        resolved.white_source = 'average';
    } else if (col.white === 'manual') {
        factors = col.manual.slice();
        resolved.white_source = 'manual';
    }
    factors = factors.map(f => f / factors[1]);
    Object.assign(resolved, { offsets, sky, factors, stars_used: starsUsed });
    work = applyColour(work, offsets, sky, factors);

    const st = p.stretch;
    if (st.mode === 'auto') {
        const [black, mid] = autoStretchLevels(work, st.target, st.shadows, st.linked);
        resolved.levels = { black, mid, white: [1, 1, 1] };
    } else if (st.mode === 'manual') {
        resolved.levels = { black: [st.black, st.black, st.black], mid: [st.mid, st.mid, st.mid], white: [st.white, st.white, st.white] };
    } else if (st.mode === 'arcsinh') {
        const lum = sub4(lumaOf(work.planes), work.width, work.height);
        const [med, sd] = robustStats(lum);
        const black = Math.max(med + st.shadows * sd / 1.4826, 0);
        const hi = percentile(lum, 99.95);
        resolved.asinh = { black, beta: st.asinh, white: Math.max(hi, black + 1e-4) };
    } else {
        let peak = 1;
        if (!planetary) {
            const vals = [];
            for (let y = 0; y < work.height; y += 4) for (let x = 0; x < work.width; x += 4) {
                for (let c = 0; c < 3; c++) vals.push(Math.max(work.planes[c][y * work.width + x], 0));
            }
            peak = percentile(vals, 99.99);
        }
        resolved.peak = Math.max(peak, 1);
    }
    return resolved;
}

// Develop img (linear) with resolved numbers. scale: this image's size relative to full
// size (for blur radii). region [row0, col0, cropH, cropW] places a piece of the cropped
// image; without it, img is the whole uncropped image and the crop happens here.
export function apply(img, resolved, scale = 1, region = null) {
    const p = resolved.params;
    if (!region) {
        img = cropImage(img, cropBox(img.height, img.width, p.crop));
        region = [0, 0, img.height, img.width];
    } else {
        img = { ...img, planes: img.planes.map(pl => pl.map(v => (v === v ? v : 0))) };
    }
    const [row0, col0, cropH, cropW] = region;
    const { width: w, height: h } = img;
    if (resolved.background_model) {
        img = applyBackground(img, resolved.background_model, p.background.mode, row0, col0, cropH, cropW, p.colour.neutralise);
    }
    img = applyColour(img, resolved.offsets, resolved.sky, resolved.factors);
    let [r, g, b] = img.planes;
    const n = r.length;

    const st = p.stretch;
    if ((st.mode === 'auto' || st.mode === 'manual') && resolved.levels) {
        const lv = resolved.levels;
        [r, g, b].forEach((pl, c) => {
            const black = lv.black[c], mid = lv.mid[c], span = Math.max(lv.white[c] - black, 1e-6);
            for (let i = 0; i < n; i++) pl[i] = mtf(mid, Math.min(Math.max((pl[i] - black) / span, 0), 1));
        });
    } else if (st.mode === 'arcsinh' && resolved.asinh) {
        const a = resolved.asinh;
        const span = Math.max(a.white - a.black, 1e-6);
        const norm = Math.asinh(a.beta);
        for (let i = 0; i < n; i++) {
            const lum = r[i] * LUMA[0] + g[i] * LUMA[1] + b[i] * LUMA[2];
            const x = Math.max((lum - a.black) / span, 0);
            const y = Math.asinh(a.beta * x) / norm;
            const k = lum - a.black > 1e-9 ? y / Math.max(lum - a.black, 1e-9) : 0;
            let R = Math.max((r[i] - a.black) * k, 0), G = Math.max((g[i] - a.black) * k, 0), B = Math.max((b[i] - a.black) * k, 0);
            const peak = Math.max(R, G, B);
            if (peak > 1) { R /= peak; G /= peak; B /= peak; }
            r[i] = R; g[i] = G; b[i] = B;
        }
    } else {
        const k = 1 / (resolved.peak || 1);
        [r, g, b].forEach(pl => { for (let i = 0; i < n; i++) pl[i] = Math.max(pl[i], 0) * k; });
    }

    const contrast = p.contrast;
    const sat = p.saturation;
    for (let i = 0; i < n; i++) {
        let R = Math.min(Math.max(r[i], 0), 1), G = Math.min(Math.max(g[i], 0), 1), B = Math.min(Math.max(b[i], 0), 1);
        if (contrast) {
            R = Math.min(Math.max(R - contrast * Math.sin(2 * Math.PI * R) / (2 * Math.PI), 0), 1);
            G = Math.min(Math.max(G - contrast * Math.sin(2 * Math.PI * G) / (2 * Math.PI), 0), 1);
            B = Math.min(Math.max(B - contrast * Math.sin(2 * Math.PI * B) / (2 * Math.PI), 0), 1);
        }
        if (sat !== 1) {
            const L = R * LUMA[0] + G * LUMA[1] + B * LUMA[2];
            R = L + (R - L) * sat; G = L + (G - L) * sat; B = L + (B - L) * sat;
        }
        r[i] = R; g[i] = G; b[i] = B;
    }

    const nz = p.noise;
    if (nz.chroma > 0) {
        const lum = lumaOf([r, g, b]);
        const sigma = Math.max(0.5, 4 * nz.chroma * Math.sqrt(scale));
        [r, g, b] = [r, g, b].map(pl => {
            const chroma = new Float32Array(n);
            for (let i = 0; i < n; i++) chroma[i] = pl[i] - lum[i];
            const blurred = gaussianFilter(chroma, w, h, sigma);
            for (let i = 0; i < n; i++) blurred[i] += lum[i];
            return blurred;
        });
    }
    const gains = p.sharpen.layers;
    if (nz.luma > 0 || gains.some(gn => Math.abs(gn - 1) > 1e-3)) {
        const lum = lumaOf([r, g, b]);
        const { layers, residual } = waveletLayers(lum, w, h, 4);
        const next = Float32Array.from(residual);
        layers.forEach((layer, j) => {
            if (nz.luma > 0 && j < 2) {
                const s = [];
                for (let y = 0; y < h; y += 3) for (let x = 0; x < w; x += 3) s.push(layer[y * w + x]);
                const thr = nz.luma * (j === 0 ? 3 : 1.5) * robustStats(s)[1];
                for (let i = 0; i < n; i++) {
                    const v = layer[i];
                    layer[i] = Math.sign(v) * Math.max(Math.abs(v) - thr, 0);
                }
            }
            for (let i = 0; i < n; i++) next[i] += layer[i] * gains[j];
        });
        for (let i = 0; i < n; i++) {
            const d = next[i] - lum[i];
            r[i] += d; g[i] += d; b[i] += d;
        }
    }
    [r, g, b].forEach(pl => { for (let i = 0; i < n; i++) pl[i] = Math.min(Math.max(pl[i], 0), 1); });
    return { planes: [r, g, b], width: w, height: h };
}

// Develop a big image a strip at a time: readRows(r0, r1) gives cropped linear rows as
// {planes, width, height}, writeRows(r0, r1, developed) takes them. The margin keeps
// blurs and sharpening seamless across strips.
export async function applyStrips(readRows, height, width, resolved, writeRows, stripRows = 512, margin = 64) {
    for (let start = 0; start < height; start += stripRows) {
        const lo = Math.max(0, start - margin);
        const hi = Math.min(height, start + stripRows + margin);
        const rows = await readRows(lo, hi);
        const out = apply(rows, resolved, 1, [lo, 0, height, width]);
        const a = start - lo;
        const count = Math.min(stripRows, height - start);
        await writeRows(start, start + count, {
            planes: out.planes.map(pl => pl.subarray(a * width, (a + count) * width)),
            width, height: count
        });
    }
}

// Interleaved 8-bit RGBA for a canvas
export function toRGBA(img, out = new Uint8ClampedArray(img.width * img.height * 4)) {
    const [r, g, b] = img.planes;
    for (let i = 0, j = 0; i < r.length; i++, j += 4) {
        out[j] = r[i] * 255 + 0.5;
        out[j + 1] = g[i] * 255 + 0.5;
        out[j + 2] = b[i] * 255 + 0.5;
        out[j + 3] = 255;
    }
    return out;
}
