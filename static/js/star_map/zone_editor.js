// Star map - drawing a telescope's exclusion zones. Only on the map opened from a
// telescope's own page (/telescopes/<id>/zones/map), which sits at the telescope's site,
// so the sky's altitude and azimuth here are the telescope's.
//
// A zone is a box of altitude and azimuth (app/dead_zones.py). With "Draw a zone" on,
// dragging across the sky marks one from the corner where the drag started to the corner
// where it ends; it can then be named, typed and nudged in the panel. Nothing reaches the
// telescope until "Save to the telescope".

const ZONE_EDITOR = JSON.parse(document.getElementById('zone-editor-data').textContent);
window.ZONE_EDITOR = ZONE_EDITOR;

let zoneEditorZones = [];        // the working list (what's drawn)
let zoneEditorReported = [];     // what the telescope last said
let zoneEditorSelected = -1;     // index into zoneEditorZones, or -1
let zoneEditorDraft = null;      // the box being dragged out
let zoneEditorDirty = false;
let zoneEditorConnected = false;
let zoneDrawMode = false;
let zoneDragStart = null;        // { altDeg, azDeg } where the drag began
let zoneDragMoved = false;

const zeEl = id => document.getElementById(id);

// For drawDeadZones (dead_zones.js): the zones, the selected one marked, and the draft
function zoneEditorDrawList() {
    const list = zoneEditorZones.map((z, i) => (i === zoneEditorSelected ? { ...z, _selected: true } : z));
    if (zoneEditorDraft) list.push({ ...zoneEditorDraft, _draft: true });
    return list;
}

function zeRound(value) {
    return Math.round(value * 2) / 2;
}

function zeWrap(az) {
    return ((az % 360) + 360) % 360;
}

// The box between two sky points: azimuth the short way round between them
function zeBoxBetween(a, b) {
    const d = ((b.azDeg - a.azDeg + 540) % 360) - 180;
    const from = zeWrap(zeRound(d >= 0 ? a.azDeg : b.azDeg));
    let to = zeWrap(zeRound(d >= 0 ? b.azDeg : a.azDeg));
    if (to === from) to = zeWrap(from + 0.5);
    let lo = zeRound(Math.min(a.altDeg, b.altDeg));
    const hi = Math.max(lo + 0.5, zeRound(Math.max(a.altDeg, b.altDeg)));
    if (zeEl('ze-to-ground').checked) lo = -90;
    return { name: '', az: [from, to], alt: [Math.max(-90, lo), Math.min(90, hi)], reason: zeEl('ze-new-reason').value, locked: false };
}

function zeSetStatus(text, kind) {
    const el = zeEl('ze-status');
    el.textContent = text || '';
    el.className = 'ze-status' + (kind ? ' ' + kind : '');
}

function zeMarkDirty() {
    zoneEditorDirty = true;
    zeEl('ze-undo').hidden = false;
    deadZones = zoneEditorZones;
    zeRenderList();
    scheduleDraw();
}

function zeFold(folded) {
    zeEl('zone-editor').classList.toggle('folded', folded);
    zeEl('ze-fold').textContent = folded ? '▸' : '▾';
    zeEl('ze-fold').setAttribute('aria-expanded', folded ? 'false' : 'true');
}

function zeSetDrawMode(on) {
    zoneDrawMode = on;
    // On a phone the panel covers half the sky: fold it away while drawing
    if (isCompactLayout()) zeFold(on);
    zeEl('ze-draw').setAttribute('aria-pressed', on ? 'true' : 'false');
    document.documentElement.classList.toggle('zone-drawing', on);
    if (!on) {
        zoneEditorDraft = null;
        zoneDragStart = null;
        scheduleDraw();
    }
    zeSetStatus(on ? _('Drag across the sky to mark a zone. Press Draw a zone again to look around.') : '');
}

// --- the panel -------------------------------------------------------------------------

function zeDescribe(zone) {
    return _('Az {from}° to {to}°, alt {lo}° to {hi}°', { from: zone.az[0], to: zone.az[1], lo: zone.alt[0], hi: zone.alt[1] });
}

