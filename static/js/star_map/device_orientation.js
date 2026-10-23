// Star map - phone orientation mode: hold the phone up and the map looks
// wherever the back of the phone points, plus star alignment through the back
// camera for accuracy the compass alone can't give.
//
// Frames used in this file
//   device   W3C device frame: x to the right of the screen, y towards the top
//            of the phone, z out of the screen. The back camera looks down -z.
//   ENU      x east, y north, z up. The browser's Euler angles rotate device
//            vectors into this frame (with an arbitrary heading when relative).
//   horizon  the map's own frame: x east, y up, z north (see astronomy.js).
//
// Pipeline: the browser's gyro-stabilised orientation is smoothed, then turned
// to true north by a heading. That heading comes from the compass plus
// magnetic declination until the user aligns on a real object; alignment then
// replaces it. Compasses are typically 5-15 degrees out, alignment gets well
// under one. The result is handed to the renderer as viewMatrixOverride.

// --- Smoothing -------------------------------------------------------------
// Adaptive: heavy while the phone is held still (kills hand tremor), almost
// none while it is swept (no lag). Time constants in seconds, speeds in rad/s.
const ORIENT_TAU_STILL = 0.15;
const ORIENT_TAU_MOVING = 0.015;
const ORIENT_SPEED_STILL = 0.05;   // ~3 deg/s
const ORIENT_SPEED_MOVING = 1.0;   // ~57 deg/s
const ORIENT_SNAP_ANGLE = 0.35;    // jumps over ~20 deg are taken at once
const ORIENT_COMPASS_TAU = 1.5;    // compass heading filter
const ORIENT_REDRAW_ANGLE = 0.0003;      // ~0.02 deg: smaller changes skip the redraw
const ORIENT_INTERACTION_ANGLE = 0.004;  // ~0.2 deg: bigger ones draw at moving quality
const ORIENT_GAP_MS = 3000;        // a silence this long may mean the sensor frame was reset
const ORIENT_HISTORY_MS = 1500;

// --- Alignment -------------------------------------------------------------
const ALIGN_MIN_ALT = 20;          // below this, refraction and obstructions get in the way
const ALIGN_MAX_ALT = 75;          // above this, a single object can't fix the heading well
const ALIGN_STAR_MAG = 1.6;
const ALIGN_PLANET_MAG = 2.5;
const ALIGN_MIN_SEPARATION = 15 * DEG2RAD; // two objects closer than this can't fix all three axes
const ALIGN_STEADY_ANGLE = 1.0 * DEG2RAD;  // phone movement allowed while locking
const MOON_HORIZONTAL_PARALLAX_DEG = 0.9507;
const ALIGN_STORAGE_KEY = 'starMapAlignment';
const ALIGN_STORAGE_MAX_AGE_MS = 12 * 3600 * 1000;
const ALIGN_REANCHOR_SETTLE_MS = 2000;     // compass settling time before re-anchoring
const ALIGN_SAME_FRAME_DEG = 3;            // smaller compass shifts are noise, not a new frame

// --- State -----------------------------------------------------------------
let orientActive = false;          // the sensors are driving the view
let orientListening = false;       // listeners stay attached once granted, so the sensor frame (and any alignment) survives pausing
let orientSource = null;           // 'relative' (gyro, arbitrary heading) or 'absolute' (compass-referenced)
let orientRawQ = null;             // latest device quaternion from the driving source
let orientSmoothQ = null;
let orientLastDriverTime = 0;
let orientSpeed = 0;               // smoothed angular speed, rad/s
let orientLastRelTime = -Infinity;
let orientLastRel = null;          // { q, t } for pairing with absolute samples
let orientHistory = [];            // recent { t, q } for alignment capture
let orientCompassOffset = null;    // driver frame -> magnetic ENU, radians about up
let orientCompassVec = null;       // [cos, sin] filter state for the offset
let orientCompassTime = 0;
let orientCompassAccuracy = null;  // degrees, iOS only
let orientHasIOSCompass = false;   // webkitCompassHeading seen: north comes from holding the phone flat
let orientDeclinationRad = 0;      // east positive
let orientDeclinationKey = '';
let orientAlignment = null;        // { A: driver -> true ENU, pairs, residualDeg }
let orientAlignPairs = [];
let orientPendingAlign = null;     // alignment from an earlier sensor frame, waiting for the compass to carry it over
let orientCompassSince = 0;        // first compass sample in the current sensor frame
let orientFirstFrame = true;
let orientWatchdog = null;
let orientWakeLock = null;
let orientBlend = null;            // { fromQ, start } while roll eases out after leaving

const orientToggleBtn = document.getElementById('orient-toggle');
const orientBar = document.getElementById('orient-bar');
const orientStatus = document.getElementById('orient-status');
const orientAlignBtn = document.getElementById('orient-align-btn');
const orientUnalignBtn = document.getElementById('orient-unalign-btn');
const alignPicker = document.getElementById('align-picker');
const alignCandidatesList = document.getElementById('align-candidates');
const alignPickerCancel = document.getElementById('align-picker-cancel');
const alignCamera = document.getElementById('align-camera');
const alignVideo = document.getElementById('align-video');
const alignHint = document.getElementById('align-hint');
const alignLockBtn = document.getElementById('align-lock');
const alignCancelBtn = document.getElementById('align-cancel');

