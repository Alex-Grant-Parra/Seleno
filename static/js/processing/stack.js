// Stacking in the browser: a port of app/processing/stack.py's run().
//
//   1. Master bias, dark, dark flat and flat, each combined with rejection.
//   2. Pass 1: every light calibrated (RAW: on the mosaic) and its stars measured.
//   3. The sharpest, roundest frame becomes the reference; frames that won't match, or
//      are clearly worse than the rest, are left out (with the reason).
//   4. Pass 2: each light debayered, coloured and warped onto the reference, into the
//      combine stage (combine.js: memory, disk or streaming, chosen to fit).
//   5. The outputs: master.fits, the preview, auto.jpg and thumb.jpg, and a report.
//
// Everything heavy runs in the pool's workers; this file only coordinates.

import { chooseMode, FrameSet, HeldCombine, previewHalf, ResultSink, stripCombine, StreamCombine } from './combine.js';
import { hotPixelMap } from './calibrate.js';
import { autoParams } from './develop.js';
import { frameBytes } from './combine.js';
import { median, robustStats } from './image.js';
import { METHODS } from './integrate.js';
import { matchStars, RegistrationError, transformRotationDeg } from './register.js';
import { outputShape, outputTransform } from './frame-ops.js';

export const FRAME_TYPES = ['light', 'dark', 'flat', 'darkflat', 'bias'];

export const DEFAULT_SETTINGS = {
    debayer: 'bilinear',
    colour: 'camera',
    wb: 'daylight',
    calibration_method: 'winsorized',
    dark_scaling: true,
    hot_pixels: true,
    cosmetic: true,
    flat_per_channel: true,
    reference: 'auto',
    transform: 'similarity',
    star_sigma: 5.0,
    interpolation: 'bicubic',
    method: 'winsorized',
    kappa_low: 3.0,
    kappa_high: 3.0,
    iterations: 3,
    normalise: true,
    weighting: 'noise',
    reject_auto: true,
    reject_fwhm: null,
    reject_ecc: null,
    reject_stars: null,
    keep_best: null,
    crop_common: true,
    keep_masters: false
};

// Settings a project keeps for its whole life (calibration may change night to night)
export const PROJECT_FIXED = ['debayer', 'colour', 'wb', 'transform', 'interpolation', 'method', 'kappa_low', 'kappa_high',
    'iterations', 'normalise', 'weighting'];

export class StackError extends Error {
    constructor(message) {
        super(message);
        this.name = 'StackError';
    }
}

const pick = (v, list, fallback) => (list.includes(v) ? v : fallback);

export function cleanSettings(settings) {
    const s = { ...DEFAULT_SETTINGS };
    Object.entries(settings || {}).forEach(([k, v]) => { if (k in s) s[k] = v; });
    s.debayer = pick(s.debayer, ['bilinear', 'superpixel'], 'bilinear');
    s.colour = pick(s.colour, ['camera', 'wb', 'none'], 'camera');
    s.wb = pick(s.wb, ['daylight', 'as_shot'], 'daylight');
    s.calibration_method = pick(s.calibration_method, METHODS, 'winsorized');
    s.method = pick(s.method, METHODS, 'winsorized');
    s.transform = pick(s.transform, ['similarity', 'affine'], 'similarity');
    s.interpolation = pick(s.interpolation, ['bilinear', 'bicubic'], 'bicubic');
    s.weighting = pick(s.weighting, ['noise', 'none'], 'noise');
    for (const [k, lo, hi, d] of [['star_sigma', 2, 50, 5], ['kappa_low', 0.5, 10, 3], ['kappa_high', 0.5, 10, 3], ['iterations', 1, 10, 3]]) {
        const v = Number(s[k]);
        s[k] = Number.isFinite(v) && s[k] !== null && s[k] !== '' ? Math.min(Math.max(v, lo), hi) : d;
    }
    s.iterations = Math.trunc(s.iterations);
    for (const k of ['reject_fwhm', 'reject_ecc', 'reject_stars', 'keep_best']) {
        const v = Number(s[k]);
        s[k] = s[k] === null || s[k] === '' || s[k] === undefined || !Number.isFinite(v) ? null : v;
    }
    if (s.keep_best !== null) s.keep_best = Math.min(Math.max(s.keep_best, 0.05), 1);
    for (const k of ['dark_scaling', 'hot_pixels', 'cosmetic', 'flat_per_channel', 'normalise', 'reject_auto', 'crop_common', 'keep_masters']) s[k] = !!s[k];
    return s;
}

