// Star map - working without a connection.
//
// The service worker (static/js/star_map_sw.js, served at /sw.js) stores the
// page, its scripts and the catalogue. This module registers it, makes sure
// everything the map needs offline has passed through it once, and stands in
// for the server where the map would otherwise ask it something:
//   * planet positions, interpolated from /api/planets_table
//   * search and its suggestions, over the names already loaded
// Telescope control and the full object details still need the server.

const offlineSupported = ('serviceWorker' in navigator) && window.isSecureContext;
const controlledAtLoad = offlineSupported && !!navigator.serviceWorker.controller;

function registerOfflineWorker() {
    if (!offlineSupported) return Promise.resolve(null);
    return navigator.serviceWorker.register('/sw.js', { scope: '/' })
        .catch(err => {
            console.warn('Offline support unavailable:', err);
            return null;
        });
}

// Resolves once a worker controls this page (on a first visit, when it has
// installed and claimed it), or null if that doesn't happen
function offlineWorkerControlling() {
    if (!offlineSupported) return Promise.resolve(null);
    if (navigator.serviceWorker.controller) return Promise.resolve(navigator.serviceWorker.controller);
    return new Promise(resolve => {
        const timer = setTimeout(() => resolve(null), 30000);
        navigator.serviceWorker.addEventListener('controllerchange', () => {
            clearTimeout(timer);
            resolve(navigator.serviceWorker.controller);
        }, { once: true });
    });
}

// ---- Connection badge -----------------------------------------------------

// navigator.onLine stays true with Wi-Fi but no route to the server (a phone
// hotspot at a dark site), so a failed request counts as offline too
let serverUnreachable = false;

function updateOfflineBadge() {
    const badge = document.getElementById('offline-badge');
    if (badge) badge.hidden = navigator.onLine && !serverUnreachable;
}

function noteServerReachable(reachable) {
    if (serverUnreachable === !reachable) return;
    serverUnreachable = !reachable;
    updateOfflineBadge();
}

window.addEventListener('online', () => noteServerReachable(true));
window.addEventListener('offline', updateOfflineBadge);
document.addEventListener('DOMContentLoaded', updateOfflineBadge);

// ---- Storing what the map needs ---------------------------------------------

function sameOrigin(url) {
    try { return new URL(url, location.href).origin === location.origin; } catch { return false; }
}

// Fetch each URL once through the worker, which stores it. Mostly answered
// by the browser's HTTP cache, since the map has just loaded the same things.
async function warmOfflineUrls(urls) {
    const queue = [...new Set(urls)].filter(sameOrigin);
    const worker = async () => {
        while (queue.length) {
            const url = queue.shift();
            try {
                const res = await fetch(url, { credentials: 'same-origin' });
                await res.arrayBuffer();
            } catch (e) {
                console.debug('Offline store skipped', url, e);
            }
        }
    };
    await Promise.all([worker(), worker(), worker()]);
}

function offlinePageAssetUrls() {
    const urls = [];
    document.querySelectorAll('script[src]').forEach(el => urls.push(el.src));
    document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"], link[rel="manifest"]')
        .forEach(el => urls.push(el.href));
    return urls;
}

function planetIconUrls() {
    const icons = new Set(planetsList.map(p => p.icon).filter(Boolean));
    for (const body of (planetTable && planetTable.bodies) || []) {
        if (body.icon) icons.add(body.icon);
    }
    return [...icons];
}

// Called once the whole catalogue has loaded. On the visit that installs the
// worker, the page and catalogue loaded before it took over, so they go
// through it again now; afterwards the map's own requests keep it current
// and only what the map loads lazily is fetched here.
async function prepareOfflineUse() {
    if (!offlineSupported || window.ZONE_EDITOR) return;
    const controller = await offlineWorkerControlling();
    if (!controller) return;

    const urls = [constellationsUrl(), planetTableUrl()];
    if (!controlledAtLoad) {
        urls.push(location.pathname + location.search, '/api/stars_bands', starNamesUrl(),
                  ...catalogueBands.map(starBandUrl), ...offlinePageAssetUrls());
    }
    await warmOfflineUrls(urls);
    await loadPlanetTable(true);
    await warmOfflineUrls(planetIconUrls());

    // An installed app may keep its storage; a browser tab is asked politely
    // only when it was opened as one
    try {
        const installed = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
        if (installed && navigator.storage && navigator.storage.persist) await navigator.storage.persist();
    } catch (e) {}
    console.log(controlledAtLoad ? 'Offline copy refreshed' : 'Star map saved for offline use');
}