function orientationModeActive() {
    return orientActive;
}

// ===========================================================================
// Quaternions ([w, x, y, z]) and small vector helpers
// ===========================================================================
function quatMultiply(a, b) {
    return [
        a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
        a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
        a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
        a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0]
    ];
}

function quatNormalize(q) {
    const n = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
    return [q[0] / n, q[1] / n, q[2] / n, q[3] / n];
}

function quatDot(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
}

// Rotation angle between two orientations, radians
function quatAngle(a, b) {
    return 2 * Math.acos(Math.min(1, Math.abs(quatDot(a, b))));
}

function quatSlerp(a, b, t) {
    let d = quatDot(a, b);
    if (d < 0) { b = [-b[0], -b[1], -b[2], -b[3]]; d = -d; }
    if (d > 0.9995) {
        return quatNormalize([
            a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t
        ]);
    }
    const theta = Math.acos(d);
    const s = Math.sin(theta);
    const wa = Math.sin((1 - t) * theta) / s, wb = Math.sin(t * theta) / s;
    return [wa * a[0] + wb * b[0], wa * a[1] + wb * b[1], wa * a[2] + wb * b[2], wa * a[3] + wb * b[3]];
}

function quatAboutZ(angle) {
    return [Math.cos(angle / 2), 0, 0, Math.sin(angle / 2)];
}

// W3C DeviceOrientation angles (degrees) are intrinsic Z-X'-Y'':
// R = Rz(alpha) Rx(beta) Ry(gamma), taking device vectors into the earth frame
function quatFromDeviceEuler(alphaDeg, betaDeg, gammaDeg) {
    const a = alphaDeg * DEG2RAD / 2, b = betaDeg * DEG2RAD / 2, g = gammaDeg * DEG2RAD / 2;
    const qz = [Math.cos(a), 0, 0, Math.sin(a)];
    const qx = [Math.cos(b), Math.sin(b), 0, 0];
    const qy = [Math.cos(g), 0, Math.sin(g), 0];
    return quatMultiply(quatMultiply(qz, qx), qy);
}

function quatToMatrix(q) {
    const [w, x, y, z] = q;
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z),     2 * (x * z + w * y)],
        [2 * (x * y + w * z),     1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y),     2 * (y * z + w * x),     1 - 2 * (x * x + y * y)]
    ];
}

function matrixToQuat(m) {
    const trace = m[0][0] + m[1][1] + m[2][2];
    let q;
    if (trace > 0) {
        const s = 2 * Math.sqrt(trace + 1);
        q = [s / 4, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s];
    } else if (m[0][0] > m[1][1] && m[0][0] > m[2][2]) {
        const s = 2 * Math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]);
        q = [(m[2][1] - m[1][2]) / s, s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s];
    } else if (m[1][1] > m[2][2]) {
        const s = 2 * Math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]);
        q = [(m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s];
    } else {
        const s = 2 * Math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]);
        q = [(m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4];
    }
    return quatNormalize(q);
}

// Rotation angle between two rotation matrices, radians
function matrixAngle(A, B) {
    let trace = 0;
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) trace += A[i][j] * B[i][j];
    return Math.acos(Math.max(-1, Math.min(1, (trace - 1) / 2)));
}

// Rotation about the ENU up axis, turning east towards north
function enuYawMatrix(angle) {
    const c = Math.cos(angle), s = Math.sin(angle);
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]];
}

function vecNormalize(v) {
    const n = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / n, v[1] / n, v[2] / n];
}

