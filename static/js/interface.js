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

        // Enable/disable UI controls based on telescope selection
        function setControlsEnabled(enabled) {
            const ids = [
                "shutterSpeedSelect",
                "isoSelect",
                "whiteBalance",
                "photoFormat",
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

// Fetch camera choices and populate dropdowns
function populateCameraChoices() {
            if (!selectedTelescopeId) {
                console.warn("Skipping camera choices fetch: no telescope selected");
                return;
            }

            fetch("/interface/get_camera_choices")
        .then(response => response.json())
        .then(data => {
                    if (data && data.status === "error") {
                        console.error("Failed to fetch camera choices:", data.message);
                        return;
                    }
            // Populate shutter speed
            let shutterSelect = document.getElementById("shutterSpeedSelect");
            shutterSelect.innerHTML = "";
                    (data.shutterSpeed || []).forEach(choice => {
                let opt = document.createElement("option");
                opt.value = choice;
                opt.text = choice;
                shutterSelect.appendChild(opt);
            });

            // Populate ISO
            let isoSelect = document.getElementById("isoSelect");
            isoSelect.innerHTML = "";
            (data.iso || []).forEach(choice => {
                let opt = document.createElement("option");
                opt.value = choice;
                opt.text = choice;
                isoSelect.appendChild(opt);
            });
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
    initializeLiveHistogram();
    
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

// Update settings to backend
function updateSetting() {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }
    let data = {
        shutterSpeed: document.getElementById("shutterSpeedSelect").value,
        iso: document.getElementById("isoSelect").value,
        aperture: document.getElementById("apertureValue").innerText,
        whiteBalance: document.getElementById("whiteBalance").value,
        photoFormat: document.getElementById("photoFormat").value,
        telescope_id: selectedTelescopeId
    };
    fetch("/interface/update_camera", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data)
    }).then(response => response.json())
    .then(data => console.log("Response from server:", data))
    .catch(error => console.error("Fetch error:", error));
}

function takePhoto() {
    if (!selectedTelescopeId) {
        alert("Please select a telescope first.");
        return;
    }
    fetch("/interface/take_photo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ telescope_id: selectedTelescopeId })
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
    });
}

// Live View Toggle Logic
let liveViewActive = false;
let liveViewImage = null;
let refreshInterval = null;
let liveHistogram = null;
const REFRESH_RATE = 1000; // Refresh every 1 second

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

    const image = document.getElementById('liveViewImage');
    if (liveHistogram && image) {
        image.crossOrigin = 'anonymous';
        liveHistogram.setSourceImage(image);
    }
}

// Function to start refreshing the live view image
function startImageRefresh() {
    if (!liveViewImage) {
        liveViewImage = document.getElementById('liveViewImage');
    }
    
    if (liveViewImage && !refreshInterval) {
        const baseUrl = liveViewImage.src.split('?')[0]; // Remove any existing query params
        
        refreshInterval = setInterval(() => {
            // Add a timestamp to prevent caching
            liveViewImage.src = baseUrl + '?t=' + new Date().getTime();
        }, REFRESH_RATE);
        
        console.log("Image refresh started");
    }
}

// Function to stop refreshing the live view image
function stopImageRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
        refreshInterval = null;
        console.log("Image refresh stopped");
    }
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
    
    // console.log("Button found, current liveViewActive state:", liveViewActive);
    
    if (liveViewActive) {
        // Stop live view
        console.log("Attempting to stop live view...");
        console.log("Making POST request to /interface/stop_live_view");
        fetch("/interface/stop_live_view", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ telescope_id: selectedTelescopeId })
        })
        .then(response => {
            console.log("Stop live view response received:", response);
            return response.json();
        })
        .then(data => {
            console.log("Stop live view data:", data);
            if (data.status === "success") {
                liveViewActive = false;
                btn.textContent = "Start";
                btn.classList.remove("btn-outline-danger");
                btn.classList.add("btn-outline-primary");
                stopImageRefresh(); // Stop refreshing the image
                console.log("Live view stopped successfully");
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
        // Start live view
        console.log("Attempting to start live view...");
        console.log("Making POST request to /interface/start_live_view");
        // Ensure the image src matches the selected telescope before starting refresh
        updateLiveViewSrcForTelescope(selectedTelescopeId);
        fetch("/interface/start_live_view", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ telescope_id: selectedTelescopeId })
        })
        .then(checkAuthResponse)
        .then(response => {
            console.log("Start live view response received:", response);
            return response.json();
        })
        .then(data => {
            console.log("Start live view data:", data);
            if (data.status === "success") {
                liveViewActive = true;
                btn.textContent = "Stop";
                btn.classList.remove("btn-outline-primary");
                btn.classList.add("btn-outline-danger");
                startImageRefresh(); // Start refreshing the image
                console.log("Live view started successfully");
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

// Update the main live view image src to point to the selected telescope
function updateLiveViewSrcForTelescope(telescopeId) {
    if (!telescopeId) return;
    if (!liveViewImage) {
        liveViewImage = document.getElementById('liveViewImage');
    }
    if (!liveViewImage) return;

    liveViewImage.crossOrigin = 'anonymous';

    const domain = (window.APP_DOMAIN && typeof window.APP_DOMAIN === 'string') ? window.APP_DOMAIN : 'telescopes.dev';
    const newSrc = `https://${domain}/liveview/${encodeURIComponent(telescopeId)}`;
    const wasRefreshing = !!refreshInterval;
    if (wasRefreshing) {
        stopImageRefresh();
    }
    liveViewImage.src = newSrc;
    if (wasRefreshing) {
        startImageRefresh();
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

