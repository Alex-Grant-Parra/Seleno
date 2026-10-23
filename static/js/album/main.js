// The album: photos grouped by night, target or plan; a Basic view (the lights of a
// night with quality dots, calibration frames folded away) and an Advanced one
// (quality chart, type filters, grid or table); the inspector; bulk actions; and the
// Stacked images, Activity and editor views.

import { engine, requireEngine } from './engine.js';
import * as Q from './quality.js';
import { openViewer } from './viewer.js';
import { openStackDialog } from './stack-dialog.js';
import { loadStacks, renderStackCards, deleteStack, updateStack } from './stacks.js';
import { onJobsChange, renderJobs, runningCount, startJob } from './jobs.js';
import { openEditor, openPhotoEditor } from './editor.js';
import { openLuckyDialog } from './lucky-dialog.js';
import { renderProjects } from './projects-view.js';

const $ = (id) => document.getElementById(id);
const el = (...args) => Seleno.el(...args);

const PREFS_KEY = 'seleno_album_prefs';

const state = {
    frames: [],
    byName: new Map(),
    plans: {},
    stacks: [],
    view: 'photos',
    groupBy: 'night',
    groupKey: null,
    hiddenTypes: new Set(),       // Advanced: types switched off with their chips
    hideRejected: false,
    layout: 'grid',
    selection: new Set(),
    anchor: null,
    current: null,
    sort: { key: 'time', dir: 1 },
    expanded: new Set(),          // Basic: calibration rows opened
    quality: new Map(),
    visible: []                   // frames in display order, for keys and ranges
};

let histogram = null;
let metaDirty = false;
let inspected = null;             // the frame the target and notes fields belong to

function loadPrefs() {
    try {
        const p = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
        if (['night', 'target', 'plan'].includes(p.groupBy)) state.groupBy = p.groupBy;
        if (['grid', 'table'].includes(p.layout)) state.layout = p.layout;
        state.hideRejected = !!p.hideRejected;
        if (Array.isArray(p.hiddenTypes)) state.hiddenTypes = new Set(p.hiddenTypes);
    } catch (error) { /* ignore */ }
}

function savePrefs() {
    try {
        localStorage.setItem(PREFS_KEY, JSON.stringify({
            groupBy: state.groupBy, layout: state.layout, hideRejected: state.hideRejected,
            hiddenTypes: [...state.hiddenTypes]
        }));
    } catch (error) { /* private window */ }
}

// --- loading ------------------------------------------------------------------------

async function loadLibrary() {
    $('libraryStatus').textContent = _('Loading…');
    try {
        const data = await Seleno.api('/album/api/library');
        state.frames = data.frames || [];
        state.plans = data.plans || {};
        state.byName = new Map(state.frames.map(f => [f.name, f]));
        $('libraryStatus').textContent = `${state.frames.length} ${state.frames.length === 1 ? 'photo' : 'photos'}`;
        setStackCount(data.stacks);
        for (const name of [...state.selection]) if (!state.byName.has(name)) state.selection.delete(name);
        if (state.current && !state.byName.has(state.current)) state.current = null;
    } catch (error) {
        $('libraryStatus').textContent = '';
        $('groupTitle').textContent = _("Couldn't load your photos");
        $('groupSummary').textContent = error.message;
        throw error;
    }
}

function setStackCount(n) {
    const chip = $('stackCountChip');
    chip.textContent = String(n || 0);
    chip.classList.toggle('ws-hidden', !n);
}

// --- groups -------------------------------------------------------------------------------

function groupsFor(by) {
    return Q.groupFrames(state.frames, by, state.plans);
}

function currentFrames() {
    if (!state.groupKey || state.groupKey === 'all') return state.frames;
    const [by] = state.groupKey.split(':');
    return state.frames.filter(f => Q.groupKeyOf(f, by) === state.groupKey);
}

function chooseDefaultGroup() {
    const params = new URLSearchParams(location.search);
    const frameName = params.get('frame');
    if (frameName && state.byName.has(frameName)) {
        const frame = state.byName.get(frameName);
        state.groupKey = Q.groupKeyOf(frame, 'night');
        state.current = frameName;
        return;
    }
    const night = params.get('night');
    if (night) {
        state.groupKey = `night:${night}`;
        return;
    }
    if (state.groupKey && (state.groupKey === 'all' || currentFrames().length)) return;
    const groups = groupsFor('night');
    state.groupKey = groups.length ? groups[0].key : 'all';
}

function renderNav() {
    const nav = $('groupNav');
    nav.innerHTML = '';
    const section = (label, groups, by) => {
        if (!groups.length) return;
        nav.appendChild(el('div', { className: 'ws-side-label' }, label));
        groups.forEach(g => {
            const lights = g.frames.filter(Q.isLightLike).length;
            const videos = g.frames.filter(f => Q.typeOf(f) === 'video').length;
            const count = lights || !videos ? String(lights || g.frames.length) : (videos === 1 ? _('1 video') : _('{n} videos', { n: videos }));
            nav.appendChild(el('button', {
                type: 'button',
                className: state.groupKey === g.key ? 'on' : '',
                title: _('{n} files', { n: g.frames.length }),
                onclick: () => selectGroup(g.key)
            }, [el('span', {}, g.label || '—'), el('span', { className: 'ws-count' }, count)]));
        });
    };
    if (Seleno.mode() === 'advanced') {
        const labels = { night: _('Nights'), target: _('Targets'), plan: _('Plans') };
        section(labels[state.groupBy], groupsFor(state.groupBy), state.groupBy);
    } else {
        section(_('Nights'), groupsFor('night').slice(0, 40), 'night');
        section(_('Targets'), groupsFor('target').filter(g => g.value).slice(0, 30), 'target');
    }
    nav.appendChild(el('div', { className: 'ws-side-label' }, _('Everything')));
    nav.appendChild(el('button', { type: 'button', className: state.groupKey === 'all' ? 'on' : '', onclick: () => selectGroup('all') },
        [el('span', {}, _('All photos')), el('span', { className: 'ws-count' }, String(state.frames.length))]));
}

function selectGroup(key) {
    state.groupKey = key;
    state.selection.clear();
    state.anchor = null;
    renderPhotos();
}

// --- the group's header -------------------------------------------------------------------

