// The whole stacking pipeline on synthetic RAW frames, in-process (the inline pool):
// calibration, quality rejection, alignment, every combine mode, and the outputs.
//   node --test tests/processing/stack.test.mjs        (a few seconds)
import assert from 'node:assert/strict';
import test from 'node:test';

import { InlinePool } from '../../static/js/processing/pool.js';
import { newEnv } from '../../static/js/processing/tasks.js';
import { MemoryStore } from '../../static/js/processing/store.js';
import { runStack } from '../../static/js/processing/stack.js';
import { decodeStrip, parseFitsHeader } from '../../static/js/processing/fits.js';
import { median } from '../../static/js/processing/image.js';
import { entries, makeScene, synthLoader } from './synth.mjs';

// Node has no OffscreenCanvas: the JPEG outputs are stubbed (the browser test covers them)
globalThis.OffscreenCanvas = class {
    constructor(w, h) { this.width = w; this.height = h; }
    getContext() { return { putImageData() {}, drawImage() {} }; }
    async convertToBlob() { return new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: 'image/jpeg' }); }
};
globalThis.ImageData = class { constructor(d, w, h) { Object.assign(this, { data: d, width: w, height: h }); } };

const scene = makeScene();
const LIGHT_MOVES = {
    0: {}, 1: { dx: 3.2, dy: -2.1, rot: 0.3 }, 2: { dx: -4.5, dy: 1.7, rot: -0.4 }, 3: { dx: 1.1, dy: 4.2 },
    4: { dx: -2.2, dy: -3.3, rot: 0.6 }, 5: { dx: 5.1, dy: 0.4 }, 6: { dx: -0.7, dy: -5.2, rot: -0.2 }, 7: { dx: 2.6, dy: 2.9 },
    8: { dx: 0.4, dy: -0.6, trail: true }, 9: { dx: 1.5, dy: 1.5, fwhm: 6.5 }
};
const loader = synthLoader(scene, LIGHT_MOVES);

function spec(lights = 10, extra = {}) {
    return {
        name: 'Synthetic', target: 'Test field',
        frames: { light: entries('light', lights), dark: entries('dark', 6), flat: entries('flat', 6, 1), darkflat: [], bias: entries('bias', 8, 0.001) },
        settings: { debayer: 'bilinear', ...extra }
    };
}

async function stack(s, { mode = null, opfs = true, referenceFrames = 24 } = {}) {
    const store = new MemoryStore();
    const pool = new InlinePool(newEnv({ store, loadFrame: loader }), 3);
    const result = await runStack(s, {
        pool, store: opfs ? store : null, opfs, memoryBudget: 2 * 1024 ** 3, diskFree: 50 * 1024 ** 3, mode, referenceFrames
    });
    await result.combiner.dispose();
    const bytes = new Uint8Array(await result.master.arrayBuffer());
    const { header, dataOffset } = parseFitsHeader(bytes);
    const w = header.NAXIS1, h = header.NAXIS2;
    const planes = [0, 1, 2].map(c => decodeStrip(bytes.subarray(dataOffset + c * w * h * 4, dataOffset + (c + 1) * w * h * 4), w, 0, h));
    return { result, header, planes, w, h };
}

function noise(plane, w, h) {
    // Robust sigma of neighbour differences in the middle, / sqrt 2
    const d = [];
    for (let y = 20; y < h - 20; y += 2) for (let x = 20; x < w - 21; x += 2) d.push(plane[y * w + x + 1] - plane[y * w + x]);
    const m = median(d);
    return 1.4826 * median(d.map(v => Math.abs(v - m))) / Math.SQRT2;
}

let memory;

function isolatedHot(g, w, h) {
    let isolated = 0;
    for (let y = 12; y < h - 12; y++) for (let x = 12; x < w - 12; x++) {
        const v = g[y * w + x];
        const ring = [];
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) if (dy || dx) ring.push(g[(y + dy) * w + x + dx]);
        // A hot pixel: well above the local level, its neighbours at most half as bright
        // (bilinear debayering spreads it by half); a star's are about 0.73 of its peak.
        // Only on the sky (under 0.1): the cores of bright stars can look like this too.
        const local = median(ring), nb = Math.max(...ring);
        if (local < 0.1 && v - local > 0.03 && nb - local < 0.6 * (v - local)) isolated++;
    }
    return isolated;
}

