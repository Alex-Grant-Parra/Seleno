// Focus meter: the size of the stars in the live view picture, for focusing by eye.
// It finds the brightest stars in the frame and measures each one's half-flux radius
// (HFR: the flux-weighted mean distance from the star's centre), which shrinks as
// focus improves and grows again past it. Works on the 8-bit display picture, so the
// numbers compare frame to frame at one zoom level rather than being exact sizes.
(function () {
    'use strict';

    const MAX_STARS = 12;
    const SATURATED = 250;
    const CELL = 8;

    // Median and robust spread of 8-bit values, from a histogram
    function histogramStats(values, step) {
        const hist = new Uint32Array(256);
        let n = 0;
        for (let i = 0; i < values.length; i += step) {
            hist[values[i]]++;
            n++;
        }
        const half = n / 2;
        let acc = 0;
        let median = 0;
        for (let v = 0; v < 256; v++) {
            acc += hist[v];
            if (acc >= half) { median = v; break; }
        }
        const dev = new Uint32Array(256);
        for (let v = 0; v < 256; v++) dev[Math.abs(v - median)] += hist[v];
        acc = 0;
        let mad = 0;
        for (let d = 0; d < 256; d++) {
            acc += dev[d];
            if (acc >= half) { mad = d; break; }
        }
        return { median, sigma: Math.max(0.6, 1.4826 * mad), hist, n };
    }

    function luminance(data, count) {
        const lum = new Uint8Array(count);
        for (let i = 0, j = 0; i < count; i++, j += 4) {
            lum[i] = (data[j] * 54 + data[j + 1] * 183 + data[j + 2] * 19) >> 8;
        }
        return lum;
    }

    function measureStar(lum, w, h, px, py, bg, sigma) {
        let r = 8;
        for (let attempt = 0; attempt < 4; attempt++) {
            if (px - r < 1 || py - r < 1 || px + r >= w - 1 || py + r >= h - 1) return null;
            // Local background from the window's edge
            const ring = [];
            for (let dy = -r; dy <= r; dy++) {
                for (let dx = -r; dx <= r; dx++) {
                    const d2 = dx * dx + dy * dy;
                    if (d2 > (r - 1.5) * (r - 1.5) && d2 <= r * r) ring.push(lum[(py + dy) * w + px + dx]);
                }
            }
            ring.sort((a, b) => a - b);
            const local = ring.length ? ring[ring.length >> 1] : bg;
            let flux = 0, sx = 0, sy = 0, edge = 0;
            for (let dy = -r; dy <= r; dy++) {
                for (let dx = -r; dx <= r; dx++) {
                    const d2 = dx * dx + dy * dy;
                    if (d2 > r * r) continue;
                    const v = lum[(py + dy) * w + px + dx] - local;
                    if (v <= sigma) continue;
                    flux += v;
                    sx += v * dx;
                    sy += v * dy;
                    if (d2 > (r - 2) * (r - 2)) edge += v;
                }
            }
            if (flux <= 0) return null;
            // A star spilling past the window (defocused, a donut) needs a bigger one
            if (edge > 0.12 * flux && r < 40) {
                r = Math.min(40, Math.round(r * 1.6));
                continue;
            }
            const cx = px + sx / flux;
            const cy = py + sy / flux;
            let total = 0, sumD = 0, sumD2 = 0, peak = 0;
            const x0 = Math.round(cx), y0 = Math.round(cy);
            for (let dy = -r; dy <= r; dy++) {
                for (let dx = -r; dx <= r; dx++) {
                    const x = x0 + dx, y = y0 + dy;
                    if (x < 0 || y < 0 || x >= w || y >= h) continue;
                    const raw = lum[y * w + x];
                    const v = raw - local;
                    const ddx = x - cx, ddy = y - cy;
                    const d = Math.sqrt(ddx * ddx + ddy * ddy);
                    if (d > r || v <= 0) continue;
                    total += v;
                    sumD += v * d;
                    sumD2 += v * d * d;
                    if (d <= 1.5 && raw > peak) peak = raw;
                }
            }
            if (total < 6 * sigma) return null;
            const hfr = sumD / total;
            if (hfr < 0.5) return null;          // a hot pixel
            return {
                x: cx,
                y: cy,
                hfr,
                fwhm: 2.3548 * Math.sqrt(sumD2 / (2 * total)),
                peak,
                flux: total,
                saturated: peak >= SATURATED,
                radius: r
            };
        }
        return null;
    }

    function measure(imageData) {
        const { data, width: w, height: h } = imageData;
        const count = w * h;
        const lum = luminance(data, count);
        const stats = histogramStats(lum, 5);
        let clipped = 0;
        for (let v = SATURATED; v < 256; v++) clipped += stats.hist[v];
        const result = {
            width: w,
            height: h,
            background: stats.median / 255,
            noise: stats.sigma / 255,
            clippedPct: stats.n ? (100 * clipped) / stats.n : 0,
            stars: [],
            hfr: null,
            fwhm: null,
            peak: null
        };
        const threshold = stats.median + Math.max(6 * stats.sigma, 14);

        // The brightest pixel of each cell, kept if it clears the threshold
        const candidates = [];
        for (let cy = 0; cy < h; cy += CELL) {
            for (let cx = 0; cx < w; cx += CELL) {
                let best = -1, bx = 0, by = 0;
                const ymax = Math.min(h, cy + CELL), xmax = Math.min(w, cx + CELL);
                for (let y = cy; y < ymax; y++) {
                    const row = y * w;
                    for (let x = cx; x < xmax; x++) {
                        const v = lum[row + x];
                        if (v > best) { best = v; bx = x; by = y; }
                    }
                }
                if (best > threshold) candidates.push({ x: bx, y: by, v: best });
            }
        }
        candidates.sort((a, b) => b.v - a.v);
        const accepted = [];
        for (const c of candidates) {
            if (accepted.length >= MAX_STARS * 2) break;
            if (accepted.some(a => (a.x - c.x) ** 2 + (a.y - c.y) ** 2 < 144)) continue;
            accepted.push(c);
        }
        for (const c of accepted) {
            if (result.stars.length >= MAX_STARS) break;
            const star = measureStar(lum, w, h, c.x, c.y, stats.median, stats.sigma);
            if (!star) continue;
            if (result.stars.some(s => (s.x - star.x) ** 2 + (s.y - star.y) ** 2 < (s.radius + 2) ** 2)) continue;
            result.stars.push(star);
        }
        if (result.stars.length) {
            const unsaturated = result.stars.filter(s => !s.saturated);
            const use = unsaturated.length >= 3 ? unsaturated : result.stars;
            const median = (values) => {
                const v = values.slice().sort((a, b) => a - b);
                const m = v.length >> 1;
                return v.length % 2 ? v[m] : 0.5 * (v[m - 1] + v[m]);
            };
            result.hfr = median(use.map(s => s.hfr));
            result.fwhm = median(use.map(s => s.fwhm));
            result.peak = Math.max(...result.stars.map(s => s.peak)) / 255;
        }
        return result;
    }

    // Reads a canvas (the live view player's) and measures it
    function measureCanvas(canvas) {
        if (!canvas || !canvas.width || !canvas.height) return null;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        let imageData;
        try {
            imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
        } catch (error) {
            return null;
        }
        return measure(imageData);
    }

    window.FocusMeter = { measure, measureCanvas };
})();
