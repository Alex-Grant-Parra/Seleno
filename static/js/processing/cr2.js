// A Canon CR2 reader: the sensor's Bayer mosaic, black level, white level, colour matrix
// and white balance, the way LibRaw (and dcraw before it) reads them.
//
// A CR2 is a TIFF. Its fourth IFD (offset at byte 12, after the "CR" magic) holds the
// sensor data as lossless JPEG (SOF3, predictor 1), cut into vertical slices (tag
// 0xC640: [count, width, last width]) that run top to bottom one after another. The
// maker note says where the visible area is (0x00E0) and the as-shot white balance
// (0x4001, ColorData). Black is the integer mean of the masked columns left of the
// visible area, per Bayer position; the white level and matrix come from LibRaw's
// table (canon-colordata.js). Other cameras and sRAW files throw, and the caller falls
// back to the JPEG.

import { CANON_EOS, CANON_SIZES } from './canon-colordata.js';
import { camToSrgbMatrix, daylightWb, normalisedWb } from './colour.js';

export class RawError extends Error {
    constructor(message) {
        super(message);
        this.name = 'RawError';
    }
}

const TYPE_SIZE = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8 };

class Tiff {
    constructor(buffer) {
        this.bytes = new Uint8Array(buffer);
        this.dv = new DataView(buffer);
        const order = this.dv.getUint16(0, false);
        if (order === 0x4949) this.le = true;
        else if (order === 0x4d4d) this.le = false;
        else throw new RawError('Not a TIFF-based RAW file');
        if (this.u16(2) !== 42) throw new RawError('Not a TIFF-based RAW file');
    }

    u16(o) { return this.dv.getUint16(o, this.le); }
    u32(o) { return this.dv.getUint32(o, this.le); }

    // {tag: {type, count, offset}} and the next IFD's offset
    ifd(offset) {
        const n = this.u16(offset);
        const tags = new Map();
        for (let i = 0; i < n; i++) {
            const e = offset + 2 + i * 12;
            const type = this.u16(e + 2);
            const count = this.u32(e + 4);
            const size = (TYPE_SIZE[type] || 1) * count;
            tags.set(this.u16(e), { type, count, offset: size <= 4 ? e + 8 : this.u32(e + 8) });
        }
        return { tags, next: this.u32(offset + 2 + n * 12) };
    }

    values(entry) {
        if (!entry) return [];
        const out = [];
        for (let i = 0; i < entry.count; i++) {
            if (entry.type === 3 || entry.type === 8) out.push(this.u16(entry.offset + 2 * i));
            else if (entry.type === 4 || entry.type === 9) out.push(this.u32(entry.offset + 4 * i));
            else out.push(this.bytes[entry.offset + i]);
        }
        return out;
    }

    value(entry) {
        return entry ? this.values({ ...entry, count: 1 })[0] : undefined;
    }

    text(entry) {
        if (!entry) return '';
        let s = '';
        for (let i = 0; i < entry.count; i++) {
            const c = this.bytes[entry.offset + i];
            if (!c) break;
            s += String.fromCharCode(c);
        }
        return s.trim();
    }
}

// --- lossless JPEG ----------------------------------------------------------------

function buildHuffman(counts, symbols) {
    // A 16-bit lookup: entry = (code length << 8) | symbol, 0 for no code
    const table = new Uint16Array(65536);
    let code = 0;
    let k = 0;
    for (let len = 1; len <= 16; len++) {
        for (let i = 0; i < counts[len - 1]; i++) {
            const span = 1 << (16 - len);
            const start = code << (16 - len);
            table.fill((len << 8) | symbols[k++], start, start + span);
            code++;
        }
        code <<= 1;
    }
    return table;
}

