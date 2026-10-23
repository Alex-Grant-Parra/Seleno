// Running tasks in parallel: a pool of Web Workers sized from the machine (cores and
// memory), or the inline pool that runs the same tasks in this thread (Node tests, and
// browsers without module workers). Tasks can be pinned to one worker, for state that
// lives there (a streaming shard, the writer of an OPFS file).

import { newEnv, runTask } from './tasks.js';

// Messages in the page's language when its translations are loaded (window._),
// otherwise English: in Node tests and inside workers
const _ = (text, vars) => {
    if (typeof globalThis._ === 'function') return globalThis._(text, vars);
    return vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text;
};


const GB = 1024 ** 3;

// How much this machine can do: workers and the memory budget for data held in this tab
export function machine(nav = typeof navigator !== 'undefined' ? navigator : {}) {
    const cores = Math.max(1, nav.hardwareConcurrency || 2);
    const memoryGb = nav.deviceMemory || 4;          // browsers report at most 8
    const weak = memoryGb < 8 || /Android|iPhone|iPad|Mobile/i.test(nav.userAgent || '');
    return { cores, memoryGb, weak, budget: Math.max(0.5, memoryGb * 0.35) * GB };
}

// Workers for frames of `pixels` (output size): about 500 MB each at full size
export function workersFor(m, pixels, { raw = true } = {}) {
    const perWorker = Math.max(120, Math.min(520, (pixels * (raw ? 22 : 16)) / 1024 ** 2));
    const byMemory = Math.floor((m.memoryGb * 1024 * 0.5) / perWorker);
    return Math.max(1, Math.min(m.cores - 1 || 1, byMemory, 8));
}

class TaskError extends Error {
    constructor(info) {
        super(info.message);
        this.name = info.name && info.name !== 'Error' ? info.name : 'TaskError';
    }
}

export class WorkerPool {
    constructor(size, url = new URL('./worker.js', import.meta.url)) {
        this.size = size;
        this.workers = [];
        this.queue = [];
        this.pinned = Array.from({ length: size }, () => []);
        this.busy = new Array(size).fill(false);
        this.pending = new Map();
        this.nextId = 1;
        this.closed = false;
        for (let i = 0; i < size; i++) {
            const w = new Worker(url, { type: 'module', name: `seleno-processing-${i}` });
            w.onmessage = (e) => this.done(i, e.data);
            w.onerror = (e) => this.crashed(i, e);
            this.workers.push(w);
        }
    }

    run(name, args = {}, { transfer = [], worker = null, signal = null } = {}) {
        if (this.closed) return Promise.reject(new Error(_('The processing pool is closed')));
        return new Promise((resolve, reject) => {
            const job = { id: this.nextId++, name, args, transfer, resolve, reject, signal };
            if (signal) {
                if (signal.aborted) return reject(new DOMException('Cancelled', 'AbortError'));
                signal.addEventListener('abort', () => {
                    const q = worker === null ? this.queue : this.pinned[worker];
                    const i = q.indexOf(job);
                    if (i >= 0) {
                        q.splice(i, 1);
                        reject(new DOMException('Cancelled', 'AbortError'));
                    }
                }, { once: true });
            }
            if (worker === null) this.queue.push(job);
            else this.pinned[worker % this.size].push(job);
            this.pump();
        });
    }

    broadcast(name, args = {}) {
        return Promise.all(this.workers.map((_unused, i) => this.run(name, args, { worker: i })));
    }

    pump() {
        for (let i = 0; i < this.size; i++) {
            if (this.busy[i]) continue;
            const job = this.pinned[i].shift() || this.queue.shift();
            if (!job) continue;
            this.busy[i] = true;
            this.pending.set(job.id, { job, worker: i });
            this.workers[i].postMessage({ id: job.id, name: job.name, args: job.args }, job.transfer);
        }
    }

    done(i, msg) {
        const entry = this.pending.get(msg.id);
        this.busy[i] = false;
        if (entry) {
            this.pending.delete(msg.id);
            if (msg.error) entry.job.reject(new TaskError(msg.error));
            else entry.job.resolve(msg.result);
        }
        this.pump();
    }

    crashed(i, event) {
        // A worker that dies (often out of memory) fails its task; the pool carries on
        for (const [id, entry] of this.pending) {
            if (entry.worker !== i) continue;
            this.pending.delete(id);
            entry.job.reject(new Error(_('A processing worker stopped ({message}). Try half-size (super-pixel) or fewer frames.', { message: event.message || _('probably out of memory') })));
        }
        this.busy[i] = false;
        event.preventDefault && event.preventDefault();
        this.pump();
    }

    close() {
        this.closed = true;
        this.workers.forEach(w => w.terminate());
        const err = new DOMException('Cancelled', 'AbortError');
        this.queue.forEach(j => j.reject(err));
        this.pinned.forEach(q => q.forEach(j => j.reject(err)));
        this.pending.forEach(({ job }) => job.reject(err));
        this.queue = [];
        this.pending.clear();
    }
}

// The same tasks, in this thread, one at a time (shared env, so pinned state works)
export class InlinePool {
    constructor(env = newEnv(), size = 1) {
        this.env = env;
        this.size = size;
        this.chain = Promise.resolve();
    }

    run(name, args = {}, { signal = null } = {}) {
        const next = this.chain.then(async () => {
            if (signal && signal.aborted) throw new DOMException('Cancelled', 'AbortError');
            // Structured-clone semantics: tasks may keep or change what they're given
            const { result } = await runTask(name, args, this.env);
            return result;
        });
        this.chain = next.catch(() => {});
        return next;
    }

    broadcast(name, args = {}) {
        return this.run(name, args).then(r => [r]);
    }

    close() {}
}
