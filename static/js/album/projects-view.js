// The Projects card under Stacked images: long integrations, their nights, and
// re-stacking one from scratch (a rare job, checkpointed so it can resume).

import { engine } from './engine.js';
import { startJob } from './jobs.js';
import * as Q from './quality.js';
import { calibrationFor, runAndSave } from './stack-dialog.js';
import { cancelRebuild, deleteProject, getProject, listProjects } from '../processing/projects.js';

const el = (...args) => Seleno.el(...args);

// A project's frames as batches of one night each (with that night's calibration)
function batchesFor(project, library) {
    const byName = new Map(library.map(f => [f.name, f]));
    const frames = (project.frames || []).map(n => byName.get(n)).filter(Boolean);
    const nights = new Map();
    frames.forEach(f => {
        const night = Q.nightKey(f);
        if (!nights.has(night)) nights.set(night, []);
        nights.get(night).push(f);
    });
    const batches = [];
    [...nights.entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0]))).forEach(([night, list]) => {
        for (let i = 0; i < list.length; i += 60) {
            const lights = list.slice(i, i + 60).map(f => ({ ...f, flag: null }));
            batches.push({ name: night, frames: calibrationFor(lights, library, project.source) });
        }
    });
    return { batches, missing: (project.frames || []).length - frames.length };
}

async function rebuild(summary, library, onSaved, refresh) {
    if (!engine.available) {
        Seleno.toast(engine.reason, 'warn', 6500);
        return;
    }
    const project = await getProject(summary.id);
    const { batches, missing } = batchesFor(project, library);
    if (!batches.length) {
        Seleno.toast("None of this project's frames are in the album any more, so it can't be re-stacked.", 'warn', 7000);
        return;
    }
    const resuming = project.rebuilding && project.rebuilding.done && project.rebuilding.done.length;
    const message = `${resuming ? 'Carry on re-stacking' : 'Re-stack'} ${project.name} from scratch? ${project.frames_total} frames${missing ? ` (${missing} are no longer in the album and will be left out)` : ''}. ` +
        "It runs in this tab a night at a time and saves a checkpoint after each, so it can carry on later if the tab closes. The project's current totals stay in use until it finishes.";
    if (!confirm(message)) return;
    const controller = new AbortController();
    const job = startJob(`Re-stacking ${project.name}`, { kind: 'stack', controller });
    await runAndSave(null, job, controller, onSaved, () => engine.rebuildProject(project.id, batches, {
        signal: controller.signal, onProgress: ({ fraction, message: m }) => job.update(fraction, m)
    }));
    refresh();
}

export async function renderProjects(box, { library, onOpenStack, onSaved }) {
    let projects;
    try {
        projects = await listProjects();
    } catch (error) {
        box.textContent = `Couldn't load the projects: ${error.message}`;
        return;
    }
    const refresh = () => renderProjects(box, { library, onOpenStack, onSaved });
    box.innerHTML = '';
    if (!projects.length) {
        box.appendChild(el('div', { className: 'ws-empty' }, 'No projects yet. In the stack dialog (Advanced), choose "Add to a project".'));
        return;
    }
    projects.forEach(p => {
        const nights = p.nights || [];
        const rebuilding = p.rebuilding && p.rebuilding.done ? p.rebuilding : null;
        box.appendChild(el('div', { className: 'ws-row', style: { borderTop: '1px solid var(--line)', padding: '8px 0', alignItems: 'flex-start', gap: '10px', flexWrap: 'wrap' } }, [
            el('div', { style: { flex: '1', minWidth: '200px' } }, [
                el('b', {}, p.name),
                p.target ? el('span', { className: 'ws-muted' }, ` · ${p.target}`) : null,
                el('div', { className: 'ws-small ws-muted' }, [
                    `${p.frames_total} frames`,
                    p.exposure_total_s ? `${Seleno.fmt.duration(p.exposure_total_s)} of light` : null,
                    `${nights.length} ${nights.length === 1 ? 'night' : 'nights'}`,
                    `${p.width} × ${p.height}`,
                    p.bytes_total ? `${Seleno.fmt.bytes(p.bytes_total)} on the server` : null
                ].filter(Boolean).join(' · ')),
                rebuilding ? el('div', { className: 'ws-small' }, `Re-stacking stopped at ${rebuilding.done.length} of ${rebuilding.total} frames: it can carry on.`) : null
            ]),
            el('div', { className: 'ws-actions' }, [
                p.stack_id ? el('button', { className: 'ws-btn sm primary', type: 'button', onclick: () => onOpenStack(p.stack_id) }, 'Open the result') : null,
                el('button', { className: 'ws-btn sm', type: 'button', onclick: () => rebuild(p, library, onSaved, refresh) }, rebuilding ? 'Carry on re-stacking' : 'Re-stack from scratch'),
                rebuilding ? el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: async () => { await cancelRebuild(p.id); refresh(); } }, 'Abandon re-stack') : null,
                el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: async () => {
                    if (!confirm(`Delete the project "${p.name}" and its totals? Its photos and saved stacks stay.`)) return;
                    await deleteProject(p.id);
                    refresh();
                } }, 'Delete')
            ])
        ]));
    });
}
