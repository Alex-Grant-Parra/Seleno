// Interface Controls JavaScript

// Helper function to check for authentication errors in fetch responses
function checkAuthResponse(response) {
    if (response.status === 401) {
        alert('You must be logged in to control the telescope.');
        window.location.href = '/auth/login';
        throw new Error('Not authenticated');
    }
    return response;
}

// Toggle full screen mode
function toggleFullscreen() {
    let panel = document.getElementById("mainPanel");
    if (!document.fullscreenElement) {
        panel.requestFullscreen().catch(err => {
            alert(`Error attempting to enable full-screen mode: ${err.message} (${err.name})`);
        });
    } else {
        document.exitFullscreen();
    }
}

// Make the panels draggable
document.querySelectorAll(".draggable-panel").forEach(panel => {
    let header = panel.querySelector(".panel-header");
    header.addEventListener("mousedown", (event) => {
        let shiftX = event.clientX - panel.getBoundingClientRect().left;
        let shiftY = event.clientY - panel.getBoundingClientRect().top;

        function moveAt(pageX, pageY) {
            panel.style.left = pageX - shiftX + "px";
            panel.style.top = pageY - shiftY + "px";
        }

        function onMouseMove(event) {
            moveAt(event.pageX, event.pageY);
        }

        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", () => {
            document.removeEventListener("mousemove", onMouseMove);
        }, { once: true });
    });
});

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

function showStarInfo(star) {
    const existingModal = document.getElementById("starInfoModal");
    if (existingModal) existingModal.remove();

    // Extract basic information with proper fallbacks
    const name = star.name || star.Name || "Unknown";
    const commonName = star.friendlyName || extractCommonName(star.commonNames || star['Common names']) || "";
    const ra = parseFloat(star.ra !== undefined ? star.ra : star.RA || 0).toFixed(4);
    const dec = parseFloat(star.dec !== undefined ? star.dec : star.DEC || 0).toFixed(4);
    const magnitude = star.mag !== undefined ? star.mag : star["V-Mag"] || "N/A";

    // Create modal with enhanced styling
    const modal = document.createElement("div");
    modal.id = "starInfoModal";
    modal.className = "star-info-modal";
    modal.style.cssText = `
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        background: #ffffff;
        color: #333;
        border-radius: 12px;
        z-index: 1050;
        min-width: 350px;
        max-width: 500px;
        box-shadow: 0 0.5rem 1rem rgba(0, 0, 0, 0.3);
        border: none;
        overflow: hidden;
    `;

    modal.innerHTML = `
        <div class="modal-header" style="background: linear-gradient(135deg, #007bff, #0056b3); color: white; padding: 1rem; border-bottom: none;">
            <h5 class="modal-title" style="margin: 0; font-weight: 600;">🌟 Object Information</h5>
            <button type="button" class="btn-close" id="closeStarInfo" style="filter: invert(1); background: none; border: none; font-size: 1.2rem; cursor: pointer;">&times;</button>
        </div>
        <div class="modal-body" style="padding: 1.5rem;">
            <!-- Basic Information -->
            <div class="basic-info">
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>Identifier:</strong> <span class="text-primary">${name}</span>${commonName ? `  <span class="text-success">(${commonName})</span>` : ''}
                </div>
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>RA:</strong> <span class="text-info">${ra}°</span>
                </div>
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>DEC:</strong> <span class="text-info">${dec}°</span>
                </div>
            </div>
            
            <!-- Advanced Info Toggle -->
            <div class="advanced-toggle" style="margin: 1.5rem 0;">
                <button id="toggleAdvancedInfo" class="btn btn-outline-secondary btn-sm w-100" onclick="toggleAdvancedObjectInfo()">
                    📊 Show Advanced Information
                </button>
            </div>
            
            <!-- Advanced Information (Initially Hidden) -->
            <div id="advancedObjectInfo" class="advanced-info" style="display: none; padding: 1rem; background-color: #f8f9fa; border-radius: 8px; border-left: 4px solid #007bff;">
                ${generateAdvancedInfo(star)}
            </div>
        </div>
        <div class="modal-footer" style="padding: 1rem; background-color: #f8f9fa; border-top: 1px solid #dee2e6; display: flex; justify-content: space-between;">
            <button id="trackObjectBtn" class="btn btn-success">🎯 Track Object</button>
            <button id="closeStarInfoFooter" class="btn btn-secondary">Close</button>
        </div>
    `;

    // Apply night mode styling if active
    if (document.body.classList.contains('night-mode')) {
        modal.style.background = '#1a1a1a';
        modal.style.color = '#e6e6e6';
        const modalBody = modal.querySelector('.modal-body');
        if (modalBody) modalBody.style.color = '#e6e6e6';
        const modalFooter = modal.querySelector('.modal-footer');
        if (modalFooter) {
            modalFooter.style.backgroundColor = '#2a2a2a';
            modalFooter.style.borderTopColor = '#444';
        }
        const advancedInfo = modal.querySelector('.advanced-info');
        if (advancedInfo) {
            advancedInfo.style.backgroundColor = '#2a2a2a';
            advancedInfo.style.color = '#e6e6e6';
        }
    }

    document.body.appendChild(modal);

    // Event listeners
    document.getElementById("closeStarInfo").addEventListener("click", () => modal.remove());
    document.getElementById("closeStarInfoFooter").addEventListener("click", () => modal.remove());
    
    document.getElementById("trackObjectBtn").addEventListener("click", () => {
        trackObject(star);
        modal.remove();
    });

    // Make modal draggable by header
    makeDraggable(modal, modal.querySelector('.modal-header'));
}

