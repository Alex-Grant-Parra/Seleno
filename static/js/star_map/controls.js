// Star map - control panel wiring and page start-up. Loaded last, since it hands
// functions from every other file straight to addEventListener.

// UI event listeners
let manualMagnitudeTimeout = null; // Timer to re-enable auto magnitude after manual adjustment

magFilter.addEventListener('input', () => {
    magValue.textContent = magFilter.value;
    beginInteraction();
    // If user expands the magnitude beyond what we've fetched, fetch more
    const newMagLimit = parseFloat(magFilter.value);
    fetchMoreStarsIfNeeded(newMagLimit);
    
    // Temporarily disable auto magnitude-zoom linking only if it's currently enabled
    if (magnitudeZoomEnabled) {
        magnitudeZoomEnabled = false;
        // Also uncheck the checkbox to show user the state
        if (autoMagnitudeZoom) {
            autoMagnitudeZoom.checked = false;
        }
        
        // Clear any existing timeout
        if (manualMagnitudeTimeout) {
            clearTimeout(manualMagnitudeTimeout);
        }
    }
    
    scheduleDraw();
});
latInput.addEventListener('change', draw);
lonInput.addEventListener('change', draw);
showStars.addEventListener('change', draw);
showPlanets.addEventListener('change', draw);
showHorizonGrid.addEventListener('change', draw);
showEquatorialGrid.addEventListener('change', draw);
if (showEcliptic) showEcliptic.addEventListener('change', draw);
if (showBelowHorizon) showBelowHorizon.addEventListener('change', draw);
if (horizonTintOpacityInput) horizonTintOpacityInput.addEventListener('input', draw);
const horizonTintApplyBtn = document.getElementById('horizon-tint-apply');
if (horizonTintApplyBtn) horizonTintApplyBtn.addEventListener('click', () => {
    horizonTintOpacityInput.blur();
    draw();
});
if (showUnknownMag) {
    showUnknownMag.addEventListener('change', () => {
        // These sit at the end of the catalogue, so make sure the tail is on its way
        if (showUnknownMag.checked) loadRemainingStarBands();
        scheduleDraw();
    });
}
if (showRenderStats) showRenderStats.addEventListener('change', updateRenderStats);
if (magnitudeZoomRatioInput) {
    syncMagnitudeZoomRatioInput();
    magnitudeZoomRatioInput.addEventListener('change', updateMagnitudeZoomRatio);
}
if (showConstellations) {
    showConstellations.addEventListener('change', () => {
        if (showConstellations.checked) loadConstellations();
        scheduleDraw();
    });
}
if (showConstellationNames) showConstellationNames.addEventListener('change', scheduleDraw);
if (showStarNames) {
    showStarNames.addEventListener('change', () => {
        if (showStarNames.checked) loadStarNames();
        scheduleDraw();
    });
}

// Auto-magnitude zoom checkbox
if (autoMagnitudeZoom) {
    autoMagnitudeZoom.addEventListener('change', () => {
        magnitudeZoomEnabled = autoMagnitudeZoom.checked;
        if (magnitudeZoomEnabled) {
            // If re-enabling, update magnitude based on current zoom
            updateMagnitudeForZoom();
        }
        console.log('Auto magnitude-zoom linking', magnitudeZoomEnabled ? 'enabled' : 'disabled');
    });
}

if (flipVerticalCheckbox) {
    flipVerticalCheckbox.addEventListener('change', () => {
        // Capture current orientation so toggling doesn't move the map
        const prevRotX = rotX;
        const prevRotY = rotY;
        const prevZoom = zoom;

        invertControls = !!flipVerticalCheckbox.checked;
        try { localStorage.setItem('starMap.flipVertical', invertControls ? '1' : '0'); } catch {}
        console.log('Invert controls set to', invertControls);
        // Restore orientation and redraw once to ensure no visual jump
        requestAnimationFrame(() => {
            rotX = prevRotX;
            rotY = prevRotY;
            zoom = prevZoom;
            draw();
        });
    });
}
resetBtn.addEventListener('click', () => {
    rotX = 0; rotY = 0;
    zoom = 1.0; // Reset zoom level
    magFilter.value = "4.0"; // Reset magnitude to 4
    magValue.textContent = "4.0";
    showStars.checked = true;
    showPlanets.checked = true;
    clearSearch(); // Clear search when resetting view
    
    // Reset auto-magnitude zoom to enabled
    magnitudeZoomEnabled = true;
    if (autoMagnitudeZoom) {
        autoMagnitudeZoom.checked = true;
    }
    
    // Clear any pending timeout
    if (manualMagnitudeTimeout) {
        clearTimeout(manualMagnitudeTimeout);
        manualMagnitudeTimeout = null;
    }
    
    // Reset to current time and user's location if available
    if (window.resetToCurrentLocationAndTime) {
        window.resetToCurrentLocationAndTime();
    } else {
        // Fallback: reset to 0,0 if location function not available
        latInput.value = 0;
        lonInput.value = 0;
    }
    
    draw();
});
// Search event listeners (guarded)
if (searchBtn) searchBtn.addEventListener('click', searchObject);
else console.warn('searchBtn not found');
if (clearSearchBtn) clearSearchBtn.addEventListener('click', clearSearch);
else console.warn('clearSearchBtn not found');
searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        searchObject();
    } else if (e.key === 'Escape') {
        clearSearch();
    }
});

