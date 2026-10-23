// The engine as the album uses it (through static/js/album/engine.js): sets up a job's
// workers and storage, runs it, and cleans up. Everything runs in this browser tab;
// closing the tab stops it.

import { machine, workersFor, WorkerPool } from './pool.js';
import { OpfsStore, storageEstimate } from './store.js';
import { pruneCache } from './fetch-cache.js';
import { runStack, PROJECT_FIXED } from './stack.js';
import { runLucky } from './lucky.js';
import { openSession } from './session.js';
import { commitProject, createProject, getProject, loadTotals, projectSettings, totalsBlobs, uploadTotals } from './projects.js';

// Messages in the page's language when its translations are loaded (window._),
// otherwise English: in Node tests and inside workers
const _ = (text, vars) => {
    if (typeof globalThis._ === 'function') return globalThis._(text, vars);
    return vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text;
};


export function support() {
    const need = [
        ['Worker', typeof Worker !== 'undefined'],
        ['OffscreenCanvas', typeof OffscreenCanvas !== 'undefined'],
        ['createImageBitmap', typeof createImageBitmap !== 'undefined']
    ];
    const missing = need.filter(([, ok]) => !ok).map(([name]) => name);
    if (missing.length) {
        return { ok: false, reason: _('This browser can\'t run the processing (it lacks {missing}). A recent Chrome, Edge, Firefox or Safari can.', { missing: missing.join(', ') }) };
    }
    return { ok: true, reason: '' };
}

export function machineInfo() {
    return machine();
}