// The header of the lossless JPEG at [start, end): tables, frame, scan and where the
// entropy-coded data begins
function ljpegHeader(bytes, start, end) {
    if (bytes[start] !== 0xff || bytes[start + 1] !== 0xd8) throw new RawError('The sensor data is not a JPEG stream');
    const huff = [];
    let pos = start + 2;
    let frame = null;
    let restart = 0;
    while (pos < end) {
        if (bytes[pos] !== 0xff) throw new RawError('Bad JPEG marker in the sensor data');
        const marker = bytes[pos + 1];
        const len = (bytes[pos + 2] << 8) | bytes[pos + 3];
        const seg = pos + 4;
        if (marker === 0xc4) {
            let p = seg;
            while (p < pos + 2 + len) {
                const id = bytes[p] & 15;
                const counts = bytes.subarray(p + 1, p + 17);
                const total = counts.reduce((a, b) => a + b, 0);
                huff[id] = buildHuffman(counts, bytes.subarray(p + 17, p + 17 + total));
                p += 17 + total;
            }
        } else if (marker === 0xc3) {
            const comps = bytes[seg + 5];
            frame = {
                bits: bytes[seg],
                high: (bytes[seg + 1] << 8) | bytes[seg + 2],
                wide: (bytes[seg + 3] << 8) | bytes[seg + 4],
                clrs: comps,
                ids: Array.from({ length: comps }, (_, i) => bytes[seg + 6 + i * 3])
            };
        } else if (marker === 0xc0 || marker === 0xc1 || marker === 0xc2) {
            throw new RawError('This RAW uses lossy compression (sRAW or mRAW), which is not supported');
        } else if (marker === 0xdd) {
            restart = (bytes[seg] << 8) | bytes[seg + 1];
        } else if (marker === 0xda) {
            if (!frame) throw new RawError('The sensor data has no frame header');
            const ns = bytes[seg];
            const tables = [];
            for (let i = 0; i < ns; i++) tables.push(huff[bytes[seg + 2 + i * 2] >> 4]);
            const psv = bytes[seg + 1 + ns * 2];
            if (tables.some(t => !t)) throw new RawError('The sensor data is missing a Huffman table');
            return { ...frame, tables, psv, restart, data: pos + 2 + len };
        }
        pos += 2 + len;
    }
    throw new RawError('The sensor data has no scan');
}

// Decode the lossless JPEG, handing each sample to the slice layout. Predictor 1 (the
// sample to the left; the first column from the first column of the row above).
function decodeLjpeg(bytes, start, end, put) {
    const h = ljpegHeader(bytes, start, end);
    if (h.psv !== 1) throw new RawError(`Lossless JPEG predictor ${h.psv} is not supported`);
    const { wide, high, clrs, tables, bits } = h;
    const restartRows = h.restart ? Math.max(1, Math.round(h.restart / wide)) : Infinity;
    let pos = h.data;
    let buf = 0;
    let cnt = 0;
    const vpred = new Int32Array(clrs);
    const last = new Int32Array(clrs);
    const max = (1 << bits) - 1;

    for (let jrow = 0; jrow < high; jrow++) {
        if (jrow % restartRows === 0) {
            vpred.fill(1 << (bits - 1));
            if (jrow) {
                // Skip to the restart marker
                while (pos < end - 1 && !(bytes[pos] === 0xff && bytes[pos + 1] >= 0xd0 && bytes[pos + 1] <= 0xd7)) pos++;
                pos += 2;
            }
            buf = 0;
            cnt = 0;
        }
        for (let col = 0; col < wide; col++) {
            for (let c = 0; c < clrs; c++) {
                // Refill to at least 25 bits; 0xFF00 is a stuffed 0xFF, any other marker ends the data
                while (cnt <= 24) {
                    let byte = 0;
                    if (pos < end) {
                        byte = bytes[pos];
                        if (byte === 0xff) {
                            if (bytes[pos + 1] === 0) pos += 2;
                            else byte = 0;
                        } else pos++;
                    }
                    buf = (buf << 8) | byte;
                    cnt += 8;
                }
                const entry = tables[c][(buf >>> (cnt - 16)) & 0xffff];
                const len = entry >> 8;
                if (!len) throw new RawError('Corrupt sensor data (bad Huffman code)');
                cnt -= len;
                const s = entry & 0xff;
                let diff;
                if (s === 0) diff = 0;
                else if (s === 16) diff = -32768;
                else {
                    if (cnt < s) {
                        while (cnt <= 24) {
                            let byte = 0;
                            if (pos < end) {
                                byte = bytes[pos];
                                if (byte === 0xff) {
                                    if (bytes[pos + 1] === 0) pos += 2;
                                    else byte = 0;
                                } else pos++;
                            }
                            buf = (buf << 8) | byte;
                            cnt += 8;
                        }
                    }
                    diff = (buf >>> (cnt - s)) & ((1 << s) - 1);
                    cnt -= s;
                    if (diff < (1 << (s - 1))) diff -= (1 << s) - 1;
                }
                let pred;
                if (col) pred = last[c];
                else {
                    pred = vpred[c];
                    vpred[c] += diff;
                }
                const v = pred + diff;
                if (v < 0 || v > max) throw new RawError('Corrupt sensor data (value out of range)');
                last[c] = v;
                put(v);
            }
        }
    }
    return h;
}

