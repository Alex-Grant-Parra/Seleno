// Star map - the selected telescope: live position polling and object tracking.

// Telescope position tracking
let telescopePosition = null;
let telescopePositionUpdateInterval = null;
let telescopePositionAvailable = false; // Track if we've successfully fetched at least once
const telescopeMarkerSize = 25;
const telescopeMarkerColor = "#00ff00"; // Green for telescope position

// Get telescope data from embedded template
let telescopeDataFromSession = null;
try {
    const telescopeDataElement = document.getElementById('telescope-data');
    if (telescopeDataElement && telescopeDataElement.textContent) {
        telescopeDataFromSession = JSON.parse(telescopeDataElement.textContent);
    }
} catch (e) {
    console.debug('Could not parse telescope data from template:', e);
}

function isTelescopeSelected() {
    return telescopeDataFromSession && telescopeDataFromSession.telescope_id;
}

function updateTelescopePosition() {
    // Only fetch telescope position if one is selected in the session
    if (!isTelescopeSelected()) {
        return;
    }
    
    fetch('/api/telescope_position')
        .then(response => {
            if (response.status === 401) {
                // Not authenticated, stop polling
                console.warn('Telescope position: Not authenticated');
                if (telescopePositionUpdateInterval) clearInterval(telescopePositionUpdateInterval);
                return null;
            }
            // For 422 (no telescope selected), keep polling in case one gets selected
            if (response.status === 422) {
                if (telescopePositionAvailable) {
                    console.debug('Telescope position: No telescope currently selected (422)');
                    telescopePositionAvailable = false;
                }
                telescopePosition = null;
                return null;
            }
            if (!response.ok) {
                console.debug('Telescope position API returned status:', response.status);
                return null;
            }
            return response.json();
        })
        .then(data => {
            if (data && data.status === 'success' && data.ra !== null && data.dec !== null) {
                // Get current observer position and time to convert RA/DEC to Alt/Az
                const latDeg = parseFloat(latInput.value) || 0;
                const lonDeg = parseFloat(lonInput.value) || 0;
                let selectedDate = new Date();
                try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
                
                // Convert RA/DEC to Alt/Az for fixed horizon-based positioning
                const { altDeg, azDeg } = radecToAltAz(data.ra, data.dec, selectedDate, latDeg, lonDeg);
                
                telescopePosition = {
                    ra: data.ra,
                    dec: data.dec,
                    alt: altDeg,
                    az: azDeg,
                    timestamp: Date.now()
                };
                if (!telescopePositionAvailable) {
                    telescopePositionAvailable = true;
                    console.log('%c✓ Telescope position now available!', 'color: green; font-weight: bold;', `RA: ${data.ra}°, DEC: ${data.dec}° (Alt: ${altDeg.toFixed(1)}°, Az: ${azDeg.toFixed(1)}°)`);
                }
                scheduleDraw();
            } else if (data && data.status === 'error') {
                console.debug('Telescope position error:', data.message);
                telescopePosition = null;
                telescopePositionAvailable = false;
            }
        })
        .catch(err => {
            // Silently fail - just don't display telescope marker, but keep trying
            console.debug('Telescope position update failed:', err.message);
        });
}

function startTelescopePositionTracking() {
    // Update immediately
    updateTelescopePosition();
    
    // Then update every 5 seconds to reduce connection load (the interval used
    // to be 1s despite this comment, which meant a full redraw every second)
    if (telescopePositionUpdateInterval) clearInterval(telescopePositionUpdateInterval);
    telescopePositionUpdateInterval = setInterval(updateTelescopePosition, 5000);
}

function stopTelescopePositionTracking() {
    if (telescopePositionUpdateInterval) {
        clearInterval(telescopePositionUpdateInterval);
        telescopePositionUpdateInterval = null;
    }
    telescopePosition = null;
    telescopePositionAvailable = false;
}

