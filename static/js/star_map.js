// 3D Planetarium JavaScript

// Helper function to check for authentication errors in fetch responses
function checkAuthResponse(response) {
    if (response.status === 401) {
        alert('You must be logged in to control the telescope.');
        window.location.href = '/login';
        throw new Error('Not authenticated');
    }
    return response;
}

// `stars` holds the handful of solar-system objects (sun, moon, planets), which
// carry icons and phase data and are replaced whenever the time changes. The
// 286k catalogue stars are NOT kept as objects - they live in flat typed arrays
// (see "Star catalogue" below), which is what makes mag 20 drawable at all.
const stars = JSON.parse(document.getElementById('stars-data').textContent);

// Image cache for planet sprites
const planetImages = {};
const basePlanetSize = 24; // Base size for all planets (will be scaled by zoom)
const baseStarSizeMultiplier = 1.0; // Base star size multiplier (will be scaled by zoom)

// Magnitude range offered by the slider; refined once the catalogue reports its
// real extremes. Objects with no recorded magnitude are not on this scale.
let minMag = -2, maxMag = 20;

function updateMagSliderRange(minVal, maxVal) {
    if (typeof minVal !== 'number' || typeof maxVal !== 'number') return;
    if (!isFinite(minVal) || !isFinite(maxVal)) return;
    // Ensure min < max
    if (maxVal <= minVal) maxVal = minVal + 0.1;
    magFilter.min = minVal.toFixed(1);
    magFilter.max = maxVal.toFixed(1);
    // Clamp current value
    let current = parseFloat(magFilter.value);
    if (isNaN(current)) current = Math.min(4.0, maxVal);
    const clamped = Math.min(Math.max(current, minVal), maxVal);
    if (Math.abs(clamped - current) > 1e-6) {
        magFilter.value = clamped.toFixed(1);
        magValue.textContent = clamped.toFixed(1);
        scheduleDraw();
    }
}

function getPlanetsMagRange() {
    if (!planetsList || planetsList.length === 0) return null;
    let pmin = Infinity, pmax = -Infinity;
    for (const p of planetsList) {
        const m = (p && typeof p.mag === 'number') ? p.mag : null;
        if (m == null || isNaN(m)) continue;
        if (m < pmin) pmin = m;
        if (m > pmax) pmax = m;
    }
    if (pmin === Infinity || pmax === -Infinity) return null;
    return { min: pmin, max: pmax };
}

// UI elements
const magFilter = document.getElementById('mag-filter');
const magValue = document.getElementById('mag-value');
const autoMagnitudeZoom = document.getElementById('auto-magnitude-zoom');
const latInput = document.getElementById('latitude');
const lonInput = document.getElementById('longitude');
const showStars = document.getElementById('show-stars');
const showPlanets = document.getElementById('show-planets');
const showHorizonGrid = document.getElementById('show-horizon-grid');
const showEquatorialGrid = document.getElementById('show-equatorial-grid');
const showEcliptic = document.getElementById('show-ecliptic');
const showBelowHorizon = document.getElementById('show-below-horizon');
const timeControl = document.getElementById('time-control');
const timeNowBtn = document.getElementById('time-now');
const timeSegmentIndicator = document.getElementById('time-segment-indicator');
let currentLSTDeg = 0; // updated per draw based on time and longitude
let lastPlanetUpdateTime = 0; // Track when planets were last updated
const PLANET_UPDATE_THROTTLE_MS = 60000; // Only update planets every 60 seconds max
const resetBtn = document.getElementById('reset-view');
const helpBtn = document.getElementById('help-btn');
const helpModal = document.getElementById('help-modal');
const closeHelp = document.getElementById('close-help');
const loading = document.getElementById('loading');
// Small bottom-right throbber for star loading/processing
const starLoadingIndicator = document.getElementById('star-loading-indicator');
const starLoadingLabel = document.getElementById('star-loading-label');
const showUnknownMag = document.getElementById('show-unknown-mag');
const showStarNames = document.getElementById('show-star-names');
const showConstellations = document.getElementById('show-constellations');
const showConstellationNames = document.getElementById('show-constellation-names');
const unknownMagCountLabel = document.getElementById('unknown-mag-count');
const showRenderStats = document.getElementById('show-render-stats');
const renderStatsDiv = document.getElementById('render-stats');
let starLoadingCounter = 0;
function starLoadingBegin() {
    starLoadingCounter++;
    if (starLoadingIndicator && starLoadingCounter > 0) {
        starLoadingIndicator.style.display = 'flex';
    }
}
function starLoadingEnd() {
    starLoadingCounter = Math.max(0, starLoadingCounter - 1);
    if (starLoadingIndicator && starLoadingCounter === 0) {
        starLoadingIndicator.style.display = 'none';
    }
}
// Debug orientation toggle
const flipVerticalCheckbox = document.getElementById('flip-vertical');

// Cursor coordinate elements
const showRADecCursor = document.getElementById('show-radec-cursor');
const showAzElCursor = document.getElementById('show-azel-cursor');
const cursorCoordsDiv = document.getElementById('cursor-coords');
let lastCursorX = null;
let lastCursorY = null;

// Context menu elements
const magContextMenu = document.getElementById('mag-context-menu');
const magCustomInput = document.getElementById('mag-custom-input');
const magApplyBtn = document.getElementById('mag-apply');
const magCancelBtn = document.getElementById('mag-cancel');

// Search elements
const searchInput = document.getElementById('search-object');
const searchBtn = document.getElementById('search-btn');
const clearSearchBtn = document.getElementById('clear-search-btn');

// Search state
let searchedObject = null;
let highlightAnimation = 0;
let searchHighlightUntil = 0; // the ring pulses until this time, then sits still

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

// Controls inversion state (affects drag deltas only)
let invertControls = false;

// Canvas setup
const canvas = document.getElementById('planetarium');
const ctx = canvas.getContext('2d');
let width = window.innerWidth, height = window.innerHeight;

