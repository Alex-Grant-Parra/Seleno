// A minimal 16-bit RGB TIFF writer (uncompressed, one strip): what the editor exports
// for further work in other programs.

export function encodeTiff16(rgb, width, height) {
    const entries = [
        [256, 4, 1, width], [257, 4, 1, height], [258, 3, 3, null], [259, 3, 1, 1], [262, 3, 1, 2],
        [273, 4, 1, null], [277, 3, 1, 3], [278, 4, 1, height], [279, 4, 1, width * height * 6],
        [284, 3, 1, 1], [305, 2, 7, null]
    ];
    const ifdOffset = 8;
    const ifdBytes = 2 + entries.length * 12 + 4;
    const bpsOffset = ifdOffset + ifdBytes;
    const softOffset = bpsOffset + 6;
    const dataOffset = softOffset + 8;
    const head = new DataView(new ArrayBuffer(dataOffset));
    head.setUint16(0, 0x4949);
    head.setUint16(2, 42, true);
    head.setUint32(4, ifdOffset, true);
    head.setUint16(ifdOffset, entries.length, true);
    entries.forEach(([tag, type, count, value], i) => {
        const o = ifdOffset + 2 + i * 12;
        head.setUint16(o, tag, true);
        head.setUint16(o + 2, type, true);
        head.setUint32(o + 4, count, true);
        if (tag === 258) head.setUint32(o + 8, bpsOffset, true);
        else if (tag === 273) head.setUint32(o + 8, dataOffset, true);
        else if (tag === 305) head.setUint32(o + 8, softOffset, true);
        else if (type === 3) head.setUint16(o + 8, value, true);
        else head.setUint32(o + 8, value, true);
    });
    head.setUint32(ifdOffset + 2 + entries.length * 12, 0, true);
    [16, 16, 16].forEach((v, i) => head.setUint16(bpsOffset + i * 2, v, true));
    'Seleno\0'.split('').forEach((ch, i) => head.setUint8(softOffset + i, ch.charCodeAt(0)));
    // Little-endian 16-bit samples: the platform's own order everywhere that matters
    return new Blob([head.buffer, new Uint8Array(rgb.buffer, rgb.byteOffset, rgb.byteLength)], { type: 'image/tiff' });
}
