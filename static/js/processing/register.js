// Lining frames up. A port of app/processing/register.py.
//
// Star fields: triangles of bright stars matched by shape (side ratios); each match
// proposes a transform, the one most stars agree with wins, and least squares over all
// matched stars refines it. The Moon and planets: phase correlation.
//
// A transform is a 2x3 matrix M (array of two rows) taking reference (x, y) to the
// frame's: [x', y'] = M [x, y, 1].

export class RegistrationError extends Error {
    constructor(message) {
        super(message);
        this.name = 'RegistrationError';
    }
}

export const IDENTITY = [[1, 0, 0], [0, 1, 0]];

export function applyTransform(m, x, y) {
    return [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]];
}

// Rotation, uniform scale and shift taking src to dst, least squares, no mirroring
// (Umeyama's solution, which in 2-D has this closed form)
export function estimateSimilarity(src, dst) {
    const n = src.length;
    let msx = 0, msy = 0, mdx = 0, mdy = 0;
    for (let i = 0; i < n; i++) {
        msx += src[i][0]; msy += src[i][1]; mdx += dst[i][0]; mdy += dst[i][1];
    }
    msx /= n; msy /= n; mdx /= n; mdy /= n;
    let a = 0, b = 0, v = 0;
    for (let i = 0; i < n; i++) {
        const sx = src[i][0] - msx, sy = src[i][1] - msy;
        const dx = dst[i][0] - mdx, dy = dst[i][1] - mdy;
        a += sx * dx + sy * dy;
        b += sx * dy - sy * dx;
        v += sx * sx + sy * sy;
    }
    if (v / n <= 1e-12) throw new RegistrationError('degenerate points');
    const c = a / v, s = b / v;     // scale * (cos, sin)
    return [[c, -s, mdx - (c * msx - s * msy)], [s, c, mdy - (s * msx + c * msy)]];
}

function solve3(m, r) {
    // Gaussian elimination with partial pivoting on a 3x3 system
    const a = m.map((row, i) => [...row, r[i]]);
    for (let col = 0; col < 3; col++) {
        let p = col;
        for (let i = col + 1; i < 3; i++) if (Math.abs(a[i][col]) > Math.abs(a[p][col])) p = i;
        [a[col], a[p]] = [a[p], a[col]];
        if (Math.abs(a[col][col]) < 1e-12) throw new RegistrationError('degenerate points');
        for (let i = col + 1; i < 3; i++) {
            const f = a[i][col] / a[col][col];
            for (let j = col; j < 4; j++) a[i][j] -= f * a[col][j];
        }
    }
    const x = [0, 0, 0];
    for (let i = 2; i >= 0; i--) {
        let s = a[i][3];
        for (let j = i + 1; j < 3; j++) s -= a[i][j] * x[j];
        x[i] = s / a[i][i];
    }
    return x;
}

export function estimateAffine(src, dst) {
    const ata = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    const bx = [0, 0, 0], by = [0, 0, 0];
    for (let i = 0; i < src.length; i++) {
        const row = [src[i][0], src[i][1], 1];
        for (let j = 0; j < 3; j++) {
            for (let k = 0; k < 3; k++) ata[j][k] += row[j] * row[k];
            bx[j] += row[j] * dst[i][0];
            by[j] += row[j] * dst[i][1];
        }
    }
    return [solve3(ata, bx), solve3(ata, by)];
}

export function transformScale(m) {
    return Math.sqrt(Math.abs(m[0][0] * m[1][1] - m[0][1] * m[1][0]));
}

export function transformRotationDeg(m) {
    return Math.atan2(m[1][0], m[0][0]) * 180 / Math.PI;
}

export function invertTransform(m) {
    const [[a, b, tx], [c, d, ty]] = m;
    const det = a * d - b * c;
    const ia = d / det, ib = -b / det, ic = -c / det, id = a / det;
    return [[ia, ib, -(ia * tx + ib * ty)], [ic, id, -(ic * tx + id * ty)]];
}