// Touch-screen / small-viewport detection.
// sm-touch  -> device can be touched (gesture hints, finger-sized hit targets)
// sm-compact -> viewport is small (controls collapse into a bottom sheet)
const isTouchDevice = ('ontouchstart' in window)
    || (navigator.maxTouchPoints || 0) > 0
    || (window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
const COMPACT_MAX_WIDTH = 820;  // phones and narrow windows
const COMPACT_MAX_HEIGHT = 480; // phones held in landscape

function isCompactLayout() {
    return window.innerWidth <= COMPACT_MAX_WIDTH || window.innerHeight <= COMPACT_MAX_HEIGHT;
}

document.documentElement.classList.toggle('sm-touch', isTouchDevice);
document.documentElement.classList.toggle('sm-compact', isCompactLayout());

// Size the canvas to the viewport with a device-pixel-ratio backing store so
// the sky stays sharp on high-DPI phone screens. Everything else keeps drawing
// in CSS pixels (width/height) because of the transform set here.
function resizeCanvas() {
    width = Math.max(1, Math.round(window.innerWidth));
    height = Math.max(1, Math.round(window.innerHeight));
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5); // cap: 3x of a full phone screen is a lot of pixels
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
resizeCanvas();

// Set up the magnitude slider with actual data range
magFilter.min = minMag.toFixed(1);
magFilter.max = maxMag.toFixed(1);
magFilter.step = "0.1";
magFilter.value = "4.0"; // Start at magnitude 4
magValue.textContent = "4.0";

// 3D sphere parameters
// Increase the sphere radius for a more immersive effect
const R = Math.min(width, height) * 0.9 / 2;
let rotX = 0, rotY = 0; // rotation angles
let dragging = false, lastX = 0, lastY = 0;

// Zoom parameters
let zoom = 1.0; // Default zoom level
const minZoom = 1; // 80% - only slightly zoomed out
const maxZoom = 6.7; // 500% - zoomed way in
const zoomStep = 0.1; // Zoom increment per scroll

// Magnitude-Zoom linking parameters
let magnitudeZoomEnabled = true; // Enable magnitude change with zoom
const baseMagnitude = 4.0; // Base magnitude at zoom level 1.0
const magnitudePerZoomLevel = 1.5; // How much magnitude increases per zoom level

// Size scaling with zoom
function getMagnitudeBasedSize(effectiveMag) {
    // Improved magnitude-based sizing with larger overall sizes and good size differences
    // Examples: mag -1 → ~8.5, mag 0 → ~6.0, mag 2 → ~3.8, mag 4 → ~2.2, mag 6 → ~1.2
    const referenceMag = 3.0; // Reference magnitude for size calculations
    const baseSizeAtRef = 2.5; // Increased base size at reference magnitude
    const sizeFactor = 1.75; // Slightly increased size factor for more variation
    
    // Calculate size using a power function with fractional exponent
    const magDiff = referenceMag - effectiveMag;
    const calculatedSize = baseSizeAtRef * Math.pow(sizeFactor, magDiff * 0.6);
    
    return Math.max(0.8, calculatedSize); // Increased minimum size
}

function getZoomedStarSize(baseMagnitudeSize) {
    // Scale star size based on zoom level
    // At zoom 1.0, use base size; higher zoom = larger stars
    const zoomScale = 0.5 + (zoom * 0.5); // Range from 0.5x to ~3.85x
    return Math.max(0.5, baseMagnitudeSize * baseStarSizeMultiplier * zoomScale);
}

function getZoomedPlanetSize() {
    // Scale planet size based on zoom level
    const zoomScale = 0.5 + (zoom * 0.5); // Range from 0.5x to ~3.85x
    return Math.max(8, basePlanetSize * zoomScale);
}

// Camera is inside the sphere: invert z-culling (draw z < 0)
// Convert RA/DEC to 3D Cartesian coordinates
function radecToXYZ(ra, dec) {
    // RA in degrees, DEC in degrees
    const raRad = ra * Math.PI / 180;
    const decRad = dec * Math.PI / 180;
    const x = Math.cos(decRad) * Math.cos(raRad);
    const y = Math.sin(decRad);
    const z = Math.cos(decRad) * Math.sin(raRad);
    return [x, y, z];
}

// Time and sidereal time utilities
// Date/time helpers for local datetime input handling with rollover
function formatLocalDateTime(date) {
    // Formats a Date as YYYY-MM-DDTHH:MM in local time
    const pad = (n) => n.toString().padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseLocalDateTime(str) {
    // Safely parse local datetime string (YYYY-MM-DDTHH:MM)
    // new Date(str) with no timezone is treated as local time by modern browsers
    const d = new Date(str);
    if (isNaN(d.getTime())) return null;
    return d;
}

function setTimeControlFromDate(d, preserveSelectionSegment) {
    if (!timeControl || !(d instanceof Date) || isNaN(d)) return;
    const segBounds = preserveSelectionSegment ? getCurrentSegmentBounds(timeControl) : null;
    timeControl.value = formatLocalDateTime(d);
    // Try to keep the caret on the same segment if supported
    if (segBounds && typeof timeControl.setSelectionRange === 'function') {
        try { timeControl.setSelectionRange(segBounds.start, segBounds.end); } catch {}
    }
}

// Determine which segment of the datetime string the caret is on
// Returns one of: 'year'|'month'|'day'|'hour'|'minute' or null if unknown
function getCaretSegment(input) {
    // For datetime-local inputs, selectionStart is unreliable, so we return null
    // and rely on virtualTimeSegment set by click handlers
    return null;
}

// Virtual caret segment used when browser doesn't expose selectionStart for datetime-local
let virtualTimeSegment = null;

// Map a click position within the input to an approximate character index and then segment
function getSegmentFromIndex(index, value) {
    if (!value) return null;
    const re = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;
    const m = value.match(re);
    if (!m) return null;
    let idx = 0;
    const ranges = {};
    ranges.year = { start: idx, end: idx + m[1].length - 1 };
    idx += m[1].length; idx += 1; // '-'
    ranges.month = { start: idx, end: idx + m[2].length - 1 };
    idx += m[2].length; idx += 1; // '-'
    ranges.day = { start: idx, end: idx + m[3].length - 1 };
    idx += m[3].length; idx += 1; // 'T'
    ranges.hour = { start: idx, end: idx + m[4].length - 1 };
    idx += m[4].length; idx += 1; // ':'
    ranges.minute = { start: idx, end: idx + m[5].length - 1 };
    idx += m[5].length;
    if (m[6]) { idx += 1; ranges.second = { start: idx, end: idx + m[6].length - 1 }; }

    for (const seg of ['year','month','day','hour','minute','second']) {
        if (!ranges[seg]) continue;
        if (index >= ranges[seg].start && index <= ranges[seg].end + 1) return seg;
    }
    return null;
}

function handleTimeInputClick(e) {
    const input = e.currentTarget;
    const value = input.value || '';
    if (!value) return;
    
    // For datetime-local inputs, estimate segment based on click position
    const rect = input.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    
    // Create a temporary span to measure the actual text width
    const tempSpan = document.createElement('span');
    tempSpan.style.cssText = `
        position: absolute; 
        visibility: hidden; 
        white-space: nowrap;
        font-family: ${getComputedStyle(input).fontFamily};
        font-size: ${getComputedStyle(input).fontSize};
        font-weight: ${getComputedStyle(input).fontWeight};
    `;
    
    // Format the value for display (DD/MM/YYYY HH:MM)
    const dateObj = new Date(value);
    const displayText = dateObj.toLocaleString('en-GB', { 
        day: '2-digit', 
        month: '2-digit', 
        year: 'numeric', 
        hour: '2-digit', 
        minute: '2-digit',
        hour12: false 
    }).replace(',', '');
    
    tempSpan.textContent = displayText;
    document.body.appendChild(tempSpan);
    const textWidth = tempSpan.offsetWidth;
    document.body.removeChild(tempSpan);
    
    // Calculate padding to center the text in the input
    const padding = (rect.width - textWidth) / 2;
    const textStart = padding;
    const textEnd = padding + textWidth;
    
    // Check if click is within the text area
    if (clickX < textStart || clickX > textEnd) {
        // Click is in padding area, ignore or default to last segment
        console.log(`Click at ${clickX.toFixed(0)}px is outside text area (${textStart.toFixed(0)}-${textEnd.toFixed(0)}px)`);
        return;
    }
    
    // Calculate relative position within the actual text
    const relativePosition = (clickX - textStart) / textWidth;
    console.log(`Click at ${(relativePosition * 100).toFixed(1)}% of text width`);
    
    let seg = null;
    
    // Format is DD/MM/YYYY HH:MM (UK/ISO format)
    // Split based on typical character positions:
    // DD (2) / (1) MM (2) / (1) YYYY (4) space (1) HH (2) : (1) MM (2) = 18 chars
    // Proportions: day=2/18, month=2/18, year=4/18, hour=2/18, minute=2/18
    
    if (relativePosition < 0.15) {
        seg = 'day';       // First ~15% (DD)
    } else if (relativePosition < 0.3) {
        seg = 'month';     // Next ~15% (MM)
    } else if (relativePosition < 0.55) {
        seg = 'year';      // Next ~25% (YYYY)
    } else if (relativePosition < 0.75) {
        seg = 'hour';      // Next ~20% (HH)
    } else {
        seg = 'minute';    // Last ~25% (MM)
    }
    
    console.log(`Detected segment: ${seg}`);
    
    if (seg) {
        virtualTimeSegment = seg;
        updateTimeSegmentIndicator(seg);
    }
}

function getCurrentSegmentBounds(input) {
    const v = input.value || '';
    let seg = getCaretSegment(input);
    
    // If no caret segment detected, use virtualTimeSegment
    if (!seg && virtualTimeSegment) {
        seg = virtualTimeSegment;
    }
    
    if (!seg) return null;

    // Recompute ranges using the same regex-based method so bounds match caret detection
    const re = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;
    const m = v.match(re);
    if (!m) return null;

    let idx = 0;
    const bounds = {};
    bounds.year = { start: idx, end: idx + m[1].length };
    idx += m[1].length; idx += 1; // '-'
    bounds.month = { start: idx, end: idx + m[2].length };
    idx += m[2].length; idx += 1; // '-'
    bounds.day = { start: idx, end: idx + m[3].length };
    idx += m[3].length; idx += 1; // 'T'
    bounds.hour = { start: idx, end: idx + m[4].length };
    idx += m[4].length; idx += 1; // ':'
    bounds.minute = { start: idx, end: idx + m[5].length };
    idx += m[5].length;
    if (m[6]) { idx += 1; bounds.second = { start: idx, end: idx + m[6].length }; }

    return bounds[seg] || null;
}

function adjustDateByUnit(date, unit, delta) {
    // Returns a new Date adjusted in local time, relying on JS rollover behavior
    const d = new Date(date.getTime());
    switch (unit) {
        case 'minute': d.setMinutes(d.getMinutes() + delta); break;
        case 'hour': d.setHours(d.getHours() + delta); break;
        case 'day': d.setDate(d.getDate() + delta); break;
        case 'month': d.setMonth(d.getMonth() + delta); break;
        case 'year': d.setFullYear(d.getFullYear() + delta); break;
        case 'second': d.setSeconds(d.getSeconds() + delta); break;
        default: d.setMinutes(d.getMinutes() + delta); break;
    }
    // Zero seconds and ms for stability with our control
    if (unit !== 'second') d.setSeconds(0, 0);
    return d;
}

// Update the visual indicator showing which time segment is selected
let segmentIndicatorTimeout = null;
function updateTimeSegmentIndicator(segment) {
    if (!timeSegmentIndicator) return;
    
    const labels = {
        'year': 'Year',
        'month': 'Month',
        'day': 'Day',
        'hour': 'Hour',
        'minute': 'Minute'
    };
    
    if (segment && labels[segment]) {
        timeSegmentIndicator.textContent = `[${labels[segment]}]`;
        timeSegmentIndicator.style.display = 'inline';
        
        // Auto-hide after 2 seconds
        if (segmentIndicatorTimeout) clearTimeout(segmentIndicatorTimeout);
        segmentIndicatorTimeout = setTimeout(() => {
            timeSegmentIndicator.style.display = 'none';
        }, 2000);
    } else {
        timeSegmentIndicator.style.display = 'none';
    }
}

function handleTimeControlKeydown(e) {
    if (!timeControl) return;
    const key = e.key;
    
    // Handle Tab key to cycle through segments
    if (key === 'Tab') {
        e.preventDefault();
        const segments = ['hour', 'minute', 'day', 'month', 'year'];
        const currentIndex = segments.indexOf(virtualTimeSegment || 'hour');
        const nextIndex = e.shiftKey 
            ? (currentIndex - 1 + segments.length) % segments.length 
            : (currentIndex + 1) % segments.length;
        virtualTimeSegment = segments[nextIndex];
        updateTimeSegmentIndicator(virtualTimeSegment);
        return;
    }
    
    if (key !== 'ArrowUp' && key !== 'ArrowDown') return;

    const raw = timeControl.value;
    let baseDate = parseLocalDateTime(raw);
    if (!baseDate) {
        baseDate = new Date();
        baseDate.setSeconds(0, 0);
    }

    // Try multiple methods to determine which segment is focused
    let unit = null;
    
    // Method 1: Try to get caret position (works in some browsers)
    unit = getCaretSegment(timeControl);
    
    // Method 2: Use stored virtual segment from last click
    if (!unit && virtualTimeSegment) {
        unit = virtualTimeSegment;
    }
    
    // Method 3: Use modifier keys for explicit control
    if (!unit || e.ctrlKey || e.shiftKey || e.altKey) {
        if (e.ctrlKey && e.shiftKey) unit = 'year';
        else if (e.altKey) unit = 'month';
        else if (e.ctrlKey) unit = 'day';
        else if (e.shiftKey) unit = 'minute';
    }
    
    // Method 4: If still no unit and no virtualTimeSegment, start with hour as default
    if (!unit) {
        unit = 'hour';
        virtualTimeSegment = 'hour';
    }
    
    // Prevent native browser handling to enable our rollover behavior
    e.preventDefault();
    e.stopPropagation();
    
    // Store the unit for consistency in subsequent keypresses
    virtualTimeSegment = unit;
    
    // Show visual feedback of which segment is being adjusted
    updateTimeSegmentIndicator(unit);

    const delta = (key === 'ArrowUp') ? 1 : -1;
    const newDate = adjustDateByUnit(baseDate, unit, delta);

    // Update input and redraw
    setTimeControlFromDate(newDate, true);
    schedulePlanetsRefresh();
    draw();
}
// Convert a Date to Julian Date (UTC)
function toJulianDate(date) {
    // Algorithm from NOAA; date should be a JS Date in UTC
    const year = date.getUTCFullYear();
    let month = date.getUTCMonth() + 1; // 1-12
    const day = date.getUTCDate() + (date.getUTCHours() + (date.getUTCMinutes() + date.getUTCSeconds() / 60) / 60) / 24;
    let Y = year;
    let M = month;
    if (M <= 2) { Y -= 1; M += 12; }
    const A = Math.floor(Y / 100);
    const B = 2 - A + Math.floor(A / 4);
    const JD = Math.floor(365.25 * (Y + 4716)) + Math.floor(30.6001 * (M + 1)) + day + B - 1524.5;
    return JD;
}

// Greenwich Mean Sidereal Time in degrees (0-360)
function gmstDegrees(date) {
    // Use the IAU 1982/1994 expression based on full Julian Date
    const JD = toJulianDate(date);
    const T = (JD - 2451545.0) / 36525.0;
    let gmst = 280.46061837 + 360.98564736629 * (JD - 2451545.0) + 0.000387933 * T * T - (T * T * T) / 38710000.0;
    gmst = ((gmst % 360) + 360) % 360; // normalize
    return gmst;
}

// Local Sidereal Time in degrees given longitude in degrees (east positive)
function lstDegrees(date, longitudeDeg) {
    const gmst = gmstDegrees(date);
    let lst = gmst + longitudeDeg; // East positive
    lst = ((lst % 360) + 360) % 360; // normalize 0-360
    return lst;
}

// --- Dynamic planet updates (server-driven ephemerides) ---
let planetsRefreshTimer = null;

async function fetchPlanetsForDate(date) {
    try {
        const iso = date.toISOString();
        const res = await fetch(`/api/planets?datetime=${encodeURIComponent(iso)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return data; // [{name, ra, dec, mag, icon, type:'planet'}]
    } catch (err) {
        console.error('Failed to fetch planets:', err);
        return null;
    }
}

function replacePlanetsInScene(newPlanets) {
    if (!Array.isArray(newPlanets)) return;
    // Remove existing planet entries in-place (preserve stars array reference)
    for (let i = stars.length - 1; i >= 0; i--) {
        if (stars[i] && stars[i].type === 'planet') stars.splice(i, 1);
    }
    // Insert fresh planets
    const newList = [];
    for (const p of newPlanets) {
        if (!p || typeof p.ra !== 'number' || typeof p.dec !== 'number') continue;
        const obj = {
            name: p.name,
            ra: p.ra,   // degrees
            dec: p.dec, // degrees
            mag: p.mag,
            icon: p.icon,
            type: 'planet'
        };
        try { obj.xyz = radecToXYZ(obj.ra, obj.dec); } catch { obj.xyz = radecToXYZ(0,0); }
        stars.push(obj);
        newList.push(obj);
    }
    planetsList = newList;
}

async function refreshPlanetsForCurrentTime(force = false) {
    // Throttle planet updates to avoid excessive queries during live time mode
    const now = Date.now();
    if (!force && (now - lastPlanetUpdateTime) < PLANET_UPDATE_THROTTLE_MS) {
        // Skip planet update but still trigger draw for sky rotation
        draw();
        return;
    }
    
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
    selectedDate.setSeconds(0, 0);
    const updated = await fetchPlanetsForDate(new Date(selectedDate.toISOString()));
    if (updated) {
        replacePlanetsInScene(updated);
        lastPlanetUpdateTime = now;
        draw();
    }
}

function schedulePlanetsRefresh() {
    if (planetsRefreshTimer) clearTimeout(planetsRefreshTimer);
    planetsRefreshTimer = setTimeout(() => {
        planetsRefreshTimer = null;
        refreshPlanetsForCurrentTime(true); // Force update for manual keyboard adjustments
    }, 250); // debounce rapid keypresses
}

// Compute Hour Angle (degrees, range -180..+180) for a given RA (deg) and LST (deg)
function hourAngleDegrees(raDeg, lstDeg) {
    // HA = LST - RA
    let ha = lstDeg - raDeg;
    // normalize to -180..+180 for labeling aesthetics
    ha = ((ha + 180) % 360 + 360) % 360 - 180;
    return ha;
}

// Fast RA/Dec -> Alt/Az using precomputed LST and observer lat
function radecToAltAzFast(raDeg, decDeg, lstDeg, sinLat, cosLat) {
    const H = (lstDeg - raDeg) * Math.PI / 180; // hour angle in radians
    const dec = decDeg * Math.PI / 180;
    const sinDec = Math.sin(dec), cosDec = Math.cos(dec);
    const sinAlt = sinDec * sinLat + cosDec * cosLat * Math.cos(H);
    const alt = Math.asin(sinAlt);
    const sinAz = -cosDec * Math.sin(H);
    const cosAz = sinDec * cosLat - cosDec * Math.cos(H) * sinLat;
    let az = Math.atan2(sinAz, cosAz);
    if (az < 0) az += 2 * Math.PI;
    return { altDeg: alt * 180 / Math.PI, azDeg: az * 180 / Math.PI };
}

// Variant that reuses cached sin/cos(dec) for static stars
function radecToAltAzFastStar(raDeg, sinDec, cosDec, lstDeg, sinLat, cosLat) {
    const H = (lstDeg - raDeg) * Math.PI / 180;
    const sinAlt = sinDec * sinLat + cosDec * cosLat * Math.cos(H);
    const alt = Math.asin(sinAlt);
    const sinAz = -cosDec * Math.sin(H);
    const cosAz = sinDec * cosLat - cosDec * Math.cos(H) * sinLat;
    let az = Math.atan2(sinAz, cosAz);
    if (az < 0) az += 2 * Math.PI;
    return { altDeg: alt * 180 / Math.PI, azDeg: az * 180 / Math.PI };
}

function precomputeObserver(latDeg) {
    const lat = latDeg * Math.PI / 180;
    return { sinLat: Math.sin(lat), cosLat: Math.cos(lat) };
}

// Convenience wrapper: compute Alt/Az for a given RA/Dec at a Date and observer location
function radecToAltAz(raDeg, decDeg, dateObj, latDeg, lonDeg) {
    // Normalize date to a Date object and round seconds for stability
    let d = (dateObj instanceof Date) ? new Date(dateObj.getTime()) : new Date();
    try { if (dateObj) d = new Date(dateObj); } catch (e) { d = new Date(); }
    d.setSeconds(0, 0);
    const lstDegVal = lstDegrees(new Date(d.toISOString()), lonDeg || 0);
    const obs = precomputeObserver(latDeg || 0);
    return radecToAltAzFast(raDeg, decDeg, lstDegVal, obs.sinLat, obs.cosLat);
}

// Convert altitude/azimuth to Cartesian coordinates
function altazToXYZ(alt, az) {
    const altRad = alt * Math.PI / 180;
    const azRad = az * Math.PI / 180;
    const x = Math.cos(altRad) * Math.sin(azRad);
    const y = Math.sin(altRad);
    const z = Math.cos(altRad) * Math.cos(azRad);
    return [x, y, z];
}

// 3D rotation (optional pre-rotation by latitude/longitude, then user rotation)
function rotate([x, y, z], rotX, rotY, lat, lon) {
    // Apply longitude (azimuthal) rotation about Y axis
    let x1 = x * Math.cos(lon) - z * Math.sin(lon);
    let z1 = x * Math.sin(lon) + z * Math.cos(lon);
    // Apply latitude rotation about X axis
    let y1 = y * Math.cos(lat) - z1 * Math.sin(lat);
    let z2 = y * Math.sin(lat) + z1 * Math.cos(lat);
    // User rotation: Y then X
    let x2 = x1 * Math.cos(rotY) - z2 * Math.sin(rotY);
    let z3 = x1 * Math.sin(rotY) + z2 * Math.cos(rotY);
    let y2 = y1 * Math.cos(rotX) - z3 * Math.sin(rotX);
    let z4 = y1 * Math.sin(rotX) + z3 * Math.cos(rotX);
    return [x2, y2, z4];
}

// Project 3D point to 2D canvas (stereographic)
function project([x, y, z]) {
    // Stereographic projection from (0,0,-1) onto plane through origin.
    // Preserves circles as circles; the horizon (z=0) maps to a circle.
    const denom = 1 + z;
    // Callers should cull z <= 0. For safety, clamp extremely small denom.
    const safeDenom = Math.max(denom, 1e-6);
    const k = Math.max(width, height) * 0.35 * zoom;
    const s = (2 * k) / safeDenom;
    return [
        width / 2 + x * s,
        height / 2 - y * s
    ];
}

// Inverse stereographic projection: screen coords to 3D unit vector (in view space)
function unproject(screenX, screenY) {
    const k = Math.max(width, height) * 0.35 * zoom;
    const x = (screenX - width / 2) / (2 * k);
    const y = -(screenY - height / 2) / (2 * k);
    
    // Inverse stereographic: given (x,y) on projection plane, find unit vector
    // Standard formula: p² = x² + y²; X = 2x/(1+p²), Y = 2y/(1+p²), Z = (1-p²)/(1+p²)
    const p2 = x * x + y * y;
    const denom = 1 + p2;
    return [
        (2 * x) / denom,
        (2 * y) / denom,
        (1 - p2) / denom
    ];
}

// Inverse rotation: from view space back to horizon space
function inverseRotate([x, y, z], rotX, rotY) {
    // Reverse user rotations: inverse of X rotation first, then Y
    const cx = Math.cos(rotX), sx = Math.sin(rotX);
    const cy = Math.cos(rotY), sy = Math.sin(rotY);
    
    // Inverse X rotation (rotX is applied second in forward, so undo first)
    let y1 = y * cx + z * sx;
    let z1 = -y * sx + z * cx;
    
    // Inverse Y rotation
    let x1 = x * cy + z1 * sy;
    let z2 = -x * sy + z1 * cy;
    
    return [x1, y1, z2];
}

// Convert XYZ in horizon frame to Alt/Az
function xyzToAltAz([x, y, z]) {
    const alt = Math.asin(y) * 180 / Math.PI;
    const az = Math.atan2(x, z) * 180 / Math.PI;
    return { altDeg: alt, azDeg: (az + 360) % 360 };
}

// Convert Alt/Az to RA/Dec using LST and latitude
function altazToRaDec(altDeg, azDeg, lstDeg, latDeg) {
    const alt = altDeg * Math.PI / 180;
    const az = azDeg * Math.PI / 180;
    const lat = latDeg * Math.PI / 180;
    
    const sinAlt = Math.sin(alt), cosAlt = Math.cos(alt);
    const sinAz = Math.sin(az), cosAz = Math.cos(az);
    const sinLat = Math.sin(lat), cosLat = Math.cos(lat);
    
    // Dec calculation
    const sinDec = sinAlt * sinLat + cosAlt * cosLat * cosAz;
    const dec = Math.asin(sinDec) * 180 / Math.PI;
    
    // Hour angle calculation
    const cosH = (sinAlt - sinDec * Math.sin(dec * Math.PI / 180)) / (Math.cos(dec * Math.PI / 180) * cosLat);
    const sinH = -cosAlt * sinAz / Math.cos(dec * Math.PI / 180);
    let H = Math.atan2(sinH, cosH) * 180 / Math.PI;
    
    // RA = LST - HA
    let ra = lstDeg - H;
    ra = ((ra % 360) + 360) % 360;
    
    return { raDeg: ra, decDeg: dec };
}

// Get celestial coordinates at screen position
function getCoordsAtScreen(screenX, screenY) {
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
    const lstDeg = lstDegrees(new Date(selectedDate.toISOString()), lonDeg);
    
    // Unproject screen to view space
    const viewVec = unproject(screenX, screenY);
    
    // Inverse rotate to horizon space
    const horizonVec = inverseRotate(viewVec, rotX, rotY);
    
    // Convert to Alt/Az
    const { altDeg, azDeg } = xyzToAltAz(horizonVec);
    
    // Convert to RA/Dec
    const { raDeg, decDeg } = altazToRaDec(altDeg, azDeg, lstDeg, latDeg);
    
    return { raDeg, decDeg, altDeg, azDeg };
}

// Planets are kept as a small object list; stars live in the typed arrays
let planetsList = [];    // updated when planets are replaced

function updatePlanetsList() {
    planetsList = [];
    for (let i = 0; i < stars.length; i++) {
        if (stars[i] && stars[i].type === 'planet') planetsList.push(stars[i]);
    }
}

// Update magnitude slider based on zoom level
function updateMagnitudeForZoom() {
    if (!magnitudeZoomEnabled) return;
    
    // Calculate new magnitude based on zoom level
    // Higher zoom = fainter stars visible (higher magnitude)
    const newMagnitude = baseMagnitude + (zoom - 1.0) * magnitudePerZoomLevel;
    
    // Clamp to slider bounds
    const minSliderMag = parseFloat(magFilter.min) || -2;
    const maxSliderMag = parseFloat(magFilter.max) || 20;
    const clampedMagnitude = Math.max(minSliderMag, Math.min(maxSliderMag, newMagnitude));
    
    // Update the slider and display
    magFilter.value = clampedMagnitude.toFixed(1);
    magValue.textContent = clampedMagnitude.toFixed(1);
    
    // Fetch more stars if needed and rebuild visible stars
    fetchMoreStarsIfNeeded(clampedMagnitude);
}

// ===========================================================================
// Star catalogue
//
// Stars are held as parallel typed arrays sorted by magnitude (objects with no
// recorded magnitude last). That buys three things:
//   * no per-star JS objects, so 286k stars cost ~7 MB instead of ~100 MB
//   * "everything brighter than X" is a binary search, not a filter pass
//   * the draw loop walks memory linearly, brightest first, so it can stop
//     early during interaction and still show the most important stars
// ===========================================================================
const DEG2RAD = Math.PI / 180;

let starCount = 0;        // total catalogue size, from /api/stars_bands
let starLoadedCount = 0;  // contiguous prefix actually loaded so far
let starKnownCount = 0;   // entries [0, starKnownCount) have a real magnitude
let starRA = null;        // Float32Array, degrees
let starDec = null;       // Float32Array, degrees
let starMag = null;       // Float32Array, NaN where no magnitude was recorded
let starVX = null, starVY = null, starVZ = null; // unit vectors, equatorial frame
let starNames = [];
let starNameIndex = null; // lazily built lowercase name -> index map

function allocateStarCatalogue(total) {
    starCount = total;
    starLoadedCount = 0;
    starRA = new Float32Array(total);
    starDec = new Float32Array(total);
    starMag = new Float32Array(total);
    starVX = new Float32Array(total);
    starVY = new Float32Array(total);
    starVZ = new Float32Array(total);
    starNames = new Array(total);
    starNameIndex = null;
}

// Decode the binary payload from /api/stars_bin:
// 20-byte header, then float32 ra[], dec[], mag[], then newline-joined names.
function decodeStarBand(buffer) {
    if (!buffer || buffer.byteLength < 20) throw new Error('star payload too short');
    const view = new DataView(buffer);
    const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
    if (magic !== 'SMAP') throw new Error(`unexpected star payload "${magic}"`);
    const flags = view.getUint16(6, true);
    const count = view.getUint32(8, true);
    const namesLength = view.getUint32(12, true);
    let offset = 20;
    const ra = new Float32Array(buffer, offset, count); offset += count * 4;
    const dec = new Float32Array(buffer, offset, count); offset += count * 4;
    const mag = new Float32Array(buffer, offset, count); offset += count * 4;
    let names = null;
    if ((flags & 1) && namesLength > 0) {
        names = new TextDecoder('utf-8').decode(new Uint8Array(buffer, offset, namesLength)).split('\n');
    }
    return { count, ra, dec, mag, names };
}

// Copy a decoded band into the catalogue at `start`, computing unit vectors as
// we go. Done in slices so a 100k-star band never blocks a frame.
function ingestStarBand(start, band, onDone) {
    const total = band.count;
    let i = 0;
    const SLICE = 20000;

    function step() {
        const end = Math.min(i + SLICE, total);
        for (; i < end; i++) {
            const at = start + i;
            const raDeg = band.ra[i];
            const decDeg = band.dec[i];
            starRA[at] = raDeg;
            starDec[at] = decDeg;
            starMag[at] = band.mag[i];
            const raRad = raDeg * DEG2RAD;
            const decRad = decDeg * DEG2RAD;
            const cosDec = Math.cos(decRad);
            starVX[at] = cosDec * Math.cos(raRad);
            starVY[at] = Math.sin(decRad);
            starVZ[at] = cosDec * Math.sin(raRad);
            if (band.names) starNames[at] = band.names[i];
        }
        starLoadedCount = Math.max(starLoadedCount, start + i);
        starNameIndex = null; // names changed; rebuild on next search
        scheduleDraw();
        if (i < total) {
            requestAnimationFrame(step);
        } else if (typeof onDone === 'function') {
            onDone();
        }
    }
    step();
}

// Index of the first star fainter than `magLimit`. The array is magnitude
// sorted, so everything before it is visible at this limit.
function starsBrighterThan(magLimit) {
    let lo = 0;
    let hi = Math.min(starKnownCount, starLoadedCount);
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (starMag[mid] <= magLimit) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

// Range of catalogue entries with no recorded magnitude that are loaded.
function unknownMagRange() {
    if (starLoadedCount <= starKnownCount) return [0, 0];
    return [starKnownCount, starLoadedCount];
}

function starNameAt(index) {
    return starNames[index] || `#${index}`;
}

function findStarByName(name) {
    if (!name) return -1;
    if (!starNameIndex) {
        starNameIndex = new Map();
        for (let i = 0; i < starLoadedCount; i++) {
            const n = starNames[i];
            if (n) starNameIndex.set(n.toLowerCase(), i);
        }
    }
    const hit = starNameIndex.get(String(name).toLowerCase());
    return hit === undefined ? -1 : hit;
}

// A catalogue entry in the object shape the rest of the UI expects
function starObjectAt(index) {
    const mag = starMag[index];
    return {
        name: starNameAt(index),
        ra: starRA[index],
        dec: starDec[index],
        mag: Number.isNaN(mag) ? null : mag,
        magUnknown: Number.isNaN(mag),
        type: 'star',
        catalogIndex: index,
    };
}

// Build a 3x3 matrix that maps equatorial unit vectors (x=cosδcosα, y=sinδ, z=cosδsinα)
// into view space (after converting to horizon frame using LST/latitude, then applying user rotY, rotX)
function buildEqToViewMatrix(latDeg, lstDeg, rotX, rotY) {
    const deg2rad = Math.PI / 180;
    const φ = latDeg * deg2rad;
    const Θ = lstDeg * deg2rad;
    const sφ = Math.sin(φ), cφ = Math.cos(φ);
    const sΘ = Math.sin(Θ), cΘ = Math.cos(Θ);

    // Equatorial -> Horizon (x_east, y_up, z_north) for input vector [x=cosδcosα, y=sinδ, z=cosδsinα]
    const M = [
        [-sΘ,            0,   cΘ],
        [ cφ * cΘ,      sφ,  cφ * sΘ],
        [-sφ * cΘ,      cφ, -sφ * sΘ]
    ];

    // User rotations: first around Y (rotY), then around X (rotX) in horizon frame
    const sy = Math.sin(rotY), cy = Math.cos(rotY);
    const sx = Math.sin(rotX), cx = Math.cos(rotX);
    const Ry = [
        [ cy,  0, -sy],
        [  0,  1,   0],
        [ sy,  0,  cy]
    ];
    const Rx = [
        [ 1,  0,   0],
        [ 0, cx, -sx],
        [ 0, sx,  cx]
    ];

    // Multiply A*B helper
    function mul3x3(A, B) {
        const R = [ [0,0,0], [0,0,0], [0,0,0] ];
        for (let i = 0; i < 3; i++) {
            for (let j = 0; j < 3; j++) {
                R[i][j] = A[i][0]*B[0][j] + A[i][1]*B[1][j] + A[i][2]*B[2][j];
            }
        }
        return R;
    }

    const Ruser = mul3x3(Rx, Ry);
    return { Mview: mul3x3(Ruser, M), upRow: M[1] };
}

function mulMat3Vec3(M, v) {
    return [
        M[0][0]*v[0] + M[0][1]*v[1] + M[0][2]*v[2],
        M[1][0]*v[0] + M[1][1]*v[1] + M[1][2]*v[2],
        M[2][0]*v[0] + M[2][1]*v[1] + M[2][2]*v[2]
    ];
}

// Ecliptic coordinate conversion
// Convert ecliptic longitude/latitude (lambda, beta) to equatorial RA/Dec (degrees)
function eclipticToEquatorial(lambdaDeg, betaDeg) {
    // Mean obliquity of the ecliptic (approx, J2000)
    const eps = 23.4392911 * Math.PI / 180; // radians
    const lam = lambdaDeg * Math.PI / 180;
    const bet = betaDeg * Math.PI / 180;
    const sinDec = Math.sin(bet) * Math.cos(eps) + Math.cos(bet) * Math.sin(eps) * Math.sin(lam);
    const dec = Math.asin(sinDec);
    const y = Math.sin(lam) * Math.cos(eps) - Math.tan(bet) * Math.sin(eps);
    const x = Math.cos(lam);
    let ra = Math.atan2(y, x); // radians, range -pi..pi
    if (ra < 0) ra += 2 * Math.PI;
    return { raDeg: ra * 180 / Math.PI, decDeg: dec * 180 / Math.PI };
}

// ===========================================================================
// Star names
//
// The database itself names only 40 stars. /api/star_names serves the IAU
// proper names (407 of them) matched onto catalogue designations, which we
// resolve to catalogue indices so a label can be drawn at the star's position.
// ===========================================================================
let starNameEntries = null;       // [{ id, name, mag, index }]
let starNameLoadPromise = null;
let starNamesResolvedAt = -1;     // starLoadedCount when indices were last resolved

function loadStarNames() {
    if (starNameEntries) return Promise.resolve(starNameEntries);
    if (starNameLoadPromise) return starNameLoadPromise;

    starNameLoadPromise = fetch('/api/star_names')
        .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
        })
        .then(data => {
            starNameEntries = (data.stars || []).map(entry => ({
                id: entry.id,
                name: entry.name,
                bayer: entry.bayer || '',
                mag: entry.mag,
                index: -1,
            }));
            starNamesResolvedAt = -1;
            console.log(`Loaded ${starNameEntries.length} star names`);
            scheduleDraw();
            return starNameEntries;
        })
        .catch(err => {
            console.error('Star names failed to load:', err);
            starNameLoadPromise = null;
            return null;
        });
    return starNameLoadPromise;
}

// Map each name onto its catalogue row. Repeated only when more of the
// catalogue has arrived, never per frame.
function resolveStarNameIndices() {
    if (!starNameEntries || starNamesResolvedAt === starLoadedCount) return;
    starNamesResolvedAt = starLoadedCount;
    for (const entry of starNameEntries) {
        if (entry.index < 0) entry.index = findStarByName(entry.id);
    }
}

const MAX_STAR_LABELS = 80;

function drawStarNames(Mview, magLimit) {
    if (!starNameEntries || !starMag || starLoadedCount === 0) return;
    resolveStarNameIndices();

    const m00 = Mview[0][0], m01 = Mview[0][1], m02 = Mview[0][2];
    const m10 = Mview[1][0], m11 = Mview[1][1], m12 = Mview[1][2];
    const m20 = Mview[2][0], m21 = Mview[2][1], m22 = Mview[2][2];
    const k2 = 2 * Math.max(width, height) * 0.35 * zoom;
    const halfW = width / 2, halfH = height / 2;

    ctx.save();
    ctx.fillStyle = 'rgba(255, 226, 168, 0.82)';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';

    // Coarse occupancy grid so labels don't pile on top of each other
    const cell = 54;
    const taken = new Set();
    let drawnLabels = 0;

    for (const entry of starNameEntries) {
        if (drawnLabels >= MAX_STAR_LABELS) break;
        const i = entry.index;
        if (i < 0) continue;
        const mag = starMag[i];
        // Entries are sorted brightest first, so once we pass the limit we can stop
        if (!Number.isNaN(mag) && mag > magLimit) break;

        const ax = starVX[i], ay = starVY[i], az = starVZ[i];
        const z = m20 * ax + m21 * ay + m22 * az;
        if (z <= 0) continue;
        const x = m00 * ax + m01 * ay + m02 * az;
        const y = m10 * ax + m11 * ay + m12 * az;
        const scale = k2 / (1 + z);
        const px = halfW + x * scale;
        const py = halfH - y * scale;
        if (px < 0 || px > width - 40 || py < 8 || py > height - 8) continue;

        const key = ((px / cell) | 0) + ':' + ((py / cell) | 0);
        if (taken.has(key)) continue;
        taken.add(key);

        const offset = Math.max(5, starSizeForMag(mag) + 4);
        ctx.fillText(entry.name, px + offset, py);
        drawnLabels++;
    }
    ctx.restore();
}

// ===========================================================================
// Constellation figures
//
// Line data is generated by scripts/build_constellations.py from d3-celestial
// (BSD-3-Clause) and lives in static/data/constellations.json. Every vertex has
// been snapped onto a star in this server's own catalogue, so the figures land
// exactly on the drawn stars. Loaded on first use, not on page load.
// ===========================================================================
let constellationFigures = null;   // [{ id, name, anchor:{v}, paths:[Float64Array-ish] }]
let constellationLoadPromise = null;

function loadConstellations() {
    if (constellationFigures) return Promise.resolve(constellationFigures);
    if (constellationLoadPromise) return constellationLoadPromise;

    constellationLoadPromise = fetch('/static/data/constellations.json')
        .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
        })
        .then(data => {
            // Precompute a unit vector per vertex once; the draw loop then only
            // has to rotate and project them.
            constellationFigures = (data.constellations || []).map(entry => ({
                id: entry.id,
                name: entry.name,
                anchor: entry.anchor ? radecToXYZ(entry.anchor[0], entry.anchor[1]) : null,
                paths: entry.lines.map(line => line.map(([ra, dec]) => radecToXYZ(ra, dec))),
            }));
            console.log(`Loaded ${constellationFigures.length} constellation figures`);
            scheduleDraw();
            return constellationFigures;
        })
        .catch(err => {
            console.error('Constellation data failed to load:', err);
            constellationLoadPromise = null;
            return null;
        });
    return constellationLoadPromise;
}

