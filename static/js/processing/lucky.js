// Lucky imaging for the Moon and planets (app/processing/planetary.py), in one pass:
// every frame is scored for sharpness in a worker and cropped around the planet; only
// the best crops are kept (so a long video needs little memory); then they're lined
// up with the sharpest by phase correlation and averaged. The result is
// display-referred, like the video itself. Multi-point alignment comes later.
//
// Sources: a video the browser can play (decoded by a <video> element, frame by frame
// with requestVideoFrameCallback), a SER file (read in JS), or a burst of photos.

import { autoParams } from './develop.js';
import { fitsBlob } from './fits.js';
import { downsamplePlanes, median } from './image.js';
import { previewHalf } from './combine.js';
import { extensionOf } from './decode.js';
import { serFrames } from './ser.js';

export const DEFAULT_LUCKY = { keep_percent: 25, min_frames: 5, max_frames: 4000, roi: 'auto' };
export const VIDEO_EXTENSIONS = ['mp4', 'm4v', 'mov', 'webm', 'mkv', 'avi'];

export function cleanLucky(settings) {
    const s = { ...DEFAULT_LUCKY, ...(settings || {}) };
    s.keep_percent = Math.min(Math.max(Number(s.keep_percent) || 25, 1), 100);
    s.min_frames = Math.max(1, Math.trunc(Number(s.min_frames) || 5));
    s.max_frames = Math.min(Math.max(Math.trunc(Number(s.max_frames) || 4000), 10), 20000);
    s.roi = s.roi === 'full' ? 'full' : 'auto';
    return s;
}

// --- sources: {count (an estimate), frames(): async iterator of {frame, transfer}} -----

function photoSource(entries, max) {
    const list = entries.slice(0, max);
    return {
        count: list.length,
        async *frames() {
            for (const entry of list) yield { frame: { entry }, transfer: [] };
        }
    };
}

function serSource(url, max, signal) {
    const source = {
        count: max,
        async *frames() {
            for await (const item of serFrames(url, { signal, maxFrames: max })) {
                if (item.header) {
                    source.count = Math.min(max, item.header.frames);
                    source.header = item.header;
                    continue;
                }
                yield { frame: { ser: source.header, raw: item.raw }, transfer: [item.raw.buffer] };
            }
        }
    };
    return source;
}

// Frames of a video, as the browser plays it. Pauses while the workers catch up;
// hidden tabs stop painting video, so the tab has to stay in view.
function videoSource(url, max, signal) {
    const source = {
        count: max,
        skipped: 0,
        async *frames() {
            const video = document.createElement('video');
            video.muted = true;
            video.playsInline = true;
            video.preload = 'auto';
            video.src = url;
            await new Promise((resolve, reject) => {
                video.onloadedmetadata = resolve;
                video.onerror = () => reject(new Error("This browser can't play this video. MP4 (H.264) and WebM play everywhere; SER files work too."));
            });
            const w = video.videoWidth, h = video.videoHeight;
            source.count = Math.min(max, Math.round((video.duration || 60) * 30));
            const canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d', { willReadFrequently: true });
            const queue = [];
            let ended = false, wake = null, last = 0, taken = 0;
            const notify = () => { if (wake) { const f = wake; wake = null; f(); } };
            const grab = (meta) => {
                ctx.drawImage(video, 0, 0, w, h);
                const data = ctx.getImageData(0, 0, w, h).data;
                if (meta && meta.presentedFrames && last && meta.presentedFrames > last + 1) source.skipped += meta.presentedFrames - last - 1;
                if (meta && meta.presentedFrames) last = meta.presentedFrames;
                queue.push({ frame: { rgba: data, width: w, height: h }, transfer: [data.buffer] });
                if (++taken >= max) { ended = true; video.pause(); }
                if (queue.length > 12) video.pause();
                notify();
            };
            const useCallback = typeof video.requestVideoFrameCallback === 'function';
            if (useCallback) {
                const onFrame = (now, meta) => {
                    if (ended) return;
                    grab(meta);
                    video.requestVideoFrameCallback(onFrame);
                };
                video.requestVideoFrameCallback(onFrame);
                video.onended = () => { ended = true; notify(); };
                await video.play();
            } else {
                // No frame callbacks: step through by seeking (slower, but every frame)
                (async () => {
                    for (let t = 0; t < video.duration && !ended; t += 1 / 30) {
                        while (queue.length > 12 && !ended) await new Promise(r => setTimeout(r, 20));
                        video.currentTime = t;
                        await new Promise(r => { video.onseeked = r; });
                        grab(null);
                    }
                    ended = true;
                    notify();
                })();
            }
            try {
                for (;;) {
                    if (signal && signal.aborted) throw new DOMException('Cancelled', 'AbortError');
                    if (queue.length) {
                        yield queue.shift();
                        if (useCallback && !ended && video.paused && queue.length < 4) video.play().catch(() => {});
                        continue;
                    }
                    if (ended) break;
                    await new Promise(r => { wake = r; });
                }
            } finally {
                ended = true;
                video.pause();
                video.removeAttribute('src');
                video.load();
            }
        }
    };
    return source;
}