function vecCross(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function vecAngle(a, b) {
    const d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    return Math.acos(Math.max(-1, Math.min(1, d)));
}

// Compass bearing of an ENU vector, radians clockwise from north
function enuHeading(v) {
    return Math.atan2(v[0], v[1]);
}

function wrapAngle(a) {
    return Math.atan2(Math.sin(a), Math.cos(a));
}

// ===========================================================================
// Sensor input
// ===========================================================================
function screenAngleRad() {
    let deg = 0;
    if (screen.orientation && typeof screen.orientation.angle === 'number') deg = screen.orientation.angle;
    else if (typeof window.orientation === 'number') deg = window.orientation; // older iOS
    return deg * DEG2RAD;
}

function handleOrientationEvent(e) {
    if (e.alpha == null || e.beta == null || e.gamma == null) return;
    const now = performance.now();
    const q = quatFromDeviceEuler(e.alpha, e.beta, e.gamma);
    const absolute = e.type === 'deviceorientationabsolute' || e.absolute === true;

    if (!absolute) {
        orientLastRelTime = now;
        orientLastRel = { q, t: now };
        ingestDriverSample(q, now, 'relative');
        // iOS: the relative frame has an arbitrary heading, the compass comes separately
        if (typeof e.webkitCompassHeading === 'number') {
            orientHasIOSCompass = true;
            sampleIOSCompass(q, e.webkitCompassHeading, e.webkitCompassAccuracy, now);
        }
    } else if (now - orientLastRelTime > 1000) {
        // Only a compass-referenced source: drive the view from it directly
        ingestDriverSample(q, now, 'absolute');
    } else {
        sampleAbsoluteCompass(q, now);
    }
}

function ingestDriverSample(q, now, source) {
    const gap = now - orientLastDriverTime;
    if (source !== orientSource || gap > ORIENT_GAP_MS) {
        // New or possibly restarted sensor frame: nothing measured in the old one still holds
        resetSensorFrame(source);
    }

    if (!orientSmoothQ) {
        orientSmoothQ = q;
        orientSpeed = 0;
    } else {
        const dt = Math.min(0.1, Math.max(0.001, gap / 1000));
        const instSpeed = quatAngle(orientRawQ, q) / dt;
        orientSpeed += (instSpeed - orientSpeed) * Math.min(1, dt / 0.08);
        const t = Math.min(1, Math.max(0, (orientSpeed - ORIENT_SPEED_STILL) / (ORIENT_SPEED_MOVING - ORIENT_SPEED_STILL)));
        const tau = ORIENT_TAU_STILL + (ORIENT_TAU_MOVING - ORIENT_TAU_STILL) * t;
        orientSmoothQ = quatAngle(orientSmoothQ, q) > ORIENT_SNAP_ANGLE
            ? q
            : quatSlerp(orientSmoothQ, q, 1 - Math.exp(-dt / tau));
    }
    orientRawQ = q;
    orientLastDriverTime = now;

    orientHistory.push({ t: now, q });
    while (orientHistory.length && now - orientHistory[0].t > ORIENT_HISTORY_MS) orientHistory.shift();

    if (orientActive) {
        if (orientWatchdog) { clearTimeout(orientWatchdog); orientWatchdog = null; }
        applyOrientationToView();
        refreshOrientStatus();
    }
}

// A new sensor frame can differ from the old one only by a turn about the
// vertical (gravity fixes the rest). The compass heading of both frames gives
// that turn, so an alignment carries over instead of being lost.
function resetSensorFrame(source) {
    let pending = null;
    if (orientPendingAlign) {
        // Still waiting to carry over from an earlier frame: keep its reference
        pending = orientPendingAlign;
    } else if (orientAlignment) {
        pending = {
            alignment: orientAlignment,
            pairs: orientAlignPairs,
            offset: orientCompassOffset,
            sameSession: true
        };
    } else if (orientFirstFrame) {
        pending = loadSavedAlignment();
    }
    orientFirstFrame = false;

    orientSource = source;
    orientSmoothQ = null;
    orientRawQ = null;
    orientHistory = [];
    // A compass-referenced driver is already in the magnetic frame
    orientCompassOffset = source === 'absolute' ? 0 : null;
    orientCompassVec = null;
    orientCompassSince = 0;
    orientAlignment = null;
    orientAlignPairs = [];
    orientPendingAlign = null;

    if (!pending) return;
    if (pending.sameSession) {
        // Most pauses don't restart the frame: keep using the alignment until
        // the compass says otherwise
        orientAlignment = pending.alignment;
        orientAlignPairs = pending.pairs;
    }
    if (pending.offset != null) {
        orientPendingAlign = pending;
        tryReanchorAlignment(performance.now());
    }
}

// Rotate a pending alignment into the current sensor frame once the compass
// heading of that frame is known
function tryReanchorAlignment(now) {
    const p = orientPendingAlign;
    if (!p || orientCompassOffset == null) return;
    if (orientSource !== 'absolute' && now - orientCompassSince < ALIGN_REANCHOR_SETTLE_MS) return;
    let delta = wrapAngle(orientCompassOffset - p.offset);
    if (p.sameSession && Math.abs(delta) < ALIGN_SAME_FRAME_DEG * DEG2RAD) delta = 0;
    const Y = enuYawMatrix(delta);
    const Yt = transpose3(Y);
    orientAlignment = { ...p.alignment, A: mul3x3(p.alignment.A, Y) };
    orientAlignPairs = p.pairs.map(pair => ({ ...pair, m: mulMat3Vec3(Yt, pair.m) }));
    orientPendingAlign = null;
    saveAlignment();
    if (orientActive) {
        applyOrientationToView();
        refreshOrientStatus();
        updateOrientUI();
    }
}

// Saved with the compass heading of its frame, so it survives a page reload
function saveAlignment() {
    if (!orientAlignment || orientCompassOffset == null) return;
    try {
        localStorage.setItem(ALIGN_STORAGE_KEY, JSON.stringify({
            alignment: orientAlignment,
            pairs: orientAlignPairs,
            offset: orientCompassOffset,
            savedAt: Date.now()
        }));
    } catch (e) { /* storage unavailable */ }
}

function loadSavedAlignment() {
    try {
        const saved = JSON.parse(localStorage.getItem(ALIGN_STORAGE_KEY) || 'null');
        if (!saved || !saved.alignment || !Array.isArray(saved.alignment.A)
            || typeof saved.offset !== 'number' || !Array.isArray(saved.pairs)) return null;
        if (Date.now() - saved.savedAt > ALIGN_STORAGE_MAX_AGE_MS) return null;
        return { alignment: saved.alignment, pairs: saved.pairs, offset: saved.offset, sameSession: false };
    } catch (e) {
        return null;
    }
}

function forgetSavedAlignment() {
    try { localStorage.removeItem(ALIGN_STORAGE_KEY); } catch (e) { /* storage unavailable */ }
}

// Fold one heading-offset sample into the slow circular filter
function addCompassSample(offset, now) {
    if (!orientCompassVec) {
        orientCompassVec = [Math.cos(offset), Math.sin(offset)];
        orientCompassSince = now;
    } else {
        const dt = Math.min(1, Math.max(0, (now - orientCompassTime) / 1000));
        const a = 1 - Math.exp(-dt / ORIENT_COMPASS_TAU);
        orientCompassVec[0] += (Math.cos(offset) - orientCompassVec[0]) * a;
        orientCompassVec[1] += (Math.sin(offset) - orientCompassVec[1]) * a;
    }
    orientCompassTime = now;
    orientCompassOffset = Math.atan2(orientCompassVec[1], orientCompassVec[0]);
    tryReanchorAlignment(now);
}

// Android: compare the compass-referenced orientation with the gyro one. Their
// difference is a pure turn about the vertical: the heading of the gyro frame.
function sampleAbsoluteCompass(qAbs, now) {
    if (orientSource !== 'relative' || !orientLastRel) return;
    if (now - orientLastRel.t > 50 || orientSpeed > 0.5) return; // pairs must be simultaneous
    const M = mul3x3(quatToMatrix(qAbs), transpose3(quatToMatrix(orientLastRel.q)));
    addCompassSample(Math.atan2(M[1][0], M[0][0]), now);
}

// iOS: webkitCompassHeading is the bearing of the top of the phone. Exactly
// what it reports once the phone is tilted towards the sky isn't documented,
// so it is only trusted while the screen faces mostly upwards, where its
// meaning is unambiguous. The gyro frame then carries that heading as the
// phone is raised.
function sampleIOSCompass(qRel, headingDeg, accuracyDeg, now) {
    if (orientSource !== 'relative') return;
    if (typeof accuracyDeg === 'number' && accuracyDeg < 0) return; // compass uncalibrated
    const R = quatToMatrix(qRel);
    if (R[2][2] < 0.5) return; // screen tilted more than 60 degrees from face-up
    const headingRel = Math.atan2(R[0][1], R[1][1]); // bearing of device +y in the gyro frame
    addCompassSample(wrapAngle(headingRel - headingDeg * DEG2RAD), now);
    if (typeof accuracyDeg === 'number') orientCompassAccuracy = accuracyDeg;
}

function transpose3(M) {
    return [
        [M[0][0], M[1][0], M[2][0]],
        [M[0][1], M[1][1], M[2][1]],
        [M[0][2], M[1][2], M[2][2]]
    ];
}

// ===========================================================================
// Orientation -> view
// ===========================================================================

// Driver frame -> true-north ENU
function currentHeadingMatrix() {
    if (orientAlignment) return orientAlignment.A;
    return enuYawMatrix((orientCompassOffset || 0) - orientDeclinationRad);
}

// Device (with the screen's rotation applied) -> true-north ENU
function deviceToWorldMatrix(q) {
    const screenQ = quatMultiply(q, quatAboutZ(-screenAngleRad()));
    return mul3x3(currentHeadingMatrix(), quatToMatrix(screenQ));
}

// The map's horizon -> view matrix for a device orientation. Its rows are the
// screen's right, up and forward (out of the back) directions in horizon
// coordinates, which reorders ENU (east, north, up) to (east, up, north).
function viewMatrixForDevice(q) {
    const F = deviceToWorldMatrix(q);
    return [
        [ F[0][0],  F[2][0],  F[1][0]],
        [ F[0][1],  F[2][1],  F[1][1]],
        [-F[0][2], -F[2][2], -F[1][2]]
    ];
}

function applyOrientationToView() {
    if (!orientActive || !orientSmoothQ) return;
    const R = viewMatrixForDevice(orientSmoothQ);
    const moved = viewMatrixOverride ? matrixAngle(viewMatrixOverride, R) : Infinity;
    if (moved < ORIENT_REDRAW_ANGLE) return;
    viewMatrixOverride = R;
    if (moved > ORIENT_INTERACTION_ANGLE) beginInteraction();
    scheduleDraw();
}

// After leaving orientation mode the view keeps pointing where the phone was;
// the roll eases back to a level horizon over a few frames.
function stepRollBlend() {
    if (!orientBlend) return;
    const t = (performance.now() - orientBlend.start) / 300;
    if (t >= 1) {
        orientBlend = null;
        viewMatrixOverride = null;
        scheduleDraw();
        return;
    }
    const eased = 1 - Math.pow(1 - t, 3);
    const target = matrixToQuat(viewMatrixFromAngles(rotX, rotY));
    viewMatrixOverride = quatToMatrix(quatSlerp(orientBlend.fromQ, target, eased));
    scheduleDraw();
    requestAnimationFrame(stepRollBlend);
}

// ===========================================================================
// Starting and stopping
// ===========================================================================
async function startOrientationMode() {
    if (orientActive) return;
    // iOS only grants motion access from inside a tap, so ask before anything else
    if (typeof DeviceOrientationEvent !== 'undefined'
        && typeof DeviceOrientationEvent.requestPermission === 'function') {
        let result = 'denied';
        try { result = await DeviceOrientationEvent.requestPermission(); } catch (e) { console.warn(e); }
        if (result !== 'granted') {
            alert('Motion sensor access was not allowed. To use phone pointing, allow "Motion & Orientation Access" for this site in Safari settings and try again.');
            return;
        }
    }
    if (!orientListening) {
        window.addEventListener('deviceorientation', handleOrientationEvent);
        window.addEventListener('deviceorientationabsolute', handleOrientationEvent);
        orientListening = true;
    }

    orientActive = true;
    orientBlend = null;
    updateOrientUI();
    refreshOrientStatus();
    if (orientSmoothQ) applyOrientationToView();

    // Desktop browsers define the API but never deliver readings
    orientWatchdog = setTimeout(() => {
        orientWatchdog = null;
        if (orientActive && !orientSmoothQ) {
            stopOrientationMode({ immediate: true });
            alert('This device is not reporting its orientation, so phone pointing is unavailable.');
        }
    }, 2500);

    // Pointing only makes sense against the sky as it is right now, from here
    const liveTime = document.getElementById('live-time');
    if (liveTime && !liveTime.checked) {
        liveTime.checked = true;
        liveTime.dispatchEvent(new Event('change'));
    }
    refinePositionForOrientation();
    refreshDeclination();
    requestOrientWakeLock();
}

function stopOrientationMode({ immediate = false } = {}) {
    if (!orientActive) return;
    orientActive = false;
    if (orientWatchdog) { clearTimeout(orientWatchdog); orientWatchdog = null; }
    closeAlignPicker();
    closeAlignCamera();
    releaseOrientWakeLock();

    if (viewMatrixOverride) {
        // Carry on looking where the phone pointed: forward is the matrix's third row
        const f = viewMatrixOverride[2];
        rotX = Math.asin(Math.max(-1, Math.min(1, f[1])));
        rotY = Math.atan2(f[0], f[2]);
        if (immediate) {
            viewMatrixOverride = null;
        } else {
            orientBlend = { fromQ: matrixToQuat(viewMatrixOverride), start: performance.now() };
            requestAnimationFrame(stepRollBlend);
        }
    }
    updateOrientUI();
    scheduleDraw();
}

// A precise fix matters here: the sky shifts by as much as the location is wrong
function refinePositionForOrientation() {
    if (!('geolocation' in navigator)) return;
    navigator.geolocation.getCurrentPosition(pos => {
        const lat = pos.coords.latitude, lon = pos.coords.longitude;
        if (Math.abs(lat - (parseFloat(latInput.value) || 0)) < 0.005
            && Math.abs(lon - (parseFloat(lonInput.value) || 0)) < 0.005) return;
        latInput.value = lat.toFixed(2);
        lonInput.value = lon.toFixed(2);
        latInput.dispatchEvent(new Event('change'));
        lonInput.dispatchEvent(new Event('change'));
        refreshDeclination();
    }, err => {
        console.log('Precise location unavailable for phone pointing:', err.message);
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
}

function refreshDeclination() {
    const lat = parseFloat(latInput.value), lon = parseFloat(lonInput.value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    // Declination changes slowly with position: a tenth of a degree is plenty
    const key = `${lat.toFixed(1)},${lon.toFixed(1)}`;
    if (key === orientDeclinationKey) return;
    orientDeclinationKey = key;
    fetch(`/api/magnetic_declination?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`)
        .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
        })
        .then(data => {
            if (typeof data.declination !== 'number') return;
            orientDeclinationRad = data.declination * DEG2RAD;
            if (orientActive) applyOrientationToView();
        })
        .catch(err => {
            orientDeclinationKey = '';
            console.warn('Magnetic declination unavailable; compass headings stay magnetic:', err);
        });
}

async function requestOrientWakeLock() {
    if (!navigator.wakeLock || orientWakeLock) return;
    try {
        orientWakeLock = await navigator.wakeLock.request('screen');
        orientWakeLock.addEventListener('release', () => { orientWakeLock = null; });
    } catch (e) {
        orientWakeLock = null;
    }
}

function releaseOrientWakeLock() {
    if (orientWakeLock) {
        orientWakeLock.release().catch(() => {});
        orientWakeLock = null;
    }
}

// ===========================================================================
// Alignment
//
// The user centres a known bright object in the camera's crosshair and taps
// Lock. The camera direction measured by the sensors and the object's real
// direction then give the heading (one object) or the full rotation (two),
// replacing the compass entirely.
//
// Targets are placed in the map's own catalogue frame, so the drawn object
// lands on the real one; refraction and the Moon's parallax, which the map
// doesn't model, are applied because they belong to the target, not the frame.
// ===========================================================================
function refractionDeg(altDeg) {
    // Saemundsson: true -> apparent altitude, standard temperature and pressure
    if (altDeg < -1) return 0;
    return 1.02 / Math.tan((altDeg + 10.3 / (altDeg + 5.11)) * DEG2RAD) / 60;
}

function alignTargetAltAz(target, date) {
    const lat = parseFloat(latInput.value) || 0, lon = parseFloat(lonInput.value) || 0;
    let { altDeg, azDeg } = radecToAltAz(target.ra, target.dec, date, lat, lon);
    if (target.name === 'Moon') {
        // The server's Moon is geocentric; from the surface it sits up to ~1 degree lower
        altDeg -= MOON_HORIZONTAL_PARALLAX_DEG * Math.cos(altDeg * DEG2RAD);
    }
    altDeg += refractionDeg(altDeg);
    return { altDeg, azDeg };
}

function altAzToENU(altDeg, azDeg) {
    const alt = altDeg * DEG2RAD, az = azDeg * DEG2RAD;
    return [Math.cos(alt) * Math.sin(az), Math.cos(alt) * Math.cos(az), Math.sin(alt)];
}

function compassPoint(azDeg) {
    return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(((azDeg % 360) + 360) % 360 / 45) % 8];
}

// Bright objects currently well placed for aligning on, brightest first
async function alignmentCandidates() {
    const now = new Date();
    const found = [];
    if (planetsList.length === 0) updatePlanetsList();
    for (const p of planetsList) {
        if (p.name === 'Sun') continue;
        if (p.name !== 'Moon' && (p.mag == null || p.mag > ALIGN_PLANET_MAG)) continue;
        found.push({ name: p.name, ra: p.ra, dec: p.dec, mag: p.name === 'Moon' ? -12 : p.mag });
    }
    await loadStarNames();
    if (starNameEntries && starRA) {
        resolveStarNameIndices();
        for (const entry of starNameEntries) {
            if (entry.mag == null || entry.mag > ALIGN_STAR_MAG) continue;
            if (entry.index < 0) continue;
            found.push({ name: entry.name, ra: starRA[entry.index], dec: starDec[entry.index], mag: entry.mag });
        }
    }
    return found
        .map(t => ({ ...t, ...alignTargetAltAz(t, now) }))
        .filter(t => t.altDeg >= ALIGN_MIN_ALT && t.altDeg <= ALIGN_MAX_ALT)
        .sort((a, b) => a.mag - b.mag);
}

async function openAlignPicker() {
    if (!alignPicker) return;
    alignCandidatesList.innerHTML = '<li class="align-empty">Finding bright objects…</li>';
    alignPicker.hidden = false;
    const candidates = await alignmentCandidates();
    if (alignPicker.hidden) return; // cancelled while loading
    alignCandidatesList.innerHTML = '';
    if (candidates.length === 0) {
        alignCandidatesList.innerHTML = `<li class="align-empty">Nothing bright enough is between ${ALIGN_MIN_ALT}° and ${ALIGN_MAX_ALT}° above the horizon right now.</li>`;
        return;
    }
    const aligned = new Set(orientAlignPairs.map(p => p.name));
    for (const target of candidates) {
        const li = document.createElement('li');
        const btn = document.createElement('button');
        btn.type = 'button';
        const name = document.createElement('span');
        name.className = 'align-name';
        name.textContent = target.name + (aligned.has(target.name) ? ' ✓' : '');
        const where = document.createElement('span');
        where.className = 'align-where';
        where.textContent = `${compassPoint(target.azDeg)}, ${Math.round(target.altDeg)}° up · mag ${target.mag.toFixed(1)}`;
        btn.append(name, where);
        btn.addEventListener('click', () => {
            closeAlignPicker();
            openAlignCamera(target);
        });
        li.appendChild(btn);
        alignCandidatesList.appendChild(li);
    }
}

function closeAlignPicker() {
    if (alignPicker) alignPicker.hidden = true;
}

let alignTarget = null;
let alignStream = null;
let alignAimAxis = 'back';     // 'back' through the camera, 'top' sighting along the top edge
let alignPressTime = 0;
let alignHintFrame = null;

async function openAlignCamera(target) {
    alignTarget = target;
    alignAimAxis = 'back';
    alignCamera.hidden = false;
    alignCamera.classList.remove('no-video');
    alignHint.textContent = '';
    alignHintFrame = requestAnimationFrame(updateAlignHint);
    try {
        alignStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
            audio: false
        });
        if (alignCamera.hidden) { stopAlignStream(); return; } // cancelled meanwhile
        alignVideo.srcObject = alignStream;
        await alignVideo.play().catch(() => {});
    } catch (e) {
        console.warn('Camera unavailable for alignment:', e);
        // Fall back to sighting along the phone's top edge, like a gun sight
        alignAimAxis = 'top';
        alignCamera.classList.add('no-video');
    }
}

