// Lucky imaging on a synthetic planet: frames blurred by "seeing" by different amounts
// and drifting; the sharpest should be chosen, lined up and averaged.
//   node --test tests/processing/lucky.test.mjs        (a couple of seconds)
import assert from 'node:assert/strict';
import test from 'node:test';

import { InlinePool } from '../../static/js/processing/pool.js';
import { newEnv } from '../../static/js/processing/tasks.js';
import { LUCKY_TASKS, analyse } from '../../static/js/processing/lucky-tasks.js';
import { runLucky } from '../../static/js/processing/lucky.js';
import { gaussianFilter } from '../../static/js/processing/image.js';
import { rng } from './synth.mjs';

globalThis.OffscreenCanvas = class {
    constructor(w, h) { this.width = w; this.height = h; }
    getContext() { return { putImageData() {}, drawImage() {} }; }
    async convertToBlob() { return new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: 'image/jpeg' }); }
};
globalThis.ImageData = class { constructor(d, w, h) { Object.assign(this, { data: d, width: w, height: h }); } };

const W = 200, H = 150;

function planet(dx, dy, blur, seed) {
    const r = rng(seed);
    const lum = new Float32Array(W * H);
    const cx = 100 + dx, cy = 75 + dy, R = 34;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const d = Math.hypot(x - cx, y - cy);
        if (d < R) lum[y * W + x] = 0.55 + 0.25 * Math.sin((y - cy) * 0.5) + 0.15 * (Math.hypot(x - cx - 10, y - cy + 8) < 5 ? 1 : 0);
    }
    const b = blur > 0.05 ? gaussianFilter(lum, W, H, blur) : lum;
    const rgba = new Uint8ClampedArray(W * H * 4);
    for (let i = 0; i < W * H; i++) {
        const v = Math.min(1, Math.max(0, b[i] + 0.01 * r.normal()));
        rgba[i * 4] = v * 255; rgba[i * 4 + 1] = v * 230; rgba[i * 4 + 2] = v * 190; rgba[i * 4 + 3] = 255;
    }
    return rgba;
}

function frames(n) {
    const r = rng(9);
    return Array.from({ length: n }, (_, i) => ({ dx: (r() - 0.5) * 10, dy: (r() - 0.5) * 10, blur: i % 4 === 0 ? 0.3 : 1.2 + 2 * r(), i }));
}

test('sharpness scoring prefers the sharp frame', () => {
    const sharp = planet(0, 0, 0.3, 1), soft = planet(0, 0, 2.5, 1);
    const toPlanes = (rgba) => [0, 1, 2].map(c => Float32Array.from({ length: W * H }, (_, i) => rgba[i * 4 + c] / 255));
    assert.ok(analyse(toPlanes(sharp), W, H).quality > 2 * analyse(toPlanes(soft), W, H).quality);
    const a = analyse(toPlanes(planet(6, -4, 1, 2)), W, H);
    assert.ok(Math.abs(a.cx - 106) < 1 && Math.abs(a.cy - 71) < 1, `centre ${a.cx}, ${a.cy}`);
});

test('lucky imaging picks, aligns and averages the best frames', async () => {
    const list = frames(40);
    const source = {
        count: list.length,
        async *frames() { for (const f of list) yield { frame: { rgba: planet(f.dx, f.dy, f.blur, f.i + 1), width: W, height: H }, transfer: [] }; }
    };
    const pool = new InlinePool(newEnv({ extraTasks: LUCKY_TASKS }), 2);
    const result = await runLucky({ name: 'Jupiter', frames: { light: [] }, settings: { keep_percent: 25 } }, { pool, source });
    assert.equal(result.kind, 'planetary');
    assert.equal(result.frames_total, 40);
    assert.equal(result.frames_used, 10);
    // The kept frames are the sharp ones (every fourth)
    const accepted = result.report.frames.filter(f => f.accepted).map(f => f.frame);
    assert.ok(accepted.filter(i => i % 4 === 0).length >= 9, `accepted ${accepted}`);
    assert.ok(result.width >= 64 && result.width === result.height && result.width % 2 === 0);
    // Shifts were measured (the crops follow the planet, so they're small)
    assert.ok(result.report.shifts.every(([dx, dy]) => Math.abs(dx) < 3 && Math.abs(dy) < 3), JSON.stringify(result.report.shifts));
    // The result is sharp: inside the disc, its bands are as steep as a sharp frame's
    // (about 0.11 a pixel; the blurred frames' are about 0.06)
    const g = result.previewPlanes.planes[1], w = result.previewPlanes.width;
    let grad = 0;
    for (let y = w / 2 - 20; y < w / 2 + 20; y++) grad = Math.max(grad, Math.abs(g[y * w + w / 2 - 6] - g[(y - 1) * w + w / 2 - 6]));
    assert.ok(grad > 0.085, `steepest step ${grad}`);
});