// ---- Planets ------------------------------------------------------------------

let planetTable = null;
let planetTablePromise = null;

function planetTableUrl() {
    return `/api/planets_table?lang=${encodeURIComponent(window.I18N_LANG || 'en')}`;
}

function loadPlanetTable(refresh = false) {
    if (planetTable && !refresh) return Promise.resolve(planetTable);
    if (planetTablePromise) return planetTablePromise;
    planetTablePromise = fetch(planetTableUrl())
        .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
        })
        .then(table => {
            planetTable = table;
            return table;
        })
        .catch(err => {
            console.warn('Planet table unavailable:', err);
            return planetTable;
        })
        .finally(() => { planetTablePromise = null; });
    return planetTablePromise;
}

function lerpAngleDeg(a, b, f) {
    let d = b - a;
    if (d > 180) d -= 360;
    else if (d < -180) d += 360;
    return ((a + d * f) % 360 + 360) % 360;
}

function lerpSample(values, i, f) {
    if (!values) return undefined;
    const a = values[i], b = values[i + 1];
    if (a == null || b == null) return a == null ? b : a;
    return a + (b - a) * f;
}

// Positions for `date` in /api/planets' shape, or null outside the table
function planetsFromTable(date) {
    const table = planetTable;
    if (!table || !table.count || !Array.isArray(table.bodies)) return null;
    const t = (date.getTime() - table.startMs) / (table.stepMinutes * 60000);
    if (!(t >= 0 && t <= table.count - 1)) return null;
    const i = Math.min(Math.floor(t), table.count - 2);
    const f = t - i;
    const nearest = Math.round(t);
    return table.bodies.map(body => {
        const planet = {
            name: body.name,
            label: body.label,
            icon: body.icon,
            type: 'planet',
            ra: lerpAngleDeg(body.ra[i], body.ra[i + 1], f),
            dec: lerpSample(body.dec, i, f),
            mag: lerpSample(body.mag, i, f),
        };
        for (const field of ['phase_angle_deg', 'moon_illumination_fraction', 'moon_elongation_deg']) {
            const value = lerpSample(body[field], i, f);
            if (value != null) planet[field] = value;
        }
        if (body.phase_name) {
            let phase = null;
            for (const [from, name] of body.phase_name) {
                if (from > nearest) break;
                phase = name;
            }
            if (phase) planet.phase_name = phase;
        }
        return planet;
    });
}

// What fetchPlanetsForDate falls back to when the server can't be reached
async function offlinePlanetsForDate(date) {
    noteServerReachable(false);
    await loadPlanetTable();
    const planets = planetsFromTable(date);
    if (!planets) console.warn(`No stored planet positions for ${date.toISOString()}`);
    return planets;
}

// ---- Search ---------------------------------------------------------------------

const LOCAL_SEARCH_KIND_ORDER = { body: 0, constellation: 1, catalog: 2 };

function localSearchRank(query, name) {
    const folded = searchFold(name);
    if (!folded) return null;
    if (folded === query || folded.replace(/\s+/g, '') === query.replace(/\s+/g, '')) return 0;
    if (folded.startsWith(query)) return 1;
    if (folded.includes(query)) return 2;
    return null;
}