function zeRenderList() {
    const list = zeEl('ze-list');
    list.replaceChildren();
    if (!zoneEditorZones.length) {
        const none = document.createElement('div');
        none.className = 'ze-muted';
        none.textContent = _('No exclusion zones yet.');
        list.append(none);
    }
    zoneEditorZones.forEach((zone, i) => {
        const item = document.createElement('div');
        item.className = 'ze-item' + (i === zoneEditorSelected ? ' selected' : '');
        item.tabIndex = 0;
        const dot = document.createElement('span');
        dot.className = 'ze-dot';
        dot.style.background = (DEAD_ZONE_STYLES[zone.reason] || DEAD_ZONE_STYLES.other).edge;
        const text = document.createElement('div');
        const title = document.createElement('div');
        title.textContent = zone.name || _('Unnamed zone');
        if (zone.locked) {
            const lock = document.createElement('span');
            lock.className = 'ze-lock';
            lock.textContent = _('set on the telescope');
            lock.title = _("Locked on the telescope itself: it can't be changed here");
            title.append(lock);
        }
        const detail = document.createElement('div');
        detail.className = 'ze-muted';
        detail.textContent = zeDescribe(zone);
        text.append(title, detail);
        const kind = document.createElement('span');
        kind.className = 'ze-muted';
        kind.textContent = deadZoneLabel({ reason: zone.reason });
        item.append(dot, text, kind);
        const pick = () => zeSelect(i, true);
        item.addEventListener('click', pick);
        item.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
        list.append(item);
    });
    zeRenderForm();
}

function zeRenderForm() {
    const form = zeEl('ze-form');
    const zone = zoneEditorZones[zoneEditorSelected];
    form.hidden = !zone;
    if (!zone) return;
    const locked = !!zone.locked;
    zeEl('ze-name').value = zone.name || '';
    zeEl('ze-reason').value = zone.reason || 'other';
    zeEl('ze-az0').value = zone.az[0];
    zeEl('ze-az1').value = zone.az[1];
    zeEl('ze-alt0').value = zone.alt[0];
    zeEl('ze-alt1').value = zone.alt[1];
    ['ze-name', 'ze-reason', 'ze-az0', 'ze-az1', 'ze-alt0', 'ze-alt1', 'ze-delete']
        .forEach(id => { zeEl(id).disabled = locked; });
}

// Turn the view to face a zone
function zeLookAt(zone) {
    const span = deadZoneWidth(zone);
    const mid = zone.az[0] + span / 2;
    const hi = zone.alt[1], lo = zone.alt[0];
    const alt = hi > 0 ? (Math.max(lo, 0) + hi) / 2 : (lo + hi) / 2;
    rotY = zeWrap(mid) * Math.PI / 180;
    rotX = Math.max(-0.2, Math.min(1.3, (alt - 10) * Math.PI / 180));
}

function zeSelect(index, look) {
    zoneEditorSelected = index;
    if (look && zoneEditorZones[index]) zeLookAt(zoneEditorZones[index]);
    zeRenderList();
    scheduleDraw();
}

// The form's values into the selected zone, as far as they make sense
function zeApplyForm() {
    const zone = zoneEditorZones[zoneEditorSelected];
    if (!zone || zone.locked) return;
    const num = id => parseFloat(zeEl(id).value);
    const az0 = num('ze-az0'), az1 = num('ze-az1'), alt0 = num('ze-alt0'), alt1 = num('ze-alt1');
    zone.name = zeEl('ze-name').value.slice(0, 40);
    zone.reason = zeEl('ze-reason').value;
    if (Number.isFinite(az0) && Number.isFinite(az1)) zone.az = [az0, az1];
    if (Number.isFinite(alt0) && Number.isFinite(alt1)) {
        zone.alt = [Math.max(-90, Math.min(alt0, alt1)), Math.min(90, Math.max(alt0, alt1))];
    }
    zoneEditorDirty = true;
    zeEl('ze-undo').hidden = false;
    deadZones = zoneEditorZones;
    // Refresh the list text without rebuilding the form under the cursor
    const item = zeEl('ze-list').children[zoneEditorSelected];
    if (item) {
        item.querySelector('.ze-dot').style.background = (DEAD_ZONE_STYLES[zone.reason] || DEAD_ZONE_STYLES.other).edge;
        item.children[1].children[0].textContent = zone.name || _('Unnamed zone');
        item.children[1].children[1].textContent = zeDescribe(zone);
        item.children[2].textContent = deadZoneLabel({ reason: zone.reason });
    }
    scheduleDraw();
}