function extractCommonName(commonNames) {
    if (!commonNames) return "";
    const parts = commonNames.split(',').map(p => p.trim());
    for (const name of parts) {
        const nameUpper = name.toUpperCase();
        // Skip catalog designations (HD, NGC, IC, M followed by number)
        if (nameUpper.startsWith('HD') || 
            nameUpper.startsWith('NGC') || 
            nameUpper.startsWith('IC') || 
            (nameUpper.startsWith('M') && name.length > 1 && name.slice(1).trim().replace(/\s/g, '').match(/^\d+$/))) {
            continue;
        }
        // Found a friendly name
        return name;
    }
    return "";
}

function generateAdvancedInfo(star) {
    let advancedHtml = "<h6 style='margin-bottom: 1rem; color: #007bff;'>📋 Detailed Information</h6>";
    
    // Create a table of all available properties
    const excludeKeys = ['name', 'Name', 'ra', 'RA', 'dec', 'DEC', 'friendlyName'];
    const propertyMappings = {
        'mag': 'Visual Magnitude',
        'V-Mag': 'Visual Magnitude', 
        'B-Mag': 'Blue Magnitude',
        'U-Mag': 'Ultraviolet Magnitude',
        'R-Mag': 'Red Magnitude',
        'I-Mag': 'Infrared Magnitude',
        'J-Mag': 'J-band Magnitude',
        'H-Mag': 'H-band Magnitude',
        'K-Mag': 'K-band Magnitude',
        'commonNames': 'All Names',
        'SpectralType': 'Spectral Type',
        'spectralType': 'Spectral Type',
        'Parallax': 'Parallax (mas)',
        'parallax': 'Parallax (mas)',
        'ProperMotionRA': 'Proper Motion RA (mas/yr)',
        'ProperMotionDec': 'Proper Motion DEC (mas/yr)',
        'RadialVelocity': 'Radial Velocity (km/s)',
        'Distance': 'Distance (pc)',
        'Luminosity': 'Luminosity',
        'Temperature': 'Temperature (K)',
        'Mass': 'Mass (Solar masses)',
        'Radius': 'Radius (Solar radii)',
        'Age': 'Age (Gyr)',
        'Metallicity': 'Metallicity [Fe/H]'
    };
    
    advancedHtml += '<div class="table-responsive"><table class="table table-sm table-hover">';
    advancedHtml += '<thead><tr><th>Property</th><th>Value</th></tr></thead><tbody>';
    
    for (const [key, value] of Object.entries(star)) {
        if (excludeKeys.includes(key) || value === null || value === undefined || value === "") continue;
        
        const displayName = propertyMappings[key] || key.replace(/([A-Z])/g, ' $1').trim();
        let displayValue = value;
        
        // Format numeric values
        if (typeof value === 'number' && !Number.isInteger(value)) {
            displayValue = value.toFixed(4);
        }
        
        advancedHtml += `<tr><td><strong>${displayName}:</strong></td><td>${displayValue}</td></tr>`;
    }
    
    advancedHtml += '</tbody></table></div>';
    
    if (Object.keys(star).filter(key => !excludeKeys.includes(key)).length === 0) {
        advancedHtml = "<p class='text-muted'>No additional information available for this object.</p>";
    }
    
    return advancedHtml;
}

