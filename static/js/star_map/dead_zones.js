// Star map - the selected telescope's dead zones: parts of the sky it never
// points at (it would hit its own mount, or look into somebody's window).
//
// The telescope reports them in its status push as altitude/azimuth boxes at
// its own site (app/dead_zones.py). They are fixed to the telescope's ground,
// so when the map is set to a different place they are carried across through
// RA/Dec at the map's time, and drift across that sky as it turns.

let deadZones = [];            // [{ name, az: [from, to], alt: [lo, hi], reason, locked }]
let deadZoneSite = null;       // { lat, lon } of the telescope, or null (assume the map's place)
let deadZonesTimer = null;
let deadZonesInFlight = false;
const DEAD_ZONE_REFRESH_MS = 60000;
const DEAD_ZONE_STEP_DEG = 3;  // cell size for the tint

const showDeadZones = document.getElementById('show-dead-zones');

const DEAD_ZONE_STYLES = {
    physical: { fill: 'rgba(255, 70, 70, 0.20)', edge: 'rgba(255, 90, 90, 0.85)', label: 'rgba(255, 150, 150, 0.95)' },
    privacy: { fill: 'rgba(200, 80, 255, 0.20)', edge: 'rgba(210, 120, 255, 0.85)', label: 'rgba(225, 175, 255, 0.95)' },
    other: { fill: 'rgba(255, 140, 40, 0.20)', edge: 'rgba(255, 160, 70, 0.85)', label: 'rgba(255, 200, 140, 0.95)' },
    draft: { fill: 'rgba(255, 255, 255, 0.14)', edge: 'rgba(255, 255, 255, 0.95)', label: 'rgba(255, 255, 255, 0.95)' },
};

function deadZoneLabel(zone) {
    const reason = { physical: _('Physical limit'), privacy: _('Privacy'), other: _('No-go') }[zone.reason] || _('No-go');
    return zone.name ? `${zone.name} · ${reason}` : reason;
}

function refreshDeadZones() {
    if (!isTelescopeSelected() || document.hidden || deadZonesInFlight) return;
    deadZonesInFlight = true;
    fetch('/api/telescope_dead_zones')
        .then(response => (response.ok ? response.json() : null))
        .then(data => {
            if (!data || data.status !== 'success') {
                deadZones = [];
                return;
            }
            deadZones = Array.isArray(data.zones) ? data.zones : [];
            deadZoneSite = data.site || null;
            scheduleDraw();
        })
        .catch(() => {})
        .finally(() => { deadZonesInFlight = false; });
}

function startDeadZoneUpdates() {
    if (window.ZONE_EDITOR) return;  // the editor (zone_editor.js) owns the zones on its page
    if (!isTelescopeSelected()) return;
    refreshDeadZones();
    if (deadZonesTimer) clearInterval(deadZonesTimer);
    deadZonesTimer = setInterval(refreshDeadZones, DEAD_ZONE_REFRESH_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshDeadZones(); });
}

// A function taking the telescope's (alt, az) to the map's (alt, az) at this
// moment: the identity when the map is at the telescope's site.
function deadZoneMapper(mapLat, mapLon, when) {
    const site = deadZoneSite;
    if (!site || (Math.abs(site.lat - mapLat) < 0.05 && Math.abs(site.lon - mapLon) < 0.05)) {
        return (alt, az) => [alt, az];
    }
    const D = Math.PI / 180;
    const sinT = Math.sin(site.lat * D), cosT = Math.cos(site.lat * D);
    const lstT = lstDegrees(when, site.lon);
    const lstMap = lstDegrees(when, mapLon);
    const { sinLat, cosLat } = precomputeObserver(mapLat);
    return (alt, az) => {
        const a = alt * D, z = az * D;
        const sinDec = Math.sin(a) * sinT + Math.cos(a) * cosT * Math.cos(z);
        const dec = Math.asin(Math.max(-1, Math.min(1, sinDec)));
        const ha = Math.atan2(-Math.cos(a) * Math.sin(z), Math.sin(a) * cosT - Math.cos(a) * sinT * Math.cos(z));
        const ra = lstT - ha / D;
        const { altDeg, azDeg } = radecToAltAzFast(ra, dec / D, lstMap, sinLat, cosLat);
        return [altDeg, azDeg];
    };
}

function deadZoneWidth(zone) {
    const [from, to] = zone.az;
    if (to - from >= 360) return 360;
    return (((to - from) % 360) + 360) % 360 || 360;
}

// View-space point for a telescope (alt, az), or null when behind the viewer
function deadZonePoint(toMap, alt, az) {
    const [mAlt, mAz] = toMap(alt, az);
    const v = horizonToView(altazToXYZ(mAlt, mAz));
    return v[2] > 0 ? v : null;
}