function zeProblem(zones) {
    for (const zone of zones) {
        const [az0, az1] = zone.az;
        if (![az0, az1, zone.alt[0], zone.alt[1]].every(Number.isFinite)) return _('Fill in both azimuths and both altitudes for every zone.');
        if (zeWrap(az0) === zeWrap(az1) && !(az0 === 0 && az1 === 360)) return _('A zone needs two different azimuths (0 to 360 is all round).');
        if (zone.alt[1] - zone.alt[0] < 0.1) return _('A zone needs two different altitudes.');
    }
    return null;
}

// --- talking to the server ----------------------------------------------------------------

function zeShowConnection(connected, reported) {
    zoneEditorConnected = connected;
    const badge = zeEl('ze-connection');
    badge.textContent = connected ? _('Connected') : _('Offline');
    badge.classList.toggle('on', connected);
    zeEl('ze-save').disabled = !connected;
    if (!connected) {
        zeSetStatus(_('The telescope is offline. You can draw, but it has to be connected to save.'), 'bad');
    } else if (!reported) {
        zeSetStatus(_("This telescope hasn't reported its exclusion zones yet. Update its Pi software to use them."), 'bad');
    }
}

function zeLoad() {
    return fetch(ZONE_EDITOR.zones_url, { headers: { Accept: 'application/json' } })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') throw new Error(data.message || _('Could not load the zones'));
            deadZoneSite = data.site || ZONE_EDITOR.site || null;
            zeEl('ze-site').textContent = deadZoneSite
                ? _("Drawn from the telescope's site: {lat}°, {lon}°", { lat: deadZoneSite.lat.toFixed(2), lon: deadZoneSite.lon.toFixed(2) })
                : _("The telescope hasn't reported where it is, so this map uses your location.");
            zeShowConnection(data.connected, data.reported);
            if (!zoneEditorDirty) {
                zoneEditorReported = data.zones.map(z => ({ ...z, az: [...z.az], alt: [...z.alt] }));
                zoneEditorZones = data.zones;
                deadZones = zoneEditorZones;
                zeRenderList();
                scheduleDraw();
            }
        })
        .catch(error => zeSetStatus(error.message, 'bad'));
}

function zeSave() {
    zeApplyForm();
    const zones = zoneEditorZones.filter(z => !z.locked)
        .map(z => ({ name: z.name, az: z.az, alt: z.alt, reason: z.reason }));
    const problem = zeProblem(zones);
    if (problem) {
        zeSetStatus(problem, 'bad');
        return;
    }
    zeEl('ze-save').disabled = true;
    zeSetStatus(_('Saving…'));
    fetch(ZONE_EDITOR.zones_url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ zones }) })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') throw new Error(data.message || _('Saving failed'));
            zoneEditorDirty = false;
            zeEl('ze-undo').hidden = true;
            zoneEditorSelected = -1;
            zoneEditorReported = data.zones.map(z => ({ ...z, az: [...z.az], alt: [...z.alt] }));
            zoneEditorZones = data.zones;
            deadZones = zoneEditorZones;
            zeRenderList();
            scheduleDraw();
            zeShowConnection(data.connected, data.reported);
            zeSetStatus(_('Saved on the telescope.'), 'ok');
        })
        .catch(error => zeSetStatus(error.message, 'bad'))
        .finally(() => { zeEl('ze-save').disabled = !zoneEditorConnected; });
}

// --- drawing ------------------------------------------------------------------------------

function zeSkyAt(x, y) {
    const rect = canvas.getBoundingClientRect();
    const { altDeg, azDeg } = getCoordsAtScreen(x - rect.left, y - rect.top);
    return { altDeg, azDeg };
}

// The zone under a screen point (for picking one with a click while drawing)
function zeZoneAt(x, y) {
    const { altDeg, azDeg } = zeSkyAt(x, y);
    const hit = deadZoneAtMapAltAz(altDeg, azDeg);
    return hit ? zoneEditorZones.indexOf(hit) : -1;
}

function zeDragStart(x, y) {
    zoneDragStart = zeSkyAt(x, y);
    zoneDragMoved = false;
    zoneEditorDraft = null;
}

function zeDragMove(x, y) {
    if (!zoneDragStart) return;
    const here = zeSkyAt(x, y);
    const moved = Math.abs(here.altDeg - zoneDragStart.altDeg) > 0.5
        || Math.abs(((here.azDeg - zoneDragStart.azDeg + 540) % 360) - 180) > 0.5;
    if (!moved && !zoneDragMoved) return;
    zoneDragMoved = true;
    zoneEditorDraft = zeBoxBetween(zoneDragStart, here);
    scheduleDraw();
}