function groupTitle(frames) {
    const key = state.groupKey || 'all';
    const lights = frames.filter(Q.isLightLike);
    const target = Q.mostCommon(lights.map(f => f.target));
    if (key === 'all') return _('All photos');
    const [by, value] = [key.slice(0, key.indexOf(':')), key.slice(key.indexOf(':') + 1)];
    if (by === 'night') return `${target ? target + ' · ' : ''}${Q.nightLabel(value, true).replace(/^t/, 'T')}`;
    if (by === 'target') return value ? `${target || value}` : _('Photos without a target');
    const plan = state.plans[value];
    return value ? (plan ? plan.name : value) : _('Photos not from a plan');
}

function summaryLine(frames) {
    const counts = Q.countTypes(frames);
    const lights = frames.filter(Q.isLightLike);
    const light = lights.reduce((s, f) => s + (f.exposure_s || 0), 0);
    const parts = [];
    if (lights.length) {
        const count = lights.length === 1 ? _('1 photo') : _('{n} photos', { n: lights.length });
        parts.push(light > 60 ? _('{count} ({duration} of light)', { count, duration: Seleno.fmt.duration(light) }) : count);
    }
    ['dark', 'flat', 'darkflat', 'bias', 'snapshot', 'test', 'bracket', 'timelapse', 'video', 'processed', 'other'].forEach(type => {
        if (counts[type]) parts.push(`${counts[type]} ${counts[type] === 1 ? Q.TYPE_SINGULAR[type] : Q.TYPE_LABELS[type].toLowerCase()}`);
    });
    const bad = lights.filter(f => (state.quality.get(f.name) || {}).level === 'bad').length;
    if (bad) parts.push(bad === 1 ? _('1 photo looks bad') : _('{n} photos look bad', { n: bad }));
    const unmeasured = lights.filter(f => !f.metrics).length;
    if (lights.length && unmeasured === lights.length && Seleno.mode() === 'advanced') parts.push(_('not measured yet'));
    return parts.join(' · ') || _('Nothing here');
}

function renderHead(frames) {
    $('groupTitle').textContent = groupTitle(frames);
    $('groupSummary').textContent = summaryLine(frames);
    const lights = frames.filter(Q.isLightLike);
    const stack = $('btnStackGroup');
    stack.disabled = lights.length < 1;
    stack.textContent = (state.groupKey || '').startsWith('night:') ? _('Stack this night') : _('Stack these photos');
    $('btnDownloadGroup').disabled = !frames.length;
    const zip = $('groupPlanZip');
    const isPlan = (state.groupKey || '').startsWith('plan:') && state.groupKey.length > 5;
    zip.classList.toggle('ws-hidden', !isPlan || Seleno.mode() !== 'advanced');
    if (isPlan) zip.href = `/album/plan/${encodeURIComponent(state.groupKey.slice(5))}/download`;
}

// --- tiles and table --------------------------------------------------------------------

function qualityOf(frame) {
    return state.quality.get(frame.name) || { level: 'off', reason: '' };
}

function tile(frame) {
    const q = qualityOf(frame);
    const classes = ['ws-tile'];
    if (state.selection.has(frame.name)) classes.push('selected');
    if (state.current === frame.name) classes.push('current');
    if (frame.flag === 'reject') classes.push('rejected');
    const picture = frame.kind === 'video'
        ? el('div', { className: 'ws-tile-blank' }, _('Video'))
        : el('img', { src: frame.thumb, alt: frame.name, loading: 'lazy', decoding: 'async' });
    if (picture.tagName === 'IMG') picture.addEventListener('error', () => picture.replaceWith(el('div', { className: 'ws-tile-blank' }, frame.kind === 'raw' ? 'RAW' : _('No preview'))));
    const flagText = frame.flag === 'reject' ? _('Rejected') : frame.flag === 'pick' ? _('Accepted') : q.level === 'bad' ? q.reason : '';
    return el('button', {
        type: 'button',
        className: classes.join(' '),
        dataset: { name: frame.name },
        title: `${frame.name}${frame.captured_at ? ' · ' + Seleno.fmt.dateTime(frame.captured_at) : ''}${q.reason ? ' · ' + q.reason : ''}`,
        'aria-pressed': state.selection.has(frame.name) ? 'true' : 'false'
    }, [
        picture,
        flagText ? el('span', { className: `ws-flag${frame.flag === 'pick' ? ' pick' : ''}` }, flagText) : null,
        frame.raw || frame.kind === 'raw' ? el('span', { className: 'ws-badge-raw' }, 'RAW') : null,
        el('span', { className: 'ws-cap' }, [el('span', {}, frame.name.replace(/\.[^.]+$/, '')), el('span', { className: `ws-dot ${q.level}`, title: q.reason || '' })])
    ]);
}

function grid(frames) {
    const box = el('div', { className: 'ws-tiles' });
    frames.forEach(f => box.appendChild(tile(f)));
    return box;
}

const COLUMNS = [
    { key: 'name', label: _('File'), value: f => f.name },
    { key: 'time', label: _('Time'), value: f => Q.frameTime(f), show: f => f.captured_at ? Seleno.fmt.time(f.captured_at) : '—' },
    { key: 'type', label: _('Type'), value: f => Q.typeOf(f), show: f => Q.typeOf(f) === 'unknown' ? '—' : Q.typeOf(f) },
    { key: 'exp', label: _('Exp'), value: f => f.exposure_s || 0, show: f => Seleno.fmt.exposure(f.exposure_s) },
    { key: 'iso', label: 'ISO', value: f => parseFloat(f.iso) || 0, show: f => f.iso || '—' },
    { key: 'stars', label: _('Stars'), metric: 'stars', digits: 0 },
    { key: 'fwhm', label: 'FWHM', metric: 'fwhm', digits: 2 },
    { key: 'hfr', label: 'HFR', metric: 'hfr', digits: 2 },
    { key: 'ecc', label: _('Ecc'), metric: 'ecc', digits: 2 },
    { key: 'bg', label: _('Bkg'), metric: 'background_pct', digits: 1, suffix: '%' },
    { key: 'noise', label: _('Noise'), metric: 'noise_pct', digits: 2, suffix: '%' },
    { key: 'snr', label: 'SNR', metric: 'snr', digits: 1 },
    { key: 'flag', label: _('Flag'), value: f => ({ ok: 3, warn: 2, bad: 1, off: 0 })[qualityOf(f).level] }
];

function columnValue(col, f) {
    if (col.metric) {
        const v = f.metrics ? f.metrics[col.metric] : null;
        return v === null || v === undefined || !isFinite(v) ? null : Number(v);
    }
    return col.value(f);
}

