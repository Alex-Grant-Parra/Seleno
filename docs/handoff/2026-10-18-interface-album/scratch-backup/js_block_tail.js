
// Telescope commands (POST /interface/command). Returns the result, or throws with the
// telescope's message. Jobs (park, dither, solve, centre, polar align) are polled until done.
function showCommandResult(title, value) {
    const box = document.getElementById('telescopeResult');
    if (!box) return;
    box.classList.remove('d-none');
    const text = (value === undefined || value === null) ? '' :
        (typeof value === 'string' ? value : JSON.stringify(value, null, 2));
    box.textContent = `${title}${text ? '\n' + text : ''}`;
}

function telescopeAction(action, params = {}, options = {}) {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return Promise.reject(new Error("No telescope selected"));
    }
    return fetch("/interface/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, params, telescope_id: selectedTelescopeId })
    })
    .then(checkAuthResponse)
    .then(response => response.json())
    .then(data => {
        if (data.status !== "success") {
            showCommandResult(`✗ ${action}: ${data.message || 'failed'}`);
            throw new Error(data.message || `${action} failed`);
        }
        if (!options.quiet) {
            showCommandResult(`✓ ${action}`, data.result);
        }
        if (data.job) {
            pollJob(data.job);
        }
        return data.result;
    })
    .catch(error => {
        console.error(`Command ${action} failed:`, error);
        throw error;
    });
}

function confirmAction(question, action, params = {}) {
    if (confirm(question)) {
        telescopeAction(action, params).catch(() => {});
    }
}

const JOB_LABELS = { park: 'Parking', dither: 'Dithering', solve: 'Plate solving', centre: 'Centring', polar: 'Polar alignment' };
const runningJobs = new Set();

// Poll getJobStatus every 2 s until the job stops running, then show its result or error
function pollJob(name) {
    if (runningJobs.has(name)) return;
    runningJobs.add(name);
    const line = document.getElementById('jobStatusLine');
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
                    if (line) line.textContent = `${label} failed: ${status.error}`;
                    showCommandResult(`✗ ${label} failed`, status.error);
                } else {
                    if (line) line.textContent = `${label} done.`;
                    showCommandResult(`✓ ${label} done`, status.result);
                }
            })
            .catch(() => {
                runningJobs.delete(name);
                if (line) line.textContent = `${label}: lost track of the job.`;
            });
    };
    if (line) line.textContent = `${label}…`;
    setTimeout(tick, 2000);
}

function emergencyStopMount() {
    telescopeAction('estop').then(() => updateMotorStatusDisplay('🚨 Emergency stop sent', 'error')).catch(() => {});
}

function clearEstop() {
    confirmAction('Clear the emergency stop? Make sure nothing is in the way of the mount.', 'clear_estop');
}

function solveSource() {
    return document.getElementById('solveSource').value || null;
}

function setTrackingRate() {
    const rate = document.getElementById('trackingRateSelect').value;
    const params = { rate };
    if (rate === 'custom') {
        const custom = parseFloat(document.getElementById('customRateInput').value);
        if (isNaN(custom)) {
            alert('Enter a custom rate in arcseconds per second.');
            return;
        }
        params.custom_arcsec_per_s = custom;
    }
    telescopeAction('set_tracking_rate', params).catch(() => {});
}

// The Moon and planets: the Pi works out where they appear from its own site
function trackPlanet() {
    const name = document.getElementById('planetSelect').value;
    telescopeAction('track_object', { name })
        .then(result => {
            const ra = result && result.ra !== undefined ? result.ra : '—';
            const dec = result && result.dec !== undefined ? result.dec : '—';
            updateTrackingPanel({ name, ra, dec, mag: '—' });
            updateMotorStatusDisplay(`🎯 Tracking ${name}`, 'success');
        })
        .catch(error => alert(`Couldn't go to ${name}: ${error.message}`));
}

