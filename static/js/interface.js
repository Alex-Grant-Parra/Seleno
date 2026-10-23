// Interface page: choosing a telescope, its status, commands, the camera, live view,
// going to targets, the mount, uploads and capture plans. Shared by both modes;
// interface-guide.js adds Basic mode's guided steps on top.
//
// capture-plan.js relies on these globals: selectedTelescopeId, pollTelescopeStatus,
// renderPlan, loadPlanHistory, formatBytes and window.cameraChoices.

const $ = (id) => document.getElementById(id);

let selectedTelescopeId = null;
let selectedMotorId = 'motor1';
let telescopeAccess = { control: false, owner: false };
let telescopeList = [];
let latestStatus = null;          // the last /interface/telescope_status reply
let latestSky = null;             // the last /interface/api/sky reply
let currentTarget = null;         // {name, ra, dec, kind, preset, moving, designation}
let currentPlan = null;
window.cameraChoices = null;

function formatBytes(bytes) {
    return Seleno.fmt.bytes(bytes);
}

function emit(name, detail) {
    document.dispatchEvent(new CustomEvent(name, { detail }));
}

// --- the log ------------------------------------------------------------------

function logLine(text, level = '') {
    const box = $('commandLog');
    if (!box) return;
    const now = new Date();
    const stamp = now.toTimeString().slice(0, 8);
    const line = Seleno.el('div', { className: level }, `${stamp} ${text}`);
    box.appendChild(line);
    while (box.childNodes.length > 200) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
}

function shortResult(value) {
    if (value === undefined || value === null || value === '') return '';
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    return text.length > 240 ? text.slice(0, 240) + '…' : text;
}

// --- commands -------------------------------------------------------------------

function checkAuthResponse(response) {
    if (response.status === 401) {
        Seleno.toast(_('You need to log in again to control the telescope.'), 'bad');
        window.location.href = '/auth/login';
        throw new Error(_('Not authenticated'));
    }
    return response;
}

// Telescope commands (POST /interface/command). Resolves to the result, or rejects
// with the telescope's message. Jobs (park, dither, solve, centre, polar align) are
// polled until they finish.
function telescopeAction(action, params = {}, options = {}) {
    if (!selectedTelescopeId) {
        Seleno.toast(_('Select a telescope first.'), 'warn');
        return Promise.reject(new Error(_('No telescope selected')));
    }
    return fetch('/interface/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, params, telescope_id: selectedTelescopeId })
    })
        .then(checkAuthResponse)
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                logLine(`✗ ${action}: ${data.message || 'failed'}`, 'bad');
                if (!options.quiet) Seleno.toast(`${action.replace(/_/g, ' ')}: ${data.message || _('failed')}`, 'bad');
                throw new Error(data.message || _('{action} failed', { action }));
            }
            if (!options.quiet) logLine(`✓ ${action}${data.result !== undefined ? ' ' + shortResult(data.result) : ''}`, 'ok');
            if (data.job) pollJob(data.job);
            return data.result;
        });
}

function confirmAction(question, action, params = {}) {
    if (confirm(question)) {
        return telescopeAction(action, params).catch(() => {});
    }
    return Promise.resolve();
}

const JOB_LABELS = { park: _('Parking'), dither: _('Dithering'), solve: _('Plate solving'), centre: _('Centring'), polar: _('Polar alignment') };
const runningJobs = new Set();

// Poll getJobStatus every 2 s until the job stops, then show its result or error
function pollJob(name) {
    if (runningJobs.has(name)) return;
    runningJobs.add(name);
    const line = $('jobStatusLine');
    const label = JOB_LABELS[name] || name;
    const tick = () => {
        telescopeAction('job_status', { name }, { quiet: true })
            .then(status => {
                status = status || {};
                if (status.running) {
                    const progress = status.progress !== undefined ? ` (${status.progress})` : '';
                    if (line) line.textContent = `${label}…${progress}`;
                    setTimeout(tick, 2000);
                    return;
                }
                runningJobs.delete(name);
                if (status.error) {
                    if (line) line.textContent = _('{label} failed: {error}', { label, error: status.error });
                    logLine(_('✗ {label} failed: {error}', { label, error: status.error }), 'bad');
                    Seleno.toast(_('{label} failed: {error}', { label, error: status.error }), 'bad');
                } else {
                    if (line) line.textContent = _('{label} done.', { label });
                    logLine(_('✓ {label} done {status}', { label, status: shortResult(status.result) }), 'ok');
                    if (name === 'solve' || name === 'centre') renderSolve(status.result);
                }
                emit('seleno:job', { name, status });
            })
            .catch(() => {
                runningJobs.delete(name);
                if (line) line.textContent = _('{label}: lost track of the job.', { label });
            });
    };
    if (line) line.textContent = `${label}…`;
    logLine(`… ${label}`);
    setTimeout(tick, 2000);
}

function renderSolve(result) {
    const line = $('lastSolveLine');
    if (!line || !result || typeof result !== 'object') return;
    const parts = [];
    const scale = result.pixel_scale ?? result.scale_arcsec_px ?? result.arcsec_per_px;
    if (scale !== undefined) parts.push(`${Number(scale).toFixed(2)}″/px`);
    const fw = result.field_w_deg ?? result.width_deg;
    const fh = result.field_h_deg ?? result.height_deg;
    if (fw !== undefined && fh !== undefined) parts.push(`${Number(fw).toFixed(2)}° × ${Number(fh).toFixed(2)}°`);
    if (result.rotation_deg !== undefined) parts.push(_('rot {rotationDeg}°', { rotationDeg: Number(result.rotation_deg).toFixed(1) }));
    const off = result.offset_arcmin ?? result.error_arcmin;
    if (off !== undefined) parts.push(_('off by {off}′', { off: Number(off).toFixed(1) }));
    line.textContent = parts.length ? _('Last solve {time} · {parts}', { time: Seleno.fmt.time(new Date()), parts: parts.join(' · ') }) : '';
}

// --- choosing a telescope ---------------------------------------------------------

const CONTROL_IDS = ['shutterSpeedSelect', 'isoSelect', 'apertureSelect', 'whiteBalance', 'photoFormat',
    'exposureCompSelect', 'bulbSeconds', 'takePhotoBtn', 'btnAdvTestShot', 'liveViewToggleBtn', 'btnLiveToggleBasic'];

function setControlsEnabled(enabled) {
    CONTROL_IDS.forEach(id => {
        const node = $(id);
        if (node) node.disabled = !enabled;
    });
    setAdvancedControlsEnabled(enabled);
}

function setAdvancedControlsEnabled(enabled) {
    ['motorEnableBtn', 'speedControl', 'stepsControl', 'microstepsControl', 'currentControl', 'accelControl', 'modeControl']
        .forEach(id => { const node = $(id); if (node) node.disabled = !enabled; });
    document.querySelectorAll('#advancedControlsModal .modal-body button').forEach(btn => { btn.disabled = !enabled; });
}

function formatLastSeen(lastSeen) {
    if (!lastSeen) return _('Unknown');
    const diff = Math.max(0, Date.now() / 1000 - lastSeen);
    if (diff < 60) return _('{n} s ago', { n: Math.floor(diff) });
    if (diff < 3600) return _('{n} min ago', { n: Math.floor(diff / 60) });
    if (diff < 86400) return _('{n} h ago', { n: Math.floor(diff / 3600) });
    return _('{n} days ago', { n: Math.floor(diff / 86400) });
}

function loadTelescopes() {
    return fetch('/interface/get_telescopes')
        .then(response => response.json())
        .then(data => {
            if (data.status === 'success') {
                telescopeList = data.telescopes || [];
                populateTelescopeDropdown(telescopeList);
            }
        })
        .catch(() => {});
}

function populateTelescopeDropdown(telescopes) {
    const select = $('telescopeSelect');
    select.innerHTML = '';
    select.appendChild(Seleno.el('option', { value: '' }, _('Select a telescope…')));
    telescopes.forEach(telescope => {
        const id = telescope.telescope_id || 'Unknown';
        const option = Seleno.el('option', { value: id }, `${id} · ${telescope.online ? _('online') : _('offline')}`);
        if (id === selectedTelescopeId) option.selected = true;
        select.appendChild(option);
    });
}

function showTelescopeInfo(id) {
    const telescope = telescopeList.find(t => t.telescope_id === id);
    if (!telescope) return;
    $('telescopeName').textContent = telescope.telescope_id;
    $('telescopeIp').textContent = telescope.ip_address || '—';
    $('telescopeType').textContent = telescope.type || '—';
    $('telescopeLastSeen').textContent = formatLastSeen(telescope.last_seen);
}

function selectTelescope() {
    const value = $('telescopeSelect').value;
    if (!value) {
        selectedTelescopeId = null;
        setControlsEnabled(false);
        return;
    }
    fetch('/interface/select_telescope', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telescope_id: value })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                Seleno.toast(_('Couldn\'t select the telescope: {message}', { message: data.message }), 'bad');
                return;
            }
            const changed = selectedTelescopeId !== value;
            selectedTelescopeId = value;
            if (changed && liveViewPlayer) {
                liveViewPlayer.stop();
                liveViewActive = false;
                setLiveViewButtons(false);
            }
            onTelescopeSelected(data.access);
        })
        .catch(() => Seleno.toast(_("Couldn't select the telescope"), 'bad'));
}

function loadSelectedTelescope() {
    return fetch('/interface/get_selected_telescope')
        .then(response => response.json())
        .then(data => {
            if (data.status === 'success' && data.telescope) {
                const id = data.telescope.telescope_id;
                if (telescopeList.some(t => t.telescope_id === id)) {
                    $('telescopeSelect').value = id;
                    selectedTelescopeId = id;
                    onTelescopeSelected(data.access);
                    return;
                }
            }
            // One telescope only: choose it
            if (!selectedTelescopeId && telescopeList.length === 1) {
                $('telescopeSelect').value = telescopeList[0].telescope_id;
                selectTelescope();
            } else {
                setControlsEnabled(false);
            }
        })
        .catch(() => {});
}

