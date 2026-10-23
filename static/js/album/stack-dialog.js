// The stack dialog: which frames go in (lights, plus darks, flats, dark flats and bias
// that match them), the settings, and an estimate. Builds the spec engine.stack takes;
// the settings mirror app/processing/stack.py's DEFAULT_SETTINGS.

import { engine } from './engine.js';
import { startJob } from './jobs.js';
import { saveStackResult } from './stacks.js';
import * as Q from './quality.js';
import { listProjects, updateProject } from '../processing/projects.js';

const $ = (id) => document.getElementById(id);
const el = (...args) => Seleno.el(...args);

const DEFAULTS_KEY = 'seleno_stack_defaults';
export const DEFAULT_SETTINGS = {
    debayer: 'bilinear',
    colour: 'camera',
    wb: 'daylight',
    calibration_method: 'winsorized',
    dark_scaling: true,
    hot_pixels: true,
    cosmetic: true,
    flat_per_channel: true,
    reference: 'auto',
    transform: 'similarity',
    star_sigma: 5,
    interpolation: 'bicubic',
    method: 'winsorized',
    kappa_low: 3,
    kappa_high: 3,
    iterations: 3,
    normalise: true,
    weighting: 'noise',
    reject_auto: true,
    keep_best: null,
    crop_common: true,
    keep_masters: false
};
const METHODS = [
    ['winsorized', _('Winsorized sigma clipping')],
    ['sigma', _('Sigma clipping')],
    ['median', _('Median')],
    ['average', _('Average (no rejection)')]
];
const CALIBRATION_HELP = {
    dark: _("Darks take away the camera's own glow and hot pixels. Take them with the cover on, at the same exposure and ISO as the lights."),
    bias: _('Bias frames are the shortest possible exposures with the cover on: they take away the camera\'s fixed pattern.'),
    flat: _('Flats remove vignetting and dust shadows. Take them of an evenly lit sky or panel without moving the focus.'),
    darkflat: _('Dark flats match the flats\' exposure with the cover on. The bias frames stand in for them.')
};

let ctx = null;         // the dialog's current job
let wired = false;

function storedDefaults() {
    try {
        return Object.assign({}, DEFAULT_SETTINGS, JSON.parse(localStorage.getItem(DEFAULTS_KEY) || '{}'));
    } catch (error) {
        return { ...DEFAULT_SETTINGS };
    }
}

function sameIso(a, b) {
    return String(a || '').replace(/\D/g, '') === String(b || '').replace(/\D/g, '');
}

function sameExposure(a, b) {
    if (!a || !b) return false;
    return Math.abs(a - b) / Math.max(a, b) <= 0.02;
}

function daysBetween(nightA, nightB) {
    if (nightA === 'unknown' || nightB === 'unknown') return 9999;
    return Math.abs(new Date(nightA) - new Date(nightB)) / 86400000;
}

// Candidate calibration frames of one type, grouped by night, nearest night first
function candidates(type, frames, ref) {
    const pool = frames.filter(f => Q.typeOf(f) === type && f.flag !== 'reject').filter(f => {
        if (type === 'dark') return sameIso(f.iso, ref.iso) && sameExposure(f.exposure_s, ref.exposure);
        if (type === 'bias') return sameIso(f.iso, ref.iso);
        if (type === 'flat' || type === 'darkflat') return !ref.camera || !f.camera || f.camera === ref.camera;
        return true;
    });
    const byNight = new Map();
    pool.forEach(f => {
        const night = Q.nightKey(f);
        if (!byNight.has(night)) byNight.set(night, []);
        byNight.get(night).push(f);
    });
    return [...byNight.entries()]
        .map(([night, list]) => ({ night, frames: list, distance: daysBetween(night, ref.night) }))
        .filter(g => g.distance <= 120)
        .sort((a, b) => a.distance - b.distance);
}

function describeChoice(type, group, ref) {
    if (!group) return { level: 'off', title: _('No {type}', { type: Q.TYPE_LABELS[type].toLowerCase() }), detail: CALIBRATION_HELP[type] };
    const n = group.frames.length;
    const when = group.night === ref.night ? _('Same night') : _('From {night}', { night: Q.nightLabel(group.night) });
    const what = type === 'dark' ? _('same exposure and ISO') : type === 'bias' ? _('same ISO') : _('same camera');
    return { level: group.night === ref.night ? 'ok' : 'warn', title: `${n} ${n === 1 ? Q.TYPE_SINGULAR[type] : Q.TYPE_LABELS[type].toLowerCase()}`, detail: _('{when}, {what}', { when, what }) };
}

