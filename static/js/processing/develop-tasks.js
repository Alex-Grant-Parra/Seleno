// The editor's work, in a worker: hold an image (a stack's preview, or one photo),
// render develop settings on it, and export at full size a strip at a time, reading a
// stack's master FITS from the server with range requests.

import * as develop from './develop.js';
import { decodeStrip, parseFitsHeader, stripPlacement } from './fits.js';
import { loadFrame } from './decode.js';
import { downsamplePlanes } from './image.js';
import { toRgb } from './frame-ops.js';
import { encodeTiff16 } from './tiff.js';

const PREVIEW_SIDE = 1600;

function key(params) {
    return JSON.stringify(params);
}

// The image-dependent numbers, measured once per set of params on the preview
function resolved(s, params) {
    const k = key(params);
    if (s.resolvedKey !== k) {
        s.resolved = develop.resolve(s.preview, params, s.planetary);
        s.resolvedKey = k;
    }
    return s.resolved;
}

async function fetchRange(url, start, end) {
    const r = await fetch(url, { headers: { Range: `bytes=${start}-${end - 1}` }, credentials: 'same-origin' });
    if (!r.ok) throw new Error(`Couldn't read the master (${r.status})`);
    const buf = new Uint8Array(await r.arrayBuffer());
    return r.status === 206 ? buf : buf.subarray(start, end);
}

// Rows [a, b) (of the full image) of every plane: from the master FITS, or the held image
async function fullRows(s, a, b, x0, x1) {
    const w = x1 - x0;
    const crop = (plane, width) => {
        const out = new Float32Array((b - a) * w);
        for (let y = 0; y < b - a; y++) out.set(plane.subarray(y * width + x0, y * width + x1), y * w);
        return out;
    };
    if (s.full) return s.full.planes.map(p => crop(p.subarray(a * s.full.width, b * s.full.width), s.full.width));
    const { width, height, dataOffset } = s.master;
    const planes = [];
    for (let c = 0; c < 3; c++) {
        const { offset, bytes } = stripPlacement(width, height, c, a, b);
        const raw = await fetchRange(s.master.url, dataOffset + offset, dataOffset + offset + bytes);
        planes.push(crop(decodeStrip(raw, width, a, b), width));
    }
    return planes;
}

export const DEVELOP_TASKS = {
    // A stack: its preview (float16 RGB, interleaved), and where its master is
    async devOpen({ id, preview, width, height, planetary, masterUrl, fullWidth, fullHeight }, env) {
        const s = { preview: { planes: preview, width, height }, planetary: !!planetary, fullWidth, fullHeight };
        if (masterUrl) {
            const head = await fetchRange(masterUrl, 0, 2880 * 4);
            const { header, dataOffset } = parseFitsHeader(head);
            s.master = { url: masterUrl, width: header.NAXIS1, height: header.NAXIS2, dataOffset };
            s.fullWidth = header.NAXIS1;
            s.fullHeight = header.NAXIS2;
        } else {
            // No master saved with it: exports come from the preview
            s.full = s.preview;
            s.fullWidth = width;
            s.fullHeight = height;
        }
        env.state.set(id, s);
        return { result: { ok: true, fullWidth: s.fullWidth, fullHeight: s.fullHeight } };
    },

    // One photo, colour and linear (Edit this photo): kept at full size for export
    async devOpenFrame({ id, entry, settings }, env) {
        const frame = await loadFrame(entry, env);
        const rgb = toRgb(frame, { debayer: 'bilinear', colour: 'camera', wb: 'daylight', ...(settings || {}) });
        const small = downsamplePlanes(rgb.planes, rgb.width, rgb.height, PREVIEW_SIDE);
        env.state.set(id, {
            preview: { planes: small.planes, width: small.width, height: small.height },
            full: { planes: rgb.planes, width: rgb.width, height: rgb.height },
            planetary: false, fullWidth: rgb.width, fullHeight: rgb.height
        });
        return { result: { ok: true, fullWidth: rgb.width, fullHeight: rgb.height, preview: small } };
    },

    // The developed preview as RGBA, plus the "before" (just stretched) when asked
    async devRender({ id, params, before, showBackground }, env) {
        const s = env.state.get(id);
        const r = resolved(s, params);
        const scale = s.preview.width / Math.max(s.fullWidth || s.preview.width, 1);
        const out = develop.apply(s.preview, r, scale);
        const rgba = develop.toRGBA(out);
        const result = { rgba, width: out.width, height: out.height, resolved: { ...r, params: undefined } };
        const transfer = [rgba.buffer];
        if (before) {
            const plain = develop.cleanParams({ crop: params.crop, colour: { neutralise: false, white: 'none' }, stretch: params.stretch, background: { enabled: false } }, s.planetary);
            const b = develop.toRGBA(develop.apply(s.preview, develop.resolve(s.preview, plain, s.planetary), scale));
            result.before = b;
            transfer.push(b.buffer);
        }
        if (showBackground && r.background_model) {
            const [y0, y1, x0, x1] = develop.cropBox(s.preview.height, s.preview.width, r.params.crop);
            const bg = develop.toRGBA(develop.backgroundImage(r.background_model, x1 - x0, y1 - y0));
            result.background = bg;
            transfer.push(bg.buffer);
        }
        return { result, transfer };
    },

    // The full-size picture: jpeg, png or tiff (16-bit)
    async devExport({ id, params, format, quality = 0.95 }, env) {
        const s = env.state.get(id);
        const r = resolved(s, params);
        const W = s.fullWidth, H = s.fullHeight;
        const [y0, y1, x0, x1] = develop.cropBox(H, W, r.params.crop);
        const w = x1 - x0, h = y1 - y0;
        const sixteen = format === 'tiff';
        const out = sixteen ? new Uint16Array(w * h * 3) : new Uint8ClampedArray(w * h * 4);
        await develop.applyStrips(
            async (lo, hi) => ({ planes: await fullRows(s, y0 + lo, y0 + hi, x0, x1), width: w, height: hi - lo }),
            h, w, r,
            async (a, b, rows) => {
                const [R, G, B] = rows.planes;
                for (let i = 0; i < R.length; i++) {
                    const o = a * w + i;
                    if (sixteen) {
                        out[o * 3] = R[i] * 65535 + 0.5; out[o * 3 + 1] = G[i] * 65535 + 0.5; out[o * 3 + 2] = B[i] * 65535 + 0.5;
                    } else {
                        out[o * 4] = R[i] * 255 + 0.5; out[o * 4 + 1] = G[i] * 255 + 0.5; out[o * 4 + 2] = B[i] * 255 + 0.5; out[o * 4 + 3] = 255;
                    }
                }
            });
        if (sixteen) return { result: encodeTiff16(out, w, h) };
        const canvas = new OffscreenCanvas(w, h);
        canvas.getContext('2d').putImageData(new ImageData(out, w, h), 0, 0);
        const blob = await canvas.convertToBlob({ type: format === 'png' ? 'image/png' : 'image/jpeg', quality });
        return { result: blob };
    },

    async devClose({ id }, env) {
        env.state.delete(id);
        return { result: { ok: true } };
    }
};

