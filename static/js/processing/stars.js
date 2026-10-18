// Finding stars and measuring them: position, flux, HFR, FWHM and eccentricity, with
// adaptive Gaussian moments. A port of app/processing/stars.py (see there for why each
// step is the way it is; the comments here are short).

import { backgroundMap, gaussianFilter, maximumFilter, median, robustStats, subsample } from './image.js';

export const GAUSS_FWHM = 2.0 * Math.sqrt(2.0 * Math.log(2.0));

const KEYS = ['x', 'y', 'flux', 'peak', 'snr', 'hfr', 'fwhm', 'ecc'];

function emptyResult(background, noise) {
    const out = { saturated: [], background, noise };
    KEYS.forEach(k => { out[k] = []; });
    return out;
}

// Stars in a monochrome image {data, width, height}. Returns arrays (x, y, flux, peak,
// snr, hfr, fwhm, ecc, saturated) brightest first, plus background and noise.
export function detectStars(image, { k = 5.0, maxStars = 1500, saturation = null, radius = null, minFwhm = 0.9, background = null } = {}) {
    const { data: img, width: w, height: h } = image;
    const bg = background || backgroundMap(img, w, h);
    const data = new Float32Array(w * h);
    for (let i = 0; i < data.length; i++) data[i] = img[i] - bg[i];
    const noise = Math.max(robustStats(subsample(data, w, h, 3))[1], 1e-7);
    const smooth = gaussianFilter(data, w, h, 1.0);
    const noiseS = Math.max(robustStats(subsample(smooth, w, h, 3))[1], 1e-7);
    const mx = maximumFilter(smooth, w, h, 5);
    const bgLevel = median(subsample(bg, w, h, 4));

    const thr = k * noiseS;
    const ys = [], xs = [];
    for (let y = 0; y < h; y++) {
        const row = y * w;
        for (let x = 0; x < w; x++) {
            const v = smooth[row + x];
            if (v > thr && v === mx[row + x]) { ys.push(y); xs.push(x); }
        }
    }
    if (!ys.length) return emptyResult(bgLevel, noise);

    const ctx = { img, data, smooth, w, h, noise, ys, xs, k, maxStars, saturation, minFwhm, bgLevel };
    if (radius !== null) return measure(ctx, Math.round(radius));
    let r = 6;
    for (;;) {
        const result = measure(ctx, r);
        const capFwhm = GAUSS_FWHM * r / 2.5;
        const n = result.fwhm.length;
        if (r >= 24 || n < 3) return result;
        if (median(result.fwhm.slice(0, Math.max(3, n >> 1))) < 0.8 * capFwhm) return result;
        r = Math.min(24, r * 2);
    }
}

function measure(ctx, r) {
    const { img, data, smooth, w, h, noise, k, maxStars, saturation, minFwhm, bgLevel } = ctx;
    const empty = emptyResult(bgLevel, noise);
    let cand = [];
    for (let i = 0; i < ctx.ys.length; i++) {
        const y = ctx.ys[i], x = ctx.xs[i];
        if (y >= r && y < h - r && x >= r && x < w - r) cand.push(i);
    }
    if (!cand.length) return empty;
    cand.sort((a, b) => smooth[ctx.ys[b] * w + ctx.xs[b]] - smooth[ctx.ys[a] * w + ctx.xs[a]]);
    cand = cand.slice(0, maxStars * 3);

    const side = 2 * r + 1;
    const n = side * side;
    const ox = new Float64Array(n), oy = new Float64Array(n);
    const ringIdx = [];
    for (let j = 0; j < side; j++) for (let i = 0; i < side; i++) {
        const p = j * side + i;
        ox[p] = i - r;
        oy[p] = j - r;
        if (Math.hypot(oy[p], ox[p]) > r - 1.5) ringIdx.push(p);
    }
    const ringBuf = new Float64Array(ringIdx.length);
    const win = new Float64Array(n);
    const sat = saturation === null || saturation === undefined ? Infinity : saturation;
    const stars = [];

    for (const ci of cand) {
        const ys = ctx.ys[ci], xs = ctx.xs[ci];
        for (let j = 0; j < side; j++) {
            const row = (ys - r + j) * w + xs - r;
            for (let i = 0; i < side; i++) win[j * side + i] = data[row + i];
        }
        for (let q = 0; q < ringIdx.length; q++) ringBuf[q] = win[ringIdx[q]];
        const localBg = median(ringBuf, true);
        let peak = -Infinity;
        for (let p = 0; p < n; p++) win[p] -= localBg;
        for (let j = r - 1; j <= r + 1; j++) for (let i = r - 1; i <= r + 1; i++) peak = Math.max(peak, win[j * side + i]);
        if (!(peak > k * noise)) continue;

        // Centroid from the pixels above the noise
        let f0 = 0, sx = 0, sy = 0;
        for (let p = 0; p < n; p++) {
            const v = win[p] > noise ? win[p] : 0;
            f0 += v;
            sx += v * ox[p];
            sy += v * oy[p];
        }
        f0 = Math.max(f0, 1e-12);
        const m = adaptiveMoments(win, ox, oy, sx / f0, sy / f0, r);
        const tr = m.sxx + m.syy;
        const det = m.sxx * m.syy - m.sxy * m.sxy;
        const disc = Math.sqrt(Math.max((tr * 0.5) ** 2 - det, 0));
        const l1 = Math.max(tr * 0.5 + disc, 1e-6);
        const l2 = Math.max(tr * 0.5 - disc, 1e-6);
        const fwhm = GAUSS_FWHM * Math.sqrt(Math.sqrt(l1 * l2));
        const ecc = Math.sqrt(Math.min(Math.max(1 - l2 / l1, 0), 1));

        const aperture = Math.min(Math.max(3 * Math.sqrt(l1), 2), r);
        let flux = 0, hsum = 0;
        for (let p = 0; p < n; p++) {
            const d = Math.hypot(ox[p] - m.cx, oy[p] - m.cy);
            if (d <= aperture) {
                const v = Math.max(win[p], 0);
                flux += v;
                hsum += v * d;
            }
        }
        flux = Math.max(flux, 1e-12);
        if (!(Number.isFinite(fwhm) && fwhm >= minFwhm && fwhm < 4 * r)) continue;
        stars.push({
            x: xs + m.cx, y: ys + m.cy, flux, peak, snr: peak / noise, hfr: hsum / flux, fwhm, ecc,
            saturated: img[ys * w + xs] >= sat
        });
    }
    stars.sort((a, b) => b.flux - a.flux);
    const kept = stars.slice(0, maxStars);
    const out = { saturated: kept.map(s => s.saturated), background: bgLevel, noise };
    KEYS.forEach(key => { out[key] = kept.map(s => s[key]); });
    return out;
}

