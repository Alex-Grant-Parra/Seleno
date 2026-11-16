// Temporary storage for a job: aligned float16 frames, calibrated masters, the result.
//
// OpfsStore keeps files in the browser's private storage (OPFS). Workers write through
// sync access handles (fast, and the only way Safari allows); anyone reads through
// getFile().slice(), which takes no lock, so several workers can read one file. A
// worker closes its handles at the end of each task (flush), before others read.
//
// MemoryStore keeps everything in this realm's memory: the fallback without OPFS (and
// what the Node tests use, with the inline pool).

const ROOT_PREFIX = 'seleno-job-';
const STALE_MS = 24 * 3600 * 1000;

function flat(name) {
    return name.replace(/\//g, '__');
}

function asBytes(data) {
    return data instanceof Uint8Array ? data : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
}

export class MemoryStore {
    constructor() {
        this.files = new Map();
        this.kind = 'memory';
    }

    async write(name, offset, data) {
        const bytes = asBytes(data);
        let buf = this.files.get(name);
        const need = offset + bytes.length;
        if (!buf || buf.length < need) {
            const grown = new Uint8Array(Math.max(need, buf ? Math.min(buf.length * 2, need * 2) : need));
            if (buf) grown.set(buf);
            buf = grown;
            this.files.set(name, buf);
            buf.used = Math.max(need, (buf.used || 0));
        }
        buf.set(bytes, offset);
        buf.used = Math.max(buf.used || 0, need);
    }

    async read(name, offset, length) {
        const buf = this.files.get(name);
        if (!buf) throw new Error(`${name} isn't stored`);
        return buf.slice(offset, offset + length);
    }

    async size(name) {
        const buf = this.files.get(name);
        return buf ? buf.used || buf.length : 0;
    }

    async file(name, type = 'application/octet-stream') {
        const buf = this.files.get(name);
        return new Blob([buf.subarray(0, buf.used || buf.length)], { type });
    }

    async remove(name) {
        this.files.delete(name);
    }

    async removePrefix(prefix) {
        [...this.files.keys()].filter(k => k.startsWith(prefix)).forEach(k => this.files.delete(k));
    }

    async flush() {}

    async destroy() {
        this.files.clear();
    }
}

export class OpfsStore {
    constructor(dir, id) {
        this.dir = dir;
        this.id = id;
        this.handles = new Map();      // open sync access handles (workers only)
        this.kind = 'opfs';
    }

    static async open(id) {
        const root = await navigator.storage.getDirectory();
        const dir = await root.getDirectoryHandle(`${ROOT_PREFIX}${id}`, { create: true });
        return new OpfsStore(dir, id);
    }

    static async supported() {
        try {
            if (!navigator.storage || !navigator.storage.getDirectory) return false;
            const root = await navigator.storage.getDirectory();
            return typeof root.getDirectoryHandle === 'function'
                && typeof FileSystemFileHandle !== 'undefined'
                && 'createSyncAccessHandle' in FileSystemFileHandle.prototype;
        } catch (error) {
            return false;
        }
    }

    // Remove job folders a closed or crashed tab left behind
    static async cleanStale(activeIds = []) {
        try {
            const root = await navigator.storage.getDirectory();
            for await (const [name, handle] of root.entries()) {
                if (!name.startsWith(ROOT_PREFIX) || handle.kind !== 'directory') continue;
                const id = name.slice(ROOT_PREFIX.length);
                const started = Number(id.split('-')[0]) || 0;
                if (!activeIds.includes(id) && Date.now() - started > STALE_MS) {
                    await root.removeEntry(name, { recursive: true }).catch(() => {});
                }
            }
        } catch (error) { /* no OPFS */ }
    }

    async handle(name) {
        let h = this.handles.get(name);
        if (!h) {
            const fh = await this.dir.getFileHandle(flat(name), { create: true });
            h = await fh.createSyncAccessHandle();
            this.handles.set(name, h);
        }
        return h;
    }

    async write(name, offset, data) {
        const h = await this.handle(name);
        const bytes = asBytes(data);
        let done = 0;
        while (done < bytes.length) done += h.write(bytes.subarray(done), { at: offset + done });
    }

    async read(name, offset, length) {
        const open = this.handles.get(name);
        if (open) {
            const out = new Uint8Array(length);
            open.read(out, { at: offset });
            return out;
        }
        const fh = await this.dir.getFileHandle(flat(name));
        const file = await fh.getFile();
        return new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
    }

    async size(name) {
        try {
            const fh = await this.dir.getFileHandle(flat(name));
            return (await fh.getFile()).size;
        } catch (error) {
            return 0;
        }
    }

    // A disk-backed File (for uploading without reading it into memory)
    async file(name) {
        await this.flush(name);
        const fh = await this.dir.getFileHandle(flat(name));
        return fh.getFile();
    }

    async remove(name) {
        await this.flush(name);
        await this.dir.removeEntry(flat(name)).catch(() => {});
    }

    async removePrefix(prefix) {
        const p = flat(prefix);
        const names = [];
        for await (const name of this.dir.keys()) if (name.startsWith(p)) names.push(name);
        for (const name of names) {
            const h = this.handles.get(name.replace(/__/g, '/'));
            if (h) { h.close(); this.handles.delete(name.replace(/__/g, '/')); }
            await this.dir.removeEntry(name).catch(() => {});
        }
    }

    async flush(name = null) {
        for (const [key, h] of [...this.handles.entries()]) {
            if (name && key !== name) continue;
            h.flush();
            h.close();
            this.handles.delete(key);
        }
    }

    async destroy() {
        await this.flush();
        const root = await navigator.storage.getDirectory();
        await root.removeEntry(`${ROOT_PREFIX}${this.id}`, { recursive: true }).catch(() => {});
    }
}

export async function storageEstimate() {
    try {
        const e = await navigator.storage.estimate();
        return { quota: e.quota || 0, usage: e.usage || 0, free: Math.max(0, (e.quota || 0) - (e.usage || 0)) };
    } catch (error) {
        return { quota: 0, usage: 0, free: 0 };
    }
}

// Typed views of stored float16 and float32 data
export async function readHalf(store, name, offset, count) {
    const bytes = await store.read(name, offset * 2, count * 2);
    return new Uint16Array(bytes.buffer, bytes.byteOffset, count);
}

export async function readFloat(store, name, offset, count) {
    const bytes = await store.read(name, offset * 4, count * 4);
    return new Float32Array(bytes.buffer, bytes.byteOffset, count);
}