function renderRows() {
    const box = $('stackFrameRows');
    box.innerHTML = '';
    const { lights, rejected, ref } = ctx;
    const total = lights.reduce((s, f) => s + (f.exposure_s || 0), 0);
    const kinds = lights.some(f => f.raw || f.kind === 'raw') ? (ctx.source === 'raw' ? 'RAW' : 'JPEG') : 'JPEG';
    box.appendChild(el('div', { className: 'ws-frame-row' }, [
        el('span', { className: `ws-dot ${lights.length >= 2 ? 'ok' : 'warn'}` }),
        el('div', { style: { flex: 1 } }, [
            el('b', {}, lights.length === 1 ? _('1 light') : _('{n} lights', { n: lights.length })),
            el('div', { className: 'ws-muted ws-small' }, [
                ref.exposure ? Seleno.fmt.exposure(ref.exposure) : null, ref.iso ? _('ISO {iso}', { iso: ref.iso }) : null, kinds,
                total ? _('{duration} total', { duration: Seleno.fmt.duration(total) }) : null,
                rejected ? _('{n} rejected', { n: rejected }) : null,
                ctx.bad ? _('{n} look bad', { n: ctx.bad }) : null
            ].filter(Boolean).join(' · '))
        ])
    ]));
    ['dark', 'flat', 'darkflat', 'bias'].forEach(type => {
        const group = ctx.choice[type];
        const d = describeChoice(type, group, ref);
        const options = ctx.options[type];
        const nearestOther = !group && options.length ? options[0] : null;
        let detail = d.detail;
        if (nearestOther) detail = _('None from this night; {frames} from {night} match.', { frames: nearestOther.frames.length, night: Q.nightLabel(nearestOther.night) });
        box.appendChild(el('div', { className: 'ws-frame-row', style: { marginTop: '8px' } }, [
            el('span', { className: `ws-dot ${d.level}` }),
            el('div', { style: { flex: 1 } }, [el('b', {}, d.title), el('div', { className: 'ws-muted ws-small' }, detail)]),
            nearestOther ? el('button', { className: 'ws-btn sm', type: 'button', onclick: () => { ctx.choice[type] = nearestOther; refresh(); } }, _('Use them')) : null,
            options.length > (group ? 1 : 0) || group ? el('button', { className: 'ws-btn sm ghost', type: 'button', onclick: () => chooseAlternative(type) }, _('Change')) : null
        ]));
    });
}

function chooseAlternative(type) {
    const dialog = $('altDialog');
    $('altTitle').textContent = _('Choose {type}', { type: Q.TYPE_LABELS[type].toLowerCase() });
    const box = $('altGroups');
    box.innerHTML = '';
    const pick = (group) => {
        ctx.choice[type] = group;
        dialog.close();
        refresh();
    };
    box.appendChild(el('button', { className: 'ws-btn', type: 'button', onclick: () => pick(null) }, _('No {type}', { type: Q.TYPE_LABELS[type].toLowerCase() })));
    ctx.options[type].forEach(group => {
        const current = ctx.choice[type] === group;
        box.appendChild(el('button', { className: `ws-btn${current ? ' sel' : ''}`, type: 'button', onclick: () => pick(group), style: { justifyContent: 'space-between' } }, [
            el('span', {}, Q.nightLabel(group.night)),
            el('span', { className: 'ws-muted' }, _('{n} frames', { n: group.frames.length }) + (group.frames[0].exposure_s ? ' · ' + Seleno.fmt.exposure(group.frames[0].exposure_s) : '') + (group.frames[0].iso ? ' · ISO ' + group.frames[0].iso : ''))
        ]));
    });
    if (!ctx.options[type].length) box.appendChild(el('p', { className: 'ws-small ws-muted' }, _('None in your album match these lights. {help}', { help: CALIBRATION_HELP[type] })));
    dialog.showModal();
}