// Project a vector already in view space, or null when it is behind the viewer.
function projectViewVector(vx, vy, vz, k2, halfW, halfH) {
    if (vz <= 0) return null;
    const scale = k2 / (1 + vz);
    return [halfW + vx * scale, halfH - vy * scale];
}

function drawConstellations(Mview) {
    if (!constellationFigures) return;

    const m00 = Mview[0][0], m01 = Mview[0][1], m02 = Mview[0][2];
    const m10 = Mview[1][0], m11 = Mview[1][1], m12 = Mview[1][2];
    const m20 = Mview[2][0], m21 = Mview[2][1], m22 = Mview[2][2];
    const k2 = 2 * Math.max(width, height) * 0.35 * zoom;
    const halfW = width / 2, halfH = height / 2;

    ctx.save();
    ctx.strokeStyle = 'rgba(126, 176, 255, 0.45)';
    ctx.lineWidth = 1;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();

    for (const figure of constellationFigures) {
        for (const path of figure.paths) {
            // Transform the whole path into view space first
            let prevX = 0, prevY = 0, prevZ = 0, havePrev = false;
            for (const v of path) {
                const vx = m00 * v[0] + m01 * v[1] + m02 * v[2];
                const vy = m10 * v[0] + m11 * v[1] + m12 * v[2];
                const vz = m20 * v[0] + m21 * v[1] + m22 * v[2];
                if (havePrev) {
                    strokeGreatCircle(prevX, prevY, prevZ, vx, vy, vz, k2, halfW, halfH);
                }
                prevX = vx; prevY = vy; prevZ = vz;
                havePrev = true;
            }
        }
    }
    ctx.stroke();

    // Names, at the anchor point the source data places inside each figure
    if (showConstellationNames && showConstellationNames.checked) {
        ctx.fillStyle = 'rgba(150, 195, 255, 0.72)';
        ctx.font = '12px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        for (const figure of constellationFigures) {
            const a = figure.anchor;
            if (!a) continue;
            const vx = m00 * a[0] + m01 * a[1] + m02 * a[2];
            const vy = m10 * a[0] + m11 * a[1] + m12 * a[2];
            const vz = m20 * a[0] + m21 * a[1] + m22 * a[2];
            const p = projectViewVector(vx, vy, vz, k2, halfW, halfH);
            if (!p) continue;
            if (p[0] < 0 || p[0] > width || p[1] < 0 || p[1] > height) continue;
            ctx.fillText(figure.name, p[0], p[1]);
        }
    }
    ctx.restore();
}

