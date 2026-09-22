// Star map - astronomy and projection maths: sidereal time, RA/Dec <-> Alt/Az,
// view rotations, the stereographic projection and coordinate formatting.

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
