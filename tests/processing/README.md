# Processing engine tests

The engine in `static/js/processing/` runs in the browser; these tests run its modules
in Node (22 or later), with the inline pool standing in for Web Workers and an in-memory
store for OPFS. The browser itself is covered by `tests/e2e/scenario_engine.js`.

| File | Covers |
|---|---|
| `core.test.mjs` | the maths against `app/processing` (Python): robust stats, background map, filters, star detection and measurement, registration (and that unrelated fields don't match), phase correlation, every integration method, debayering, the colour matrix, float16, developing |
| `stack.test.mjs` | the whole stacking pipeline on synthetic Bayer frames: masters, hot pixels, flats, quality rejection, alignment, the memory, disk and streaming combine modes (and that they agree), the no-OPFS path, noise falling with frame count, half size |
| `project.test.mjs` | a project over two nights, through the real FITS writer and range-request reader: equal to one stack of everything; frames already in are skipped |
| `lucky.test.mjs` | lucky imaging on a synthetic planet: sharpness scoring, choosing the sharpest, alignment |
| `synth.mjs` | the synthetic frames (the JS twin of `synth.py`) |
| `oracle.py` | writes Python's answers for `core.test.mjs` |

## Running them

`core.test.mjs` needs Python's answers first (scipy is required by `app/processing`;
`rawpy` only for the CR2 check below):

```bash
venv/bin/pip install --target /tmp/pylib scipy rawpy      # once, outside the venv
venv/bin/python tests/processing/oracle.py /tmp/oracle /tmp/pylib
ORACLE=/tmp/oracle node --test tests/processing/*.test.mjs
```

Without `ORACLE` the oracle tests are skipped and the rest run. About 10 seconds; the
frames are small (320 x 240), so it's light on memory.

## The CR2 reader against LibRaw

The reader (`cr2.js`) was checked against `rawpy` (LibRaw) on the CC0 sample
<https://raw.pixls.us/data/Canon/EOS%205D%20Mark%20II/08.canon.raw.cr2>: every one of the
22 million raw samples identical; visible area 5634 x 3752 at top 52, left 158; pattern
GBRG; black 1023/1024/1024/1024 (R, G, B, G2); white 15600; as-shot white balance
2016/1024/2335; daylight from the matrix 2.391/0.929/1.289. To repeat it (a few
seconds, about 200 MB of memory):

```bash
curl -o /tmp/08.cr2 "https://raw.pixls.us/data/Canon/EOS%205D%20Mark%20II/08.canon.raw.cr2"
mkdir -p /tmp/cr2-oracle
venv/bin/python -I tests/processing/tools/cr2_oracle.py /tmp/08.cr2 /tmp/cr2-oracle /tmp/pylib
node tests/processing/tools/cr2_check.mjs /tmp/08.cr2 /tmp/cr2-oracle
```

## The Canon tables

`static/js/processing/canon-colordata.js` is generated from LibRaw's sources (credited in
`THIRD_PARTY_NOTICES.md`):

```bash
curl -O https://raw.githubusercontent.com/LibRaw/LibRaw/master/src/tables/colordata.cpp
curl -O https://raw.githubusercontent.com/LibRaw/LibRaw/master/src/metadata/identify.cpp
node tests/processing/tools/extract_canon.mjs colordata.cpp identify.cpp static/js/processing/canon-colordata.js
```