function readSettings() {
    const s = { ...ctx.settings };
    document.querySelectorAll('#stackDialog [data-setting]').forEach(input => {
        const key = input.dataset.setting;
        if (input.type === 'checkbox') s[key] = input.checked;
        else if (input.tagName === 'SELECT' && (input.value === 'true' || input.value === 'false')) s[key] = input.value === 'true';
        else if (['kappa_low', 'kappa_high', 'star_sigma'].includes(key)) {
            const v = parseFloat(input.value);
            s[key] = isNaN(v) ? DEFAULT_SETTINGS[key] : v;
        } else s[key] = input.value;
    });
    if (Seleno.mode() === 'basic') {
        const choice = (document.querySelector('input[name="stackQuality"]:checked') || {}).value || 'auto';
        s.reject_auto = choice !== 'all';
        s.keep_best = choice === 'best' ? 0.5 : null;
        s.debayer = $('stackHalfSize').checked ? 'superpixel' : 'bilinear';
    }
    return s;
}

function writeSettings(s) {
    document.querySelectorAll('#stackDialog [data-setting]').forEach(input => {
        const value = s[input.dataset.setting];
        if (input.type === 'checkbox') input.checked = !!value;
        else input.value = String(value);
    });
    $('stackHalfSize').checked = s.debayer === 'superpixel';
    const choice = !s.reject_auto ? 'all' : s.keep_best ? 'best' : 'auto';
    const radio = document.querySelector(`input[name="stackQuality"][value="${choice}"]`);
    if (radio) radio.checked = true;
}

function selectedCalibration() {
    const out = {};
    ['dark', 'flat', 'darkflat', 'bias'].forEach(type => { out[type] = ctx.choice[type] ? ctx.choice[type].frames : []; });
    return out;
}

function frameEntry(f) {
    const useRaw = ctx.source === 'raw' && (f.raw_url || f.kind === 'raw');
    return {
        name: f.name,
        url: useRaw ? (f.raw_url || f.url) : f.url,
        jpeg_url: f.kind === 'jpeg' ? f.url : null,
        kind: useRaw ? 'raw' : f.kind,
        exposure_s: f.exposure_s,
        iso: f.iso,
        camera: f.camera,
        captured_at: f.captured_at,
        width: f.width,
        height: f.height,
        metrics: f.metrics || null
    };
}

function projectChoice() {
    const value = $('stackProject').value;
    if (!value) return null;
    if (value === 'new') return { name: $('stackName').value.trim() || ctx.defaultName };
    return { id: value };
}

function buildSpec() {
    const calibration = selectedCalibration();
    return {
        name: $('stackName').value.trim() || ctx.defaultName,
        target: ctx.target,
        source: ctx.source,
        project: projectChoice(),
        frames: {
            light: ctx.lights.map(frameEntry),
            dark: calibration.dark.map(frameEntry),
            flat: calibration.flat.map(frameEntry),
            darkflat: calibration.darkflat.map(frameEntry),
            bias: calibration.bias.map(frameEntry)
        },
        settings: readSettings()
    };
}

