// Compare static/js/processing/cr2.js with what cr2_oracle.py wrote:
//   node cr2_check.mjs <file.cr2> <oracle dir>
import fs from 'node:fs';
import { decodeCR2 } from '../../../static/js/processing/cr2.js';

const [sample, oracle] = process.argv.slice(2);
const buf = fs.readFileSync(sample);
const t = performance.now();
const d = decodeCR2(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
console.log(`decoded in ${Math.round(performance.now() - t)} ms`);
const info = JSON.parse(fs.readFileSync(`${oracle}/info.json`, 'utf8'));
const ref = new Uint16Array(fs.readFileSync(`${oracle}/raw.u16`).buffer.slice(0));
let diff = 0;
for (let i = 0; i < ref.length; i++) if (ref[i] !== d.raw[i]) diff++;
// LibRaw's colour indices: 0 R, 1 G, 2 B, 3 G2; ours: 0 R, 1 G, 2 B
const pattern = info.colors.map(row => row.map(c => (c === 3 ? 1 : c)));
const black = info.colors.map(row => row.map(c => info.black[c]));
const checks = {
    raw: diff === 0 && d.rawWidth === info.raw_shape[1] && d.rawHeight === info.raw_shape[0],
    area: d.top === info.top && d.left === info.left && d.width === info.width && d.height === info.height,
    pattern: JSON.stringify(d.pattern) === JSON.stringify(pattern),
    black: JSON.stringify(d.black) === JSON.stringify(black),
    white: d.white === info.white,
    daylight: Math.abs(d.wb[0] - info.daylight[0] / info.daylight[1]) < 1e-3 && Math.abs(d.wb[2] - info.daylight[2] / info.daylight[1]) < 1e-3,
    asShot: Math.abs(d.wbAsShot[0] - info.camera_wb[0] / info.camera_wb[1]) < 1e-6 && Math.abs(d.wbAsShot[2] - info.camera_wb[2] / info.camera_wb[1]) < 1e-6
};
console.log(checks, `${diff} raw samples differ`);
process.exit(Object.values(checks).every(Boolean) ? 0 : 1);