// M measured on images binned by `factor` (pixel centres at factor*i + offset in the
// full image) as a full-resolution transform
export function scaleTransform(m, factor, offset = 0) {
    const c = offset;
    return [0, 1].map(i => [m[i][0], m[i][1], factor * m[i][2] + c - (m[i][0] * c + m[i][1] * c)]);
}

// --- triangles ----------------------------------------------------------------------

function triangles(xy, neighbours = 5) {
    const n = xy.length;
    if (n < 3) return { tris: [], inv: [] };
    const k = Math.min(neighbours + 1, n);
    const seen = new Set();
    const list = [];
    for (let i = 0; i < n; i++) {
        const near = xy.map((p, j) => [Math.hypot(p[0] - xy[i][0], p[1] - xy[i][1]), j])
            .sort((a, b) => a[0] - b[0] || a[1] - b[1]).slice(0, k).map(d => d[1]).filter(j => j !== i).slice(0, k - 1);
        for (let a = 0; a < near.length; a++) for (let b = a + 1; b < near.length; b++) {
            const t = [i, near[a], near[b]].sort((p, q) => p - q);
            const key = t.join(',');
            if (!seen.has(key)) {
                seen.add(key);
                list.push(t);
            }
        }
    }
    list.sort((p, q) => p[0] - q[0] || p[1] - q[1] || p[2] - q[2]);
    const tris = [], inv = [];
    for (const t of list) {
        const p = t.map(i => xy[i]);
        // The side opposite each vertex
        const sides = [
            [Math.hypot(p[1][0] - p[2][0], p[1][1] - p[2][1]), t[0]],
            [Math.hypot(p[0][0] - p[2][0], p[0][1] - p[2][1]), t[1]],
            [Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]), t[2]]
        ].sort((a, b) => a[0] - b[0]);
        if (!(sides[2][0] > 8)) continue;
        const i0 = sides[0][0] / Math.max(sides[2][0], 1e-9), i1 = sides[1][0] / Math.max(sides[2][0], 1e-9);
        // Near-isosceles triangles have an ambiguous vertex order
        if (!(i1 - i0 > 0.03 && 1 - i1 > 0.03)) continue;
        tris.push(sides.map(s => s[1]));
        inv.push([i0, i1]);
    }
    return { tris, inv };
}

// Nearest point within `bound`, through a grid of cells `bound` wide
class PointGrid {
    constructor(points, cell) {
        this.points = points;
        this.cell = cell;
        this.map = new Map();
        points.forEach((p, i) => {
            const key = this.key(Math.floor(p[0] / cell), Math.floor(p[1] / cell));
            if (!this.map.has(key)) this.map.set(key, []);
            this.map.get(key).push(i);
        });
    }

    key(gx, gy) { return gx * 100003 + gy; }

    // [distance, index] of the nearest point closer than bound, or [Infinity, -1]
    nearest(x, y, bound) {
        const gx = Math.floor(x / this.cell), gy = Math.floor(y / this.cell);
        const reach = Math.ceil(bound / this.cell);
        let best = Infinity, bi = -1;
        for (let dy = -reach; dy <= reach; dy++) for (let dx = -reach; dx <= reach; dx++) {
            const list = this.map.get(this.key(gx + dx, gy + dy));
            if (!list) continue;
            for (const i of list) {
                const d = Math.hypot(this.points[i][0] - x, this.points[i][1] - y);
                if (d < best) { best = d; bi = i; }
            }
        }
        return best < bound ? [best, bi] : [Infinity, -1];
    }
}

