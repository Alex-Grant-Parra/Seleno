// Star map - object search, its suggestions, and flying the view to a result.

// Search state
let searchedObject = null;
let highlightAnimation = 0;
let searchHighlightUntil = 0; // the ring pulses until this time, then sits still

const searchSuggestions = document.getElementById('search-suggestions');
let suggestTimer = 0;
let suggestSeq = 0;      // drops answers to queries the box no longer holds
let suggestItems = [];
let suggestActive = -1;

// Lower case without accents, as the server compares names ("Orión" = "orion")
function searchFold(text) {
    return String(text || '').normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase().trim();
}

function escapeSearchHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function isTelescopeQuery(text) {
    return ['telescope', _('telescope').toLowerCase()].includes(String(text).trim().toLowerCase());
}

// What a match is, for the grey hint beside it: the name it matched when that
// isn't the name shown (another language, an alias), otherwise its kind
function searchMatchHint(match) {
    if (match.match !== 'number' && match.matched && searchFold(match.matched) !== searchFold(match.name)) {
        return match.matched;
    }
    if (match.kind === 'body') return _('Solar System');
    if (match.kind === 'constellation') return _('Constellation');
    return match.key !== match.name ? match.key : '';
}

// Buttons for a list of matches; wireSearchChoices() makes them search
function searchChoicesHtml(matches) {
    return `<div class="search-choices">${matches.map(m => {
        const hint = searchMatchHint(m);
        return `<button type="button" data-pick-kind="${escapeSearchHtml(m.kind)}" data-pick-key="${escapeSearchHtml(m.key)}"`
            + ` data-pick-name="${escapeSearchHtml(m.name)}"${hint ? ` title="${escapeSearchHtml(hint)}"` : ''}>`
            + `${escapeSearchHtml(m.name)}</button>`;
    }).join('')}</div>`;
}

function wireSearchChoices(container) {
    container.querySelectorAll('[data-pick-kind]').forEach(button => {
        button.addEventListener('click', () => {
            searchInput.value = button.dataset.pickName;
            searchObject({ kind: button.dataset.pickKind, key: button.dataset.pickKey });
        });
    });
}

// ---- Suggestions as you type -------------------------------------------

function closeSearchSuggestions() {
    clearTimeout(suggestTimer);
    suggestSeq++;
    suggestItems = [];
    suggestActive = -1;
    if (!searchSuggestions) return;
    searchSuggestions.hidden = true;
    searchSuggestions.innerHTML = '';
    searchInput.setAttribute('aria-expanded', 'false');
    searchInput.removeAttribute('aria-activedescendant');
}

function requestSearchSuggestions() {
    clearTimeout(suggestTimer);
    const query = searchInput.value.trim();
    if (!searchSuggestions || query.length < 2 || isTelescopeQuery(query)) {
        closeSearchSuggestions();
        return;
    }
    suggestTimer = setTimeout(() => {
        const seq = ++suggestSeq;
        const lang = encodeURIComponent(window.I18N_LANG || 'en');
        fetch(`/api/search_suggest?q=${encodeURIComponent(query)}&lang=${lang}`)
            .then(r => r.json())
            .then(data => {
                if (seq === suggestSeq && searchInput.value.trim() === query) {
                    renderSearchSuggestions(data.matches || []);
                }
            })
            .catch(err => console.warn('Search suggestions failed:', err));
    }, 150);
}

function renderSearchSuggestions(matches) {
    suggestItems = matches;
    suggestActive = -1;
    searchSuggestions.innerHTML = '';
    if (!matches.length) {
        closeSearchSuggestions();
        return;
    }
    matches.forEach((match, i) => {
        const item = document.createElement('li');
        item.id = `search-suggestion-${i}`;
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', 'false');
        const name = document.createElement('span');
        name.textContent = match.name;
        item.appendChild(name);
        const hint = searchMatchHint(match);
        if (hint) {
            const hintEl = document.createElement('span');
            hintEl.className = 'hint';
            hintEl.textContent = hint;
            item.appendChild(hintEl);
        }
        // Keep the focus in the box, so a tap doesn't close the list first
        item.addEventListener('pointerdown', e => e.preventDefault());
        item.addEventListener('mousedown', e => e.preventDefault());
        item.addEventListener('click', () => pickSearchSuggestion(i));
        searchSuggestions.appendChild(item);
    });
    searchSuggestions.hidden = false;
    searchInput.setAttribute('aria-expanded', 'true');
}