// Called whenever a telescope becomes the selected one
function onTelescopeSelected(access) {
    applyTelescopeAccess(access);
    setControlsEnabled(true);
    showTelescopeInfo(selectedTelescopeId);
    if (!telescopeAccess.control) {
        logLine(_('You can see this telescope, but controlling it needs ownership or a booking that covers now.'), 'warn');
    }
    populateCameraChoices();
    pollTelescopeStatus();
    loadPlanHistory();
    loadPlanets();
    refreshCameraStatus();
    refreshSky();
    pollLatestCaptures();
    emit('seleno:telescope', { id: selectedTelescopeId, access: telescopeAccess });
}

// Owner-only controls (the Device tab, clearing an e-stop) for the owner and admins
function applyTelescopeAccess(access) {
    telescopeAccess = access || { control: false, owner: false };
    document.querySelectorAll('.owner-only').forEach(node => node.classList.toggle('ws-hidden', !telescopeAccess.owner));
    renderAccessChip();
}

function setChip(id, level, text, title) {
    const chip = $(id);
    if (!chip) return;
    chip.querySelector('.ws-dot').className = `ws-dot ${level}`;
    chip.querySelector('span:last-child').textContent = text;
    if (title !== undefined) chip.title = title;
}

function renderAccessChip() {
    if (!selectedTelescopeId) {
        setChip('chipAccess', 'off', _('Choose a telescope'));
        return;
    }
    const booking = latestStatus && latestStatus.booking;
    if (booking && booking.end) {
        setChip('chipAccess', 'ok', _('Your booking until {end}', { end: Seleno.fmt.time(booking.end) }));
    } else if (telescopeAccess.owner) {
        setChip('chipAccess', 'ok', _('Owner'));
    } else if (telescopeAccess.control) {
        setChip('chipAccess', 'ok', _('You can control it'));
    } else {
        setChip('chipAccess', 'warn', _('Viewing only'), _('Controlling it needs ownership or a booking that covers now'));
    }
}

// --- status ----------------------------------------------------------------------

// The Pi pushes a status message every 15 s; the page reads the latest
function statusPart(status, name) {
    const part = status ? status[name] : undefined;
    if (part && typeof part === 'object' && part.error && Object.keys(part).length === 1) {
        return { error: part.error };
    }
    return part;
}

function describeStatus(status, viewers) {
    const rows = [];
    const add = (label, value) => rows.push([label, value]);
    const tracking = statusPart(status, 'tracking');
    if (tracking) {
        if (tracking.error) add(_('Mount'), `⚠ ${tracking.error}`);
        else {
            let text = tracking.parked ? _('Parked') : (stateLabel(tracking.state) || '—');
            const target = targetName(tracking.target);
            if (target) text += ` · ${target}`;
            if (tracking.rate) text += ` · ${stateLabel(tracking.rate)}`;
            add(_('Mount'), text);
        }
    }
    const camera = statusPart(status, 'camera');
    if (camera) {
        add(_('Camera'), camera.error ? `⚠ ${camera.error}` : `${camera.connected ? (camera.model || _('Connected')) : _('Not connected')}`);
        if (!camera.error && camera.temperature_c !== undefined) add(_('Sensor'), `${Number(camera.temperature_c).toFixed(1)} °C`);
    }
    const lv = statusPart(status, 'liveview');
    if (lv) {
        if (lv.error) add(_('Live view'), `⚠ ${lv.error}`);
        else {
            let text = lv.enabled ? (lv.paused ? _('Paused (no viewers)') : _('On')) : _('Off');
            if (lv.enabled && lv.fps !== undefined) text += ` · ${Number(lv.fps).toFixed(1)} fps`;
            if (lv.enabled && lv.kbps !== undefined) text += ` · ${Math.round(lv.kbps)} kbps`;
            add(_('Live view'), text);
        }
    }
    add(_('Watching'), String(viewers ?? 0));
    const recording = statusPart(status, 'recording');
    if (recording && recording.recording) add(_('Recording'), _('{kind} · {frames} frames', { kind: recording.kind || '', frames: recording.frames || 0 }));
    const uploads = statusPart(status, 'uploads');
    if (uploads) {
        add(_('Uploads'), uploads.error ? `⚠ ${uploads.error}` :
            _('{waiting} waiting · {failed} failed{lastError}', { waiting: uploads.waiting ?? 0, failed: uploads.failed ?? 0, lastError: uploads.last_error ? ' · ' + uploads.last_error : '' }));
    }
    const clock = statusPart(status, 'clock');
    if (clock) add(_('Clock'), clock.error ? `⚠ ${clock.error}` : `${clock.trusted ? _('Trusted') : _('Not trusted')}${clock.source ? ' · ' + clock.source : ''}${clock.offset_s !== undefined ? ' · ' + Number(clock.offset_s).toFixed(2) + ' s' : ''}`);
    const disk = status ? status.disk : undefined;
    if (disk !== undefined) add(_('Disk'), typeof disk === 'number' ? _('{size} free', { size: formatBytes(disk) }) : (disk.error ? `⚠ ${disk.error}` : _('{size} free', { size: formatBytes(disk.free_bytes ?? disk.free) })));
    const esp = statusPart(status, 'esp32');
    if (esp) add(_('Controller'), esp.error ? `⚠ ${esp.error}` : `${esp.connected ? _('Connected') : _('Not connected')}${esp.estop ? _(' · E-STOP') : ''}`);
    return rows;
}

function renderChips(data) {
    const status = data.telescope_status;
    if (!status) {
        setChip('chipMount', 'off', _('No status from the telescope yet'));
        setChip('chipCamera', 'off', _('Camera —'));
        return;
    }
    const tracking = statusPart(status, 'tracking');
    const esp = statusPart(status, 'esp32');
    if (esp && esp.estop) {
        setChip('chipMount', 'bad', _('Emergency stop is on'));
    } else if (!tracking || tracking.error) {
        setChip('chipMount', tracking ? 'bad' : 'off', tracking ? _('Mount: {error}', { error: tracking.error }) : _('Mount —'));
    } else if (tracking.parked) {
        setChip('chipMount', 'warn', _('Mount parked'));
    } else {
        const state = tracking.state || 'idle';
        const level = /track/i.test(state) ? 'ok' : /slew/i.test(state) ? 'warn' : 'off';
        const extra = Seleno.mode() === 'advanced' ? [stateLabel(tracking.rate), targetName(tracking.target)].filter(Boolean).join(' · ') : '';
        setChip('chipMount', level, _('Mount {state}{extra}', { state: stateLabel(state), extra: extra ? ' · ' + extra : '' }));
    }
    const camera = statusPart(status, 'camera');
    const battery = camera && (camera.battery ?? camera.battery_pct);
    if (!camera || camera.error) {
        setChip('chipCamera', camera ? 'bad' : 'off', camera ? _('Camera: {error}', { error: camera.error }) : _('Camera —'));
    } else if (!camera.connected) {
        setChip('chipCamera', 'bad', _('Camera not connected'));
    } else {
        const model = Seleno.mode() === 'advanced' ? (camera.model || _('Camera')) : _('Camera ready');
        const batteryText = battery !== undefined && battery !== null ? ` · ${String(battery).replace(/%?$/, '%')}` : '';
        setChip('chipCamera', 'ok', `${model}${batteryText}`);
    }
    const clock = statusPart(status, 'clock');
    if (clock && !clock.error) {
        setChip('chipClock', clock.trusted ? 'ok' : 'warn', _('Clock {source}{offsetS}', { source: clock.source || '', offsetS: clock.offset_s !== undefined ? ' ' + (clock.offset_s >= 0 ? '+' : '') + Number(clock.offset_s).toFixed(2) + ' s' : '' }).trim());
    }
    const disk = status.disk;
    if (disk !== undefined && disk !== null) {
        const free = typeof disk === 'number' ? disk : (disk.free_bytes ?? disk.free);
        if (free !== undefined) setChip('chipDisk', free < 2 * 1024 ** 3 ? 'warn' : 'ok', _('Disk {free} free', { free: formatBytes(free) }));
    }
    const uploads = statusPart(status, 'uploads');
    if (uploads && !uploads.error) {
        const waiting = uploads.waiting ?? 0;
        const held = uploads.waiting_raw ?? uploads.held;
        setChip('chipUploads', uploads.failed ? 'bad' : waiting ? 'warn' : 'ok',
            _('Uploads {waiting} waiting{held}{failed}', { waiting, held: held ? _(' · {held} RAWs held', { held }) : '', failed: uploads.failed ? _(' · {failed} failed', { failed: uploads.failed }) : '' }));
    }
}

function renderTelescopeStatus(data) {
    latestStatus = data;
    const grid = $('telescopeStatusGrid');
    const ageText = data.age_s !== null && data.age_s !== undefined ? _('updated {n} s ago', { n: Math.round(data.age_s) }) : '';
    $('statusAge').textContent = ageText;
    $('statusAgeCard').textContent = ageText;
    grid.innerHTML = '';
    if (!data.telescope_status) {
        grid.append(Seleno.el('dt', {}, '—'), Seleno.el('dd', {}, _('No status from the telescope yet')));
    } else {
        describeStatus(data.telescope_status, data.viewers).forEach(([label, value]) => {
            grid.append(Seleno.el('dt', {}, label), Seleno.el('dd', {}, value));
        });
        const lv = statusPart(data.telescope_status, 'liveview');
        // Live view switched on elsewhere (or before a reload): watch it here too
        if (lv && lv.enabled && !liveViewActive && liveViewPlayer) {
            liveViewActive = true;
            setLiveViewButtons(true);
            liveViewPlayer.start(selectedTelescopeId);
        }
        const recording = statusPart(data.telescope_status, 'recording');
        if (recording && !recording.error) renderRecording(recording);
        const uploads = statusPart(data.telescope_status, 'uploads');
        if (uploads && !uploads.error) renderUploads(uploads);
    }
    renderChips(data);
    renderAccessChip();
    renderPlan(data.plan);
    emit('seleno:status', data);
}

