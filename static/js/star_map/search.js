// Star map - object search and flying the view to a result.

// Search state
let searchedObject = null;
let highlightAnimation = 0;
let searchHighlightUntil = 0; // the ring pulses until this time, then sits still

// Function to search for objects
function searchObject() {
    const searchValue = searchInput.value.trim();
    if (!searchValue) {
        alert('Please enter a search term.');
        return;
    }

    // Check if searching for "telescope"
    if (searchValue.toLowerCase() === 'telescope') {
        if (!isTelescopeSelected()) {
            alert('No telescope selected. Please select a telescope first.');
            return;
        }
        
        if (!telescopePosition && !telescopePositionAvailable) {
            // Try fetching once more before giving up
            fetch('/api/telescope_position')
                .then(r => r.json())
                .then(data => {
                    if (data && data.status === 'success' && data.ra !== null && data.dec !== null) {
                        telescopePosition = {
                            ra: data.ra,
                            dec: data.dec,
                            timestamp: Date.now()
                        };
                        telescopePositionAvailable = true;
                        console.log('Telescope position fetched on demand:', data.ra, data.dec);
                        performTelescopeSearch();
                    } else {
                        console.warn('Telescope position search failed:', data.message || 'Unknown error');
                        alert(`Telescope position not available: ${data.message || 'No telescope selected or unable to contact telescope'}`);
                    }
                })
                .catch(err => {
                    console.error('Telescope search error:', err);
                    alert('Error fetching telescope position. Check browser console for details.');
                });
        } else if (telescopePosition) {
            performTelescopeSearch();
        } else {
            alert('Telescope position not available. Make sure a telescope is selected.');
        }
        return;
    }

    fetch('/interface/search_object', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ searchValue: searchValue })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success' && data.data) {
            const objData = data.data;
            // Find the object in our stars array or use the search result
            let foundObject = null;
            
            // First try the solar-system objects, then the star catalogue
            for (const obj of stars) {
                if (obj.name && objData.Name && 
                    obj.name.toLowerCase() === objData.Name.toLowerCase()) {
                    foundObject = obj;
                    break;
                }
            }
            if (!foundObject && objData.Name) {
                const catalogIndex = findStarByName(objData.Name);
                if (catalogIndex >= 0) {
                    foundObject = starObjectAt(catalogIndex);
                    foundObject.xyz = radecToXYZ(foundObject.ra, foundObject.dec);
                }
            }
            
            // If not found in stars array, create from search result
            if (!foundObject) {
                foundObject = {
                    name: objData.Name,
                    ra: parseFloat(objData.RA) || 0,
                    dec: parseFloat(objData.DEC) || 0,
                    mag: (objData['V-Mag'] === undefined || objData['V-Mag'] === null)
                        ? null
                        : objData['V-Mag'],
                    type: objData.type || 'star',
                    friendlyName: objData.friendlyName || null,
                    phase_name: objData.phase_name || null,
                    phase_angle_deg: (objData.phase_angle_deg !== undefined && objData.phase_angle_deg !== null)
                        ? parseFloat(objData.phase_angle_deg)
                        : null,
                    moon_illumination_fraction: (objData.moon_illumination_fraction !== undefined && objData.moon_illumination_fraction !== null)
                        ? parseFloat(objData.moon_illumination_fraction)
                        : null,
                    moon_elongation_deg: (objData.moon_elongation_deg !== undefined && objData.moon_elongation_deg !== null)
                        ? parseFloat(objData.moon_elongation_deg)
                        : null
                };
                // Precompute xyz for transient search result
                // Note: Do NOT invert Y here; projection already handles canvas Y direction
                const _tmp = radecToXYZ(foundObject.ra, foundObject.dec);
                foundObject.xyz = _tmp;
            } else {
                // Update friendlyName if returned from search
                if (objData.friendlyName) {
                    foundObject.friendlyName = objData.friendlyName;
                }
            }
            
            // Set as searched object and move camera to it
            searchedObject = foundObject;
            highlightAnimation = 0;
            searchHighlightUntil = Date.now() + 8000;
            moveToObject(foundObject);
            
            // Automatically adjust settings so the found object is actually visible
            if ((foundObject.mag == null || Number.isNaN(foundObject.mag))
                && showUnknownMag && !showUnknownMag.checked) {
                showUnknownMag.checked = true;
                loadRemainingStarBands();
            }
            const objectMag = foundObject.mag == null ? 6 : foundObject.mag;
            const currentMagLimit = parseFloat(magFilter.value);
            
            // If the object is fainter than current limit, increase the magnitude limit
            if (objectMag > currentMagLimit) {
                const newMagLimit = Math.ceil(objectMag) + 0.3;
                const maxSliderMag = parseFloat(magFilter.max) || 20;
                const clampedMagLimit = Math.min(newMagLimit, maxSliderMag);
                
                magFilter.value = clampedMagLimit.toFixed(1);
                magValue.textContent = clampedMagLimit.toFixed(1);
                
                // Temporarily disable auto magnitude-zoom linking if it's enabled
                if (magnitudeZoomEnabled) {
                    magnitudeZoomEnabled = false;
                    if (autoMagnitudeZoom) {
                        autoMagnitudeZoom.checked = false;
                    }
                    
                    // Clear any existing timeout
                    if (manualMagnitudeTimeout) {
                        clearTimeout(manualMagnitudeTimeout);
                    }
                    
                    // Re-enable auto magnitude-zoom linking after 5 seconds
                    manualMagnitudeTimeout = setTimeout(() => {
                        magnitudeZoomEnabled = true;
                        if (autoMagnitudeZoom) {
                            autoMagnitudeZoom.checked = true;
                        }
                        console.log('Auto magnitude-zoom linking re-enabled after search');
                    }, 5000);
                }
                
                // Fetch more stars if needed for the new magnitude limit
                fetchMoreStarsIfNeeded(clampedMagLimit);
                
                console.log(`Adjusted magnitude limit from ${currentMagLimit} to ${clampedMagLimit} to show ${foundObject.name} (mag ${objectMag})`);
            }
            
            // Show info with same format as click handler (two-button layout)
            const lstDeg2 = lstDegrees(new Date((timeControl && timeControl.value) ? new Date(timeControl.value).toISOString() : new Date().toISOString()), parseFloat(lonInput.value));
            const ha2 = hourAngleDegrees(foundObject.ra, lstDeg2);
            
            // Convert coordinates to HMS/DMS format like click handler
            const raHMS = decimalToHMS(foundObject.ra);
            const decDMS = decimalToDMS(foundObject.dec);
            const displayMagFormatted = (foundObject.mag == null || Number.isNaN(foundObject.mag))
                ? "not recorded"
                : foundObject.mag.toFixed(2);
            
            const displayName = foundObject.friendlyName 
                ? `${foundObject.name} (${foundObject.friendlyName})`
                : foundObject.name;
                
            // Store the full data for advanced info modal 
            window.currentStarData = { ...foundObject, hourAngle: ha2 };
                
            document.getElementById('info').innerHTML = 
                `<b>🔍 ${displayName}</b><br>RA: ${raHMS}<br>DEC: ${decDMS}<br>V-Mag: ${displayMagFormatted}<br>
                 <div style="margin-top: 5px; display: flex; gap: 4px;">
                    <button onclick="trackObject('${foundObject.name}', ${foundObject.ra}, ${foundObject.dec}, ${foundObject.mag})" style="padding: 4px 8px; background: #4CAF50; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">Track</button>
                    <button onclick="showStarInfoModal(window.currentStarData)" style="padding: 4px 8px; background: #007bff; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">Advanced Info</button>
                 </div>`;
        } else {
            alert(data.message || 'Object not found.');
        }
    })
    .catch(error => {
        console.error('Search error:', error);
        alert('Search failed. Please try again.');
    });
}

