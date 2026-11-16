// Calibration: flats normalised by their level, hot pixels found in the master dark,
// stray hot pixels in each light, and light - dark (or bias), / flat. A port of the
// calibration parts of app/processing/stack.py. RAW frames calibrate on the Bayer
// mosaic (one plane, `pattern` set); JPEGs on three RGB planes.
//
// A frame here is {planes: [Float32Array...], width, height, pattern|null}.

import { median, mirrorIndex, robustStats, subsample } from './image.js';

// Divide a flat by its own level: per Bayer position, per RGB channel, or overall
export function normaliseFlat(frame, perChannel = true) {
    const { planes, width: w, height: h, pattern } = frame;
    if (pattern && perChannel) {
        const p = planes[0];
        for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
            // plane[::4, ::4] of the dy::2, dx::2 sub-image
            const vals = [];
            for (let y = dy; y < h; y += 8) for (let x = dx; x < w; x += 8) vals.push(p[y * w + x]);
            const level = median(vals);
            if (level > 0) for (let y = dy; y < h; y += 2) for (let x = dx; x < w; x += 2) p[y * w + x] /= level;
        }
        return frame;
    }
    if (planes.length === 3 && perChannel) {
        planes.forEach(p => {
            const level = median(subsample(p, w, h, 4));
            if (level > 0) for (let i = 0; i < p.length; i++) p[i] /= level;
        });
        return frame;
    }
    const level = median(subsample(planes[0], w, h, 4));
    if (level > 0) planes.forEach(p => { for (let i = 0; i < p.length; i++) p[i] /= level; });
    return frame;
}

// Indices of pixels far brighter than the rest of the master dark (per Bayer colour)
export function hotPixelMap(dark, k = 8.0) {
    const { planes, width: w, height: h, pattern } = dark;
    const hot = [];
    if (planes.length === 3) {
        const lum = new Float32Array(w * h);
        for (let i = 0; i < lum.length; i++) lum[i] = (planes[0][i] + planes[1][i] + planes[2][i]) / 3;
        const [med, sd] = robustStats(subsample(lum, w, h, 3));
        const thr = med + k * Math.max(sd, 1e-7);
        for (let i = 0; i < lum.length; i++) if (lum[i] > thr) hot.push(i);
        return Int32Array.from(hot);
    }
    const p = planes[0];
    for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
        const vals = [];
        // plane[::3, ::3] of the dy::2, dx::2 sub-image
        for (let y = dy; y < h; y += 6) for (let x = dx; x < w; x += 6) vals.push(p[y * w + x]);
        const [med, sd] = robustStats(vals);
        const thr = med + k * Math.max(sd, 1e-7);
        for (let y = dy; y < h; y += 2) for (let x = dx; x < w; x += 2) if (p[y * w + x] > thr) hot.push(y * w + x);
    }
    hot.sort((a, b) => a - b);
    return Int32Array.from(hot);
}

// Replace the listed pixels with the mean of their same-colour neighbours (not
// themselves listed), in every plane
export function fixPixels(frame, indices, bayer) {
    const { planes, width: w, height: h } = frame;
    if (!indices.length) return frame;
    const listed = new Set(indices);
    const step = bayer ? 2 : 1;
    const nb = [[-step, 0], [step, 0], [0, -step], [0, step]];
    const values = planes.map(() => new Float32Array(indices.length));
    const have = new Uint8Array(indices.length);
    indices.forEach((idx, n) => {
        const y = Math.floor(idx / w), x = idx - y * w;
        const sums = planes.map(() => 0);
        let count = 0;
        for (const [dy, dx] of nb) {
            const yy = y + dy, xx = x + dx;
            if (yy < 0 || yy >= h || xx < 0 || xx >= w) continue;
            const j = yy * w + xx;
            if (listed.has(j)) continue;
            planes.forEach((p, c) => { sums[c] += p[j]; });
            count++;
        }
        if (count) {
            have[n] = 1;
            planes.forEach((_, c) => { values[c][n] = sums[c] / count; });
        }
    });
    indices.forEach((idx, n) => {
        if (have[n]) planes.forEach((p, c) => { p[idx] = values[c][n]; });
    });
    return frame;
}