// The sharpest frame among those with plenty of stars and round ones (stack.pick_reference)
export function pickReference(measured, wanted = 'auto') {
    const names = measured.map(m => m.name);
    if (wanted && wanted !== 'auto' && names.includes(wanted)) return names.indexOf(wanted);
    const counts = measured.map(m => m.metrics.stars);
    const fwhm = measured.map(m => m.metrics.fwhm || Infinity);
    const ecc = measured.map(m => (m.metrics.ecc !== null && m.metrics.ecc !== undefined ? m.metrics.ecc : 1));
    const cMed = median(counts), eMed = median(ecc);
    let ok = counts.map((c, i) => c >= 0.7 * cMed && ecc[i] <= eMed + 0.1);
    if (!ok.some(Boolean)) {
        const max = Math.max(...counts);
        ok = counts.map(c => c === max);
    }
    let best = 0, bestScore = Infinity;
    fwhm.forEach((f, i) => {
        const score = ok[i] ? f : Infinity;
        if (score < bestScore) { bestScore = score; best = i; }
    });
    return best;
}

const fmt = (v, d = 2) => Number(v).toFixed(d);

// {index: reason} for frames to leave out on quality (stack.quality_rejections), plus
// keep_best: only the sharpest share of what's left
export function qualityRejections(measured, settings, refIdx = null) {
    const out = new Map();
    const metrics = measured.map(m => m.metrics);
    const fwhm = metrics.map(m => (m.fwhm ? m.fwhm : NaN));
    const ecc = metrics.map(m => (m.ecc !== null && m.ecc !== undefined ? m.ecc : NaN));
    const count = metrics.map(m => m.stars);
    if (settings.reject_auto && measured.length >= 5) {
        const [fMed, fSd] = robustStats(fwhm);
        const [eMed, eSd] = robustStats(ecc);
        const cMed = median(count);
        measured.forEach((_, i) => {
            if (Number.isFinite(fwhm[i]) && fwhm[i] > fMed + Math.max(3 * fSd, 0.15 * fMed)) out.set(i, `stars blurred (FWHM ${fmt(fwhm[i])} px, typical ${fmt(fMed)})`);
            else if (Number.isFinite(ecc[i]) && ecc[i] > Math.max(0.6, eMed + 3 * eSd)) out.set(i, `stars stretched (eccentricity ${fmt(ecc[i])})`);
            else if (count[i] < 0.5 * cMed) out.set(i, `few stars (${count[i]}, typical ${Math.trunc(cMed)}): cloud or haze?`);
        });
    }
    measured.forEach((_, i) => {
        if (out.has(i)) return;
        if (settings.reject_fwhm && Number.isFinite(fwhm[i]) && fwhm[i] > settings.reject_fwhm) out.set(i, `FWHM ${fmt(fwhm[i])} px over the ${settings.reject_fwhm} px limit`);
        else if (settings.reject_ecc && Number.isFinite(ecc[i]) && ecc[i] > settings.reject_ecc) out.set(i, `eccentricity ${fmt(ecc[i])} over the ${settings.reject_ecc} limit`);
        else if (settings.reject_stars && count[i] < settings.reject_stars) out.set(i, `${count[i]} stars, under the ${Math.trunc(settings.reject_stars)} limit`);
    });
    if (settings.keep_best && settings.keep_best < 1) {
        const left = measured.map((_, i) => i).filter(i => !out.has(i));
        const keep = Math.max(1, Math.ceil(left.length * settings.keep_best));
        left.sort((a, b) => (fwhm[a] || Infinity) - (fwhm[b] || Infinity));
        left.slice(keep).forEach(i => {
            if (i !== refIdx) out.set(i, `not among the sharpest ${Math.round(settings.keep_best * 100)}% (FWHM ${fmt(fwhm[i])} px)`);
        });
    }
    return out;
}

// Progress across weighted stages: stage(name, weight), then sub(fraction, message)
class Progress {
    constructor(onProgress, weights) {
        this.on = onProgress || (() => {});
        this.weights = weights;
        this.total = Object.values(weights).reduce((a, b) => a + b, 0) || 1;
        this.done = 0;
        this.current = null;
    }

    stage(name) {
        if (this.current) this.done += this.weights[this.current] || 0;
        this.current = name;
    }

    sub(fraction, message) {
        const w = this.weights[this.current] || 0;
        this.on({ fraction: Math.min(1, (this.done + w * Math.min(Math.max(fraction, 0), 1)) / this.total), message });
    }
}