// Function to move camera to look at an object
function moveToObject(obj) {
    // Observer/time
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}

    function testRotation(testRotX, testRotY) {
        const { altDeg, azDeg } = radecToAltAz(obj.ra, obj.dec, selectedDate, latDeg, lonDeg);
        let [x, y, z] = altazToXYZ(altDeg, azDeg);
        [x, y, z] = rotate([x, y, z], testRotX, testRotY, 0, 0);
        if (z <= 0) return null; // Behind camera
        const [screenX, screenY] = project([x, y, z]);
        return [screenX, screenY, z];
    }

    const centerX = width / 2;
    const centerY = height / 2;
    let bestRotX = rotX;
    let bestRotY = rotY;
    let bestDistance = Infinity;

    const searchRange = Math.PI; // 180 degrees
    const searchSteps = 20;

    for (let i = 0; i < searchSteps; i++) {
        for (let j = 0; j < searchSteps; j++) {
            const testRotY = rotY + (i - searchSteps/2) * searchRange / searchSteps;
            const testRotX = rotX + (j - searchSteps/2) * searchRange / searchSteps;
            const constrainedRotX = Math.max(-Math.PI/2, Math.min(Math.PI/2, testRotX));
            const result = testRotation(constrainedRotX, testRotY);
            if (result) {
                const [screenX, screenY, z] = result;
                const distance = Math.hypot(screenX - centerX, screenY - centerY);
                if (distance < bestDistance && z > 0) {
                    bestDistance = distance;
                    bestRotX = constrainedRotX;
                    bestRotY = testRotY;
                }
            }
        }
    }

    if (bestDistance > 100) {
        for (let i = 0; i < searchSteps; i++) {
            for (let j = 0; j < searchSteps; j++) {
                const testRotY = (i / searchSteps) * 2 * Math.PI - Math.PI;
                const testRotX = (j / searchSteps) * Math.PI - Math.PI/2;
                const constrainedRotX = Math.max(-Math.PI/2, Math.min(Math.PI/2, testRotX));
                const result = testRotation(constrainedRotX, testRotY);
                if (result) {
                    const [screenX, screenY, z] = result;
                    const distance = Math.hypot(screenX - centerX, screenY - centerY);
                    if (distance < bestDistance && z > 0) {
                        bestDistance = distance;
                        bestRotX = constrainedRotX;
                        bestRotY = testRotY;
                    }
                }
            }
        }
    }

    while (bestRotY - rotY > Math.PI) bestRotY -= 2*Math.PI;
    while (bestRotY - rotY < -Math.PI) bestRotY += 2*Math.PI;

    const startRotX = rotX;
    const startRotY = rotY;
    const steps = 30;
    let step = 0;

    function animateMove() {
        if (step <= steps) {
            const progress = step / steps;
            const eased = 1 - Math.pow(1 - progress, 3);
            rotX = startRotX + (bestRotX - startRotX) * eased;
            rotY = startRotY + (bestRotY - startRotY) * eased;
            draw();
            step++;
            requestAnimationFrame(animateMove);
        }
    }

    animateMove();
}

