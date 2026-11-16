// Projects: long integrations built up night by night. The running totals (weighted
// sums, weights, coverage and rejection limits) live on the server as FITS files
// (controllers/projects_api.py); each night they're read back a band of rows at a
// time into the streaming shards, the night's frames are added, and the new totals go
// back up in chunks. See stack.js (ctx.project) for the stacking side.

import { decodeStrip, FitsAssembler, parseFitsHeader, stripPlacement } from './fits.js';

// Messages in the page's language when its translations are loaded (window._),
// otherwise English: in Node tests and inside workers
const _ = (text, vars) => {
    if (typeof globalThis._ === 'function') return globalThis._(text, vars);
    return vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text;
};


const CHUNK = 32 * 1024 * 1024;
export const TOTALS = { sum: 3, wsum: 3, coverage: 1, limits: 6 };

async function range(url, start, end, signal) {
    const r = await fetch(url, { headers: { Range: `bytes=${start}-${end - 1}` }, credentials: 'same-origin', signal });
    if (!r.ok) throw new Error(_('Couldn\'t read the project\'s totals ({status})', { status: r.status }));
    const buf = new Uint8Array(await r.arrayBuffer());
    return r.status === 206 ? buf : buf.subarray(start, end);
}

// The saved totals of a project ('current', or a rebuild's 'rebuild' checkpoint), read
// lazily: rows(y0, y1) gives one shard's part
export async function loadTotals(project, area = 'current', { signal = null } = {}) {
    const base = `/album/projects/${project.id}/${area}`;
    const files = {};
    for (const name of Object.keys(TOTALS)) {
        const url = `${base}/${name}.fits`;
        const { header, dataOffset } = parseFitsHeader(await range(url, 0, 2880 * 2, signal));
        files[name] = { url, header, dataOffset, width: header.NAXIS1, height: header.NAXIS2 };
    }
    const hasLimits = files.limits.header.LIMITS === true;
    const read = async (name, plane, y0, y1) => {
        const f = files[name];
        const { offset, bytes } = stripPlacement(f.width, f.height, plane, y0, y1);
        return decodeStrip(await range(f.url, f.dataOffset + offset, f.dataOffset + offset + bytes, signal), f.width, y0, y1);
    };
    return {
        hasLimits,
        frames: files.sum.header.NFRAMES || 0,
        async rows(y0, y1) {
            const planes = async (name, n, first = 0) => Promise.all(Array.from({ length: n }, (_unused, c) => read(name, first + c, y0, y1)));
            const cov = await read('coverage', 0, y0, y1);
            return {
                sum: await planes('sum', 3),
                wsum: await planes('wsum', 3),
                coverage: Uint16Array.from(cov, v => Math.min(65535, Math.max(0, Math.round(v)))),
                low: hasLimits ? await planes('limits', 3, 0) : null,
                high: hasLimits ? await planes('limits', 3, 3) : null,
                frames: files.sum.header.NFRAMES || 0
            };
        }
    };
}

// The streaming combiner's totals as four FITS Blobs, a shard at a time
export async function totalsBlobs(combiner, { frames = 0, name = '' } = {}) {
    const { width: w, height: h } = combiner;
    const header = (what) => ({ IMAGETYP: `Project totals: ${what}`, CREATOR: 'Seleno (in the browser)', OBJECT: name || null, NFRAMES: frames });
    const out = {
        sum: new FitsAssembler(w, h, 3, header('weighted sums')),
        wsum: new FitsAssembler(w, h, 3, header('weights')),
        coverage: new FitsAssembler(w, h, 1, header('coverage')),
        limits: null
    };
    let hasLimits = false;
    for (const sh of combiner.shards) {
        const t = await combiner.pool.run('shardTotals', { id: sh.id }, { worker: sh.worker });
        if (t.low && !out.limits) {
            hasLimits = true;
            out.limits = new FitsAssembler(w, h, 6, { ...header('rejection limits, low RGB then high RGB'), LIMITS: true });
        }
        for (let c = 0; c < 3; c++) {
            out.sum.put(c, t.y0, t.y1, t.sum[c]);
            out.wsum.put(c, t.y0, t.y1, t.wsum[c]);
            if (t.low) {
                out.limits.put(c, t.y0, t.y1, t.low[c]);
                out.limits.put(3 + c, t.y0, t.y1, t.high[c]);
            }
        }
        out.coverage.put(0, t.y0, t.y1, Float32Array.from(t.coverage));
    }
    if (!hasLimits) {
        out.limits = new FitsAssembler(w, h, 6, { ...header('rejection limits (none yet)'), LIMITS: false });
        const rows = 64;
        for (let y = 0; y < h; y += rows) {
            const n = Math.min(rows, h - y) * w;
            for (let c = 0; c < 6; c++) out.limits.put(c, y, y + n / w, new Float32Array(n).fill(c < 3 ? -Infinity : Infinity));
        }
    }
    return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.blob()]));
}

