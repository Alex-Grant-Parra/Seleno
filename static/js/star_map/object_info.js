// Star map - picking objects on the canvas and showing their details (the info
// panel and the advanced info modal).

// Default hint shown in the info panel when nothing is selected
function defaultInfoText() {
    return isTouchDevice
        ? "Swipe to rotate. Pinch to zoom. Tap a star/planet for info."
        : "Drag to rotate. Click a star/planet for info.";
}

// Render the info panel for a picked object. `data` is the /star_info payload
// when we have it, otherwise null and we fall back to what the map already knows.
function showObjectInfoPanel(obj, data, ha) {
    const raHMS = decimalToHMS(obj.ra);
    const decDMS = decimalToDMS(obj.dec);
    const displayMagFormatted = (obj.mag == null || Number.isNaN(obj.mag))
        ? "not recorded"
        : obj.mag.toFixed(2);

    // The proper name is already on the client, so it goes in on the first
    // paint rather than a round trip later.
    const local = starNameById.get(obj.name);
    const friendlyName = (data && data.friendlyName) || (local && local.name) || null;
    const displayName = friendlyName
        ? `${(data && data.name) || obj.name} (${friendlyName})`
        : ((data && data.name) ? data.name : obj.name);
    const btnPadding = isTouchDevice ? '10px 8px' : '4px 8px';

    // Store the full data for the advanced info modal with additional context
    window.currentStarData = data
        ? { ...data, ra: obj.ra, dec: obj.dec, hourAngle: ha }
        : { name: obj.name, ra: obj.ra, dec: obj.dec, mag: obj.mag, hourAngle: ha };
    if (!window.currentStarData.friendlyName && friendlyName) {
        window.currentStarData.friendlyName = friendlyName;
    }
    if (!window.currentStarData.bayer && local && local.bayer) {
        window.currentStarData.bayer = local.bayer;
    }

    const info = document.getElementById('info');
    info.replaceChildren();
    const title = document.createElement('b');
    title.textContent = displayName;
    info.append(
        title, document.createElement('br'),
        `RA: ${raHMS}`, document.createElement('br'),
        `DEC: ${decDMS}`, document.createElement('br'),
        `V-Mag: ${displayMagFormatted}`, document.createElement('br')
    );

    const buttons = document.createElement('div');
    buttons.style.cssText = 'margin-top: 5px; display: flex; gap: 4px;';
    const makeButton = (label, background, onClick) => {
        const btn = document.createElement('button');
        btn.textContent = label;
        btn.style.cssText = `padding: ${btnPadding}; background: ${background}; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;`;
        btn.addEventListener('click', onClick);
        return btn;
    };
    buttons.append(
        makeButton('Track', '#4CAF50', () => trackObject(obj.name, obj.ra, obj.dec, obj.mag)),
        makeButton('Advanced Info', '#007bff', () => showStarInfoModal(window.currentStarData))
    );
    info.appendChild(buttons);
}

// Nearest catalogue star to a screen point, or null. Mirrors the draw loop so
// what you can click is exactly what you can see.
function pickCatalogueStar(mx, my, magLimit, minHitRadius) {
    refreshStarSizeLUT();
    const { Mview } = buildEqToViewMatrix(
        parseFloat(latInput.value) || 0, currentLSTDeg, rotX, rotY);
    const m00 = Mview[0][0], m01 = Mview[0][1], m02 = Mview[0][2];
    const m10 = Mview[1][0], m11 = Mview[1][1], m12 = Mview[1][2];
    const m20 = Mview[2][0], m21 = Mview[2][1], m22 = Mview[2][2];
    const k2 = 2 * Math.max(width, height) * 0.35 * zoom;
    const halfW = width / 2, halfH = height / 2;

    let best = -1, bestDist2 = Infinity;

    function scan(from, to, fixedRadius) {
        for (let i = from; i < to; i++) {
            const ax = starVX[i], ay = starVY[i], az = starVZ[i];
            const z = m20 * ax + m21 * ay + m22 * az;
            if (z <= 0) continue;
            const x = m00 * ax + m01 * ay + m02 * az;
            const y = m10 * ax + m11 * ay + m12 * az;
            const scale = k2 / (1 + z);
            const px = halfW + x * scale;
            const dx = mx - px;
            if (dx > 64 || dx < -64) continue;
            const py = halfH - y * scale;
            const dy = my - py;
            if (dy > 64 || dy < -64) continue;
            const radius = Math.max(fixedRadius !== undefined ? fixedRadius : starSizeForMag(starMag[i]),
                                    minHitRadius);
            const dist2 = dx * dx + dy * dy;
            if (dist2 < radius * radius * 1.5 && dist2 < bestDist2) {
                best = i;
                bestDist2 = dist2;
            }
        }
    }

    scan(0, starsBrighterThan(magLimit));
    if (showUnknownMag && showUnknownMag.checked) {
        const [uStart, uEnd] = unknownMagRange();
        scan(uStart, uEnd, Math.max(1, Math.min(3, 1 + zoom * 0.35)));
    }

    return best >= 0 ? { index: best, dist2: bestDist2 } : null;
}

