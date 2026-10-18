// Work running in this browser tab (measuring, stacking, saving results), for the
// Activity tab and its counter. Jobs live only as long as the page.

const jobs = [];
const listeners = new Set();
let nextId = 1;

function changed() {
    listeners.forEach(fn => {
        try { fn(jobs); } catch (error) { console.error(error); }
    });
}

export function onJobsChange(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
}

export function runningCount() {
    return jobs.filter(j => j.state === 'running').length;
}

// A job: update(fraction, message), finish(message), fail(error), and cancel() when it
// was started with an AbortController
export function startJob(title, { kind = 'job', controller = null } = {}) {
    const job = {
        id: nextId++,
        kind,
        title,
        state: 'running',
        fraction: 0,
        message: 'Starting…',
        started: Date.now(),
        ended: null,
        controller,
        update(fraction, message) {
            if (job.state !== 'running') return;
            if (fraction !== null && fraction !== undefined) job.fraction = Math.max(0, Math.min(1, fraction));
            if (message) job.message = message;
            changed();
        },
        finish(message) {
            job.state = 'done';
            job.fraction = 1;
            job.message = message || 'Done';
            job.ended = Date.now();
            changed();
        },
        fail(error) {
            job.state = error && error.name === 'AbortError' ? 'cancelled' : 'failed';
            job.message = job.state === 'cancelled' ? 'Cancelled' : (error && error.message) || String(error);
            job.ended = Date.now();
            changed();
        },
        cancel() {
            if (job.controller) job.controller.abort();
        }
    };
    jobs.unshift(job);
    changed();
    return job;
}

export function renderJobs(runningBox, historyBox) {
    const el = Seleno.el;
    const running = jobs.filter(j => j.state === 'running');
    const finished = jobs.filter(j => j.state !== 'running');
    runningBox.innerHTML = '';
    if (!running.length) runningBox.appendChild(el('div', { className: 'ws-empty' }, 'Nothing running.'));
    running.forEach(job => {
        runningBox.appendChild(el('div', { style: { padding: '8px 0', borderTop: '1px solid var(--line)' } }, [
            el('div', { className: 'ws-actions' }, [
                el('b', {}, job.title),
                el('span', { className: 'ws-small ws-muted' }, `${Math.round(job.fraction * 100)}% · ${Seleno.fmt.duration((Date.now() - job.started) / 1000)}`),
                el('span', { className: 'ws-spacer' }),
                job.controller ? el('button', { className: 'ws-btn sm', type: 'button', onclick: () => job.cancel() }, 'Cancel') : null
            ]),
            el('div', { className: 'ws-progress' }, el('div', { style: { width: `${Math.round(job.fraction * 100)}%` } })),
            el('div', { className: 'ws-small ws-muted' }, job.message)
        ]));
    });
    historyBox.innerHTML = '';
    if (!finished.length) historyBox.appendChild(el('div', { className: 'ws-empty' }, 'Nothing yet this visit.'));
    finished.forEach(job => {
        historyBox.appendChild(el('div', { className: 'ws-row', style: { borderTop: '1px solid var(--line)', padding: '6px 0' } }, [
            el('span', {}, [el('span', { className: `ws-dot ${job.state === 'done' ? 'ok' : job.state === 'cancelled' ? 'off' : 'bad'}`, style: { marginRight: '8px' } }), job.title]),
            el('span', { className: 'ws-small' }, `${job.message} · ${Seleno.fmt.time(job.ended)}`)
        ]));
    });
}