// Single hot pixels in a mosaic: far above the median of their 8 neighbours while
// every neighbour stays near it (stars light up their neighbours too)
export function cosmeticMask(data, w, h, noise, k = 10.0) {
    const out = [];
    const ring = new Float64Array(8);
    const thr = k * noise;
    const offsets = [-w - 1, -w, -w + 1, -1, 1, w - 1, w, w + 1];
    for (let y = 0; y < h; y++) {
        const inner = y > 0 && y < h - 1;
        for (let x = 0; x < w; x++) {
            const i = y * w + x;
            const v = data[i];
            let nbMax = -Infinity;
            if (inner && x > 0 && x < w - 1) {
                // nbMax - local < 0.15 (v - local) with local <= nbMax needs v > nbMax: a
                // cheap test that skips the median for nearly every pixel
                for (let n = 0; n < 8; n++) {
                    const u = data[i + offsets[n]];
                    if (u > nbMax) nbMax = u;
                }
                if (!(v > nbMax)) continue;
                for (let n = 0; n < 8; n++) ring[n] = data[i + offsets[n]];
            } else {
                let n = 0;
                for (let dy = -1; dy <= 1; dy++) {
                    const yy = mirrorIndex(y + dy, h);
                    for (let dx = -1; dx <= 1; dx++) {
                        if (!dy && !dx) continue;
                        const u = data[yy * w + mirrorIndex(x + dx, w)];
                        ring[n++] = u;
                        if (u > nbMax) nbMax = u;
                    }
                }
                if (!(v > nbMax)) continue;
            }
            const local = median(ring, true);
            const excess = v - local;
            if (excess > thr && nbMax - local < 0.15 * excess) out.push(i);
        }
    }
    return Int32Array.from(out);
}

// Light - dark (scaled by exposure when there's a bias and the exposures differ), or
// - bias; / flat; hot pixels fixed; stray hot pixels fixed on RAW. `masters` gives rows
// of each master: rows(name, y0, y1) -> Float32Array planes (or null).
export async function calibrateFrame(frame, masters, { exposure = null, darkScaling = true, cosmetic = true, rowsPerStrip = 256 } = {}) {
    const { planes, width: w, height: h, pattern } = frame;
    const info = masters.info || {};
    const has = (name) => !!(info[name]);
    const scaled = has('dark') && darkScaling && has('bias') && exposure && info.dark.exposure_s
        && Math.abs(exposure - info.dark.exposure_s) / info.dark.exposure_s > 0.02;
    const factor = scaled ? exposure / info.dark.exposure_s : 1;
    if (has('dark') || has('bias') || has('flat')) {
        for (let y0 = 0; y0 < h; y0 += rowsPerStrip) {
            const y1 = Math.min(h, y0 + rowsPerStrip);
            const dark = has('dark') ? await masters.rows('dark', y0, y1) : null;
            const bias = (scaled || !has('dark')) && has('bias') ? await masters.rows('bias', y0, y1) : null;
            const flat = has('flat') ? await masters.rows('flat', y0, y1) : null;
            planes.forEach((p, c) => {
                const off = y0 * w;
                const n = (y1 - y0) * w;
                const d = dark && dark[c], b = bias && bias[c], f = flat && flat[c];
                for (let i = 0; i < n; i++) {
                    let v = p[off + i];
                    if (d) {
                        if (scaled) v = v - b[i] - (d[i] - b[i]) * factor;
                        else v -= d[i];
                    } else if (b) v -= b[i];
                    if (f) v /= Math.max(f[i], 0.05);
                    p[off + i] = v;
                }
            });
        }
    }
    const bayer = !!pattern;
    if (masters.hot && masters.hot.length) fixPixels(frame, masters.hot, bayer);
    if (cosmetic && bayer) {
        const noise = Math.max(robustStats(subsample(planes[0], w, h, 7))[1], 1e-6);
        fixPixels(frame, cosmeticMask(planes[0], w, h, noise), bayer);
    }
    return frame;
}
