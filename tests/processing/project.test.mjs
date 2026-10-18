// A project over two nights: the first night's running totals go out as FITS, come
// back by range requests, and the second night adds to them.
//   node --test tests/processing/project.test.mjs        (a few seconds)
import assert from 'node:assert/strict';
import test from 'node:test';

import { InlinePool } from '../../static/js/processing/pool.js';
import { newEnv } from '../../static/js/processing/tasks.js';
import { MemoryStore } from '../../static/js/processing/store.js';
import { runStack, PROJECT_FIXED } from '../../static/js/processing/stack.js';
import { loadTotals, projectSettings, totalsBlobs } from '../../static/js/processing/projects.js';
import { decodeStrip, parseFitsHeader } from '../../static/js/processing/fits.js';
import { entries, makeScene, synthLoader } from './synth.mjs';

globalThis.OffscreenCanvas = class {
    constructor(w, h) { this.width = w; this.height = h; }
    getContext() { return { putImageData() {}, drawImage() {} }; }
    async convertToBlob() { return new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: 'image/jpeg' }); }
};
globalThis.ImageData = class { constructor(d, w, h) { Object.assign(this, { data: d, width: w, height: h }); } };

// The server, for loadTotals: files by URL, with Range
const served = new Map();
globalThis.fetch = async (url, init = {}) => {
    const blob = served.get(url);
    if (!blob) return new Response('missing', { status: 404 });
    const m = /bytes=(\d+)-(\d+)/.exec((init.headers || {}).Range || '');
    if (!m) return new Response(blob, { status: 200 });
    return new Response(blob.slice(Number(m[1]), Number(m[2]) + 1), { status: 206 });
};

const scene = makeScene({ seed: 5 });
const MOVES = {
    0: {}, 1: { dx: 3, dy: -2, rot: 0.3 }, 2: { dx: -4, dy: 2 }, 3: { dx: 1, dy: 4 }, 4: { dx: -2, dy: -3, rot: 0.5 },
    5: { dx: 5, dy: 0.4, rot: -0.3 }, 6: { dx: -0.7, dy: -5 }, 7: { dx: 2.6, dy: 2.9 }, 8: { dx: 0.4, dy: -0.6, trail: true }
};
const loader = synthLoader(scene, MOVES);
const lights = entries('light', 9);

function ctx() {
    const store = new MemoryStore();
    return { pool: new InlinePool(newEnv({ store, loadFrame: loader }), 2), store, opfs: true, memoryBudget: 2 ** 31, diskFree: 2 ** 36 };
}

function planes(bytes) {
    const { header, dataOffset } = parseFitsHeader(bytes);
    const w = header.NAXIS1, h = header.NAXIS2;
    return { w, h, header, p: [0, 1, 2].map(c => decodeStrip(bytes.subarray(dataOffset + c * w * h * 4, dataOffset + (c + 1) * w * h * 4), w, 0, h)) };
}

test('two nights in a project equal one stack of everything', async () => {
    const settings = { debayer: 'superpixel' };
    // With flats: on frames this small the 64 px background cells can't follow raw vignetting
    const spec = (frames) => ({ name: 'Project', frames: { light: frames, dark: entries('dark', 4), flat: entries('flat', 4, 1), darkflat: [], bias: entries('bias', 4, 0.001) }, settings });

    // Night one: a new project
    const c1 = ctx();
    const night1 = await runStack(spec(lights.slice(0, 5)), { ...c1, project: { meta: null, totals: null, skip: new Set() } });
    assert.equal(night1.report.combine.mode, 'stream');
    assert.equal(night1.night.frames.length, 5);
    const blobs = await totalsBlobs(night1.combiner, { frames: 5, name: 'Project' });
    await night1.combiner.dispose();
    const meta = {
        id: 'p1', width: night1.width, height: night1.height, source: 'raw', generation: 1, frames_total: 5,
        exposure_total_s: night1.night.exposure_s, frames: night1.night.frames,
        settings: projectSettings(night1.settings, PROJECT_FIXED), reference: night1.projectReference
    };
    Object.entries(blobs).forEach(([k, b]) => served.set(`/album/projects/p1/current/${k}.fits`, b));

    // Night two: carry on from the saved totals; frames already in are skipped
    const totals = await loadTotals(meta);
    assert.ok(totals.hasLimits);
    const c2 = ctx();
    const night2 = await runStack(spec(lights), { ...c2, project: { meta, totals, skip: new Set(meta.frames) } });
    await night2.combiner.dispose();
    assert.equal(night2.night.frames.length, 4);
    assert.equal(night2.frames_used, 9);
    assert.equal(night2.total_exposure_s, 9 * 60);
    assert.ok(night2.report.warnings.some(w => /already in the project/.test(w)));

    // One stack of all nine, streaming, with the project's reference frame: the same
    // five frames make the rejection limits, and every frame lands on the same grid
    assert.ok(/light_[0-4]\.cr2/.test(night1.projectReference.name));
    const c3 = ctx();
    const one = spec(lights);
    one.settings = { ...settings, reference: night1.projectReference.name };
    const all = await runStack(one, { ...c3, mode: 'stream', referenceFrames: 5 });
    await all.combiner.dispose();
    const a = planes(new Uint8Array(await night2.master.arrayBuffer()));
    const b = planes(new Uint8Array(await all.master.arrayBuffer()));
    assert.equal(a.header.NCOMBINE, 9);
    let sum = 0, n = 0;
    for (let c = 0; c < 3; c++) for (let i = 0; i < a.p[c].length; i++) {
        if (Number.isNaN(a.p[c][i]) || Number.isNaN(b.p[c][i])) continue;
        sum += Math.abs(a.p[c][i] - b.p[c][i]);
        n++;
    }
    assert.ok(sum / n < 2e-5, `mean difference ${sum / n}`);

    // The same frames again add nothing
    await assert.rejects(runStack(spec(lights), { ...ctx(), project: { meta: { ...meta, frames: lights.map(l => l.name) }, totals, skip: new Set(lights.map(l => l.name)) } }), /already in the project/);
});
