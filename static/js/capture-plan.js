// Capture plan builder: a list of steps the telescope runs in order.
// The server checks the plan (/interface/capture/estimate) as it's built and again on start.
(function () {
    'use strict';

    const num = (key, label, extra = {}) => ({ key, label, type: 'number', ...extra });
    const int = (key, label, extra = {}) => ({ key, label, type: 'number', step: 1, integer: true, ...extra });

    // Fields per step type; `camera` steps also get the camera settings section
    const STEP_TYPES = {
        goto: {
            label: 'Go to',
            fields: [
                { key: 'target', label: 'Target', type: 'text', placeholder: 'Moon, Jupiter… (or RA/Dec below)' },
                { key: 'name', label: 'Name for RA/Dec', type: 'text', placeholder: 'M42' },
                num('ra', 'RA °', { min: 0, max: 360, step: 0.0001 }),
                num('dec', 'Dec °', { min: -90, max: 90, step: 0.0001 }),
                { key: 'centre', label: 'Centre by plate solving', type: 'checkbox' }
            ],
            defaults: { target: 'Moon' }
        },
        bracket: {
            label: 'Bracket (Moon, planets, HDR)',
            camera: true,
            fields: [
                { key: 'exposures_s', label: 'Exposures (s)', type: 'list', placeholder: '0.001, 0.004, 0.016' },
                int('repeat', 'Repeat', { min: 1, max: 100 })
            ],
            defaults: { exposures_s: [0.001, 0.004, 0.016], repeat: 3 }
        },
        light: {
            label: 'Lights',
            camera: true,
            fields: [
                int('count', 'Frames', { min: 1, max: 2000 }),
                num('exposure_s', 'Exposure (s)', { min: 0.0001, max: 900, step: 0.1 }),
                num('interval_s', 'Gap (s)', { min: 0, max: 3600, step: 0.5 }),
                int('dither_every', 'Dither every', { min: 0, max: 2000 }),
                num('dither_arcmin', 'Dither (′)', { min: 0, max: 60, step: 0.5 })
            ],
            defaults: { count: 20, exposure_s: 30 }
        },
        timelapse: {
            label: 'Timelapse',
            camera: true,
            fields: [
                num('every_s', 'Every (s)', { min: 1, max: 3600, step: 1 }),
                num('duration_s', 'For (s)', { min: 1, max: 43200, step: 60 }),
                int('count', 'or frames', { min: 1, max: 2000 }),
                num('exposure_s', 'Exposure (s)', { min: 0.0001, max: 900, step: 0.1 })
            ],
            defaults: { every_s: 10, duration_s: 1800 }
        },
        snapshot: {
            label: 'Snapshots (live view)',
            fields: [
                int('count', 'Frames', { min: 1, max: 2000 }),
                num('interval_s', 'Every (s)', { min: 0, max: 3600, step: 0.5 })
            ],
            labelOnly: true,
            defaults: { count: 10, interval_s: 2 }
        },
        video: {
            label: 'Video (for stacking)',
            fields: [
                num('duration_s', 'Length (s)', { min: 1, max: 600, step: 1 }),
                { key: 'quality', label: 'Quality', type: 'select', options: ['original', 'high', 'medium'] }
            ],
            labelOnly: true,
            defaults: { duration_s: 60, quality: 'original' }
        },
        dark: {
            label: 'Darks (scope covered)',
            camera: true,
            fields: [
                int('count', 'Frames', { min: 1, max: 2000 }),
                num('exposure_s', 'Exposure (s)', { min: 0.0001, max: 900, step: 0.1 })
            ],
            defaults: { count: 20, exposure_s: 30 }
        },
        flat: {
            label: 'Flats (even light)',
            camera: true,
            fields: [
                int('count', 'Frames', { min: 1, max: 2000 }),
                { key: 'exposure_s', label: 'Exposure (s or auto)', type: 'numberOrWord', word: 'auto' }
            ],
            defaults: { count: 20, exposure_s: 0.5 }
        },
        bias: {
            label: 'Bias (scope covered)',
            camera: true,
            fields: [int('count', 'Frames', { min: 1, max: 2000 })],
            defaults: { count: 30 }
        },
        wait: {
            label: 'Wait',
            fields: [{ key: 'wait', type: 'wait' }],
            defaults: { seconds: 600 }
        }
    };

    // Camera settings a step can set, and the getCameraChoices list that offers them
    const CAMERA_FIELDS = [
        { key: 'iso', label: 'ISO', choices: 'iso' },
        { key: 'shutter', label: 'Shutter', choices: 'shutterSpeed' },
        { key: 'aperture', label: 'Aperture', choices: 'aperture' },
        { key: 'white_balance', label: 'White balance', choices: 'whiteBalance' },
        { key: 'image_format', label: 'Format', choices: 'imageFormat' },
        { key: 'exposure_compensation', label: 'Exp. comp.', choices: 'exposureCompensation' }
    ];
    const WAIT_MODES = { seconds: 'For (s)', until: 'Until (local time)', until_alt_deg: 'Until target altitude (°)' };

    let steps = [];
    let savedPlanId = null;
    let estimateTimer = null;
    let dragIndex = null;
    let modal = null;

    const $ = (id) => document.getElementById(id);

    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        Object.entries(attrs).forEach(([key, value]) => {
            if (key === 'className') node.className = value;
            else if (key === 'text') node.textContent = value;
            else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
            else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : value);
        });
        (Array.isArray(children) ? children : [children]).forEach(child => child && node.appendChild(child));
        return node;
    }

    function telescopeId() {
        return typeof selectedTelescopeId !== 'undefined' ? selectedTelescopeId : null;
    }

    function formatDuration(seconds) {
        seconds = Math.round(seconds || 0);
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        if (h) return `${h} h ${m} min`;
        if (m) return `${m} min ${s} s`;
        return `${s} s`;
    }

    function formatSize(bytes) {
        if (typeof formatBytes === 'function') return formatBytes(bytes);
        return `${Math.round((bytes || 0) / 1048576)} MB`;
    }

    // The plan as the server takes it
    function currentPlan() {
        return {
            name: ($('planName').value || '').trim() || 'Untitled plan',
            on_error: $('planOnError').value,
            upload: $('planUpload').value,
            park_at_end: $('planParkAtEnd').checked,
            steps: steps.map(step => {
                const clean = {};
                Object.entries(step).forEach(([key, value]) => {
                    if (value === '' || value === null || value === undefined) return;
                    if (Array.isArray(value) && !value.length) return;
                    clean[key] = value;
                });
                return clean;
            })
        };
    }

    function loadPlan(plan, savedId = null) {
        savedPlanId = savedId;
        $('planName').value = plan.name || 'New plan';
        $('planOnError').value = plan.on_error || 'skip_step';
        $('planUpload').value = plan.upload || 'jpeg_first';
        $('planParkAtEnd').checked = !!plan.park_at_end;
        steps = (plan.steps || []).map(step => ({ ...step }));
        render();
    }

    function addStep(type) {
        steps.push({ type, ...JSON.parse(JSON.stringify(STEP_TYPES[type].defaults || {})) });
        render();
    }

    function parseValue(field, raw) {
        if (field.type === 'checkbox') return raw;
        const text = String(raw).trim();
        if (text === '') return '';
        if (field.type === 'number') {
            const value = field.integer ? parseInt(text, 10) : parseFloat(text);
            return isNaN(value) ? text : value;
        }
        if (field.type === 'list') {
            return text.split(/[,\s]+/).filter(Boolean).map(part => {
                const value = parseFloat(part);
                return isNaN(value) ? part : value;
            });
        }
        if (field.type === 'numberOrWord') {
            if (text.toLowerCase() === field.word) return field.word;
            const value = parseFloat(text);
            return isNaN(value) ? text : value;
        }
        return text;
    }

    function fieldInput(step, field) {
        const id = `plan-${Math.random().toString(36).slice(2)}`;
        const value = step[field.key];
        let input;
        if (field.type === 'checkbox') {
            input = el('input', { type: 'checkbox', className: 'form-check-input', id });
            input.checked = !!value;
            input.addEventListener('change', () => { step[field.key] = input.checked; scheduleEstimate(); });
            return el('div', { className: 'col-12' }, el('div', { className: 'form-check' }, [
                input, el('label', { className: 'form-check-label small', for: id, text: field.label })
            ]));
        }
        if (field.type === 'select' || field.options) {
            input = el('select', { className: 'form-select form-select-sm', id });
            input.appendChild(el('option', { value: '', text: '—' }));
            (field.options || []).forEach(option => input.appendChild(el('option', { value: option, text: option })));
            if (value !== undefined && value !== null && value !== '' && !(field.options || []).map(String).includes(String(value))) {
                input.appendChild(el('option', { value, text: value }));
            }
            input.value = value === undefined || value === null ? '' : String(value);
            input.addEventListener('change', () => { step[field.key] = input.value; scheduleEstimate(); });
        } else {
            const isNumber = field.type === 'number';
            input = el('input', {
                className: 'form-control form-control-sm', id,
                type: isNumber ? 'number' : 'text',
                min: field.min, max: field.max, step: field.step,
                placeholder: field.placeholder || ''
            });
            input.value = Array.isArray(value) ? value.join(', ') : (value === undefined || value === null ? '' : value);
            input.addEventListener('input', () => { step[field.key] = parseValue(field, input.value); scheduleEstimate(); });
        }
        return el('div', { className: 'col-6 col-md-4' }, [
            el('label', { className: 'form-label small mb-0', for: id, text: field.label }), input
        ]);
    }

    function waitInputs(step) {
        const mode = ['until', 'until_alt_deg', 'seconds'].find(key => key in step) || 'seconds';
        const modeSelect = el('select', { className: 'form-select form-select-sm' });
        Object.entries(WAIT_MODES).forEach(([key, label]) => modeSelect.appendChild(el('option', { value: key, text: label })));
        modeSelect.value = mode;

        const valueInput = el('input', { className: 'form-control form-control-sm' });
        const setInputFor = (key) => {
            valueInput.type = key === 'until' ? 'datetime-local' : 'number';
            if (key === 'until') {
                const date = step.until ? new Date(step.until) : null;
                valueInput.value = date && !isNaN(date) ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
            } else {
                valueInput.value = step[key] ?? '';
            }
        };
        setInputFor(mode);

        const store = () => {
            const key = modeSelect.value;
            delete step.seconds; delete step.until; delete step.until_alt_deg;
            if (key === 'until') {
                if (valueInput.value) step.until = new Date(valueInput.value).toISOString().replace(/\.\d{3}Z$/, 'Z');
            } else if (valueInput.value !== '') {
                step[key] = parseFloat(valueInput.value);
            } else {
                step[key] = '';
            }
            scheduleEstimate();
        };
        modeSelect.addEventListener('change', () => { setInputFor(modeSelect.value); valueInput.value = ''; store(); });
        valueInput.addEventListener('input', store);
        return [
            el('div', { className: 'col-6 col-md-5' }, [el('label', { className: 'form-label small mb-0', text: 'Wait' }), modeSelect]),
            el('div', { className: 'col-6 col-md-4' }, [el('label', { className: 'form-label small mb-0', text: 'Value' }), valueInput])
        ];
    }

    function cameraSection(step, index) {
        const choices = window.cameraChoices || {};
        const body = el('div', { className: 'row g-2 mt-1' });
        CAMERA_FIELDS.forEach(field => {
            const offered = Array.isArray(choices[field.choices]) ? choices[field.choices] : null;
            body.appendChild(fieldInput(step, offered && offered.length
                ? { key: field.key, label: field.label, type: 'select', options: offered }
                : { key: field.key, label: field.label, type: 'text', placeholder: 'camera default' }));
        });
        body.appendChild(fieldInput(step, { key: 'label', label: 'Album label', type: 'text', placeholder: 'optional' }));
        const set = CAMERA_FIELDS.filter(f => step[f.key] !== undefined && step[f.key] !== '').length;
        const details = el('details', { className: 'mt-2' }, [
            el('summary', { className: 'small text-muted', text: `Camera settings${set ? ` (${set} set)` : ''}` }),
            body
        ]);
        if (set) details.open = true;
        return details;
    }

    function stepCard(step, index) {
        const spec = STEP_TYPES[step.type] || { label: step.type, fields: [] };
        const card = el('div', { className: 'plan-step', 'data-index': index });

        const handle = el('span', { className: 'step-handle me-2', title: 'Drag to reorder', text: '⋮⋮' });
        handle.addEventListener('mousedown', () => { card.draggable = true; });
        card.addEventListener('dragstart', (event) => {
            dragIndex = index;
            card.classList.add('dragging');
            event.dataTransfer.effectAllowed = 'move';
        });
        card.addEventListener('dragend', () => { card.draggable = false; card.classList.remove('dragging'); });
        card.addEventListener('dragover', (event) => event.preventDefault());
        card.addEventListener('drop', (event) => {
            event.preventDefault();
            if (dragIndex === null || dragIndex === index) return;
            const [moved] = steps.splice(dragIndex, 1);
            steps.splice(index, 0, moved);
            dragIndex = null;
            render();
        });

        const header = el('div', { className: 'd-flex align-items-center' }, [
            handle,
            el('strong', { className: 'small flex-grow-1', text: `${index + 1}. ${spec.label}` }),
            el('button', { type: 'button', className: 'btn btn-outline-secondary btn-sm py-0 me-1', title: 'Duplicate', text: '⧉',
                onclick: () => { steps.splice(index + 1, 0, JSON.parse(JSON.stringify(step))); render(); } }),
            el('button', { type: 'button', className: 'btn btn-outline-danger btn-sm py-0', title: 'Delete', text: '✕',
                onclick: () => { steps.splice(index, 1); render(); } })
        ]);
        card.appendChild(header);

        const fields = el('div', { className: 'row g-2 mt-1' });
        spec.fields.forEach(field => {
            if (field.type === 'wait') waitInputs(step).forEach(node => fields.appendChild(node));
            else fields.appendChild(fieldInput(step, field));
        });
        card.appendChild(fields);

        if (spec.camera) {
            card.appendChild(cameraSection(step, index));
        } else if (spec.labelOnly) {
            const row = el('div', { className: 'row g-2 mt-1' });
            row.appendChild(fieldInput(step, { key: 'label', label: 'Album label', type: 'text', placeholder: 'optional' }));
            card.appendChild(row);
        }
        if (step.type === 'dark' || step.type === 'bias') {
            card.appendChild(el('div', { className: 'small text-muted mt-1',
                text: 'The plan pauses here until you press Cover fitted (or Skip).' }));
        }
        return card;
    }

    function render() {
        const list = $('planSteps');
        list.innerHTML = '';
        steps.forEach((step, index) => list.appendChild(stepCard(step, index)));
        $('planEmpty').classList.toggle('d-none', steps.length > 0);
        scheduleEstimate();
    }

    function scheduleEstimate() {
        clearTimeout(estimateTimer);
        estimateTimer = setTimeout(runEstimate, 400);
    }

    function showErrors(errors) {
        const box = $('planErrors');
        box.innerHTML = '';
        if (!errors || !errors.length) {
            box.classList.add('d-none');
            return;
        }
        const list = el('ul', { className: 'mb-0 ps-3' });
        errors.slice(0, 12).forEach(error => list.appendChild(el('li', { text: error })));
        box.appendChild(list);
        box.classList.remove('d-none');
    }

    function runEstimate() {
        if (!steps.length) {
            $('planEstimate').innerHTML = '<dt>Time</dt><dd>—</dd><dt>Frames</dt><dd>—</dd><dt>Storage</dt><dd>—</dd>';
            showErrors([]);
            return;
        }
        fetch('/interface/capture/estimate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plan: currentPlan(), telescope_id: telescopeId() })
        })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') return;
            const est = data.estimate;
            const grid = $('planEstimate');
            grid.innerHTML = '';
            const rows = [
                ['Time', formatDuration(est.seconds) + (est.unknown_steps ? ' + altitude waits' : '')],
                ['Frames', String(est.frames)],
                ['Storage', formatSize(est.bytes)],
                ['Overhead', `${est.overhead_s} s/frame`]
            ];
            rows.forEach(([label, value]) => grid.append(el('dt', { text: label }), el('dd', { text: value })));
            showErrors(data.errors);
        })
        .catch(() => {});
    }

    function fillMenus() {
        const addMenu = $('addStepMenu');
        addMenu.innerHTML = '';
        Object.entries(STEP_TYPES).forEach(([type, spec]) => {
            addMenu.appendChild(el('li', {}, el('button', { type: 'button', className: 'dropdown-item', text: spec.label, onclick: () => addStep(type) })));
        });

        fetch('/interface/capture/presets').then(r => r.json()).then(data => {
            const menu = $('presetMenu');
            menu.innerHTML = '';
            (data.presets || []).forEach(preset => {
                menu.appendChild(el('li', {}, el('button', { type: 'button', className: 'dropdown-item', text: preset.name,
                    onclick: () => {
                        if (steps.length && !confirm('Replace the current plan with this preset?')) return;
                        loadPlan(preset.plan);
                    } })));
            });
        }).catch(() => {});
        refreshSaved();
    }

    function refreshSaved() {
        fetch('/interface/capture/saved').then(r => r.json()).then(data => {
            const menu = $('savedPlanMenu');
            menu.innerHTML = '';
            const plans = data.plans || [];
            if (!plans.length) {
                menu.appendChild(el('li', {}, el('span', { className: 'dropdown-item-text small text-muted', text: 'No saved plans yet' })));
                return;
            }
            plans.forEach(saved => {
                const row = el('li', { className: 'd-flex align-items-center' }, [
                    el('button', { type: 'button', className: 'dropdown-item flex-grow-1', text: saved.name,
                        onclick: () => {
                            if (steps.length && !confirm(`Replace the current plan with "${saved.name}"?`)) return;
                            loadPlan(saved.plan, saved.id);
                        } }),
                    el('button', { type: 'button', className: 'btn btn-link btn-sm text-danger me-2', title: 'Delete', text: '✕',
                        onclick: (event) => {
                            event.stopPropagation();
                            if (!confirm(`Delete the saved plan "${saved.name}"?`)) return;
                            fetch(`/interface/capture/saved/${saved.id}`, { method: 'DELETE' }).then(refreshSaved);
                            if (savedPlanId === saved.id) savedPlanId = null;
                        } })
                ]);
                menu.appendChild(row);
            });
        }).catch(() => {});
    }

    function save() {
        const plan = currentPlan();
        let id = null;
        if (savedPlanId !== null) {
            id = confirm(`Update your saved plan "${plan.name}"? Cancel saves a new copy.`) ? savedPlanId : null;
        }
        fetch('/interface/capture/saved', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plan, id })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                showErrors(data.errors || [data.message]);
                return;
            }
            savedPlanId = data.plan.id;
            $('planNotice').textContent = `Saved "${data.plan.name}" to your plans.`;
            refreshSaved();
        })
        .catch(error => showErrors([String(error)]));
    }

    function start() {
        const id = telescopeId();
        if (!id) {
            alert('Select a telescope first.');
            return;
        }
        const btn = $('planStartBtn');
        btn.disabled = true;
        $('planNotice').textContent = 'Checking the plan against the camera and sending it…';
        fetch('/interface/capture/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plan: currentPlan(), telescope_id: id })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                $('planNotice').textContent = '';
                showErrors(data.errors || [data.message]);
                return;
            }
            $('planNotice').textContent = '';
            modal.hide();
            if (typeof pollTelescopeStatus === 'function') pollTelescopeStatus();
            if (typeof loadPlanHistory === 'function') loadPlanHistory();
            if (typeof renderPlan === 'function') renderPlan(data.plan);
        })
        .catch(error => showErrors([String(error)]))
        .finally(() => { btn.disabled = false; });
    }

    function open() {
        if (!telescopeId()) {
            alert('Select a telescope first.');
            return;
        }
        if (!modal) {
            modal = bootstrap.Modal.getOrCreateInstance($('captureModal'));
            fillMenus();
            $('planName').addEventListener('input', scheduleEstimate);
        }
        render();
        modal.show();
    }

    window.CapturePlanBuilder = { open, save, start, currentPlan, loadPlan };
})();
