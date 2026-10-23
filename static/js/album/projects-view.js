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
        Seleno.toast(_("None of this project's frames are in the album any more, so it can't be re-stacked."), 'warn', 7000);
        return;
    }
    const resuming = project.rebuilding && project.rebuilding.done && project.rebuilding.done.length;
    const v = { name: project.name, frames: project.frames_total, missing };
    const question = resuming
        ? (missing ? _('Carry on re-stacking {name} from scratch? {frames} frames ({missing} are no longer in the album and will be left out).', v)
            : _('Carry on re-stacking {name} from scratch? {frames} frames.', v))
        : (missing ? _('Re-stack {name} from scratch? {frames} frames ({missing} are no longer in the album and will be left out).', v)
            : _('Re-stack {name} from scratch? {frames} frames.', v));
    const message = question + ' ' +
        _("It runs in this tab a night at a time and saves a checkpoint after each, so it can carry on later if the tab closes. The project's current totals stay in use until it finishes.");
    if (!confirm(message)) return;
    const controller = new AbortController();
    const job = startJob(_('Re-stacking {name}', { name: project.name }), { kind: 'stack', controller });
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
        box.textContent = _('Couldn\'t load the projects: {message}', { message: error.message });
        return;
    }
    const refresh = () => renderProjects(box, { library, onOpenStack, onSaved });
    box.innerHTML = '';
    if (!projects.length) {
        box.appendChild(el('div', { className: 'ws-empty' }, _('No projects yet. In the stack dialog (Advanced), choose "Add to a project".')));
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
                    _('{n} frames', { n: p.frames_total }),
                    p.exposure_total_s ? _('{duration} of light', { duration: Seleno.fmt.duration(p.exposure_total_s) }) : null,
                    `${nights.length} ${nights.length === 1 ? 'night' : 'nights'}`,
                    `${p.width} × ${p.height}`,
                    p.bytes_total ? _('{size} on the server', { size: Seleno.fmt.bytes(p.bytes_total) }) : null
                ].filter(Boolean).join(' · ')),
                rebuilding ? el('div', { className: 'ws-small' }, _('Re-stacking stopped at {done} of {total} frames: it can carry on.', { done: rebuilding.done.length, total: rebuilding.total })) : null
            ]),
            el('div', { className: 'ws-actions' }, [
                p.stack_id ? el('button', { className: 'ws-btn sm primary', type: 'button', onclick: () => onOpenStack(p.stack_id) }, _('Open the result')) : null,
                el('button', { className: 'ws-btn sm', type: 'button', onclick: () => rebuild(p, library, onSaved, refresh) }, rebuilding ? _('Carry on re-stacking') : _('Re-stack from scratch')),
                rebuilding ? el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: async () => { await cancelRebuild(p.id); refresh(); } }, _('Abandon re-stack')) : null,
                el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: async () => {
                    if (!confirm(_('Delete the project "{name}" and its totals? Its photos and saved stacks stay.', { name: p.name }))) return;
                    await deleteProject(p.id);
                    refresh();
                } }, _('Delete'))
            ])
        ]));
    });
}