function renderEstimate() {
    const settings = readSettings();
    const { lights } = ctx;
    const sample = lights.find(f => f.width && f.height) || {};
    const half = settings.debayer === 'superpixel' && ctx.source === 'raw';
    const w = sample.width ? Math.round(sample.width / (half ? 2 : 1)) : null;
    const h = sample.height ? Math.round(sample.height / (half ? 2 : 1)) : null;
    const calibration = selectedCalibration();
    const all = [...lights, ...Object.values(calibration).flat()];
    const rawBytes = (f) => f.width && f.height ? f.width * f.height * 1.15 : 25e6;
    const download = all.reduce((sum, f) => sum + (ctx.source === 'raw' && f.raw ? rawBytes(f) : (f.bytes || 0)), 0);
    const temp = w && h ? lights.length * w * h * 3 * 2 : null;
    const cores = Math.max(1, Math.min(4, Math.floor((navigator.hardwareConcurrency || 2) / 2)));
    const perFrame = w && h ? (w * h / 20e6) * (half ? 4 : 7) : 6;
    const seconds = (all.length * perFrame) / cores + (temp ? temp / 150e6 : 0);
    const parts = [_('About {seconds} in this browser (it depends on your computer)', { seconds: Seleno.fmt.duration(Math.max(60, seconds)) })];
    if (download) parts.push(_('{size} to download', { size: Seleno.fmt.bytes(download) }));
    if (temp) parts.push(_('{temp} of temporary space', { temp: Seleno.fmt.bytes(temp) }));
    if (w && h) parts.push(_('result {w} × {h}, 32-bit', { w, h }));
    $('stackEstimate').textContent = parts.join(' · ');

    const missing = lights.filter(f => f.kind === 'jpeg' && !f.raw).length;
    const source = $('stackSource');
    source.innerHTML = '';
    if (ctx.quick) {
        source.textContent = _('Quick mode: stacks the JPEGs, so only small files are downloaded. For the best result (exact calibration), untick it to use the RAWs.');
    } else if (ctx.source === 'raw' && !missing) {
        source.textContent = _('Uses the RAW files, which are linear, so the darks and bias subtract exactly.');
    } else if (missing) {
        source.append(_('{missing} of the lights have no RAW here yet (it\'s still on the telescope), so this stacks the JPEGs. JPEGs stack fine, but calibrating them is only approximate. ', { missing }));
        source.appendChild(el('button', { className: 'ws-btn sm', type: 'button', onclick: askForRaws }, _('Ask the telescope for the RAWs')));
    } else {
        source.textContent = _('Stacks the JPEGs: they align and combine well, but calibration on JPEGs is only approximate.');
    }
}

async function askForRaws() {
    const files = ctx.lights.filter(f => f.kind === 'jpeg' && !f.raw).map(f => f.name);
    try {
        const data = await Seleno.api('/album/api/request_raws', { json: { files } });
        const ok = data.results.filter(r => r.status === 'success');
        if (ok.length) Seleno.toast(_("Asked the telescope for the RAWs. They show up as they arrive; reopen this when they're in."), 'ok', 7000);
        data.results.filter(r => r.status !== 'success').forEach(r => Seleno.toast(`${r.telescope || _('Telescope')}: ${r.message}`, 'warn', 7000));
        if (data.skipped.length) Seleno.toast(_('{skipped} photo(s) didn\'t come from a telescope upload, so there\'s nothing to ask for.', { skipped: data.skipped.length }), 'warn');
    } catch (error) {
        Seleno.toast(_('Couldn\'t ask for the RAWs: {message}', { message: error.message }), 'bad');
    }
}

function refresh() {
    renderRows();
    renderEstimate();
}

function postDownload(files, name) {
    const form = el('form', { method: 'post', action: '/album/download', style: { display: 'none' } }, [
        el('input', { type: 'hidden', name: 'files_json', value: JSON.stringify(files) }),
        el('input', { type: 'hidden', name: 'name', value: name }),
        el('input', { type: 'hidden', name: 'csrf_token', value: window.CSRF_TOKEN || '' })
    ]);
    document.body.appendChild(form);
    form.submit();
    setTimeout(() => form.remove(), 1000);
}

async function start() {
    if (!engine.available) {
        Seleno.toast(engine.reason, 'warn', 6000);
        return;
    }
    const spec = buildSpec();
    if (spec.frames.light.length < 1) {
        Seleno.toast(_('There are no lights to stack.'), 'warn');
        return;
    }
    $('stackDialog').close();
    const onSaved = ctx && ctx.onSaved;
    const controller = new AbortController();
    const job = startJob(_('Stacking {name}', { name: spec.name }), { kind: 'stack', controller });
    Seleno.toast(_('Stacking {name} in this tab. Keep it open until it finishes; it shows under Activity.', { name: spec.name }), 'ok', 6000);
    await runAndSave(spec, job, controller, onSaved);
}