// A straight canvas line between two projected stars is not the same as the
// line between them on the sky, so walk the great circle instead - roughly one
// step every 2 degrees - and break the stroke wherever it passes behind us.
function strokeGreatCircle(ax, ay, az, bx, by, bz, k2, halfW, halfH) {
    let dot = ax * bx + ay * by + az * bz;
    if (dot > 1) dot = 1; else if (dot < -1) dot = -1;
    const omega = Math.acos(dot);

    if (omega < 1e-4) {
        // Coincident endpoints: nothing to draw
        return;
    }

    const steps = Math.max(2, Math.min(48, Math.ceil((omega * 180 / Math.PI) / 2)));
    const sinOmega = Math.sin(omega);
    let started = false;

    for (let i = 0; i <= steps; i++) {
        const t = i / steps;
        const s1 = Math.sin((1 - t) * omega) / sinOmega;
        const s2 = Math.sin(t * omega) / sinOmega;
        const vz = s1 * az + s2 * bz;
        if (vz <= 0) { started = false; continue; }
        const vx = s1 * ax + s2 * bx;
        const vy = s1 * ay + s2 * by;
        const scale = k2 / (1 + vz);
        const px = halfW + vx * scale;
        const py = halfH - vy * scale;
        if (started) ctx.lineTo(px, py);
        else { ctx.moveTo(px, py); started = true; }
    }
}