function stopAlignStream() {
    if (alignStream) {
        alignStream.getTracks().forEach(track => track.stop());
        alignStream = null;
    }
    if (alignVideo) alignVideo.srcObject = null;
}

function closeAlignCamera() {
    if (!alignCamera || alignCamera.hidden) return;
    alignCamera.hidden = true;
    stopAlignStream();
    if (alignHintFrame) cancelAnimationFrame(alignHintFrame);
    alignHintFrame = null;
    alignTarget = null;
}

// Where the target is relative to the crosshair, by the current estimate
function updateAlignHint() {
    alignHintFrame = null;
    if (alignCamera.hidden || !alignTarget) return;
    alignHintFrame = requestAnimationFrame(updateAlignHint);

    const { altDeg, azDeg } = alignTargetAltAz(alignTarget, new Date());
    const lines = [`${alignTarget.name}: ${compassPoint(azDeg)}, ${Math.round(altDeg)}° above the horizon`];
    if (alignAimAxis === 'top') {
        lines.push('No camera: point the top edge of the phone straight at it, sighting along the edge, then tap Lock.');
    } else if (orientSmoothQ && (orientAlignment || orientCompassOffset != null)) {
        const F = deviceToWorldMatrix(orientSmoothQ);
        const t = altAzToENU(altDeg, azDeg);
        const right = F[0][0] * t[0] + F[1][0] * t[1] + F[2][0] * t[2];
        const up = F[0][1] * t[0] + F[1][1] * t[1] + F[2][1] * t[2];
        const fwd = -(F[0][2] * t[0] + F[1][2] * t[1] + F[2][2] * t[2]);
        const off = Math.acos(Math.max(-1, Math.min(1, fwd))) / DEG2RAD;
        if (off < 8) {
            lines.push('It should be close: put it exactly in the crosshair, hold still and tap Lock.');
        } else {
            const h = Math.atan2(right, fwd) / DEG2RAD, v = Math.atan2(up, fwd) / DEG2RAD;
            const parts = [];
            if (Math.abs(h) >= 3) parts.push(`${Math.round(Math.abs(h))}° ${h > 0 ? 'right' : 'left'}`);
            if (Math.abs(v) >= 3) parts.push(`${Math.round(Math.abs(v))}° ${v > 0 ? 'up' : 'down'}`);
            lines.push(`Compass estimate: about ${parts.join(', ') || `${Math.round(off)}° away`}.`);
        }
    } else {
        lines.push('Centre it in the crosshair, hold still and tap Lock.');
    }
    const text = lines.join('\n');
    if (alignHint.textContent !== text) alignHint.textContent = text;
}