// Frames without a value (unmeasured darks, say) go last whichever way it's sorted
function sortFrames(frames) {
    const col = COLUMNS.find(c => c.key === state.sort.key) || COLUMNS[1];
    const dir = state.sort.dir;
    return frames.slice().sort((a, b) => {
        const va = columnValue(col, a), vb = columnValue(col, b);
        if (va === null || vb === null) return (va === null) - (vb === null);
        if (typeof va === 'string') return dir * va.localeCompare(vb);
        return dir * (va - vb);
    });
}

function table(frames) {
    const head = el('tr', {}, [
        el('th', {}, el('input', { type: 'checkbox', 'aria-label': _('Select all'), onclick: (e) => {
            e.stopPropagation();
            frames.forEach(f => (e.target.checked ? state.selection.add(f.name) : state.selection.delete(f.name)));
            renderContent();
            renderBulk();
        } })),
        el('th', {}, ''),
        ...COLUMNS.map(col => el('th', {
            onclick: () => {
                state.sort = { key: col.key, dir: state.sort.key === col.key ? -state.sort.dir : 1 };
                renderContent();
            },
            'aria-sort': state.sort.key === col.key ? (state.sort.dir > 0 ? 'ascending' : 'descending') : 'none'
        }, `${col.label}${state.sort.key === col.key ? (state.sort.dir > 0 ? ' ▲' : ' ▼') : ''}`))
    ]);
    head.firstChild.firstChild.checked = frames.length > 0 && frames.every(f => state.selection.has(f.name));
    const body = el('tbody');
    frames.forEach(f => {
        const q = qualityOf(f);
        const classes = [];
        if (state.current === f.name) classes.push('current');
        if (state.selection.has(f.name)) classes.push('selected');
        if (f.flag === 'reject' || q.level === 'bad') classes.push('rejected');
        const row = el('tr', { className: classes.join(' '), dataset: { name: f.name } }, [
            el('td', {}, el('input', { type: 'checkbox', checked: state.selection.has(f.name), 'aria-label': _('Select {name}', { name: f.name }), onclick: (e) => {
                e.stopPropagation();
                toggleSelection(f.name);
            } })),
            el('td', {}, f.kind === 'video' ? '▶' : el('img', { className: 'ws-mini', src: f.thumb, alt: '', loading: 'lazy' })),
            ...COLUMNS.map(col => {
                if (col.key === 'name') return el('td', {}, [f.name, f.raw || f.kind === 'raw' ? el('span', { className: 'ws-muted ws-small' }, ' RAW') : null]);
                if (col.key === 'flag') return el('td', { title: q.reason || '' }, el('span', { className: `ws-dot ${q.level}` }));
                if (col.metric) {
                    const v = f.metrics ? f.metrics[col.metric] : null;
                    return el('td', {}, v === null || v === undefined ? '—' : `${Number(v).toFixed(col.digits)}${col.suffix || ''}`);
                }
                return el('td', {}, col.show ? col.show(f) : String(col.value(f)));
            })
        ]);
        body.appendChild(row);
    });
    return el('div', { className: 'ws-card flush ws-table-wrap' }, el('table', { className: 'ws-table' }, [el('thead', {}, head), body]));
}

function calibrationRow(type, frames) {
    const first = frames[0];
    const exposure = Q.mostCommon(frames.map(f => f.exposure_s));
    const iso = Q.mostCommon(frames.map(f => f.iso));
    const open = state.expanded.has(type);
    const details = [exposure ? Seleno.fmt.exposure(exposure) : null, iso ? _('ISO {iso}', { iso }) : null,
        type === 'dark' || type === 'bias' || type === 'darkflat' ? 'cover on' : null,
        type === 'dark' ? _('match your photos') : null].filter(Boolean).join(' · ');
    const wrap = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } });
    wrap.appendChild(el('div', { className: 'ws-grouprow' }, [
        el('img', { className: 'ws-thumb', src: first.thumb, alt: '', loading: 'lazy' }),
        el('div', { style: { flex: 1 } }, [el('b', {}, `${frames.length} ${frames.length === 1 ? Q.TYPE_SINGULAR[type] : Q.TYPE_LABELS[type].toLowerCase()}`), ' ',
            el('span', { className: 'ws-muted ws-small' }, details)]),
        el('button', { className: 'ws-btn sm', type: 'button', onclick: () => {
            if (open) state.expanded.delete(type); else state.expanded.add(type);
            renderContent();
        } }, open ? _('Hide') : _('Show'))
    ]));
    if (open) wrap.appendChild(grid(frames));
    return wrap;
}

function renderBasic(frames) {
    const box = $('groupContent');
    const lights = frames.filter(Q.isLightLike).sort((a, b) => Q.frameTime(a) - Q.frameTime(b));
    const ordered = [];
    if (lights.length) {
        const target = Q.mostCommon(lights.map(f => f.target));
        const exposure = Q.mostCommon(lights.map(f => f.exposure_s));
        const iso = Q.mostCommon(lights.map(f => f.iso));
        box.appendChild(el('div', { className: 'ws-actions', style: { justifyContent: 'space-between', alignItems: 'baseline' } }, [
            el('h2', { className: 'ws-section-title' }, [target ? _('Photos of {target}', { target }) : _('Photos'), ' ',
                el('span', { className: 'ws-muted ws-small', style: { fontWeight: 500 } }, [exposure ? Seleno.fmt.exposure(exposure) : null, iso ? _('ISO {iso}', { iso }) : null].filter(Boolean).join(' · '))]),
            el('span', { className: 'ws-small ws-muted' }, [el('span', { className: 'ws-dot ok' }), ' good ', el('span', { className: 'ws-dot warn', style: { marginLeft: '8px' } }), ' so-so ',
                el('span', { className: 'ws-dot bad', style: { marginLeft: '8px' } }), ' bad ', el('span', { className: 'ws-dot off', style: { marginLeft: '8px' } }), ' not measured'])
        ]));
        box.appendChild(grid(lights));
        ordered.push(...lights);
    }
    Q.CALIBRATION.forEach(type => {
        const list = frames.filter(f => Q.typeOf(f) === type).sort((a, b) => Q.frameTime(a) - Q.frameTime(b));
        if (!list.length) return;
        box.appendChild(calibrationRow(type, list));
        if (state.expanded.has(type)) ordered.push(...list);
    });
    ['bracket', 'timelapse', 'snapshot', 'test', 'video', 'processed', 'other'].forEach(type => {
        const list = frames.filter(f => Q.typeOf(f) === type).sort((a, b) => Q.frameTime(a) - Q.frameTime(b));
        if (!list.length) return;
        box.appendChild(calibrationRow(type, list));
        if (state.expanded.has(type)) ordered.push(...list);
    });
    if (!frames.length) box.appendChild(el('div', { className: 'ws-card ws-empty' }, _('No photos here yet. They appear as the telescope uploads them.')));
    state.visible = ordered;
}

