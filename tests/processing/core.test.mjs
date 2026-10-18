// The engine's maths against app/processing (Python), on the data oracle.py writes.
//   ORACLE=<dir> node --test tests/processing/core.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { backgroundMap, debayerChannel, debayerSuperpixel, gaussianFilter, maximumFilter, mosaicLuminance, robustStats, subsample } from '../../static/js/processing/image.js';
import { detectStars, summarise } from '../../static/js/processing/stars.js';
import { applyTransform, matchStars, phaseCorrelate, scaleTransform } from '../../static/js/processing/register.js';
import { combine } from '../../static/js/processing/integrate.js';
import { camToSrgbMatrix } from '../../static/js/processing/colour.js';
import { decodeHalf, encodeHalf } from '../../static/js/processing/half.js';
import * as develop from '../../static/js/processing/develop.js';

const DIR = process.env.ORACLE;
const manifest = DIR ? JSON.parse(fs.readFileSync(path.join(DIR, 'manifest.json'), 'utf8')) : null;
const skip = !manifest && 'set ORACLE to the folder oracle.py wrote';

function load(ref) {
    const buf = fs.readFileSync(path.join(DIR, ref.file));
    return { data: new Float32Array(buf.buffer, buf.byteOffset, buf.byteLength / 4), shape: ref.shape };
}

function mono(ref) {
    const a = load(ref);
    return { data: a.data, width: a.shape[a.shape.length - 1], height: a.shape[a.shape.length - 2] };
}

function maxAbsDiff(a, b) {
    let m = 0;
    for (let i = 0; i < a.length; i++) {
        if (Number.isNaN(a[i]) && Number.isNaN(b[i])) continue;
        m = Math.max(m, Math.abs(a[i] - b[i]));
    }
    return m;
}

const close = (a, b, tol, what) => assert.ok(Math.abs(a - b) <= tol, `${what}: ${a} vs ${b} (tolerance ${tol})`);

test('robust stats, background map, gaussian and maximum filters', { skip }, () => {
    const b = manifest.basics;
    const img = mono(b.img);
    const [med, sd] = robustStats(subsample(img.data, img.width, img.height, 3));
    close(med, b.robust[0], 1e-6, 'median');
    close(sd, b.robust[1], 1e-6, 'sigma');
    assert.ok(maxAbsDiff(backgroundMap(img.data, img.width, img.height), load(b.background).data) < 1e-5, 'background map');
    assert.ok(maxAbsDiff(gaussianFilter(img.data, img.width, img.height, 1.0), load(b.gauss).data) < 1e-5, 'gaussian');
    assert.ok(maxAbsDiff(maximumFilter(img.data, img.width, img.height, 5), load(b.maxf).data) === 0, 'maximum filter');
});

test('star detection and measurement match stars.py', { skip }, () => {
    for (const c of manifest.stars) {
        const img = mono(c.img);
        const st = detectStars(img, { saturation: 0.89 });
        const ref = c.result;
        assert.ok(Math.abs(st.x.length - ref.count) <= Math.max(1, ref.count * 0.01), `fwhm ${c.fwhm}: ${st.x.length} stars vs ${ref.count}`);
        // The brightest stars line up one to one
        for (let i = 0; i < Math.min(20, ref.count); i++) {
            close(st.x[i], ref.x[i], 1e-3, `star ${i} x`);
            close(st.y[i], ref.y[i], 1e-3, `star ${i} y`);
            close(st.fwhm[i], ref.fwhm[i], 1e-3 * ref.fwhm[i], `star ${i} fwhm`);
            close(st.ecc[i], ref.ecc[i], 2e-3, `star ${i} ecc`);
            close(st.hfr[i], ref.hfr[i], 1e-3 * ref.hfr[i], `star ${i} hfr`);
            assert.equal(st.saturated[i], ref.saturated[i]);
        }
        const s = summarise(st);
        for (const key of ['fwhm', 'hfr', 'ecc']) close(s[key], c.summary[key], 0.01 * c.summary[key] + 1e-3, `summary ${key}`);
        close(s.background, c.summary.background, 1e-6, 'background');
        close(s.noise, c.summary.noise, 1e-6, 'noise');
    }
});