function drawEcliptic() {
    // Draw the ecliptic (beta=0) converted to horizon coordinates
    ctx.save();
    ctx.strokeStyle = "rgba(255, 215, 0, 0.6)"; // golden line
    ctx.lineWidth = 1.5;
    const cullThreshold = 0;
    ctx.beginPath();
    let started = false;

    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
    const lstDegVal = lstDegrees(new Date(selectedDate.toISOString()), lonDeg);
    const { sinLat, cosLat } = precomputeObserver(latDeg);

    for (let lam = 0; lam <= 360; lam += 2) {
    const { raDeg, decDeg } = eclipticToEquatorial(lam, 0);
    const { altDeg, azDeg } = radecToAltAzFast(raDeg, decDeg, lstDegVal, sinLat, cosLat);
        let [x, y, z] = altazToXYZ(altDeg, azDeg);
        [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
        if (z <= cullThreshold) { started = false; continue; }
        const [cx, cy] = project([x, y, z]);
        if (!started) { ctx.moveTo(cx, cy); started = true; }
        else ctx.lineTo(cx, cy);
    }
    ctx.stroke();
    ctx.restore();
}

// Draw horizon coordinate grid
function drawHorizonGrid(lat, lon) {
    ctx.save();
    ctx.strokeStyle = "rgba(100, 150, 255, 0.3)"; // Light blue
    ctx.lineWidth = 1;
    ctx.font = "12px sans-serif";
    ctx.fillStyle = "rgba(100, 150, 255, 0.6)";

    // Simple back-face culling - only draw lines facing the viewer
    const cullThreshold = 0;

    // Draw altitude circles (elevation lines) - include negative altitudes
    for (let alt = -90; alt <= 90; alt += 10) {
        ctx.beginPath();
        let firstPoint = true;
        for (let az = 0; az <= 360; az += 3) {
            let [x, y, z] = altazToXYZ(alt, az);
            // Only apply user rotation, not lat/lon (horizon stays fixed)
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z <= cullThreshold) { firstPoint = true; continue; } // break path across back side
            const [cx, cy] = project([x, y, z]);
            
            if (firstPoint) {
                ctx.moveTo(cx, cy);
                firstPoint = false;
            } else {
                ctx.lineTo(cx, cy);
            }
        }
        ctx.stroke();
        
        // Label altitude lines more frequently
        if (alt % 20 === 0 && alt !== 0) {
            let [x, y, z] = altazToXYZ(alt, 0); // North point
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z > cullThreshold) {
                const [cx, cy] = project([x, y, z]);
                ctx.fillText(`${alt}°`, cx + 5, cy - 5);
            }
        }
    }

    // Draw azimuth lines (compass directions)
    for (let az = 0; az < 360; az += 10) {
        ctx.beginPath();
        let firstPoint = true;
        for (let alt = -90; alt <= 90; alt += 2) {
            let [x, y, z] = altazToXYZ(alt, az);
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z <= cullThreshold) { firstPoint = true; continue; } // break path across back side
            const [cx, cy] = project([x, y, z]);
            
            if (firstPoint) {
                ctx.moveTo(cx, cy);
                firstPoint = false;
            } else {
                ctx.lineTo(cx, cy);
            }
        }
        ctx.stroke();
        
        // Label azimuth lines more frequently
        if (az % 30 === 0) {
            let [x, y, z] = altazToXYZ(5, az); // 5° above horizon
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z > cullThreshold) {
                const [cx, cy] = project([x, y, z]);
                ctx.fillText(`${az}°`, cx - 8, cy + 15);
            }
        }
    }

    // Draw and label cardinal directions
    const cardinals = [
        { az: 0, label: "N" },
        { az: 90, label: "E" },
        { az: 180, label: "S" },
        { az: 270, label: "W" }
    ];
    
    ctx.font = "16px sans-serif";
    ctx.fillStyle = "rgba(100, 150, 255, 0.8)";
    for (const cardinal of cardinals) {
        let [x, y, z] = altazToXYZ(5, cardinal.az); // 5° above horizon
        [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
        if (z > cullThreshold) {
            const [cx, cy] = project([x, y, z]);
            ctx.fillText(cardinal.label, cx - 8, cy + 5);
        }
    }
    
    ctx.restore();
}

// Draw a translucent green tint for the region below the horizon (alt < 0)
function drawBelowHorizonTint() {
    ctx.save();
    ctx.fillStyle = 'rgba(50, 205, 50, 0.10)'; // grass green at ~10%
    const altStep = 3; // finer near horizon to avoid visible faceting
    const azStep = 4;
    const cullThreshold = 0; // front hemisphere only

    // Iterate small horizon cells and fill quads that are fully visible (all corners z>0)
    for (let alt = -90; alt < 0; alt += altStep) {
        const alt2 = Math.min(alt + altStep, 0);
        for (let az = 0; az < 360; az += azStep) {
            const az2 = az + azStep;

            // Compute four corners in horizon coords
            let p1 = altazToXYZ(alt, az);
            let p2 = altazToXYZ(alt2, az);
            let p3 = altazToXYZ(alt2, az2);
            let p4 = altazToXYZ(alt, az2);

            // Apply only user rotation (horizon base frame)
            p1 = rotate(p1, rotX, rotY, 0, 0);
            p2 = rotate(p2, rotX, rotY, 0, 0);
            p3 = rotate(p3, rotX, rotY, 0, 0);
            p4 = rotate(p4, rotX, rotY, 0, 0);

            // Cull any cell that is partially or fully behind the camera to avoid artifacts
            if (p1[2] <= cullThreshold || p2[2] <= cullThreshold || p3[2] <= cullThreshold || p4[2] <= cullThreshold) {
                continue;
            }

            // Project and fill the quad
            const a = project(p1);
            const b = project(p2);
            const c = project(p3);
            const d = project(p4);
            ctx.beginPath();
            ctx.moveTo(a[0], a[1]);
            ctx.lineTo(b[0], b[1]);
            ctx.lineTo(c[0], c[1]);
            ctx.lineTo(d[0], d[1]);
            ctx.closePath();
            ctx.fill();
        }
    }
    ctx.restore();
}

// Draw equatorial coordinate grid
function drawEquatorialGrid() {
    ctx.save();
    ctx.strokeStyle = "rgba(255, 150, 100, 0.3)"; // Light orange
    ctx.lineWidth = 1;
    ctx.font = "12px sans-serif";
    ctx.fillStyle = "rgba(255, 150, 100, 0.6)";

    // Simple back-face culling - only draw lines facing the viewer
    const cullThreshold = 0;

    // Observer/time
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
    currentLSTDeg = lstDegrees(new Date(selectedDate.toISOString()), lonDeg);
    const { sinLat, cosLat } = precomputeObserver(latDeg);

    // Draw declination circles in equatorial coords, converted to horizon
    for (let dec = -90; dec <= 90; dec += 10) {
        ctx.strokeStyle = (dec === 0) ? "rgba(255, 150, 100, 0.5)" : "rgba(255, 150, 100, 0.3)";
        ctx.beginPath();
        let firstPoint = true;
        for (let ra = 0; ra <= 360; ra += 3) {
            const { altDeg, azDeg } = radecToAltAzFast(ra, dec, currentLSTDeg, sinLat, cosLat);
            let [x, y, z] = altazToXYZ(altDeg, azDeg);
            // Only apply user rotation; horizon is our base frame
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z <= cullThreshold) { firstPoint = true; continue; }
            const [cx, cy] = project([x, y, z]);
            if (firstPoint) { ctx.moveTo(cx, cy); firstPoint = false; }
            else ctx.lineTo(cx, cy);
        }
        ctx.stroke();

        // Label declination lines every 30°
        if (dec % 30 === 0) {
            const { altDeg, azDeg } = radecToAltAzFast(0, dec, currentLSTDeg, sinLat, cosLat); // RA=0h point
            let [x, y, z] = altazToXYZ(altDeg, azDeg);
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z > cullThreshold) {
                const [cx, cy] = project([x, y, z]);
                ctx.fillText(`${dec}°`, cx + 5, cy - 5);
            }
        }
    }

    // Draw hour angle (HA) lines by converting HA -> RA (RA = LST - HA)
    ctx.strokeStyle = "rgba(255, 150, 100, 0.3)";
    for (let ha = -180; ha < 180; ha += 10) {
        let ra = currentLSTDeg - ha; // degrees
        ra = ((ra % 360) + 360) % 360;
        ctx.beginPath();
        let firstPoint = true;
        for (let dec = -90; dec <= 90; dec += 2) {
            const { altDeg, azDeg } = radecToAltAzFast(ra, dec, currentLSTDeg, sinLat, cosLat);
            let [x, y, z] = altazToXYZ(altDeg, azDeg);
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z <= cullThreshold) { firstPoint = true; continue; }
            const [cx, cy] = project([x, y, z]);
            if (firstPoint) { ctx.moveTo(cx, cy); firstPoint = false; }
            else ctx.lineTo(cx, cy);
        }
        ctx.stroke();

        // Label HA at celestial equator for multiples of 30° (2 hours)
        const norm30 = ((ha % 30) + 30) % 30;
        if (Math.abs(norm30) < 1e-6) {
            const { altDeg, azDeg } = radecToAltAzFast(ra, 0, currentLSTDeg, sinLat, cosLat);
            let [x, y, z] = altazToXYZ(altDeg, azDeg);
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z > cullThreshold) {
                const [cx, cy] = project([x, y, z]);
                let haHours = ha / 15;
                let label;
                if (Math.abs(haHours) < 0.5) label = 'HA 0h';
                else label = `HA ${(haHours > 0 ? '+' : '')}${Math.round(haHours)}h`;
                ctx.fillText(label, cx - 18, cy + 15);
            }
        }
    }

    ctx.restore();
}

