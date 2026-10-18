// Editing a stack: the develop settings (app/processing/develop.py's, so the engine can
// render them), Basic and Advanced controls, undo and redo, the stack's report, and
// saving. Rendering needs the engine; without it the editor shows the automatic
// result the stack was saved with, and saving and downloading still work.

import { engine } from './engine.js';
import { loadStack, updateStack } from './stacks.js';

const $ = (id) => document.getElementById(id);
const el = (...args) => Seleno.el(...args);

export const DEVELOP_DEFAULTS = {
    crop: { x0: 0, y0: 0, x1: 1, y1: 1 },
    background: { enabled: false, degree: 2, grid: 16, mode: 'subtract' },
    colour: { neutralise: true, white: 'stars', manual: [1, 1, 1] },
    stretch: { mode: 'auto', linked: true, target: 0.2, shadows: -2.8, black: 0, mid: 0.5, white: 1, asinh: 30 },
    contrast: 0,
    saturation: 1,
    noise: { chroma: 0, luma: 0 },
    sharpen: { layers: [1, 1, 1, 1] }
};
export const DEVELOP_AUTO = {
    background: { enabled: true, degree: 2 },
    colour: { neutralise: true, white: 'stars' },
    stretch: { mode: 'auto', linked: true, target: 0.2, shadows: -2.8 },
    contrast: 0.15,
    saturation: 1.3,
    noise: { chroma: 0.5, luma: 0.2 },
    sharpen: { layers: [1, 1, 1, 1] }
};
export const DEVELOP_AUTO_PLANETARY = {
    background: { enabled: false },
    colour: { neutralise: false, white: 'average' },
    stretch: { mode: 'none' },
    contrast: 0.1,
    saturation: 1.1,
    noise: { chroma: 0.2, luma: 0 },
    sharpen: { layers: [1.6, 1.4, 1.15, 1] }
};

let stack = null;
let session = null;
let params = null;
let history = [];
let future = [];
let saveTimer = 0;
let wired = false;

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

function merge(base, extra) {
    const out = clone(base);
    Object.entries(extra || {}).forEach(([key, value]) => {
        if (!(key in out)) return;
        if (out[key] && typeof out[key] === 'object' && !Array.isArray(out[key]) && value && typeof value === 'object' && !Array.isArray(value)) {
            out[key] = merge(out[key], value);
        } else {
            out[key] = clone(value);
        }
    });
    return out;
}

function autoParams() {
    const p = merge(DEVELOP_DEFAULTS, stack && stack.kind === 'planetary' ? DEVELOP_AUTO_PLANETARY : DEVELOP_AUTO);
    const crop = stack && stack.report && stack.report.crop;
    if (crop) p.crop = clone(crop);
    return p;
}

// Read and write settings by path: "stretch.target", "sharpen.layers.1"; plus a few
// Basic-mode sliders that set several values at once
function getParam(path) {
    if (path === 'background.divide') return params.background.mode === 'divide';
    if (path === 'basic.noise') return params.noise.chroma;
    if (path === 'basic.sharpen') return params.sharpen.layers[0];
    return path.split('.').reduce((node, key) => (node === undefined || node === null ? undefined : node[key]), params);
}

function setParam(path, value) {
    if (path === 'background.divide') {
        params.background.mode = value ? 'divide' : 'subtract';
        return;
    }
    if (path === 'basic.noise') {
        params.noise.chroma = value;
        params.noise.luma = +(value * 0.5).toFixed(3);
        return;
    }
    if (path === 'basic.sharpen') {
        params.sharpen.layers[0] = value;
        params.sharpen.layers[1] = +(1 + (value - 1) * 0.6).toFixed(3);
        return;
    }
    const keys = path.split('.');
    let node = params;
    keys.slice(0, -1).forEach(key => { node = node[key]; });
    node[keys[keys.length - 1]] = value;
}

function formatValue(path, value) {
    if (value === undefined || value === null) return '';
    if (path === 'stretch.shadows') return `${Number(value).toFixed(1)}σ`;
    if (path === 'contrast') return `${value >= 0 ? '+' : ''}${Math.round(value * 100)}`;
    if (path === 'background.grid') return String(value);
    if (path === 'stretch.asinh') return String(Math.round(value));
    return Number(value).toFixed(2);
}

function syncControls() {
    document.querySelectorAll('#editorControls [data-param]').forEach(input => {
        const value = getParam(input.dataset.param);
        if (input.type === 'checkbox') input.checked = !!value;
        else if (value !== undefined) input.value = String(value);
        const out = input.parentElement.querySelector('output');
        if (out) out.textContent = formatValue(input.dataset.param, value);
    });
    const crop = params.crop;
    $('editCropCommon').checked = !(crop.x0 === 0 && crop.y0 === 0 && crop.x1 === 1 && crop.y1 === 1);
    $('btnUndo').disabled = !history.length;
    $('btnRedo').disabled = !future.length;
}

