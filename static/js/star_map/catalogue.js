// Star map - the star catalogue: typed-array storage, banded binary loading and
// the star name list.

let starLoadingCounter = 0;
function starLoadingBegin() {
    starLoadingCounter++;
    if (starLoadingIndicator && starLoadingCounter > 0) {
        starLoadingIndicator.style.display = 'flex';
    }
}
function starLoadingEnd() {
    starLoadingCounter = Math.max(0, starLoadingCounter - 1);
    if (starLoadingIndicator && starLoadingCounter === 0) {
        starLoadingIndicator.style.display = 'none';
    }
}

// ===========================================================================
// Star catalogue
//
// Stars are held as parallel typed arrays sorted by magnitude (objects with no
// recorded magnitude last). That buys three things:
//   * no per-star JS objects, so 286k stars cost ~7 MB instead of ~100 MB
//   * "everything brighter than X" is a binary search, not a filter pass
//   * the draw loop walks memory linearly, brightest first, so it can stop
//     early during interaction and still show the most important stars
// ===========================================================================
const DEG2RAD = Math.PI / 180;

let starCount = 0;        // total catalogue size, from /api/stars_bands
let starLoadedCount = 0;  // contiguous prefix actually loaded so far
let starKnownCount = 0;   // entries [0, starKnownCount) have a real magnitude
let starRA = null;        // Float32Array, degrees
let starDec = null;       // Float32Array, degrees
let starMag = null;       // Float32Array, NaN where no magnitude was recorded
let starVX = null, starVY = null, starVZ = null; // unit vectors, equatorial frame
let starNames = [];
let starNameIndex = null; // lazily built lowercase name -> index map

function allocateStarCatalogue(total) {
    starCount = total;
    starLoadedCount = 0;
    starRA = new Float32Array(total);
    starDec = new Float32Array(total);
    starMag = new Float32Array(total);
    starVX = new Float32Array(total);
    starVY = new Float32Array(total);
    starVZ = new Float32Array(total);
    starNames = new Array(total);
    starNameIndex = null;
}

// Decode the binary payload from /api/stars_bin:
// 20-byte header, then float32 ra[], dec[], mag[], then newline-joined names.
function decodeStarBand(buffer) {
    if (!buffer || buffer.byteLength < 20) throw new Error('star payload too short');
    const view = new DataView(buffer);
    const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
    if (magic !== 'SMAP') throw new Error(`unexpected star payload "${magic}"`);
    const flags = view.getUint16(6, true);
    const count = view.getUint32(8, true);
    const namesLength = view.getUint32(12, true);
    let offset = 20;
    const ra = new Float32Array(buffer, offset, count); offset += count * 4;
    const dec = new Float32Array(buffer, offset, count); offset += count * 4;
    const mag = new Float32Array(buffer, offset, count); offset += count * 4;
    let names = null;
    if ((flags & 1) && namesLength > 0) {
        names = new TextDecoder('utf-8').decode(new Uint8Array(buffer, offset, namesLength)).split('\n');
    }
    return { count, ra, dec, mag, names };
}

// Copy a decoded band into the catalogue at `start`, computing unit vectors as
// we go. Done in slices so a 100k-star band never blocks a frame.
function ingestStarBand(start, band, onDone) {
    const total = band.count;
    let i = 0;
    const SLICE = 20000;

    function step() {
        const end = Math.min(i + SLICE, total);
        for (; i < end; i++) {
            const at = start + i;
            const raDeg = band.ra[i];
            const decDeg = band.dec[i];
            starRA[at] = raDeg;
            starDec[at] = decDeg;
            starMag[at] = band.mag[i];
            const raRad = raDeg * DEG2RAD;
            const decRad = decDeg * DEG2RAD;
            const cosDec = Math.cos(decRad);
            starVX[at] = cosDec * Math.cos(raRad);
            starVY[at] = Math.sin(decRad);
            starVZ[at] = cosDec * Math.sin(raRad);
            if (band.names) starNames[at] = band.names[i];
        }
        starLoadedCount = Math.max(starLoadedCount, start + i);
        starNameIndex = null; // names changed; rebuild on next search
        scheduleDraw();
        if (i < total) {
            requestAnimationFrame(step);
        } else if (typeof onDone === 'function') {
            onDone();
        }
    }
    step();
}