// --- the file ---------------------------------------------------------------------

function cameraModel(tiff, ifd0) {
    const make = tiff.text(ifd0.tags.get(0x10f));
    let model = tiff.text(ifd0.tags.get(0x110));
    if (make && model.toLowerCase().startsWith(make.split(' ')[0].toLowerCase())) model = model.slice(make.split(' ')[0].length).trim();
    return { make, model };
}

export function canonColourData(model) {
    const m = model.toLowerCase();
    for (const [prefix, black, white, coeffs] of CANON_EOS) {
        if (m.startsWith(prefix.toLowerCase())) return { prefix, black, white, coeffs };
    }
    return null;
}

function makerNote(tiff, ifd0) {
    const exifEntry = ifd0.tags.get(0x8769);
    if (!exifEntry) return null;
    const exif = tiff.ifd(tiff.value(exifEntry));
    const exposure = exif.tags.get(0x829a);
    const iso = exif.tags.get(0x8827);
    const note = exif.tags.get(0x927c);
    const info = {
        exposure: exposure ? tiff.u32(exposure.offset) / Math.max(1, tiff.u32(exposure.offset + 4)) : null,
        iso: iso ? tiff.value(iso) : null,
        tags: null
    };
    if (note) info.tags = tiff.ifd(note.offset).tags;       // Canon: offsets from the TIFF start
    return info;
}

// The embedded full-size JPEG (IFD0's strip), as [offset, length], or null
export function cr2PreviewRange(buffer) {
    try {
        const tiff = new Tiff(buffer);
        const ifd0 = tiff.ifd(tiff.u32(4));
        const off = tiff.value(ifd0.tags.get(0x111));
        const len = tiff.value(ifd0.tags.get(0x117));
        if (off && len && tiff.bytes[off] === 0xff && tiff.bytes[off + 1] === 0xd8) return [off, len];
    } catch (error) { /* not a TIFF */ }
    return null;
}