function renderTypeChips(frames) {
    const box = $('typeChips');
    box.innerHTML = '';
    const counts = Q.countTypes(frames);
    Q.TYPE_ORDER.filter(t => counts[t]).forEach(type => {
        const on = !state.hiddenTypes.has(type);
        box.appendChild(el('button', {
            type: 'button', className: `ws-chip${on ? ' on' : ''}`, 'aria-pressed': on ? 'true' : 'false',
            onclick: () => {
                if (state.hiddenTypes.has(type)) state.hiddenTypes.delete(type); else state.hiddenTypes.add(type);
                savePrefs();
                renderContent();
                renderTypeChips(frames);
            }
        }, [Q.TYPE_LABELS[type], ' ', el('span', { className: 'ws-count' }, String(counts[type]))]));
    });
    $('hideRejectedChip').classList.toggle('on', state.hideRejected);
    $('hideRejectedChip').setAttribute('aria-pressed', state.hideRejected ? 'true' : 'false');
}

function renderQuality(frames) {
    const lights = frames.filter(Q.isLightLike);
    const measured = lights.filter(f => f.metrics);
    const svg = $('qualityChart');
    const empty = $('qualityEmpty');
    $('qualityTitle').textContent = measured.length ? _('· {measured} of {lights} lights measured', { measured: measured.length, lights: lights.length }) : '';
    const limits = readLimits();
    const drawn = Q.drawQualityChart(svg, frames, limits);
    svg.classList.toggle('ws-hidden', drawn < 2);
    $('qualityControls').classList.toggle('ws-hidden', !measured.length);
    empty.classList.toggle('ws-hidden', drawn >= 2);
    if (drawn < 2) {
        empty.textContent = lights.length
            ? (engine.available ? _('Select photos and choose Measure quality to chart their star sizes and roundness.')
                : _('Star size, roundness and sky level show here once the photos are measured. {reason}', { reason: engine.reason }))
            : _('No lights in this group.');
    }
    const over = Q.overLimits(frames, limits);
    $('btnRejectByLimits').textContent = over.length === 1 ? _('Reject 1 frame') : _('Reject {n} frames', { n: over.length });
    $('btnRejectByLimits').disabled = !over.length;
}

function readLimits() {
    const num = (id) => {
        const v = parseFloat($(id).value);
        return isNaN(v) ? null : v;
    };
    return { fwhm: num('limitFwhm'), ecc: num('limitEcc'), stars: num('limitStars') };
}

function suggestLimits() {
    const limits = Q.suggestLimits(currentFrames());
    if (!limits) return;
    $('limitFwhm').value = limits.fwhm ?? '';
    $('limitEcc').value = limits.ecc ?? '';
    $('limitStars').value = limits.stars ?? '';
    renderQuality(currentFrames());
}

function renderAdvanced(frames) {
    const box = $('groupContent');
    let shown = frames.filter(f => !state.hiddenTypes.has(Q.typeOf(f)));
    if (state.hideRejected) shown = shown.filter(f => f.flag !== 'reject' && qualityOf(f).level !== 'bad');
    shown = sortFrames(shown);
    state.visible = shown;
    if (!shown.length) {
        box.appendChild(el('div', { className: 'ws-card ws-empty' }, frames.length ? _('Nothing matches the filters.') : _('No photos here yet.')));
        return;
    }
    box.appendChild(state.layout === 'table' ? table(shown) : grid(shown));
}

function renderContent() {
    const frames = currentFrames();
    state.quality = Q.evaluate(frames);
    const box = $('groupContent');
    box.innerHTML = '';
    box.style.display = 'flex';
    box.style.flexDirection = 'column';
    box.style.gap = 'var(--ws-gap)';
    if (Seleno.mode() === 'advanced') {
        renderAdvanced(frames);
        renderQuality(frames);
    } else {
        renderBasic(frames);
    }
    renderHead(frames);
}

function renderPhotos() {
    const frames = currentFrames();
    renderNav();
    renderTypeChips(frames);
    document.querySelectorAll('#groupBySeg button').forEach(b => b.classList.toggle('on', b.dataset.groupBy === state.groupBy));
    document.querySelectorAll('#layoutSeg button').forEach(b => b.classList.toggle('on', b.dataset.layout === state.layout));
    if (!$('limitFwhm').value) {
        const limits = Q.suggestLimits(frames);
        if (limits) {
            $('limitFwhm').value = limits.fwhm ?? '';
            $('limitEcc').value = limits.ecc ?? '';
            $('limitStars').value = limits.stars ?? '';
        }
    }
    renderContent();
    renderInspector();
    renderBulk();
}

// --- selection --------------------------------------------------------------------------

function toggleSelection(name) {
    if (state.selection.has(name)) state.selection.delete(name); else state.selection.add(name);
    state.anchor = name;
    refreshSelectionMarks();
    renderBulk();
}

function refreshSelectionMarks() {
    document.querySelectorAll('#groupContent [data-name]').forEach(node => {
        const name = node.dataset.name;
        node.classList.toggle('selected', state.selection.has(name));
        node.classList.toggle('current', state.current === name);
        if (node.tagName === 'TR') {
            const box = node.querySelector('input[type=checkbox]');
            if (box) box.checked = state.selection.has(name);
        } else {
            node.setAttribute('aria-pressed', state.selection.has(name) ? 'true' : 'false');
        }
    });
}

function onFrameClick(event) {
    const node = event.target.closest('[data-name]');
    if (!node || !$('groupContent').contains(node)) return;
    const name = node.dataset.name;
    if (event.shiftKey && state.anchor) {
        const names = state.visible.map(f => f.name);
        const a = names.indexOf(state.anchor), b = names.indexOf(name);
        if (a >= 0 && b >= 0) {
            names.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(n => state.selection.add(n));
            refreshSelectionMarks();
            renderBulk();
            return;
        }
    }
    if (event.ctrlKey || event.metaKey) {
        toggleSelection(name);
        return;
    }
    setCurrent(name);
    state.anchor = name;
}

function setCurrent(name) {
    if (metaDirty && inspected && inspected !== name) saveMeta();
    state.current = name;
    refreshSelectionMarks();
    renderInspector();
    const node = document.querySelector(`#groupContent [data-name="${CSS.escape(name)}"]`);
    if (node) node.scrollIntoView({ block: 'nearest' });
}

function selectedFrames() {
    return [...state.selection].map(n => state.byName.get(n)).filter(Boolean);
}