function changed(record = true) {
    if (record) future = [];
    syncControls();
    if (session) session.render(params);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
        updateStack(stack.id, { edit: params }).catch(error => { $('editorSaveStatus').textContent = `Couldn't save the settings: ${error.message}`; });
    }, 1500);
}

function pushHistory() {
    history.push(clone(params));
    if (history.length > 100) history.shift();
}

function stackInfo(s) {
    const report = s.report || {};
    const masters = report.masters || {};
    const parts = [];
    if (s.frames_used) {
        const exposure = report.frames && report.frames.length ? report.frames.find(f => f.accepted && f.exposure_s) : null;
        parts.push(`${s.frames_used}${exposure ? ' × ' + Seleno.fmt.exposure(exposure.exposure_s) : ' frames'}${s.total_exposure_s ? ` (${Seleno.fmt.duration(s.total_exposure_s)})` : ''}`);
    }
    ['dark', 'bias', 'flat', 'darkflat'].forEach(type => {
        if (masters[type] && masters[type].frames) parts.push(`${masters[type].frames} ${type === 'darkflat' ? 'dark flats' : type === 'bias' ? 'bias' : type + 's'}`);
    });
    if (s.width) parts.push(`${s.width} × ${s.height}`);
    parts.push(`stacked ${Seleno.fmt.dateTime(s.created_at)}`);
    return parts.join(' · ');
}

function renderReport(s) {
    const box = $('editorReport');
    box.innerHTML = '';
    const report = s.report || {};
    box.appendChild(el('h3', {}, 'How it was stacked'));
    const kv = el('dl', { className: 'ws-kv' });
    const add = (k, v) => { if (v !== undefined && v !== null && v !== '') kv.append(el('dt', {}, k), el('dd', {}, v)); };
    add('Frames used', s.frames_used ? `${s.frames_used} of ${s.frames_total || s.frames_used}` : '');
    add('Reference', report.reference);
    const settings = s.settings || {};
    add('Combine', settings.method);
    add('Debayer', settings.debayer);
    if (report.stats) {
        add('Took', report.stats.seconds ? Seleno.fmt.duration(report.stats.seconds) : '');
        add('Rejected pixels', report.stats.rejected_low_pct !== undefined ? `${report.stats.rejected_low_pct}% low · ${report.stats.rejected_high_pct}% high` : '');
    }
    if (report.masters && report.masters.hot_pixels !== undefined) add('Hot pixels fixed', String(report.masters.hot_pixels));
    box.appendChild(kv);
    const left = (report.frames || []).filter(f => !f.accepted);
    if (left.length) {
        box.appendChild(el('div', { className: 'ws-small ws-strong', style: { marginTop: '8px' } }, `Left out (${left.length})`));
        left.slice(0, 50).forEach(f => box.appendChild(el('div', { className: 'ws-row ws-small' }, [el('span', {}, f.name), el('span', {}, f.reason || '')])));
    }
    (report.warnings || []).forEach(w => box.appendChild(el('div', { className: 'ws-note warn', style: { marginTop: '6px' } }, w)));
    box.classList.toggle('ws-hidden', !kv.childNodes.length && !left.length);
}

async function saveJpegToAlbum() {
    const status = $('editorSaveStatus');
    status.textContent = 'Saving…';
    try {
        let blob;
        if (session) blob = await session.export(params, 'jpeg');
        else blob = await (await fetch(stack.auto_url)).blob();
        const form = new FormData();
        form.append('file', blob, `${stack.name}.jpg`);
        form.append('format', 'jpg');
        form.append('name', stack.name);
        if (stack.target) form.append('target', stack.target);
        const data = await Seleno.api('/album/api/save_image', { method: 'POST', body: form });
        status.textContent = `Saved to the album as ${data.name}.`;
        Seleno.toast(`Saved ${data.name} to the album.`, 'ok');
    } catch (error) {
        status.textContent = `Couldn't save: ${error.message}`;
    }
}

async function exportAs(format) {
    if (!session) {
        Seleno.toast(`${format.toUpperCase()} export: ${engine.reason}`, 'warn', 6500);
        return;
    }
    const blob = await session.export(params, format);
    const a = el('a', { href: URL.createObjectURL(blob), download: `${stack.name}.${format === 'tiff' ? 'tif' : format}` });
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
}

