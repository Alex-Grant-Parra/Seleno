// node extract_canon.mjs colordata.cpp identify.cpp out.js
import fs from 'fs';
const [colordata, identify, out] = process.argv.slice(2);
const src = fs.readFileSync(colordata, 'utf8');
const re = /\{\s*LIBRAW_CAMERAMAKER_Canon,\s*"(EOS[^"]*)",\s*(-?\w+),\s*(-?\w+),[^{]*\{([^}]*)\}\s*\}/g;
const rows = [];
let m;
while ((m = re.exec(src))) {
  const coeffs = m[4].split(',').map(s => s.trim()).filter(Boolean).map(Number);
  rows.push([m[1], Number(m[2]), Number(m[3]), coeffs.slice(0, 9)]);
}
const id = fs.readFileSync(identify, 'utf8');
const block = id.slice(id.indexOf('static const ushort canon[][11]'));
const body2 = block.slice(block.indexOf('{') + 1, block.indexOf('};'));
const sizes = [];
for (const line of body2.split('\n')) {
  const mm = line.match(/^\s*\{([^}]*)\}/);
  if (!mm) continue;
  const v = mm[1].split(',').map(s => s.trim()).filter(Boolean).map(Number);
  while (v.length < 10) v.push(0);
  sizes.push(v.slice(0, 10));
}
const fmt = r => `    [${JSON.stringify(r[0])}, ${r[1]}, ${r[2]}, [${r[3].join(', ')}]]`;
fs.writeFileSync(out, `// Canon data from LibRaw (LGPL 2.1 or CDDL 1.0, copyright LibRaw LLC; dcraw by Dave
// Coffin before it). Credited in THIRD_PARTY_NOTICES.md. Generated: regenerate rather
// than hand-edit (see tests/processing/README.md).

// From src/tables/colordata.cpp (adobe_coeff): [model prefix, black, white level,
// camera-from-XYZ matrix x 10000, from Adobe DNG Converter]. Matched by prefix, in order.
export const CANON_EOS = [
${rows.map(fmt).join(',\n')}
];

// From src/metadata/identify.cpp (the canon[][11] table): [raw width, raw height, left
// margin, top margin, width decrement, height decrement, mask01, mask03, mask11, mask13]
export const CANON_SIZES = [
${sizes.map(r => `    [${r.join(', ')}]`).join(',\n')}
];
`);
console.log(rows.length, 'colour rows;', sizes.length, 'size rows', sizes.find(r => r[0] === 5792));