function mulberry32(seed) {
    return () => {
        seed = (seed + 0x6d2b79f5) | 0;
        let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

// M taking reference stars to target stars (both [x, y] lists, brightest first).
// Returns {m, matched, rms}.
export function matchStars(refXy, tgtXy, { nBright = 40, tolerance = 2.5, transform = 'similarity', maxScaleChange = 0.2, minMatches = 6 } = {}) {
    if (refXy.length < 3 || tgtXy.length < 3) throw new RegistrationError('not enough stars to match');
    const refB = refXy.slice(0, nBright), tgtB = tgtXy.slice(0, nBright);
    const tr = triangles(refB), tt = triangles(tgtB);
    if (!tr.tris.length || !tt.tris.length) throw new RegistrationError('not enough stars to match');

    // Triangles with the same shape (invariants within 0.012)
    const invGrid = new PointGrid(tt.inv, 0.012);
    let candidates = [];
    tr.inv.forEach((p, i) => {
        const gx = Math.floor(p[0] / 0.012), gy = Math.floor(p[1] / 0.012);
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
            const list = invGrid.map.get(invGrid.key(gx + dx, gy + dy));
            if (!list) continue;
            for (const j of list) if (Math.hypot(tt.inv[j][0] - p[0], tt.inv[j][1] - p[1]) <= 0.012) candidates.push([i, j]);
        }
    });
    if (!candidates.length) throw new RegistrationError('no matching star patterns');
    candidates.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (candidates.length > 4000) {
        const rand = mulberry32(1);
        for (let i = candidates.length - 1; i > 0; i--) {
            const j = Math.floor(rand() * (i + 1));
            [candidates[i], candidates[j]] = [candidates[j], candidates[i]];
        }
        candidates = candidates.slice(0, 4000);
    }

    const refAll = refXy.slice(0, 250);
    const tgtAll = tgtXy.slice(0, 400);
    const grid = new PointGrid(tgtAll, tolerance * 2);
    const need = Math.min(refAll.length, tgtAll.length);
    let bestM = null, bestCount = 0;
    for (const [i, j] of candidates) {
        let m;
        try {
            m = estimateSimilarity(tr.tris[i].map(k => refB[k]), tt.tris[j].map(k => tgtB[k]));
        } catch (error) {
            continue;
        }
        if (Math.abs(transformScale(m) - 1) > maxScaleChange) continue;
        let count = 0;
        for (const p of refAll) {
            const [x, y] = applyTransform(m, p[0], p[1]);
            if (grid.nearest(x, y, tolerance * 2)[1] >= 0) count++;
        }
        if (count > bestCount) {
            bestM = m;
            bestCount = count;
            if (count >= 0.8 * need) break;
        }
    }
    if (!bestM || bestCount < Math.min(minMatches, need)) throw new RegistrationError("star patterns didn't agree");

    let m = bestM, count = 0, rms = 0;
    for (const tol of [tolerance * 2, tolerance, tolerance]) {
        const src = [], dst = [];
        for (const p of refAll) {
            const [x, y] = applyTransform(m, p[0], p[1]);
            const [, k] = grid.nearest(x, y, tol);
            if (k >= 0) {
                src.push(p);
                dst.push(tgtAll[k]);
            }
        }
        if (src.length < 3) throw new RegistrationError('too few stars matched');
        m = transform === 'affine' && src.length >= 6 ? estimateAffine(src, dst) : estimateSimilarity(src, dst);
        let ss = 0;
        src.forEach((p, i) => {
            const [x, y] = applyTransform(m, p[0], p[1]);
            ss += (x - dst[i][0]) ** 2 + (y - dst[i][1]) ** 2;
        });
        count = src.length;
        rms = Math.sqrt(ss / count);
    }
    // Stricter than register.py's six: in a dense field six chance coincidences are
    // easy (a frame of a different field "matched" in testing), so a real match must
    // also account for a fair share of the stars
    if (count < Math.min(Math.max(minMatches, Math.round(0.15 * need)), need)) throw new RegistrationError('too few stars matched');
    return { m, matched: count, rms };
}

// --- phase correlation --------------------------------------------------------------

// In-place radix-2 complex FFT of length n (a power of two); inverse when invert
export function fft(re, im, invert = false) {
    const n = re.length;
    for (let i = 1, j = 0; i < n; i++) {
        let bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            let t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (let len = 2; len <= n; len <<= 1) {
        const ang = (2 * Math.PI / len) * (invert ? 1 : -1);
        const wr = Math.cos(ang), wi = Math.sin(ang);
        const half = len >> 1;
        for (let i = 0; i < n; i += len) {
            let cr = 1, ci = 0;
            for (let j = 0; j < half; j++) {
                const a = i + j, b = a + half;
                const xr = re[b] * cr - im[b] * ci;
                const xi = re[b] * ci + im[b] * cr;
                re[b] = re[a] - xr; im[b] = im[a] - xi;
                re[a] += xr; im[a] += xi;
                const ncr = cr * wr - ci * wi;
                ci = cr * wi + ci * wr;
                cr = ncr;
            }
        }
    }
    if (invert) for (let i = 0; i < n; i++) { re[i] /= n; im[i] /= n; }
}

export function fft2d(re, im, w, h, invert = false) {
    const rr = new Float64Array(w), ri = new Float64Array(w);
    for (let y = 0; y < h; y++) {
        const o = y * w;
        for (let x = 0; x < w; x++) { rr[x] = re[o + x]; ri[x] = im[o + x]; }
        fft(rr, ri, invert);
        for (let x = 0; x < w; x++) { re[o + x] = rr[x]; im[o + x] = ri[x]; }
    }
    const cr = new Float64Array(h), ci = new Float64Array(h);
    for (let x = 0; x < w; x++) {
        for (let y = 0; y < h; y++) { cr[y] = re[y * w + x]; ci[y] = im[y * w + x]; }
        fft(cr, ci, invert);
        for (let y = 0; y < h; y++) { re[y * w + x] = cr[y]; im[y * w + x] = ci[y]; }
    }
}

function nextPow2(n) {
    let p = 1;
    while (p < n) p <<= 1;
    return p;
}

function hann(n) {
    const w = new Float64Array(n);
    if (n === 1) { w[0] = 1; return w; }
    for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1));
    return w;
}

