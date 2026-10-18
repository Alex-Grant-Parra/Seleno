// Turning a file into a frame the pipeline can use.
//
// A frame is {planes, width, height, pattern, isRaw, wb, wbAsShot, camToSrgb, camera}:
// a RAW gives one plane (the Bayer mosaic, linear, 0 = black, 1 = white level) and its
// pattern; a JPEG gives three planes, linear through the inverse sRGB curve unless
// display-referred values are asked for (lucky imaging). Canon CR2s are read here (see
// cr2.js); other RAWs fall back to the JPEG that came with them.

import { RawError, readCR2 } from './cr2.js';
import { SRGB_TO_LINEAR_LUT } from './colour.js';
import { fetchBytes } from './fetch-cache.js';

export const RAW_EXTENSIONS = ['cr2', 'cr3', 'crw', 'nef', 'nrw', 'arw', 'srf', 'sr2', 'dng', 'orf', 'rw2', 'raf', 'pef', 'srw', 'erf', 'kdc', 'mrw', '3fr'];

export function extensionOf(url) {
    return (String(url || '').split('?')[0].split('.').pop() || '').toLowerCase();
}

// RGBA pixels of a JPEG (or any image the browser decodes), optionally scaled down
export async function decodeImageData(buffer, { type = 'image/jpeg', maxSide = null, scale = 1 } = {}) {
    const blob = new Blob([buffer], { type });
    const options = { imageOrientation: 'none', colorSpaceConversion: 'none', premultiplyAlpha: 'none' };
    let bitmap = await createImageBitmap(blob, options);
    let w = bitmap.width, h = bitmap.height;
    const fullWidth = w, fullHeight = h;
    let s = scale;
    if (maxSide && Math.max(w, h) > maxSide) s = Math.min(s, maxSide / Math.max(w, h));
    if (s < 1) {
        const rw = Math.max(1, Math.round(w * s)), rh = Math.max(1, Math.round(h * s));
        bitmap.close();
        bitmap = await createImageBitmap(blob, { ...options, resizeWidth: rw, resizeHeight: rh, resizeQuality: 'medium' });
        w = rw;
        h = rh;
    }
    const canvas = new OffscreenCanvas(w, h);
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    const data = ctx.getImageData(0, 0, w, h).data;
    canvas.width = canvas.height = 1;
    return { data, width: w, height: h, fullWidth, fullHeight };
}

export function rgbaToPlanes(rgba, width, height, linear = true) {
    const n = width * height;
    const planes = [new Float32Array(n), new Float32Array(n), new Float32Array(n)];
    const [r, g, b] = planes;
    if (linear) {
        const lut = SRGB_TO_LINEAR_LUT;
        for (let i = 0, j = 0; i < n; i++, j += 4) { r[i] = lut[rgba[j]]; g[i] = lut[rgba[j + 1]]; b[i] = lut[rgba[j + 2]]; }
    } else {
        const k = 1 / 255;
        for (let i = 0, j = 0; i < n; i++, j += 4) { r[i] = rgba[j] * k; g[i] = rgba[j + 1] * k; b[i] = rgba[j + 2] * k; }
    }
    return planes;
}

export function rawFrame(buffer) {
    const m = readCR2(buffer);
    return {
        planes: [m.data], width: m.width, height: m.height, pattern: m.pattern, isRaw: true,
        wb: m.wb, wbAsShot: m.wbAsShot, camToSrgb: m.camToSrgb, camera: m.camera
    };
}

export async function jpegFrame(buffer, { linear = true, scale = 1 } = {}) {
    const { data, width, height } = await decodeImageData(buffer, { scale });
    return { planes: rgbaToPlanes(data, width, height, linear), width, height, pattern: null, isRaw: false };
}

// entry: {name, url, kind, jpeg_url}. env.loadFrame overrides everything (tests).
// Returns the frame; frame.fellBack is set when a RAW couldn't be read and its JPEG was.
export async function loadFrame(entry, env = {}, { linear = true, signal = null, onBytes = null } = {}) {
    if (env.loadFrame) return env.loadFrame(entry, { linear });
    const ext = extensionOf(entry.url);
    const isRaw = entry.kind === 'raw' || RAW_EXTENSIONS.includes(ext);
    if (isRaw) {
        if (ext === 'cr2') {
            const { buffer } = await fetchBytes(entry.url, { cache: true, signal, onBytes });
            try {
                const frame = rawFrame(buffer);
                if (!linear) frame.displayReferred = false;
                return frame;
            } catch (error) {
                if (!(error instanceof RawError) || !entry.jpeg_url) throw error;
            }
        } else if (!entry.jpeg_url) {
            throw new RawError(`${entry.name}: only Canon CR2 RAWs can be read in the browser; stack the JPEGs instead`);
        }
        const { buffer } = await fetchBytes(entry.jpeg_url, { cache: false, signal, onBytes });
        const frame = await jpegFrame(buffer, { linear });
        frame.fellBack = true;
        return frame;
    }
    const { buffer } = await fetchBytes(entry.url, { cache: false, signal, onBytes });
    return jpegFrame(buffer, { linear });
}