function wire() {
    if (wired) return;
    wired = true;
    document.querySelectorAll('#editorControls [data-param]').forEach(input => {
        const apply = (record) => {
            if (!params) return;
            if (record) pushHistory();
            let value;
            if (input.type === 'checkbox') value = input.checked;
            else if (input.tagName === 'SELECT') value = /^\d+$/.test(input.value) ? Number(input.value) : input.value;
            else value = Number(input.value);
            setParam(input.dataset.param, value);
            changed(record);
        };
        // A drag is one undo step: history on the first input, live render on each
        let dragging = false;
        input.addEventListener('input', () => { apply(!dragging); dragging = input.type === 'range'; });
        input.addEventListener('change', () => { dragging = false; if (input.type !== 'range') return; syncControls(); });
    });
    $('editCropCommon').addEventListener('change', () => {
        pushHistory();
        const crop = stack.report && stack.report.crop;
        params.crop = $('editCropCommon').checked && crop ? clone(crop) : { x0: 0, y0: 0, x1: 1, y1: 1 };
        changed();
    });
    $('btnEditAuto').addEventListener('click', () => {
        pushHistory();
        params = session && session.auto ? session.auto() : autoParams();
        changed();
    });
    $('btnEditReset').addEventListener('click', () => {
        pushHistory();
        params = merge(DEVELOP_DEFAULTS, {});
        changed();
    });
    $('btnUndo').addEventListener('click', () => {
        if (!history.length) return;
        future.push(clone(params));
        params = history.pop();
        changed(false);
    });
    $('btnRedo').addEventListener('click', () => {
        if (!future.length) return;
        history.push(clone(params));
        params = future.pop();
        changed(false);
    });
    $('btnSaveJpegToAlbum').addEventListener('click', saveJpegToAlbum);
    $('btnExportTiff').addEventListener('click', () => exportAs('tiff'));
    $('btnExportPng').addEventListener('click', () => exportAs('png'));
    document.querySelectorAll('#editorZoomSeg button').forEach(btn => btn.addEventListener('click', () => {
        document.querySelectorAll('#editorZoomSeg button').forEach(b => b.classList.toggle('on', b === btn));
        const img = $('editorImg');
        const stage = $('editorStage');
        const actual = btn.dataset.zoom === '100';
        img.style.maxWidth = actual ? 'none' : '';
        img.style.maxHeight = actual ? 'none' : '';
        stage.style.overflow = actual ? 'auto' : 'hidden';
        stage.style.alignItems = actual ? 'flex-start' : 'center';
        stage.style.justifyContent = actual ? 'flex-start' : 'center';
    }));
    document.querySelectorAll('#compareSeg button').forEach(btn => btn.addEventListener('click', () => {
        if (!session) return;
        document.querySelectorAll('#compareSeg button').forEach(b => b.classList.toggle('on', b === btn));
        session.compare(btn.dataset.compare);
    }));
    $('showBgModel').addEventListener('change', (e) => { if (session && session.showBackground) session.showBackground(e.target.checked); });
    document.addEventListener('keydown', (e) => {
        if ($('viewEditor').hidden || !(e.ctrlKey || e.metaKey) || e.target.closest('input, textarea')) return;
        if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); $('btnUndo').click(); }
        if (e.key === 'y' || (e.key === 'z' && e.shiftKey)) { e.preventDefault(); $('btnRedo').click(); }
    });
}

export async function openEditor(stackId) {
    wire();
    if (session) {
        session.close();
        session = null;
    }
    stack = await loadStack(stackId);
    params = stack.edit ? merge(DEVELOP_DEFAULTS, stack.edit) : autoParams();
    history = [];
    future = [];
    $('editorName').textContent = stack.name;
    $('editorInfo').textContent = stackInfo(stack);
    const img = $('editorImg');
    img.src = stack.auto_url;
    img.alt = stack.name;
    renderReport(stack);
    $('btnDownloadMaster').href = stack.has_master ? `${stack.master_url}?download=1` : '#';
    $('btnDownloadMaster').classList.toggle('ws-hidden', !stack.has_master);
    $('btnDownloadAuto').href = `${stack.auto_url}?download=1`;
    $('editorSaveStatus').textContent = '';

    const note = $('editorEngineNote');
    if (engine.available && stack.linear) {
        session = await engine.develop(stack, $('editorStage'));
        img.classList.add('ws-hidden');
        session.render(params);
    }
    const live = !!session;
    $('editorControls').disabled = !live;
    $('btnEditAuto').disabled = !live;
    $('btnEditReset').disabled = !live;
    document.querySelectorAll('#compareSeg button').forEach(b => { b.disabled = !live; });
    note.classList.toggle('ws-hidden', live);
    note.textContent = stack.linear
        ? `Showing the automatic result. Adjusting it needs the in-browser processing, which isn't built yet; saving and downloading work now.`
        : 'Showing the result as it was saved.';
    img.classList.toggle('ws-hidden', live);
    syncControls();
}