function loadPlanets() {
    // Fill the planet list from the Pi when it offers one
    telescopeAction('get_planets', {}, { quiet: true })
        .then(result => {
            const names = Array.isArray(result) ? result : (result && Array.isArray(result.planets) ? result.planets : null);
            if (!names || !names.length) return;
            const select = document.getElementById('planetSelect');
            select.innerHTML = '';
            names.forEach(entry => {
                const name = typeof entry === 'string' ? entry : entry.name;
                if (!name) return;
                const opt = document.createElement('option');
                opt.value = name;
                opt.text = typeof entry === 'object' && entry.alt_deg !== undefined
                    ? `${name} (${Number(entry.alt_deg).toFixed(0)}°)` : name;
                select.appendChild(opt);
            });
        })
        .catch(() => {});
}

function setZoomLevel(level) {
    telescopeAction('set_camera_zoom', { level: parseFloat(level) }, { quiet: true }).catch(() => {});
}

function startRecording() {
    const kind = document.getElementById('recordingKind').value;
    const params = { kind };
    if (kind === 'timelapse') {
        params.interval_s = parseFloat(document.getElementById('recordingInterval').value) || 5;
    }
    telescopeAction('start_recording', params).then(refreshRecordingStatus).catch(() => {});
}

function refreshRecordingStatus() {
    return telescopeAction('recording_status', {}, { quiet: true })
        .then(status => renderRecording(status))
        .catch(() => {});
}

function renderRecording(status) {
    const line = document.getElementById('recordingStatus');
    if (!line || !status) return;
    if (status.error && !status.recording) {
        line.textContent = `Last recording error: ${status.error}`;
    } else if (status.recording) {
        const seconds = status.seconds !== undefined ? ` · ${Math.round(status.seconds)} s` : '';
        line.textContent = `● Recording ${status.kind || ''}: ${status.frames || 0} frames${seconds}`;
    } else {
        line.textContent = status.path ? `Saved ${status.path}` : 'Not recording.';
    }
}

function refreshLiveViewStats() {
    telescopeAction('liveview_stats', {}, { quiet: true })
        .then(stats => {
            if (!stats) return;
            const parts = [];
            if (stats.codec) parts.push(stats.codec.toUpperCase());
            if (stats.fps !== undefined) parts.push(`${Number(stats.fps).toFixed(1)} fps`);
            if (stats.kbps !== undefined || stats.bitrate_kbps !== undefined) parts.push(`${Math.round(stats.kbps ?? stats.bitrate_kbps)} kbps`);
            if (stats.avg_kb !== undefined) parts.push(`${Number(stats.avg_kb).toFixed(0)} KB/frame`);
            if (stats.keyframes !== undefined) parts.push(`${stats.keyframes} keyframes`);
            document.getElementById('liveViewStats').textContent = parts.join(' · ') || 'No stats';
        })
        .catch(() => {});
}

function formatBytes(bytes) {
    if (bytes === undefined || bytes === null || isNaN(bytes)) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = Number(bytes);
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit++;
    }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function refreshCameraStatus() {
    telescopeAction('camera_status', {}, { quiet: true })
        .then(status => {
            if (!status) return;
            const battery = status.battery ?? status.batteryLevel ?? '—';
            const card = status.card_free ?? status.cardFree ?? status.card ?? '—';
            const cardText = typeof card === 'number' ? formatBytes(card) + ' free' : card;
            document.getElementById('cameraStatusLine').textContent = `Battery ${battery} · Card ${cardText}`;
        })
        .catch(() => {});
}

function refreshUploadQueue() {
    if (!selectedTelescopeId) return Promise.resolve();
    return telescopeAction('upload_queue', {}, { quiet: true })
        .then(queue => renderUploads(queue))
        .catch(() => {});
}

function renderUploads(uploads) {
    const box = document.getElementById('uploadQueueSummary');
    if (!box || !uploads) return;
    if (uploads.error && Object.keys(uploads).length === 1) {
        box.textContent = `Uploads: ${uploads.error}`;
        return;
    }
    const waiting = uploads.waiting ?? (Array.isArray(uploads.queue) ? uploads.queue.length : undefined);
    const parts = [
        `Waiting ${waiting ?? '—'}`,
        `RAWs held ${uploads.waiting_raw ?? uploads.held ?? '—'}`,
        `Failed ${uploads.failed ?? '—'}`,
        `Sent ${uploads.sent ?? '—'}`
    ];
    let text = parts.join(' · ');
    if (uploads.current) text += `\nSending ${typeof uploads.current === 'string' ? uploads.current : (uploads.current.filename || '')}`;
    if (uploads.last_error) text += `\nLast error: ${uploads.last_error}`;
    box.textContent = text;
    box.style.whiteSpace = 'pre-line';
}

