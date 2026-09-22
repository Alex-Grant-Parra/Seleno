// Star map - pointer input: mouse drag, touch gestures, wheel zoom, cursor
// coordinate readout and viewport resizing.

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
// Rotate the sky by a drag delta in screen pixels (shared by mouse and touch).
// Mouse keeps its classic zoom-1 speed, slowed in proportion to the zoom.
// Touch tracks the finger: one pixel of drag moves the sky by one pixel at
// the view centre, where the projection gives max(w,h) * 0.35 * zoom px/rad.
function applyRotationDelta(dx, dy, touch = false) {
    const radPerPx = touch
        ? 1 / (Math.max(width, height) * 0.35 * zoom)
        : 0.01 / zoom;
    // Optionally invert controls: affects deltas only
    const controlInvert = invertControls ? -1 : 1;
    rotY += dx * radPerPx * controlInvert;
    rotX -= dy * radPerPx * controlInvert;
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
        applyRotationDelta(p.x - lastTouchX, p.y - lastTouchY, true);
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
    applyRotationDelta(mid.x - lastTouchX, mid.y - lastTouchY, true);
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

// Click to show info
canvas.addEventListener('click', function(e) {
    selectObjectAt(e.clientX, e.clientY);
});

// Responsive resize (also covers phone rotation and browser chrome sliding away)
let resizeFrame = null;
function handleViewportResize() {
    if (resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
        resizeFrame = null;
        document.documentElement.classList.toggle('sm-compact', isCompactLayout());
        syncMagnitudeZoomRatioInput();
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
