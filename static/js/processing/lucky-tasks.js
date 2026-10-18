// Lucky imaging, the per-frame work (app/processing/planetary.py): score a frame's
// sharpness around the planet (or across the Moon), crop it, and later line a crop up
// with the best one by phase correlation and shift it there.

import { linearToSrgb, LUMA } from './colour.js';
import { loadFrame, rgbaToPlanes } from './decode.js';
import { decodeHalf, encodeHalf } from './half.js';
import { debayerChannel, gaussianFilter, percentile, reflectIndex } from './image.js';
import { phaseCorrelate, phaseSpectrum } from './register.js';
import { serFramePlanes } from './ser.js';
import { applyColour } from './colour.js';
import { warpRows } from './warp.js';

function lumaOf(planes) {
    const [r, g, b] = planes;
    const out = new Float32Array(r.length);
    for (let i = 0; i < r.length; i++) out[i] = r[i] * LUMA[0] + g[i] * LUMA[1] + b[i] * LUMA[2];
    return out;
}

// (quality, cx, cy, extent, brightness) of one frame (planetary._analyse)
export function analyse(planes, w, h) {
    const lum = lumaOf(planes);
    const step = Math.max(w, h) <= 1400 ? 1 : 2;
    const sw = Math.ceil(w / step), sh = Math.ceil(h / step);
    const small = step === 1 ? lum : new Float32Array(sw * sh).map((_, i) => lum[Math.floor(i / sw) * step * w + (i % sw) * step]);
    const scale = h / sh;
    const sorted = Float32Array.from(small).sort();
    const q = (p) => {
        const pos = (p / 100) * (sorted.length - 1), i = Math.floor(pos), f = pos - i;
        return i + 1 < sorted.length ? sorted[i] * (1 - f) + sorted[i + 1] * f : sorted[i];
    };
    const bg = q(10), top = q(99.8);
    if (top - bg < 0.02) return { quality: 0, cx: w / 2, cy: h / 2, extent: Math.max(w, h), brightness: 0 };
    const thr = bg + 0.4 * (top - bg);
    let sx = 0, sy = 0, sw8 = 0;
    const ys = [], xs = [];
    for (let y = 0; y < sh; y++) for (let x = 0; x < sw; x++) {
        const v = small[y * sw + x];
        if (v > thr) {
            const wt = v - bg;
            sx += x * wt; sy += y * wt; sw8 += wt;
            ys.push(y); xs.push(x);
        }
    }
    const cy = (sy / sw8) * scale, cx = (sx / sw8) * scale;
    const extent = Math.max(percentile(ys, 99) - percentile(ys, 1), percentile(xs, 99) - percentile(xs, 1)) * scale;
    const half = Math.trunc(Math.max(extent * 0.6, 24) / scale);
    const y0 = Math.trunc(cy / scale) - half, x0 = Math.trunc(cx / scale) - half;
    const ya = Math.max(0, y0), xa = Math.max(0, x0), yb = Math.min(sh, y0 + 2 * half), xb = Math.min(sw, x0 + 2 * half);
    const rw = xb - xa, rh = yb - ya;
    const roi = new Float32Array(rw * rh);
    let mean = 0;
    for (let y = 0; y < rh; y++) for (let x = 0; x < rw; x++) { roi[y * rw + x] = small[(ya + y) * sw + xa + x]; mean += roi[y * rw + x]; }
    mean /= Math.max(roi.length, 1);
    const smooth = gaussianFilter(roi, rw, rh, 1.0);
    // scipy.ndimage.laplace (reflect edges), then its variance
    let s1 = 0, s2 = 0;
    for (let y = 0; y < rh; y++) for (let x = 0; x < rw; x++) {
        const at = (yy, xx) => smooth[reflectIndex(yy, rh) * rw + reflectIndex(xx, rw)];
        const v = at(y - 1, x) + at(y + 1, x) + at(y, x - 1) + at(y, x + 1) - 4 * smooth[y * rw + x];
        s1 += v; s2 += v * v;
    }
    const n = Math.max(rw * rh, 1);
    const variance = s2 / n - (s1 / n) ** 2;
    return { quality: variance / Math.max(mean, 1e-3) ** 2, cx, cy, extent, brightness: mean };
}

