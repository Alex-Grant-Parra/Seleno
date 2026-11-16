// SER video files (planetary cameras): a 178-byte header, then raw frames one after
// another. Reads the header and any frame by index from bytes (or ranges of a URL).

const HEADER = 178;
export const SER_COLOURS = { 0: 'mono', 8: 'RGGB', 9: 'GRBG', 10: 'GBRG', 11: 'BGGR', 100: 'RGB', 101: 'BGR' };

export function parseSerHeader(bytes) {
    const text = String.fromCharCode(...bytes.subarray(0, 14));
    if (text !== 'LUCAM-RECORDER') throw new Error("This isn't a SER video");
    const dv = new DataView(bytes.buffer, bytes.byteOffset, HEADER);
    const colour = dv.getInt32(18, true);
    const width = dv.getInt32(26, true);
    const height = dv.getInt32(30, true);
    const depth = dv.getInt32(34, true);
    const frames = dv.getInt32(38, true);
    if (!(colour in SER_COLOURS)) throw new Error(`Unsupported SER colour format ${colour}`);
    const planes = colour >= 100 ? 3 : 1;
    const bytesPerSample = depth > 8 ? 2 : 1;
    return { colour: SER_COLOURS[colour], width, height, depth, frames, planes, bytesPerSample, frameBytes: width * height * planes * bytesPerSample, headerBytes: HEADER };
}

const BAYER = { RGGB: [[0, 1], [1, 2]], GRBG: [[1, 0], [2, 1]], GBRG: [[1, 2], [0, 1]], BGGR: [[2, 1], [1, 0]] };

// One frame's raw bytes as display-referred RGB planes 0..1 (Bayer data is binned 2x2,
// as super-pixel debayering does; 16-bit data is little-endian, as nearly every
// capture program writes it whatever the header says)
export function serFramePlanes(header, raw) {
    const { width: w, height: h, planes, bytesPerSample, depth, colour } = header;
    const max = bytesPerSample === 2 ? (2 ** Math.min(16, Math.max(depth, 9)) - 1) : 255;
    const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
    const at = bytesPerSample === 2 ? (i) => dv.getUint16(i * 2, true) / max : (i) => raw[i] / 255;
    if (planes === 3) {
        const out = [0, 1, 2].map(() => new Float32Array(w * h));
        const order = colour === 'BGR' ? [2, 1, 0] : [0, 1, 2];
        for (let i = 0; i < w * h; i++) for (let c = 0; c < 3; c++) out[order[c]][i] = at(i * 3 + c);
        return { planes: out, width: w, height: h };
    }
    if (colour === 'mono') {
        const p = new Float32Array(w * h);
        for (let i = 0; i < w * h; i++) p[i] = at(i);
        return { planes: [p, p, p], width: w, height: h };
    }
    const pattern = BAYER[colour];
    const w2 = w >> 1, h2 = h >> 1;
    const out = [0, 1, 2].map(() => new Float32Array(w2 * h2));
    const counts = [0, 0, 0];
    for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) counts[pattern[dy][dx]]++;
    for (let y = 0; y < h2; y++) for (let x = 0; x < w2; x++) {
        for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
            const c = pattern[dy][dx];
            out[c][y * w2 + x] += at((2 * y + dy) * w + 2 * x + dx) / counts[c];
        }
    }
    return { planes: out, width: w2, height: h2 };
}

// Frames of a SER at a URL, read with range requests a batch at a time
export async function* serFrames(url, { signal = null, batchBytes = 32 * 1024 ** 2, maxFrames = Infinity } = {}) {
    const get = async (start, end) => {
        const r = await fetch(url, { headers: { Range: `bytes=${start}-${end - 1}` }, credentials: 'same-origin', signal });
        if (!r.ok) throw new Error(`Couldn't read the SER video (${r.status})`);
        const buf = new Uint8Array(await r.arrayBuffer());
        // A server that ignores Range sends everything
        return r.status === 206 ? buf : buf.subarray(start, end);
    };
    const header = parseSerHeader(await get(0, HEADER));
    yield { header };
    const per = header.frameBytes;
    const count = Math.min(header.frames, maxFrames);
    const batch = Math.max(1, Math.floor(batchBytes / per));
    for (let i = 0; i < count; i += batch) {
        const n = Math.min(batch, count - i);
        const bytes = await get(HEADER + i * per, HEADER + (i + n) * per);
        for (let k = 0; k < n; k++) yield { index: i + k, raw: bytes.slice(k * per, (k + 1) * per) };
    }
}