// Windowed, mean-removed image, zero-padded to powers of two, transformed
export function phaseSpectrum(img, w, h) {
    const W = nextPow2(w), H = nextPow2(h);
    const re = new Float64Array(W * H), im = new Float64Array(W * H);
    let mean = 0;
    for (let i = 0; i < w * h; i++) mean += img[i];
    mean /= w * h;
    const wy = hann(h), wx = hann(w);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) re[y * W + x] = (img[y * w + x] - mean) * wy[y] * wx[x];
    fft2d(re, im, W, H);
    return { re, im, W, H };
}

// The (dy, dx) shift that lines img up with ref: img(y + dy, x + dx) ~ ref(y, x).
// ref may be a precomputed phaseSpectrum. Returns {dy, dx, strength}.
export function phaseCorrelate(ref, img, w, h, upsample = true) {
    const a = ref.re ? ref : phaseSpectrum(ref, w, h);
    const b = phaseSpectrum(img, w, h);
    const { W, H } = a;
    const re = new Float64Array(W * H), im = new Float64Array(W * H);
    for (let i = 0; i < W * H; i++) {
        // b * conj(a), normalised
        const r = b.re[i] * a.re[i] + b.im[i] * a.im[i];
        const m = b.im[i] * a.re[i] - b.re[i] * a.im[i];
        const mag = Math.max(Math.hypot(r, m), 1e-12);
        re[i] = r / mag;
        im[i] = m / mag;
    }
    fft2d(re, im, W, H, true);
    let best = -Infinity, py = 0, px = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        if (re[y * W + x] > best) { best = re[y * W + x]; py = y; px = x; }
    }
    const at = (y, x) => re[((y + H) % H) * W + ((x + W) % W)];
    const refine = (m, c, p) => {
        const d = m - 2 * c + p;
        return Math.abs(d) < 1e-12 ? 0 : 0.5 * (m - p) / d;
    };
    let dy = py + (upsample ? refine(at(py - 1, px), best, at(py + 1, px)) : 0);
    let dx = px + (upsample ? refine(at(py, px - 1), best, at(py, px + 1)) : 0);
    if (dy > H / 2) dy -= H;
    if (dx > W / 2) dx -= W;
    return { dy, dx, strength: best };
}
