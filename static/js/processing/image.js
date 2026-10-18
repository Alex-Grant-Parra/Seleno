// Image basics: robust statistics, the background map, separable filters, binning and
// debayering. Images are Float32Arrays, row-major, with explicit width and height;
// colour images are three planes. Ported from app/processing/stars.py and frames.py;
// edge modes follow scipy's (reflect for gaussian_filter and maximum_filter, mirror
// for the debayer and median filters).

// k-th smallest of a[lo..hi] (inclusive), partially sorting `a` in place
function quickselect(a, k, lo = 0, hi = a.length - 1) {
    while (hi > lo) {
        const mid = (lo + hi) >> 1;
        // Median of three as the pivot
        if (a[mid] < a[lo]) { const t = a[mid]; a[mid] = a[lo]; a[lo] = t; }
        if (a[hi] < a[lo]) { const t = a[hi]; a[hi] = a[lo]; a[lo] = t; }
        if (a[hi] < a[mid]) { const t = a[hi]; a[hi] = a[mid]; a[mid] = t; }
        const pivot = a[mid];
        let i = lo, j = hi;
        while (i <= j) {
            while (a[i] < pivot) i++;
            while (a[j] > pivot) j--;
            if (i <= j) {
                const t = a[i]; a[i] = a[j]; a[j] = t;
                i++; j--;
            }
        }
        if (k <= j) hi = j;
        else if (k >= i) lo = i;
        else return a[k];
    }
    return a[k];
}

// Median of the values (numpy's: the mean of the middle two for an even count). Works
// on a copy unless inPlace. NaNs are dropped.
export function median(values, inPlace = false) {
    let a = inPlace ? values : Float64Array.from(values);
    let n = 0;
    for (let i = 0; i < a.length; i++) if (a[i] === a[i]) a[n++] = a[i];
    if (!n) return NaN;
    a = a.subarray(0, n);
    const k = n >> 1;
    const hi = quickselect(a, k);
    if (n & 1) return hi;
    let lo = -Infinity;
    for (let i = 0; i < k; i++) if (a[i] > lo) lo = a[i];
    return 0.5 * (lo + hi);
}

// (median, robust standard deviation from the MAD) of the finite values
export function robustStats(values) {
    const a = new Float64Array(values.length);
    let n = 0;
    for (let i = 0; i < values.length; i++) {
        const v = values[i];
        if (Number.isFinite(v)) a[n++] = v;
    }
    if (!n) return [0, 0];
    const v = a.subarray(0, n);
    const med = median(v, true);
    for (let i = 0; i < n; i++) v[i] = Math.abs(v[i] - med);
    return [med, 1.4826 * median(v, true)];
}

// Every step-th row and column (numpy's img[::step, ::step]), flattened
export function subsample(data, width, height, step, offset = 0) {
    const nx = Math.ceil((width - offset) / step), ny = Math.ceil((height - offset) / step);
    const out = new Float32Array(Math.max(0, nx * ny));
    let k = 0;
    for (let y = offset; y < height; y += step) {
        const row = y * width;
        for (let x = offset; x < width; x += step) out[k++] = data[row + x];
    }
    return out;
}

export function percentile(values, q) {
    const a = Float64Array.from(values).filter(v => v === v);
    if (!a.length) return NaN;
    a.sort();
    const pos = (q / 100) * (a.length - 1);
    const i = Math.floor(pos);
    const f = pos - i;
    return i + 1 < a.length ? a[i] * (1 - f) + a[i + 1] * f : a[i];
}

// --- edge modes -------------------------------------------------------------------

export function reflectIndex(i, n) {
    // scipy 'reflect' (d c b a | a b c d | d c b a)
    if (n === 1) return 0;
    const period = 2 * n;
    i = ((i % period) + period) % period;
    return i < n ? i : period - 1 - i;
}

export function mirrorIndex(i, n) {
    // scipy 'mirror' (d c b | a b c d | c b a)
    if (n === 1) return 0;
    const period = 2 * n - 2;
    i = ((i % period) + period) % period;
    return i < n ? i : period - i;
}

// --- filters ----------------------------------------------------------------------

export function gaussianKernel(sigma, truncate = 4.0) {
    const radius = Math.floor(truncate * sigma + 0.5);
    const k = new Float64Array(2 * radius + 1);
    let sum = 0;
    for (let i = -radius; i <= radius; i++) {
        k[i + radius] = Math.exp(-0.5 * (i * i) / (sigma * sigma));
        sum += k[i + radius];
    }
    for (let i = 0; i < k.length; i++) k[i] /= sum;
    return k;
}

