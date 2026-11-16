// Resampling a frame onto the reference: for each output pixel (x, y), sample the frame
// at M(x, y) with Keys cubic (a = -0.5) or bilinear interpolation. Outside the frame
// is NaN ("no data"), which integration skips. Output comes a strip of rows at a time,
// so a full frame never needs a second float32 copy.

function keys(t) {
    const a = -0.5;
    t = Math.abs(t);
    if (t < 1) return ((a + 2) * t - (a + 3)) * t * t + 1;
    if (t < 2) return ((a * t - 5 * a) * t + 8 * a) * t - 4 * a;
    return 0;
}

// The four Keys weights for every 1/1024 of a pixel (far finer than the noise)
const STEPS = 1024;
const KEYS = (() => {
    const t = new Float32Array((STEPS + 1) * 4);
    for (let i = 0; i <= STEPS; i++) for (let k = 0; k < 4; k++) t[i * 4 + k] = keys(i / STEPS - (k - 1));
    return t;
})();

export function isIdentity(m) {
    return Math.abs(m[0][0] - 1) < 1e-12 && Math.abs(m[0][1]) < 1e-12 && Math.abs(m[0][2]) < 1e-9
        && Math.abs(m[1][0]) < 1e-12 && Math.abs(m[1][1] - 1) < 1e-12 && Math.abs(m[1][2]) < 1e-9;
}

// Rows [y0, y1) of the warped plane into out (length (y1 - y0) * outW)
export function warpRows(src, w, h, m, outW, y0, y1, cubic = true, out = new Float32Array((y1 - y0) * outW)) {
    const [[a, b, c], [d, e, f]] = m;
    const eps = 1e-6, maxX = w - 1 + eps, maxY = h - 1 + eps;
    const K = KEYS;
    for (let y = y0; y < y1; y++) {
        const orow = (y - y0) * outW;
        // Step along the row instead of multiplying for every pixel
        let sx = b * y + c, sy = e * y + f;
        for (let x = 0; x < outW; x++, sx += a, sy += d) {
            if (!(sx >= -eps && sy >= -eps && sx <= maxX && sy <= maxY)) {
                out[orow + x] = NaN;
                continue;
            }
            const ix = Math.floor(sx), iy = Math.floor(sy);
            const fx = sx - ix, fy = sy - iy;
            if (!cubic) {
                const x1 = Math.min(ix + 1, w - 1), y1r = Math.min(iy + 1, h - 1);
                const xa = Math.max(ix, 0), ya = Math.max(iy, 0);
                const p00 = src[ya * w + xa], p01 = src[ya * w + x1], p10 = src[y1r * w + xa], p11 = src[y1r * w + x1];
                out[orow + x] = (p00 * (1 - fx) + p01 * fx) * (1 - fy) + (p10 * (1 - fx) + p11 * fx) * fy;
                continue;
            }
            const ox = ((fx * STEPS + 0.5) | 0) << 2, oy = ((fy * STEPS + 0.5) | 0) << 2;
            const wx0 = K[ox], wx1 = K[ox + 1], wx2 = K[ox + 2], wx3 = K[ox + 3];
            const wy0 = K[oy], wy1 = K[oy + 1], wy2 = K[oy + 2], wy3 = K[oy + 3];
            if (ix >= 1 && iy >= 1 && ix + 2 < w && iy + 2 < h) {
                let r = (iy - 1) * w + ix - 1;
                let v = wy0 * (wx0 * src[r] + wx1 * src[r + 1] + wx2 * src[r + 2] + wx3 * src[r + 3]);
                r += w;
                v += wy1 * (wx0 * src[r] + wx1 * src[r + 1] + wx2 * src[r + 2] + wx3 * src[r + 3]);
                r += w;
                v += wy2 * (wx0 * src[r] + wx1 * src[r + 1] + wx2 * src[r + 2] + wx3 * src[r + 3]);
                r += w;
                v += wy3 * (wx0 * src[r] + wx1 * src[r + 1] + wx2 * src[r + 2] + wx3 * src[r + 3]);
                out[orow + x] = v;
                continue;
            }
            // Near the edges: clamp the neighbours
            const wx = [wx0, wx1, wx2, wx3], wy = [wy0, wy1, wy2, wy3];
            let v = 0;
            for (let j = 0; j < 4; j++) {
                const yy = Math.min(Math.max(iy - 1 + j, 0), h - 1);
                let r = 0;
                for (let k = 0; k < 4; k++) r += wx[k] * src[yy * w + Math.min(Math.max(ix - 1 + k, 0), w - 1)];
                v += wy[j] * r;
            }
            out[orow + x] = v;
        }
    }
    return out;
}

// The rows of the source that rows [y0, y1) of the output read from (for streaming a
// source in strips): [first, last] inclusive, clamped
export function sourceRowsFor(m, outW, y0, y1, h, margin = 2) {
    const [, [d, e, f]] = m;
    const ys = [];
    for (const x of [0, outW - 1]) for (const y of [y0, y1 - 1]) ys.push(d * x + e * y + f);
    return [Math.max(0, Math.floor(Math.min(...ys)) - margin), Math.min(h - 1, Math.ceil(Math.max(...ys)) + margin)];
}