// Average of the orientations recorded just before the finger touched Lock,
// so the tap itself doesn't shake the measurement. Null if the phone moved.
function captureSteadyQuat(pressTime) {
    const recent = orientHistory.filter(s => s.t >= pressTime - 450 && s.t <= pressTime - 60);
    if (recent.length === 0) return orientRawQ;
    const ref = recent[0].q;
    let sum = [0, 0, 0, 0];
    for (const s of recent) {
        const sign = quatDot(ref, s.q) < 0 ? -1 : 1;
        for (let k = 0; k < 4; k++) sum[k] += sign * s.q[k];
    }
    const mean = quatNormalize(sum);
    for (const s of recent) {
        if (quatAngle(mean, s.q) > ALIGN_STEADY_ANGLE) return null;
    }
    return mean;
}

// Direction the user aimed, in the driver frame
function aimedDirection(q) {
    const R = quatToMatrix(q);
    return alignAimAxis === 'top'
        ? [R[0][1], R[1][1], R[2][1]]       // device +y
        : [-R[0][2], -R[1][2], -R[2][2]];   // device -z, out of the back camera
}

function lockAlignment() {
    if (!alignTarget || !orientRawQ) return;
    // Keyboard activation has no pointerdown, so only trust a press time from this tap
    const now = performance.now();
    const q = captureSteadyQuat(now - alignPressTime < 2000 ? alignPressTime : now);
    if (!q) {
        alignHint.textContent = 'The phone moved while locking. Hold it steady on the target and tap Lock again.';
        return;
    }
    const { altDeg, azDeg } = alignTargetAltAz(alignTarget, new Date());
    orientAlignPairs.push({ name: alignTarget.name, m: aimedDirection(q), t: altAzToENU(altDeg, azDeg) });
    orientAlignment = solveAlignment(orientAlignPairs);
    orientPendingAlign = null;
    saveAlignment();
    closeAlignCamera();
    applyOrientationToView();
    refreshOrientStatus();
    updateOrientUI();
}