// Global Ctrl+F handler to focus search box instead of browser search
document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'f') {
        e.preventDefault(); // Prevent browser's find dialog
        if (searchInput) {
            searchInput.focus();
            searchInput.select(); // Select any existing text for easy replacement
        }
    }
});

// Controls panel toggle (the panel is a collapsible sheet on small screens)
const uiToggle = document.getElementById('ui-toggle');
const controlsPanel = document.getElementById('controls');
function setControlsOpen(open) {
    if (!controlsPanel) return;
    controlsPanel.classList.toggle('open', open);
    if (uiToggle) {
        uiToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        uiToggle.textContent = open ? '\u2715' : '\u2630';
    }
}
if (uiToggle) {
    uiToggle.addEventListener('click', () => {
        setControlsOpen(!controlsPanel.classList.contains('open'));
    });
}

helpBtn.addEventListener('click', () => {
    helpModal.style.display = "flex";
});
closeHelp.addEventListener('click', () => {
    helpModal.style.display = "none";
});
helpModal.addEventListener('click', (e) => {
    if (e.target === helpModal) helpModal.style.display = "none";
});

// Expose functions to global scope so inline onclick handlers work robustly
window.trackObject = trackObject;
window.stopTracking = stopTracking;
window.searchObject = searchObject;
window.clearSearch = clearSearch;

// Magnitude slider context menu
function openMagContextMenu(clientX, clientY) {
    magCustomInput.value = magFilter.value;
    magContextMenu.style.display = 'block';
    magContextMenu.style.left = '0px';
    magContextMenu.style.top = '0px';
    // Clamp into the viewport - on a phone the slider sits near an edge
    const rect = magContextMenu.getBoundingClientRect();
    const left = Math.max(8, Math.min(clientX, window.innerWidth - rect.width - 8));
    const top = Math.max(8, Math.min(clientY, window.innerHeight - rect.height - 8));
    magContextMenu.style.left = left + 'px';
    magContextMenu.style.top = top + 'px';
    if (!isTouchDevice) {
        // Autofocus would pop up the on-screen keyboard and shove the menu around
        magCustomInput.focus();
        magCustomInput.select();
    }
}

magFilter.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    openMagContextMenu(e.clientX, e.clientY);
});

// Touch equivalent of the right-click menu: press and hold the slider
let magLongPressTimer = null;
function cancelMagLongPress() {
    if (magLongPressTimer) {
        clearTimeout(magLongPressTimer);
        magLongPressTimer = null;
    }
}
magFilter.addEventListener('touchstart', (e) => {
    const t = e.touches[0];
    if (!t) return;
    const x = t.clientX, y = t.clientY;
    cancelMagLongPress();
    magLongPressTimer = setTimeout(() => {
        magLongPressTimer = null;
        openMagContextMenu(x, y);
    }, 550);
}, { passive: true });
// Any movement means they're dragging the slider, not holding it
magFilter.addEventListener('touchmove', cancelMagLongPress, { passive: true });
magFilter.addEventListener('touchend', cancelMagLongPress, { passive: true });
magFilter.addEventListener('touchcancel', cancelMagLongPress, { passive: true });

// Context menu functionality
function hideContextMenu() {
    magContextMenu.style.display = 'none';
}

magApplyBtn.addEventListener('click', () => {
    const customValue = parseFloat(magCustomInput.value);
    if (!isNaN(customValue) && customValue >= minMag && customValue <= maxMag) {
        magFilter.value = customValue.toFixed(1);
        magValue.textContent = customValue.toFixed(1);
        draw();
    } else {
        alert(`Please enter a magnitude value between ${minMag.toFixed(1)} and ${maxMag.toFixed(1)}`);
        return;
    }
    hideContextMenu();
});

magCancelBtn.addEventListener('click', hideContextMenu);

