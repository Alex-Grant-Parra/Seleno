// The combine stage, swappable. Every per-frame step before it is shared (decode,
// calibrate, measure, align, warp); only where the aligned frames go and how they're
// combined changes:
//
//   memory   aligned float16 frames held in this tab, combined strip by strip with
//            full rejection (every frame's value for a pixel seen at once)
//   disk     aligned float16 frames in OPFS, combined strip by strip, full rejection
//   stream   running weighted sums (thousands of frames, or a multi-night project):
//            each value is checked against rejection limits from a reference stack of
//            the first few dozen frames, so memory stays fixed whatever the count
//
// chooseMode() picks from the frame count and size against memory and storage.
// Each mode: dest(k) (where warp tasks put frame k), add(k, warped), finish(sink).

import { encodeHalf, decodeHalf } from './half.js';
import { stripRowsFor } from './integrate.js';
import { encodeStrip, fitsDataBytes, fitsHeader, stripPlacement } from './fits.js';

// Messages in the page's language when its translations are loaded (window._),
// otherwise English: in Node tests and inside workers
const _ = (text, vars) => {
    if (typeof globalThis._ === 'function') return globalThis._(text, vars);
    return vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text;
};


const MB = 1024 ** 2;
export const REFERENCE_FRAMES = 24;
const STREAM_BYTES_PER_PIXEL = 38;      // sums and weights (float32 x 6), coverage, limits (float16 x 6)

export function frameBytes(width, height, planes = 3) {
    return width * height * planes * 2;
}

// Which mode fits: {mode, why}
export function chooseMode({ n, width, height, memoryBudget, diskFree, opfs, force = null }) {
    const per = frameBytes(width, height);
    const result = width * height * 3 * 4;
    if (force) return { mode: force, why: 'chosen' };
    if (n * per + result <= memoryBudget * 0.6) return { mode: 'memory', why: _('{n} frames fit in memory', { n }) };
    if (opfs && n * per + 2 * result + 1.5 * 1024 ** 3 <= diskFree) return { mode: 'disk', why: _('{n} frames fit in this browser\'s storage', { n }) };
    return { mode: 'stream', why: _('{n} frames are too many to hold at once, so they\'re added up as they come', { n }) };
}

// --- frame sets ------------------------------------------------------------------------

// Float16 frames (planes after one another), held in memory or in the store
export class FrameSet {
    constructor({ kind, name, planes, width, height, store = null }) {
        Object.assign(this, { kind, name, planes, width, height, store });
        this.frames = new Map();
    }

    dest() {
        return this.kind === 'memory' ? { kind: 'return' } : { kind: 'store', name: this.name };
    }

    accept(k, result) {
        this.frames.set(k, this.kind === 'memory' ? result.data : true);
    }

    keys() {
        return [...this.frames.keys()].sort((a, b) => a - b);
    }

    // What a combineStrip task needs for rows [r0, r1) of plane c of frames ks
    source(c, r0, r1, ks) {
        if (this.kind === 'store') return { source: { kind: 'store', name: this.name, ks, width: this.width, height: this.height }, transfer: [] };
        const len = (r1 - r0) * this.width;
        const data = new Uint16Array(ks.length * len);
        const start = c * this.width * this.height + r0 * this.width;
        ks.forEach((k, i) => data.set(this.frames.get(k).subarray(start, start + len), i * len));
        return { source: { kind: 'memory', data }, transfer: [data.buffer] };
    }

    // One whole frame's float16 data (to read a stored frame back)
    async frame(k) {
        if (this.kind === 'memory') return this.frames.get(k);
        const n = this.planes * this.width * this.height;
        const bytes = await this.store.read(`${this.name}/${k}`, 0, n * 2);
        return new Uint16Array(bytes.buffer, bytes.byteOffset, n);
    }

    async clear() {
        this.frames.clear();
        if (this.kind === 'store' && this.store) await this.store.removePrefix(`${this.name}/`);
    }
}