function crop(planes, w, h, cx, cy, cw, ch) {
    let y0 = Math.round(cy - ch / 2), x0 = Math.round(cx - cw / 2);
    y0 = Math.min(Math.max(y0, 0), Math.max(h - ch, 0));
    x0 = Math.min(Math.max(x0, 0), Math.max(w - cw, 0));
    const out = new Uint16Array(cw * ch * 3);
    const tmp = new Float32Array(cw);
    for (let c = 0; c < 3; c++) for (let y = 0; y < ch; y++) {
        const src = (y0 + y) * w + x0;
        for (let x = 0; x < cw; x++) tmp[x] = planes[c][src + x];
        encodeHalf(tmp, out.subarray(c * cw * ch + y * cw, c * cw * ch + (y + 1) * cw));
    }
    return out;
}

// A frame as display-referred RGB planes, from what the source gave
async function framePlanes(frame, env) {
    if (frame.rgba) return { planes: rgbaToPlanes(frame.rgba, frame.width, frame.height, false), width: frame.width, height: frame.height };
    if (frame.ser) return serFramePlanes(frame.ser, frame.raw);
    // A photo from a burst: JPEGs as they are, RAWs stretched to their 99.9th percentile
    const f = await loadFrame(frame.entry, env, { linear: !!frame.entry && frame.entry.kind === 'raw' });
    if (!f.pattern) return { planes: f.planes, width: f.width, height: f.height };
    const planes = [0, 1, 2].map(c => debayerChannel(f.planes[0], f.width, f.height, f.pattern, c));
    applyColour(planes, f.wb, f.camToSrgb);
    const sample = [];
    for (let i = 0; i < planes[1].length; i += 16) for (let c = 0; c < 3; c++) sample.push(planes[c][i]);
    const peak = Math.max(percentile(sample, 99.9), 1e-6);
    planes.forEach(p => { for (let i = 0; i < p.length; i++) p[i] = linearToSrgb(Math.min(Math.max(p[i] / peak, 0), 1)); });
    return { planes, width: f.width, height: f.height };
}

export const LUCKY_TASKS = {
    // Score a frame; with a crop size, also cut the crop around its own centre (float16)
    async luckyFrame({ frame, cropW = null, cropH = null }, env) {
        const img = await framePlanes(frame, env);
        const a = analyse(img.planes, img.width, img.height);
        const result = { ...a, width: img.width, height: img.height };
        if (cropW) {
            result.crop = crop(img.planes, img.width, img.height, a.cx, a.cy, cropW, cropH);
            return { result, transfer: [result.crop.buffer] };
        }
        return { result };
    },

    // Line a crop up with the reference and shift it there (bicubic, NaN outside)
    async luckyAlign({ ref, crop: data, w, h }, env) {
        const toPlanes = (d) => [0, 1, 2].map(c => decodeHalf(d.subarray(c * w * h, (c + 1) * w * h)));
        if (!env.luckyRef || env.luckyRef.key !== ref.key) {
            const planes = toPlanes(ref.data);
            env.luckyRef = { key: ref.key, spectrum: phaseSpectrum(lumaOf(planes), w, h) };
        }
        const planes = toPlanes(data);
        const { dy, dx } = phaseCorrelate(env.luckyRef.spectrum, lumaOf(planes), w, h);
        if (Math.abs(dy) > h / 4 || Math.abs(dx) > w / 4) return { result: { skipped: true } };
        // img(y + dy, x + dx) ~ ref(y, x): sample the crop at (x + dx, y + dy)
        const m = [[1, 0, dx], [0, 1, dy]];
        const shifted = planes.map(p => warpRows(p, w, h, m, w, 0, h, true));
        return { result: { dx, dy, planes: shifted }, transfer: shifted.map(p => p.buffer) };
    }
};
