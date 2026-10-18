// Saved stacks: listing them, and saving a result the browser made to the server
// (description first, then the preview files, then the master in chunks the server
// can resume from).

import { startJob } from './jobs.js';

const CHUNK = 32 * 1024 * 1024;

export async function loadStacks() {
    const data = await Seleno.api('/album/api/stacks');
    return data.stacks || [];
}

export async function loadStack(id) {
    const data = await Seleno.api(`/album/api/stacks/${id}`);
    return data.stack;
}

async function putFile(stackId, name, body, signal) {
    const response = await fetch(`/album/api/stacks/${stackId}/file/${name}`, { method: 'PUT', body, signal });
    if (!response.ok) {
        let message = `${name}: ${response.status}`;
        try { message = (await response.json()).message || message; } catch (error) { /* not JSON */ }
        throw new Error(message);
    }
}

async function putMaster(stackId, master, onProgress, signal) {
    let offset = 0;
    let retries = 0;
    while (offset < master.size) {
        const chunk = master.slice(offset, Math.min(master.size, offset + CHUNK));
        let response;
        try {
            response = await fetch(`/album/api/stacks/${stackId}/master?offset=${offset}`, { method: 'PUT', body: chunk, signal });
        } catch (error) {
            if (error.name === 'AbortError' || ++retries > 5) throw error;
            await new Promise(resolve => setTimeout(resolve, 2000 * retries));
            continue;
        }
        const body = await response.json().catch(() => ({}));
        if (response.status === 409 && typeof body.received === 'number') {
            offset = body.received;        // the server already has more (a retried chunk)
            continue;
        }
        if (!response.ok) throw new Error(body.message || `Upload failed (${response.status})`);
        offset = body.received;
        retries = 0;
        onProgress(offset / master.size);
    }
}

// Save a result from the engine (see engine.js for its shape). Resolves to the stack.
export async function saveStackResult(result, { signal } = {}) {
    const job = startJob(`Saving ${result.name}`, { kind: 'upload' });
    try {
        const master = result.master || null;
        const created = await Seleno.api('/album/api/stacks', {
            json: {
                name: result.name,
                target: result.target,
                kind: result.kind || 'stack',
                width: result.width,
                height: result.height,
                preview_width: result.preview ? result.preview.width : null,
                preview_height: result.preview ? result.preview.height : null,
                frames_used: result.frames_used,
                frames_total: result.frames_total,
                total_exposure_s: result.total_exposure_s,
                linear: result.linear !== false,
                settings: result.settings || null,
                report: result.report || null,
                edit: result.edit || null,
                master_bytes: master ? master.size : null
            },
            signal
        });
        const stack = created.stack;
        job.update(0.02, 'Saving the preview');
        if (result.preview) await putFile(stack.id, 'preview.f16', result.preview.data, signal);
        await putFile(stack.id, 'auto.jpg', result.auto, signal);
        await putFile(stack.id, 'thumb.jpg', result.thumb, signal);
        if (result.coverage) await putFile(stack.id, 'coverage.u8', result.coverage, signal);
        if (master) {
            await putMaster(stack.id, master, fraction => job.update(0.05 + 0.93 * fraction,
                `Uploading the master: ${Seleno.fmt.bytes(fraction * master.size)} of ${Seleno.fmt.bytes(master.size)}`), signal);
        }
        const finished = await Seleno.api(`/album/api/stacks/${stack.id}/finish`, { method: 'POST' });
        job.finish(`Saved (${Seleno.fmt.bytes(finished.stack.bytes_total)})`);
        return finished.stack;
    } catch (error) {
        job.fail(error);
        throw error;
    }
}

export async function deleteStack(id) {
    await Seleno.api(`/album/api/stacks/${id}`, { method: 'DELETE' });
}

export async function updateStack(id, changes) {
    const data = await Seleno.api(`/album/api/stacks/${id}`, { method: 'PATCH', json: changes });
    return data.stack;
}

export function renderStackCards(box, stacks, { onOpen, onDelete, onRename }) {
    const el = Seleno.el;
    box.innerHTML = '';
    const ready = stacks.filter(s => s.state === 'ready');
    if (!ready.length) {
        box.appendChild(el('div', { className: 'ws-card ws-empty', style: { gridColumn: '1/-1' } },
            'No stacked images yet. Select some photos in Photos and choose Stack.'));
        return;
    }
    ready.forEach(stack => {
        const exposure = stack.total_exposure_s ? ` · ${Seleno.fmt.duration(stack.total_exposure_s)} of light` : '';
        const frames = stack.frames_used ? `${stack.frames_used}${stack.frames_total ? ' of ' + stack.frames_total : ''} frames` : '';
        box.appendChild(el('div', { className: 'ws-card ws-stack-card' }, [
            el('button', { type: 'button', className: 'ws-tile', onclick: () => onOpen(stack), title: `Edit ${stack.name}` },
                el('img', { src: stack.thumb_url, alt: stack.name, loading: 'lazy' })),
            el('div', { className: 'ws-actions' }, [
                el('b', {}, stack.name),
                stack.kind === 'planetary' ? el('span', { className: 'ws-chip' }, 'Lucky imaging') : null,
                stack.edited ? el('span', { className: 'ws-chip' }, 'Edited') : null
            ]),
            el('div', { className: 'ws-small ws-muted' }, `${frames}${exposure} · ${stack.width} × ${stack.height} · ${Seleno.fmt.dateTime(stack.created_at)}`),
            el('div', { className: 'ws-actions' }, [
                el('button', { className: 'ws-btn sm primary', type: 'button', onclick: () => onOpen(stack) }, 'Edit'),
                el('a', { className: 'ws-btn sm', href: `${stack.auto_url}?download=1` }, 'Download JPEG'),
                stack.has_master ? el('a', { className: 'ws-btn sm adv-only', href: `${stack.master_url}?download=1` }, `FITS (${Seleno.fmt.bytes(stack.master_bytes)})`) : null,
                el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: () => onRename(stack) }, 'Rename'),
                el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: () => onDelete(stack) }, 'Delete')
            ])
        ]));
    });
}
