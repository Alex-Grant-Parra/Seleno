// A telescope's page: its exclusion zones, as a table to edit.
//
// The telescope keeps the zones and enforces them; this page shows what it last
// reported (GET .../zones) and sends the whole list back to it (POST .../zones).
// Zones locked on the telescope itself are shown but can't be changed here.
(function () {
    'use strict';

    const _ = (text, values) => (window._ ? window._(text, values) : text);
    const card = document.getElementById('tz-card');
    if (!card) return;
    const url = card.dataset.zonesUrl;
    const rows = document.getElementById('tz-rows');
    const note = document.getElementById('tz-note');
    const empty = document.getElementById('tz-empty');
    const connection = document.getElementById('tz-connection');
    const addBtn = document.getElementById('tz-add');
    const saveBtn = document.getElementById('tz-save');
    const revertBtn = document.getElementById('tz-revert');
    const REASONS = [['privacy', _('Privacy')], ['physical', _('Physical limit')],
        ['other', window._p ? window._p('dead zone reason', 'Other') : 'Other']];

    let reported = [];      // what the telescope last said
    let dirty = false;
    let connected = false;

    function setNote(text, kind) {
        note.textContent = text || '';
        note.className = `alert alert-${kind || 'secondary'} small py-2` + (text ? '' : ' d-none');
    }

    function markDirty() {
        dirty = true;
        revertBtn.classList.remove('d-none');
        updateEmpty();
    }

    function updateEmpty() {
        empty.classList.toggle('d-none', rows.children.length > 0);
    }

    function input(key, value, attrs) {
        const el = document.createElement('input');
        el.className = 'form-control form-control-sm';
        el.dataset.key = key;
        el.value = value ?? '';
        Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
        el.addEventListener('input', markDirty);
        return el;
    }

    function row(zone) {
        const locked = !!zone.locked;
        const tr = document.createElement('tr');
        if (locked) tr.dataset.locked = '1';
        // On phones the table turns into cards, and each cell shows its column's heading
        const headings = [...document.querySelectorAll('#tz-table thead th')].map(th => th.textContent.trim());
        const cell = (child) => {
            const td = document.createElement('td');
            const label = headings[tr.children.length];
            if (label) td.dataset.label = label;
            td.append(child);
            tr.append(td);
            return td;
        };
        const num = (key, value, min, max) => input(key, value, { type: 'number', min, max, step: '0.5', inputmode: 'decimal' });
        const az = Array.isArray(zone.az) ? zone.az : ['', ''];
        const alt = Array.isArray(zone.alt) ? zone.alt : [-90, ''];
        const name = input('name', zone.name || '', { type: 'text', maxlength: '40', placeholder: _('e.g. Next door') });
        const nameCell = cell(name);
        if (locked) {
            const badge = document.createElement('div');
            badge.className = 'small text-muted mt-1';
            badge.innerHTML = '<i class="fas fa-lock"></i> ';
            badge.append(_('Set on the telescope'));
            nameCell.append(badge);
        }
        cell(num('az0', az[0], 0, 360));
        cell(num('az1', az[1], 0, 360));
        cell(num('alt0', alt[0], -90, 90));
        cell(num('alt1', alt[1], -90, 90));
        const reason = document.createElement('select');
        reason.className = 'form-select form-select-sm';
        reason.dataset.key = 'reason';
        REASONS.forEach(([value, label]) => {
            reason.append(new Option(label, value, false, (zone.reason || 'privacy') === value));
        });
        reason.addEventListener('change', markDirty);
        cell(reason);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-sm btn-outline-danger';
        remove.title = _('Remove this zone');
        remove.innerHTML = '<i class="fas fa-trash"></i>';
        remove.addEventListener('click', () => { tr.remove(); markDirty(); });
        cell(locked ? document.createTextNode('') : remove);
        if (locked) tr.querySelectorAll('input, select').forEach(el => { el.disabled = true; });
        return tr;
    }

    function render(zones) {
        rows.replaceChildren(...zones.map(row));
        dirty = false;
        revertBtn.classList.add('d-none');
        updateEmpty();
    }

    function setControls(reportedZones, isConnected) {
        connected = isConnected;
        connection.textContent = isConnected ? _('Connected') : _('Offline');
        connection.className = `badge fs-6 align-middle ${isConnected ? 'bg-success' : 'bg-secondary'}`;
        saveBtn.disabled = !isConnected;
        addBtn.disabled = !reportedZones;
        if (!reportedZones) {
            setNote(isConnected
                ? _("This telescope hasn't reported its exclusion zones yet. Update its Pi software if this doesn't change within a minute.")
                : _('This telescope is offline. Its exclusion zones show here once it connects.'), 'secondary');
        } else if (!isConnected) {
            setNote(_('The telescope is offline, so these are the zones it last reported. It has to be connected to change them.'), 'warning');
        } else {
            setNote('');
        }
    }

    function load() {
        return fetch(url, { headers: { Accept: 'application/json' } })
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'success') throw new Error(data.message || _('Could not load the zones'));
                setControls(data.reported, data.connected);
                if (data.reported) {
                    reported = data.zones;
                    if (!dirty) render(reported);
                }
            })
            .catch(error => setNote(error.message, 'danger'));
    }

    function read() {
        const zones = [];
        for (const tr of rows.querySelectorAll('tr')) {
            if (tr.dataset.locked) continue;  // the telescope keeps these whatever is sent
            const get = key => tr.querySelector(`[data-key="${key}"]`).value;
            const values = ['az0', 'az1', 'alt0', 'alt1'].map(key => parseFloat(get(key)));
            if (values.some(v => !Number.isFinite(v))) {
                tr.querySelector('input[data-key="az0"]').focus();
                throw new Error(_('Fill in both azimuths and both altitudes for every zone.'));
            }
            zones.push({ name: get('name').trim(), az: [values[0], values[1]], alt: [values[2], values[3]], reason: get('reason') });
        }
        return zones;
    }

    function save() {
        let zones;
        try {
            zones = read();
        } catch (error) {
            setNote(error.message, 'warning');
            return;
        }
        saveBtn.disabled = true;
        fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ zones }) })
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'success') throw new Error(data.message || _('Saving failed'));
                reported = data.zones;
                render(reported);
                setControls(data.reported, data.connected);
                setNote(_('Saved on the telescope.'), 'success');
            })
            .catch(error => setNote(error.message, 'danger'))
            .finally(() => { saveBtn.disabled = !connected; });
    }

    addBtn.addEventListener('click', () => {
        const tr = row({ reason: 'privacy', alt: [-90, 30] });
        rows.append(tr);
        markDirty();
        tr.querySelector('input').focus();
    });
    saveBtn.addEventListener('click', save);
    revertBtn.addEventListener('click', () => { render(reported); setNote(''); });
    window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

    load();
    // Keep the connection badge current; the rows stay as they are while being edited
    setInterval(() => { if (!document.hidden) load(); }, 30000);
})();