// Index of the first star fainter than `magLimit`. The array is magnitude
// sorted, so everything before it is visible at this limit.
function starsBrighterThan(magLimit) {
    let lo = 0;
    let hi = Math.min(starKnownCount, starLoadedCount);
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (starMag[mid] <= magLimit) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

// Range of catalogue entries with no recorded magnitude that are loaded.
function unknownMagRange() {
    if (starLoadedCount <= starKnownCount) return [0, 0];
    return [starKnownCount, starLoadedCount];
}

function starNameAt(index) {
    return starNames[index] || `#${index}`;
}

function findStarByName(name) {
    if (!name) return -1;
    if (!starNameIndex) {
        starNameIndex = new Map();
        for (let i = 0; i < starLoadedCount; i++) {
            const n = starNames[i];
            if (n) starNameIndex.set(n.toLowerCase(), i);
        }
    }
    const hit = starNameIndex.get(String(name).toLowerCase());
    return hit === undefined ? -1 : hit;
}

// A catalogue entry in the object shape the rest of the UI expects
function starObjectAt(index) {
    const mag = starMag[index];
    return {
        name: starNameAt(index),
        ra: starRA[index],
        dec: starDec[index],
        mag: Number.isNaN(mag) ? null : mag,
        magUnknown: Number.isNaN(mag),
        type: 'star',
        catalogIndex: index,
    };
}

// ===========================================================================
// Star names
//
// /api/star_names serves every named catalogue object (IAU proper names plus
// the catalogue's own names) keyed by designation, which we resolve to
// catalogue indices so a label can be drawn at the star's position.
// ===========================================================================
let starNameEntries = null;       // [{ id, name, mag, index }]
let starNameById = new Map();     // designation -> entry, for instant lookups on click
let starNameLoadPromise = null;
let starNamesResolvedAt = -1;     // starLoadedCount when indices were last resolved

function loadStarNames() {
    if (starNameEntries) return Promise.resolve(starNameEntries);
    if (starNameLoadPromise) return starNameLoadPromise;

    starNameLoadPromise = fetch('/api/star_names')
        .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
        })
        .then(data => {
            starNameEntries = (data.stars || []).map(entry => ({
                id: entry.id,
                name: entry.name,
                bayer: entry.bayer || '',
                mag: entry.mag,
                index: -1,
            }));
            starNameById = new Map(starNameEntries.map(entry => [entry.id, entry]));
            starNamesResolvedAt = -1;
            console.log(`Loaded ${starNameEntries.length} star names`);
            scheduleDraw();
            return starNameEntries;
        })
        .catch(err => {
            console.error('Star names failed to load:', err);
            starNameLoadPromise = null;
            return null;
        });
    return starNameLoadPromise;
}

// Map each name onto its catalogue row. Repeated only when more of the
// catalogue has arrived, never per frame.
function resolveStarNameIndices() {
    if (!starNameEntries || starNamesResolvedAt === starLoadedCount) return;
    starNamesResolvedAt = starLoadedCount;
    for (const entry of starNameEntries) {
        if (entry.index < 0) entry.index = findStarByName(entry.id);
    }
}

// ---------------------------------------------------------------------------
// Catalogue loading
//
// /api/stars_bands hands back disjoint index ranges, brightest first, and
// /api/stars_bin serves each one as a compact binary block. Nothing is ever
// downloaded twice, the first band paints within a few hundred KB, and the
// responses are cacheable so a repeat visit costs one 304 per band.
// ---------------------------------------------------------------------------
let catalogueBands = [];
let catalogueLoadStarted = false;
let cataloguePendingBand = 0;