function toggleAdvancedObjectInfo() {
    const advancedInfo = document.getElementById("advancedObjectInfo");
    const toggleBtn = document.getElementById("toggleAdvancedInfo");
    
    if (advancedInfo.style.display === "none") {
        advancedInfo.style.display = "block";
        toggleBtn.innerHTML = "📊 Hide Advanced Information";
        toggleBtn.classList.remove("btn-outline-secondary");
        toggleBtn.classList.add("btn-outline-primary");
    } else {
        advancedInfo.style.display = "none";
        toggleBtn.innerHTML = "📊 Show Advanced Information";
        toggleBtn.classList.remove("btn-outline-primary");
        toggleBtn.classList.add("btn-outline-secondary");
    }
}

function trackObject(star) {
    console.log('=== trackObject CALLED ===');
    console.log('Star object received:', star);
    console.log('Star object keys:', Object.keys(star));
    
    // Extract with better fallback handling
    const name = star.name || star.Name || star.friendlyName || 'Unknown';
    const ra = star.ra !== undefined ? star.ra : (star.RA !== undefined ? star.RA : null);
    const dec = star.dec !== undefined ? star.dec : (star.DEC !== undefined ? star.DEC : null);
    const mag = star.mag !== undefined ? star.mag : (star["V-Mag"] !== undefined ? star["V-Mag"] : null);
    
    console.log('Extracted values:');
    console.log('  name:', name, '(type:', typeof name, ')');
    console.log('  ra:', ra, '(type:', typeof ra, ')');
    console.log('  dec:', dec, '(type:', typeof dec, ')');
    console.log('  mag:', mag, '(type:', typeof mag, ')');
    
    if (!name || ra === null || dec === null) {
        console.error('ERROR: Missing required properties!');
        updateMotorStatusDisplay('❌ Cannot track - missing coordinate data', 'error');
        return;
    }
    
    console.log('Sending fetch to /track_star with:', { name, ra, dec, mag });
    
    fetch("/track_star", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, ra, dec, mag })
    })
    .then(response => response.json())
    .then(data => {
        console.log('=== RESPONSE FROM /track_star ===');
        console.log('Response data:', data);
        console.log('Response status:', data.status);
        
        if (data.status === 'tracking') {
            console.log('✓ Tracking confirmed! Updating panel...');
            updateMotorStatusDisplay(`🎯 Tracking command sent to telescope for ${name}`, 'success');
            
            // Update the tracking panel
            const trackingInfo = { name, ra, dec, mag };
            console.log('Calling updateTrackingPanel with:', trackingInfo);
            updateTrackingPanel(trackingInfo);
            console.log(`Tracking ${name} on telescope ${data.telescope_id}`);
        } else if (data.redirect) {
            console.log('! No telescope selected');
            updateMotorStatusDisplay(`⚠️ ${data.message}`, 'error');
            alert(data.message);
        } else {
            console.error('✗ Tracking failed:', data);
            updateMotorStatusDisplay(`❌ Failed to start tracking: ${data.message || data.error}`, 'error');
        }
    })
    .catch(error => {
        console.error('✗ FETCH ERROR:', error);
        updateMotorStatusDisplay(`❌ Failed to start tracking: ${error}`, 'error');
    });
}

function makeDraggable(element, handle) {
    let pos1 = 0, pos2 = 0, pos3 = 0, pos4 = 0;
    handle.style.cursor = "move";
    
    handle.onmousedown = dragMouseDown;
    
    function dragMouseDown(e) {
        e = e || window.event;
        e.preventDefault();
        pos3 = e.clientX;
        pos4 = e.clientY;
        document.onmouseup = closeDragElement;
        document.onmousemove = elementDrag;
    }
    
    function elementDrag(e) {
        e = e || window.event;
        e.preventDefault();
        pos1 = pos3 - e.clientX;
        pos2 = pos4 - e.clientY;
        pos3 = e.clientX;
        pos4 = e.clientY;
        element.style.top = (element.offsetTop - pos2) + "px";
        element.style.left = (element.offsetLeft - pos1) + "px";
    }
    
    function closeDragElement() {
        document.onmouseup = null;
        document.onmousemove = null;
    }
}