function zeDragEnd(x, y) {
    if (!zoneDragStart) return;
    if (zoneDragMoved && zoneEditorDraft) {
        zoneEditorZones.push(zoneEditorDraft);
        zoneEditorDraft = null;
        zoneDragStart = null;
        zoneEditorSelected = zoneEditorZones.length - 1;
        zeMarkDirty();
        zeSetStatus(_('Zone added. Name it, then save it to the telescope.'));
        zeFold(false);
        const name = zeEl('ze-name');
        if (!isTouchDevice) name.focus();
    } else {
        // A click, not a drag: pick the zone there, if any
        zoneDragStart = null;
        const index = zeZoneAt(x, y);
        zeSelect(index, false);
    }
}

// Capture-phase listeners run before the map's own (input.js), so while drawing a drag
// marks a zone instead of turning the sky. A two-finger gesture still pans and zooms.
canvas.addEventListener('mousedown', e => {
    if (!zoneDrawMode || e.button !== 0) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    zeDragStart(e.clientX, e.clientY);
}, { capture: true });
window.addEventListener('mousemove', e => {
    if (!zoneDragStart) return;
    e.stopImmediatePropagation();
    zeDragMove(e.clientX, e.clientY);
}, { capture: true });
window.addEventListener('mouseup', e => {
    if (!zoneDragStart) return;
    e.stopImmediatePropagation();
    zeDragEnd(e.clientX, e.clientY);
}, { capture: true });
canvas.addEventListener('click', e => {
    // The map would open the star under the pointer; while drawing, a click picks a zone
    if (zoneDrawMode) e.stopImmediatePropagation();
}, { capture: true });

canvas.addEventListener('touchstart', e => {
    if (!zoneDrawMode) return;
    if (e.touches.length === 1) {
        e.stopImmediatePropagation();
        e.preventDefault();
        zeDragStart(e.touches[0].clientX, e.touches[0].clientY);
    } else if (zoneDragStart) {
        // A second finger: give the gesture back to the map
        zoneDragStart = null;
        zoneEditorDraft = null;
        scheduleDraw();
    }
}, { capture: true, passive: false });
canvas.addEventListener('touchmove', e => {
    if (!zoneDragStart || e.touches.length !== 1) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    zeDragMove(e.touches[0].clientX, e.touches[0].clientY);
}, { capture: true, passive: false });
canvas.addEventListener('touchend', e => {
    if (!zoneDragStart) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    const t = e.changedTouches[0];
    zeDragEnd(t.clientX, t.clientY);
}, { capture: true, passive: false });

// --- wiring -------------------------------------------------------------------------------

zeEl('ze-draw').addEventListener('click', () => zeSetDrawMode(!zoneDrawMode));
zeEl('ze-fold').addEventListener('click', () => zeFold(!zeEl('zone-editor').classList.contains('folded')));
['ze-name', 'ze-reason', 'ze-az0', 'ze-az1', 'ze-alt0', 'ze-alt1'].forEach(id => {
    zeEl(id).addEventListener('input', zeApplyForm);
    zeEl(id).addEventListener('change', zeApplyForm);
});
zeEl('ze-done').addEventListener('click', () => { zeApplyForm(); zeSelect(-1, false); });
zeEl('ze-delete').addEventListener('click', () => {
    if (zoneEditorSelected < 0 || zoneEditorZones[zoneEditorSelected].locked) return;
    zoneEditorZones.splice(zoneEditorSelected, 1);
    zoneEditorSelected = -1;
    zeMarkDirty();
});
zeEl('ze-save').addEventListener('click', zeSave);
zeEl('ze-undo').addEventListener('click', () => {
    zoneEditorZones = zoneEditorReported.map(z => ({ ...z, az: [...z.az], alt: [...z.alt] }));
    deadZones = zoneEditorZones;
    zoneEditorSelected = -1;
    zoneEditorDirty = false;
    zeEl('ze-undo').hidden = true;
    zeRenderList();
    zeSetStatus('');
    scheduleDraw();
});
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && zoneDrawMode && !e.target.closest('input, select, textarea')) zeSetDrawMode(false);
});
window.addEventListener('beforeunload', e => {
    if (zoneEditorDirty) { e.preventDefault(); e.returnValue = ''; }
});

deadZoneSite = ZONE_EDITOR.site || null;
zeRenderList();
zeLoad();
setInterval(() => { if (!document.hidden) zeLoad(); }, 30000);