async function fetchStarBand(band) {
    const url = new URL('/api/stars_bin', window.location.origin);
    url.searchParams.set('start', String(band.start));
    url.searchParams.set('end', String(band.end));
    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const buffer = await res.arrayBuffer();
    const decoded = decodeStarBand(buffer);
    return new Promise(resolve => ingestStarBand(band.start, decoded, resolve));
}

// Load the catalogue: the first (brightest) band is awaited so the sky appears
// straight away, the rest stream in behind it.
async function loadStarCatalogue() {
    if (catalogueLoadStarted) return;
    catalogueLoadStarted = true;
    starLoadingBegin();
    try {
        const res = await fetch('/api/stars_bands');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const meta = await res.json();
        catalogueBands = Array.isArray(meta.bands) ? meta.bands : [];
        starKnownCount = meta.known || 0;
        allocateStarCatalogue(meta.total || 0);

        // The slider now covers the real magnitude range of the catalogue
        if (typeof meta.minMag === 'number' && typeof meta.maxMag === 'number') {
            minMag = meta.minMag;
            maxMag = meta.maxMag;
            updateMagSliderRange(meta.minMag, meta.maxMag);
        }
        if (unknownMagCountLabel && meta.unknown) {
            unknownMagCountLabel.textContent = ` (${meta.unknown.toLocaleString()})`;
        }

        if (catalogueBands.length > 0) {
            await fetchStarBand(catalogueBands[0]);
            cataloguePendingBand = 1;
            scheduleDraw();
        }
    } catch (e) {
        console.error('Star catalogue index fetch failed:', e);
        catalogueLoadStarted = false;
    } finally {
        starLoadingEnd();
    }
}

// Pull in the remaining bands one at a time, yielding between each so the map
// stays interactive while several megabytes arrive.
async function loadRemainingStarBands() {
    if (cataloguePendingBand <= 0 || cataloguePendingBand >= catalogueBands.length) return;
    starLoadingBegin();
    try {
        for (; cataloguePendingBand < catalogueBands.length; cataloguePendingBand++) {
            const band = catalogueBands[cataloguePendingBand];
            updateStarLoadingProgress(band);
            try {
                await fetchStarBand(band);
            } catch (e) {
                console.warn(`Star band ${band.start}-${band.end} failed:`, e);
            }
            await new Promise(resolve => setTimeout(resolve, 0));
        }
        console.log(`Star catalogue loaded: ${starLoadedCount.toLocaleString()} objects`);
    } finally {
        updateStarLoadingProgress(null);
        starLoadingEnd();
    }
}

function updateStarLoadingProgress(band) {
    if (!starLoadingLabel) return;
    if (!band || !starCount) {
        starLoadingLabel.textContent = 'Loading stars…';
        return;
    }
    const pct = Math.min(100, Math.round((band.end / starCount) * 100));
    starLoadingLabel.textContent = `Loading stars… ${pct}%`;
}

// Kept for callers that ask for a deeper magnitude than is loaded yet: with
// band loading there is nothing extra to fetch, the data is already on its way.
async function fetchMoreStarsIfNeeded(newMagLimit) {
    if (!isFinite(newMagLimit)) return;
    if (cataloguePendingBand > 0 && cataloguePendingBand < catalogueBands.length) {
        loadRemainingStarBands();
    }
}

// After the first paint, stream in the rest of the catalogue
async function stagedPrefetchAfterFirstDraw() {
    try {
        await new Promise(requestAnimationFrame);
        // Names are small and cached for a day; fetching them up front is what
        // lets a click show an object's name immediately rather than after a
        // round trip. Not awaited - the catalogue is the bigger prize.
        loadStarNames();
        await loadRemainingStarBands();
    } catch (e) {
        console.warn('Staged prefetch encountered an issue:', e);
    }
}
