// What happens to one frame: measuring its stars, turning it into colour, warping it
// onto the reference. Pure functions over frames (see decode.js for their shape); the
// tasks in tasks.js call these inside workers. Ports of the per-frame parts of
// app/processing/stack.py and analysis.py.

import { applyColour, linearToSrgb } from './colour.js';
import { encodeHalf, toHalf } from './half.js';
import { debayerChannel, debayerSuperpixel, mosaicLuminance, rgbLuminanceHalf, robustStats, subsample } from './image.js';
import { scaleTransform } from './register.js';
import { detectStars, summarise } from './stars.js';
import { isIdentity, warpRows } from './warp.js';

// A half-size monochrome image for finding stars: the 2x2 binned mosaic, or binned luma
export function luminanceHalf(frame) {
    if (frame.pattern) return mosaicLuminance(frame.planes[0], frame.width, frame.height);
    return rgbLuminanceHalf(frame.planes, frame.width, frame.height);
}

function maxOf(a) {
    let m = -Infinity;
    for (let i = 0; i < a.length; i++) if (a[i] > m) m = a[i];
    return m;
}

// Stars and the frame's summary, on the half-size luminance (stack.measure)
export function measureStars(frame, starSigma = 5) {
    const lum = luminanceHalf(frame);
    const peak = lum.data.length ? maxOf(lum.data) : 0;
    const st = detectStars(lum, { k: starSigma, maxStars: 800, saturation: peak > 0 ? 0.97 * peak : null });
    const summary = summarise(st, 2.0);
    const xy = st.x.slice(0, 300).map((x, i) => [x, st.y[i]]);
    return { summary, xy };
}

// The album's numbers for one frame (analysis.measure_file); lum is the half-size
// luminance, fullWidth the frame's real width
export function frameMetrics(lum, fullWidth, fullHeight, source) {
    const sat = 0.97;
    const st = detectStars(lum, { k: 5, maxStars: 1000, saturation: sat });
    const s = summarise(st, fullWidth / lum.width);
    const sample = subsample(lum.data, lum.width, lum.height, 3);
    let clipped = 0;
    for (let i = 0; i < sample.length; i++) if (sample[i] >= sat) clipped++;
    const round = (v, d) => (v === null || v === undefined ? null : Math.round(v * 10 ** d) / 10 ** d);
    return {
        stars: s.stars,
        fwhm: round(s.fwhm, 3),
        hfr: round(s.hfr, 3),
        ecc: round(s.ecc, 3),
        snr: round(s.snr, 3),
        background: round(s.background, 6),
        noise: round(s.noise, 7),
        saturated_stars: s.saturated_stars,
        source,
        width: fullWidth,
        height: fullHeight,
        clipped_pct: round(100 * clipped / Math.max(sample.length, 1), 4),
        background_display: round(linearToSrgb(s.background), 4),
        background_pct: round(100 * s.background, 3),
        noise_pct: round(100 * s.noise, 4)
    };
}

// Camera RGB to (white balanced, colour-matrixed) linear RGB planes (stack.to_rgb)
export function toRgb(frame, settings) {
    if (!frame.pattern) return { planes: frame.planes, width: frame.width, height: frame.height };
    let out;
    if (settings.debayer === 'superpixel') {
        out = debayerSuperpixel(frame.planes[0], frame.width, frame.height, frame.pattern);
    } else {
        const planes = [0, 1, 2].map(c => debayerChannel(frame.planes[0], frame.width, frame.height, frame.pattern, c));
        out = { planes, width: frame.width, height: frame.height };
    }
    if (settings.colour !== 'none') {
        const wb = settings.wb === 'as_shot' ? frame.wbAsShot : frame.wb;
        applyColour(out.planes, wb, settings.colour === 'camera' ? frame.camToSrgb : null);
    }
    return out;
}

// The half-size star transform for the output image's pixels (stack.output_transform)
export function outputTransform(mHalf, settings, isRaw) {
    if (isRaw && settings.debayer === 'superpixel') return mHalf;
    return scaleTransform(mHalf, 2, 0.5);
}

export function outputShape(width, height, settings, isRaw) {
    return isRaw && settings.debayer === 'superpixel' ? [width >> 1, height >> 1] : [width, height];
}

// Warp three planes with m into float16 planes (one Uint16Array, plane after plane),
// and each plane's background level and spread (from every 5th row and column). The
// input planes are released as they're done.
export function warpToHalf(rgb, m, outW, outH, cubic = true, strip = 128) {
    const plane = outW * outH;
    const data = new Uint16Array(3 * plane);
    const loc = [0, 0, 0], scale = [1, 1, 1];
    const identity = isIdentity(m) && rgb.width === outW && rgb.height === outH;
    const tmp = new Float32Array(strip * outW);
    rgb.planes.forEach((src, c) => {
        const samples = [];
        for (let y0 = 0; y0 < outH; y0 += strip) {
            const y1 = Math.min(outH, y0 + strip);
            const rows = identity ? src.subarray(y0 * outW, y1 * outW) : warpRows(src, rgb.width, rgb.height, m, outW, y0, y1, cubic, tmp);
            const base = c * plane + y0 * outW;
            const n = (y1 - y0) * outW;
            for (let i = 0; i < n; i++) data[base + i] = toHalf(rows[i]);
            for (let y = y0 + ((5 - (y0 % 5)) % 5); y < y1; y += 5) {
                const r = (y - y0) * outW;
                for (let x = 0; x < outW; x += 5) {
                    const v = rows[r + x];
                    if (Number.isFinite(v)) samples.push(v);
                }
            }
        }
        const [med, sd] = samples.length ? robustStats(samples) : [0, 1];
        loc[c] = med;
        scale[c] = Math.max(sd, 1e-7);
        rgb.planes[c] = null;          // done with it: frees memory while the output grows
    });
    return { data, loc, scale };
}

// A calibrated frame as float16 planes (to cache between the passes) and back
export function frameToHalf(frame) {
    const n = frame.width * frame.height;
    const data = new Uint16Array(frame.planes.length * n);
    frame.planes.forEach((p, c) => encodeHalf(p, data.subarray(c * n, (c + 1) * n)));
    return data;
}