// Combine every strip of `planes` planes of frames ks, a few strips at a time across
// the pool. onStrip(c, r0, r1, result) gets each, in any order. bands: row ranges a
// strip mustn't cross (the streaming shards').
export async function stripCombine({ pool, frames, ks, planes = 3, options, norms = null, output = 'result', onStrip, signal, budget = 256 * MB, bands = null, onProgress = null }) {
    const { width, height } = frames;
    const rows = stripRowsFor(ks.length, width, budget, 0.8);
    const jobs = [];
    const ranges = bands || [[0, height]];
    for (let c = 0; c < planes; c++) for (const [a, b] of ranges) for (let r0 = a; r0 < b; r0 += rows) jobs.push([c, r0, Math.min(b, r0 + rows)]);
    let next = 0, done = 0;
    const inFlight = Math.max(1, pool.size * 2);
    const worker = async () => {
        while (next < jobs.length) {
            if (signal && signal.aborted) throw new DOMException('Cancelled', 'AbortError');
            const [c, r0, r1] = jobs[next++];
            const { source, transfer } = frames.source(c, r0, r1, ks);
            const out = await pool.run('combineStrip', {
                source, c, r0, r1, width, n: ks.length, output,
                method: options.method, kappaLow: options.kappaLow, kappaHigh: options.kappaHigh,
                iterations: options.iterations, weights: options.weights || null,
                norm: norms ? norms[c] : null
            }, { transfer, signal });
            await onStrip(c, r0, r1, out);
            done++;
            if (onProgress) onProgress(done / jobs.length);
        }
    };
    await Promise.all(Array.from({ length: Math.min(inFlight, jobs.length) }, worker));
}

// Per-channel normalisation (to the reference frame's background) for frames ks
function normsFor(stats, ks, refK) {
    return [0, 1, 2].map(c => ({
        loc: ks.map(k => stats.get(k).loc[c]),
        scale: ks.map(k => stats.get(k).scale[c]),
        refLoc: stats.get(refK).loc[c],
        refScale: stats.get(refK).scale[c]
    }));
}

// Noise weights (stack.py): 1 / (noise x the frame's scaling to the reference)^2
export function noiseWeight(noise, scaleGreen, refScaleGreen, normalise) {
    const k = normalise ? refScaleGreen / scaleGreen : 1;
    return 1 / Math.max(noise * k, 1e-9) ** 2;
}

// --- memory and disk -------------------------------------------------------------------

export class HeldCombine {
    constructor(mode, { pool, store, width, height, n, settings, refK = 0, signal }) {
        Object.assign(this, { mode, pool, width, height, n, settings, refK, signal });
        this.frames = new FrameSet({ kind: mode === 'disk' ? 'store' : 'memory', name: 'aligned', planes: 3, width, height, store });
        this.stats = new Map();
    }

    dest() {
        return this.frames.dest();
    }

    async add(k, warped, { noise }) {
        this.frames.accept(k, warped);
        this.stats.set(k, { loc: warped.loc, scale: warped.scale, noise });
    }

    async finish(sink, onProgress) {
        const ks = this.frames.keys();
        const n = ks.length;
        const s = this.settings;
        const refK = this.stats.has(this.refK) ? this.refK : ks[0];
        let weights = null;
        if (s.weighting === 'noise' && n >= 3) {
            const ref = this.stats.get(refK).scale[1];
            weights = ks.map(k => noiseWeight(this.stats.get(k).noise, this.stats.get(k).scale[1], ref, s.normalise));
            const max = Math.max(...weights);
            weights = weights.map(w => w / max);
        }
        const options = { method: n >= 3 ? s.method : 'average', kappaLow: s.kappa_low, kappaHigh: s.kappa_high, iterations: s.iterations, weights };
        const norms = s.normalise && n > 1 ? normsFor(this.stats, ks, refK) : null;
        let low = 0, high = 0;
        await stripCombine({
            pool: this.pool, frames: this.frames, ks, options, norms, signal: this.signal, onProgress,
            onStrip: async (c, r0, r1, out) => {
                low += out.rejectedLow;
                high += out.rejectedHigh;
                await sink.put(c, r0, r1, out.result, c === 1 ? out.coverage : null);
            }
        });
        await this.frames.clear();
        return { rejectedLow: low, rejectedHigh: high, frames: n };
    }

    async dispose() {
        await this.frames.clear();
    }
}