// Matches in /api/search_suggest's shape ({kind, key, name, matched, match}),
// best first: exact names, then prefixes, then anywhere in the name; a
// catalogue number is looked up in the whole catalogue
async function localSearchMatches(query, limit = 8) {
    noteServerReachable(false);
    const q = searchFold(query);
    if (q.length < 2) return [];
    await Promise.all([loadStarNames(), loadConstellations()]);

    const candidates = [];
    for (const p of planetsList) {
        candidates.push({ kind: 'body', key: p.name.toLowerCase(), name: p.label || p.name, names: [p.label, p.name], mag: p.mag });
    }
    for (const c of constellationFigures || []) {
        candidates.push({ kind: 'constellation', key: c.id, name: c.name, names: [c.name, c.id], mag: null });
    }
    for (const s of starNameEntries || []) {
        candidates.push({ kind: 'catalog', key: s.id, name: s.name, names: [s.name, s.bayer, s.id], mag: s.mag });
    }

    const matches = [];
    for (const cand of candidates) {
        let best = null;
        for (const name of cand.names) {
            const rank = localSearchRank(q, name);
            if (rank !== null && (!best || rank < best.rank)) best = { rank, matched: name };
        }
        if (best) matches.push({ kind: cand.kind, key: cand.key, name: cand.name, matched: best.matched,
                                 match: 'name', rank: best.rank, mag: cand.mag });
    }

    if (!matches.some(m => m.rank === 0)) {
        const raw = String(query).trim();
        let index = findStarByName(raw);
        if (index < 0) index = findStarByName(raw.replace(/\s+/g, ''));
        if (index >= 0) {
            const id = starNameAt(index);
            const named = starNameById.get(id);
            matches.push({ kind: 'catalog', key: id, name: named ? named.name : id, matched: id,
                           match: 'number', rank: 0, mag: Number.isNaN(starMag[index]) ? null : starMag[index] });
        }
    }

    matches.sort((a, b) => a.rank - b.rank
        || LOCAL_SEARCH_KIND_ORDER[a.kind] - LOCAL_SEARCH_KIND_ORDER[b.kind]
        || (a.mag == null ? 99 : a.mag) - (b.mag == null ? 99 : b.mag));
    const seen = new Set();
    return matches
        .filter(m => !seen.has(m.kind + ':' + m.key) && seen.add(m.kind + ':' + m.key))
        .slice(0, limit)
        .map(({ kind, key, name, matched, match }) => ({ kind, key, name, matched, match }));
}

// One object's search data in /interface/search_object's shape, or null
function localSearchObjectData(kind, key) {
    if (kind === 'body') {
        const p = planetsList.find(body => body.name.toLowerCase() === String(key).toLowerCase());
        if (!p) return null;
        return { Name: p.name, RA: p.ra, DEC: p.dec, 'V-Mag': p.mag, type: 'planet', label: p.label };
    }
    if (kind === 'constellation') {
        const c = (constellationFigures || []).find(fig => fig.id === key);
        if (!c || !c.anchorRaDec) return null;
        return { Name: c.name, RA: c.anchorRaDec[0], DEC: c.anchorRaDec[1], 'V-Mag': null,
                 type: 'constellation', constellation: key, label: c.name };
    }
    const index = findStarByName(key);
    if (index < 0) return null;
    const star = starObjectAt(index);
    const named = starNameById.get(star.name);
    const data = { Name: star.name, RA: star.ra, DEC: star.dec, 'V-Mag': star.mag, type: 'star' };
    if (named && named.name) data.friendlyName = named.name;
    if (named && named.bayer) data.bayer = named.bayer;
    return data;
}

// What searchObject falls back to when /interface/search_object can't be reached
async function localSearchObject(query, pick) {
    let kind, key, best = null, alternatives = [];
    if (pick) {
        noteServerReachable(false);
        kind = pick.kind;
        key = pick.key;
        await Promise.all([loadStarNames(), loadConstellations()]);
    } else {
        const matches = await localSearchMatches(query, 6);
        if (!matches.length) return { status: 'error' };
        [best, ...alternatives] = matches;
        kind = best.kind;
        key = best.key;
    }
    const data = localSearchObjectData(kind, key);
    if (!data) return { status: 'error' };
    data.kind = kind;
    if (best) data.match = { query, matched: best.matched, match: best.match };
    data.alternatives = alternatives;
    return { status: 'success', data };
}

registerOfflineWorker();