function searchObject() {
    let searchValue = document.getElementById("searchObject").value;
    if (searchValue) {
        fetch("/interface/search_object", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ searchValue: searchValue })
        }).then(response => response.json())
        .then(data => {
            if (data.status === "success" && data.data) {
                showStarInfo(data.data);
            } else {
                alert(data.message || "Object not found.");
            }
        })
        .catch(error => console.error("Fetch error:", error));
    } else {
        alert("Please enter a valid HD, NGC, or IC number.");
    }
}

// Telescope selection functions
function loadTelescopes() {
    fetch("/interface/get_telescopes")
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                populateTelescopeDropdown(data.telescopes);
            } else {
                console.error("Failed to load telescopes:", data.message);
            }
        })
        .catch(error => {
            console.error("Error loading telescopes:", error);
        });
}

function formatLastSeen(lastSeen) {
    if (!lastSeen) return "Unknown";
    const now = Date.now() / 1000;
    const diff = Math.max(0, now - lastSeen);
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

function populateTelescopeDropdown(telescopes) {
    const select = document.getElementById("telescopeSelect");
    select.innerHTML = '<option value="">Select a telescope...</option>';
    const currentSelection = selectedTelescopeId;
    
    telescopes.forEach(telescope => {
        const option = document.createElement("option");
        const displayId = telescope.telescope_id || telescope.telescopeId || "Unknown";
        const online = telescope.online ? "Online" : "Offline";
        option.value = displayId;
        option.text = `${displayId} (${online})`;
        option.dataset.telescope = JSON.stringify(telescope);
        if (currentSelection && displayId === currentSelection) {
            option.selected = true;
        }
        select.appendChild(option);
    });
}

function selectTelescope() {
    const select = document.getElementById("telescopeSelect");
    const selectedValue = select.value;
    
    if (!selectedValue) {
        document.getElementById("telescopeInfo").style.display = "none";
        selectedTelescopeId = null;
        setControlsEnabled(false);
        return;
    }
    
    const selectedOption = select.options[select.selectedIndex];
    const telescope = JSON.parse(selectedOption.dataset.telescope);
    
    // Update UI with telescope info
    const displayId = telescope.telescope_id || telescope.telescopeId || "Unknown";
    document.getElementById("telescopeName").textContent = displayId;
    document.getElementById("telescopeIp").textContent = telescope.ip_address || telescope.ipAddress || "";
    document.getElementById("telescopeType").textContent = telescope.type || telescope.telescope_type || telescope.firmwareVersion || "";
    const lastSeen = telescope.last_seen || telescope.lastSeen || null;
    document.getElementById("telescopeLastSeen").textContent = formatLastSeen(lastSeen);
    document.getElementById("telescopeInfo").style.display = "block";
    
    // Send selection to backend
    fetch("/interface/select_telescope", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ telescope_id: selectedValue })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === "success") {
            console.log("Telescope selected:", data.message);
            selectedTelescopeId = selectedValue;
            setControlsEnabled(true);
            updateLiveViewSrcForTelescope(selectedTelescopeId);
            populateCameraChoices();
            onTelescopeSelected(data.access);
        } else {
            alert("Failed to select telescope: " + data.message);
        }
    })
    .catch(error => {
        console.error("Error selecting telescope:", error);
        alert("Error selecting telescope");
    });
}

function refreshTelescopes() {
    fetch("/interface/get_telescopes")
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                populateTelescopeDropdown(data.telescopes);
                if (!selectedTelescopeId) {
                    return;
                }
                const match = data.telescopes.find(t => {
                    const id = t.telescope_id || t.telescopeId;
                    return id === selectedTelescopeId;
                });
                if (match) {
                    const displayId = match.telescope_id || match.telescopeId || "Unknown";
                    document.getElementById("telescopeName").textContent = displayId;
                    document.getElementById("telescopeIp").textContent = match.ip_address || match.ipAddress || "";
                    document.getElementById("telescopeType").textContent = match.type || match.telescope_type || match.firmwareVersion || "";
                    const lastSeen = match.last_seen || match.lastSeen || null;
                    document.getElementById("telescopeLastSeen").textContent = formatLastSeen(lastSeen);
                    document.getElementById("telescopeInfo").style.display = "block";
                }
            } else {
                console.error("Failed to load telescopes:", data.message);
            }
        })
        .catch(error => {
            console.error("Error loading telescopes:", error);
        });
}