function renderBulk() {
    const n = state.selection.size;
    $('bulkBar').classList.toggle('ws-hidden', n === 0);
    $('bulkCount').textContent = _('{n} selected', { n });
    $('btnBlink').disabled = n < 2 && currentFrames().filter(Q.isLightLike).length < 2;
}

// --- the inspector ------------------------------------------------------------------------

function initHistogram() {
    if (histogram || typeof window.HistogramPanel !== 'function') return;
    histogram = new window.HistogramPanel({
        canvasId: 'albumHistogramCanvas',
        controlsRootId: 'albumHistogramControls',
        statsId: 'albumHistogramStats',
        emptyMessage: _('Select a photo')
    });
    histogram.setSourceImage($('inspectorImg'));
}

function kvRows(box, rows) {
    box.innerHTML = '';
    rows.filter(([, v]) => v !== null && v !== undefined && v !== '').forEach(([k, v]) => box.append(el('dt', {}, k), el('dd', {}, v)));
}

function renderInspector() {
    const frame = state.current ? state.byName.get(state.current) : null;
    $('inspectorEmpty').classList.toggle('ws-hidden', !!frame);
    $('inspectorBody').classList.toggle('ws-hidden', !frame);
    if (!frame) return;
    const img = $('inspectorImg');
    if (img.dataset.name !== frame.name) {
        img.dataset.name = frame.name;
        img.src = frame.kind === 'video' ? '' : frame.preview;
    }
    $('inspectorName').textContent = frame.name;
    const q = qualityOf(frame);
    const chip = $('inspectorQuality');
    chip.querySelector('.ws-dot').className = `ws-dot ${q.level}`;
    chip.querySelector('span:last-child').textContent = q.level === 'off' ? (frame.metrics ? _('Measured') : _('Not measured'))
        : q.reason || ({ ok: _('Good'), warn: _('So-so'), bad: _('Bad') })[q.level];
    $('inspectorSub').textContent = [
        frame.captured_at ? Seleno.fmt.time(frame.captured_at) : null,
        Seleno.fmt.exposure(frame.exposure_s) !== '—' ? Seleno.fmt.exposure(frame.exposure_s) : null,
        frame.iso ? _('ISO {iso}', { iso: frame.iso }) : null,
        frame.raw ? 'RAW + JPEG' : frame.kind === 'raw' ? 'RAW' : frame.kind === 'video' ? _('Video') : 'JPEG',
        Q.typeOf(frame) !== 'unknown' ? Q.TYPE_SINGULAR[Q.typeOf(frame)] : null
    ].filter(Boolean).join(' · ');
    $('inspectorSentence').textContent = Q.describe(frame, q, currentFrames());
    $('inspectorSentence').classList.toggle('ws-hidden', !$('inspectorSentence').textContent);
    if (inspected !== frame.name || !metaDirty) {
        inspected = frame.name;
        $('inspectorTarget').value = frame.target || '';
        $('inspectorNotes').value = frame.notes || '';
        metaDirty = false;
        $('btnSaveMeta').disabled = true;
        $('metaStatus').textContent = '';
    }
    $('btnFlagPick').classList.toggle('sel', frame.flag === 'pick');
    $('btnFlagReject').classList.toggle('sel', frame.flag === 'reject');
    const typeSelect = $('inspectorType');
    typeSelect.value = frame.frame_type && Q.SETTABLE_TYPES.includes(frame.frame_type) ? frame.frame_type : '';
    $('btnEditPhoto').classList.toggle('ws-hidden', frame.kind === 'video');
    $('btnLucky').classList.toggle('ws-hidden', frame.kind !== 'video');
    $('btnEditPhoto').classList.toggle('primary', engine.available);
    $('btnViewFull').disabled = frame.kind === 'video';

    const plan = frame.plan_id ? state.plans[frame.plan_id] : null;
    kvRows($('inspectorKv'), [
        [_('Captured'), frame.captured_at ? Seleno.fmt.dateTime(frame.captured_at) : null],
        [_('Camera'), frame.camera],
        [_('Exposure'), [Seleno.fmt.exposure(frame.exposure_s) !== '—' ? Seleno.fmt.exposure(frame.exposure_s) : null, frame.iso ? _('ISO {iso}', { iso: frame.iso }) : null].filter(Boolean).join(' · ')],
        [_('Type'), Q.typeOf(frame) === 'unknown' ? _('Not set') : `${Q.typeOf(frame)}${frame.frame_type_set ? _(' (set by you)') : ''}`],
        [_('Plan'), plan ? [plan.name, frame.step !== undefined && frame.step !== null ? _(' · step {step}', { step: frame.step + 1 }) : '', frame.frame !== undefined && frame.frame !== null ? _(' · frame {frame}', { frame: frame.frame + 1 }) : ''].join('') : null],
        [_('Telescope'), frame.telescope],
        [_('Pointing'), frame.ra !== undefined && frame.ra !== null ? _('RA {ra} · Dec {dec}', { ra: Seleno.fmt.ra(frame.ra), dec: Seleno.fmt.dec(frame.dec) }) : null],
        [_('Size'), [frame.width ? `${frame.width} × ${frame.height}` : null, frame.bytes ? Seleno.fmt.bytes(frame.bytes) : null].filter(Boolean).join(' · ')],
        ['RAW', frame.raw || (frame.kind === 'raw' ? frame.name : null)]
    ]);
    const m = frame.metrics;
    $('inspectorMeasuredFrom').textContent = m ? (frame.metrics_source === 'raw' ? _('from the RAW') : _('from the JPEG')) : '';
    kvRows($('inspectorMetrics'), m ? [
        [_('Stars found'), m.stars !== undefined ? String(m.stars) : null],
        [_('FWHM / HFR'), m.fwhm !== undefined ? _('{fwhm} / {hfr} px{fwhmArcsec}', { fwhm: Seleno.fmt.num(m.fwhm), hfr: Seleno.fmt.num(m.hfr), fwhmArcsec: m.fwhm_arcsec ? ` · ${Number(m.fwhm_arcsec).toFixed(1)}″` : '' }) : null],
        [_('Eccentricity'), m.ecc !== undefined ? Seleno.fmt.num(m.ecc) : null],
        [_('Background'), m.background_pct !== undefined ? `${Seleno.fmt.num(m.background_pct, 1)}%` : null],
        [_('Noise'), m.noise_pct !== undefined ? `${Seleno.fmt.num(m.noise_pct, 2)}%` : null],
        [_('Clipped pixels'), m.clipped_pct !== undefined ? `${Seleno.fmt.num(m.clipped_pct, 2)}%` : null]
    ] : [[_('Measured'), _('Not yet')]]);
    const details = $('exifDetails');
    details.open = false;
    details.dataset.name = frame.name;
    $('exifSections').innerHTML = '';
    if (histogram) histogram.refresh();
}