// Rotation (driver frame -> true ENU) from the latest pair, plus the most
// recent earlier one far enough away from it to pin down all three axes.
function solveAlignment(pairs) {
    const latest = pairs[pairs.length - 1];
    let partner = null;
    for (let i = pairs.length - 2; i >= 0; i--) {
        const sep = vecAngle(pairs[i].t, latest.t);
        if (sep >= ALIGN_MIN_SEPARATION && sep <= Math.PI - ALIGN_MIN_SEPARATION) { partner = pairs[i]; break; }
    }

    if (!partner) {
        // One object: gravity already fixes tilt, so only the heading is solved
        const yaw = wrapAngle(enuHeading(latest.m) - enuHeading(latest.t));
        return { A: enuYawMatrix(yaw), names: [latest.name], residualDeg: null };
    }

    // Two objects: symmetric TRIAD. The bisector and difference of the two
    // directions are orthogonal, so both measurements carry equal weight.
    const basis = (a, b) => {
        const s = vecNormalize([a[0] + b[0], a[1] + b[1], a[2] + b[2]]);
        const d = vecNormalize([a[0] - b[0], a[1] - b[1], a[2] - b[2]]);
        return [s, d, vecCross(s, d)];
    };
    const Bm = basis(latest.m, partner.m), Bt = basis(latest.t, partner.t);
    const A = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    for (let i = 0; i < 3; i++) {
        for (let j = 0; j < 3; j++) {
            A[i][j] = Bt[0][i] * Bm[0][j] + Bt[1][i] * Bm[1][j] + Bt[2][i] * Bm[2][j];
        }
    }
    // The measured and true separations should match; the gap is the aiming error
    const residualDeg = Math.abs(vecAngle(latest.m, partner.m) - vecAngle(latest.t, partner.t)) / DEG2RAD;
    return { A, names: [partner.name, latest.name], residualDeg };
}