// Pick the object nearest a screen point and show its info.
// `touch` widens the hit area to a finger-sized target.
function selectObjectAt(mx, my, { touch = false } = {}) {
    const magLimit = parseFloat(magFilter.value);
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    const minHitRadius = touch ? 18 : 0; // fingers are much blunter than a cursor
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}

    let picked = null, pickedDist2 = Infinity;

    // Solar-system objects first (a short object list)
    for (const obj of stars) {
        const effectiveMag = obj.mag == null ? 50 : obj.mag;
        if (effectiveMag > magLimit) continue;
        if (obj.type === "star" && !showStars.checked) continue;
        if (obj.type === "planet" && !showPlanets.checked) continue;

        const { altDeg, azDeg } = radecToAltAz(obj.ra, obj.dec, selectedDate, latDeg, lonDeg);
        let [x, y, z] = altazToXYZ(altDeg, azDeg);
        [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
        if (z <= 0) continue;
        const [cx, cy] = project([x, y, z]);

        let size, hitRadius;
        if (obj.type === "planet") {
            size = getZoomedPlanetSize();
            hitRadius = size / 2;
        } else {
            const baseMagnitudeSize = getMagnitudeBasedSize(effectiveMag);
            size = getZoomedStarSize(baseMagnitudeSize);
            hitRadius = size;
        }
        hitRadius = Math.max(hitRadius, minHitRadius);

        // With a finger-sized target several objects can overlap, so keep the closest
        const dist2 = (mx-cx)**2 + (my-cy)**2;
        if (dist2 < hitRadius*hitRadius*1.5 && dist2 < pickedDist2) {
            picked = obj;
            pickedDist2 = dist2;
        }
    }

    // Then the catalogue, using the same projection the renderer uses
    if (showStars.checked && starMag && starLoadedCount > 0) {
        const hit = pickCatalogueStar(mx, my, magLimit, minHitRadius);
        if (hit && hit.dist2 < pickedDist2) {
            picked = starObjectAt(hit.index);
            pickedDist2 = hit.dist2;
        }
    }

    if (!picked) {
        document.getElementById('info').innerHTML = defaultInfoText();
        return false;
    }

    const lstDegNow = lstDegrees(new Date(selectedDate.toISOString()), lonDeg);
    const ha = hourAngleDegrees(picked.ra, lstDegNow);

    // Show what we already have immediately, then refine with the catalog entry
    showObjectInfoPanel(picked, null, ha);
    fetch(`/star_info/${encodeURIComponent(picked.name)}`)
        .then(response => response.json())
        .then(data => {
            showObjectInfoPanel(picked, (data && !data.error) ? data : null, ha);
        })
        .catch(() => {
            showObjectInfoPanel(picked, null, ha);
        });
    return true;
}