async function loadExif() {
    const details = $('exifDetails');
    const frame = state.byName.get(details.dataset.name);
    const box = $('exifSections');
    if (!details.open || !frame || box.childNodes.length) return;
    if (frame.kind !== 'jpeg') {
        box.textContent = _('Only JPEGs have their details read here.');
        return;
    }
    box.textContent = _('Loading…');
    try {
        const data = await Seleno.api(`/album/photo/${encodeURIComponent(frame.name)}/metadata`);
        box.innerHTML = '';
        (data.photo.sections || []).forEach(section => {
            box.appendChild(el('h4', {}, section.title));
            const dl = el('dl', { className: 'ws-kv' });
            section.items.forEach(item => dl.append(el('dt', {}, item.label), el('dd', {}, item.value && String(item.value).trim() ? String(item.value) : '—')));
            box.appendChild(dl);
        });
    } catch (error) {
        box.textContent = _('Couldn\'t read the file: {message}', { message: error.message });
    }
}

// Save the target and notes fields to the frame they were typed for
async function saveMeta() {
    const frame = state.byName.get(inspected);
    if (!frame) return;
    const target = $('inspectorTarget').value.trim();
    const notes = $('inspectorNotes').value.trim();
    metaDirty = false;
    $('btnSaveMeta').disabled = true;
    $('metaStatus').textContent = _('Saving…');
    try {
        await setFrames([frame], { target, notes });
        if (inspected === frame.name) $('metaStatus').textContent = _('Saved');
    } catch (error) {
        Seleno.toast(_('Couldn\'t save the notes for {name}: {message}', { name: frame.name, message: error.message }), 'bad');
        if (inspected === frame.name) $('metaStatus').textContent = _('Couldn\'t save: {message}', { message: error.message });
    }
}

// --- changing frames ----------------------------------------------------------------------

async function setFrames(frames, changes) {
    if (!frames.length) return;
    await Seleno.api('/album/api/frames', { json: { files: frames.map(f => f.name), set: changes } });
    frames.forEach(f => {
        Object.entries(changes).forEach(([key, value]) => { f[key] = value; });
        if ('frame_type' in changes) f.frame_type_set = !!changes.frame_type;
    });
    renderContent();
    renderNav();
    renderInspector();
}

async function setFlag(frames, flag) {
    try {
        await setFrames(frames, { flag });
    } catch (error) {
        Seleno.toast(_('Couldn\'t change the flag: {message}', { message: error.message }), 'bad');
    }
}

function postDownload(files, name) {
    const form = el('form', { method: 'post', action: '/album/download', style: { display: 'none' } }, [
        el('input', { type: 'hidden', name: 'files_json', value: JSON.stringify(files) }),
        el('input', { type: 'hidden', name: 'name', value: name || 'photos' }),
        el('input', { type: 'hidden', name: 'csrf_token', value: window.CSRF_TOKEN || '' })
    ]);
    document.body.appendChild(form);
    form.submit();
    setTimeout(() => form.remove(), 1000);
}

async function deleteFrames(frames) {
    if (!frames.length) return;
    const rawCount = frames.filter(f => f.raw).length;
    const what = frames.length === 1 ? _('1 photo') : _('{n} photos', { n: frames.length });
    const raws = rawCount === 1 ? _('1 RAW file') : _('{n} RAW files', { n: rawCount });
    const question = rawCount ? _("Delete {what} (and {raws} with them)? This can't be undone.", { what, raws })
        : _("Delete {what}? This can't be undone.", { what });
    if (!confirm(question)) return;
    try {
        const response = await fetch('/album/photo/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files: frames.map(f => f.name) })
        });
        const data = await response.json();
        if (data.failed && data.failed.length) Seleno.toast(_('{failed} couldn\'t be deleted.', { failed: data.failed.length }), 'warn');
        frames.forEach(f => state.selection.delete(f.name));
        if (frames.some(f => f.name === state.current)) state.current = null;
        await loadLibrary();
        renderPhotos();
    } catch (error) {
        Seleno.toast(_('Couldn\'t delete: {message}', { message: error.message }), 'bad');
    }
}

async function requestRaws(frames) {
    const files = frames.filter(f => f.kind === 'jpeg' && !f.raw).map(f => f.name);
    if (!files.length) {
        Seleno.toast(_('Those photos already have their RAWs here.'), 'ok');
        return;
    }
    try {
        const data = await Seleno.api('/album/api/request_raws', { json: { files } });
        data.results.forEach(r => Seleno.toast(r.status === 'success'
            ? (r.frames === 1 ? _('Asked {telescope} for 1 RAW. It appears here when it arrives.', { telescope: r.telescope })
                    : _('Asked {telescope} for {n} RAWs. They appear here as they arrive.', { telescope: r.telescope, n: r.frames }))
            : `${r.telescope || _('Telescope')}: ${r.message}`, r.status === 'success' ? 'ok' : 'warn', 7000));
        if (data.skipped.length) Seleno.toast(_('{skipped} didn\'t come from a telescope upload, so there\'s no RAW to ask for.', { skipped: data.skipped.length }), 'warn');
    } catch (error) {
        Seleno.toast(_('Couldn\'t ask for the RAWs: {message}', { message: error.message }), 'bad');
    }
}

// Star counts, sizes and sky levels for the album, measured in this tab
async function measureFrames(frames) {
    if (!requireEngine(_('Measuring quality'))) return;
    const list = frames.filter(f => f.kind !== 'video');
    if (!list.length) return;
    const controller = new AbortController();
    const job = startJob(list.length === 1 ? _('Measuring 1 photo') : _('Measuring {n} photos', { n: list.length }), { kind: 'measure', controller });
    try {
        const out = await engine.measure(list.map(f => ({ name: f.name, url: f.url, raw_url: f.raw_url, kind: f.kind })), {
            signal: controller.signal,
            onProgress: ({ fraction, message }) => job.update(fraction, message)
        });
        const good = Object.fromEntries(Object.entries(out).filter(([, m]) => m && !m.error));
        const failed = Object.entries(out).filter(([, m]) => !m || m.error);
        if (Object.keys(good).length) await Seleno.api('/album/api/metrics', { json: { metrics: good } });
        job.finish(failed.length ? _("{good} measured, {failed} couldn't be", { good: Object.keys(good).length, failed: failed.length })
            : _('{good} measured', { good: Object.keys(good).length }));
        if (failed.length) Seleno.toast(failed.length > 1
            ? _("Couldn't measure {name} and {more} more: {error}", { name: failed[0][0], more: failed.length - 1, error: failed[0][1] && failed[0][1].error })
            : _("Couldn't measure {name}: {error}", { name: failed[0][0], error: failed[0][1] && failed[0][1].error }), 'warn', 7000);
        await loadLibrary();
        renderPhotos();
    } catch (error) {
        job.fail(error);
        if (error.name !== 'AbortError') Seleno.toast(_('Measuring failed: {message}', { message: error.message }), 'bad');
    }
}

