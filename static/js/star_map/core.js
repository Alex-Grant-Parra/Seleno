// Star map - shared state and setup.
//
// The star map is split into plain (non-module) scripts that share one global
// scope and are loaded in order by templates/star_map.html. This one comes
// first: DOM handles, canvas sizing, view/zoom state and magnitude helpers.

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
const horizonTintOpacityInput = document.getElementById('horizon-tint-opacity');
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
const magnitudeZoomRatioInput = document.getElementById('magnitude-zoom-ratio');

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
const baseMagnitude = 4.0; // Magnitude limit at zoom level 1.0
// Magnitude added each time the zoom doubles. The limit follows
// log2(zoom), so it rises quickly at first and flattens out when zoomed far in:
//   zoom      1    2    3    4    5    6.7
//   2.0  ->  4.0  6.0  7.2  8.0  8.6  9.5
//   1.25 ->  4.0  5.3  6.0  6.5  6.9  7.4
// Raise it for more stars at every zoom, lower it for fewer.
// Small screens get their own value: the same sky is squeezed into far fewer
// pixels there, so the same limit looks several times as crowded.
let magnitudePerZoomDoubling = 2;             // desktop
let magnitudePerZoomDoublingCompact = 1.25;   // phones / narrow windows (see isCompactLayout)

function getMagnitudeZoomRatio() {
    return isCompactLayout() ? magnitudePerZoomDoublingCompact : magnitudePerZoomDoubling;
}

function syncMagnitudeZoomRatioInput() {
    if (magnitudeZoomRatioInput) {
        magnitudeZoomRatioInput.value = getMagnitudeZoomRatio().toFixed(2);
    }
}

function updateMagnitudeZoomRatio() {
    if (!magnitudeZoomRatioInput) return;
    const value = parseFloat(magnitudeZoomRatioInput.value);
    if (!Number.isFinite(value) || value < 0) return;
    const ratio = Math.min(20, value);
    if (isCompactLayout()) magnitudePerZoomDoublingCompact = ratio;
    else magnitudePerZoomDoubling = ratio;
    syncMagnitudeZoomRatioInput();
    if (magnitudeZoomEnabled) updateMagnitudeForZoom();
}

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

// Update magnitude slider based on zoom level
function updateMagnitudeForZoom() {
    if (!magnitudeZoomEnabled) return;
    
    // Calculate new magnitude based on zoom level
    // Higher zoom = fainter stars visible (higher magnitude)
    const perDoubling = getMagnitudeZoomRatio();
    const newMagnitude = baseMagnitude + Math.log2(zoom) * perDoubling;
    
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
