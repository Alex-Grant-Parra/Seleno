// The telescope's own settings (templates/telescope_settings.html): what the
// telescope's getSettings() describes, as a form; changes go to setSettings().
(function () {
    'use strict';

    const root = document.getElementById('ts-root');
    if (!root || !root.dataset.telescopeId) return;

    const T = window.TS_TEXT || {};
    const telescopeId = root.dataset.telescopeId;
    const url = root.dataset.url;
    const sectionsEl = document.getElementById('ts-sections');
    const statusEl = document.getElementById('ts-status');
    const actionsEl = document.getElementById('ts-actions');
    const saveBtn = document.getElementById('ts-save');
    const revertBtn = document.getElementById('ts-revert');

    let original = {};   // key -> value as the telescope sent it
    let current = {};    // key -> value in the form
    let clockTimer = null;

    function csrf() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    function status(text, kind) {
        statusEl.className = `alert alert-${kind || 'secondary'} small py-2`;
        statusEl.textContent = text;
        statusEl.classList.toggle('d-none', !text);
    }

    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        Object.entries(attrs || {}).forEach(([k, v]) => {
            if (v === undefined || v === null || v === false) return;
            if (k === 'text') node.textContent = v;
            else if (k === 'class') node.className = v;
            else node.setAttribute(k, v === true ? '' : v);
        });
        (children || []).forEach(c => c && node.appendChild(c));
        return node;
    }

    function changedKeys() {
        return Object.keys(current).filter(k => JSON.stringify(current[k]) !== JSON.stringify(original[k]));
    }

    function refreshButtons() {
        const dirty = changedKeys().length > 0;
        saveBtn.disabled = !dirty;
        revertBtn.disabled = !dirty;
    }

    function setValue(key, value) {
        current[key] = value;
        refreshButtons();
    }

    function inputFor(item) {
        const id = `ts-${item.key.replace(/[^a-z0-9]+/gi, '-')}`;
        const value = item.value;
        if (item.type === 'toggle') {
            const box = el('input', { class: 'form-check-input', type: 'checkbox', role: 'switch', id });
            box.checked = !!value;
            box.addEventListener('change', () => setValue(item.key, box.checked));
            return [id, el('div', { class: 'form-check form-switch' }, [box, el('label', { class: 'form-check-label', for: id, text: T.on || 'On' })])];
        }
        if (item.type === 'choice') {
            const select = el('select', { class: 'form-select', id });
            (item.options || []).forEach(opt => {
                const o = el('option', { value: opt.value, text: opt.label });
                if (String(opt.value) === String(value)) o.selected = true;
                select.appendChild(o);
            });
            select.addEventListener('change', () => setValue(item.key, select.value));
            return [id, select];
        }
        if (item.type === 'number') {
            const input = el('input', { class: 'form-control', type: 'number', id, min: item.min, max: item.max, step: item.step || 'any', inputmode: 'decimal' });
            input.value = value ?? '';
            input.addEventListener('input', () => setValue(item.key, input.value === '' ? null : Number(input.value)));
            const group = el('div', { class: 'input-group' }, [input]);
            if (item.unit) group.appendChild(el('span', { class: 'input-group-text', text: item.unit }));
            return [id, group];
        }
        if (item.type === 'info') {
            return [null, el('div', { class: 'form-control-plaintext', text: value ?? '-' })];
        }
        if (item.type === 'clock') {
            return [null, clockControl(item)];
        }
        // text and password
        const input = el('input', {
            class: 'form-control', id, type: item.type === 'password' ? 'password' : 'text',
            maxlength: item.max_length, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
            placeholder: item.placeholder,
        });
        input.value = value ?? '';
        input.addEventListener('input', () => setValue(item.key, input.value));
        if (item.type !== 'password') return [id, input];
        const toggle = el('button', { class: 'btn btn-outline-secondary', type: 'button', text: T.show || 'Show' });
        toggle.addEventListener('click', () => {
            const hidden = input.type === 'password';
            input.type = hidden ? 'text' : 'password';
            toggle.textContent = hidden ? (T.hide || 'Hide') : (T.show || 'Show');
        });
        return [id, el('div', { class: 'input-group' }, [input, toggle])];
    }

    function fmtUtc(date) {
        return date.toISOString().replace('T', ' ').slice(0, 19);
    }

    function clockControl(item) {
        // The telescope's clock against this device's: set it from here (a Pi
        // without a clock battery wakes up at the wrong time, offline)
        const telescopeAt = item.value ? new Date(item.value) : null;
        const fetchedAt = Date.now();
        const scope = el('div', { class: 'small' });
        const device = el('div', { class: 'small text-muted' });
        const button = el('button', { class: 'btn btn-sm btn-outline-primary mt-1', type: 'button', text: T.use_phone_time || "Use this device's time" });
        function tick() {
            const now = new Date();
            scope.textContent = `${T.clock_now || 'Telescope'}: ${telescopeAt ? fmtUtc(new Date(telescopeAt.getTime() + (Date.now() - fetchedAt))) : '-'}`;
            device.textContent = `${T.clock_device || 'This device'}: ${fmtUtc(now)}`;
        }
        tick();
        if (clockTimer) clearInterval(clockTimer);
        clockTimer = setInterval(tick, 1000);
        button.addEventListener('click', () => save({ [item.key]: new Date().toISOString() }, T.clock_set));
        return el('div', {}, [scope, device, button]);
    }

    function render(settings) {
        sectionsEl.replaceChildren();
        original = {};
        current = {};
        (settings.sections || []).forEach(section => {
            const body = el('div', { class: 'card-body' });
            if (section.note) body.appendChild(el('p', { class: 'text-muted small', text: section.note }));
            (section.items || []).forEach(item => {
                if (item.type !== 'info' && item.type !== 'clock') {
                    original[item.key] = item.value;
                    current[item.key] = item.value;
                }
                const [id, control] = inputFor(item);
                const row = el('div', { class: 'row g-2 align-items-center mb-3' }, [
                    el('div', { class: 'col-sm-4' }, [el('label', { class: 'form-label mb-0 fw-semibold', for: id, text: item.label })]),
                    el('div', { class: 'col-sm-8' }, [control, item.help ? el('div', { class: 'form-text', text: item.help }) : null]),
                ]);
                body.appendChild(row);
            });
            sectionsEl.appendChild(el('div', { class: 'card mb-3' }, [
                el('div', { class: 'card-header' }, [el('strong', { text: section.title })]),
                body,
            ]));
        });
        actionsEl.classList.remove('d-none');
        refreshButtons();
    }

    async function load() {
        status(T.loading || 'Loading...');
        try {
            const res = await fetch(`${url}?telescope_id=${encodeURIComponent(telescopeId)}`, { headers: { Accept: 'application/json' } });
            const data = await res.json();
            if (data.status !== 'success') {
                status(data.message || T.offline, 'warning');
                return;
            }
            render(data.settings || {});
            status('');
        } catch (e) {
            status(T.failed || 'Failed', 'danger');
        }
    }

    async function save(changes, doneText) {
        if (!Object.keys(changes).length) {
            status(T.unchanged || 'No changes', 'secondary');
            return;
        }
        saveBtn.disabled = true;
        status(T.saving || 'Saving...');
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': csrf() },
                body: JSON.stringify({ telescope_id: telescopeId, changes }),
            });
            const data = await res.json();
            if (data.status !== 'success') {
                status(data.message || T.failed, 'danger');
                refreshButtons();
                return;
            }
            const result = data.result || {};
            if (result.settings) render(result.settings);
            const errors = Object.entries(result.errors || {});
            const parts = [];
            if (errors.length) parts.push(`${T.some_failed || 'Not saved:'} ${errors.map(([k, v]) => `${k}: ${v}`).join('; ')}`);
            else parts.push(doneText || T.saved || 'Saved');
            if (result.notice) parts.push(result.notice);
            status(parts.join(' '), errors.length ? 'warning' : (result.notice ? 'info' : 'success'));
        } catch (e) {
            // Renaming the hotspot or changing its password drops this connection
            status(T.failed || 'Failed', 'danger');
            refreshButtons();
        }
    }

    saveBtn.addEventListener('click', () => {
        const changes = {};
        changedKeys().forEach(k => { changes[k] = current[k]; });
        save(changes);
    });
    revertBtn.addEventListener('click', load);
    document.getElementById('ts-reload').addEventListener('click', load);
    load();
})();