// Separable convolution with a symmetric kernel, scipy's 'reflect' (or 'mirror') edges
export function convolveSeparable(data, width, height, kernel, mode = 'reflect', out = null) {
    const edge = mode === 'mirror' ? mirrorIndex : reflectIndex;
    const r = (kernel.length - 1) >> 1;
    const tmp = new Float32Array(width * height);
    out = out || new Float32Array(width * height);
    const line = new Float32Array(Math.max(width, height) + 2 * r);
    for (let y = 0; y < height; y++) {
        const row = y * width;
        for (let x = -r; x < width + r; x++) line[x + r] = data[row + (x < 0 || x >= width ? edge(x, width) : x)];
        for (let x = 0; x < width; x++) {
            let s = 0;
            for (let k = 0; k < kernel.length; k++) s += kernel[k] * line[x + k];
            tmp[row + x] = s;
        }
    }
    for (let x = 0; x < width; x++) {
        for (let y = -r; y < height + r; y++) line[y + r] = tmp[(y < 0 || y >= height ? edge(y, height) : y) * width + x];
        for (let y = 0; y < height; y++) {
            let s = 0;
            for (let k = 0; k < kernel.length; k++) s += kernel[k] * line[y + k];
            out[y * width + x] = s;
        }
    }
    return out;
}

export function gaussianFilter(data, width, height, sigma, mode = 'reflect') {
    return convolveSeparable(data, width, height, gaussianKernel(sigma), mode);
}

// size x size maximum (scipy maximum_filter, reflect edges), separable
export function maximumFilter(data, width, height, size) {
    const r = size >> 1;
    const tmp = new Float32Array(width * height);
    const out = new Float32Array(width * height);
    for (let y = 0; y < height; y++) {
        const row = y * width;
        for (let x = 0; x < width; x++) {
            let m = -Infinity;
            for (let d = -r; d <= r; d++) {
                const xx = x + d;
                const v = data[row + (xx < 0 || xx >= width ? reflectIndex(xx, width) : xx)];
                if (v > m) m = v;
            }
            tmp[row + x] = m;
        }
    }
    for (let x = 0; x < width; x++) {
        for (let y = 0; y < height; y++) {
            let m = -Infinity;
            for (let d = -r; d <= r; d++) {
                const yy = y + d;
                const v = tmp[(yy < 0 || yy >= height ? reflectIndex(yy, height) : yy) * width + x];
                if (v > m) m = v;
            }
            out[y * width + x] = m;
        }
    }
    return out;
}

// 3x3 median with 'nearest' edges (for the small background grid)
function medianFilter3Nearest(grid, nx, ny) {
    const out = new Float32Array(grid.length);
    const v = new Float64Array(9);
    for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) {
        let k = 0;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
            const yy = Math.min(ny - 1, Math.max(0, y + dy));
            const xx = Math.min(nx - 1, Math.max(0, x + dx));
            v[k++] = grid[yy * nx + xx];
        }
        out[y * nx + x] = median(v, true);
    }
    return out;
}

function interpAxis(nOut, nGrid, cell) {
    const i0 = new Int32Array(nOut), i1 = new Int32Array(nOut), frac = new Float32Array(nOut);
    for (let i = 0; i < nOut; i++) {
        let pos = (i + 0.5) / cell - 0.5;
        pos = Math.min(Math.max(pos, 0), nGrid - 1);
        i0[i] = Math.floor(pos);
        i1[i] = Math.min(i0[i] + 1, nGrid - 1);
        frac[i] = pos - i0[i];
    }
    return { i0, i1, frac };
}

// A smooth background: medians of cell x cell blocks (every other pixel), median
// filtered so stars don't pull it up, bilinearly interpolated to full size
export function backgroundMap(img, width, height, cell = 64) {
    cell = Math.floor(Math.max(8, Math.min(cell, (height >> 1) || 1, (width >> 1) || 1)));
    const ny = Math.max(1, Math.floor(height / cell)), nx = Math.max(1, Math.floor(width / cell));
    const step = cell >= 16 ? 2 : 1;
    let grid = new Float32Array(nx * ny);
    const per = Math.ceil(cell / step) ** 2;
    const buf = new Float64Array(per);
    for (let gy = 0; gy < ny; gy++) for (let gx = 0; gx < nx; gx++) {
        let k = 0;
        for (let y = gy * cell; y < (gy + 1) * cell; y += step) {
            const row = y * width;
            for (let x = gx * cell; x < (gx + 1) * cell; x += step) buf[k++] = img[row + x];
        }
        grid[gy * nx + gx] = median(buf.subarray(0, k), true);
    }
    if (ny >= 3 && nx >= 3) grid = medianFilter3Nearest(grid, nx, ny);
    const ay = interpAxis(height, ny, cell), ax = interpAxis(width, nx, cell);
    const out = new Float32Array(width * height);
    const rowBuf = new Float32Array(nx);
    for (let y = 0; y < height; y++) {
        const fy = ay.frac[y], r0 = ay.i0[y] * nx, r1 = ay.i1[y] * nx;
        for (let gx = 0; gx < nx; gx++) rowBuf[gx] = (1 - fy) * grid[r0 + gx] + fy * grid[r1 + gx];
        const row = y * width;
        for (let x = 0; x < width; x++) {
            const fx = ax.frac[x];
            out[row + x] = (1 - fx) * rowBuf[ax.i0[x]] + fx * rowBuf[ax.i1[x]];
        }
    }
    return out;
}

// --- binning and colour --------------------------------------------------------------