// Upload a file into the project's staging area, resuming where the server is
async function uploadFile(projectId, name, blob, { signal, onBytes }) {
    let offset = 0, retries = 0;
    while (offset < blob.size) {
        const chunk = blob.slice(offset, Math.min(blob.size, offset + CHUNK));
        let response;
        try {
            response = await fetch(`/album/api/projects/${projectId}/upload/${name}?offset=${offset}`, { method: 'PUT', body: chunk, signal, credentials: 'same-origin' });
        } catch (error) {
            if (error.name === 'AbortError' || ++retries > 5) throw error;
            await new Promise(r => setTimeout(r, 2000 * retries));
            continue;
        }
        const body = await response.json().catch(() => ({}));
        if (response.status === 409 && typeof body.received === 'number') {
            offset = body.received;
            continue;
        }
        if (!response.ok) throw new Error(body.message || _('Upload failed ({status})', { status: response.status }));
        if (onBytes) onBytes(body.received - offset);
        offset = body.received;
        retries = 0;
    }
}

// Upload the totals; returns {file: bytes} for the commit to check
export async function uploadTotals(projectId, blobs, { signal = null, onProgress = null } = {}) {
    const total = Object.values(blobs).reduce((s, b) => s + b.size, 0);
    let sent = 0;
    const sizes = {};
    for (const [key, blob] of Object.entries(blobs)) {
        await uploadFile(projectId, `${key}.fits`, blob, {
            signal, onBytes: (n) => { sent += n; if (onProgress) onProgress(sent / total, sent, total); }
        });
        sizes[`${key}.fits`] = blob.size;
    }
    return sizes;
}

async function api(url, options = {}) {
    const init = { credentials: 'same-origin', method: options.method || (options.json ? 'POST' : 'GET'), headers: {} };
    if (options.json) {
        init.body = JSON.stringify(options.json);
        init.headers['Content-Type'] = 'application/json';
    }
    if (typeof window !== 'undefined' && window.CSRF_TOKEN) init.headers['X-CSRFToken'] = window.CSRF_TOKEN;
    const r = await fetch(url, init);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.message || `${r.status}`);
    return body;
}

export async function listProjects() {
    return (await api('/album/api/projects')).projects || [];
}

export async function getProject(id) {
    return (await api(`/album/api/projects/${id}`)).project;
}

export async function createProject(fields) {
    return (await api('/album/api/projects', { json: fields })).project;
}

export async function commitProject(id, body) {
    return (await api(`/album/api/projects/${id}/commit`, { json: body })).project;
}

export async function updateProject(id, fields) {
    return (await api(`/album/api/projects/${id}`, { method: 'PATCH', json: fields })).project;
}

export async function deleteProject(id) {
    return api(`/album/api/projects/${id}`, { method: 'DELETE' });
}

export async function cancelRebuild(id) {
    return (await api(`/album/api/projects/${id}/rebuild`, { method: 'DELETE' })).project;
}

// The settings a project keeps, from a stack's
export function projectSettings(settings, fixed) {
    const out = {};
    fixed.forEach(k => { if (k in settings) out[k] = settings[k]; });
    return out;
}
