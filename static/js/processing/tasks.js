// The work a worker does, one task at a time. Every task is (args, env) -> {result,
// transfer}: the pool runs them in Web Workers (worker.js), or in-process (the inline
// pool, for the Node tests and the no-worker fallback). env holds the job's store
// (OPFS, or null when frames travel back to the main thread), the calibration masters,
// and long-lived state (streaming shards, editor images).

import { calibrateFrame, normaliseFlat } from './calibrate.js';
import { loadFrame, decodeImageData, rgbaToPlanes } from './decode.js';
import { frameMetrics, frameToHalf, measureStars, toRgb, warpToHalf } from './frame-ops.js';
import { decodeHalf, encodeHalf, HALF_TO_FLOAT } from './half.js';
import { mosaicLuminance, rgbLuminanceHalf } from './image.js';
import { combine, normaliseStrip, thresholds } from './integrate.js';
import { readCR2, RawError } from './cr2.js';
import { fetchBytes } from './fetch-cache.js';
import * as develop from './develop.js';
import { readFloat, readHalf } from './store.js';

// --- masters ------------------------------------------------------------------------

// Rows of the calibration masters: from the store (float32 planes, one file each), or
// from arrays sent to this worker
function mastersAccess(spec, store) {
    if (!spec) return { info: {}, hot: null, rows: async () => null };
    const { info, hot, width, height, planes, arrays } = spec;
    return {
        info,
        hot: hot || null,
        async rows(name, y0, y1) {
            if (!info[name]) return null;
            const n = (y1 - y0) * width;
            const out = [];
            for (let c = 0; c < planes; c++) {
                if (arrays) out.push(arrays[name][c].subarray(y0 * width, y1 * width));
                else out.push(await readFloat(store, `master/${name}`, c * width * height + y0 * width, n));
            }
            return out;
        }
    };
}

// --- helpers ------------------------------------------------------------------------

async function fromHalfStore(store, name, planes, n) {
    const data = await readHalf(store, name, 0, planes * n);
    return Array.from({ length: planes }, (_, c) => decodeHalf(data.subarray(c * n, (c + 1) * n)));
}

async function deliver(env, dest, k, data) {
    // A frame's float16 planes: into the store, or back to the main thread
    if (dest.kind === 'store') {
        await env.store.write(`${dest.name}/${k}`, 0, data);
        return { stored: true };
    }
    return { data };
}

function checkShape(frame, shape, name, what) {
    if (shape && (frame.width !== shape[0] || frame.height !== shape[1])) {
        throw new Error(`${name} is a different size from the other ${what} (${frame.width} × ${frame.height}, not ${shape[0]} × ${shape[1]})`);
    }
}

async function loadCalibrated(env, args) {
    const { entry, shape, exposure, settings, cached } = args;
    if (cached) {
        const n = shape[0] * shape[1];
        const planes = await fromHalfStore(env.store, cached.name, cached.planes, n);
        return { planes, width: shape[0], height: shape[1], ...cached.meta };
    }
    const frame = await loadFrame(entry, env);
    checkShape(frame, shape, entry.name, 'lights');
    await calibrateFrame(frame, env.masters, { exposure, darkScaling: settings.dark_scaling, cosmetic: settings.cosmetic });
    return frame;
}

// --- tasks ------------------------------------------------------------------------------

