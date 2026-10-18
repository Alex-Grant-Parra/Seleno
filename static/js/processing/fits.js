// A small FITS writer and reader for 32-bit float images with one or three planes, as
// app/processing/fitsio.py: rows stored bottom-up (ROWORDER = 'BOTTOM-UP'), big-endian.
// The writer works a strip at a time, so a full-size master never sits in memory twice.

const BLOCK = 2880;
const CARD = 80;

function formatValue(value) {
    if (typeof value === 'boolean') return (value ? 'T' : 'F').padStart(20);
    if (typeof value === 'number') {
        if (Number.isInteger(value)) return String(value).padStart(20);
        let text = String(Number(value.toPrecision(10))).toUpperCase();
        if (!/[.E]/.test(text)) text += '.0';
        return text.padStart(20);
    }
    const text = String(value).replace(/'/g, "''").replace(/[^\x20-\x7e]/g, '?').slice(0, 68);
    return `'${text.padEnd(8)}'`;
}

function card(key, value, comment) {
    key = key.toUpperCase().slice(0, 8);
    let text = value === undefined ? key.padEnd(8) : `${key.padEnd(8)}= ${formatValue(value)}`;
    if (value !== undefined && comment) text += ` / ${comment}`;
    return text.slice(0, CARD).padEnd(CARD);
}

// The header bytes for a width x height image of `planes` planes. extra: {key: value or
// [value, comment]}; null values are left out.
export function fitsHeader(width, height, planes, extra = {}) {
    const cards = [
        card('SIMPLE', true, 'conforms to FITS standard'),
        card('BITPIX', -32, '32-bit floating point'),
        card('NAXIS', planes === 1 ? 2 : 3),
        card('NAXIS1', width),
        card('NAXIS2', height)
    ];
    if (planes > 1) cards.push(card('NAXIS3', planes));
    cards.push(card('ROWORDER', 'BOTTOM-UP', 'first row is the bottom of the image'));
    Object.entries(extra).forEach(([key, value]) => {
        let comment;
        if (Array.isArray(value)) [value, comment] = value;
        if (value === null || value === undefined) return;
        cards.push(card(key, value, comment));
    });
    cards.push(card('END'));
    let head = cards.join('');
    head += ' '.repeat((BLOCK - (head.length % BLOCK)) % BLOCK);
    return new TextEncoder().encode(head);
}

export function fitsDataBytes(width, height, planes) {
    const n = width * height * planes * 4;
    return n + ((BLOCK - (n % BLOCK)) % BLOCK);
}

// Where rows [y0, y1) of a plane go in the data (they're stored bottom-up, so it's one
// block in reverse row order): {offset, bytes} relative to the start of the data
export function stripPlacement(width, height, plane, y0, y1) {
    const first = height - y1;
    return { offset: (plane * height + first) * width * 4, bytes: (y1 - y0) * width * 4 };
}

// Rows [y0, y1) of a plane (Float32Array, top row first) as stored bytes
export function encodeStrip(rows, width, y0, y1) {
    const n = y1 - y0;
    const out = new DataView(new ArrayBuffer(n * width * 4));
    for (let r = 0; r < n; r++) {
        const src = (n - 1 - r) * width;
        const dst = r * width * 4;
        for (let x = 0; x < width; x++) out.setFloat32(dst + x * 4, rows[src + x], false);
    }
    return new Uint8Array(out.buffer);
}

// A complete FITS as a Blob from whole planes (small images, tests)
export function fitsBlob(planes, width, height, extra = {}) {
    const parts = [fitsHeader(width, height, planes.length, extra)];
    planes.forEach((p, c) => parts.push(encodeStrip(p, width, 0, height)));
    const pad = fitsDataBytes(width, height, planes.length) - width * height * planes.length * 4;
    if (pad) parts.push(new Uint8Array(pad));
    return new Blob(parts, { type: 'application/fits' });
}

// The header of a FITS file's bytes: {header: {key: value}, dataOffset}
export function parseFitsHeader(bytes) {
    const header = {};
    for (let off = 0; off + BLOCK <= bytes.length; off += BLOCK) {
        for (let i = 0; i < BLOCK; i += CARD) {
            const text = String.fromCharCode(...bytes.subarray(off + i, off + i + CARD));
            const key = text.slice(0, 8).trim();
            if (key === 'END') return { header, dataOffset: off + BLOCK };
            if (text.slice(8, 10) !== '= ') continue;
            const raw = text.slice(10).trim();
            if (raw.startsWith("'")) {
                const m = raw.match(/^'((?:[^']|'')*)'/);
                header[key] = m ? m[1].replace(/''/g, "'").trimEnd() : '';
                continue;
            }
            const v = raw.split('/')[0].trim();
            if (v === 'T' || v === 'F') header[key] = v === 'T';
            else if (v !== '' && !Number.isNaN(Number(v))) header[key] = Number(v);
            else header[key] = v;
        }
    }
    throw new Error('Truncated FITS header');
}

// Rows [y0, y1) of a plane, top row first, from stored bytes (as stripPlacement gives)
export function decodeStrip(bytes, width, y0, y1, bottomUp = true) {
    const n = y1 - y0;
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const out = new Float32Array(n * width);
    for (let r = 0; r < n; r++) {
        const src = (bottomUp ? n - 1 - r : r) * width * 4;
        for (let x = 0; x < width; x++) out[r * width + x] = dv.getFloat32(src + x * 4, false);
    }
    return out;
}

// Builds a FITS file from strips in any order, as Blob parts (which the browser can
// keep on disk), so a big file never has to sit in memory whole
export class FitsAssembler {
    constructor(width, height, planes, header = {}) {
        Object.assign(this, { width, height, planes });
        this.head = fitsHeader(width, height, planes, header);
        this.parts = [];
    }

    put(plane, y0, y1, rows) {
        const { offset } = stripPlacement(this.width, this.height, plane, y0, y1);
        this.parts.push([offset, new Blob([encodeStrip(rows, this.width, y0, y1)])]);
    }

    blob() {
        this.parts.sort((a, b) => a[0] - b[0]);
        const n = this.width * this.height * this.planes * 4;
        const pad = fitsDataBytes(this.width, this.height, this.planes) - n;
        const parts = [this.head, ...this.parts.map(p => p[1])];
        if (pad) parts.push(new Uint8Array(pad));
        this.parts = [];
        return new Blob(parts, { type: 'application/fits' });
    }
}