// --- streaming ------------------------------------------------------------------------

export class StreamCombine {
    // totals: a project's saved state ({planes per shard rows...}) to carry on from;
    // reference: {loc, scale} of the reference frame, when carrying on
    constructor({ pool, store, width, height, settings, signal, opfs, memoryBudget, reference = null, totals = null, referenceFrames = REFERENCE_FRAMES }) {
        Object.assign(this, { pool, width, height, settings, signal, reference, totals });
        this.mode = 'stream';
        const bytes = width * height * STREAM_BYTES_PER_PIXEL;
        const shards = Math.max(1, Math.min(pool.size, Math.ceil(bytes / (300 * MB))));
        const rows = Math.ceil(height / shards);
        this.shards = Array.from({ length: shards }, (_unused, i) => ({ id: `shard-${i}-${Date.now()}`, worker: i, y0: i * rows, y1: Math.min(height, (i + 1) * rows) }))
            .filter(s => s.y1 > s.y0);
        const per = frameBytes(width, height);
        this.referenceFrames = Math.max(3, Math.min(referenceFrames, opfs ? referenceFrames : Math.floor(memoryBudget * 0.4 / per)));
        this.refSet = new FrameSet({ kind: opfs ? 'store' : 'memory', name: 'reference', planes: 3, width, height, store });
        this.waiting = [];               // frames kept until the limits exist
        this.stats = new Map();
        this.ready = !!(totals && totals.hasLimits);
        this.added = 0;
    }

    async begin() {
        for (const s of this.shards) {
            await this.pool.run('shardInit', {
                id: s.id, y0: s.y0, y1: s.y1, width: this.width,
                totals: this.totals ? await this.totals.rows(s.y0, s.y1) : null
            }, { worker: s.worker });
        }
    }

    dest() {
        return this.ready ? { kind: 'return' } : this.refSet.dest();
    }

    norm(k) {
        if (!this.settings.normalise || !this.reference) return null;
        const st = this.stats.get(k);
        return { loc: st.loc, scale: st.scale, refLoc: this.reference.loc, refScale: this.reference.scale };
    }

    weight(k) {
        if (this.settings.weighting !== 'noise' || !this.reference) return 1;
        const st = this.stats.get(k);
        // Relative to a noise of 1e-3, so totals saved over many nights stay comparable
        return noiseWeight(st.noise, st.scale[1], this.reference.scale[1], this.settings.normalise) * 1e-6;
    }

    async add(k, warped, { noise }) {
        this.stats.set(k, { loc: warped.loc, scale: warped.scale, noise });
        if (!this.reference) this.reference = { loc: warped.loc.slice(), scale: warped.scale.slice() };
        if (this.ready) {
            await this.accumulate(k, warped.data);
            return;
        }
        this.refSet.accept(k, warped);
        this.waiting.push(k);
        if (this.waiting.length >= this.referenceFrames) await this.buildLimits();
    }

    // Rejection limits from the reference frames, then add those frames in
    async buildLimits() {
        const ks = this.waiting.slice();
        const s = this.settings;
        const n = ks.length;
        if (n >= 3 && s.method !== 'average') {
            const norms = s.normalise && this.reference ? [0, 1, 2].map(c => ({
                loc: ks.map(k => this.stats.get(k).loc[c]), scale: ks.map(k => this.stats.get(k).scale[c]),
                refLoc: this.reference.loc[c], refScale: this.reference.scale[c]
            })) : null;
            const method = s.method === 'median' ? 'sigma' : s.method;
            await stripCombine({
                pool: this.pool, frames: this.refSet, ks, norms, output: 'thresholds', signal: this.signal,
                bands: this.shards.map(sh => [sh.y0, sh.y1]),
                options: { method, kappaLow: s.kappa_low, kappaHigh: s.kappa_high, iterations: s.iterations },
                onStrip: async (c, r0, r1, out) => {
                    const sh = this.shards.find(x => r0 >= x.y0 && r1 <= x.y1);
                    await this.pool.run('shardLimits', { id: sh.id, c, r0: r0 - sh.y0, r1: r1 - sh.y0, low: out.low, high: out.high }, { worker: sh.worker, transfer: [out.low.buffer, out.high.buffer] });
                }
            });
        }
        this.ready = true;
        for (const k of ks) await this.accumulate(k, await this.refSet.frame(k));
        this.waiting = [];
        await this.refSet.clear();
    }