// Handle Enter key in the input field
magCustomInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        magApplyBtn.click();
    } else if (e.key === 'Escape') {
        hideContextMenu();
    }
});

// Hide context menu when clicking elsewhere
document.addEventListener('click', (e) => {
    if (!magContextMenu.contains(e.target) && e.target !== magFilter) {
        hideContextMenu();
    }
});

// Hide loading screen after first draw
function hideLoading() {
    loading.style.display = "none";
}

// Initial draw and loading
window.addEventListener('DOMContentLoaded', () => {
    console.log('%cStar Map JS loaded v2025-10-27-1', 'color:#0bf');
    // Hint text depends on whether this device has a touch screen
    document.getElementById('info').innerHTML = defaultInfoText();
    // Initialize time control to current local time (rounded to minute)
    if (timeControl) {
        const now = new Date();
        now.setSeconds(0, 0);
        timeControl.value = formatLocalDateTime(now);
        timeControl.addEventListener('change', () => { 
            // During live time updates, refreshPlanetsForCurrentTime handles throttling
            // For manual changes, force immediate planet update
            const isLiveTimeActive = document.getElementById('live-time')?.checked;
            refreshPlanetsForCurrentTime(!isLiveTimeActive); 
        });
        // Add keyboard rollover handling for ArrowUp/ArrowDown
        timeControl.addEventListener('keydown', handleTimeControlKeydown);
        // Add click handler to support virtual caret segmentation on browsers without selectionStart
        timeControl.addEventListener('click', handleTimeInputClick);
        
        // Also detect segment on mouseup for better accuracy
        timeControl.addEventListener('mouseup', handleTimeInputClick);
    }
    if (timeNowBtn) {
        timeNowBtn.addEventListener('click', () => {
            const now = new Date();
            now.setSeconds(0, 0);
            if (timeControl) timeControl.value = formatLocalDateTime(now);
            refreshPlanetsForCurrentTime(true); // Force update when clicking "Now" button
            draw();
        });
    }
    // Initialize flip vertical from localStorage, if present
    try {
        const saved = localStorage.getItem('starMap.flipVertical');
        if (flipVerticalCheckbox && (saved === '0' || saved === '1')) {
            invertControls = (saved === '1');
            flipVerticalCheckbox.checked = invertControls;
        } else if (flipVerticalCheckbox) {
            invertControls = !!flipVerticalCheckbox.checked;
        }
    } catch { invertControls = !!(flipVerticalCheckbox && flipVerticalCheckbox.checked); }
    
    // Add event listeners for cursor coordinate toggles
    if (showRADecCursor) {
        showRADecCursor.addEventListener('change', () => {
            if (lastCursorX !== null && lastCursorY !== null) {
                updateCursorCoords(lastCursorX, lastCursorY);
            }
        });
    }
    if (showAzElCursor) {
        showAzElCursor.addEventListener('change', () => {
            if (lastCursorX !== null && lastCursorY !== null) {
                updateCursorCoords(lastCursorX, lastCursorY);
            }
        });
    }
    
    // Start initial fetches: planets for current time and a small bright-star set
    const planetsPromise = refreshPlanetsForCurrentTime();
    const starsPromise = loadStarCatalogue();

    Promise.allSettled([planetsPromise, starsPromise]).then(() => {
        // Slider bounds come from the catalogue itself (see loadStarCatalogue);
        // fall back to -2..20 only if that never arrived.
        if (!starCount) updateMagSliderRange(-2, 20);

        // Once planets are present, preload their icons, then draw
        preloadPlanetImages().then(() => {
            draw();
            setTimeout(hideLoading, 200);
            // Begin background staged prefetch so data is ready before user requests it
            stagedPrefetchAfterFirstDraw();
            // Start telescope position tracking
            startTelescopePositionTracking();
            
            // Restore tracking state from sessionStorage if it exists
            try {
                const trackingState = sessionStorage.getItem('currentTracking');
                if (trackingState) {
                    const trackingData = JSON.parse(trackingState);
                    updateTrackingPanel(trackingData);
                }
            } catch (e) {
                console.log('No tracking state to restore');
            }
            
            // Pulse the search highlight, but only for a few seconds and at a
            // modest rate: this used to force a full redraw of the whole
            // catalogue on every animation frame for as long as a search was
            // active, which is ruinous at mag 20.
            let lastHighlightFrame = 0;
            function animate(now) {
                if (searchedObject && now - lastHighlightFrame > 50 &&
                    Date.now() < searchHighlightUntil) {
                    lastHighlightFrame = now;
                    scheduleDraw();
                }
                requestAnimationFrame(animate);
            }
            requestAnimationFrame(animate);
        });
    });
});
