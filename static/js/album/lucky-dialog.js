// The lucky imaging dialog: a planet or Moon video (or a burst of photos), how much of
// it to keep, then the engine's lucky imaging (processing/lucky.js) in this tab.

import { engine } from './engine.js';
import { startJob } from './jobs.js';
import { runAndSave } from './stack-dialog.js';

const $ = (id) => document.getElementById(id);
let current = null;
let wired = false;

function entryOf(f) {
    const useRaw = f.kind === 'raw' || (f.raw_url && /\.cr2$/i.test(f.raw || ''));
    return { name: f.name, url: useRaw && f.raw_url ? f.raw_url : f.url, kind: useRaw ? 'raw' : f.kind, jpeg_url: f.kind === 'jpeg' ? f.url : null };
}

function wire() {
    if (wired) return;
    wired = true;
    const dialog = $('luckyDialog');
    dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
    $('luckyKeep').addEventListener('input', () => { $('luckyKeepOut').textContent = `${$('luckyKeep').value}%`; });
    $('btnLuckyStart').addEventListener('click', async () => {
        if (!current) return;
        const { frames, target, onSaved } = current;
        const spec = {
            mode: 'lucky',
            name: $('luckyName').value.trim() || current.defaultName,
            target,
            frames: { light: frames.map(entryOf) },
            settings: { keep_percent: Number($('luckyKeep').value), roi: $('luckyRoi').value }
        };
        dialog.close();
        const controller = new AbortController();
        const job = startJob(`Lucky imaging ${spec.name}`, { kind: 'stack', controller });
        Seleno.toast('Lucky imaging runs in this tab. For a video, keep the tab in view until it finishes.', 'ok', 6000);
        await runAndSave(spec, job, controller, onSaved);
    });
}

// frames: one video (MP4, WebM, SER...) or several photos of the Moon or a planet
export function openLuckyDialog({ frames, onSaved }) {
    if (!engine.available) {
        Seleno.toast(`Lucky imaging: ${engine.reason}`, 'warn', 6500);
        return;
    }
    wire();
    const video = frames.length === 1 && frames[0].kind === 'video';
    const target = frames.map(f => f.target).find(Boolean) || '';
    current = { frames, target, onSaved, defaultName: `${target || (video ? frames[0].name.replace(/\.[^.]+$/, '') : 'Lucky imaging')}` };
    $('luckyName').value = current.defaultName;
    $('luckySource').textContent = video
        ? `${frames[0].name}: every frame is scored for sharpness, the sharpest are lined up and averaged.`
        : `${frames.length} photos: the sharpest are lined up and averaged. This suits short exposures of the Moon or a planet, not star fields (use Stack for those).`;
    $('luckyDialog').showModal();
}
