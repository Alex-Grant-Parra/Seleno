// Star map - everything drawn besides stars and planets: star labels,
// constellation figures, the ecliptic, coordinate grids and the horizon tint.

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
// Figures live in the database (ConstellationsTable / ConstellationLinesTable,
// imported by scripts/import_constellations.py from d3-celestial, BSD-3-Clause).
// Each line joins two catalogue stars, so the figures land exactly on the
// drawn stars. Loaded on first use, not on page load.
// ===========================================================================
let constellationFigures = null;   // [{ id, name, anchor:{v}, paths:[Float64Array-ish] }]
let constellationLoadPromise = null;

function loadConstellations() {
    if (constellationFigures) return Promise.resolve(constellationFigures);
    if (constellationLoadPromise) return constellationLoadPromise;

    constellationLoadPromise = fetch('/api/constellations')
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
    const opacity = horizonTintOpacityInput ? parseFloat(horizonTintOpacityInput.value) : 0.10;
    const tintOpacity = Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : 0.10;
    ctx.fillStyle = `rgba(50, 205, 50, ${tintOpacity})`;
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