    async accumulate(k, data) {
        const plane = this.width * this.height;
        const norm = this.norm(k), weight = this.weight(k);
        await Promise.all(this.shards.map(sh => {
            const len = (sh.y1 - sh.y0) * this.width;
            const band = new Uint16Array(3 * len);
            for (let c = 0; c < 3; c++) band.set(data.subarray(c * plane + sh.y0 * this.width, c * plane + sh.y1 * this.width), c * len);
            return this.pool.run('shardAdd', { id: sh.id, data: band, weight, norm }, { worker: sh.worker, transfer: [band.buffer] });
        }));
        this.added++;
    }

    async finish(sink, onProgress) {
        if (!this.ready) await this.buildLimits();
        let low = 0, high = 0;
        for (let i = 0; i < this.shards.length; i++) {
            const sh = this.shards[i];
            const out = await this.pool.run('shardResult', { id: sh.id, drop: false }, { worker: sh.worker });
            low += out.rejectedLow;
            high += out.rejectedHigh;
            for (let c = 0; c < 3; c++) await sink.put(c, sh.y0, sh.y1, out.planes[c], c === 1 ? out.coverage : null);
            if (onProgress) onProgress((i + 1) / this.shards.length);
        }
        return { rejectedLow: low, rejectedHigh: high, frames: this.added };
    }

    // The running totals, rows of each shard, for a project's saved state
    async totalsRows() {
        const out = [];
        for (const sh of this.shards) out.push(await this.pool.run('shardTotals', { id: sh.id }, { worker: sh.worker }));
        return out;
    }

    async dispose() {
        for (const sh of this.shards) await this.pool.run('shardDrop', { id: sh.id }, { worker: sh.worker }).catch(() => {});
        await this.refSet.clear();
    }
}

// --- the result ----------------------------------------------------------------------

// Takes combined rows in any order and makes the outputs: master.fits (in the store,
// or in memory), the preview (block averages, at most previewSide), the coverage
// preview and the common-area crop.
export class ResultSink {
    constructor({ width, height, header, store = null, pool = null, previewSide = 1600, needed = 1 }) {
        Object.assign(this, { width, height, store, pool, needed });
        this.head = fitsHeader(width, height, 3, header);
        this.dataBytes = fitsDataBytes(width, height, 3);
        this.parts = store ? null : [];
        this.writes = [];
        const f = Math.max(1, Math.ceil(Math.max(width, height) / previewSide));
        this.factor = f;
        this.pw = Math.floor(width / f);
        this.ph = Math.floor(height / f);
        this.psum = [0, 1, 2].map(() => new Float64Array(this.pw * this.ph));
        this.pcount = [0, 1, 2].map(() => new Uint32Array(this.pw * this.ph));
        const cf = Math.max(1, Math.ceil(Math.max(width, height) / (previewSide / 4)));
        this.cfactor = cf;
        this.cw = Math.floor(width / cf);
        this.ch = Math.floor(height / cf);
        this.csum = new Float64Array(this.cw * this.ch);
        this.rowFull = new Uint32Array(height);
        this.colFull = new Uint32Array(width);
        this.rowAny = new Uint8Array(height);
        this.colAny = new Uint8Array(width);
        this.started = false;
    }

    async write(offset, bytes) {
        if (this.parts) {
            this.parts.push([offset, bytes]);
            return;
        }
        // One writer (worker 0) owns the file's sync handle
        await this.pool.run('storeWrite', { name: 'result.fits', offset, data: bytes }, { worker: 0, transfer: [bytes.buffer] });
    }