function loadSelectedTelescope() {
    fetch("/interface/get_selected_telescope")
        .then(response => response.json())
        .then(data => {
            if (data.status === "success" && data.telescope) {
                // Set the dropdown to show the selected telescope
                const select = document.getElementById("telescopeSelect");
                const selectedId = data.telescope.telescope_id || data.telescope.telescopeId;
                for (let i = 0; i < select.options.length; i++) {
                    if (select.options[i].value === selectedId) {
                        select.selectedIndex = i;
                        // Update the info panel and local state without re-posting to backend
                        const selectedOption = select.options[select.selectedIndex];
                        const telescope = JSON.parse(selectedOption.dataset.telescope);
                        const displayId = telescope.telescope_id || telescope.telescopeId || "Unknown";
                        document.getElementById("telescopeName").textContent = displayId;
                        document.getElementById("telescopeIp").textContent = telescope.ip_address || telescope.ipAddress || "";
                        document.getElementById("telescopeType").textContent = telescope.type || telescope.telescope_type || telescope.firmwareVersion || "";
                        const lastSeen = telescope.last_seen || telescope.lastSeen || null;
                        document.getElementById("telescopeLastSeen").textContent = formatLastSeen(lastSeen);
                        document.getElementById("telescopeInfo").style.display = "block";
                        selectedTelescopeId = selectedId;
                        setControlsEnabled(true);
                        updateLiveViewSrcForTelescope(selectedTelescopeId);
                        populateCameraChoices();
                        onTelescopeSelected(data.access);
                        break;
                    }
                }
            } else {
                selectedTelescopeId = null;
                setControlsEnabled(false);
            }
        })
        .catch(error => {
            console.error("Error loading selected telescope:", error);
        });
}

// Watch the selected telescope's live view, if live view is on
function updateLiveViewSrcForTelescope(telescopeId) {
    if (!telescopeId || !liveViewPlayer) return;
    if (liveViewActive) {
        liveViewPlayer.start(telescopeId);
    }
}

// Advanced Manual Controls Functions
let motorEnabled = false;

function openAdvancedControlsModal() {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }
    
    // Wait for Bootstrap to be available
    if (typeof bootstrap === 'undefined') {
        console.error('Bootstrap is not loaded');
        return;
    }
    
    // Initialize modal with Bootstrap
    const modalElement = document.getElementById('advancedControlsModal');
    if (!modalElement) {
        console.error('Modal element not found');
        return;
    }
    
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
    
    // Load available motors
    loadAvailableMotors();
    
    // Update motor status when modal opens
    updateMotorStatusDisplay("Advanced controls opened - Ready for commands", 'info');
    
    // Reset motor state display
    updateMotorEnableButton();
}

function loadAvailableMotors() {
    if (!selectedTelescopeId) return;
    
    fetch("/interface/get_motors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ telescopeId: selectedTelescopeId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === "success" && data.motors) {
            const motorSelect = document.getElementById("motorSelect");
            if (motorSelect) {
                motorSelect.innerHTML = "";
                data.motors.forEach(motorId => {
                    const option = document.createElement("option");
                    option.value = motorId;
                    option.text = motorId === "motor1" ? "Motor 1 (Azimuth)" : 
                                 motorId === "motor2" ? "Motor 2 (Altitude)" : 
                                 motorId.charAt(0).toUpperCase() + motorId.slice(1);
                    if (motorId === selectedMotorId) {
                        option.selected = true;
                    }
                    motorSelect.appendChild(option);
                });
                console.log(`Loaded ${data.motors.length} motors:`, data.motors);
            }
        }
    })
    .catch(error => {
        console.error("Failed to load motors:", error);
    });
}

function updateSelectedMotor() {
    const motorSelect = document.getElementById("motorSelect");
    if (motorSelect) {
        selectedMotorId = motorSelect.value;
        console.log(`Selected motor: ${selectedMotorId}`);
        updateMotorStatusDisplay(`Switched to ${selectedMotorId}`, 'info');
        
        // Get status of newly selected motor
        getMotorStatus();
    }
}

function emergencyStop() {
    if (confirm("Are you sure you want to execute an emergency stop? This will immediately stop all motor movement.")) {
        sendMotorCommand("stop")
        .then(() => {
            updateMotorStatusDisplay("🚨 EMERGENCY STOP EXECUTED", 'error');
        });
    }
}