// Function to clear search
function clearSearch() {
    searchedObject = null;
    searchInput.value = '';
    document.getElementById('info').innerHTML = defaultInfoText();
    draw();
}

// Helper function to perform telescope search
function performTelescopeSearch() {
    if (!telescopePosition) {
        console.error('performTelescopeSearch called but telescopePosition is null');
        alert('Telescope position not available.');
        return;
    }
    
    // Create a searchedObject from telescopePosition
    const telescopeObj = {
        name: 'Telescope',
        ra: telescopePosition.ra,
        dec: telescopePosition.dec,
        mag: -99, // Very bright so it shows up
        type: 'telescope'
    };
    
    // Set as searched object and move camera to it
    searchedObject = telescopeObj;
    highlightAnimation = 0;
    moveToObject(telescopeObj);
    
    // Show info
    const raHMS = decimalToHMS(telescopeObj.ra);
    const decDMS = decimalToDMS(telescopeObj.dec);
    
    window.currentStarData = { ...telescopeObj };
    
    document.getElementById('info').innerHTML = 
        `<b>🔍 Telescope Position</b><br>RA: ${raHMS}<br>DEC: ${decDMS}<br>
         <div style="margin-top: 5px; display: flex; gap: 4px;">
            <button onclick="clearSearch()" style="padding: 4px 8px; background: #999; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">Clear</button>
         </div>`;
    
    draw();
}