    async put(c, r0, r1, rows, coverage = null) {
        if (!this.started) {
            this.started = true;
            await this.write(0, this.head.slice());
        }
        const w = this.width;
        const { offset } = stripPlacement(w, this.height, c, r0, r1);
        await this.write(this.head.length + offset, encodeStrip(rows, w, r0, r1));
        // Preview: block averages, NaN skipped
        const f = this.factor, ps = this.psum[c], pc = this.pcount[c];
        for (let y = r0; y < r1; y++) {
            const py = Math.floor(y / f);
            if (py >= this.ph) break;
            const row = (y - r0) * w;
            for (let x = 0; x < this.pw * f; x++) {
                const v = rows[row + x];
                if (v !== v) continue;
                const i = py * this.pw + Math.floor(x / f);
                ps[i] += v;
                pc[i]++;
            }
        }
        if (coverage) {
            const cf = this.cfactor;
            for (let y = r0; y < r1; y++) {
                const row = (y - r0) * w;
                let full = 0;
                for (let x = 0; x < w; x++) {
                    const v = coverage[row + x];
                    if (v >= this.needed) { full++; this.colFull[x]++; this.colAny[x] = 1; }
                    const cy = Math.floor(y / cf), cx = Math.floor(x / cf);
                    if (cy < this.ch && cx < this.cw) this.csum[cy * this.cw + cx] += v;
                }
                this.rowFull[y] = full;
                if (full) this.rowAny[y] = 1;
            }
        }
    }

    // The box (fractions) of rows and columns covered by every frame (stack.common_area)
    crop() {
        const { width: w, height: h } = this;
        const rows = [], cols = [];
        for (let y = 0; y < h; y++) if (this.rowFull[y] / w >= 0.995) rows.push(y);
        for (let x = 0; x < w; x++) if (this.colFull[x] / h >= 0.995) cols.push(x);
        let r = rows, c = cols;
        if (!r.length || !c.length) {
            r = []; c = [];
            for (let y = 0; y < h; y++) if (this.rowAny[y]) r.push(y);
            for (let x = 0; x < w; x++) if (this.colAny[x]) c.push(x);
        }
        if (!r.length || !c.length) return { x0: 0, y0: 0, x1: 1, y1: 1 };
        return { x0: c[0] / w, y0: r[0] / h, x1: (c[c.length - 1] + 1) / w, y1: (r[r.length - 1] + 1) / h };
    }

    async finish() {
        const padding = this.dataBytes - this.width * this.height * 12;
        if (padding) await this.write(this.head.length + this.width * this.height * 12, new Uint8Array(padding));
        let master;
        if (this.parts) {
            this.parts.sort((a, b) => a[0] - b[0]);
            master = new Blob(this.parts.map(p => p[1]), { type: 'application/fits' });
            this.parts = [];
        } else {
            master = await this.store.file('result.fits');
        }
        const n = this.pw * this.ph;
        const planes = [0, 1, 2].map(c => {
            const p = new Float32Array(n);
            for (let i = 0; i < n; i++) p[i] = this.pcount[c][i] ? this.psum[c][i] / this.pcount[c][i] : 0;
            return p;
        });
        const cov = new Uint8Array(8 + this.cw * this.ch);
        const dv = new DataView(cov.buffer);
        dv.setUint32(0, this.cw, true);
        dv.setUint32(4, this.ch, true);
        const per = this.cfactor * this.cfactor * Math.max(this.needed, 1);
        for (let i = 0; i < this.cw * this.ch; i++) cov[8 + i] = Math.min(255, Math.round(255 * this.csum[i] / per));
        return { master, preview: { planes, width: this.pw, height: this.ph }, coverage: cov };
    }
}

// The preview as interleaved float16 RGB (the stack's preview.f16)
export function previewHalf(preview) {
    const { planes, width, height } = preview;
    const out = new Uint16Array(width * height * 3);
    const tmp = new Float32Array(3);
    for (let i = 0; i < width * height; i++) {
        tmp[0] = planes[0][i]; tmp[1] = planes[1][i]; tmp[2] = planes[2][i];
        out.set(encodeHalf(tmp), i * 3);
    }
    return out;
}

export function previewFromHalf(data, width, height) {
    const all = decodeHalf(data);
    const planes = [0, 1, 2].map(c => {
        const p = new Float32Array(width * height);
        for (let i = 0; i < p.length; i++) p[i] = all[i * 3 + c];
        return p;
    });
    return { planes, width, height };
}
