        // Selected telescope state
        let selectedTelescopeId = null;
        let selectedMotorId = "motor1"; // Default motor
        let telescopeAccess = { control: false, owner: false };

        // Enable/disable UI controls based on telescope selection
        function setControlsEnabled(enabled) {
            const ids = [
                "shutterSpeedSelect",
                "isoSelect",
                "apertureSelect",
                "whiteBalance",
                "photoFormat",
                "exposureCompSelect",
                "bulbSeconds",
                "takePhotoBtn",
                "liveViewToggleBtn"
            ];
            ids.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.disabled = !enabled;
            });

            // Also enable/disable advanced motor controls
            setAdvancedControlsEnabled(enabled);
        }

        function setAdvancedControlsEnabled(enabled) {
            const advancedIds = [
                "motorEnableBtn",
                "speedControl",
                "stepsControl",
                "microstepsControl",
                "currentControl",
                "accelControl",
                "modeControl"
            ];

            advancedIds.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.disabled = !enabled;
            });

            // Also disable direction and movement buttons
            const directionBtns = document.querySelectorAll('[onclick^="setDirection"]');
            const movementBtns = document.querySelectorAll('[onclick="startMotor()"], [onclick="stopMotor()"], [onclick="moveSteps()"]');
            const settingBtns = document.querySelectorAll('[onclick="setSpeed()"], [onclick="setCurrent()"], [onclick="setAcceleration()"], [onclick="getMotorStatus()"]');

            [...directionBtns, ...movementBtns, ...settingBtns].forEach(btn => {
                btn.disabled = !enabled;
            });
        }

// Show owner-only controls (the Device tab, clearing an e-stop) to the owner and admins
function applyTelescopeAccess(access) {
    telescopeAccess = access || { control: false, owner: false };
    document.getElementById('deviceTabItem')?.classList.toggle('d-none', !telescopeAccess.owner);
    document.querySelectorAll('.owner-only').forEach(el => el.classList.toggle('d-none', !telescopeAccess.owner));
}

// Camera settings: the select each getCameraChoices list fills, and the field it sets
const CAMERA_SELECTS = [
    { key: "shutterSpeed", id: "shutterSpeedSelect" },
    { key: "iso", id: "isoSelect" },
    { key: "aperture", id: "apertureSelect" },
    { key: "whiteBalance", id: "whiteBalance" },
    { key: "imageFormat", id: "photoFormat" },
    { key: "exposureCompensation", id: "exposureCompSelect" }
];
window.cameraChoices = null;

