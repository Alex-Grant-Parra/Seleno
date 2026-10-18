// Downloading frames, with RAWs kept in the browser's Cache Storage between re-stacks:
// a 21 MP CR2 is about 25 MB, so bandwidth is usually the real limit, and stacking the
// same night again with other settings shouldn't download it all again.

const CACHE_NAME = 'seleno-frames-v1';
const DEFAULT_LIMIT = 8 * 1024 ** 3;

function cacheAvailable() {
    return typeof caches !== 'undefined' && typeof caches.open === 'function';
}

// Bytes of a URL, from the cache when there. onBytes(n) reports downloaded bytes.
export async function fetchBytes(url, { cache = true, signal = null, onBytes = null } = {}) {
    let store = null;
    if (cache && cacheAvailable()) {
        try {
            store = await caches.open(CACHE_NAME);
            const hit = await store.match(url);
            if (hit) {
                const buf = await hit.arrayBuffer();
                // Refresh its place in the eviction order
                store.put(url, new Response(buf, { headers: { 'x-size': String(buf.byteLength), 'x-time': String(Date.now()) } })).catch(() => {});
                return { buffer: buf, cached: true };
            }
        } catch (error) {
            store = null;
        }
    }
    const response = await fetch(url, { credentials: 'same-origin', signal });
    if (!response.ok) throw new Error(`Couldn't download ${decodeURIComponent(url.split('/').pop().split('?')[0])} (${response.status})`);
    let buffer;
    if (onBytes && response.body && response.body.getReader) {
        const reader = response.body.getReader();
        const parts = [];
        let total = 0;
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            parts.push(value);
            total += value.length;
            onBytes(value.length);
        }
        const all = new Uint8Array(total);
        let o = 0;
        parts.forEach(p => { all.set(p, o); o += p.length; });
        buffer = all.buffer;
    } else {
        buffer = await response.arrayBuffer();
        if (onBytes) onBytes(buffer.byteLength);
    }
    if (store) {
        store.put(url, new Response(buffer.slice(0), { headers: { 'x-size': String(buffer.byteLength), 'x-time': String(Date.now()) } })).catch(() => {});
    }
    return { buffer, cached: false };
}

// Keep the cache under `limit` bytes, oldest first out. Returns {bytes, files}.
export async function pruneCache(limit = DEFAULT_LIMIT) {
    if (!cacheAvailable()) return { bytes: 0, files: 0 };
    const store = await caches.open(CACHE_NAME);
    const entries = [];
    for (const request of await store.keys()) {
        const r = await store.match(request);
        entries.push({ request, size: Number(r.headers.get('x-size')) || 0, time: Number(r.headers.get('x-time')) || 0 });
    }
    entries.sort((a, b) => b.time - a.time);
    let total = 0;
    for (const e of entries) {
        total += e.size;
        if (total > limit) await store.delete(e.request);
    }
    return { bytes: Math.min(total, limit), files: entries.length };
}

export async function clearCache() {
    if (cacheAvailable()) await caches.delete(CACHE_NAME);
}