function luckyFrames(frames) {
    openLuckyDialog({
        frames,
        onSaved: async () => {
            state.stacks = await loadStacks();
            setStackCount(state.stacks.filter(s => s.state === 'ready').length);
        }
    });
}

function stackFrames(frames, title) {
    openStackDialog({
        lights: frames,
        library: state.frames,
        title,
        onSaved: async () => {
            state.stacks = await loadStacks();
            setStackCount(state.stacks.filter(s => s.state === 'ready').length);
        }
    });
}

function viewFrames(list, start, blink = false) {
    openViewer(list, start, {
        blink,
        quality: (f) => qualityOf(f),
        onReject: async (f) => setFlag([f], f.flag === 'reject' ? null : 'reject'),
        onChange: (f) => { state.current = f.name; refreshSelectionMarks(); renderInspector(); }
    });
}

// --- views --------------------------------------------------------------------------

async function showView(view, options = {}) {
    state.view = view;
    ['photos', 'stacks', 'activity', 'editor'].forEach(v => {
        const section = $(`view${v[0].toUpperCase()}${v.slice(1)}`);
        if (section) section.hidden = v !== view;
    });
    document.querySelectorAll('.ws-toolbar .ws-tab[data-view]').forEach(tab => {
        const on = tab.dataset.view === view || (view === 'editor' && tab.dataset.view === 'stacks');
        tab.classList.toggle('on', on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (!options.keepHash) {
        const hash = view === 'photos' ? '' : view === 'editor' ? (options.photo ? '' : `#edit=${options.id}`) : `#${view}`;
        if (location.hash !== hash) history.replaceState(null, '', location.pathname + location.search + hash);
    }
    if (view === 'stacks') await refreshStacks();
    if (view === 'activity') renderJobs($('jobList'), $('jobHistory'));
    if (view === 'editor') {
        try {
            if (options.photo) await openPhotoEditor(options.photo);
            else await openEditor(options.id);
        } catch (error) {
            Seleno.toast(_('Couldn\'t open that stack: {message}', { message: error.message }), 'bad');
            showView('stacks');
        }
    }
}

async function refreshStacks() {
    try {
        state.stacks = await loadStacks();
    } catch (error) {
        Seleno.toast(_('Couldn\'t load the stacks: {message}', { message: error.message }), 'bad');
        return;
    }
    setStackCount(state.stacks.filter(s => s.state === 'ready').length);
    renderStackCards($('stackCards'), state.stacks, {
        onOpen: (stack) => showView('editor', { id: stack.id }),
        onDelete: async (stack) => {
            if (!confirm(_('Delete the stack "{name}" and its master? The photos it came from stay.', { name: stack.name }))) return;
            await deleteStack(stack.id);
            refreshStacks();
        },
        onRename: async (stack) => {
            const name = prompt(_('New name'), stack.name);
            if (!name || name === stack.name) return;
            await updateStack(stack.id, { name });
            refreshStacks();
        }
    });
    renderProjects($('projectList'), {
        library: state.frames,
        onOpenStack: (id) => showView('editor', { id }),
        onSaved: () => refreshStacks()
    });
}

function routeFromHash() {
    const hash = location.hash.slice(1);
    if (hash.startsWith('edit=')) return showView('editor', { id: hash.slice(5), keepHash: true });
    if (hash === 'stacks' || hash === 'activity') return showView(hash, { keepHash: true });
    return showView('photos', { keepHash: true });
}

// --- wiring -------------------------------------------------------------------------------

function wire() {
    document.querySelectorAll('.ws-toolbar .ws-tab[data-view], #viewEditor [data-view]').forEach(tab => tab.addEventListener('click', () => showView(tab.dataset.view)));
    $('groupContent').addEventListener('click', onFrameClick);
    $('groupContent').addEventListener('dblclick', (event) => {
        const node = event.target.closest('[data-name]');
        if (!node) return;
        const list = state.visible.filter(f => f.kind !== 'video');
        const i = list.findIndex(f => f.name === node.dataset.name);
        if (i >= 0) viewFrames(list, i);
    });
    document.querySelectorAll('#groupBySeg button').forEach(b => b.addEventListener('click', () => {
        state.groupBy = b.dataset.groupBy;
        savePrefs();
        const frame = state.current ? state.byName.get(state.current) : (currentFrames()[0] || null);
        state.groupKey = frame ? Q.groupKeyOf(frame, state.groupBy) : 'all';
        renderPhotos();
    }));
    document.querySelectorAll('#layoutSeg button').forEach(b => b.addEventListener('click', () => {
        state.layout = b.dataset.layout;
        savePrefs();
        renderPhotos();
    }));
    $('hideRejectedChip').addEventListener('click', () => {
        state.hideRejected = !state.hideRejected;
        savePrefs();
        renderPhotos();
    });
    ['limitFwhm', 'limitEcc', 'limitStars'].forEach(id => $(id).addEventListener('input', () => renderQuality(currentFrames())));
    $('btnSuggestLimits').addEventListener('click', suggestLimits);
    $('btnRejectByLimits').addEventListener('click', async () => {
        const over = Q.overLimits(currentFrames(), readLimits());
        if (over.length && confirm(over.length === 1 ? _('Reject 1 frame over the limits?') : _('Reject {n} frames over the limits?', { n: over.length }))) await setFlag(over, 'reject');
    });

    // Group header
    $('btnDownloadGroup').addEventListener('click', () => {
        const frames = currentFrames();
        postDownload(frames.map(f => f.name), groupTitle(frames));
    });
    $('btnStackGroup').addEventListener('click', () => {
        const frames = currentFrames();
        stackFrames(frames.filter(Q.isLightLike), _('Stack {title}', { title: groupTitle(frames) }));
    });

    // Inspector
    ['inspectorTarget', 'inspectorNotes'].forEach(id => $(id).addEventListener('input', () => {
        metaDirty = true;
        $('btnSaveMeta').disabled = false;
        $('metaStatus').textContent = '';
    }));
    $('btnSaveMeta').addEventListener('click', saveMeta);
    $('inspectorTarget').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveMeta(); });
    $('btnFlagPick').addEventListener('click', () => { const f = state.byName.get(state.current); if (f) setFlag([f], f.flag === 'pick' ? null : 'pick'); });
    $('btnFlagReject').addEventListener('click', () => { const f = state.byName.get(state.current); if (f) setFlag([f], f.flag === 'reject' ? null : 'reject'); });
    $('btnFlagClear').addEventListener('click', () => { const f = state.byName.get(state.current); if (f) setFlag([f], null); });
    const typeOptions = (select, placeholder) => {
        select.appendChild(el('option', { value: '' }, placeholder));
        Q.SETTABLE_TYPES.forEach(t => select.appendChild(el('option', { value: t }, Q.TYPE_LABELS[t])));
    };
    typeOptions($('inspectorType'), _('Not set'));
    typeOptions($('bulkType'), _('Choose…'));
    $('inspectorType').addEventListener('change', (e) => {
        const f = state.byName.get(state.current);
        if (f) setFrames([f], { frame_type: e.target.value || null }).catch(error => Seleno.toast(error.message, 'bad'));
    });
    $('btnViewFull').addEventListener('click', () => {
        const list = state.visible.length ? state.visible.filter(f => f.kind !== 'video') : [state.byName.get(state.current)];
        const i = Math.max(0, list.findIndex(f => f.name === state.current));
        viewFrames(list.length ? list : [state.byName.get(state.current)], i);
    });
    $('btnBlink').addEventListener('click', () => {
        let list = selectedFrames().filter(f => f.kind !== 'video');
        if (list.length < 2) list = currentFrames().filter(Q.isLightLike).sort((a, b) => Q.frameTime(a) - Q.frameTime(b));
        if (list.length < 2) return;
        viewFrames(list, Math.max(0, list.findIndex(f => f.name === state.current)), true);
    });
    $('btnDownloadOne').addEventListener('click', () => postDownload([state.current], state.current));
    $('btnEditPhoto').addEventListener('click', () => {
        const f = state.byName.get(state.current);
        if (f && requireEngine(_('Editing a photo'))) showView('editor', { photo: f });
    });
    $('btnLucky').addEventListener('click', () => {
        const f = state.byName.get(state.current);
        if (f) luckyFrames([f]);
    });
    $('exifDetails').addEventListener('toggle', loadExif);

    // Bulk bar
    $('btnClearSelection').addEventListener('click', () => { state.selection.clear(); refreshSelectionMarks(); renderBulk(); });
    $('bulkType').addEventListener('change', async (e) => {
        const type = e.target.value;
        if (!type) return;
        await setFrames(selectedFrames(), { frame_type: type }).catch(error => Seleno.toast(error.message, 'bad'));
        e.target.value = '';
    });
    $('btnBulkAccept').addEventListener('click', () => setFlag(selectedFrames(), 'pick'));
    $('btnBulkReject').addEventListener('click', () => setFlag(selectedFrames(), 'reject'));
    $('btnBulkMeasure').addEventListener('click', () => measureFrames(selectedFrames()));
    $('btnBulkLucky').addEventListener('click', () => {
        const list = selectedFrames();
        const videos = list.filter(f => f.kind === 'video');
        if (videos.length === 1 && list.length === 1) luckyFrames(videos);
        else if (list.filter(f => f.kind !== 'video').length >= 2) luckyFrames(list.filter(f => f.kind !== 'video'));
        else Seleno.toast(_('Choose one video, or two or more photos of the Moon or a planet.'), 'warn');
    });
    $('btnBulkRaws').addEventListener('click', () => requestRaws(selectedFrames()));
    $('btnBulkDownload').addEventListener('click', () => postDownload([...state.selection], `${state.selection.size} photos`));
    $('btnBulkDelete').addEventListener('click', () => deleteFrames(selectedFrames()));
    $('btnBulkStack').addEventListener('click', () => stackFrames(selectedFrames(), _('Stack {size} photos', { size: state.selection.size })));

    // Keys, in the photos view
    document.addEventListener('keydown', (e) => {
        if (state.view !== 'photos' || e.target.closest('input, textarea, select, dialog') || document.querySelector('dialog[open]')) return;
        const list = state.visible;
        const i = list.findIndex(f => f.name === state.current);
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { if (list.length) setCurrent(list[Math.min(list.length - 1, i + 1)].name); }
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { if (list.length) setCurrent(list[Math.max(0, i - 1)].name); }
        else if (e.key === ' ' && state.current) toggleSelection(state.current);
        else if (e.key === 'x' && state.current) $('btnFlagReject').click();
        else if (e.key === 'p' && state.current) $('btnFlagPick').click();
        else if (e.key === 'Enter' && state.current) $('btnViewFull').click();
        else if (e.key === 'Escape') { state.selection.clear(); refreshSelectionMarks(); renderBulk(); }
        else if ((e.ctrlKey || e.metaKey) && e.key === 'a') { list.forEach(f => state.selection.add(f.name)); refreshSelectionMarks(); renderBulk(); }
        else return;
        e.preventDefault();
    });

    document.addEventListener('seleno:mode', () => { if (state.view === 'photos') renderPhotos(); });
    window.addEventListener('hashchange', routeFromHash);
    let resizeTimer = 0;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => { if (state.view === 'photos' && Seleno.mode() === 'advanced') renderQuality(currentFrames()); }, 150);
    });
    window.addEventListener('beforeunload', (e) => {
        if (runningCount()) {
            e.preventDefault();
            e.returnValue = '';
        }
    });
    onJobsChange(() => {
        const n = runningCount();
        const chip = $('activityChip');
        chip.textContent = _('{n} running', { n });
        chip.classList.toggle('ws-hidden', !n);
        if (state.view === 'activity') renderJobs($('jobList'), $('jobHistory'));
    });
}

async function start() {
    loadPrefs();
    wire();
    initHistogram();
    try {
        await loadLibrary();
    } catch (error) {
        return;
    }
    chooseDefaultGroup();
    renderPhotos();
    await routeFromHash();
    // New uploads arrive while the page is open
    setInterval(async () => {
        if (document.hidden || state.view !== 'photos' || document.querySelector('dialog[open]')) return;
        const before = state.frames.length;
        try {
            await loadLibrary();
        } catch (error) {
            return;
        }
        if (state.frames.length !== before) renderPhotos();
    }, 60000);
}

start();