// Fetch camera choices and populate dropdowns, preset to the camera's current values
function populateCameraChoices() {
            if (!selectedTelescopeId) {
                console.warn("Skipping camera choices fetch: no telescope selected");
                return;
            }

            fetch(`/interface/get_camera_choices?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
        .then(response => response.json())
        .then(data => {
                    if (data && data.status === "error") {
                        console.error("Failed to fetch camera choices:", data.message);
                        return;
                    }
            window.cameraChoices = data;
            const current = data.current || {};
            CAMERA_SELECTS.forEach(({ key, id }) => {
                const select = document.getElementById(id);
                if (!select) return;
                select.innerHTML = "";
                (data[key] || []).forEach(choice => {
                    const opt = document.createElement("option");
                    opt.value = choice;
                    opt.text = choice;
                    select.appendChild(opt);
                });
                if (current[key] !== undefined && current[key] !== null) {
                    select.value = String(current[key]);
                }
                select.disabled = !(data[key] || []).length;
            });
            updateBulbVisibility();
        })
        .catch(error => {
            console.error("Failed to fetch camera choices:", error);
        });
}

// Call on page load
document.addEventListener("DOMContentLoaded", function() {
    console.log('Page loaded - initializing...');
    setControlsEnabled(false);
    loadTelescopes();
    loadSelectedTelescope();
    loadTrackingStatus();
    initializeLiveView();
    initializeLiveHistogram();
    setInterval(pollTelescopeStatus, 5000);

    // Initialize draggable panels
    const trackingPanel = document.getElementById('trackingPanel');
    if (trackingPanel) {
        const header = trackingPanel.querySelector('.panel-header');
        if (header) {
            console.log('Making trackingPanel draggable');
            makeDraggable(trackingPanel, header);
        }
    } else {
        console.warn('WARNING: trackingPanel element not found!');
    }
});

// Send one changed camera setting to the backend
function updateSetting(field, value) {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }
    if (value === undefined || value === null || value === "") return;
    fetch("/interface/update_camera", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: value, telescope_id: selectedTelescopeId })
    }).then(response => response.json())
    .then(data => {
        if (data.status !== "success") {
            alert("Camera setting not changed: " + (data.message || "Unknown error"));
        }
    })
    .catch(error => console.error("Fetch error:", error));
}

function isBulbShutter(value) {
    return /bulb/i.test(String(value || ""));
}

function updateBulbVisibility() {
    const shutter = document.getElementById("shutterSpeedSelect");
    document.getElementById("bulbRow")?.classList.toggle("d-none", !isBulbShutter(shutter && shutter.value));
}

function takePhoto() {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }
    const shutter = document.getElementById("shutterSpeedSelect").value;
    const body = { telescope_id: selectedTelescopeId, shutter };
    if (isBulbShutter(shutter)) {
        const seconds = parseFloat(document.getElementById("bulbSeconds").value);
        if (isNaN(seconds) || seconds <= 0 || seconds > 900) {
            alert("Enter a bulb exposure between 0 and 900 seconds.");
            return;
        }
        body.exposure_s = seconds;
    }
    const btn = document.getElementById("takePhotoBtn");
    btn.disabled = true;
    fetch("/interface/take_photo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === "success") {
            alert("Photo taken and saved!");
        } else {
            alert("Failed to take photo: " + (data.message || "Unknown error"));
        }
    })
    .catch(error => {
        alert("Error taking photo: " + error);
    })
    .finally(() => {
        btn.disabled = false;
    });
}

// Live View: a canvas player fed over the live-view socket (H.264, or JPEG)
let liveViewActive = false;
let liveViewPlayer = null;
let liveHistogram = null;

function setLiveViewOverlay(text) {
    const overlay = document.getElementById('liveViewOverlay');
    if (!overlay) return;
    overlay.textContent = text || '';
    overlay.classList.toggle('d-none', !text);
}

function initializeLiveView() {
    const canvas = document.getElementById('liveViewCanvas');
    if (!canvas || typeof window.LiveViewPlayer !== 'function') return;
    liveViewPlayer = new window.LiveViewPlayer(canvas, {
        onState: (state, detail) => {
            const badge = document.getElementById('liveViewBadge');
            if (state === 'playing') {
                setLiveViewOverlay('');
                if (badge) {
                    badge.textContent = detail || '';
                    badge.classList.remove('d-none');
                }
                return;
            }
            badge?.classList.add('d-none');
            // Keep showing the last frame while the camera restarts
            const messages = {
                connecting: 'Connecting…',
                waiting: 'Waiting for the telescope…',
                offline: 'Live view off',
                error: detail || 'Live view unavailable'
            };
            setLiveViewOverlay(messages[state] || '');
        }
    });

    // Click the picture to zoom there
    canvas.addEventListener('click', (event) => {
        if (!selectedTelescopeId || !liveViewActive) return;
        const rect = canvas.getBoundingClientRect();
        const x = (event.clientX - rect.left) / rect.width;
        const y = (event.clientY - rect.top) / rect.height;
        const levelSelect = document.getElementById('liveViewZoom');
        let level = parseFloat(levelSelect.value);
        if (level <= 1) {
            level = 5;
            levelSelect.value = '5';
        }
        telescopeAction('set_camera_zoom', { level, x: x.toFixed(4), y: y.toFixed(4) }, { quiet: true });
    });
}

function initializeLiveHistogram() {
    if (typeof window.HistogramPanel !== 'function' || liveHistogram) {
        return;
    }

    liveHistogram = new window.HistogramPanel({
        canvasId: 'liveHistogramCanvas',
        controlsRootId: 'liveHistogramControls',
        statsId: 'liveHistogramStats',
        emptyMessage: 'Waiting for live frame...'
    });

    const canvas = document.getElementById('liveViewCanvas');
    if (liveHistogram && canvas) {
        liveHistogram.setSourceImage(canvas);
    }
}

function setLiveViewButton(active) {
    const btn = document.getElementById("liveViewToggleBtn");
    if (!btn) return;
    btn.textContent = active ? "Stop" : "Start";
    btn.classList.toggle("btn-outline-danger", active);
    btn.classList.toggle("btn-outline-primary", !active);
}

function toggleLiveView() {
    console.log("Live view toggled");

    const btn = document.getElementById("liveViewToggleBtn");

    if (!btn) {
        console.error("Live view button not found! Make sure an element with ID 'liveViewToggleBtn' exists.");
        alert("Live view button not found in the page!");
        return;
    }
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }

    if (liveViewActive) {
        // Stop live view
        fetch("/interface/stop_live_view", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ telescope_id: selectedTelescopeId })
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                liveViewActive = false;
                setLiveViewButton(false);
                if (liveViewPlayer) liveViewPlayer.stop();
                setLiveViewOverlay('Live view off');
            } else {
                console.error("Failed to stop live view:", data.message);
                alert("Error stopping live view: " + data.message);
            }
        })
        .catch(error => {
            console.error("Error stopping live view:", error);
            alert("Error stopping live view: " + error);
        });
    } else {
        // Start live view, and watch it
        fetch("/interface/start_live_view", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ telescope_id: selectedTelescopeId })
        })
        .then(checkAuthResponse)
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                liveViewActive = true;
                setLiveViewButton(true);
                if (liveViewPlayer) liveViewPlayer.start(selectedTelescopeId);
            } else {
                console.error("Failed to start live view:", data.message);
                alert("Error starting live view: " + data.message);
            }
        })
        .catch(error => {
            console.error("Error starting live view:", error);
            if (error.message !== 'Not authenticated') {
                alert("Error starting live view: " + error);
            }
        });
    }
}