// Function to update the tracking info panel
function updateTrackingPanel(trackingData) {
    const trackingInfoDiv = document.getElementById('tracking-info');
    const trackingContent = document.getElementById('tracking-content');
    
    if (!trackingData) {
        // Hide panel when no tracking
        trackingInfoDiv.style.display = 'none';
        trackingContent.innerHTML = '<em>No object being tracked</em>';
        return;
    }
    
    // Show panel and update content
    trackingInfoDiv.style.display = 'block';
    trackingContent.innerHTML = `
        <div><strong>Object:</strong> ${trackingData.name}</div>
        <div><strong>RA:</strong> ${trackingData.ra.toFixed(4)}°</div>
        <div><strong>DEC:</strong> ${trackingData.dec.toFixed(4)}°</div>
        ${(trackingData.mag !== undefined && trackingData.mag !== null && !Number.isNaN(trackingData.mag))
            ? `<div><strong>Magnitude:</strong> ${trackingData.mag.toFixed(2)}</div>`
            : '<div><strong>Magnitude:</strong> not recorded</div>'}
    `;
}

// Function to stop tracking
function stopTracking() {
    console.log('=== stopTracking CALLED from star map ===');
    
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
            // Clear sessionStorage
            sessionStorage.removeItem('currentTracking');
            console.log('✓ Tracking stopped successfully');
            alert('🛑 Tracking stopped successfully');
        } else {
            throw new Error(data.message || "Failed to stop tracking");
        }
    })
    .catch(error => {
        console.error('✗ Error stopping tracking:', error);
        alert(`❌ Failed to stop tracking: ${error.message}`);
        
        // Still clear the panel even if command failed
        updateTrackingPanel(null);
        sessionStorage.removeItem('currentTracking');
    });
}

// Function to track a celestial object
function trackObject(name, ra, dec, mag) {
    // First fetch star info to get friendly name if available
    fetch(`/star_info/${encodeURIComponent(name)}`)
        .then(response => response.json())
        .then(starData => {
            const displayName = starData.friendlyName 
                ? `${starData.name} (${starData.friendlyName})`
                : name;
            
            // Now send the tracking request
            fetch('/track_star', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    name: name,
                    ra: ra,
                    dec: dec,
                    mag: mag
                })
            })
            .then(checkAuthResponse)
            .then(response => response.json())
            .then(data => {
                if (data.status === 'tracking') {
                    console.log(`Successfully started tracking ${name} on telescope ${data.telescope_id}`);
                    
                    // Store tracking state in sessionStorage so it can be displayed on interface page
                    const trackingData = {
                        name: name,
                        ra: ra,
                        dec: dec,
                        mag: mag
                    };
                    sessionStorage.setItem('currentTracking', JSON.stringify(trackingData));
                    
                    // Update tracking panel to show tracking info
                    updateTrackingPanel(trackingData);
                    
                } else if (data.redirect) {
                    // No telescope selected - inform user
                    alert('Please select a telescope in the Interface page to begin tracking');
                } else {
                    console.error('Tracking failed:', data);
                    alert(data.message || 'Failed to start tracking. Please try again.');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('Failed to start tracking. Please check your connection.');
            });
        })
        .catch(() => {
            // Fallback if star info fetch fails - just use the raw name
            fetch('/track_star', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    name: name,
                    ra: ra,
                    dec: dec,
                    mag: mag
                })
            })
            .then(checkAuthResponse)
            .then(response => response.json())
            .then(data => {
                if (data.status === 'tracking') {
                    console.log(`Successfully started tracking ${name} on telescope ${data.telescope_id}`);
                    
                    // Store tracking state in sessionStorage so it can be displayed on interface page
                    const trackingData = {
                        name: name,
                        ra: ra,
                        dec: dec,
                        mag: mag
                    };
                    sessionStorage.setItem('currentTracking', JSON.stringify(trackingData));
                    
                    // Update tracking panel to show tracking info
                    updateTrackingPanel(trackingData);
                    
                    // Show success message instead of redirecting
                    alert(`✓ Now tracking ${name}`);
                } else if (data.redirect) {
                    // No telescope selected - inform user
                    alert('Please select a telescope in the Interface page to begin tracking');
                } else {
                    console.error('Tracking failed:', data);
                    alert(data.message || 'Failed to start tracking. Please try again.');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('Failed to start tracking. Please check your connection.');
            });
        });
}
