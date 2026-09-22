// Star map - frame scheduling, the star draw loop and the main draw().

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

// Magnitude -> radius lookup, rebuilt only when the zoom or magnitude limit
// changes. Stars at the limit start as a 1px dot and ease up to their full
// size over STAR_FADE_MAGS, so newly revealed stars grow in gradually.
const SIZE_LUT_MIN = -2, SIZE_LUT_MAX = 22, SIZE_LUT_STEP = 0.05;
const SIZE_LUT_LENGTH = Math.ceil((SIZE_LUT_MAX - SIZE_LUT_MIN) / SIZE_LUT_STEP) + 1;
const starSizeLUT = new Float32Array(SIZE_LUT_LENGTH);
const STAR_DOT_RADIUS = 0.5;
const STAR_FADE_MAGS = 1.5;
let sizeLUTZoom = -1, sizeLUTMagLimit = NaN;

function refreshStarSizeLUT() {
    const magLimit = parseFloat(magFilter.value);
    if (sizeLUTZoom === zoom && sizeLUTMagLimit === magLimit) return;
    sizeLUTZoom = zoom;
    sizeLUTMagLimit = magLimit;
    for (let i = 0; i < SIZE_LUT_LENGTH; i++) {
        const mag = SIZE_LUT_MIN + i * SIZE_LUT_STEP;
        const full = getZoomedStarSize(getMagnitudeBasedSize(mag));
        let t = (magLimit - mag) / STAR_FADE_MAGS;
        t = t <= 0 ? 0 : (t >= 1 ? 1 : t * t * (3 - 2 * t)); // smoothstep
        starSizeLUT[i] = STAR_DOT_RADIUS + (Math.max(full, STAR_DOT_RADIUS) - STAR_DOT_RADIUS) * t;
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
        if (size <= 0.6) {
            // Faintest stars: a single pixel, several times cheaper than an arc
            ctx.fillRect(px | 0, py | 0, 1, 1);
        } else if (size < 1.2) {
            // Growing dots: an unsnapped square with the same area as the
            // circle, so the size eases smoothly into the arc below
            const side = size * 1.7725;
            ctx.fillRect(px - side / 2, py - side / 2, side, side);
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
        // Project the reported RA/Dec through the same transform as the stars,
        // every frame, so the marker turns with the sky between position polls
        // instead of holding a stale Alt/Az and jumping when the next one lands
        const v = radecToXYZ(telescopePosition.ra, telescopePosition.dec);
        const [x, y, z] = mulMat3Vec3(Mview, v);
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