function medianExposure(entries) {
    const known = entries.map(e => e.exposure_s).filter(Boolean);
    return known.length ? median(known) : null;
}

function aborted(signal) {
    if (signal && signal.aborted) throw new DOMException('Cancelled', 'AbortError');
}

// --- masters ------------------------------------------------------------------------

async function buildMaster(type, entries, ctx, { subtract = null, normalise = false } = {}) {
    if (!entries.length) return null;
    const { pool, shape, planes, signal, progress, store, opfs } = ctx;
    const [w, h] = shape;
    const frames = new FrameSet({ kind: opfs ? 'store' : 'memory', name: `cal-${type}`, planes, width: w, height: h, store });
    let done = 0;
    await Promise.all(entries.map((entry, k) => pool.run('loadCalibration', {
        entry, k, shape, dest: frames.dest(), normalise, perChannel: ctx.settings.flat_per_channel, subtract
    }, { signal }).then(r => {
        frames.accept(k, r);
        progress.sub(++done / (entries.length + 1), `Reading ${type === 'darkflat' ? 'dark flat' : type} frames (${done} of ${entries.length})`);
    })));
    progress.sub(entries.length / (entries.length + 1), `Combining ${entries.length} ${type} frames`);
    const n = entries.length;
    const arrays = opfs ? null : Array.from({ length: planes }, () => new Float32Array(w * h));
    await stripCombine({
        pool, frames, ks: frames.keys(), planes, signal,
        options: { method: n >= 3 ? ctx.settings.calibration_method : 'average', kappaLow: 3, kappaHigh: 3, iterations: 3 },
        onStrip: async (c, r0, r1, out) => {
            if (arrays) arrays[c].set(out.result, r0 * w);
            else await pool.run('storeWrite', { name: `master/${type}`, offset: (c * w * h + r0 * w) * 4, data: out.result }, { worker: 0, transfer: [out.result.buffer] });
        }
    });
    await frames.clear();
    return { info: { frames: n, exposure_s: medianExposure(entries) }, arrays };
}

async function readMaster(ctx, type) {
    const [w, h] = ctx.shape;
    const out = [];
    for (let c = 0; c < ctx.planes; c++) {
        const bytes = await ctx.store.read(`master/${type}`, c * w * h * 4, w * h * 4);
        out.push(new Float32Array(bytes.buffer, bytes.byteOffset, w * h));
    }
    return out;
}

// --- the job ------------------------------------------------------------------------