function updateMotorEnableButton() {
    const btn = document.getElementById("motorEnableBtn");
    if (!btn) return;
    
    btn.textContent = motorEnabled ? "Disable" : "Enable";
    if (motorEnabled) {
        btn.className = "btn btn-outline-danger btn-sm motor-enabled";
    } else {
        btn.className = "btn btn-outline-success btn-sm motor-disabled";
    }
}

function sendMotorCommand(command, args = null) {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return Promise.reject("No telescope selected");
    }
    
    // Show loading state
    updateMotorStatusDisplay(`Executing ${command} on ${selectedMotorId}...`, 'info');
    
    return fetch("/interface/motor_command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
            telescopeId: selectedTelescopeId,
            command: command,
            args: args,
            motor_id: selectedMotorId
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === "success") {
            console.log(`Motor command ${command} executed successfully on ${selectedMotorId}:`, data.result);
            updateMotorStatusDisplay(`${command} executed successfully on ${selectedMotorId}`, 'success');
            return data.result;
        } else {
            throw new Error(data.message || "Motor command failed");
        }
    })
    .catch(error => {
        console.error(`Motor command ${command} failed on ${selectedMotorId}:`, error);
        updateMotorStatusDisplay(`${command} failed on ${selectedMotorId}: ${error}`, 'error');
        alert(`Motor command failed: ${error}`);
        throw error;
    });
}

function updateMotorStatusDisplay(message, type = 'info') {
    const statusDiv = document.getElementById("motorStatus");
    if (!statusDiv) return;
    
    const timestamp = new Date().toLocaleTimeString();
    let className = '';
    let icon = '';
    
    switch(type) {
        case 'success':
            className = 'text-success';
            icon = '✅ ';
            break;
        case 'error':
            className = 'text-danger';
            icon = '❌ ';
            break;
        case 'warning':
            className = 'text-warning';
            icon = '⚠️ ';
            break;
        case 'info':
        default:
            className = 'text-info';
            icon = 'ℹ️ ';
            break;
    }
    
    // Append new messages instead of replacing (keep history)
    const newMessage = document.createElement('div');
    newMessage.className = className;
    newMessage.textContent = `[${timestamp}] ${icon}${message}`;
    
    // If statusDiv is getting too full, keep only last 10 messages
    const messages = statusDiv.querySelectorAll('div');
    if (messages.length >= 10) {
        statusDiv.removeChild(messages[0]);
    }
    
    statusDiv.appendChild(newMessage);
    
    // Auto-scroll to bottom
    statusDiv.scrollTop = statusDiv.scrollHeight;
}

function toggleMotorEnable() {
    const newState = !motorEnabled;
    
    sendMotorCommand("enable", [newState])
    .then(() => {
        motorEnabled = newState;
        updateMotorEnableButton();
    });
}

function setDirection(forward) {
    sendMotorCommand("set_direction", [forward])
    .then(() => {
        // Update UI to show which direction is selected
        const buttons = document.querySelectorAll('[onclick^="setDirection"]');
        buttons.forEach(btn => btn.classList.remove('active'));
        
        // Find the clicked button and mark it active
        const clickedBtn = event ? event.target : null;
        if (clickedBtn) {
            clickedBtn.classList.add('active');
        } else {
            // Fallback: find by forward/reverse text
            buttons.forEach(btn => {
                if ((forward && btn.textContent.includes('Forward')) || 
                    (!forward && btn.textContent.includes('Reverse'))) {
                    btn.classList.add('active');
                }
            });
        }
    });
}

function setSpeed() {
    const speed = parseFloat(document.getElementById("speedControl").value);
    if (isNaN(speed) || speed < 0) {
        alert("Please enter a valid speed value");
        return;
    }
    
    sendMotorCommand("set_speed", [speed]);
}

function startMotor() {
    if (!motorEnabled) {
        alert("Motor must be enabled before starting. Please enable the motor first.");
        return;
    }
    
    const speed = parseFloat(document.getElementById("speedControl").value);
    if (isNaN(speed) || speed < 0) {
        alert("Please enter a valid speed value");
        return;
    }
    
    if (speed > 1000) {
        if (!confirm(`Warning: Speed ${speed} steps/sec is quite high. Are you sure you want to proceed?`)) {
            return;
        }
    }
    
    sendMotorCommand("start", [speed]);
}

function stopMotor() {
    sendMotorCommand("stop");
}