export function sourceFor(entries, settings, signal) {
    if (entries.length === 1) {
        const e = entries[0];
        const ext = extensionOf(e.url);
        if (ext === 'ser') return serSource(e.url, settings.max_frames, signal);
        if (e.kind === 'video' || VIDEO_EXTENSIONS.includes(ext)) return videoSource(e.url, settings.max_frames, signal);
    }
    return photoSource(entries, settings.max_frames);
}

// --- the job --------------------------------------------------------------------------

// spec: {name, target, frames: {light: [entries]}, settings}. ctx: {pool, onProgress,
// signal, memoryBudget, source (to override where frames come from)}
export async function runLucky(spec, ctx) {
    const t0 = Date.now();
    const settings = cleanLucky(spec.settings);
    const entries = (spec.frames && spec.frames.light) || [];
    const { pool, signal } = ctx;
    const progress = ctx.onProgress || (() => {});
    if (!entries.length && !ctx.source) throw new Error('Nothing to stack');
    const source = ctx.source || sourceFor(entries, settings, signal);

    // Pass 1: score everything, keep the best crops
    const scores = [];
    const kept = [];
    let crop = null;                     // [w, h] once the first frames have been seen
    const early = [];                    // the first frames, until the crop size is known
    let capacity = 0;
    let frameW = 0, frameH = 0;
    const inFlight = new Set();
    const limit = Math.max(2, pool.size * 2);

    const keep = (index, r) => {
        scores[index] = { quality: r.quality, brightness: r.brightness };
        if (r.crop) {
            kept.push({ index, quality: r.quality, brightness: r.brightness, crop: r.crop });
            if (kept.length > capacity * 1.5) {
                kept.sort((a, b) => b.quality - a.quality);
                kept.length = capacity;
            }
        }
    };
    const decideCrop = () => {
        const extent = median(early.map(e => e.r.extent));
        if (settings.roi === 'full' || extent > 0.7 * Math.min(frameW, frameH)) crop = [frameW, frameH];
        else {
            let side = Math.trunc(Math.min(Math.max(extent * 1.6, 64), Math.min(frameW, frameH)));
            side -= side % 2;
            crop = [side, side];
        }
        const cropBytes = crop[0] * crop[1] * 6;
        const wanted = Math.ceil(Math.max(settings.min_frames, (source.count || 100) * settings.keep_percent / 100) * 1.25) + 4;
        capacity = Math.max(settings.min_frames, Math.min(wanted, Math.floor(((ctx.memoryBudget || 2 * 1024 ** 3) * 0.3) / cropBytes)));
    };

    let index = 0;
    for await (const item of source.frames()) {
        if (signal && signal.aborted) throw new DOMException('Cancelled', 'AbortError');
        const i = index++;
        if (!crop) {
            // The first few frames: scored without a crop (their crops are cut once the size is known)
            const keepCopy = item.frame.rgba ? { ...item.frame, rgba: item.frame.rgba.slice() } : item.frame.raw ? { ...item.frame, raw: item.frame.raw.slice() } : item.frame;
            const r = await pool.run('luckyFrame', { frame: item.frame }, { transfer: item.transfer, signal });
            frameW = r.width; frameH = r.height;
            early.push({ i, frame: keepCopy, r });
            if (early.length >= 5) {
                decideCrop();
                for (const e of early) keep(e.i, await pool.run('luckyFrame', { frame: e.frame, cropW: crop[0], cropH: crop[1] }, { signal }));
                early.length = 0;
            }
            continue;
        }
        while (inFlight.size >= limit) await Promise.race(inFlight);
        const p = pool.run('luckyFrame', { frame: item.frame, cropW: crop[0], cropH: crop[1] }, { transfer: item.transfer, signal })
            .then(r => {
                if (r.width !== frameW || r.height !== frameH) throw new Error("The frames aren't all the same size");
                keep(i, r);
            });
        const tracked = p.finally(() => inFlight.delete(tracked));
        inFlight.add(tracked);
        if (i % 10 === 0) progress({ fraction: 0.6 * Math.min(1, i / Math.max(source.count || i + 50, 1)), message: `Scoring frames for sharpness (${i + 1})` });
    }
    await Promise.all(inFlight);
    if (!crop && early.length) {
        decideCrop();
        for (const e of early) keep(e.i, await pool.run('luckyFrame', { frame: e.frame, cropW: crop[0], cropH: crop[1] }, { signal }));
    }
    const n = scores.filter(Boolean).length;
    if (n < 2) throw new Error('Need at least two frames for lucky imaging');

    // The best usable frames (bright enough: not clouded over)
    const medBright = median(scores.filter(Boolean).map(s => s.brightness));
    const want = Math.max(settings.min_frames, Math.round(n * settings.keep_percent / 100));
    const chosen = kept.filter(k => k.brightness >= 0.5 * medBright).sort((a, b) => b.quality - a.quality).slice(0, want);
    if (!chosen.length) throw new Error('No usable frames (all too dark)');
    const ref = chosen[0];

    // Pass 2: line up and average
    const [cw, ch] = crop;
    const total = [0, 1, 2].map(() => new Float64Array(cw * ch));
    const weight = new Float64Array(cw * ch);
    const shifts = [];
    let used = 0, done = 0;
    const refMsg = { key: `ref-${t0}-${ref.index}`, data: ref.crop };
    const queue = chosen.slice();
    await Promise.all(Array.from({ length: Math.min(pool.size + 1, queue.length) }, async () => {
        while (queue.length) {
            const k = queue.shift();
            const out = await pool.run('luckyAlign', { ref: refMsg, crop: k.crop, w: cw, h: ch }, { signal });
            done++;
            if (!out.skipped) {
                const [r, g, b] = out.planes;
                for (let i = 0; i < cw * ch; i++) {
                    if (r[i] === r[i] && g[i] === g[i] && b[i] === b[i]) {
                        total[0][i] += r[i]; total[1][i] += g[i]; total[2][i] += b[i];
                        weight[i]++;
                    }
                }
                shifts.push([Math.round(out.dx * 100) / 100, Math.round(out.dy * 100) / 100]);
                used++;
            }
            progress({ fraction: 0.6 + 0.35 * done / chosen.length, message: `Stacking the best frames (${done} of ${chosen.length})` });
        }
    }));
    if (!used) throw new Error('None of the chosen frames could be aligned');
    const planes = total.map(t => Float32Array.from(t, (v, i) => v / Math.max(weight[i], 1)));

    // Outputs
    progress({ fraction: 0.96, message: 'Writing the result' });
    const master = fitsBlob(planes, cw, ch, { IMAGETYP: 'Lucky imaging stack', NCOMBINE: used, CREATOR: 'Seleno (in the browser)', OBJECT: spec.target || null });
    const preview = downsamplePlanes(planes, cw, ch, 1600);
    const params = autoParams(true);
    const outputs = await pool.run('autoOutputs', { preview: { planes: preview.planes, width: preview.width, height: preview.height }, params, planetary: true }, { signal });
    const qs = scores.filter(Boolean).map(s => s.quality);
    const chosenSet = new Set(chosen.map(c => c.index));
    return {
        name: spec.name,
        target: spec.target || null,
        kind: 'planetary',
        width: cw,
        height: ch,
        linear: false,
        frames_used: used,
        frames_total: n,
        total_exposure_s: null,
        settings,
        report: {
            kind: 'planetary',
            reference: `frame ${ref.index + 1}`,
            keep_percent: settings.keep_percent,
            quality_range: [Math.min(...qs), median(qs), Math.max(...qs)],
            frames: scores.map((s, i) => ({ name: entries.length === 1 ? entries[0].name : (entries[i] || {}).name, frame: i, quality: s ? Math.round(s.quality * 1e4) / 1e4 : null, accepted: chosenSet.has(i) })).slice(0, 5000),
            shifts: shifts.slice(0, 2000),
            auto_params: params,
            stats: { seconds: Math.round((Date.now() - t0) / 100) / 10, frames_skipped_by_player: source.skipped || 0 },
            warnings: source.skipped ? [`The video player skipped ${source.skipped} frames; the rest were scored.`] : [],
            engine: 'browser'
        },
        preview: { width: preview.width, height: preview.height, data: previewHalf(preview) },
        previewPlanes: { planes: preview.planes, width: preview.width, height: preview.height },
        auto: outputs.auto,
        thumb: outputs.thumb,
        master
    };
}