function clearAlignment() {
    orientAlignment = null;
    orientAlignPairs = [];
    orientPendingAlign = null;
    forgetSavedAlignment();
    applyOrientationToView();
    refreshOrientStatus();
    updateOrientUI();
}

// ===========================================================================
// On-map overlay and UI
// ===========================================================================

// Centre reticle, plus an arrow to the searched object when it's off screen
function drawOrientationOverlay(Mview) {
    if (!orientActive) return;
    const cx = width / 2, cy = height / 2;
    ctx.save();
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.45)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(cx, cy, 12, 0, 2 * Math.PI);
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        ctx.moveTo(cx + dx * 16, cy + dy * 16);
        ctx.lineTo(cx + dx * 24, cy + dy * 24);
    }
    ctx.stroke();

    if (searchedObject) {
        const [x, y, z] = mulMat3Vec3(Mview, radecToXYZ(searchedObject.ra, searchedObject.dec));
        let onScreen = false;
        if (z > 0) {
            const [px, py] = project([x, y, z]);
            onScreen = px > 30 && px < width - 30 && py > 30 && py < height - 30;
        }
        if (!onScreen) {
            let dx = x, dy = -y;
            const len = Math.hypot(dx, dy);
            if (len < 1e-6) { dx = 0; dy = 1; } else { dx /= len; dy /= len; }
            const edge = Math.min(
                (width / 2 - 44) / Math.max(Math.abs(dx), 1e-6),
                (height / 2 - 44) / Math.max(Math.abs(dy), 1e-6));
            const ax = cx + dx * edge, ay = cy + dy * edge;
            ctx.save();
            ctx.translate(ax, ay);
            ctx.rotate(Math.atan2(dy, dx));
            ctx.fillStyle = 'rgba(255, 165, 0, 0.9)';
            ctx.beginPath();
            ctx.moveTo(16, 0);
            ctx.lineTo(-8, -11);
            ctx.lineTo(-8, 11);
            ctx.closePath();
            ctx.fill();
            ctx.restore();
            const off = Math.round(Math.acos(Math.max(-1, Math.min(1, z))) / DEG2RAD);
            ctx.fillStyle = 'rgba(255, 165, 0, 0.95)';
            ctx.font = '12px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(`${searchedObject.name} ${off}°`, ax - dx * 34, ay - dy * 30);
        }
    }
    ctx.restore();
}

