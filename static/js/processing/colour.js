// Colour helpers shared by the RAW and JPEG readers: the camera-to-sRGB matrix and the
// daylight white balance (both computed dcraw-style from a camera-from-XYZ matrix),
// sRGB transfer curves, and Rec. 709 luma. Ported from app/processing/frames.py.

// sRGB (D65) to XYZ, as dcraw and LibRaw use it
export const XYZ_FROM_SRGB = [
    [0.412453, 0.357580, 0.180423],
    [0.212671, 0.715160, 0.072169],
    [0.019334, 0.119193, 0.950227]
];

export const LUMA = [0.2126, 0.7152, 0.0722];

export function matMul3(a, b) {
    const out = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
        out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
    }
    return out;
}

export function invert3(m) {
    const [a, b, c] = m[0], [d, e, f] = m[1], [g, h, i] = m[2];
    const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
    const det = a * A + b * B + c * C;
    if (Math.abs(det) < 1e-12) return null;
    return [
        [A / det, -(b * i - c * h) / det, (b * f - c * e) / det],
        [B / det, (a * i - c * g) / det, -(a * f - c * d) / det],
        [C / det, -(a * h - b * g) / det, (a * e - b * d) / det]
    ];
}

const IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];

// Rows of cam_xyz @ XYZ_FROM_SRGB, normalised to sum to 1 (and the sums, for the
// daylight white balance)
function camRgb(camXyz) {
    const m = matMul3(camXyz, XYZ_FROM_SRGB);
    const sums = m.map(row => row[0] + row[1] + row[2]);
    return { m, sums };
}

// Camera RGB (white balanced) to linear sRGB, as frames.cam_to_srgb_matrix
export function camToSrgbMatrix(camXyz) {
    if (!camXyz || !camXyz.flat().some(v => v)) return IDENTITY.map(r => r.slice());
    const { m, sums } = camRgb(camXyz);
    if (sums.some(s => Math.abs(s) < 1e-9)) return IDENTITY.map(r => r.slice());
    const norm = m.map((row, i) => row.map(v => v / sums[i]));
    return invert3(norm) || IDENTITY.map(r => r.slice());
}

// Daylight white balance from the matrix (dcraw's pre_mul = 1 / row sums), green = 1
export function daylightWb(camXyz) {
    const { sums } = camRgb(camXyz);
    if (sums.some(s => Math.abs(s) < 1e-9)) return null;
    const pre = sums.map(s => 1 / s);
    return [pre[0] / pre[1], 1, pre[2] / pre[1]];
}

export function normalisedWb(values) {
    if (!values || values.length < 3) return null;
    const [r, g, b] = values;
    if (!(r > 0 && g > 0 && b > 0)) return null;
    return [r / g, 1, b / g];
}

export function srgbToLinear(v) {
    return v <= 0.04045 ? v / 12.92 : Math.pow((Math.max(v, 0) + 0.055) / 1.055, 2.4);
}

export function linearToSrgb(v) {
    v = Math.max(v, 0);
    return v <= 0.0031308 ? v * 12.92 : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
}

export const SRGB_TO_LINEAR_LUT = (() => {
    const lut = new Float32Array(256);
    for (let i = 0; i < 256; i++) lut[i] = srgbToLinear(i / 255);
    return lut;
})();

// White balance then the matrix, in place, over planar channels (Float32Arrays)
export function applyColour(planes, wb, matrix) {
    const [r, g, b] = planes;
    const n = r.length;
    const w = wb || [1, 1, 1];
    const m = matrix || IDENTITY;
    for (let i = 0; i < n; i++) {
        const R = r[i] * w[0], G = g[i] * w[1], B = b[i] * w[2];
        r[i] = m[0][0] * R + m[0][1] * G + m[0][2] * B;
        g[i] = m[1][0] * R + m[1][1] * G + m[1][2] * B;
        b[i] = m[2][0] * R + m[2][1] * G + m[2][2] * B;
    }
    return planes;
}