// 2x2 binned mosaic: a half-size monochrome image for finding stars
export function mosaicLuminance(data, width, height) {
    const w2 = width >> 1, h2 = height >> 1;
    const out = new Float32Array(w2 * h2);
    for (let y = 0; y < h2; y++) {
        const a = 2 * y * width, b = a + width;
        for (let x = 0; x < w2; x++) {
            const i = 2 * x;
            out[y * w2 + x] = (data[a + i] + data[a + i + 1] + data[b + i] + data[b + i + 1]) * 0.25;
        }
    }
    return { data: out, width: w2, height: h2 };
}

// Rec. 709 luma of three planes, binned 2x2
export function rgbLuminanceHalf(planes, width, height) {
    const [r, g, b] = planes;
    const w2 = width >> 1, h2 = height >> 1;
    const out = new Float32Array(w2 * h2);
    for (let y = 0; y < h2; y++) {
        for (let x = 0; x < w2; x++) {
            let s = 0;
            for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
                const i = (2 * y + dy) * width + 2 * x + dx;
                s += 0.2126 * r[i] + 0.7152 * g[i] + 0.0722 * b[i];
            }
            out[y * w2 + x] = s * 0.25;
        }
    }
    return { data: out, width: w2, height: h2 };
}

export function luma(planes) {
    const [r, g, b] = planes;
    const out = new Float32Array(r.length);
    for (let i = 0; i < r.length; i++) out[i] = 0.2126 * r[i] + 0.7152 * g[i] + 0.0722 * b[i];
    return out;
}

// One colour of a Bayer mosaic at full size (bilinear: the missing values are the
// mean of the nearest ones of that colour, mirror edges), rows [y0, y1)
export function debayerChannel(data, width, height, pattern, channel, y0 = 0, y1 = height) {
    const out = new Float32Array((y1 - y0) * width);
    const at = (y, x) => data[mirrorIndex(y, height) * width + mirrorIndex(x, width)];
    for (let y = y0; y < y1; y++) {
        const prow = pattern[y & 1];
        const orow = (y - y0) * width;
        const inner = y > 0 && y < height - 1;
        for (let x = 0; x < width; x++) {
            const here = prow[x & 1];
            let v;
            if (here === channel) v = data[y * width + x];
            else if (inner && x > 0 && x < width - 1) {
                const i = y * width + x;
                if (channel === 1) v = 0.25 * (data[i - 1] + data[i + 1] + data[i - width] + data[i + width]);
                else if (prow[(x + 1) & 1] === channel) v = 0.5 * (data[i - 1] + data[i + 1]);
                else if (pattern[(y + 1) & 1][x & 1] === channel) v = 0.5 * (data[i - width] + data[i + width]);
                else v = 0.25 * (data[i - width - 1] + data[i - width + 1] + data[i + width - 1] + data[i + width + 1]);
            } else {
                if (channel === 1) v = 0.25 * (at(y, x - 1) + at(y, x + 1) + at(y - 1, x) + at(y + 1, x));
                else if (prow[(x + 1) & 1] === channel) v = 0.5 * (at(y, x - 1) + at(y, x + 1));
                else if (pattern[(y + 1) & 1][x & 1] === channel) v = 0.5 * (at(y - 1, x) + at(y + 1, x));
                else v = 0.25 * (at(y - 1, x - 1) + at(y - 1, x + 1) + at(y + 1, x - 1) + at(y + 1, x + 1));
            }
            out[orow + x] = v;
        }
    }
    return out;
}

// Each 2x2 cell becomes one pixel: three half-size planes
export function debayerSuperpixel(data, width, height, pattern) {
    const w2 = width >> 1, h2 = height >> 1;
    const planes = [new Float32Array(w2 * h2), new Float32Array(w2 * h2), new Float32Array(w2 * h2)];
    const counts = [0, 0, 0];
    for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) counts[pattern[dy][dx]]++;
    for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
        const c = pattern[dy][dx];
        const p = planes[c];
        const k = 1 / counts[c];
        for (let y = 0; y < h2; y++) {
            const src = (2 * y + dy) * width + dx;
            const dst = y * w2;
            for (let x = 0; x < w2; x++) p[dst + x] += data[src + 2 * x] * k;
        }
    }
    return { planes, width: w2, height: h2 };
}

// Block-average to at most maxSide (NaNs ignored), for previews
export function downsamplePlanes(planes, width, height, maxSide) {
    const factor = Math.max(1, Math.ceil(Math.max(width, height) / maxSide));
    const ow = Math.floor(width / factor), oh = Math.floor(height / factor);
    const out = planes.map(() => new Float32Array(ow * oh));
    planes.forEach((p, c) => {
        const o = out[c];
        for (let y = 0; y < oh; y++) for (let x = 0; x < ow; x++) {
            let s = 0, n = 0;
            for (let dy = 0; dy < factor; dy++) {
                const row = (y * factor + dy) * width + x * factor;
                for (let dx = 0; dx < factor; dx++) {
                    const v = p[row + dx];
                    if (v === v) { s += v; n++; }
                }
            }
            o[y * ow + x] = n ? s / n : 0;
        }
    });
    return { planes: out, width: ow, height: oh, factor };
}