function refreshOrientStatus() {
    if (!orientStatus) return;
    let text;
    if (!orientSmoothQ) {
        text = 'Waiting for the motion sensors…';
    } else if (orientAlignment) {
        text = orientAlignment.residualDeg == null
            ? `Aligned on ${orientAlignment.names[0]} · align on a 2nd object for full accuracy`
            : `Aligned on ${orientAlignment.names.join(' + ')} (±${Math.max(0.1, orientAlignment.residualDeg).toFixed(1)}°)`;
    } else if (orientPendingAlign) {
        text = orientHasIOSCompass
            ? `Restoring alignment on ${orientPendingAlign.alignment.names.join(' + ')} · hold the phone flat for a moment`
            : `Restoring alignment on ${orientPendingAlign.alignment.names.join(' + ')}…`;
    } else if (orientCompassOffset == null) {
        text = orientHasIOSCompass
            ? 'Hold the phone flat for a moment to find north'
            : 'No compass reading yet · tap Align to set north';
    } else if (orientCompassAccuracy != null) {
        text = `Compass ±${Math.round(orientCompassAccuracy)}° · tap Align for accuracy`;
    } else {
        text = 'Compass only · tap Align for accuracy';
    }
    if (orientStatus.textContent !== text) orientStatus.textContent = text;
}

function updateOrientUI() {
    if (orientToggleBtn) {
        orientToggleBtn.classList.toggle('active', orientActive);
        orientToggleBtn.setAttribute('aria-pressed', orientActive ? 'true' : 'false');
    }
    if (orientBar) orientBar.hidden = !orientActive;
    if (orientUnalignBtn) orientUnalignBtn.hidden = !orientAlignment && !orientPendingAlign;
}

if (orientToggleBtn && typeof DeviceOrientationEvent !== 'undefined' && isTouchDevice) {
    orientToggleBtn.hidden = false;
    orientToggleBtn.addEventListener('click', () => {
        if (orientActive) stopOrientationMode();
        else startOrientationMode();
    });
}
if (orientAlignBtn) orientAlignBtn.addEventListener('click', openAlignPicker);
if (orientUnalignBtn) orientUnalignBtn.addEventListener('click', clearAlignment);
if (alignPickerCancel) alignPickerCancel.addEventListener('click', closeAlignPicker);
if (alignCancelBtn) alignCancelBtn.addEventListener('click', closeAlignCamera);
if (alignLockBtn) {
    // Note when the finger lands; the reading is taken from just before then
    alignLockBtn.addEventListener('pointerdown', () => { alignPressTime = performance.now(); });
    alignLockBtn.addEventListener('click', lockAlignment);
}

// Declination follows the observer's location while the mode is in use
for (const input of [latInput, lonInput]) {
    input.addEventListener('change', () => { if (orientActive) refreshDeclination(); });
}

// The wake lock is dropped whenever the page is hidden; the camera should be too
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        closeAlignCamera();
        saveAlignment(); // with the latest compass heading
    } else if (orientActive) {
        requestOrientWakeLock();
    }
});

// The screen's own rotation changes the device -> screen mapping
window.addEventListener('orientationchange', () => {
    if (orientActive) setTimeout(applyOrientationToView, 50);
});