// Draw all stars/planets
// ===========================================================================
// Frame scheduling and level of detail
//
// Every interaction used to call draw() synchronously, so a single drag could
// trigger several full redraws per frame. Draws are now coalesced into one per
// animation frame, and while the view is actually moving we draw only the
// brightest `interactionBudget` stars - they are first in the array, so this is
// just an early exit. The budget tunes itself to keep frames near 60fps, and a
// full-detail frame is drawn once the view settles.
// ===========================================================================
let drawScheduled = false;
let interacting = false;
let interactionTimer = null;
let interactionBudget = 40000;  // stars drawn per frame while moving
const MIN_INTERACTION_BUDGET = 4000;
const MAX_INTERACTION_BUDGET = 400000;
let lastDrawStats = { drawn: 0, ms: 0, capped: false };

function scheduleDraw() {
    if (drawScheduled) return;
    drawScheduled = true;
    requestAnimationFrame(() => {
        drawScheduled = false;
        draw();
    });
}

// Mark the view as moving; a full-quality frame follows once it stops.
function beginInteraction() {
    interacting = true;
    if (interactionTimer) clearTimeout(interactionTimer);
    interactionTimer = setTimeout(() => {
        interacting = false;
        interactionTimer = null;
        scheduleDraw(); // settle into full detail
    }, 180);
}

function adaptInteractionBudget(frameMs, wasCapped) {
    if (!interacting) return;
    if (frameMs > 20 && interactionBudget > MIN_INTERACTION_BUDGET) {
        interactionBudget = Math.max(MIN_INTERACTION_BUDGET, Math.floor(interactionBudget * 0.75));
    } else if (frameMs < 11 && wasCapped && interactionBudget < MAX_INTERACTION_BUDGET) {
        interactionBudget = Math.min(MAX_INTERACTION_BUDGET, Math.floor(interactionBudget * 1.25));
    }
}

// Cached sidereal time: recomputing it from a date string on every frame was
// costing more than projecting several thousand stars.
let lstCacheKey = '';
function currentSiderealTime(selectedDate, lonDeg) {
    const key = `${selectedDate.getTime()}|${lonDeg}`;
    if (key !== lstCacheKey) {
        lstCacheKey = key;
        currentLSTDeg = lstDegrees(new Date(selectedDate.toISOString()), lonDeg);
    }
    return currentLSTDeg;
}

// Star brightness is bucketed so the fill colour is set a handful of times per
// frame instead of once per star. Because the catalogue is magnitude sorted,
// consecutive stars almost always land in the same bucket.
const STAR_ALPHA_STEPS = 8;
const STAR_COLORS = [];
for (let i = 0; i < STAR_ALPHA_STEPS; i++) {
    const alpha = 0.35 + (0.65 * i) / (STAR_ALPHA_STEPS - 1);
    STAR_COLORS.push(`rgba(255,255,255,${alpha.toFixed(3)})`);
}
const UNKNOWN_MAG_COLOR = 'rgba(150, 200, 255, 0.75)';

function starAlphaBucket(mag) {
    // mag -1.5 (brightest) -> top bucket, mag 12+ -> dimmest
    const t = 1 - (mag + 2) / 14;
    const idx = Math.round(t * (STAR_ALPHA_STEPS - 1));
    return idx < 0 ? 0 : (idx >= STAR_ALPHA_STEPS ? STAR_ALPHA_STEPS - 1 : idx);
}

// Magnitude -> radius lookup, rebuilt only when the zoom changes.
const SIZE_LUT_MIN = -2, SIZE_LUT_MAX = 22, SIZE_LUT_STEP = 0.25;
const SIZE_LUT_LENGTH = Math.ceil((SIZE_LUT_MAX - SIZE_LUT_MIN) / SIZE_LUT_STEP) + 1;
const starSizeLUT = new Float32Array(SIZE_LUT_LENGTH);
let sizeLUTZoom = -1;

function refreshStarSizeLUT() {
    if (sizeLUTZoom === zoom) return;
    sizeLUTZoom = zoom;
    for (let i = 0; i < SIZE_LUT_LENGTH; i++) {
        const mag = SIZE_LUT_MIN + i * SIZE_LUT_STEP;
        starSizeLUT[i] = getZoomedStarSize(getMagnitudeBasedSize(mag));
    }
}

function starSizeForMag(mag) {
    let idx = ((mag - SIZE_LUT_MIN) / SIZE_LUT_STEP) | 0;
    if (idx < 0) idx = 0;
    else if (idx >= SIZE_LUT_LENGTH) idx = SIZE_LUT_LENGTH - 1;
    return starSizeLUT[idx];
}

// The hot loop. Walks the magnitude-sorted arrays, projects each star inline
// (no allocations, no property lookups) and culls anything behind the viewer or
// off-screen before it costs a fill.
function drawCatalogueStars(Mview, magLimit) {
    if (!starMag || starLoadedCount === 0) return { drawn: 0, capped: false };

    refreshStarSizeLUT();

    let end = starsBrighterThan(magLimit);
    let capped = false;
    if (interacting && end > interactionBudget) {
        end = interactionBudget;
        capped = true;
    }

    const m00 = Mview[0][0], m01 = Mview[0][1], m02 = Mview[0][2];
    const m10 = Mview[1][0], m11 = Mview[1][1], m12 = Mview[1][2];
    const m20 = Mview[2][0], m21 = Mview[2][1], m22 = Mview[2][2];
    const k2 = 2 * Math.max(width, height) * 0.35 * zoom;
    const halfW = width / 2, halfH = height / 2;
    const margin = 8;
    const maxX = width + margin, maxY = height + margin;

    const vx = starVX, vy = starVY, vz = starVZ, mags = starMag;
    let bucket = -1;
    let drawn = 0;

    for (let i = 0; i < end; i++) {
        const ax = vx[i], ay = vy[i], az = vz[i];
        // z first: half the sky is behind the viewer and costs nothing more
        const z = m20 * ax + m21 * ay + m22 * az;
        if (z <= 0) continue;
        const x = m00 * ax + m01 * ay + m02 * az;
        const y = m10 * ax + m11 * ay + m12 * az;
        const scale = k2 / (1 + z);
        const px = halfW + x * scale;
        if (px < -margin || px > maxX) continue;
        const py = halfH - y * scale;
        if (py < -margin || py > maxY) continue;

        const mag = mags[i];
        const b = starAlphaBucket(mag);
        if (b !== bucket) {
            bucket = b;
            ctx.fillStyle = STAR_COLORS[b];
        }
        const size = starSizeForMag(mag);
        if (size <= 1.6) {
            // Sub-pixel stars: a rect is several times cheaper than an arc and
            // indistinguishable at this size
            ctx.fillRect(px | 0, py | 0, 1, 1);
        } else if (size <= 2.6) {
            ctx.fillRect((px - 1) | 0, (py - 1) | 0, 2, 2);
        } else {
            ctx.beginPath();
            ctx.arc(px, py, size, 0, 6.283185307179586);
            ctx.fill();
        }
        drawn++;
    }

    // Objects with no recorded magnitude: drawn only on request, in their own
    // colour, since there is no honest place for them on the magnitude scale.
    if (showUnknownMag && showUnknownMag.checked) {
        const [uStart, uEnd] = unknownMagRange();
        if (uEnd > uStart) {
            ctx.fillStyle = UNKNOWN_MAG_COLOR;
            const unknownSize = Math.max(1, Math.min(3, 1 + zoom * 0.35));
            const half = unknownSize / 2;
            let limit = uEnd;
            if (interacting && (limit - uStart) > interactionBudget) {
                limit = uStart + interactionBudget;
                capped = true;
            }
            for (let i = uStart; i < limit; i++) {
                const ax = vx[i], ay = vy[i], az = vz[i];
                const z = m20 * ax + m21 * ay + m22 * az;
                if (z <= 0) continue;
                const x = m00 * ax + m01 * ay + m02 * az;
                const y = m10 * ax + m11 * ay + m12 * az;
                const scale = k2 / (1 + z);
                const px = halfW + x * scale;
                if (px < -margin || px > maxX) continue;
                const py = halfH - y * scale;
                if (py < -margin || py > maxY) continue;
                ctx.fillRect((px - half) | 0, (py - half) | 0, unknownSize, unknownSize);
                drawn++;
            }
        }
    }

    ctx.globalAlpha = 1;
    return { drawn, capped };
}