// Advanced info modal functions (from interface.js)
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
    const excludeKeys = ['name', 'Name', 'friendlyName'];
    const propertyMappings = {
        'ra': 'RA (decimal degrees)',
        'RA': 'RA (decimal degrees)',
        'dec': 'DEC (decimal degrees)',
        'DEC': 'DEC (decimal degrees)',
        'hourAngle': 'Hour Angle (degrees)',
        'phase_name': 'Phase Name',
        'phase_angle_deg': 'Phase Angle (degrees)',
        'moon_illumination_fraction': 'Moon Illumination Fraction',
        'moon_elongation_deg': 'Moon Elongation (degrees)',
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
        'Common names': 'All Names',
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
    
    advancedHtml += '<div class="table-responsive"><table class="table table-sm table-hover" style="color: inherit;">';
    advancedHtml += '<thead><tr><th>Property</th><th>Value</th></tr></thead><tbody>';
    
    for (const [key, value] of Object.entries(star)) {
        if (excludeKeys.includes(key) || value === null || value === undefined || value === "") continue;
        
        const displayName = propertyMappings[key] || key.replace(/([A-Z])/g, ' $1').trim();
        let displayValue = value;
        
        // Format numeric values
        if (typeof value === 'number' && !Number.isInteger(value)) {
            displayValue = value.toFixed(2);
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
    
    if (advancedInfo && toggleBtn) {
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
}

function showStarInfoModal(star) {
    const existingModal = document.getElementById("starInfoModal");
    if (existingModal) existingModal.remove();

    // Extract basic information with proper fallbacks
    const name = star.name || star.Name || "Unknown";
    const commonName = star.friendlyName || extractCommonName(star.commonNames || star['Common names']) || "";
    const raDecimal = parseFloat(star.ra !== undefined ? star.ra : star.RA || 0);
    const decDecimal = parseFloat(star.dec !== undefined ? star.dec : star.DEC || 0);
    const raHMS = decimalToHMS(raDecimal);
    const decDMS = decimalToDMS(decDecimal);
    let magnitude = star.mag !== undefined ? star.mag : star["V-Mag"];
    if (magnitude === null || magnitude === undefined || Number.isNaN(magnitude)) {
        // The catalogue has no V-Mag for this object (NULL, or an HD 20/30/40/50
        // placeholder) - say that rather than showing a fabricated number
        magnitude = "not recorded";
    }

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
        z-index: 10000;
        width: min(500px, calc(100vw - 24px));
        max-height: 88vh;
        max-height: 88dvh;
        overflow-y: auto;
        overscroll-behavior: contain;
        -webkit-overflow-scrolling: touch;
        box-shadow: 0 0.5rem 1rem rgba(0, 0, 0, 0.5);
        border: none;
    `;

    modal.innerHTML = `
        <div class="modal-header" style="background: linear-gradient(135deg, #007bff, #0056b3); color: white; padding: 1rem; border-bottom: none;">
            <h5 class="modal-title" style="margin: 0; font-weight: 600;">🌟 Object Information</h5>
            <button type="button" class="btn-close" id="closeStarInfo" style="filter: invert(1); background: none; border: none; font-size: 1.5rem; cursor: pointer; color: white; line-height: 1; padding: 0; width: 32px; height: 32px; touch-action: manipulation;">&times;</button>
        </div>
        <div class="modal-body" style="padding: 1.5rem;">
            <!-- Basic Information -->
            <div class="basic-info">
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>Identifier:</strong> <span style="color: #007bff;">${name}</span>${commonName ? `  <span style="color: #28a745;">(${commonName})</span>` : ''}
                </div>
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>RA:</strong> <span style="color: #17a2b8;">${raHMS}</span>
                </div>
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>DEC:</strong> <span style="color: #17a2b8;">${decDMS}</span>
                </div>
                <div class="info-item" style="margin-bottom: 1rem;">
                    <strong>Magnitude:</strong> <span style="color: #17a2b8;">${magnitude}</span>
                </div>
            </div>
            
            <!-- Advanced Info Toggle -->
            <div class="advanced-toggle" style="margin: 1.5rem 0;">
                <button id="toggleAdvancedInfo" class="btn btn-outline-secondary btn-sm w-100" onclick="toggleAdvancedObjectInfo()" style="padding: 12px 8px; touch-action: manipulation; border: 1px solid #6c757d; background: white; color: #6c757d; border-radius: 4px; cursor: pointer; width: 100%;">
                    📊 Show Advanced Information
                </button>
            </div>
            
            <!-- Advanced Information (Initially Hidden) -->
            <div id="advancedObjectInfo" class="advanced-info" style="display: none; padding: 1rem; background-color: #f8f9fa; border-radius: 8px; border-left: 4px solid #007bff; max-height: 300px; overflow-y: auto;">
                ${generateAdvancedInfo(star)}
            </div>
        </div>
        <div class="modal-footer" style="padding: 1rem; background-color: #f8f9fa; border-top: 1px solid #dee2e6; display: flex; justify-content: space-between; gap: 8px;">
            <button id="trackObjectBtnModal" class="btn btn-success" style="padding: 12px 16px; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; flex: 1; touch-action: manipulation;">🎯 Track Object</button>
            <button id="closeStarInfoFooter" class="btn btn-secondary" style="padding: 12px 16px; background: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; flex: 1; touch-action: manipulation;">Close</button>
        </div>
    `;

    document.body.appendChild(modal);

    // Event listeners
    document.getElementById("closeStarInfo").addEventListener("click", () => modal.remove());
    document.getElementById("closeStarInfoFooter").addEventListener("click", () => modal.remove());
    
    const raVal = star.ra !== undefined ? star.ra : star.RA;
    const decVal = star.dec !== undefined ? star.dec : star.DEC;
    const magVal = star.mag !== undefined ? star.mag : star["V-Mag"];
    
    document.getElementById("trackObjectBtnModal").addEventListener("click", () => {
        trackObject(name, raVal, decVal, magVal);
        modal.remove();
    });

    // Close on outside click
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            modal.remove();
        }
    });
}
