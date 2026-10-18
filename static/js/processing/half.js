// float16 storage: aligned frames on disk, the editor's preview. Round to nearest even;
// NaN stays NaN (it marks "no data" after alignment).

const f32 = new Float32Array(1);
const u32 = new Uint32Array(f32.buffer);

export function toHalf(value) {
    f32[0] = value;
    const x = u32[0];
    const sign = (x >>> 16) & 0x8000;
    const exp = (x >>> 23) & 0xff;
    let mant = x & 0x7fffff;
    if (exp === 0xff) return sign | 0x7c00 | (mant ? 0x200 : 0);
    let e = exp - 127 + 15;
    if (e >= 0x1f) return sign | 0x7c00;
    if (e <= 0) {
        if (e < -10) return sign;
        mant |= 0x800000;
        const shift = 14 - e;
        let half = mant >>> shift;
        const rem = mant & ((1 << shift) - 1);
        const mid = 1 << (shift - 1);
        if (rem > mid || (rem === mid && (half & 1))) half++;
        return sign | half;
    }
    let half = sign | (e << 10) | (mant >>> 13);
    const rem = mant & 0x1fff;
    if (rem > 0x1000 || (rem === 0x1000 && (half & 1))) half++;
    return half;
}

export const HALF_TO_FLOAT = (() => {
    const table = new Float32Array(65536);
    for (let h = 0; h < 65536; h++) {
        const sign = h & 0x8000 ? -1 : 1;
        const exp = (h >>> 10) & 0x1f;
        const mant = h & 0x3ff;
        let v;
        if (exp === 0) v = mant * Math.pow(2, -24);
        else if (exp === 0x1f) v = mant ? NaN : Infinity;
        else v = (1 + mant / 1024) * Math.pow(2, exp - 15);
        table[h] = sign * v;
    }
    return table;
})();

export function encodeHalf(src, dst = new Uint16Array(src.length)) {
    for (let i = 0; i < src.length; i++) dst[i] = toHalf(src[i]);
    return dst;
}

export function decodeHalf(src, dst = new Float32Array(src.length)) {
    const t = HALF_TO_FLOAT;
    for (let i = 0; i < src.length; i++) dst[i] = t[src[i]];
    return dst;
}