function moveSteps() {
    if (!motorEnabled) {
        alert("Motor must be enabled before moving. Please enable the motor first.");
        return;
    }
    
    const steps = parseInt(document.getElementById("stepsControl").value);
    const speed = parseFloat(document.getElementById("speedControl").value);
    
    if (isNaN(steps) || steps <= 0) {
        alert("Please enter a valid number of steps");
        return;
    }
    
    if (isNaN(speed) || speed <= 0) {
        alert("Please enter a valid speed value");
        return;
    }
    
    if (steps > 10000) {
        if (!confirm(`Warning: Moving ${steps} steps is a large movement. Are you sure you want to proceed?`)) {
            return;
        }
    }
    
    sendMotorCommand("move_steps", [steps, speed]);
}

function setMicrosteps() {
    const microsteps = parseInt(document.getElementById("microstepsControl").value);
    sendMotorCommand("set_microsteps", [microsteps]);
}

function setCurrent() {
    const current = parseInt(document.getElementById("currentControl").value);
    if (isNaN(current) || current < 0) {
        alert("Please enter a valid current value");
        return;
    }
    
    sendMotorCommand("set_current", [current]);
}

function setAcceleration() {
    const accel = parseFloat(document.getElementById("accelControl").value);
    if (isNaN(accel) || accel < 0) {
        alert("Please enter a valid acceleration value");
        return;
    }
    
    sendMotorCommand("set_accel", [accel]);
}

function setMode() {
    const mode = document.getElementById("modeControl").value;
    sendMotorCommand("set_mode", [mode]);
}

function getMotorStatus() {
    sendMotorCommand("status")
    .then(result => {
        if (result && typeof result === 'object') {
            const statusDiv = document.getElementById("motorStatus");
            statusDiv.replaceChildren();
            const heading = document.createElement('strong');
            heading.textContent = `Motor Status (${selectedMotorId}):`;
            statusDiv.append(heading, document.createElement('br'));
            for (const [key, value] of Object.entries(result)) {
                const label = document.createElement('span');
                label.className = 'text-muted';
                label.textContent = `${key}:`;
                statusDiv.append(label, ` ${JSON.stringify(value)}`, document.createElement('br'));
            }
        } else {
            updateMotorStatusDisplay(`Status: ${result || 'Unknown'}`, 'success');
        }
    })
    .catch(error => {
        // Error handling is already done in sendMotorCommand
    });
}

function getAllMotorStatus() {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }
    
    fetch("/interface/motor_command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
            telescopeId: selectedTelescopeId,
            command: "status_all"
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === "success" && data.result) {
            let statusText = "<strong>All Motors Status:</strong><br>";
            for (const [motorId, motorStatus] of Object.entries(data.result)) {
                statusText += `<div class="mb-2"><strong class="text-primary">${motorId}:</strong><br>`;
                for (const [key, value] of Object.entries(motorStatus)) {
                    statusText += `&nbsp;&nbsp;<span class="text-muted">${key}:</span> ${JSON.stringify(value)}<br>`;
                }
                statusText += `</div>`;
            }
            document.getElementById("motorStatus").innerHTML = statusText;
        }
    })
    .catch(error => {
        console.error("Failed to get all motor status:", error);
    });
}

function trackManualCoordinates() {
    const raInput = document.getElementById("manualRA");
    const decInput = document.getElementById("manualDEC");
    
    const ra = parseFloat(raInput.value);
    const dec = parseFloat(decInput.value);
    
    // Validation
    if (isNaN(ra) || isNaN(dec)) {
        alert("Please enter valid RA and DEC coordinates");
        return;
    }
    
    if (ra < 0 || ra > 360) {
        alert("RA must be between 0 and 360 degrees");
        return;
    }
    
    if (dec < -90 || dec > 90) {
        alert("DEC must be between -90 and 90 degrees");
        return;
    }
    
    // Create a manual object for tracking
    const manualObject = {
        name: `Manual: RA ${ra.toFixed(4)}°, DEC ${dec.toFixed(4)}°`,
        ra: ra,
        dec: dec,
        mag: 0
    };
    
    // Use the existing trackObject function
    trackObject(manualObject);
    
    // Clear the input fields after tracking
    raInput.value = "";
    decInput.value = "";
}

/**
 * Update the tracking status panel with the current object being tracked
 * @param {Object} trackingData - Object containing name, ra, dec, mag
 */