// The raw sensor data and everything needed to scale and colour it. Throws RawError for
// anything other than a supported Canon CR2.
export function decodeCR2(buffer) {
    const tiff = new Tiff(buffer);
    const bytes = tiff.bytes;
    if (bytes[8] !== 0x43 || bytes[9] !== 0x52) throw new RawError('Not a Canon CR2 file');
    const ifd0 = tiff.ifd(tiff.u32(4));
    const { make, model } = cameraModel(tiff, ifd0);
    if (!/canon/i.test(make)) throw new RawError('Only Canon CR2 files can be read in the browser');

    const rawIfd = tiff.ifd(tiff.u32(12));
    const start = tiff.value(rawIfd.tags.get(0x111));
    const length = tiff.value(rawIfd.tags.get(0x117));
    if (!start || !length) throw new RawError('No sensor data in this CR2');
    const end = Math.min(bytes.length, start + length);
    const slices = tiff.values(rawIfd.tags.get(0xc640));

    const h = ljpegHeader(bytes, start, end);
    const rawWidth = h.wide * h.clrs;
    const rawHeight = h.high;
    const raw = new Uint16Array(rawWidth * rawHeight);

    if (slices.length >= 3 && slices[0] + 0 >= 0 && slices[1] > 0) {
        const [count, width, lastWidth] = slices;
        if (count * width + lastWidth !== rawWidth) throw new RawError('Unexpected CR2 slice layout');
        let slice = 0, row = 0, col = 0, x0 = 0, sw = count ? width : lastWidth;
        let base = 0;
        decodeLjpeg(bytes, start, end, (v) => {
            raw[base + col] = v;
            if (++col === sw) {
                col = 0;
                base += rawWidth;
                if (++row === rawHeight) {
                    row = 0;
                    slice++;
                    x0 += sw;
                    sw = slice < count ? width : lastWidth;
                    base = x0;
                }
            }
        });
    } else {
        let i = 0;
        decodeLjpeg(bytes, start, end, (v) => { raw[i++] = v; });
    }

    // Visible area: LibRaw's table of Canon sensor sizes, else the maker note's SensorInfo
    const note = makerNote(tiff, ifd0);
    let top = 0, left = 0, width = rawWidth, height = rawHeight;
    let masks = [0, 0, 0, 0];
    const known = CANON_SIZES.find(r => r[0] === rawWidth && r[1] === rawHeight);
    const sensor = note && note.tags ? tiff.values(note.tags.get(0xe0)) : [];
    if (known) {
        left = known[2];
        top = known[3];
        width = rawWidth - left - known[4];
        height = rawHeight - top - known[5];
        masks = known.slice(6, 10);
    } else if (sensor.length >= 9) {
        left = sensor[5];
        top = sensor[6];
        width = sensor[7] - sensor[5] + 1;
        height = sensor[8] - sensor[6] + 1;
    }
    // Canon's mosaic is RGGB at those margins (dcraw's 0x94949494); the visible area
    // starts on an even row and column, as LibRaw's does
    const rggb = [[0, 1], [1, 2]];
    const shiftY = top & 1, shiftX = left & 1;
    top += shiftY; height -= shiftY;
    left += shiftX; width -= shiftX;
    width -= width & 1;
    height -= height & 1;
    width = Math.min(width, rawWidth - left);
    height = Math.min(height, rawHeight - top);
    const pattern = [0, 1].map(dy => [0, 1].map(dx => rggb[(shiftY + dy) & 1][(shiftX + dx) & 1]));

    // Black: integer mean of the masked columns beside the visible area, per Bayer
    // position (LibRaw's crop_masked_pixels for lossless JPEG Canons)
    const bands = [
        [masks[2] + 2, left - masks[1] - 2],
        [masks[2] + 2 + left + width, rawWidth - masks[3]]
    ].filter(([a, b]) => b > a);
    const black = [[0, 0], [0, 0]];
    let masked = 0;
    for (let dy = 0; dy < 2; dy++) for (let dx = 0; dx < 2; dx++) {
        let sum = 0, n = 0;
        for (const [a, b] of bands) {
            const first = a + ((a - left - dx) & 1);
            for (let y = top + dy; y < top + height; y += 2) {
                const rowBase = y * rawWidth;
                for (let x = first; x < b; x += 2) {
                    sum += raw[rowBase + x];
                    n++;
                }
            }
        }
        black[dy][dx] = n ? Math.floor(sum / n) : 0;
        masked += n;
    }

    const table = canonColourData(model);
    if (!table) throw new RawError(`No colour data for the ${make} ${model}`);
    if (!masked) black.forEach(r => r.fill(table.black || 0));
    const white = table.white || (1 << h.bits) - 1;
    const camXyz = [0, 1, 2].map(i => table.coeffs.slice(i * 3, i * 3 + 3).map(v => v / 10000));

    // As-shot white balance from ColorData (dcraw's offsets by table length, RGGB)
    let asShot = null;
    const colorData = note && note.tags ? note.tags.get(0x4001) : null;
    if (colorData && colorData.count > 250) {
        const len = colorData.count;
        const skip = len === 582 ? 50 : len === 653 ? 68 : len === 5120 ? 142 : 126;
        const o = colorData.offset + skip;
        asShot = normalisedWb([tiff.u16(o), tiff.u16(o + 2), tiff.u16(o + 6)]);
    }
    const daylight = daylightWb(camXyz);

    return {
        make, model, raw, rawWidth, rawHeight, top, left, width, height,
        pattern, black, white, camXyz,
        wb: daylight || asShot || [1, 1, 1],
        wbAsShot: asShot || daylight || [1, 1, 1],
        camToSrgb: camToSrgbMatrix(camXyz),
        exposure: note ? note.exposure : null,
        iso: note ? note.iso : null
    };
}

// The visible mosaic as float32, black taken away and scaled so 1 is the white level
// (frames.read_raw). Frees nothing itself; drop `decoded.raw` afterwards.
export function cr2Mosaic(decoded) {
    const { raw, rawWidth, top, left, width, height, black, white } = decoded;
    const meanBlack = (black[0][0] + black[0][1] + black[1][0] + black[1][1]) / 4;
    const scale = 1 / Math.max(white - meanBlack, 1);
    const data = new Float32Array(width * height);
    for (let y = 0; y < height; y++) {
        const src = (top + y) * rawWidth + left;
        const dst = y * width;
        const b0 = black[y & 1][0], b1 = black[y & 1][1];
        for (let x = 0; x < width; x += 2) {
            data[dst + x] = (raw[src + x] - b0) * scale;
            data[dst + x + 1] = (raw[src + x + 1] - b1) * scale;
        }
    }
    return {
        data, width, height,
        pattern: decoded.pattern,
        wb: decoded.wb,
        wbAsShot: decoded.wbAsShot,
        camToSrgb: decoded.camToSrgb,
        camera: `${decoded.make} ${decoded.model}`
    };
}

export function readCR2(buffer) {
    return cr2Mosaic(decodeCR2(buffer));
}