test('registration: shift, rotation and meridian flip', { skip }, () => {
    const r = manifest.register;
    const { m, matched, rms } = matchStars(r.ref_xy, r.tgt_xy);
    for (let i = 0; i < 2; i++) for (let j = 0; j < 3; j++) close(m[i][j], r.m[i][j], j === 2 ? 0.02 : 1e-4, `m[${i}][${j}]`);
    assert.ok(Math.abs(matched - r.matched) <= 2, `matched ${matched} vs ${r.matched}`);
    close(rms, r.rms, 0.05, 'rms');
    const flip = matchStars(r.ref_xy, r.flip_xy).m;
    for (let i = 0; i < 2; i++) for (let j = 0; j < 3; j++) close(flip[i][j], r.m_flip[i][j], j === 2 ? 0.05 : 1e-3, `flip m[${i}][${j}]`);
    const [px, py] = applyTransform(m, 100, 100);
    const t = r.truth, a = t.rot * Math.PI / 180;
    close(px, Math.cos(a) * (100 - t.cx) - Math.sin(a) * (100 - t.cy) + t.cx + t.dx, 0.1, 'true x');
    close(py, Math.sin(a) * (100 - t.cx) + Math.cos(a) * (100 - t.cy) + t.cy + t.dy, 0.1, 'true y');
    const scaled = scaleTransform(r.m, 2, 0.5);
    for (let i = 0; i < 2; i++) for (let j = 0; j < 3; j++) close(scaled[i][j], r.scaled[i][j], 1e-9, 'scale_transform');
});

test('phase correlation finds a sub-pixel shift', { skip }, () => {
    const p = manifest.phase;
    const ref = mono(p.ref), img = mono(p.img);
    const { dy, dx } = phaseCorrelate(ref.data, img.data, ref.width, ref.height);
    // Python transforms at the image size, JS pads to powers of two: both near the truth (2.3, -3.6)
    close(dy, p.dy, 0.25, 'dy');
    close(dx, p.dx, 0.25, 'dx');
});

test('integration matches integrate.py for every method', { skip }, () => {
    const g = manifest.integrate;
    const stack = load(g.stack);
    const [n, h, w] = stack.shape;
    const frames = Array.from({ length: n }, (_, k) => stack.data.subarray(k * h * w, (k + 1) * h * w));
    for (const [key, ref] of Object.entries(g.results)) {
        const [method, wkind] = key.split('_');
        const out = combine(frames, { method, weights: wkind === 'w' ? g.weights : null });
        const diff = maxAbsDiff(out.result, load(ref.result).data);
        assert.ok(diff < 2e-6, `${key}: max difference ${diff}`);
        assert.equal(out.rejectedLow, ref.rej_low, `${key} rejected low`);
        assert.equal(out.rejectedHigh, ref.rej_high, `${key} rejected high`);
        assert.equal(out.coverage.reduce((a, b) => a + b, 0), ref.coverage_sum, `${key} coverage`);
    }
});

test('debayering and binning', { skip }, () => {
    const d = manifest.debayer;
    const mos = mono(d.mosaic);
    const bil = load(d.bilinear).data;     // (H, W, 3)
    for (let c = 0; c < 3; c++) {
        const ch = debayerChannel(mos.data, mos.width, mos.height, d.pattern, c);
        let m = 0;
        for (let i = 0; i < ch.length; i++) m = Math.max(m, Math.abs(ch[i] - bil[i * 3 + c]));
        assert.ok(m < 1e-6, `bilinear channel ${c}: ${m}`);
    }
    const sp = debayerSuperpixel(mos.data, mos.width, mos.height, d.pattern);
    const ref = load(d.superpixel).data;
    for (let c = 0; c < 3; c++) for (let i = 0; i < sp.planes[c].length; i++) close(sp.planes[c][i], ref[i * 3 + c], 1e-6, 'superpixel');
    assert.ok(maxAbsDiff(mosaicLuminance(mos.data, mos.width, mos.height).data, load(d.luminance).data) < 1e-6);
    const m = camToSrgbMatrix(manifest.colour.cam_xyz);
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) close(m[i][j], manifest.colour.cam_to_srgb[i][j], 1e-5, 'cam_to_srgb');
});