function setHorizon() {
    const params = {
        min_alt_deg: document.getElementById('horizonMinAlt').value,
        profile: document.getElementById('horizonProfile').value,
        stop_when_set: document.getElementById('horizonStopWhenSet').checked
    };
    telescopeAction('set_horizon', params).catch(() => {});
}

// Telescope status: the Pi pushes a status message every 15 s; the page reads the latest
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
        if (tracking.error) add('Mount', `⚠ ${tracking.error}`);
        else {
            let text = tracking.parked ? 'Parked' : (tracking.state || '—');
            if (tracking.target) text += ` · ${tracking.target}`;
            if (tracking.rate) text += ` · ${tracking.rate}`;
            add('Mount', text);
        }
    }
    const camera = statusPart(status, 'camera');
    if (camera) {
        add('Camera', camera.error ? `⚠ ${camera.error}` : `${camera.connected ? (camera.model || 'Connected') : 'Not connected'}`);
    }
    const lv = statusPart(status, 'liveview');
    if (lv) {
        if (lv.error) add('Live view', `⚠ ${lv.error}`);
        else {
            let text = lv.enabled ? (lv.paused ? 'Paused (no viewers)' : 'On') : 'Off';
            if (lv.enabled && lv.fps !== undefined) text += ` · ${Number(lv.fps).toFixed(1)} fps`;
            if (lv.enabled && lv.kbps !== undefined) text += ` · ${Math.round(lv.kbps)} kbps`;
            add('Live view', text);
        }
    }
    add('Watching', String(viewers ?? 0));
    const recording = statusPart(status, 'recording');
    if (recording && recording.recording) add('Recording', `${recording.kind || ''} · ${recording.frames || 0} frames`);
    const uploads = statusPart(status, 'uploads');
    if (uploads) {
        add('Uploads', uploads.error ? `⚠ ${uploads.error}` :
            `${uploads.waiting ?? 0} waiting · ${uploads.failed ?? 0} failed${uploads.last_error ? ' · ' + uploads.last_error : ''}`);
    }
    const clock = statusPart(status, 'clock');
    if (clock) add('Clock', clock.error ? `⚠ ${clock.error}` : `${clock.trusted ? 'Trusted' : 'Not trusted'}${clock.source ? ' · ' + clock.source : ''}${clock.offset_s !== undefined ? ' · ' + Number(clock.offset_s).toFixed(2) + ' s' : ''}`);
    const disk = status ? status.disk : undefined;
    if (disk !== undefined) add('Disk', typeof disk === 'number' ? `${formatBytes(disk)} free` : (disk.error ? `⚠ ${disk.error}` : `${formatBytes(disk.free_bytes ?? disk.free)} free`));
    const esp = statusPart(status, 'esp32');
    if (esp) add('Controller', esp.error ? `⚠ ${esp.error}` : `${esp.connected ? 'Connected' : 'Not connected'}${esp.estop ? ' · E-STOP' : ''}`);
    return rows;
}

function renderTelescopeStatus(data) {
    const grid = document.getElementById('telescopeStatusGrid');
    const age = document.getElementById('statusAge');
    if (!grid) return;
    grid.innerHTML = '';
    if (!data.telescope_status) {
        grid.innerHTML = '<dt>—</dt><dd>No status from the telescope yet</dd>';
        if (age) age.textContent = '';
    } else {
        describeStatus(data.telescope_status, data.viewers).forEach(([label, value]) => {
            const dt = document.createElement('dt');
            dt.textContent = label;
            const dd = document.createElement('dd');
            dd.textContent = value;
            grid.append(dt, dd);
        });
        if (age) age.textContent = data.age_s !== null ? `${Math.round(data.age_s)} s ago` : '';
        const lv = statusPart(data.telescope_status, 'liveview');
        // Live view switched on elsewhere (or before a reload): watch it here too
        if (lv && lv.enabled && !liveViewActive && liveViewPlayer) {
            liveViewActive = true;
            setLiveViewButton(true);
            liveViewPlayer.start(selectedTelescopeId);
        }
        const recording = statusPart(data.telescope_status, 'recording');
        if (recording && !recording.error) renderRecording(recording);
        const uploads = statusPart(data.telescope_status, 'uploads');
        if (uploads && !uploads.error) renderUploads(uploads);
    }
    renderPlan(data.plan);
}