test('stacks in memory: rejection, calibration, outputs', async () => {
    memory = await stack(spec(), { mode: 'memory' });
    const { result, header, planes, w, h } = memory;
    assert.equal(result.width, 320);
    assert.equal(header.NAXIS3, 3);
    assert.equal(header.COMBMODE, 'memory');
    assert.equal(result.frames_total, 10);
    assert.equal(result.frames_used, 9, JSON.stringify(result.report.frames.map(f => [f.name, f.reason])));
    const blurred = result.report.frames.find(f => f.name === 'light_9.cr2');
    assert.match(blurred.reason, /blurred/);
    assert.equal(result.report.masters.dark.frames, 6);
    assert.ok(result.report.masters.hot_pixels >= 25, `hot pixels ${result.report.masters.hot_pixels}`);
    const crop = result.report.crop;
    assert.ok(crop.x0 > 0 || crop.y0 > 0 || crop.x1 < 1 || crop.y1 < 1, `common area crop ${JSON.stringify(crop)}`);
    assert.equal(result.preview.data.length, result.preview.width * result.preview.height * 3);

    // Vignetting flattened: the sky down the left edge is level (it falls ~50% without the flat)
    const g = planes[1];
    const at = (y0, x0) => median(Array.from({ length: 100 }, (_, i) => g[(y0 + Math.floor(i / 10)) * w + x0 + (i % 10)]));
    const top = at(15, 15), mid = at(h / 2 - 5, 15);
    assert.ok(Math.abs(top / mid - 1) < 0.08, `left edge top ${top} vs middle ${mid}`);

    // No isolated hot pixels survive
    assert.equal(isolatedHot(g, w, h), 0, 'isolated bright pixels');
});

test('disk mode gives the same result as memory mode', async () => {
    const disk = await stack(spec(), { mode: 'disk' });
    assert.equal(disk.header.COMBMODE, 'disk');
    for (let c = 0; c < 3; c++) for (let i = 0; i < disk.planes[c].length; i += 7) {
        const a = disk.planes[c][i], b = memory.planes[c][i];
        assert.ok((Number.isNaN(a) && Number.isNaN(b)) || a === b, `plane ${c} pixel ${i}: ${a} vs ${b}`);
    }
});

test('streaming with every frame in the reference matches memory mode', async () => {
    const s = await stack(spec(), { mode: 'stream' });
    assert.equal(s.header.COMBMODE, 'stream');
    // The limits are kept as float16, so a value right on a limit can go either way
    let differ = 0, total = 0;
    for (let c = 0; c < 3; c++) for (let i = 0; i < s.planes[c].length; i++) {
        const a = s.planes[c][i], b = memory.planes[c][i];
        if (Number.isNaN(a) || Number.isNaN(b)) continue;
        total++;
        if (Math.abs(a - b) > 1e-5) differ++;
    }
    assert.ok(differ < total * 1e-3, `${differ} of ${total} pixels differ`);
});

test('streaming against a small reference still removes the trail; no-OPFS path works', async () => {
    const s = await stack(spec(), { mode: 'stream', referenceFrames: 4, opfs: false });
    const clean = await stack(spec(8), { mode: 'memory', opfs: false });
    // The trail frame (8) is after the reference frames; compare with a stack without it
    const { w, h } = s;
    let trailish = 0;
    for (let y = 20; y < h - 20; y++) for (let x = 20; x < w - 20; x++) {
        const d = s.planes[1][y * w + x] - clean.planes[1][y * w + x];
        if (d > 0.015) trailish++;
    }
    assert.ok(trailish < 15, `${trailish} pixels look like the trail`);
    assert.ok(s.result.report.stats.rejected_high_pct > 0);
});

test('noise falls with the number of frames', async () => {
    const one = await stack({ ...spec(1), frames: { ...spec(1).frames } }, { mode: 'memory' });
    const n1 = noise(one.planes[1], one.w, one.h);
    const n9 = noise(memory.planes[1], memory.w, memory.h);
    assert.ok(n9 < 0.5 * n1, `noise ${n9} with 9 frames vs ${n1} with one`);
    assert.equal(one.result.kind, 'single');
});

test('half size (super-pixel)', async () => {
    const half = await stack(spec(10, { debayer: 'superpixel' }), { mode: 'memory' });
    assert.equal(half.w, 160);
    assert.equal(half.h, 120);
    assert.equal(half.result.frames_used, 9);
});

test('one frame without darks or cosmetic correction keeps its hot pixels (so the check above can fail)', async () => {
    // (With several dithered frames, sigma clipping takes them out anyway)
    const s = spec(1, { cosmetic: false, hot_pixels: false });
    s.frames.dark = [];
    const raw = await stack(s, { mode: 'memory' });
    assert.ok(isolatedHot(raw.planes[1], raw.w, raw.h) > 0);
});
