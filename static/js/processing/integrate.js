// Combining aligned frames pixel by pixel: average, median, sigma clipping or winsorized
// sigma clipping. A port of app/processing/integrate.py: per pixel, the N values are
// sorted, rejection keeps a contiguous run of them (iterating thresholds around the
// median), and the result is the weighted mean of the values inside the thresholds.
// NaN means "no data" (outside a frame's field after alignment).

export const METHODS = ['average', 'median', 'sigma', 'winsorized'];

function sortSmall(a, n) {
    if (n > 32) {
        a.subarray(0, n).sort();
        return;
    }
    for (let i = 1; i < n; i++) {
        const v = a[i];
        let j = i - 1;
        while (j >= 0 && a[j] > v) { a[j + 1] = a[j]; j--; }
        a[j + 1] = v;
    }
}

function spread(s, lo, hi, med, winsorized) {
    const count = Math.max(hi - lo, 1);
    const q1 = lo + Math.floor(0.159 * (count - 1));
    const q2 = lo + Math.ceil(0.841 * (count - 1));
    let sigma = 0.5 * (s[q2] - s[q1]);
    if (winsorized) {
        const a = med - 1.5 * sigma, b = med + 1.5 * sigma;
        let sum = 0;
        for (let i = lo; i < hi; i++) sum += Math.min(Math.max(s[i], a), b);
        const mean = sum / count;
        let v = 0;
        for (let i = lo; i < hi; i++) {
            const d = Math.min(Math.max(s[i], a), b) - mean;
            v += d * d;
        }
        sigma = 1.134 * Math.sqrt(v / Math.max(count - 1, 1));
    }
    return Math.max(sigma, 1e-7);
}

// The rejection thresholds for one pixel's sorted values s[0..n): [low, high]
export function thresholds(s, n, method, kappaLow, kappaHigh, iterations) {
    let lo = 0, hi = n, lowThr = -Infinity, highThr = Infinity;
    const wins = method === 'winsorized';
    for (let it = 0; it < Math.max(1, iterations); it++) {
        if (hi - lo < 3) break;
        const med = 0.5 * (s[(lo + hi - 1) >> 1] + s[(lo + hi) >> 1]);
        const sigma = spread(s, lo, hi, med, wins);
        const newLow = Math.max(med - kappaLow * sigma, lowThr);
        const newHigh = Math.min(med + kappaHigh * sigma, highThr);
        let newLo = 0, newHi = 0;
        for (let i = 0; i < n; i++) {
            if (s[i] < newLow) newLo++;
            if (s[i] <= newHigh) newHi++;
        }
        // Never reject everything: keep the previous run
        if (newHi <= newLo) { newLo = lo; newHi = hi; }
        const changed = newLo !== lo || newHi !== hi;
        lo = newLo; hi = newHi;
        lowThr = newLow; highThr = newHigh;
        if (!changed) break;
    }
    return [lowThr, highThr];
}

// frames: N Float32Arrays of the same length (one strip each). Returns {result,
// coverage, rejectedLow, rejectedHigh} (the last two are totals over the strip).
export function combine(frames, { method = 'sigma', kappaLow = 3, kappaHigh = 3, iterations = 3, weights = null } = {}) {
    if (!METHODS.includes(method)) throw new Error(`Unknown integration method: ${method}`);
    const n = frames.length;
    const size = n ? frames[0].length : 0;
    const result = new Float32Array(size);
    const coverage = new Uint16Array(size);
    const w = weights || null;
    const plain = method === 'average' || (n < 3 && method !== 'median');
    const buf = new Float64Array(n);
    let rejectedLow = 0, rejectedHigh = 0;

    for (let p = 0; p < size; p++) {
        let cnt = 0;
        for (let k = 0; k < n; k++) {
            const v = frames[k][p];
            if (v === v) buf[cnt++] = v;
        }
        coverage[p] = cnt;
        let low = -Infinity, high = Infinity;
        if (!plain && cnt) {
            sortSmall(buf, cnt);
            if (method === 'median') {
                result[p] = 0.5 * (buf[(cnt - 1) >> 1] + buf[cnt >> 1]);
                continue;
            }
            [low, high] = thresholds(buf, cnt, method, kappaLow, kappaHigh, iterations);
        } else if (method === 'median' && !cnt) {
            result[p] = NaN;
            continue;
        }
        let total = 0, wsum = 0;
        for (let k = 0; k < n; k++) {
            const v = frames[k][p];
            if (v !== v) continue;
            if (v < low) { rejectedLow++; continue; }
            if (v > high) { rejectedHigh++; continue; }
            const wk = w ? w[k] : 1;
            total += v * wk;
            wsum += wk;
        }
        result[p] = total / wsum;
    }
    return { result, coverage, rejectedLow, rejectedHigh };
}

// Match each frame's background level and spread to the reference, in place:
// x' = (x - loc_i) * (refScale / scale_i) + refLoc
export function normaliseStrip(frames, loc, scale, refLoc, refScale) {
    frames.forEach((f, i) => {
        const k = scale[i] > 0 ? refScale / scale[i] : 1;
        const l = loc[i];
        for (let p = 0; p < f.length; p++) f[p] = (f[p] - l) * k + refLoc;
    });
    return frames;
}

// Rows of an (N, rows, width) strip that fit in `share` of a memory budget
export function stripRowsFor(nFrames, width, budgetBytes, share = 0.5) {
    const perRow = width * (nFrames * 4 + 24);
    return Math.floor(Math.max(8, Math.min(4096, (budgetBytes * share) / Math.max(perRow, 1))));
}