function renderPlan(plan) {
    const box = document.getElementById('planBox');
    if (!box) return;
    if (!plan) {
        box.classList.add('d-none');
        return;
    }
    box.classList.remove('d-none');
    const status = plan.last_status || {};
    const state = status.state || plan.state;
    document.getElementById('planTitle').textContent = plan.name;
    document.getElementById('planState').textContent = state;
    const steps = plan.steps || [];
    const stepIndex = typeof status.step === 'number' ? status.step : null;
    let progress = '';
    if (stepIndex !== null && steps[stepIndex]) {
        progress = `Step ${stepIndex + 1} of ${steps.length}: ${steps[stepIndex].type}`;
        if (status.frame !== undefined) progress += ` · frame ${status.frame + 1}`;
    }
    if (status.time_left_s !== undefined) progress += ` · ${Math.round(status.time_left_s / 60)} min left`;
    if (Array.isArray(status.messages) && status.messages.length) progress += `\n${status.messages[status.messages.length - 1]}`;
    const progressEl = document.getElementById('planProgress');
    progressEl.textContent = progress || 'Starting…';
    progressEl.style.whiteSpace = 'pre-line';

    const coverWait = state === 'waiting' && status.waiting === 'cover';
    document.getElementById('planCoverPrompt').classList.toggle('d-none', !coverWait);
    document.getElementById('planCoverBtn').classList.toggle('d-none', !coverWait);
    document.getElementById('planPauseBtn').classList.toggle('d-none', state !== 'running');
    document.getElementById('planResumeBtn').classList.toggle('d-none', state !== 'paused');
}

function pollTelescopeStatus() {
    if (!selectedTelescopeId || document.hidden) return;
    fetch(`/interface/telescope_status?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
        .then(response => response.json().then(body => ({ ok: response.ok, body })))
        .then(({ ok, body }) => {
            if (!ok || body.status !== 'success') {
                const grid = document.getElementById('telescopeStatusGrid');
                if (grid) grid.innerHTML = `<dt>—</dt><dd></dd>`;
                if (grid) grid.querySelector('dd').textContent = body.message || 'Status unavailable';
                return;
            }
            if (body.access) applyTelescopeAccess(body.access);
            renderTelescopeStatus(body);
        })
        .catch(() => {});
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
            alert(`Couldn't ${action} the plan: ${data.message || 'unknown error'}`);
            return;
        }
        renderPlan(data.plan && ['done', 'stopped', 'failed'].includes(data.plan.state) ? null : data.plan);
        pollTelescopeStatus();
    })
    .catch(error => alert(`Couldn't ${action} the plan: ${error}`));
}

function loadPlanHistory() {
    const box = document.getElementById('planHistory');
    if (!box || !selectedTelescopeId) return;
    fetch(`/interface/capture/history?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') return;
            box.innerHTML = '';
            if (!data.plans.length) {
                box.textContent = 'No plans run on this telescope yet.';
                return;
            }
            data.plans.slice(0, 8).forEach(plan => {
                const row = document.createElement('div');
                row.className = 'd-flex justify-content-between border-bottom py-1';
                const name = document.createElement('span');
                name.textContent = plan.name;
                const state = document.createElement('span');
                state.className = 'text-muted';
                state.textContent = `${plan.state}${plan.frames_done ? ' · ' + plan.frames_done + ' frames' : ''}`;
                row.append(name, state);
                box.appendChild(row);
            });
        })
        .catch(() => {});
}

// Called whenever a telescope becomes the selected one
function onTelescopeSelected(access) {
    applyTelescopeAccess(access);
    if (!telescopeAccess.control) {
        updateMotorStatusDisplay("You can view this telescope, but controlling it needs ownership or a booking that covers now.", 'warning');
    }
    pollTelescopeStatus();
    loadPlanHistory();
    loadPlanets();
    refreshCameraStatus();
}