function newId() {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// Workers and storage for one job, sized for the frames it will handle
async function jobContext({ pixels = 21e6, raw = true, workers = null, signal = null, storage = true } = {}) {
    const m = machine();
    const est = await storageEstimate();
    const opfs = storage && await OpfsStore.supported();
    if (opfs) await OpfsStore.cleanStale();
    const id = newId();
    const store = opfs ? await OpfsStore.open(id) : null;
    const pool = new WorkerPool(workers || workersFor(m, pixels, { raw }));
    if (opfs) await pool.broadcast('openStore', { storeId: id });
    let cleaned = false;
    const cleanup = async () => {
        if (cleaned) return;
        cleaned = true;
        pool.close();
        if (store) await store.destroy().catch(() => {});
    };
    if (signal) signal.addEventListener('abort', () => pool.close(), { once: true });
    return { pool, store, opfs, memoryBudget: m.budget, diskFree: est.free, cleanup, machine: m };
}

function framePixels(spec) {
    const light = ((spec.frames || {}).light || [])[0] || {};
    const px = (light.width || 5616) * (light.height || 3744);
    return (spec.settings && spec.settings.debayer === 'superpixel') ? px / 4 : px;
}

// For saveStackResult: just what it uploads
function forSaving(result, job) {
    const out = {
        name: result.name, target: result.target, kind: result.kind, width: result.width, height: result.height,
        linear: result.linear, frames_used: result.frames_used, frames_total: result.frames_total,
        total_exposure_s: result.total_exposure_s, settings: result.settings, report: result.report,
        preview: result.preview, auto: result.auto, thumb: result.thumb, master: result.master,
        coverage: result.coverage ? new Blob([result.coverage]) : null,
        measured: result.measured || null,
        project: result.project || null,
        // The master can live in this job's storage: keep it until it's uploaded
        dispose: () => job.cleanup()
    };
    return out;
}

// --- measuring -----------------------------------------------------------------------

export async function measure(frames, { onProgress = null, signal = null } = {}) {
    const m = machine();
    const pool = new WorkerPool(Math.max(1, Math.min(3, m.cores - 1)));
    if (signal) signal.addEventListener('abort', () => pool.close(), { once: true });
    const out = {};
    let done = 0;
    try {
        await Promise.all(frames.map(f => pool.run('measureFrame', { entry: f }, { signal })
            .then(metrics => { out[f.name] = metrics; })
            .catch(error => {
                if (error.name === 'AbortError') throw error;
                out[f.name] = { error: error.message };
            })
            .finally(() => { if (onProgress) onProgress({ fraction: ++done / frames.length, message: _('Measured {done} of {total}', { done, total: frames.length }) }); })));
    } finally {
        pool.close();
    }
    return out;
}

// --- stacking ------------------------------------------------------------------------

// spec as stack-dialog.js builds it; spec.mode 'lucky' for lucky imaging; spec.project
// {id} or {name} to add the night to a project. Resolves to a result for
// stacks.saveStackResult(); call result.dispose() once it's saved.
export async function stack(spec, { onProgress = null, signal = null } = {}) {
    await pruneCache().catch(() => {});
    if (spec.mode === 'lucky') {
        const job = await jobContext({ pixels: 2e6, raw: false, signal, storage: false });
        try {
            const result = await runLucky(spec, { ...job, onProgress, signal });
            return forSaving(result, job);
        } catch (error) {
            await job.cleanup();
            throw error;
        }
    }
    const job = await jobContext({ pixels: framePixels(spec), raw: true, signal });
    try {
        if (!spec.project) {
            const result = await runStack(spec, { ...job, onProgress, signal });
            await result.combiner.dispose();
            return forSaving(result, job);
        }
        return forSaving(await projectNight(spec, job, { onProgress, signal }), job);
    } catch (error) {
        await job.cleanup();
        throw error;
    }
}

const scaled = (onProgress, a, b) => (p) => onProgress && onProgress({ fraction: a + (b - a) * p.fraction, message: p.message });

async function projectNight(spec, job, { onProgress, signal }) {
    const meta = spec.project.id ? await getProject(spec.project.id) : null;
    const totals = meta && meta.generation ? await loadTotals(meta, 'current', { signal }) : null;
    const result = await runStack(spec, {
        ...job, signal, onProgress: scaled(onProgress, 0, 0.85),
        project: { meta, totals, skip: new Set(meta ? [...(meta.seen || []), ...(meta.frames || [])] : []) }
    });
    let project = meta;
    if (!project) {
        project = await createProject({
            name: spec.project.name || spec.name, target: spec.target, source: result.report.source,
            width: result.width, height: result.height,
            settings: projectSettings(result.settings, PROJECT_FIXED), reference: result.projectReference
        });
    }
    if (onProgress) onProgress({ fraction: 0.86, message: _("Saving the project's totals") });
    const blobs = await totalsBlobs(result.combiner, { frames: result.frames_used, name: project.name });
    await result.combiner.dispose();
    const files = await uploadTotals(project.id, blobs, {
        signal, onProgress: (f, sent, total) => onProgress && onProgress({ fraction: 0.86 + 0.13 * f, message: _('Uploading the project\'s totals ({sent} of {total} MB)', { sent: Math.round(sent / 1048576), total: Math.round(total / 1048576) }) })
    });
    project = await commitProject(project.id, {
        mode: 'night', frames: result.night.frames, considered: result.night.considered, exposure_s: result.night.exposure_s,
        night: new Date().toISOString().slice(0, 10), files
    });
    result.project = project;
    result.name = spec.name || project.name;
    return result;
}

// Re-stack a project from scratch, a batch (a night's frames, with its calibration) at
// a time, uploading a checkpoint after each so it can resume where it stopped.
// batches: [{name, frames: {light, dark, flat, darkflat, bias}}] in order. Resolves to
// a result for saveStackResult (call dispose() after) and the finished project.
export async function rebuildProject(projectId, batches, { onProgress = null, signal = null, settings = null } = {}) {
    let project = await getProject(projectId);
    const state = project.rebuilding || { done: [], accepted: [], exposure_s: 0 };
    const done = new Set(state.done || []);
    const accepted = [...(state.accepted || [])];
    let exposure = state.exposure_s || 0;
    const todo = batches.filter(b => b.frames.light.some(e => !done.has(e.name)));
    if (!todo.length && !done.size) throw new Error(_('None of the project\'s frames are in the album any more'));
    const useSettings = { ...(project.settings || {}), ...(settings || {}) };
    const job = await jobContext({ pixels: (project.width || 5616) * (project.height || 3744), signal });
    let last = null;
    try {
        for (let i = 0; i < todo.length; i++) {
            const batch = todo[i];
            const span = [i / todo.length, (i + 1) / todo.length];
            const first = !done.size;
            const totals = first ? null : await loadTotals(project, 'rebuild', { signal });
            const meta = {
                ...project, settings: useSettings, generation: first ? 0 : 1,
                frames_total: accepted.length, exposure_total_s: exposure,
                reference: project.reference
            };
            const result = await runStack({ name: project.name, target: project.target, frames: batch.frames, settings: useSettings }, {
                ...job, signal, onProgress: scaled(onProgress, span[0], span[0] + 0.8 * (span[1] - span[0])),
                project: { meta, totals, skip: done }
            });
            result.night.considered.forEach(n => done.add(n));
            accepted.push(...result.night.frames);
            exposure += result.night.exposure_s;
            const blobs = await totalsBlobs(result.combiner, { frames: accepted.length, name: project.name });
            await result.combiner.dispose();
            const files = await uploadTotals(project.id, blobs, { signal });
            const final = i === todo.length - 1;
            project = await commitProject(project.id, final
                ? { mode: 'rebuilt', frames: accepted, considered: [...done], exposure_s: exposure, settings: useSettings, files }
                : { mode: 'checkpoint', done: [...done], accepted, total: batches.reduce((s, b) => s + b.frames.light.length, 0), exposure_s: exposure, settings: useSettings, files });
            if (onProgress) onProgress({ fraction: span[1], message: final ? _('Rebuilt') : _('Checkpoint saved ({n} frames done)', { n: done.size }) });
            last = result;
        }
        if (!last) throw new Error(_('Nothing left to rebuild'));
        last.project = project;
        last.name = _('{name} (rebuilt)', { name: project.name });
        return forSaving(last, job);
    } catch (error) {
        await job.cleanup();
        throw error;
    }
}

// --- developing ------------------------------------------------------------------------

let devPool = null;

function developPool() {
    if (!devPool) devPool = new WorkerPool(1);
    return devPool;
}

export async function develop(target, stage) {
    return openSession(developPool(), target.id ? { stack: target } : target, stage);
}