function highlightSearchSuggestion(i) {
    suggestActive = i;
    searchSuggestions.querySelectorAll('li').forEach((item, j) => {
        item.setAttribute('aria-selected', j === i ? 'true' : 'false');
        if (j === i) item.scrollIntoView({ block: 'nearest' });
    });
    if (i >= 0) searchInput.setAttribute('aria-activedescendant', `search-suggestion-${i}`);
    else searchInput.removeAttribute('aria-activedescendant');
}

function pickSearchSuggestion(i) {
    const match = suggestItems[i];
    if (!match) return;
    searchInput.value = match.name;
    searchObject({ kind: match.kind, key: match.key });
}

// Keys in the search box: arrows move through the suggestions, Enter picks
// the highlighted one (or searches), Escape closes the list (or clears)
function handleSearchKeydown(e) {
    const open = searchSuggestions && !searchSuggestions.hidden && suggestItems.length;
    if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : -1;
        const next = suggestActive + step;
        highlightSearchSuggestion(next < -1 ? suggestItems.length - 1 : next >= suggestItems.length ? -1 : next);
    } else if (e.key === 'Enter') {
        e.preventDefault();
        if (open && suggestActive >= 0) pickSearchSuggestion(suggestActive);
        else searchObject();
    } else if (e.key === 'Escape') {
        if (open) closeSearchSuggestions();
        else clearSearch();
    }
}

if (searchSuggestions) {
    searchInput.addEventListener('input', requestSearchSuggestions);
    searchInput.addEventListener('blur', () => setTimeout(closeSearchSuggestions, 150));
}

// ---- Searching -----------------------------------------------------------

// Search for what the box holds, or for `pick` ({kind, key}) chosen from a list
function searchObject(pick) {
    if (!pick || !pick.kind) pick = null; // a click handler passes its event
    closeSearchSuggestions();
    const searchValue = searchInput.value.trim();
    if (!pick && !searchValue) {
        alert(_('Please enter a search term.'));
        return;
    }

    // Check if searching for "telescope"
    if (!pick && isTelescopeQuery(searchValue)) {
        if (!isTelescopeSelected()) {
            alert(_('No telescope selected. Please select a telescope first.'));
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
                        alert(_('Telescope position not available: {reason}', { reason: data.message || _('No telescope selected or unable to contact telescope') }));
                    }
                })
                .catch(err => {
                    console.error('Telescope search error:', err);
                    alert(_('Error fetching telescope position. Check browser console for details.'));
                });
        } else if (telescopePosition) {
            performTelescopeSearch();
        } else {
            alert(_('Telescope position not available. Make sure a telescope is selected.'));
        }
        return;
    }

    fetch('/interface/search_object', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(pick ? { searchValue, pick } : { searchValue })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'choose' && data.matches && data.matches.length) {
            showSearchChoices(data.query, data.matches);
            return;
        }
        if (data.status === 'success' && data.data) {
            const objData = data.data;
            // Find the object in our stars array or use the search result
            let foundObject = null;
            
            // First try the solar-system objects, then the star catalogue
            // (a constellation is neither: it gets a marker of its own below)
            const isConstellation = objData.kind === 'constellation';
            for (const obj of (isConstellation ? [] : stars)) {
                if (obj.name && objData.Name && 
                    obj.name.toLowerCase() === objData.Name.toLowerCase()) {
                    foundObject = obj;
                    break;
                }
            }
            if (!foundObject && objData.Name && !isConstellation) {
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
                    label: objData.label || null,
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
            if (isConstellation && showConstellations && !showConstellations.checked) {
                showConstellations.checked = true;
                showConstellations.dispatchEvent(new Event('change'));
            }
            if (!isConstellation && (foundObject.mag == null || Number.isNaN(foundObject.mag))
                && showUnknownMag && !showUnknownMag.checked) {
                showUnknownMag.checked = true;
                loadRemainingStarBands();
            }
            const objectMag = foundObject.mag == null ? 6 : foundObject.mag;
            const currentMagLimit = parseFloat(magFilter.value);
            
            // If the object is fainter than current limit, increase the magnitude limit
            if (!isConstellation && objectMag > currentMagLimit) {
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
                ? _("not recorded")
                : foundObject.mag.toFixed(2);
            
            const displayName = foundObject.friendlyName 
                ? `${foundObject.name} (${foundObject.friendlyName})`
                : (foundObject.label || objData.label || foundObject.name);
                
            // Store the full data for advanced info modal 
            window.currentStarData = { ...foundObject, hourAngle: ha2 };
                
            const info = document.getElementById('info');
            const buttons = isConstellation
                ? `<button onclick="clearSearch()" style="padding: 4px 8px; background: #999; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">${_('Clear')}</button>`
                : `<button onclick="trackObject('${foundObject.name}', ${foundObject.ra}, ${foundObject.dec}, ${foundObject.mag})" style="padding: 4px 8px; background: #4CAF50; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">${_('Track')}</button>
                    <button onclick="showStarInfoModal(window.currentStarData)" style="padding: 4px 8px; background: #007bff; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">${_('Advanced Info')}</button>`;
            const details = isConstellation
                ? `${_('Constellation')}<br>`
                : `${_('RA: {ra}', { ra: raHMS })}<br>${_('DEC: {dec}', { dec: decDMS })}<br>${_('V-Mag: {mag}', { mag: displayMagFormatted })}<br>`;
            info.innerHTML =
                `<b>🔍 ${escapeSearchHtml(displayName)}</b><br>${details}
                 ${searchResultNoteHtml(objData, [foundObject.friendlyName, foundObject.label || objData.label, foundObject.name])}
                 <div style="margin-top: 5px; display: flex; gap: 4px;">${buttons}</div>`;
            wireSearchChoices(info);
        } else {
            alert(data.message || _('Object not found.'));
        }
    })
    .catch(error => {
        console.error('Search error:', error);
        alert(_('Search failed. Please try again.'));
    });
}