export const TASKS = {
    async init(args, env) {
        env.store = args.store || env.store || null;
        return { result: { ok: true } };
    },

    async setMasters(args, env) {
        env.masters = mastersAccess(args, env.store);
        return { result: { ok: true } };
    },

    async probe({ entry }, env) {
        const frame = await loadFrame(entry, env);
        return {
            result: {
                width: frame.width, height: frame.height, isRaw: !!frame.pattern, pattern: frame.pattern,
                fellBack: !!frame.fellBack, camera: frame.camera || null
            }
        };
    },

    // One calibration frame, ready to combine into its master
    async loadCalibration({ entry, k, shape, dest, normalise, perChannel, subtract }, env) {
        const frame = await loadFrame(entry, env);
        checkShape(frame, shape, entry.name, 'frames');
        if (subtract && env.masters.info[subtract]) {
            const rows = await env.masters.rows(subtract, 0, frame.height);
            frame.planes.forEach((p, c) => { const s = rows[c]; for (let i = 0; i < p.length; i++) p[i] -= s[i]; });
        }
        if (normalise) normaliseFlat(frame, perChannel);
        const data = frameToHalf(frame);
        const out = await deliver(env, dest, k, data);
        return { result: { k, ...out }, transfer: out.data ? [out.data.buffer] : [] };
    },

    // Combine rows [r0, r1) of plane c of n frames
    async combineStrip(args, env) {
        const { source, c, r0, r1, width, n, method, kappaLow, kappaHigh, iterations, weights, norm, output } = args;
        const len = (r1 - r0) * width;
        const frames = [];
        for (let k = 0; k < n; k++) {
            let half;
            if (source.kind === 'memory') half = source.data.subarray(k * len, (k + 1) * len);
            else half = await readHalf(env.store, `${source.name}/${source.ks[k]}`, c * source.width * source.height + r0 * width, len);
            frames.push(decodeHalf(half));
        }
        if (norm && n > 1) normaliseStrip(frames, norm.loc, norm.scale, norm.refLoc, norm.refScale);
        if (output === 'thresholds') {
            const low = new Float32Array(len), high = new Float32Array(len);
            const buf = new Float64Array(n);
            for (let p = 0; p < len; p++) {
                let cnt = 0;
                for (let k = 0; k < n; k++) { const v = frames[k][p]; if (v === v) buf[cnt++] = v; }
                buf.subarray(0, cnt).sort();
                [low[p], high[p]] = cnt >= 3 ? thresholds(buf, cnt, method, kappaLow, kappaHigh, iterations) : [-Infinity, Infinity];
            }
            return { result: { low, high }, transfer: [low.buffer, high.buffer] };
        }
        const out = combine(frames, { method, kappaLow, kappaHigh, iterations, weights });
        return { result: out, transfer: [out.result.buffer, out.coverage.buffer] };
    },

    async storeWrite({ name, offset, data }, env) {
        await env.store.write(name, offset, data);
        return { result: { ok: true } };
    },

    async measureLight({ entry, k, shape, exposure, settings, cacheDest }, env) {
        const frame = await loadCalibrated(env, { entry, shape, exposure, settings });
        const { summary, xy } = measureStars(frame, settings.star_sigma);
        let cached = null;
        if (cacheDest) {
            await env.store.write(`${cacheDest}/${k}`, 0, frameToHalf(frame));
            cached = { name: `${cacheDest}/${k}`, planes: frame.planes.length, meta: { pattern: frame.pattern, wb: frame.wb, wbAsShot: frame.wbAsShot, camToSrgb: frame.camToSrgb, isRaw: !!frame.pattern } };
        }
        return { result: { k, summary, xy, isRaw: !!frame.pattern, fellBack: !!frame.fellBack, cached } };
    },

    async warpLight({ entry, k, shape, exposure, settings, cached, mOut, outW, outH, cubic, dest }, env) {
        let frame = await loadCalibrated(env, { entry, shape, exposure, settings, cached });
        const rgb = toRgb(frame, settings);
        frame = null;
        const { data, loc, scale } = warpToHalf(rgb, mOut, outW, outH, cubic);
        const out = await deliver(env, dest, k, data);
        return { result: { k, loc, scale, ...out }, transfer: out.data ? [out.data.buffer] : [] };
    },

    // The album's quality numbers for one photo (analysis.measure_file)
    async measureFrame({ entry }, env) {
        const rawUrl = entry.raw_url && /\.cr2(\?|$)/i.test(entry.raw_url) ? entry.raw_url : (entry.kind === 'raw' && /\.cr2(\?|$)/i.test(entry.url) ? entry.url : null);
        if (rawUrl) {
            try {
                const { buffer } = await fetchBytes(rawUrl, { cache: true });
                const m = readCR2(buffer);
                const lum = mosaicLuminance(m.data, m.width, m.height);
                return { result: frameMetrics(lum, m.width, m.height, 'raw') };
            } catch (error) {
                if (!(error instanceof RawError) || entry.kind === 'raw') throw error;
            }
        }
        if (entry.kind === 'raw') throw new RawError('Only Canon CR2 RAWs can be measured in the browser');
        const { buffer } = await fetchBytes(entry.url, { cache: false });
        const half = await decodeImageData(buffer, { scale: 0.5 });
        const planes = rgbaToPlanes(half.data, half.width, half.height, true);
        const lum = { data: new Float32Array(half.width * half.height), width: half.width, height: half.height };
        for (let i = 0; i < lum.data.length; i++) lum.data[i] = 0.2126 * planes[0][i] + 0.7152 * planes[1][i] + 0.0722 * planes[2][i];
        return { result: frameMetrics(lum, half.fullWidth, half.fullHeight, 'jpeg') };
    },

    // auto.jpg and thumb.jpg from the preview (stack.write_outputs)
    async autoOutputs({ preview, params, planetary }, env) {
        const img = { planes: preview.planes, width: preview.width, height: preview.height };
        const resolved = develop.resolve(img, params, planetary);
        const out = develop.apply(img, resolved, 1);
        const auto = await encodeJpeg(out, 0.9);
        const thumb = await encodeJpeg(out, 0.85, 480);
        return { result: { auto, thumb, resolved: { ...resolved, background_model: resolved.background_model || null } } };
    },

    // --- streaming accumulation: a shard owns rows [y0, y1) of the running totals ---

    async shardInit({ id, y0, y1, width, totals }, env) {
        const n = (y1 - y0) * width;
        const s = {
            y0, y1, width, n,
            sum: totals ? totals.sum : [0, 1, 2].map(() => new Float32Array(n)),
            wsum: totals ? totals.wsum : [0, 1, 2].map(() => new Float32Array(n)),
            coverage: totals ? totals.coverage : new Uint16Array(n),
            low: totals && totals.low ? totals.low.map(a => encodeHalf(a)) : null,
            high: totals && totals.high ? totals.high.map(a => encodeHalf(a)) : null,
            rejectedLow: 0, rejectedHigh: 0, frames: totals ? totals.frames || 0 : 0
        };
        env.state.set(id, s);
        return { result: { ok: true } };
    },

    // Rejection limits for rows [r0, r1) (relative to the shard) of plane c
    async shardLimits({ id, c, r0, r1, low, high }, env) {
        const s = env.state.get(id);
        if (!s.low) {
            s.low = [0, 1, 2].map(() => new Uint16Array(s.n).fill(0xfc00));      // -Infinity
            s.high = [0, 1, 2].map(() => new Uint16Array(s.n).fill(0x7c00));     // +Infinity
        }
        encodeHalf(low, s.low[c].subarray(r0 * s.width, r1 * s.width));
        encodeHalf(high, s.high[c].subarray(r0 * s.width, r1 * s.width));
        return { result: { ok: true } };
    },

    // One frame's rows (float16, three planes) into the totals
    async shardAdd({ id, data, weight, norm }, env) {
        const s = env.state.get(id);
        const t = HALF_TO_FLOAT;
        const { n } = s;
        for (let c = 0; c < 3; c++) {
            const sum = s.sum[c], wsum = s.wsum[c];
            const low = s.low && s.low[c], high = s.high && s.high[c];
            const k = norm ? (norm.scale[c] > 0 ? norm.refScale[c] / norm.scale[c] : 1) : 1;
            const l0 = norm ? norm.loc[c] : 0, r0 = norm ? norm.refLoc[c] : 0;
            const base = c * n;
            for (let p = 0; p < n; p++) {
                let v = t[data[base + p]];
                if (v !== v) continue;
                if (c === 1) s.coverage[p]++;
                if (norm) v = (v - l0) * k + r0;
                if (low) {
                    if (v < t[low[p]]) { s.rejectedLow++; continue; }
                    if (v > t[high[p]]) { s.rejectedHigh++; continue; }
                }
                sum[p] += v * weight;
                wsum[p] += weight;
            }
        }
        s.frames++;
        return { result: { ok: true } };
    },

    async shardResult({ id, drop }, env) {
        const s = env.state.get(id);
        const planes = [0, 1, 2].map(c => {
            const out = new Float32Array(s.n);
            for (let p = 0; p < s.n; p++) out[p] = s.wsum[c][p] > 0 ? s.sum[c][p] / s.wsum[c][p] : NaN;
            return out;
        });
        const result = { planes, coverage: s.coverage.slice(), rejectedLow: s.rejectedLow, rejectedHigh: s.rejectedHigh, y0: s.y0, y1: s.y1 };
        if (drop) env.state.delete(id);
        return { result, transfer: [...planes.map(p => p.buffer), result.coverage.buffer] };
    },

    // The raw totals (for a project's saved state)
    async shardTotals({ id }, env) {
        const s = env.state.get(id);
        const result = {
            y0: s.y0, y1: s.y1, sum: s.sum.map(a => a.slice()), wsum: s.wsum.map(a => a.slice()), coverage: s.coverage.slice(),
            low: s.low ? s.low.map(a => decodeHalf(a)) : null, high: s.high ? s.high.map(a => decodeHalf(a)) : null
        };
        return { result };
    },

    async shardDrop({ id }, env) {
        env.state.delete(id);
        return { result: { ok: true } };
    }
};

export async function encodeJpeg(img, quality = 0.9, maxSide = null) {
    let { width: w, height: h } = img;
    const rgba = develop.toRGBA(img);
    let canvas = new OffscreenCanvas(w, h);
    canvas.getContext('2d').putImageData(new ImageData(rgba, w, h), 0, 0);
    if (maxSide && Math.max(w, h) > maxSide) {
        const s = maxSide / Math.max(w, h);
        const small = new OffscreenCanvas(Math.max(1, Math.round(w * s)), Math.max(1, Math.round(h * s)));
        const ctx = small.getContext('2d');
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(canvas, 0, 0, small.width, small.height);
        canvas = small;
    }
    return canvas.convertToBlob({ type: 'image/jpeg', quality });
}

// Run one task by name (the worker and the inline pool both come through here)
export async function runTask(name, args, env) {
    const task = TASKS[name] || (env.extraTasks && env.extraTasks[name]);
    if (!task) throw new Error(`Unknown task ${name}`);
    try {
        return await task(args, env);
    } finally {
        if (env.store && env.store.flush) await env.store.flush();
    }
}

export function newEnv(extra = {}) {
    return { store: null, masters: mastersAccess(null), state: new Map(), ...extra };
}

export { rgbLuminanceHalf };