function drawDeadZones() {
    if (showDeadZones && !showDeadZones.checked) return;
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    let when = new Date();
    try { if (timeControl && timeControl.value) when = new Date(timeControl.value); } catch {}
    const toMap = deadZoneMapper(latDeg, lonDeg, when);
    // The editor adds the zone being drawn and marks the selected one
    const list = typeof zoneEditorDrawList === 'function' ? zoneEditorDrawList() : deadZones;

    ctx.save();
    for (const zone of list) {
        const style = zone._draft ? DEAD_ZONE_STYLES.draft : (DEAD_ZONE_STYLES[zone.reason] || DEAD_ZONE_STYLES.other);
        const azFrom = Number(zone.az[0]);
        const span = deadZoneWidth(zone);
        const lo = Math.max(-90, Number(zone.alt[0]));
        const hi = Math.min(90, Number(zone.alt[1]));
        const nAz = Math.max(1, Math.ceil(span / DEAD_ZONE_STEP_DEG));
        const nAlt = Math.max(1, Math.ceil((hi - lo) / DEAD_ZONE_STEP_DEG));

        // A grid of corners, each projected once
        const grid = [];
        for (let i = 0; i <= nAlt; i++) {
            const alt = lo + (hi - lo) * i / nAlt;
            const row = [];
            for (let j = 0; j <= nAz; j++) {
                const v = deadZonePoint(toMap, alt, azFrom + span * j / nAz);
                row.push(v ? project(v) : null);
            }
            grid.push(row);
        }

        // Tint: every cell in one path, filled once, so the cells don't stack up
        // into darker seams. Cells partly behind the viewer are left out.
        ctx.beginPath();
        for (let i = 0; i < nAlt; i++) {
            for (let j = 0; j < nAz; j++) {
                const a = grid[i][j], b = grid[i][j + 1], c = grid[i + 1][j + 1], d = grid[i + 1][j];
                if (!a || !b || !c || !d) continue;
                ctx.moveTo(a[0], a[1]);
                ctx.lineTo(b[0], b[1]);
                ctx.lineTo(c[0], c[1]);
                ctx.lineTo(d[0], d[1]);
                ctx.closePath();
            }
        }
        ctx.fillStyle = style.fill;
        ctx.fill('nonzero');

        // Edge: the box's four sides (not the sides of an all-round band)
        ctx.strokeStyle = style.edge;
        ctx.lineWidth = zone._selected || zone._draft ? 3 : 1.5;
        ctx.setLineDash(zone._selected ? [] : [6, 4]);
        ctx.beginPath();
        const edges = [grid[0], grid[nAlt]];
        if (span < 360) {
            edges.push(grid.map(row => row[0]), grid.map(row => row[nAz]));
        }
        for (const edge of edges) {
            let started = false;
            for (const p of edge) {
                if (!p) { started = false; continue; }
                if (started) ctx.lineTo(p[0], p[1]);
                else { ctx.moveTo(p[0], p[1]); started = true; }
            }
        }
        ctx.stroke();
        ctx.setLineDash([]);

        // Label in the middle of the part above the horizon, if there is one
        const labelAlt = hi > 0 ? (Math.max(lo, 0) + hi) / 2 : (lo + hi) / 2;
        const v = deadZonePoint(toMap, labelAlt, azFrom + span / 2);
        if (v) {
            const [cx, cy] = project(v);
            if (cx >= 0 && cx <= width && cy >= 0 && cy <= height) {
                ctx.font = '12px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = style.label;
                const text = zone._draft
                    ? _('Az {from}° to {to}°, alt {lo}° to {hi}°', { from: zone.az[0], to: zone.az[1], lo: zone.alt[0], hi: zone.alt[1] })
                    : deadZoneLabel(zone) + (zone.locked ? ' · ' + _('set on the telescope') : '');
                ctx.fillText(text, cx, cy);
            }
        }
    }
    ctx.restore();
}

// The zone a map direction falls in, for the object panel and go-to warnings.
// alt/az are in the map's frame.
function deadZoneAtMapAltAz(mapAlt, mapAz) {
    if (!deadZones.length) return null;
    const latDeg = parseFloat(latInput.value) || 0;
    const lonDeg = parseFloat(lonInput.value) || 0;
    const site = deadZoneSite;
    let alt = mapAlt, az = mapAz;
    if (site && (Math.abs(site.lat - latDeg) >= 0.05 || Math.abs(site.lon - lonDeg) >= 0.05)) {
        // Back from the map's place to the telescope's
        let when = new Date();
        try { if (timeControl && timeControl.value) when = new Date(timeControl.value); } catch {}
        const { raDeg, decDeg } = altazToRaDec(mapAlt, mapAz, lstDegrees(when, lonDeg), latDeg);
        const { sinLat, cosLat } = precomputeObserver(site.lat);
        const there = radecToAltAzFast(raDeg, decDeg, lstDegrees(when, site.lon), sinLat, cosLat);
        alt = there.altDeg; az = there.azDeg;
    }
    az = ((az % 360) + 360) % 360;
    for (const zone of deadZones) {
        if (alt < zone.alt[0] || alt > zone.alt[1]) continue;
        const w = deadZoneWidth(zone);
        if (w >= 360 || (((az - zone.az[0]) % 360) + 360) % 360 <= w) return zone;
    }
    return null;
}