// How the search found its result, when that isn't obvious from the name
// shown, and the other objects it could have meant
function searchResultNoteHtml(objData, shownNames) {
    const match = objData.match;
    let html = '';
    if (match && match.match === 'fuzzy') {
        html += `<div class="search-note">${escapeSearchHtml(_('Closest match for “{query}”', { query: match.query }))}</div>`;
    } else if (match && match.match !== 'number' && match.matched
               && !shownNames.some(name => name && searchFold(name) === searchFold(match.matched))) {
        html += `<div class="search-note">${escapeSearchHtml(_('Found by its name “{name}”', { name: match.matched }))}</div>`;
    }
    const others = objData.alternatives || [];
    if (others.length) {
        html += `<div class="search-note">${escapeSearchHtml(_('Other matches:'))}</div>${searchChoicesHtml(others)}`;
    }
    return html;
}

// Several objects matched about equally well: list them instead of guessing
function showSearchChoices(query, matches) {
    const info = document.getElementById('info');
    info.innerHTML =
        `<b>🔍 ${escapeSearchHtml(_('No exact match for “{query}”. Did you mean:', { query }))}</b>
         ${searchChoicesHtml(matches)}
         <div style="margin-top: 5px; display: flex; gap: 4px;">
            <button onclick="clearSearch()" style="padding: 4px 8px; background: #999; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">${_('Clear')}</button>
         </div>`;
    wireSearchChoices(info);
}

// Function to move camera to look at an object
function moveToObject(obj) {
    // In phone-pointing mode the phone decides where the view looks; an arrow
    // on the map (drawOrientationOverlay) guides it to the object instead
    if (orientationModeActive()) {
        draw();
        return;
    }

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
    closeSearchSuggestions();
    searchedObject = null;
    searchInput.value = '';
    document.getElementById('info').innerHTML = defaultInfoText();
    draw();
}

// Helper function to perform telescope search
function performTelescopeSearch() {
    if (!telescopePosition) {
        console.error('performTelescopeSearch called but telescopePosition is null');
        alert(_('Telescope position not available.'));
        return;
    }
    
    // Create a searchedObject from telescopePosition
    const telescopeObj = {
        name: _('Telescope'),
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
        `<b>🔍 ${_('Telescope Position')}</b><br>${_('RA: {ra}', { ra: raHMS })}<br>${_('DEC: {dec}', { dec: decDMS })}<br>
         <div style="margin-top: 5px; display: flex; gap: 4px;">
            <button onclick="clearSearch()" style="padding: 4px 8px; background: #999; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">${_('Clear')}</button>
         </div>`;
    
    draw();
}