function pollTelescopeStatus() {
    if (!selectedTelescopeId || document.hidden) return;
    fetch(`/interface/telescope_status?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
        .then(response => response.json().then(body => ({ ok: response.ok, body })))
        .then(({ ok, body }) => {
            if (!ok || body.status !== 'success') {
                const grid = $('telescopeStatusGrid');
                grid.innerHTML = '';
                grid.append(Seleno.el('dt', {}, '—'), Seleno.el('dd', {}, body.message || _('Status unavailable')));
                return;
            }
            if (body.access) applyTelescopeAccess(body.access);
            renderTelescopeStatus(body);
        })
        .catch(() => {});
}

// --- capture plans ------------------------------------------------------------------

function planProgressText(plan) {
    const status = plan.last_status || {};
    const steps = plan.steps || [];
    const stepIndex = typeof status.step === 'number' ? status.step : null;
    const parts = [];
    let fraction = null;
    if (stepIndex !== null && steps[stepIndex]) {
        let text = _('Step {stepIndex} of {steps}: {type}', { stepIndex: stepIndex + 1, steps: steps.length, type: steps[stepIndex].type });
        const count = steps[stepIndex].count;
        if (status.frame !== undefined) text += (count ? _(' · frame {frame} of {count}', { frame: status.frame + 1, count }) : _(' · frame {frame}', { frame: status.frame + 1 }));
        parts.push(text);
        fraction = (stepIndex + (count && status.frame !== undefined ? (status.frame + 1) / count : 0)) / steps.length;
    }
    const tail = [];
    if (status.time_left_s !== undefined) {
        tail.push(_('{duration} left', { duration: Seleno.fmt.duration(status.time_left_s) }));
        tail.push(_('ends {time}', { time: Seleno.fmt.time(Date.now() + status.time_left_s * 1000) }));
    }
    const booking = latestStatus && latestStatus.booking;
    if (booking && booking.end) tail.push(_('booking ends {time}', { time: Seleno.fmt.time(booking.end) }));
    if (tail.length) parts.push(tail.join(' · '));
    if (Array.isArray(status.messages) && status.messages.length) parts.push(status.messages[status.messages.length - 1]);
    return { text: parts.join('\n') || _('Starting…'), fraction };
}

function renderPlan(plan) {
    const finished = !plan || ['done', 'stopped', 'failed'].includes((plan.last_status || {}).state || plan.state);
    currentPlan = finished ? null : plan;
    const show = (id, on) => { const node = $(id); if (node) node.classList.toggle('ws-hidden', !on); };
    show('planBox', !!currentPlan);
    show('planIdle', !currentPlan);
    show('planState', !!currentPlan);
    ['planPauseBtn', 'planResumeBtn', 'planSkipBtn', 'planStopBtn', 'planCoverBtn', 'planCoverPrompt'].forEach(id => show(id, false));
    if (currentPlan) {
        const status = plan.last_status || {};
        const state = status.state || plan.state;
        $('planTitle').textContent = plan.name;
        $('planState').querySelector('span:last-child').textContent = stateLabel(state);
        $('planState').querySelector('.ws-dot').className = `ws-dot ${state === 'running' ? 'ok' : state === 'waiting' || state === 'paused' ? 'warn' : 'off'}`;
        const progress = planProgressText(plan);
        $('planProgress').textContent = progress.text;
        $('planProgressBar').style.width = progress.fraction !== null ? `${Math.round(progress.fraction * 100)}%` : '4%';
        const coverWait = state === 'waiting' && status.waiting === 'cover';
        show('planCoverPrompt', coverWait);
        show('planCoverBtn', coverWait);
        show('planPauseBtn', state === 'running');
        show('planResumeBtn', state === 'paused');
        show('planSkipBtn', true);
        show('planStopBtn', true);
    }
    emit('seleno:plan', currentPlan);
}

function planControl(action) {
    if (!selectedTelescopeId) return;
    fetch('/interface/capture/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, telescope_id: selectedTelescopeId })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                Seleno.toast(planFailText(action, data.message || _('unknown error')), 'bad');
                return;
            }
            logLine(`✓ plan ${action}`, 'ok');
            renderPlan(data.plan);
            pollTelescopeStatus();
            loadPlanHistory();
        })
        .catch(error => Seleno.toast(planFailText(action, error), 'bad'));
}

function planFailText(action, error) {
    const v = { error };
    if (action === 'pause') return _("Couldn't pause the plan: {error}", v);
    if (action === 'resume') return _("Couldn't resume the plan: {error}", v);
    if (action === 'skip') return _("Couldn't skip the step: {error}", v);
    if (action === 'stop') return _("Couldn't stop the plan: {error}", v);
    return _("Couldn't change the plan: {error}", v);
}

function loadPlanHistory() {
    const box = $('planHistory');
    if (!box || !selectedTelescopeId) return;
    fetch(`/interface/capture/history?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') return;
            box.innerHTML = '';
            if (!data.plans.length) {
                box.textContent = _('No plans run on this telescope yet.');
                return;
            }
            data.plans.slice(0, 8).forEach(plan => {
                box.appendChild(Seleno.el('div', { className: 'ws-row' }, [
                    Seleno.el('span', {}, plan.name),
                    Seleno.el('span', { className: 'ws-muted' }, `${plan.state}${plan.frames_done ? ' · ' + plan.frames_done + ' frames' : ''}`)
                ]));
            });
        })
        .catch(() => {});
}

// --- the camera ------------------------------------------------------------------

// Camera settings: the select each getCameraChoices list fills, and the field it sets
const CAMERA_SELECTS = [
    { key: 'shutterSpeed', id: 'shutterSpeedSelect' },
    { key: 'iso', id: 'isoSelect' },
    { key: 'aperture', id: 'apertureSelect' },
    { key: 'whiteBalance', id: 'whiteBalance' },
    { key: 'imageFormat', id: 'photoFormat' },
    { key: 'exposureCompensation', id: 'exposureCompSelect' }
];