// spec: {name, target, frames: {light, dark, flat, darkflat, bias}, settings}, entries
// {name, url, jpeg_url, kind, exposure_s, ...}. ctx: {pool, store (main-thread view of
// the job's store, or null), opfs, memoryBudget, diskFree, onProgress, signal, mode}.
//
// ctx.project carries a night into a multi-night project: {meta (the project, or
// null for a new one), totals (its running totals, see projects.js), skip (names
// already in)}. Then the combine stage always streams, into those totals.
export async function runStack(spec, ctx) {
    const t0 = Date.now();
    const project = ctx.project || null;
    const meta = project && project.meta;
    const fixed = {};
    if (meta && meta.settings) PROJECT_FIXED.forEach(k => { if (k in meta.settings) fixed[k] = meta.settings[k]; });
    const settings = cleanSettings({ ...spec.settings, ...fixed });
    const { pool, signal } = ctx;
    const opfs = !!ctx.opfs;
    const groups = {};
    FRAME_TYPES.forEach(t => { groups[t] = [...((spec.frames || {})[t] || [])]; });
    const warnings = [];
    if (project && project.skip && project.skip.size) {
        const before = groups.light.length;
        groups.light = groups.light.filter(e => !project.skip.has(e.name));
        if (groups.light.length < before) warnings.push(`${before - groups.light.length} of the frames are already in the project, so they were left out`);
    }
    let lights = groups.light;
    if (!lights.length) throw new StackError(project ? 'Every one of these frames is already in the project' : 'No light frames to stack');

    // What the lights are: probe the first one
    const probe = await pool.run('probe', { entry: lights[0] }, { signal });
    if (probe.fellBack) {
        warnings.push('The RAW files could not be read in the browser (only Canon CR2 can be), so the JPEGs were stacked instead.');
        const toJpeg = (e) => (e.jpeg_url ? { ...e, url: e.jpeg_url, kind: 'jpeg' } : null);
        lights = lights.map(toJpeg).filter(Boolean);
        for (const t of ['dark', 'flat', 'darkflat', 'bias']) groups[t] = groups[t].map(toJpeg).filter(Boolean);
    }
    const isRaw = probe.isRaw && !probe.fellBack;
    const kind = isRaw ? 'raw' : 'jpeg';
    const otherKind = lights.filter(e => e.kind && ((e.kind === 'raw') !== isRaw));
    if (otherKind.length) {
        warnings.push(`Left out ${otherKind.length} light(s) that aren't ${kind.toUpperCase()} like the first; lights must all be RAW or all JPEG`);
        lights = lights.filter(e => !otherKind.includes(e));
    }
    for (const t of ['dark', 'flat', 'darkflat', 'bias']) {
        const mismatched = groups[t].filter(e => (e.kind === 'raw') !== isRaw);
        if (mismatched.length) {
            warnings.push(`Left out ${mismatched.length} ${t} frame(s) that aren't ${kind.toUpperCase()} like the lights`);
            groups[t] = groups[t].filter(e => (e.kind === 'raw') === isRaw);
        }
    }
    if (!isRaw && ['dark', 'flat', 'bias', 'darkflat'].some(t => groups[t].length)) {
        warnings.push("Calibrating JPEGs is approximate: the camera's tone curve isn't linear. RAW files calibrate exactly.");
    }
    if (settings.keep_masters) warnings.push("Keeping the master calibration frames isn't available in the browser yet.");

    const shape = [probe.width, probe.height];
    const planes = isRaw ? 1 : 3;
    const nCal = ['bias', 'dark', 'darkflat', 'flat'].reduce((s, t) => s + groups[t].length, 0);
    const progress = new Progress(ctx.onProgress, { masters: nCal ? 0.15 : 0, measure: 0.25, warp: 0.4, combine: 0.2 });
    const mctx = { pool, shape, planes, signal, progress, store: ctx.store, opfs, settings };

    // 1. Masters
    progress.stage('masters');
    const masters = {};
    const arrays = {};
    const take = (type, m) => {
        if (!m) return;
        masters[type] = m.info;
        if (m.arrays) arrays[type] = m.arrays;
    };
    const share = async (hot = null) => {
        const spec2 = { info: masters, hot, width: shape[0], height: shape[1], planes, arrays: opfs ? null : arrays };
        await pool.broadcast('setMasters', spec2);
    };
    take('bias', await buildMaster('bias', groups.bias, mctx));
    take('dark', await buildMaster('dark', groups.dark, mctx));
    take('darkflat', await buildMaster('darkflat', groups.darkflat, mctx));
    await share();
    const flatSub = masters.darkflat ? 'darkflat' : masters.bias ? 'bias' : null;
    take('flat', await buildMaster('flat', groups.flat, mctx, { subtract: flatSub, normalise: true }));
    if (masters.flat && !flatSub) warnings.push('Flats were used without bias or dark flats; add bias frames for exact flat correction');
    let hot = null;
    if (masters.dark && settings.hot_pixels) {
        const dark = opfs ? await readMaster(mctx, 'dark') : arrays.dark;
        hot = hotPixelMap({ planes: dark, width: shape[0], height: shape[1], pattern: isRaw ? probe.pattern : null });
    }
    const mastersReport = { ...masters };
    if (hot) mastersReport.hot_pixels = hot.length;
    // Lights are calibrated against the dark (or bias) and flat only
    delete masters.darkflat;
    if (masters.dark && !(settings.dark_scaling)) delete masters.bias;
    await share(hot);
    aborted(signal);

    // 2. Pass 1: measure every light
    progress.stage('measure');
    const perCal = frameBytes(shape[0], shape[1], planes);
    const cacheDest = opfs && lights.length * perCal < 0.4 * (ctx.diskFree || 0) ? 'calcache' : null;
    let done = 0;
    const measured = await Promise.all(lights.map((entry, k) => pool.run('measureLight', {
        entry, k, shape, exposure: entry.exposure_s || null, settings, cacheDest
    }, { signal }).then(r => ({ name: entry.name, entry, metrics: r.summary, xy: r.xy, cached: r.cached }))
        .catch(error => {
            if (error.name === 'AbortError') throw error;
            return { name: entry.name, entry, metrics: null, error: error.message };
        })
        .finally(() => progress.sub(++done / lights.length, `Measuring stars (${done} of ${lights.length})`))));
    aborted(signal);

    const usable = measured.filter(m => m.metrics);
    if (!usable.length) throw new StackError(`None of the light frames could be read${measured[0] && measured[0].error ? `: ${measured[0].error}` : ''}`);
    const report = new Map(measured.map(m => [m.name, { name: m.name, accepted: false, metrics: m.metrics, reason: m.error || null, exposure_s: m.entry.exposure_s || null }]));

    // 3. Reference, rejection, alignment
    const projectRef = meta && meta.reference && meta.reference.xy ? meta.reference : null;
    const single = usable.length === 1 && !projectRef && !(meta && meta.generation);
    const refIdx = pickReference(usable, settings.reference);
    const ref = usable[refIdx];
    const refXy = projectRef ? projectRef.xy : ref.xy;
    if (projectRef && meta.source && meta.source !== (probe.isRaw && !probe.fellBack ? 'raw' : 'jpeg')) {
        throw new StackError(`This project is made from ${meta.source.toUpperCase()} files; these frames aren't`);
    }
    if (!single && !projectRef && ref.metrics.stars < 8) {
        throw new StackError(`Only ${ref.metrics.stars} stars found in the best frame, too few to align. For the Moon or a planet, use lucky imaging instead.`);
    }
    const rejections = single ? new Map() : qualityRejections(usable, settings, refIdx);
    rejections.delete(refIdx);
    const accepted = [];
    usable.forEach((m, i) => {
        const r = report.get(m.name);
        if (rejections.has(i)) { r.reason = rejections.get(i); return; }
        let mHalf;
        if (i === refIdx && !projectRef) {
            mHalf = [[1, 0, 0], [0, 1, 0]];
            Object.assign(r, { matched: m.xy.length, rms: 0 });
        } else {
            try {
                const match = matchStars(refXy, m.xy, { transform: settings.transform });
                mHalf = match.m;
                Object.assign(r, { matched: match.matched, rms: Math.round(match.rms * 2 * 1000) / 1000 });
            } catch (error) {
                if (!(error instanceof RegistrationError)) throw error;
                r.reason = `couldn't align: ${error.message}`;
                return;
            }
        }
        r.shift_px = [Math.round(mHalf[0][2] * 20) / 10, Math.round(mHalf[1][2] * 20) / 10];
        r.rotation_deg = Math.round(transformRotationDeg(mHalf) * 1000) / 1000;
        accepted.push({ m, mHalf });
    });
    if (!accepted.length) throw new StackError('No frames could be aligned to the reference');
    // The reference goes first: the streaming mode builds on it
    accepted.sort((a, b) => (a.m === ref ? -1 : b.m === ref ? 1 : 0));
    if (project) accepted.forEach(a => { report.get(a.m.name).project = true; });

    // 4. Pass 2: warp into the combine stage
    const [outW, outH] = outputShape(shape[0], shape[1], settings, isRaw);
    if (meta && meta.width && (meta.width !== outW || meta.height !== outH)) {
        throw new StackError(`These frames make a ${outW} × ${outH} image; the project is ${meta.width} × ${meta.height}`);
    }
    const n = accepted.length;
    const choice = project
        ? { mode: 'stream', why: 'a project adds each night to its running totals' }
        : chooseMode({ n, width: outW, height: outH, memoryBudget: ctx.memoryBudget, diskFree: ctx.diskFree || 0, opfs, force: ctx.mode || null });
    const common = { pool, store: ctx.store, width: outW, height: outH, n, settings, refK: 0, signal };
    const combiner = choice.mode === 'stream'
        ? new StreamCombine({
            ...common, opfs, memoryBudget: ctx.memoryBudget, referenceFrames: ctx.referenceFrames,
            reference: projectRef && projectRef.loc ? { loc: projectRef.loc, scale: projectRef.scale } : null,
            totals: project ? project.totals : null
        })
        : new HeldCombine(choice.mode, common);
    if (combiner.begin) await combiner.begin();

    progress.stage('warp');
    const cubic = settings.interpolation === 'bicubic';
    let warped = 0;
    const warpOne = (k) => {
        const { m, mHalf } = accepted[k];
        return pool.run('warpLight', {
            entry: m.entry, k, shape, exposure: m.entry.exposure_s || null, settings, cached: m.cached,
            mOut: outputTransform(mHalf, settings, isRaw), outW, outH, cubic, dest: combiner.dest()
        }, { signal, transfer: [] }).then(async (res) => {
            await combiner.add(k, res, { noise: Math.max(m.metrics.noise, 1e-7) });
            progress.sub(++warped / n, `Aligning frames (${warped} of ${n})`);
        });
    };
    // A few in flight at once (so returned frames don't pile up), in order
    const runRange = async (from, to) => {
        let next = from;
        const lanes = Math.max(1, Math.min(pool.size + 1, to - from));
        await Promise.all(Array.from({ length: lanes }, async () => {
            while (next < to) {
                aborted(signal);
                await warpOne(next++);
            }
        }));
    };
    try {
        if (combiner.mode === 'stream' && !combiner.ready) {
            const first = Math.min(n, combiner.referenceFrames);
            await runRange(0, first);
            if (!combiner.ready) await combiner.buildLimits();
            await runRange(first, n);
        } else {
            await runRange(0, n);
        }
        if (cacheDest && ctx.store) await ctx.store.removePrefix(`${cacheDest}/`);

        // Combine and write the result
        progress.stage('combine');
        const nightExposure = accepted.reduce((s, a) => s + Number(a.m.entry.exposure_s || 0), 0);
        const before = meta ? { frames: meta.frames_total || 0, exposure: meta.exposure_total_s || 0 } : { frames: 0, exposure: 0 };
        const totalExposure = before.exposure + nightExposure;
        const nAll = before.frames + n;
        const method = nAll >= 3 ? settings.method : 'average';
        const header = {
            IMAGETYP: 'Master light',
            NCOMBINE: [nAll, 'frames combined'],
            EXPTIME: totalExposure ? [totalExposure, 'total exposure, s'] : null,
            OBJECT: spec.target || null,
            CREATOR: 'Seleno (in the browser)',
            DATE: new Date().toISOString().slice(0, 19),
            REFFRAME: (projectRef ? projectRef.name || '' : ref.name).slice(0, 60),
            REJECT: method,
            COMBMODE: choice.mode
        };
        const sink = new ResultSink({ width: outW, height: outH, header, store: opfs ? ctx.store : null, pool: opfs ? pool : null, needed: nAll });
        progress.sub(0, `Combining ${n} frames`);
        const stats = await combiner.finish(sink, (f) => progress.sub(f, `Combining ${n} frames`));
        const out = await sink.finish();
        accepted.forEach(a => { const r = report.get(a.m.name); r.accepted = true; r.reason = null; });
        const crop = settings.crop_common && nAll > 1 ? sink.crop() : null;

        // 5. Outputs
        progress.sub(1, 'Writing the result');
        const params = autoParams(false);
        if (crop) params.crop = crop;
        const outputs = await pool.run('autoOutputs', { preview: out.preview, params, planetary: false }, { signal });
        const pixels = outW * outH * 3;
        const result = {
            name: spec.name,
            target: spec.target || null,
            kind: single ? 'single' : 'stack',
            width: outW,
            height: outH,
            linear: true,
            frames_used: nAll,
            frames_total: before.frames + groups.light.length,
            total_exposure_s: totalExposure,
            settings,
            report: {
                kind: single ? 'single' : 'stack',
                reference: projectRef ? projectRef.name : ref.name,
                frames: [...report.values()],
                masters: mastersReport,
                crop,
                auto_params: params,
                combine: { mode: choice.mode, why: choice.why },
                stats: {
                    seconds: Math.round((Date.now() - t0) / 100) / 10,
                    rejected_low_pct: Math.round(1e5 * stats.rejectedLow / Math.max(pixels * n, 1)) / 1e3,
                    rejected_high_pct: Math.round(1e5 * stats.rejectedHigh / Math.max(pixels * n, 1)) / 1e3
                },
                warnings,
                engine: 'browser',
                source: kind
            },
            preview: { width: out.preview.width, height: out.preview.height, data: previewHalf(out.preview) },
            previewPlanes: out.preview,
            auto: outputs.auto,
            thumb: outputs.thumb,
            master: out.master,
            coverage: out.coverage,
            measured: Object.fromEntries(measured.filter(m => m.metrics).map(m => [m.name, m.metrics])),
            combiner
        };
        if (project) {
            result.night = {
                frames: accepted.map(a => a.m.name),
                considered: measured.map(m => m.name),
                exposure_s: nightExposure
            };
            result.projectReference = projectRef || {
                name: ref.name, xy: ref.xy.map(([x, y]) => [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000]),
                loc: combiner.reference.loc, scale: combiner.reference.scale
            };
        }
        return result;
    } catch (error) {
        await combiner.dispose().catch(() => {});
        throw error;
    }
}