// Run a stack (or lucky imaging), save the result, and tidy up
export async function runAndSave(spec, job, controller, onSaved, run = null) {
    let result = null;
    try {
        result = await (run ? run() : engine.stack(spec, {
            signal: controller.signal,
            onProgress: ({ fraction, message }) => job.update(fraction, message)
        }));
        job.finish(_('{used} of {total} frames stacked', { used: result.frames_used, total: result.frames_total }));
        const saved = await saveStackResult(result);
        if (result.project) await updateProject(result.project.id, { stack_id: saved.id }).catch(() => {});
        if (result.measured && Object.keys(result.measured).length) {
            Seleno.api('/album/api/metrics', { json: { metrics: albumMetrics(result.measured, result.report.source) } }).catch(() => {});
        }
        if (onSaved) onSaved(saved);
        Seleno.toast(result.project
            ? _('{name} is stacked and added to the project {project}, and saved under Stacked images.', { name: result.name, project: result.project.name })
            : _('{name} is stacked, and saved under Stacked images.', { name: result.name }), 'ok', 7000);
        return saved;
    } catch (error) {
        job.fail(error);
        if (error.name !== 'AbortError') Seleno.toast(_('Stacking failed: {message}', { message: error.message }), 'bad', 8000);
        return null;
    } finally {
        if (result && result.dispose) await result.dispose().catch(() => {});
    }
}

// The stacker's per-frame numbers (measured on the calibrated frames) as album metrics
function albumMetrics(measured, source) {
    const out = {};
    Object.entries(measured).forEach(([name, m]) => {
        if (!m) return;
        out[name] = {
            stars: m.stars, fwhm: m.fwhm, hfr: m.hfr, ecc: m.ecc, snr: m.snr, saturated_stars: m.saturated_stars,
            noise: m.noise, noise_pct: m.noise !== null ? Math.round(m.noise * 1e6) / 1e4 : null, source
        };
    });
    return out;
}

function wire() {
    if (wired) return;
    wired = true;
    const dialog = $('stackDialog');
    dialog.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => dialog.close()));
    $('altDialog').querySelector('[data-close]').addEventListener('click', () => $('altDialog').close());
    ['calibration_method', 'method'].forEach(key => {
        const select = dialog.querySelector(`[data-setting="${key}"]`);
        METHODS.forEach(([value, label]) => select.appendChild(el('option', { value }, label)));
    });
    dialog.addEventListener('input', () => { if (ctx) renderEstimate(); });
    dialog.addEventListener('change', () => { if (ctx) renderEstimate(); });
    $('btnStackDefaults').addEventListener('click', () => {
        try {
            localStorage.setItem(DEFAULTS_KEY, JSON.stringify(readSettings()));
            Seleno.toast(_('Saved as your default stacking settings.'), 'ok');
        } catch (error) { /* private window */ }
    });
    $('btnStackZip').addEventListener('click', () => {
        const spec = buildSpec();
        const files = Object.values(spec.frames).flat().map(f => f.name);
        postDownload(files, spec.name);
    });
    $('btnStackStart').addEventListener('click', start);
    $('stackQuickJpeg').addEventListener('change', () => {
        if (!ctx) return;
        ctx.quick = $('stackQuickJpeg').checked;
        ctx.source = ctx.quick ? 'jpeg' : ctx.rawSource;
        refresh();
    });
    $('stackProject').addEventListener('change', describeProject);
}

let projects = [];

function describeProject() {
    const note = $('stackProjectNote');
    const value = $('stackProject').value;
    const p = projects.find(x => x.id === value);
    note.classList.toggle('ws-hidden', !value);
    if (value === 'new') note.textContent = _("Starts a project with these frames. Its alignment and combining settings stay fixed from now on, so later nights line up.");
    else if (p) note.textContent = _('{frames} frames so far{exposure}, {width} × {height}. These frames are lined up with its reference and added to its totals; frames already in it are skipped. Its settings ({size}, {method}) are used.', {
        frames: p.frames_total, exposure: p.exposure_total_s ? `, ${Seleno.fmt.duration(p.exposure_total_s)}` : '', width: p.width, height: p.height,
        size: p.settings.debayer === 'superpixel' ? _('half size') : _('full size'), method: p.settings.method || 'winsorized' });
}

async function fillProjects(target) {
    const select = $('stackProject');
    [...select.querySelectorAll('option[data-project]')].forEach(o => o.remove());
    select.value = '';
    try {
        projects = await listProjects();
    } catch (error) {
        projects = [];
    }
    projects.forEach(p => select.appendChild(el('option', { value: p.id, 'data-project': '1' }, `${p.name}${p.target ? ` (${p.target})` : ''}`)));
    // A project for the same target is the likely choice, but adding is never automatic
    describeProject();
}