// Covariance of a star's Gaussian from moments weighted by a matched Gaussian window,
// corrected for the window: C_star = (C_meas^-1 - C_window^-1)^-1
function adaptiveMoments(win, ox, oy, cx, cy, r, iterations = 5) {
    let sxx = 2, syy = 2, sxy = 0;
    const cap = Math.max((r / 2.5) ** 2, 1);
    const cx0 = cx, cy0 = cy;
    const n = win.length;
    const wgs = new Float64Array(n);
    for (let it = 0; it < iterations; it++) {
        const sw2 = Math.min(Math.max(2 * (sxx + syy) * 0.5, 0.6), cap);
        let f = 0, mx = 0, my = 0;
        for (let p = 0; p < n; p++) {
            const dx = ox[p] - cx, dy = oy[p] - cy;
            const d2 = dx * dx + dy * dy;
            const wg = d2 > 9 * sw2 ? 0 : win[p] * Math.exp(-d2 / (2 * sw2));
            wgs[p] = wg;
            f += wg;
            mx += wg * dx;
            my += wg * dy;
        }
        f = Math.max(f, 1e-12);
        mx = Math.min(Math.max(mx / f, -1), 1);
        my = Math.min(Math.max(my / f, -1), 1);
        // Second moments about the shifted centre, with this iteration's weights
        let mxx = 0, myy = 0, mxy = 0;
        for (let p = 0; p < n; p++) {
            const wg = wgs[p];
            if (!wg) continue;
            const dx = ox[p] - cx - mx, dy = oy[p] - cy - my;
            mxx += wg * dx * dx;
            myy += wg * dy * dy;
            mxy += wg * dx * dy;
        }
        cx = Math.min(Math.max(cx + mx, cx0 - 1.5), cx0 + 1.5);
        cy = Math.min(Math.max(cy + my, cy0 - 1.5), cy0 + 1.5);
        mxx /= f; myy /= f; mxy /= f;
        const d = Math.max(mxx * myy - mxy * mxy, 1e-9);
        const ixx = myy / d - 1 / sw2, iyy = mxx / d - 1 / sw2, ixy = -mxy / d;
        const det2 = ixx * iyy - ixy * ixy;
        if (ixx <= 0 || iyy <= 0 || det2 <= 1e-9) {
            sxx = mxx * 2; syy = myy * 2; sxy = mxy * 2;
        } else {
            sxx = iyy / det2; syy = ixx / det2; sxy = -ixy / det2;
        }
    }
    sxx = Math.min(Math.max(sxx - 1 / 12, 0.05), cap);
    syy = Math.min(Math.max(syy - 1 / 12, 0.05), cap);
    return { sxx, syy, sxy, cx, cy };
}

// Frame-level numbers: medians over the well-exposed, unsaturated stars, sizes times
// pixelScale (stars.summarise)
export function summarise(stars, pixelScale = 1.0, minSnr = 10.0) {
    let idx = stars.x.map((_, i) => i);
    const streak = idx.filter(i => stars.ecc[i] > 0.85);
    if (idx.length && streak.length / idx.length < 0.5) idx = idx.filter(i => !(stars.ecc[i] > 0.85));
    const n = idx.length;
    let good = idx.filter(i => !stars.saturated[i] && stars.snr[i] >= 3 * minSnr);
    if (good.length < 10) good = idx.filter(i => !stars.saturated[i] && stars.snr[i] >= minSnr);
    if (good.length < 3) good = idx.filter(i => !stars.saturated[i]);
    if (good.length < 3) good = idx;
    const med = (key, scale = 1) => (good.length ? median(good.map(i => stars[key][i])) * scale : null);
    return {
        stars: n,
        fwhm: med('fwhm', pixelScale),
        hfr: med('hfr', pixelScale),
        ecc: med('ecc'),
        snr: med('snr'),
        background: stars.background,
        noise: stars.noise,
        saturated_stars: idx.filter(i => stars.saturated[i]).length
    };
}