function updateTrackingPanel(trackingData) {
    console.log('=== updateTrackingPanel CALLED ===');
    console.log('trackingData:', trackingData);
    
    const statusContent = document.getElementById('trackingStatusContent');
    const stopBtn = document.getElementById('stopTrackingBtn');
    
    console.log('statusContent element found:', !!statusContent);
    console.log('stopBtn element found:', !!stopBtn);
    
    if (!statusContent) {
        console.error('❌ ERROR: trackingStatusContent element not found!');
        console.error('Available elements:', document.body.innerHTML.substring(0, 500));
        return;
    }
    
    if (trackingData && trackingData.name) {
        const raValue = parseFloat(trackingData.ra);
        const decValue = parseFloat(trackingData.dec);
        const magValue = parseFloat(trackingData.mag);
        
        const ra = !isNaN(raValue) ? raValue.toFixed(4) : trackingData.ra;
        const dec = !isNaN(decValue) ? decValue.toFixed(4) : trackingData.dec;
        const mag = !isNaN(magValue) ? magValue.toFixed(2) : trackingData.mag;
        
        console.log('✓ Updating panel with tracking info:');
        console.log('  name:', trackingData.name);
        console.log('  ra:', ra);
        console.log('  dec:', dec);
        console.log('  mag:', mag);
        
        const htmlContent = `
            <div style="color: #333;">
                <strong style="color: #0d6efd; display: block; margin-bottom: 6px;">✓ Now Tracking:</strong>
                <div><strong>${trackingData.name}</strong></div>
                <hr style="margin: 8px 0;">
                <div><small><strong>RA:</strong> ${ra}°</small></div>
                <div><small><strong>DEC:</strong> ${dec}°</small></div>
                <div><small><strong>Magnitude:</strong> ${mag}</small></div>
            </div>
        `;
        
        statusContent.innerHTML = htmlContent;
        statusContent.className = 'text-dark small'; // Change from text-muted to text-dark
        statusContent.style.color = '#333';
        
        console.log('✓ Panel HTML updated');
        console.log('New HTML:', statusContent.innerHTML);
        
        if (stopBtn) {
            stopBtn.style.display = 'block';
            console.log('✓ Stop button shown');
        }
        
        // Store tracking state in sessionStorage for persistence across page reloads
        sessionStorage.setItem('currentTracking', JSON.stringify(trackingData));
        console.log('✓ Tracking data saved to sessionStorage');
    } else {
        // No object being tracked
        console.log('! Clearing tracking panel (no tracking data)');
        statusContent.innerHTML = '<em>No object being tracked</em>';
        statusContent.className = 'text-muted small';
        if (stopBtn) {
            stopBtn.style.display = 'none';
        }
        sessionStorage.removeItem('currentTracking');
    }
}

/**
 * Stop tracking the current object
 */
function stopTracking() {
    if (!selectedTelescopeId) {
        updateMotorStatusDisplay('⚠️ No telescope selected', 'warning');
        return;
    }
    
    console.log('=== stopTracking CALLED ===');
    console.log('Sending stop tracking command to telescope:', selectedTelescopeId);
    
    // Send stop tracking command directly to star_map endpoint
    fetch("/stop_tracking", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
    })
    .then(response => response.json())
    .then(data => {
        console.log('Stop tracking response:', data);
        if (data.status === "stopped") {
            // Clear tracking panel
            updateTrackingPanel(null);
            updateMotorStatusDisplay('🛑 Tracking stopped successfully', 'success');
            console.log('✓ Tracking stopped successfully');
        } else {
            throw new Error(data.message || "Failed to stop tracking");
        }
    })
    .catch(error => {
        console.error('✗ Error stopping tracking:', error);
        updateMotorStatusDisplay(`❌ Failed to stop tracking: ${error}`, 'error');
        
        // Still clear the panel even if command failed
        updateTrackingPanel(null);
    });
}

/**
 * Load and display current tracking status on page load
 */
function loadTrackingStatus() {
    // First check sessionStorage for recent tracking state
    const storedTracking = sessionStorage.getItem('currentTracking');
    if (storedTracking) {
        try {
            const trackingData = JSON.parse(storedTracking);
            updateTrackingPanel(trackingData);
        } catch (e) {
            console.log('Could not parse tracking data from sessionStorage');
        }
    }
    
    // Also fetch from server to get the authoritative state
    fetch('/get_tracking_status')
        .then(response => response.json())
        .then(data => {
            if (data.tracking && data.object) {
                updateTrackingPanel(data.object);
            }
        })
        .catch(error => console.log('Could not fetch tracking status from server'));
}

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