// Calibration frames for a set of lights, chosen as the dialog does by default (same
// night, matching exposure, ISO and camera): {dark, flat, darkflat, bias} entries
export function calibrationFor(lights, library, source = 'raw') {
    const kept = lights.filter(f => f.flag !== 'reject');
    const ref = {
        exposure: Q.mostCommon(kept.map(f => f.exposure_s)),
        iso: Q.mostCommon(kept.map(f => f.iso)),
        camera: Q.mostCommon(kept.map(f => f.camera)),
        night: Q.mostCommon(kept.map(Q.nightKey)) || 'unknown'
    };
    const saved = ctx;
    ctx = { source };
    const out = {};
    ['dark', 'flat', 'darkflat', 'bias'].forEach(type => {
        const group = candidates(type, library, ref)[0];
        out[type] = group && group.night === ref.night ? group.frames.map(frameEntry) : [];
    });
    out.light = kept.map(frameEntry);
    ctx = saved;
    return out;
}

// lights: the frames to stack (non-lights are ignored); library: every frame, for
// finding calibration frames
export function openStackDialog({ lights, library, title, onSaved }) {
    wire();
    const usable = lights.filter(Q.isLightLike);
    const rejected = usable.filter(f => f.flag === 'reject').length;
    const kept = usable.filter(f => f.flag !== 'reject').sort((a, b) => Q.frameTime(a) - Q.frameTime(b));
    const quality = Q.evaluate(usable);
    const ref = {
        exposure: Q.mostCommon(kept.map(f => f.exposure_s)),
        iso: Q.mostCommon(kept.map(f => f.iso)),
        camera: Q.mostCommon(kept.map(f => f.camera)),
        night: Q.mostCommon(kept.map(Q.nightKey)) || 'unknown'
    };
    const target = Q.mostCommon(kept.map(f => f.target)) || '';
    const options = {};
    const choice = {};
    ['dark', 'flat', 'darkflat', 'bias'].forEach(type => {
        options[type] = candidates(type, library, ref);
        choice[type] = options[type].length && options[type][0].night === ref.night ? options[type][0] : null;
    });
    const hasRaw = kept.length && kept.every(f => f.raw || f.kind === 'raw');
    const weak = engine.available && engine.machine().weak;
    const defaultName = `${target || _('Stack')} ${ref.night !== 'unknown' ? Q.nightLabel(ref.night).replace(/^Tonight, /, '') : ''}`.trim();
    ctx = {
        lights: kept, rejected, bad: kept.filter(f => (quality.get(f.name) || {}).level === 'bad').length,
        ref, target, options, choice, defaultName, onSaved,
        source: hasRaw ? 'raw' : 'jpeg',
        rawSource: hasRaw ? 'raw' : 'jpeg',
        quick: false,
        settings: storedDefaults()
    };
    // Half size by default on phones and machines with little memory (Basic mode)
    if (weak && Seleno.mode() === 'basic' && !localStorage.getItem(DEFAULTS_KEY)) ctx.settings.debayer = 'superpixel';
    $('stackQuickJpegRow').classList.toggle('ws-hidden', !(hasRaw && kept.every(f => f.kind === 'jpeg')));
    $('stackQuickJpeg').checked = false;
    fillProjects(target);
    $('stackTitle').textContent = title || _('Stack {defaultName}', { defaultName });
    $('stackName').value = defaultName;

    const reference = document.querySelector('#stackDialog [data-setting="reference"]');
    reference.innerHTML = '';
    reference.appendChild(el('option', { value: 'auto' }, _('Best (sharpest, roundest)')));
    kept.forEach(f => reference.appendChild(el('option', { value: f.name },
        f.name + (f.metrics && f.metrics.fwhm ? _(' (FWHM {fwhm})', { fwhm: Number(f.metrics.fwhm).toFixed(2) }) : ''))));
    if (!kept.some(f => f.name === ctx.settings.reference)) ctx.settings.reference = 'auto';
    writeSettings(ctx.settings);

    const note = $('stackFootNote');
    note.textContent = engine.available
        ? _('{note} It shows under Activity.', { note: engine.note })
        : _('{reason} Your choices here are ready for it. Meanwhile "Download the frames" gets them all in one zip for Siril or DeepSkyStacker.', { reason: engine.reason });
    $('btnStackStart').disabled = !engine.available || !kept.length;
    refresh();
    $('stackDialog').showModal();
}