function draw() {
    const drawStart = performance.now();
    ctx.clearRect(0, 0, width, height);

    // Get filter values
    const magLimit = parseFloat(magFilter.value);
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
    currentSiderealTime(selectedDate, lonDeg);

    const showStarsVal = showStars.checked;
    const showPlanetsVal = showPlanets.checked;
    const { sinLat, cosLat } = precomputeObserver(latDeg);

    // Optional below-horizon tint, draw first so grids and stars are above
    if (showBelowHorizon && showBelowHorizon.checked) {
        drawBelowHorizonTint();
    }

    // Draw coordinate grids (before stars so they appear behind stars but above tint)
    if (showHorizonGrid.checked) {
        // Horizon grid is defined directly in Alt/Az, only user rotation applies
        drawHorizonGrid(0, 0);
    }
    if (showEquatorialGrid.checked) {
        drawEquatorialGrid();
    }
    if (showEcliptic && showEcliptic.checked) {
        drawEcliptic();
    }

    // Everything from here on shares one equatorial -> view transform
    const { Mview, upRow } = buildEqToViewMatrix(latDeg, currentLSTDeg, rotX, rotY);

    // Constellation figures sit under the stars so the sprites stay on top
    if (showConstellations && showConstellations.checked) {
        drawConstellations(Mview);
    }

    // Ensure planets list is up-to-date (cheap scan if empty)
    if (planetsList.length === 0) updatePlanetsList();

    // Stars first (the catalogue is magnitude sorted, so this is a prefix walk)
    let drawn = 0, capped = false;
    if (showStarsVal) {
        const res = drawCatalogueStars(Mview, magLimit);
        drawn = res.drawn;
        capped = res.capped;
    }

    // Star names sit above the sprites they label
    if (showStarsVal && showStarNames && showStarNames.checked) {
        drawStarNames(Mview, magLimit);
    }

    // Planets on top
    if (showPlanetsVal) {
        for (let i = 0; i < planetsList.length; i++) {
            const obj = planetsList[i];
            // Respect magnitude filter for planets: hide planets fainter than limit
            const effectiveMag = obj.mag == null ? 50 : obj.mag;
            if (effectiveMag > magLimit) continue;
            const v = obj.xyz || radecToXYZ(obj.ra, obj.dec);
            const w0 = Mview[0], w1 = Mview[1], w2 = Mview[2];
            const x = w0[0]*v[0] + w0[1]*v[1] + w0[2]*v[2];
            const y = w1[0]*v[0] + w1[1]*v[1] + w1[2]*v[2];
            const z = w2[0]*v[0] + w2[1]*v[1] + w2[2]*v[2];
            if (z <= 0) continue;
            const [cx, cy] = project([x, y, z]);

            const size = getZoomedPlanetSize();
            ctx.globalAlpha = 1;
            if (obj.icon && planetImages[obj.icon]) {
                const img = planetImages[obj.icon];
                ctx.drawImage(img, cx - size/2, cy - size/2, size, size);
            } else {
                ctx.fillStyle = "#ffa500";
                ctx.beginPath();
                ctx.arc(cx, cy, size/2, 0, 2*Math.PI);
                ctx.fill();
            }
        }
    }

    // Draw orange highlight ring for searched object (using Alt/Az)
    if (searchedObject) {
        // An explicitly searched object is always ringed, including ones with no
        // recorded magnitude that the slider can't reach
        const effectiveMag = searchedObject.mag == null ? -99 : searchedObject.mag;
        if (effectiveMag <= magLimit) {
            const { altDeg, azDeg } = radecToAltAz(searchedObject.ra, searchedObject.dec, selectedDate, latDeg, lonDeg);
            let [x, y, z] = altazToXYZ(altDeg, azDeg);
            [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
            if (z > 0) {
                const [cx, cy] = project([x, y, z]);
                highlightAnimation += 0.1;
                const ringSize = 20 + Math.sin(highlightAnimation) * 5;
                const opacity = 0.7 + Math.sin(highlightAnimation * 2) * 0.3;
                ctx.strokeStyle = `rgba(255, 165, 0, ${opacity})`;
                ctx.lineWidth = 3;
                ctx.beginPath();
                ctx.arc(cx, cy, ringSize, 0, 2*Math.PI);
                ctx.stroke();
                ctx.lineWidth = 1;
            }
        }
    }

    // Draw telescope position marker
    if (telescopePosition) {
        // Use Alt/Az coordinates so telescope marker doesn't move with time changes
        // Only moves when actual telescope position is updated from server
        let [x, y, z] = altazToXYZ(telescopePosition.alt, telescopePosition.az);
        [x, y, z] = rotate([x, y, z], rotX, rotY, 0, 0);
        if (z > 0) {
            const [cx, cy] = project([x, y, z]);
            const scaledMarkerSize = telescopeMarkerSize * zoom;
            
            // Draw a distinctive crosshair/scope marker
            ctx.globalAlpha = 1;
            ctx.strokeStyle = telescopeMarkerColor;
            ctx.lineWidth = 2;
            
            // Outer circle
            ctx.beginPath();
            ctx.arc(cx, cy, scaledMarkerSize, 0, 2*Math.PI);
            ctx.stroke();
            
            // Crosshairs
            const crossSize = scaledMarkerSize * 1.3;
            ctx.beginPath();
            ctx.moveTo(cx - crossSize, cy);
            ctx.lineTo(cx + crossSize, cy);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(cx, cy - crossSize);
            ctx.lineTo(cx, cy + crossSize);
            ctx.stroke();
            
            // Central dot
            ctx.fillStyle = telescopeMarkerColor;
            ctx.beginPath();
            ctx.arc(cx, cy, 3, 0, 2*Math.PI);
            ctx.fill();
            
            // Label with coordinates
            ctx.globalAlpha = 1;
            ctx.fillStyle = telescopeMarkerColor;
            ctx.font = "12px monospace";
            ctx.textAlign = "left";
            const raHours = telescopePosition.ra / 15;
            const raH = Math.floor(raHours);
            const raM = Math.floor((raHours - raH) * 60);
            const raS = ((raHours - raH) * 60 - raM) * 60;
            
            const decSign = telescopePosition.dec >= 0 ? '+' : '-';
            const decAbs = Math.abs(telescopePosition.dec);
            const decD = Math.floor(decAbs);
            const decM = Math.floor((decAbs - decD) * 60);
            const decS = ((decAbs - decD) * 60 - decM) * 60;
            
            const raStr = `${raH}h${raM}m${raS.toFixed(1)}s`;
            const decStr = `${decSign}${decD}°${decM}'${decS.toFixed(1)}"`;
            ctx.fillText(`Telescope: ${raStr}`, cx + crossSize + 10, cy - 10);
            ctx.fillText(decStr, cx + crossSize + 10, cy + 5);
        }
    }

    const frameMs = performance.now() - drawStart;
    lastDrawStats = { drawn, ms: frameMs, capped };
    adaptInteractionBudget(frameMs, capped);
    updateRenderStats();
}

// Optional HUD: how many objects the last frame actually drew and how long it
// took. Handy for judging whether a magnitude limit is worth the frame cost.
function updateRenderStats() {
    if (!renderStatsDiv) return;
    if (!showRenderStats || !showRenderStats.checked) {
        if (renderStatsDiv.style.display !== 'none') renderStatsDiv.style.display = 'none';
        return;
    }
    renderStatsDiv.style.display = 'block';
    const loaded = starLoadedCount.toLocaleString();
    const total = starCount ? starCount.toLocaleString() : '?';
    const fps = lastDrawStats.ms > 0 ? (1000 / lastDrawStats.ms).toFixed(0) : '-';
    renderStatsDiv.textContent =
        `${lastDrawStats.drawn.toLocaleString()} drawn${lastDrawStats.capped ? ' (moving)' : ''} · `
        + `${lastDrawStats.ms.toFixed(1)} ms (${fps}/s) · ${loaded}/${total} loaded`;
}

// Update cursor coordinate display.
// opts.above places the readout above the point so a finger doesn't cover it.
function updateCursorCoords(screenX, screenY, opts = {}) {
    if (!cursorCoordsDiv) return;
    
    const showRADec = showRADecCursor && showRADecCursor.checked;
    const showAzEl = showAzElCursor && showAzElCursor.checked;
    
    if (!showRADec && !showAzEl) {
        cursorCoordsDiv.style.display = 'none';
        return;
    }
    
    try {
        const coords = getCoordsAtScreen(screenX, screenY);
        
        let html = '';
        
        if (showRADec) {
            // Convert RA to hours:minutes:seconds
            const raHours = coords.raDeg / 15;
            const raH = Math.floor(raHours);
            const raM = Math.floor((raHours - raH) * 60);
            const raS = Math.floor(((raHours - raH) * 60 - raM) * 60);
            
            // Convert Dec to degrees:arcminutes:arcseconds
            const decSign = coords.decDeg >= 0 ? '+' : '-';
            const decAbs = Math.abs(coords.decDeg);
            const decD = Math.floor(decAbs);
            const decM = Math.floor((decAbs - decD) * 60);
            const decS = Math.floor(((decAbs - decD) * 60 - decM) * 60);
            
            html += `<div>RA: ${raH}h ${raM}m ${raS}s</div>`;
            html += `<div>DEC: ${decSign}${decD}° ${decM}' ${decS}"</div>`;
        }
        
        if (showAzEl) {
            html += `<div>Az: ${coords.azDeg.toFixed(2)}°</div>`;
            html += `<div>Elv: ${coords.altDeg.toFixed(2)}°</div>`;
        }
        
        cursorCoordsDiv.innerHTML = html;
        cursorCoordsDiv.style.display = 'block';
        
        // Position near the pointer with an offset to avoid blocking the view
        const offset = 15;
        const rect = cursorCoordsDiv.getBoundingClientRect();
        let left, top;
        if (opts.above) {
            // Touch: centre it well above the fingertip
            left = screenX - rect.width / 2;
            top = screenY - rect.height - 32;
            if (top < 4) top = screenY + 32;
        } else {
            left = screenX + offset;
            top = screenY + offset;
            if (left + rect.width > window.innerWidth) {
                left = screenX - rect.width - offset;
            }
            if (top + rect.height > window.innerHeight) {
                top = screenY - rect.height - offset;
            }
        }
        
        // Keep within bounds
        left = Math.max(4, Math.min(left, window.innerWidth - rect.width - 4));
        top = Math.max(4, Math.min(top, window.innerHeight - rect.height - 4));
        
        cursorCoordsDiv.style.left = left + 'px';
        cursorCoordsDiv.style.top = top + 'px';
        
    } catch (e) {
        console.error('Error calculating cursor coords:', e);
        cursorCoordsDiv.style.display = 'none';
    }
}

// Mouse controls
canvas.addEventListener('mousedown', e => {
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
});
// Rotate the sky by a drag delta in screen pixels (shared by mouse and touch)
function applyRotationDelta(dx, dy) {
    // Optionally invert controls: affects deltas only
    const controlInvert = invertControls ? -1 : 1;
    rotY += dx * 0.01 * controlInvert;
    rotX -= dy * 0.01 * controlInvert;
    rotX = Math.max(-Math.PI/2, Math.min(Math.PI/2, rotX));
}

window.addEventListener('mousemove', e => {
    // Track cursor position for coordinate display
    lastCursorX = e.clientX;
    lastCursorY = e.clientY;
    updateCursorCoords(e.clientX, e.clientY);
    
    if (!dragging) return;
    applyRotationDelta(e.clientX - lastX, e.clientY - lastY);
    lastX = e.clientX;
    lastY = e.clientY;
    beginInteraction();
    scheduleDraw();
});
window.addEventListener('mouseup', () => dragging = false);

// Mouse leave canvas - hide cursor coords
canvas.addEventListener('mouseleave', () => {
    lastCursorX = null;
    lastCursorY = null;
    if (cursorCoordsDiv) cursorCoordsDiv.style.display = 'none';
});

// ---------------------------------------------------------------------------
// Touch controls
//   one finger  -> drag to rotate the sky, quick tap to pick an object
//   two fingers -> pinch to zoom, drag the midpoint to rotate
// Every handler calls preventDefault so the browser neither scrolls/zooms the
// page nor replays the gesture as synthetic mouse/click events.
// ---------------------------------------------------------------------------
const TAP_MAX_MOVE_PX = 12;  // finger travel still counted as a tap
const TAP_MAX_MS = 400;      // and how long it may rest on the glass
const MIN_PINCH_DIST = 1e-3; // guard against divide-by-zero on a degenerate pinch

let touchPoints = [];        // current finger positions, from the last event
let lastTouchX = 0, lastTouchY = 0; // drag anchor (a finger, or the pinch midpoint)
let pinchStartDist = 0;
let pinchStartZoom = 1;
let tapCandidate = null;     // {x, y, t} while the gesture could still be a tap

function syncTouches(e) {
    touchPoints = Array.from(e.touches).map(t => ({ x: t.clientX, y: t.clientY }));
}

function touchMidpoint(points) {
    let sx = 0, sy = 0;
    for (const p of points) { sx += p.x; sy += p.y; }
    return { x: sx / points.length, y: sy / points.length };
}

function touchDistance(a, b) {
    return Math.max(Math.hypot(a.x - b.x, a.y - b.y), MIN_PINCH_DIST);
}

// Re-anchor the gesture to whatever fingers are still down, so removing or
// adding a finger mid-gesture doesn't make the sky jump.
function anchorTouchGesture() {
    if (touchPoints.length === 1) {
        lastTouchX = touchPoints[0].x;
        lastTouchY = touchPoints[0].y;
    } else if (touchPoints.length >= 2) {
        pinchStartDist = touchDistance(touchPoints[0], touchPoints[1]);
        pinchStartZoom = zoom;
        const mid = touchMidpoint(touchPoints);
        lastTouchX = mid.x;
        lastTouchY = mid.y;
    }
}

canvas.addEventListener('touchstart', (e) => {
    e.preventDefault();
    hideContextMenu();
    dragging = false; // mouse drag state never applies during a touch gesture
    syncTouches(e);
    if (touchPoints.length === 1) {
        tapCandidate = { x: touchPoints[0].x, y: touchPoints[0].y, t: Date.now() };
    } else {
        tapCandidate = null;
    }
    anchorTouchGesture();
}, { passive: false });

canvas.addEventListener('touchmove', (e) => {
    e.preventDefault();
    syncTouches(e);
    if (touchPoints.length === 0) return;

    if (touchPoints.length === 1) {
        const p = touchPoints[0];
        if (tapCandidate && Math.hypot(p.x - tapCandidate.x, p.y - tapCandidate.y) > TAP_MAX_MOVE_PX) {
            tapCandidate = null; // turned into a drag
        }
        applyRotationDelta(p.x - lastTouchX, p.y - lastTouchY);
        lastTouchX = p.x;
        lastTouchY = p.y;
        updateCursorCoords(p.x, p.y, { above: true });
        beginInteraction();
        scheduleDraw();
        return;
    }

    // Two or more fingers: pinch zoom plus rotation from the midpoint
    tapCandidate = null;
    const dist = touchDistance(touchPoints[0], touchPoints[1]);
    const mid = touchMidpoint(touchPoints);
    const oldZoom = zoom;
    zoom = Math.max(minZoom, Math.min(maxZoom, pinchStartZoom * (dist / pinchStartDist)));
    if (magnitudeZoomEnabled && zoom !== oldZoom) {
        updateMagnitudeForZoom();
    }
    applyRotationDelta(mid.x - lastTouchX, mid.y - lastTouchY);
    lastTouchX = mid.x;
    lastTouchY = mid.y;
    beginInteraction();
    scheduleDraw();
}, { passive: false });

function handleTouchEnd(e) {
    e.preventDefault();
    const tap = (e.type === 'touchend' && tapCandidate && (Date.now() - tapCandidate.t) <= TAP_MAX_MS)
        ? tapCandidate
        : null;
    syncTouches(e);

    if (touchPoints.length === 0) {
        tapCandidate = null;
        if (tap) {
            selectObjectAt(tap.x, tap.y, { touch: true });
            updateCursorCoords(tap.x, tap.y, { above: true });
        }
        return;
    }

    // Fingers left over (e.g. a pinch relaxing into a one-finger drag)
    tapCandidate = null;
    anchorTouchGesture();
}

canvas.addEventListener('touchend', handleTouchEnd, { passive: false });
canvas.addEventListener('touchcancel', handleTouchEnd, { passive: false });

// iOS Safari still raises its own pinch/double-tap page zoom over the canvas
for (const gestureEvent of ['gesturestart', 'gesturechange', 'gestureend']) {
    canvas.addEventListener(gestureEvent, (e) => e.preventDefault(), { passive: false });
}

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
    const displayName = (data && data.friendlyName)
        ? `${data.name} (${data.friendlyName})`
        : ((data && data.name) ? data.name : obj.name);
    const btnPadding = isTouchDevice ? '10px 8px' : '4px 8px';

    // Store the full data for the advanced info modal with additional context
    window.currentStarData = data
        ? { ...data, ra: obj.ra, dec: obj.dec, hourAngle: ha }
        : { name: obj.name, ra: obj.ra, dec: obj.dec, mag: obj.mag, hourAngle: ha };

    document.getElementById('info').innerHTML =
        `<b>${displayName}</b><br>RA: ${raHMS}<br>DEC: ${decDMS}<br>V-Mag: ${displayMagFormatted}<br>
         <div style="margin-top: 5px; display: flex; gap: 4px;">
            <button onclick="trackObject('${obj.name}', ${obj.ra}, ${obj.dec}, ${obj.mag})" style="padding: ${btnPadding}; background: #4CAF50; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">Track</button>
            <button onclick="showStarInfoModal(window.currentStarData)" style="padding: ${btnPadding}; background: #007bff; color: white; border: none; border-radius: 3px; cursor: pointer; flex: 1;">Advanced Info</button>
         </div>`;
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

// Click to show info
canvas.addEventListener('click', function(e) {
    selectObjectAt(e.clientX, e.clientY);
});

// Coordinate conversion helpers
function decimalToHMS(degrees) {
    // Convert RA degrees to hours:minutes:seconds
    const hours = degrees / 15;
    const h = Math.floor(hours);
    const m = Math.floor((hours - h) * 60);
    const s = ((hours - h) * 60 - m) * 60;
    return `${h}h ${m}m ${s.toFixed(2)}s`;
}

function decimalToDMS(degrees) {
    // Convert DEC degrees to degrees:arcminutes:arcseconds
    const sign = degrees >= 0 ? '+' : '-';
    const abs = Math.abs(degrees);
    const d = Math.floor(abs);
    const m = Math.floor((abs - d) * 60);
    const s = ((abs - d) * 60 - m) * 60;
    return `${sign}${d}° ${m}' ${s.toFixed(2)}"`;
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

// Responsive resize (also covers phone rotation and browser chrome sliding away)
let resizeFrame = null;
function handleViewportResize() {
    if (resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
        resizeFrame = null;
        document.documentElement.classList.toggle('sm-compact', isCompactLayout());
        resizeCanvas();
        draw();
    });
}
window.addEventListener('resize', handleViewportResize);
window.addEventListener('orientationchange', () => {
    handleViewportResize();
    // Some mobile browsers report the old size until after the rotation settles
    setTimeout(handleViewportResize, 300);
});

// Scroll wheel zoom
canvas.addEventListener('wheel', (e) => {
    e.preventDefault(); // Prevent page scroll
    // Keep zoom behavior consistent (checkbox only inverts drag/pan)
    const scrollDirection = e.deltaY > 0 ? -1 : 1; // positive deltaY => zoom out
    const zoomChange = scrollDirection * zoomStep;

    const oldZoom = zoom;
    zoom = Math.max(minZoom, Math.min(maxZoom, zoom + zoomChange));
    
    // Update magnitude based on zoom level if enabled
    if (magnitudeZoomEnabled && zoom !== oldZoom) {
        updateMagnitudeForZoom();
    }
    
    beginInteraction();
    scheduleDraw();
}, { passive: false });

// Preload planet icons with proper error handling
function preloadPlanetImages() {
    const loadPromises = [];
    console.log('Starting to preload planet images...');
    
    for (const obj of stars) {
        if (obj.type === "planet" && obj.icon && !planetImages[obj.icon]) {
            console.log(`Loading planet icon for ${obj.name}: ${obj.icon}`);
            const loadPromise = new Promise((resolve, reject) => {
                const img = new Image();
                img.onload = () => {
                    planetImages[obj.icon] = img;
                    console.log(`Successfully loaded icon for ${obj.name}`);
                    resolve();
                };
                img.onerror = () => {
                    console.warn(`Failed to load planet icon: ${obj.icon} for ${obj.name}`);
                    resolve(); // Continue even if image fails to load
                };
                img.src = obj.icon;
            });
            loadPromises.push(loadPromise);
        }
    }
    
    console.log(`Loading ${loadPromises.length} planet images...`);
    return Promise.all(loadPromises).then(() => {
        console.log('All planet images loaded. Cache contains:', Object.keys(planetImages));
    });
}

// ---------------------------------------------------------------------------
// Catalogue loading
//
// /api/stars_bands hands back disjoint index ranges, brightest first, and
// /api/stars_bin serves each one as a compact binary block. Nothing is ever
// downloaded twice, the first band paints within a few hundred KB, and the
// responses are cacheable so a repeat visit costs one 304 per band.
// ---------------------------------------------------------------------------
let catalogueBands = [];
let catalogueLoadStarted = false;
let cataloguePendingBand = 0;

async function fetchStarBand(band) {
    const url = new URL('/api/stars_bin', window.location.origin);
    url.searchParams.set('start', String(band.start));
    url.searchParams.set('end', String(band.end));
    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const buffer = await res.arrayBuffer();
    const decoded = decodeStarBand(buffer);
    return new Promise(resolve => ingestStarBand(band.start, decoded, resolve));
}

// Load the catalogue: the first (brightest) band is awaited so the sky appears
// straight away, the rest stream in behind it.
async function loadStarCatalogue() {
    if (catalogueLoadStarted) return;
    catalogueLoadStarted = true;
    starLoadingBegin();
    try {
        const res = await fetch('/api/stars_bands');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const meta = await res.json();
        catalogueBands = Array.isArray(meta.bands) ? meta.bands : [];
        starKnownCount = meta.known || 0;
        allocateStarCatalogue(meta.total || 0);

        // The slider now covers the real magnitude range of the catalogue
        if (typeof meta.minMag === 'number' && typeof meta.maxMag === 'number') {
            minMag = meta.minMag;
            maxMag = meta.maxMag;
            updateMagSliderRange(meta.minMag, meta.maxMag);
        }
        if (unknownMagCountLabel && meta.unknown) {
            unknownMagCountLabel.textContent = ` (${meta.unknown.toLocaleString()})`;
        }

        if (catalogueBands.length > 0) {
            await fetchStarBand(catalogueBands[0]);
            cataloguePendingBand = 1;
            scheduleDraw();
        }
    } catch (e) {
        console.error('Star catalogue index fetch failed:', e);
        catalogueLoadStarted = false;
    } finally {
        starLoadingEnd();
    }
}

// Pull in the remaining bands one at a time, yielding between each so the map
// stays interactive while several megabytes arrive.
async function loadRemainingStarBands() {
    if (cataloguePendingBand <= 0 || cataloguePendingBand >= catalogueBands.length) return;
    starLoadingBegin();
    try {
        for (; cataloguePendingBand < catalogueBands.length; cataloguePendingBand++) {
            const band = catalogueBands[cataloguePendingBand];
            updateStarLoadingProgress(band);
            try {
                await fetchStarBand(band);
            } catch (e) {
                console.warn(`Star band ${band.start}-${band.end} failed:`, e);
            }
            await new Promise(resolve => setTimeout(resolve, 0));
        }
        console.log(`Star catalogue loaded: ${starLoadedCount.toLocaleString()} objects`);
    } finally {
        updateStarLoadingProgress(null);
        starLoadingEnd();
    }
}

function updateStarLoadingProgress(band) {
    if (!starLoadingLabel) return;
    if (!band || !starCount) {
        starLoadingLabel.textContent = 'Loading stars…';
        return;
    }
    const pct = Math.min(100, Math.round((band.end / starCount) * 100));
    starLoadingLabel.textContent = `Loading stars… ${pct}%`;
}

// Kept for callers that ask for a deeper magnitude than is loaded yet: with
// band loading there is nothing extra to fetch, the data is already on its way.
async function fetchMoreStarsIfNeeded(newMagLimit) {
    if (!isFinite(newMagLimit)) return;
    if (cataloguePendingBand > 0 && cataloguePendingBand < catalogueBands.length) {
        loadRemainingStarBands();
    }
}

// After the first paint, stream in the rest of the catalogue
async function stagedPrefetchAfterFirstDraw() {
    try {
        await new Promise(requestAnimationFrame);
        await loadRemainingStarBands();
    } catch (e) {
        console.warn('Staged prefetch encountered an issue:', e);
    }
}

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
if (showUnknownMag) {
    showUnknownMag.addEventListener('change', () => {
        // These sit at the end of the catalogue, so make sure the tail is on its way
        if (showUnknownMag.checked) loadRemainingStarBands();
        scheduleDraw();
    });
}
if (showRenderStats) showRenderStats.addEventListener('change', updateRenderStats);
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