function populateCameraChoices() {
    if (!selectedTelescopeId) return Promise.resolve(null);
    return fetch(`/interface/get_camera_choices?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
        .then(response => response.json())
        .then(data => {
            if (data && data.status === 'error') {
                logLine(_('Camera settings unavailable: {message}', { message: data.message }), 'warn');
                return null;
            }
            window.cameraChoices = data;
            const current = data.current || {};
            CAMERA_SELECTS.forEach(({ key, id }) => {
                const select = $(id);
                if (!select) return;
                select.innerHTML = '';
                (data[key] || []).forEach(choice => select.appendChild(Seleno.el('option', { value: choice }, String(choice))));
                if (current[key] !== undefined && current[key] !== null) select.value = String(current[key]);
                select.disabled = !(data[key] || []).length;
            });
            updateBulbVisibility();
            emit('seleno:camera', data);
            return data;
        })
        .catch(() => null);
}

// Change one camera setting. Resolves true when the camera took it.
function updateSetting(field, value, options = {}) {
    if (!selectedTelescopeId) {
        Seleno.toast(_('Select a telescope first.'), 'warn');
        return Promise.resolve(false);
    }
    if (value === undefined || value === null || value === '') return Promise.resolve(false);
    return fetch('/interface/update_camera', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value, telescope_id: selectedTelescopeId })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                logLine(`✗ ${field} ${value}: ${data.message || 'not changed'}`, 'bad');
                if (!options.quiet) Seleno.toast(_('Camera setting not changed: {message}', { message: data.message || _('unknown error') }), 'bad');
                return false;
            }
            logLine(`✓ set ${field} ${value}`, 'ok');
            if (window.cameraChoices) {
                window.cameraChoices.current = window.cameraChoices.current || {};
                window.cameraChoices.current[field] = value;
            }
            const select = CAMERA_SELECTS.find(s => s.key === field);
            if (select && $(select.id)) $(select.id).value = String(value);
            updateBulbVisibility();
            return true;
        })
        .catch(() => false);
}

function isBulbShutter(value) {
    return /bulb/i.test(String(value || ''));
}

function updateBulbVisibility() {
    const shutter = $('shutterSpeedSelect');
    $('bulbRow').classList.toggle('ws-hidden', !isBulbShutter(shutter && shutter.value));
}

// Take a photo. exposure_s is for bulb shutters. Resolves to the reply, or rejects.
function takePhoto(options = {}) {
    if (!selectedTelescopeId) {
        Seleno.toast(_('Select a telescope first.'), 'warn');
        return Promise.reject(new Error(_('No telescope selected')));
    }
    const shutter = options.shutter !== undefined ? options.shutter : $('shutterSpeedSelect').value;
    const body = { telescope_id: selectedTelescopeId, shutter };
    if (options.frame_type) body.frame_type = options.frame_type;
    let exposure = options.exposure_s;
    if (exposure === undefined && isBulbShutter(shutter)) exposure = parseFloat($('bulbSeconds').value);
    if (isBulbShutter(shutter)) {
        if (isNaN(exposure) || exposure <= 0 || exposure > 900) {
            Seleno.toast(_('Enter a bulb exposure between 0 and 900 seconds.'), 'warn');
            return Promise.reject(new Error('Bad exposure'));
        }
        body.exposure_s = exposure;
    }
    logLine(`… capturePhoto ${isBulbShutter(shutter) ? exposure + ' s' : shutter || ''}`);
    return fetch('/interface/take_photo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                logLine(`✗ capturePhoto: ${data.message || 'failed'}`, 'bad');
                throw new Error(data.message || _('The photo failed'));
            }
            logLine(`✓ capturePhoto ${shortResult(data.result)}`, 'ok');
            setTimeout(pollLatestCaptures, 4000);
            return data;
        });
}

function refreshCameraStatus() {
    telescopeAction('camera_status', {}, { quiet: true })
        .then(status => {
            if (!status) return;
            const battery = status.battery ?? status.batteryLevel ?? '—';
            const card = status.card_free ?? status.cardFree ?? status.card ?? '—';
            const cardText = typeof card === 'number' ? _('{size} free', { size: formatBytes(card) }) : card;
            $('cameraStatusLine').textContent = _('Battery {battery} · Card {cardText}', { battery, cardText });
        })
        .catch(() => {});
}

// --- live view -------------------------------------------------------------------

let liveViewActive = false;
let liveViewPlayer = null;
let liveHistogram = null;
let liveZoom = 1;
let lastFocus = null;
const focusHistory = [];          // [{t, hfr}] at the current zoom
const FOCUS_HISTORY_MAX = 240;
let focusTimer = 0;

function setLiveMessage(text) {
    const box = $('liveMessage');
    box.textContent = text || '';
    box.classList.toggle('ws-hidden', !text);
}

function setLiveState(level, text) {
    setChip('liveStateChip', level, text);
}

function initializeLiveView() {
    const canvas = $('liveViewCanvas');
    if (!canvas || typeof window.LiveViewPlayer !== 'function') return;
    liveViewPlayer = new window.LiveViewPlayer(canvas, {
        onState: (state, detail) => {
            if (state === 'playing') {
                setLiveMessage('');
                setLiveState('live', _('Live'));
                const chip = $('liveStatsChip');
                if (chip.textContent === '') chip.textContent = detail || '';
                return;
            }
            const messages = {
                connecting: _('Connecting…'),
                waiting: _('Waiting for the telescope…'),
                offline: _('Live view off'),
                error: detail || _('Live view unavailable')
            };
            setLiveState(state === 'error' ? 'bad' : 'off', state === 'connecting' || state === 'waiting' ? _('Connecting') : _('Off'));
            // Keep the last frame showing while the camera restarts
            setLiveMessage(messages[state] || '');
        }
    });

    // Click the picture to zoom there
    canvas.addEventListener('click', (event) => {
        if (!selectedTelescopeId || !liveViewActive) return;
        const rect = canvas.getBoundingClientRect();
        const x = (event.clientX - rect.left) / rect.width;
        const y = (event.clientY - rect.top) / rect.height;
        const level = liveZoom > 1 ? liveZoom : 5;
        setZoomLevel(level, { x, y });
    });

    // Cursor readout (Advanced)
    let lastMove = 0;
    canvas.addEventListener('mousemove', (event) => {
        if (Seleno.mode() !== 'advanced') return;
        const now = performance.now();
        if (now - lastMove < 60) return;
        lastMove = now;
        const rect = canvas.getBoundingClientRect();
        const fx = (event.clientX - rect.left) / rect.width;
        const fy = (event.clientY - rect.top) / rect.height;
        const px = Math.floor(fx * canvas.width), py = Math.floor(fy * canvas.height);
        let rgb = '';
        try {
            const d = canvas.getContext('2d').getImageData(px, py, 1, 1).data;
            rgb = ` · RGB ${d[0]} ${d[1]} ${d[2]}`;
        } catch (error) { /* tainted or empty */ }
        const chip = $('cursorChip');
        chip.textContent = `x ${fx.toFixed(3)} y ${fy.toFixed(3)}${rgb}`;
        chip.classList.remove('ws-hidden');
    });
    canvas.addEventListener('mouseleave', () => $('cursorChip').classList.add('ws-hidden'));

    // Each new frame: measure the stars (twice a second at most) and redraw overlays
    canvas.addEventListener('liveframe', () => {
        const now = performance.now();
        if (now - focusTimer < 450 || document.hidden || !window.FocusMeter) return;
        focusTimer = now;
        const result = window.FocusMeter.measureCanvas(canvas);
        if (result) onFocusMeasured(result);
    });
    window.addEventListener('resize', drawOverlay);
    new ResizeObserver(drawOverlay).observe($('liveFrame'));
}

function initializeLiveHistogram() {
    if (typeof window.HistogramPanel !== 'function' || liveHistogram) return;
    liveHistogram = new window.HistogramPanel({
        canvasId: 'liveHistogramCanvas',
        controlsRootId: 'liveHistogramControls',
        statsId: 'liveHistogramStats',
        emptyMessage: _('Waiting for live view…')
    });
    liveHistogram.setSourceImage($('liveViewCanvas'));
}

function setLiveViewButtons(active) {
    const adv = $('liveViewToggleBtn');
    adv.textContent = active ? _p('button', 'Live view off') : _('Live view on');
    adv.classList.toggle('primary', !active);
    $('btnLiveToggleBasic').textContent = active ? _('Stop live view') : _('Start live view');
    $('liveStatsChip').classList.toggle('ws-hidden', !active);
    $('focusAdvAside').textContent = active ? _('live') : _('live view off');
    if (!active) {
        setLiveState('off', _('Off'));
        setLiveMessage(_('Live view off'));
    }
}

function startLiveView() {
    if (!selectedTelescopeId) {
        Seleno.toast(_('Select a telescope first.'), 'warn');
        return Promise.resolve(false);
    }
    setLiveMessage(_('Starting live view…'));
    return fetch('/interface/start_live_view', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telescope_id: selectedTelescopeId })
    })
        .then(checkAuthResponse)
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                setLiveMessage(_('Couldn\'t start live view: {message}', { message: data.message }));
                logLine(`✗ start_live_view: ${data.message}`, 'bad');
                return false;
            }
            logLine('✓ start_live_view', 'ok');
            liveViewActive = true;
            setLiveViewButtons(true);
            if (liveViewPlayer) liveViewPlayer.start(selectedTelescopeId);
            return true;
        })
        .catch(error => {
            setLiveMessage(_('Live view unavailable'));
            return false;
        });
}

function stopLiveView() {
    return fetch('/interface/stop_live_view', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telescope_id: selectedTelescopeId })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                Seleno.toast(_('Couldn\'t stop live view: {message}', { message: data.message }), 'bad');
                return;
            }
            logLine('✓ stop_live_view', 'ok');
            liveViewActive = false;
            setLiveViewButtons(false);
            if (liveViewPlayer) liveViewPlayer.stop();
        })
        .catch(() => {});
}

function toggleLiveView() {
    return liveViewActive ? stopLiveView() : startLiveView();
}

function setZoomLevel(level, at) {
    level = Number(level) || 1;
    const params = { level };
    if (at) {
        params.x = Number(at.x).toFixed(4);
        params.y = Number(at.y).toFixed(4);
    }
    if (level !== liveZoom) resetFocusHistory();
    liveZoom = level;
    document.querySelectorAll('#zoomSeg button').forEach(btn => btn.classList.toggle('on', Number(btn.dataset.zoom) === level));
    return telescopeAction('set_camera_zoom', params, { quiet: true }).catch(() => {});
}

function toggleFullscreen() {
    const frame = $('liveFrame');
    if (!document.fullscreenElement) {
        frame.requestFullscreen().catch(error => Seleno.toast(_('Full screen isn\'t available: {message}', { message: error.message }), 'warn'));
    } else {
        document.exitFullscreen();
    }
}

function applyDisplayStretch() {
    const amount = Number($('displayStretch').value) / 100;
    const canvas = $('liveViewCanvas');
    canvas.style.filter = amount > 0 ? `brightness(${(1 + 2.5 * amount).toFixed(2)}) contrast(${(1 + 0.8 * amount).toFixed(2)})` : '';
}

// The overlay sits exactly over the picture (which is letterboxed inside the frame)
function drawOverlay() {
    const svg = $('liveOverlay');
    const canvas = $('liveViewCanvas');
    const frame = $('liveFrame');
    if (!svg || !canvas) return;
    const fr = frame.getBoundingClientRect();
    const cr = canvas.getBoundingClientRect();
    svg.setAttribute('viewBox', `0 0 ${fr.width} ${fr.height}`);
    svg.innerHTML = '';
    const mode = Seleno.mode() === 'advanced' ? $('overlaySelect').value : 'basic';
    if (!liveViewActive || mode === 'none') return;
    const left = cr.left - fr.left, top = cr.top - fr.top, w = cr.width, h = cr.height;
    const ns = 'http://www.w3.org/2000/svg';
    const line = (x1, y1, x2, y2, opacity = 0.4) => {
        const l = document.createElementNS(ns, 'line');
        Object.entries({ x1, y1, x2, y2, stroke: '#38bdf8', 'stroke-opacity': opacity, 'stroke-width': 1 }).forEach(([k, v]) => l.setAttribute(k, v));
        svg.appendChild(l);
    };
    const circle = (cx, cy, r, color, dash) => {
        const c = document.createElementNS(ns, 'circle');
        Object.entries({ cx, cy, r, fill: 'none', stroke: color, 'stroke-width': 2 }).forEach(([k, v]) => c.setAttribute(k, v));
        if (dash) c.setAttribute('stroke-dasharray', dash);
        svg.appendChild(c);
    };
    const cx = left + w / 2, cy = top + h / 2;
    if (mode === 'cross' || mode === 'circle' || mode === 'basic') {
        line(cx, top, cx, top + h);
        line(left, cy, left + w, cy);
    }
    if (mode === 'circle') circle(cx, cy, Math.min(w, h) * 0.17, 'rgba(56,189,248,.5)', '5 5');
    if (mode === 'grid') {
        for (let i = 1; i < 3; i++) {
            line(left + (w * i) / 3, top, left + (w * i) / 3, top + h, 0.35);
            line(left, top + (h * i) / 3, left + w, top + (h * i) / 3, 0.35);
        }
    }
    if ((mode === 'stars' || mode === 'basic') && lastFocus && lastFocus.stars.length) {
        const sx = w / lastFocus.width, sy = h / lastFocus.height;
        const stars = mode === 'basic' ? lastFocus.stars.slice(0, 1) : lastFocus.stars;
        stars.forEach(star => circle(left + star.x * sx, top + star.y * sy, Math.max(10, star.hfr * 3 * sx), star.saturated ? '#fbbf24' : '#34d399'));
        if (mode === 'basic') {
            const star = stars[0];
            const label = document.createElementNS(ns, 'text');
            label.setAttribute('x', left + star.x * sx + Math.max(14, star.hfr * 3 * sx) + 6);
            label.setAttribute('y', top + star.y * sy + 4);
            label.setAttribute('fill', '#edf2ff');
            label.setAttribute('font-size', '12.5');
            label.setAttribute('font-weight', '700');
            label.textContent = `HFR ${star.hfr.toFixed(1)} px`;
            svg.appendChild(label);
        }
    }
}

function resetFocusHistory() {
    focusHistory.length = 0;
    renderFocus();
}

function onFocusMeasured(result) {
    lastFocus = result;
    if (result.hfr !== null) {
        focusHistory.push({ t: Date.now(), hfr: result.hfr, fwhm: result.fwhm, peak: result.peak });
        if (focusHistory.length > FOCUS_HISTORY_MAX) focusHistory.shift();
    }
    const clip = $('liveClipStats');
    if (clip) clip.textContent = _('Sky at {background}% · clipped {clippedPct}% · {stars} stars measured', { background: Math.round(result.background * 100), clippedPct: result.clippedPct.toFixed(2), stars: result.stars.length });
    renderFocus();
    drawOverlay();
    emit('seleno:focus', { result, history: focusHistory });
}

function focusBest() {
    let best = null;
    focusHistory.forEach((entry, i) => { if (best === null || entry.hfr < focusHistory[best].hfr) best = i; });
    return best;
}

function renderFocus() {
    const values = focusHistory.map(e => e.hfr);
    const best = focusBest();
    const series = [{ values, color: 'var(--accent)', marks: best !== null ? [best] : [], markColor: '#34d399' }];
    Seleno.sparkline($('focusSparkAdv'), series, { width: 300, height: 64, lines: best !== null ? [{ value: values[best], color: '#34d399' }] : [] });
    const now = focusHistory.length ? focusHistory[focusHistory.length - 1] : null;
    $('focusKvHfr').textContent = now ? `${now.hfr.toFixed(2)} / ${values[best].toFixed(2)} px` : '—';
    $('focusKvFwhm').textContent = now && now.fwhm ? `${now.fwhm.toFixed(2)} px` : '—';
    $('focusKvPeak').textContent = now && now.peak !== null ? `${Math.round(now.peak * 100)}%${now.peak >= 0.98 ? _(' (clipped)') : ''}` : '—';
}

function startRecording() {
    const kind = $('recordingKind').value;
    const params = { kind };
    if (kind === 'timelapse') params.interval_s = parseFloat($('recordingInterval').value) || 5;
    telescopeAction('start_recording', params).then(refreshRecordingStatus).catch(() => {});
}

function refreshRecordingStatus() {
    return telescopeAction('recording_status', {}, { quiet: true })
        .then(status => renderRecording(status))
        .catch(() => {});
}

function renderRecording(status) {
    const line = $('recordingStatus');
    if (!line || !status) return;
    if (status.error && !status.recording) {
        line.textContent = _('Last recording error: {error}', { error: status.error });
    } else if (status.recording) {
        const seconds = status.seconds !== undefined ? ` · ${Math.round(status.seconds)} s` : '';
        line.textContent = _('● Recording {kind}: {frames} frames{seconds}', { kind: status.kind || '', frames: status.frames || 0, seconds });
    } else {
        line.textContent = status.path ? _('Saved {path}', { path: status.path }) : '';
    }
    $('btnRecord').classList.toggle('danger', !!status.recording);
}

function refreshLiveViewStats() {
    if (!liveViewActive) return;
    telescopeAction('liveview_stats', {}, { quiet: true })
        .then(stats => {
            if (!stats) return;
            const parts = [];
            if (stats.codec) parts.push(String(stats.codec).toUpperCase());
            if (stats.width && stats.height) parts.push(`${stats.width}×${stats.height}`);
            if (stats.fps !== undefined) parts.push(`${Number(stats.fps).toFixed(1)} fps`);
            const kbps = stats.kbps ?? stats.bitrate_kbps;
            if (kbps !== undefined) parts.push(`${(kbps / 1000).toFixed(1)} Mbps`);
            if (latestStatus && latestStatus.viewers !== undefined) parts.push(_('{n} watching', { n: latestStatus.viewers }));
            $('liveStatsChip').textContent = parts.join(' · ');
        })
        .catch(() => {});
}

// --- targets, searching and tracking ---------------------------------------------------

const TARGET_KEY = 'seleno_target';

function setCurrentTarget(target, options = {}) {
    currentTarget = target || null;
    try {
        if (currentTarget) sessionStorage.setItem(TARGET_KEY, JSON.stringify(currentTarget));
        else sessionStorage.removeItem(TARGET_KEY);
    } catch (error) { /* private window */ }
    if (!options.quiet) refreshSky();
    emit('seleno:target', currentTarget);
}

function refreshSky() {
    if (!selectedTelescopeId) return Promise.resolve(null);
    const params = new URLSearchParams({ telescope_id: selectedTelescopeId });
    if (currentTarget) {
        if (currentTarget.moving || (currentTarget.ra === undefined || currentTarget.ra === null)) params.set('target', currentTarget.designation || currentTarget.name);
        else {
            params.set('ra', currentTarget.ra);
            params.set('dec', currentTarget.dec);
            params.set('label', currentTarget.designation || currentTarget.name || '');
        }
    }
    return Seleno.api(`/interface/api/sky?${params}`)
        .then(data => {
            latestSky = data;
            renderSky(data);
            emit('seleno:sky', data);
            return data;
        })
        .catch(error => {
            if (error.status !== 404) logLine(_('Sky: {message}', { message: error.message }), 'warn');
            return null;
        });
}


// The server names the sun's state in English; show it in the page's language
function twilightLabel(state) {
    const labels = {
        'day': _('Day'),
        'civil twilight': _('Civil twilight'),
        'nautical twilight': _('Nautical twilight'),
        'astronomical twilight': _('Astronomical twilight'),
        'astronomical night': _('Astronomical night'),
    };
    return labels[state] || state;
}

function renderSky(data) {
    const kv = $('advSkyKv');
    kv.innerHTML = '';
    const add = (label, value) => kv.append(Seleno.el('dt', {}, label), Seleno.el('dd', {}, value));
    if (!data || !data.site) {
        setChip('chipSky', 'off', _('Site unknown'), data && data.message);
        add(_('Sky'), data && data.message ? data.message : '—');
        return;
    }
    const night = data.night || {};
    const sunAlt = data.sun.alt;
    const nightEnd = night.end ? Seleno.fmt.time(night.end) : null;
    const nightStart = night.start ? Seleno.fmt.time(night.start) : null;
    let skyText;
    if (data.dark) skyText = nightEnd ? _('Dark sky · night until {nightEnd}', { nightEnd }) : _('Dark sky');
    else if (nightStart) skyText = _('{state} · dark from {nightStart}', { state: twilightLabel(data.sun.state), nightStart });
    else skyText = _('No full darkness tonight');
    if (Seleno.mode() === 'advanced') skyText = _('Sun {sunAlt}° · {state}', { sunAlt: sunAlt.toFixed(0), state: twilightLabel(data.sun.state) });
    setChip('chipSky', data.dark ? 'ok' : sunAlt < -6 ? 'warn' : 'bad', skyText);

    const target = data.target;
    if (target) {
        add(_('Target'), target.name || '—');
        add(_('Alt / Az'), `${target.alt.toFixed(1)}° / ${target.az.toFixed(1)}°`);
        add(_('Transit'), target.transit ? `${Seleno.fmt.time(target.transit)} · ${target.transit_alt.toFixed(1)}°` : '—');
        add(_('Above 30°'), target.above_from ? `${Seleno.fmt.time(target.above_from)} – ${Seleno.fmt.time(target.above_to)}` : _('Not tonight'));
        add(_('Moon'), _('{illumination}% · {moonSeparation}° away', { illumination: Math.round(data.moon.illumination * 100), moonSeparation: target.moon_separation.toFixed(0) }));
    } else {
        add(_('Moon'), _('{illumination}% lit · {alt}', { illumination: Math.round(data.moon.illumination * 100), alt: data.moon.alt > 0 ? _('up, {alt}°', { alt: data.moon.alt.toFixed(0) }) : _('below the horizon') }));
    }
    add(_('Astro dark'), nightStart && nightEnd ? `${nightStart} – ${nightEnd}` : _('None tonight'));
}

function extractCommonName(commonNames) {
    if (!commonNames) return '';
    for (const name of commonNames.split(',').map(p => p.trim())) {
        const upper = name.toUpperCase();
        if (upper.startsWith('HD') || upper.startsWith('NGC') || upper.startsWith('IC') ||
            (upper.startsWith('M') && /^\d+$/.test(name.slice(1).replace(/\s/g, '')))) continue;
        return name;
    }
    return '';
}

// Search the catalogue; resolves to the result object, or null
function searchCatalogue(text) {
    return fetch('/interface/search_object', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ searchValue: text })
    })
        .then(response => response.json())
        .then(data => data.status === 'success' && data.data ? data.data : null);
}

function searchResultTarget(star) {
    const name = star.name || star.Name || _('Unknown');
    const moving = ['sun', 'moon', 'mercury', 'venus', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune', 'pluto'].includes(String(name).toLowerCase());
    return {
        name,
        designation: name,
        common: star.friendlyName || extractCommonName(star.commonNames || star['Common names']) || '',
        ra: Number(star.ra ?? star.RA),
        dec: Number(star.dec ?? star.DEC),
        mag: star.mag ?? star['V-Mag'],
        moving,
        details: star
    };
}

function renderSearchResult(box, star, onPick) {
    box.innerHTML = '';
    const target = searchResultTarget(star);
    const details = Seleno.el('details', {}, [Seleno.el('summary', { className: 'ws-small ws-muted' }, _('Everything the catalogue says'))]);
    const table = Seleno.el('table');
    Object.entries(star).forEach(([key, value]) => {
        if (value === null || value === undefined || value === '') return;
        table.appendChild(Seleno.el('tr', {}, [Seleno.el('td', { className: 'ws-muted' }, key),
            Seleno.el('td', {}, typeof value === 'number' && !Number.isInteger(value) ? value.toFixed(4) : String(value))]));
    });
    details.appendChild(table);
    box.appendChild(Seleno.el('div', { className: 'ws-card', style: { padding: '10px 12px' } }, [
        Seleno.el('div', { className: 'ws-strong' }, `${target.name}${target.common ? ' · ' + target.common : ''}`),
        Seleno.el('div', { className: 'ws-small ws-muted' }, _('RA {ra} · Dec {dec}{mag}', { ra: Seleno.fmt.ra(target.ra), dec: Seleno.fmt.dec(target.dec), mag: target.mag !== undefined && target.mag !== null ? ' · mag ' + Number(target.mag).toFixed(1) : '' })),
        Seleno.el('div', { className: 'ws-actions', style: { margin: '8px 0 4px' } }, onPick(target)),
        details
    ]));
}

function searchObject() {
    const text = $('searchObject').value.trim();
    const box = $('searchResult');
    if (!text) return;
    box.textContent = _('Searching…');
    searchCatalogue(text).then(star => {
        if (!star) {
            box.textContent = _('Nothing called “{text}” in the catalogue.', { text });
            return;
        }
        renderSearchResult(box, star, target => [
            Seleno.el('button', { className: 'ws-btn sm primary', type: 'button', onclick: () => goToTarget(target) }, _('Go to')),
            Seleno.el('button', { className: 'ws-btn sm', type: 'button', onclick: () => goToTarget(target, { centre: true }) }, _('Go to + centre')),
            Seleno.el('button', { className: 'ws-btn sm', type: 'button', onclick: () => {
                $('manualRA').value = Seleno.fmt.ra(target.ra);
                $('manualDEC').value = Seleno.fmt.dec(target.dec);
                setCurrentTarget(target);
            } }, _('Use as target'))
        ]);
    }).catch(() => { box.textContent = _('The search failed.'); });
}

// Send the mount to a target. Moon and planets go by name (the Pi works out where
// they are from its own site); everything else by coordinates.
function goToTarget(target, options = {}) {
    if (!target) return Promise.resolve(false);
    // track_name: featured targets' English name (name may be translated)
    const go = target.moving ? trackPlanet(target.track_name || target.name) : trackObject(target);
    return go.then(ok => {
        if (!ok) return false;
        setCurrentTarget(target);
        if (options.centre) telescopeAction('centre_target', { source: solveSource() }).catch(() => {});
        return true;
    });
}

function trackObject(star) {
    const name = star.track_name || star.name || star.Name || 'Target';  // sent to the telescope
    const shown = star.name || name;
    const ra = star.ra ?? star.RA;
    const dec = star.dec ?? star.DEC;
    const mag = star.mag ?? star['V-Mag'] ?? null;
    if (ra === undefined || ra === null || dec === undefined || dec === null || isNaN(ra) || isNaN(dec)) {
        Seleno.toast(_('That target has no coordinates to go to.'), 'bad');
        return Promise.resolve(false);
    }
    logLine(_('… go to {name} (RA {ra}°, Dec {dec}°)', { name: shown, ra: Number(ra).toFixed(3), dec: Number(dec).toFixed(3) }));
    return fetch('/track_star', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, ra, dec, mag })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'tracking') {
                logLine(_('✓ tracking {name}', { name: shown }), 'ok');
                updateTrackingPanel({ name: shown, ra, dec, mag });
                return true;
            }
            const message = data.message || data.error || _('The go-to failed');
            logLine(_('✗ go to {name}: {message}', { name: shown, message }), 'bad');
            Seleno.toast(message, 'bad');
            return false;
        })
        .catch(error => {
            Seleno.toast(_('The go-to failed: {error}', { error }), 'bad');
            return false;
        });
}

function trackPlanet(name) {
    return telescopeAction('track_object', { name })
        .then(result => {
            updateTrackingPanel({ name, ra: result && result.ra, dec: result && result.dec });
            return true;
        })
        .catch(() => false);
}

function trackManualCoordinates(centre) {
    const ra = Seleno.parseRA($('manualRA').value);
    const dec = Seleno.parseDec($('manualDEC').value);
    if (ra === null || dec === null) {
        Seleno.toast(_('Enter RA as h m s (or degrees) and Dec as ° ′ ″.'), 'warn');
        return;
    }
    const target = { name: `RA ${Seleno.fmt.ra(ra)} Dec ${Seleno.fmt.dec(dec)}`, ra, dec, mag: null };
    goToTarget(target, { centre });
}

function updateTrackingPanel(tracking) {
    const line = $('trackingNow');
    const stop = $('stopTrackingBtn');
    if (tracking && tracking.name) {
        line.textContent = _('Tracking {name}', { name: tracking.name });
        stop.classList.remove('ws-hidden');
        try { sessionStorage.setItem('currentTracking', JSON.stringify(tracking)); } catch (error) { /* ignore */ }
    } else {
        line.textContent = _('Not tracking anything');
        stop.classList.add('ws-hidden');
        try { sessionStorage.removeItem('currentTracking'); } catch (error) { /* ignore */ }
    }
    emit('seleno:tracking', tracking || null);
}

function stopTracking() {
    if (!selectedTelescopeId) return;
    fetch('/stop_tracking', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'stopped') {
                logLine(_('✓ stopped tracking'), 'ok');
            } else {
                logLine(_('✗ stop tracking: {message}', { message: data.message || _('failed') }), 'bad');
            }
            updateTrackingPanel(null);
        })
        .catch(() => updateTrackingPanel(null));
}

function loadTrackingStatus() {
    try {
        const stored = sessionStorage.getItem('currentTracking');
        if (stored) updateTrackingPanel(JSON.parse(stored));
        const target = sessionStorage.getItem(TARGET_KEY);
        if (target) setCurrentTarget(JSON.parse(target), { quiet: true });
    } catch (error) { /* ignore */ }
    fetch('/get_tracking_status')
        .then(response => response.json())
        .then(data => {
            if (data.tracking && data.object) {
                updateTrackingPanel(data.object);
                if (!currentTarget && data.object.ra !== undefined) setCurrentTarget(data.object, { quiet: true });
            }
        })
        .catch(() => {});
}

// Planet names stay English as values (the Pi expects them); only the label is translated
// Words the telescope and server send as plain English values (mount state, tracking
// rate, plan state, frame type). The value stays as it is; only what's shown changes.
function stateLabel(word) {
    if (!word || typeof word !== 'string') return word;
    const labels = {
        idle: _('idle'), tracking: _('tracking'), slewing: _('slewing'), parked: _('parked'), parking: _('parking'),
        stopped: _('stopped'), homing: _('homing'), error: _('error'),
        sidereal: _('sidereal'), lunar: _('lunar'), solar: _('solar'), king: _('king'), custom: _('custom'),
        running: _('running'), waiting: _('waiting'), paused: _('paused'), done: _('done'), failed: _('failed'),
        starting: _('starting'), queued: _('queued'),
        light: _('light'), dark: _('dark'), flat: _('flat'), bias: _('bias'), test: _('test shot'),
        snapshot: _('snapshot'), video: _('video'),
    };
    return labels[word.toLowerCase()] || word;
}

// The Pi may send the target as {name, ra, dec, ...} rather than a name
function targetName(target) {
    if (target && typeof target === 'object') return target.name || target.designation || '';
    return target || '';
}

function planetLabel(name) {
    const labels = {
        Sun: _('Sun'), Moon: _('Moon'), Mercury: _('Mercury'), Venus: _('Venus'), Mars: _('Mars'),
        Jupiter: _('Jupiter'), Saturn: _('Saturn'), Uranus: _('Uranus'), Neptune: _('Neptune'), Pluto: _('Pluto'),
    };
    return labels[name] || name;
}

function loadPlanets() {
    // The planet list from the Pi, with altitudes, when it offers one
    telescopeAction('get_planets', {}, { quiet: true })
        .then(result => {
            const names = Array.isArray(result) ? result : (result && Array.isArray(result.planets) ? result.planets : null);
            if (!names || !names.length) return;
            const select = $('planetSelect');
            select.innerHTML = '';
            names.forEach(entry => {
                const name = typeof entry === 'string' ? entry : entry.name;
                if (!name) return;
                const label = planetLabel(name);
                select.appendChild(Seleno.el('option', { value: name },
                    typeof entry === 'object' && entry.alt_deg !== undefined ? `${label} · ${Number(entry.alt_deg).toFixed(0)}°` : label));
            });
        })
        .catch(() => {});
}

// --- the mount ---------------------------------------------------------------------

function emergencyStopMount() {
    telescopeAction('estop')
        .then(() => {
            logLine(_('E-STOP sent'), 'bad');
            Seleno.toast(_('Emergency stop sent: every motor is stopping.'), 'bad', 7000);
        })
        .catch(() => {});
}

function clearEstop() {
    confirmAction(_('Clear the emergency stop? Make sure nothing is in the way of the mount.'), 'clear_estop');
}

function solveSource() {
    return $('solveSource').value || null;
}

function setTrackingRate() {
    const rate = $('trackingRateSelect').value;
    const params = { rate };
    if (rate === 'custom') {
        const custom = parseFloat($('customRateInput').value);
        if (isNaN(custom)) {
            Seleno.toast(_('Enter a custom rate in arcseconds per second.'), 'warn');
            return;
        }
        params.custom_arcsec_per_s = custom;
    }
    telescopeAction('set_tracking_rate', params).catch(() => {});
}

function setHorizon() {
    telescopeAction('set_horizon', {
        min_alt_deg: $('horizonMinAlt').value,
        profile: $('horizonProfile').value,
        stop_when_set: $('horizonStopWhenSet').checked
    }).catch(() => {});
}

// --- uploads -----------------------------------------------------------------------

function refreshUploadQueue() {
    if (!selectedTelescopeId) return Promise.resolve();
    return telescopeAction('upload_queue', {}, { quiet: true })
        .then(queue => renderUploads(queue))
        .catch(() => {});
}

function renderUploads(uploads) {
    const box = $('uploadQueueSummary');
    if (!box || !uploads) return;
    if (uploads.error && Object.keys(uploads).length === 1) {
        box.textContent = _('Uploads: {error}', { error: uploads.error });
        return;
    }
    const waiting = uploads.waiting ?? (Array.isArray(uploads.queue) ? uploads.queue.length : undefined);
    let text = [_('Waiting {waiting}', { waiting: waiting ?? '—' }), _('RAWs held {held}', { held: uploads.waiting_raw ?? uploads.held ?? '—' }),
        _('Failed {failed}', { failed: uploads.failed ?? '—' }), _('Sent {sent}', { sent: uploads.sent ?? '—' })].join(' · ');
    if (uploads.current) text += _('\nSending {filename}', { filename: typeof uploads.current === 'string' ? uploads.current : (uploads.current.filename || '') });
    if (uploads.last_error) text += _('\nLast error: {lastError}', { lastError: uploads.last_error });
    box.textContent = text;
}

// --- the newest frames ---------------------------------------------------------------

let latestFrames = [];

function pollLatestCaptures() {
    if (!selectedTelescopeId || document.hidden) return Promise.resolve([]);
    return Seleno.api(`/interface/api/latest_captures?telescope_id=${encodeURIComponent(selectedTelescopeId)}&limit=12`)
        .then(data => {
            latestFrames = data.frames || [];
            renderLastFrame(latestFrames[0]);
            emit('seleno:captures', latestFrames);
            return latestFrames;
        })
        .catch(() => []);
}

function renderLastFrame(frame) {
    $('lastFrameEmpty').classList.toggle('ws-hidden', !!frame);
    $('lastFrameBody').classList.toggle('ws-hidden', !frame);
    if (!frame) return;
    $('lastFrameThumb').src = frame.thumb;
    $('lastFrameName').textContent = frame.name;
    $('lastFrameSub').textContent = [stateLabel(frame.frame_type), frame.exposure_s ? Seleno.fmt.exposure(frame.exposure_s) : '', frame.iso ? _('ISO {iso}', { iso: frame.iso }) : '',
        frame.uploaded_at ? Seleno.fmt.time(frame.uploaded_at) : ''].filter(Boolean).join(' · ');
    $('lastFrameLink').href = `/album?frame=${encodeURIComponent(frame.name)}`;
    const kv = $('lastFrameKv');
    kv.innerHTML = '';
    const m = frame.metrics;
    const add = (label, value) => kv.append(Seleno.el('dt', {}, label), Seleno.el('dd', {}, value));
    if (m) {
        add(_('Stars'), String(m.stars ?? '—'));
        add(_('FWHM / HFR'), `${Seleno.fmt.num(m.fwhm)} / ${Seleno.fmt.num(m.hfr)} px`);
        add(_('Eccentricity'), Seleno.fmt.num(m.ecc));
        add(_('Background / noise'), `${Seleno.fmt.num(m.background_pct, 1)}% / ${Seleno.fmt.num(m.noise_pct, 2)}%`);
        add(_('Clipped'), `${Seleno.fmt.num(m.clipped_pct, 2)}%`);
    } else if (frame.pixel_stats) {
        add(_('Sky level'), `${Math.round(frame.pixel_stats.background * 100)}%`);
        add(_('Clipped'), `${frame.pixel_stats.clippedPct.toFixed(2)}%`);
    } else {
        add(_('Measured'), _('Not yet'));
    }
    add('RAW', frame.has_raw ? _('Uploaded') : _('On the telescope'));
}

// --- motor controls (the modal) --------------------------------------------------------

let motorEnabled = false;

function openAdvancedControlsModal() {
    if (!selectedTelescopeId) {
        Seleno.toast(_('Select a telescope first.'), 'warn');
        return;
    }
    bootstrap.Modal.getOrCreateInstance($('advancedControlsModal')).show();
    loadAvailableMotors();
    updateMotorStatusDisplay(_('Motor controls open, ready for commands'), 'info');
    updateMotorEnableButton();
}

function loadAvailableMotors() {
    if (!selectedTelescopeId) return;
    fetch('/interface/get_motors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telescopeId: selectedTelescopeId })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success' || !data.motors) return;
            const select = $('motorSelect');
            select.innerHTML = '';
            data.motors.forEach(id => {
                const label = id === 'motor1' ? _('Motor 1 (azimuth)') : id === 'motor2' ? _('Motor 2 (altitude)') : id.charAt(0).toUpperCase() + id.slice(1);
                const option = Seleno.el('option', { value: id }, label);
                if (id === selectedMotorId) option.selected = true;
                select.appendChild(option);
            });
        })
        .catch(() => {});
}

function updateSelectedMotor() {
    selectedMotorId = $('motorSelect').value;
    updateMotorStatusDisplay(_('Switched to {selectedMotorId}', { selectedMotorId }), 'info');
    getMotorStatus();
}

function emergencyStop() {
    if (confirm(_('Stop all motor movement now?'))) {
        sendMotorCommand('stop').then(() => updateMotorStatusDisplay(_('EMERGENCY STOP SENT'), 'error')).catch(() => {});
    }
}

function updateMotorEnableButton() {
    const btn = $('motorEnableBtn');
    if (!btn) return;
    btn.textContent = motorEnabled ? _('Disable') : _('Enable');
    btn.className = motorEnabled ? 'btn btn-outline-danger btn-sm' : 'btn btn-outline-success btn-sm';
}

function sendMotorCommand(command, args = null) {
    if (!selectedTelescopeId) {
        Seleno.toast(_('Select a telescope first.'), 'warn');
        return Promise.reject(new Error(_('No telescope selected')));
    }
    updateMotorStatusDisplay(_('Running {command} on {selectedMotorId}…', { command, selectedMotorId }), 'info');
    return fetch('/interface/motor_command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telescopeId: selectedTelescopeId, command, args, motor_id: selectedMotorId })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') throw new Error(data.message || _('Motor command failed'));
            updateMotorStatusDisplay(_('{command} done on {motor}', { command, motor: selectedMotorId }), 'success');
            logLine(`✓ motor ${command} ${selectedMotorId}`, 'ok');
            return data.result;
        })
        .catch(error => {
            updateMotorStatusDisplay(_('{command} failed on {motor}: {error}', { command, motor: selectedMotorId, error: error.message }), 'error');
            throw error;
        });
}

function updateMotorStatusDisplay(message, type = 'info') {
    const box = $('motorStatus');
    if (!box) return;
    const colours = { success: 'text-success', error: 'text-danger', warning: 'text-warning', info: 'text-info' };
    const line = Seleno.el('div', { className: colours[type] || '' }, `[${new Date().toLocaleTimeString()}] ${message}`);
    const lines = box.querySelectorAll('div');
    if (lines.length >= 10) box.removeChild(lines[0]);
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
}

function toggleMotorEnable() {
    const next = !motorEnabled;
    sendMotorCommand('enable', [next]).then(() => { motorEnabled = next; updateMotorEnableButton(); }).catch(() => {});
}

function setDirection(forward, button) {
    sendMotorCommand('set_direction', [forward]).then(() => {
        document.querySelectorAll('[onclick^="setDirection"]').forEach(btn => btn.classList.remove('active'));
        if (button) button.classList.add('active');
    }).catch(() => {});
}

function readNumber(id, label, { min = 0, integer = false } = {}) {
    const value = integer ? parseInt($(id).value, 10) : parseFloat($(id).value);
    if (isNaN(value) || value < min) {
        Seleno.toast(_('Enter a valid value for {label}.', { label }), 'warn');
        return null;
    }
    return value;
}

function setSpeed() {
    const speed = readNumber('speedControl', _('speed'));
    if (speed !== null) sendMotorCommand('set_speed', [speed]).catch(() => {});
}

function startMotor() {
    if (!motorEnabled) {
        Seleno.toast(_('Enable the motor first.'), 'warn');
        return;
    }
    const speed = readNumber('speedControl', _('speed'));
    if (speed === null) return;
    if (speed > 1000 && !confirm(_('{speed} steps/s is fast. Carry on?', { speed }))) return;
    sendMotorCommand('start', [speed]).catch(() => {});
}

function stopMotor() {
    sendMotorCommand('stop').catch(() => {});
}

function moveSteps() {
    if (!motorEnabled) {
        Seleno.toast(_('Enable the motor first.'), 'warn');
        return;
    }
    const steps = readNumber('stepsControl', _('number of steps'), { min: 1, integer: true });
    const speed = readNumber('speedControl', _('speed'), { min: 0.0001 });
    if (steps === null || speed === null) return;
    if (steps > 10000 && !confirm(_('Move {steps} steps? That\'s a large movement.', { steps }))) return;
    sendMotorCommand('move_steps', [steps, speed]).catch(() => {});
}

function setMicrosteps() {
    sendMotorCommand('set_microsteps', [parseInt($('microstepsControl').value, 10)]).catch(() => {});
}

function setCurrent() {
    const current = readNumber('currentControl', _('current'), { integer: true });
    if (current !== null) sendMotorCommand('set_current', [current]).catch(() => {});
}

function setAcceleration() {
    const accel = readNumber('accelControl', _('acceleration'));
    if (accel !== null) sendMotorCommand('set_accel', [accel]).catch(() => {});
}

function setMode() {
    sendMotorCommand('set_mode', [$('modeControl').value]).catch(() => {});
}

function getMotorStatus() {
    sendMotorCommand('status')
        .then(result => {
            const box = $('motorStatus');
            if (!result || typeof result !== 'object') return;
            box.replaceChildren(Seleno.el('strong', {}, _('Motor status ({selectedMotorId}):', { selectedMotorId })), Seleno.el('br'));
            Object.entries(result).forEach(([key, value]) => {
                box.append(Seleno.el('span', { className: 'text-muted' }, `${key}:`), ` ${JSON.stringify(value)}`, Seleno.el('br'));
            });
        })
        .catch(() => {});
}

function getAllMotorStatus() {
    if (!selectedTelescopeId) return;
    fetch('/interface/motor_command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telescopeId: selectedTelescopeId, command: 'status_all' })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success' || !data.result) return;
            const box = $('motorStatus');
            box.replaceChildren(Seleno.el('strong', {}, _('All motors:')));
            Object.entries(data.result).forEach(([motor, status]) => {
                const block = Seleno.el('div', { className: 'mb-2' }, [Seleno.el('strong', {}, `${motor}:`)]);
                Object.entries(status || {}).forEach(([key, value]) => {
                    block.append(Seleno.el('br'), Seleno.el('span', { className: 'text-muted' }, `  ${key}:`), ` ${JSON.stringify(value)}`);
                });
                box.appendChild(block);
            });
        })
        .catch(() => {});
}

// --- wiring ------------------------------------------------------------------------------

function selectTab(button) {
    const tabs = button.parentElement.querySelectorAll('.ws-tab');
    tabs.forEach(tab => {
        const on = tab === button;
        tab.classList.toggle('on', on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        const panel = $(tab.getAttribute('aria-controls'));
        if (panel) panel.hidden = !on;
    });
    if (button.id === 'tabBtnUploads') refreshUploadQueue();
}

function wireInterface() {
    $('telescopeSelect').addEventListener('change', selectTelescope);
    $('estopBtn').addEventListener('click', emergencyStopMount);
    document.querySelectorAll('.ws-tabs .ws-tab').forEach(tab => tab.addEventListener('click', () => selectTab(tab)));

    // Buttons that send one command: data-command, data-params (JSON), data-confirm, data-after
    document.querySelectorAll('[data-command]').forEach(btn => {
        btn.addEventListener('click', () => {
            let params = {};
            try { params = btn.dataset.params ? JSON.parse(btn.dataset.params) : {}; } catch (error) { params = {}; }
            const run = () => telescopeAction(btn.dataset.command, params)
                .then(() => { if (btn.dataset.after === 'uploads') refreshUploadQueue(); })
                .catch(() => {});
            if (btn.dataset.confirm) {
                if (confirm(btn.dataset.confirm)) run();
            } else {
                run();
            }
        });
    });

    // Target tab
    $('btnSearch').addEventListener('click', searchObject);
    $('searchObject').addEventListener('keydown', (event) => { if (event.key === 'Enter') searchObject(); });
    $('btnGotoCoords').addEventListener('click', () => trackManualCoordinates(false));
    $('btnGotoCentre').addEventListener('click', () => trackManualCoordinates(true));
    $('btnGotoPlanet').addEventListener('click', () => {
        const name = $('planetSelect').value;
        goToTarget({ name, designation: name, moving: true });
    });
    $('stopTrackingBtn').addEventListener('click', stopTracking);

    // Mount tab
    $('btnStopTrackingMount').addEventListener('click', stopTracking);
    $('trackingRateSelect').addEventListener('change', () => $('customRateInput').classList.toggle('ws-hidden', $('trackingRateSelect').value !== 'custom'));
    $('btnSetRate').addEventListener('click', setTrackingRate);
    $('btnSolveSync').addEventListener('click', () => telescopeAction('solve_and_sync', { source: solveSource() }).catch(() => {}));
    $('btnCentre').addEventListener('click', () => telescopeAction('centre_target', { source: solveSource() }).catch(() => {}));
    $('btnPolar').addEventListener('click', () => telescopeAction('polar_align', { step_deg: $('polarStepDeg').value, source: solveSource() }).catch(() => {}));
    $('btnDither').addEventListener('click', () => telescopeAction('dither', { arcmin: $('ditherArcmin').value }).catch(() => {}));
    $('btnClearEstop').addEventListener('click', clearEstop);
    $('btnMotorControls').addEventListener('click', openAdvancedControlsModal);

    // Uploads and device tabs
    $('btnRefreshUploads').addEventListener('click', refreshUploadQueue);
    $('btnSetHorizon').addEventListener('click', setHorizon);

    // Live view
    $('liveViewToggleBtn').addEventListener('click', toggleLiveView);
    $('btnLiveToggleBasic').addEventListener('click', toggleLiveView);
    $('btnFullscreen').addEventListener('click', toggleFullscreen);
    $('liveViewCodec').addEventListener('change', (e) => telescopeAction('set_liveview_options', { codec: e.target.value }).catch(() => {}));
    $('liveViewQuality').addEventListener('change', (e) => telescopeAction('set_liveview_quality', { quality: e.target.value }).catch(() => {}));
    $('liveViewFocus').addEventListener('change', (e) => telescopeAction('set_liveview_focus', { on: e.target.checked }).catch(() => {}));
    $('overlaySelect').addEventListener('change', drawOverlay);
    $('displayStretch').addEventListener('input', applyDisplayStretch);
    document.querySelectorAll('#zoomSeg button').forEach(btn => btn.addEventListener('click', () => setZoomLevel(btn.dataset.zoom)));
    $('btnSnapshot').addEventListener('click', () => telescopeAction('liveview_snapshot').then(() => setTimeout(pollLatestCaptures, 3000)).catch(() => {}));
    $('recordingKind').addEventListener('change', () => $('recordingInterval').classList.toggle('ws-hidden', $('recordingKind').value !== 'timelapse'));
    $('btnRecord').addEventListener('click', startRecording);
    $('btnStopRecord').addEventListener('click', () => telescopeAction('stop_recording').then(refreshRecordingStatus).catch(() => {}));
    $('btnFocusReset').addEventListener('click', resetFocusHistory);

    // Camera
    CAMERA_SELECTS.forEach(({ key, id }) => $(id).addEventListener('change', (e) => updateSetting(key, e.target.value)));
    $('btnCameraRefresh').addEventListener('click', refreshCameraStatus);
    $('takePhotoBtn').addEventListener('click', () => {
        const btn = $('takePhotoBtn');
        btn.disabled = true;
        takePhoto().then(() => Seleno.toast(_('Photo taken. It shows up here and in the album once it uploads.'), 'ok'))
            .catch(error => { if (error.message !== 'Bad exposure') Seleno.toast(_('The photo failed: {message}', { message: error.message }), 'bad'); })
            .finally(() => { btn.disabled = false; });
    });

    // Plans
    $('btnOpenBuilder').addEventListener('click', () => window.CapturePlanBuilder && window.CapturePlanBuilder.open());
    $('planCoverBtn').addEventListener('click', () => planControl('resume'));
    $('planPauseBtn').addEventListener('click', () => planControl('pause'));
    $('planResumeBtn').addEventListener('click', () => planControl('resume'));
    $('planSkipBtn').addEventListener('click', () => planControl('skip'));
    $('planStopBtn').addEventListener('click', () => { if (confirm(_('Stop the capture plan?'))) planControl('stop'); });
    $('btnClearLog').addEventListener('click', () => { $('commandLog').innerHTML = ''; });

    document.addEventListener('seleno:mode', () => {
        drawOverlay();
        if (latestStatus) renderChips(latestStatus);
        if (latestSky) renderSky(latestSky);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    wireInterface();
    setControlsEnabled(false);
    initializeLiveView();
    initializeLiveHistogram();
    loadTrackingStatus();
    loadTelescopes().then(loadSelectedTelescope);
    setInterval(pollTelescopeStatus, 5000);
    setInterval(refreshLiveViewStats, 5000);
    setInterval(refreshSky, 60000);
    setInterval(pollLatestCaptures, 15000);
    setInterval(() => { if (!selectedTelescopeId) loadTelescopes(); }, 30000);
});