test('float16 round trip', () => {
    const v = new Float32Array([0, 1, -2.5, 0.1, 1e-5, 65504, NaN, 3.14159, 6e-8]);
    const back = decodeHalf(encodeHalf(v));
    assert.ok(Number.isNaN(back[6]));
    [0, 1, -2.5, 65504].forEach((x, i) => assert.equal(back[[0, 1, 2, 5][i]], x));
    close(back[3], 0.1, 1e-4, '0.1');
    close(back[7], 3.14159, 2e-3, 'pi');
    close(back[4], 1e-5, 1e-7, 'subnormal');
});

test('developing matches develop.py', { skip }, () => {
    const d = manifest.develop;
    const a = load(d.img);
    const [h, w] = a.shape;
    const planes = [0, 1, 2].map(c => {
        const p = new Float32Array(w * h);
        for (let i = 0; i < w * h; i++) p[i] = a.data[i * 3 + c];
        return p;
    });
    const img = { planes, width: w, height: h };
    const wav = develop.waveletLayers(planes[1], w, h, 4);
    assert.ok(maxAbsDiff(wav.layers[0], load(d.wavelet0).data) < 1e-5, 'wavelet layer 0');
    assert.ok(maxAbsDiff(wav.residual, load(d.wavelet_res).data) < 1e-5, 'wavelet residual');
    for (const c of d.cases) {
        const resolved = develop.resolve(img, c.params);
        const r = c.resolved;
        ['offsets', 'sky', 'factors'].forEach(k => r[k].forEach((v, i) => close(resolved[k][i], v, 2e-3 * Math.abs(v) + 2e-5, `${c.name} ${k}[${i}]`)));
        if (r.levels) ['black', 'mid'].forEach(k => r.levels[k].forEach((v, i) => close(resolved.levels[k][i], v, 2e-3 * Math.abs(v) + 2e-5, `${c.name} levels.${k}`)));
        if (r.asinh) ['black', 'white'].forEach(k => close(resolved.asinh[k], r.asinh[k], 2e-3 * Math.abs(r.asinh[k]) + 2e-5, `${c.name} asinh.${k}`));
        const out = develop.apply(img, resolved);
        const ref = load(c.out).data;
        let m = 0, sum = 0;
        for (let ch = 0; ch < 3; ch++) for (let i = 0; i < out.planes[ch].length; i++) {
            const e = Math.abs(out.planes[ch][i] - ref[i * 3 + ch]);
            m = Math.max(m, e);
            sum += e;
        }
        const mean = sum / (3 * w * h);
        assert.ok(mean < 2e-3 && m < 0.05, `${c.name}: mean error ${mean}, max ${m}`);
    }
});

test('unrelated star fields do not "align" by chance', async () => {
    const { rng } = await import('./synth.mjs');
    const { RegistrationError } = await import('../../static/js/processing/register.js');
    const field = (seed) => { const r = rng(seed); return Array.from({ length: 260 }, () => [8 + 884 * r(), 8 + 584 * r()]); };
    let chance = 0;
    for (let s = 1; s <= 10; s++) {
        try {
            matchStars(field(s), field(100 + s));
            chance++;
        } catch (error) {
            assert.ok(error instanceof RegistrationError);
        }
    }
    assert.equal(chance, 0, `${chance} of 10 unrelated pairs matched`);
});
