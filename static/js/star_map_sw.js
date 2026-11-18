// Seleno's service worker, served at /sw.js (scope "/"). Two jobs:
//
// 1. A telescope nearby (local mode). The "Nearby telescopes" screen (/local,
//    static/js/local-launcher.js) connects the app straight to a telescope's
//    Pi, over HTTPS at its own name (app/local_access.py on the server). From
//    then on every request the app makes goes to that Pi instead, with the
//    user's pass: the Pi runs a copy of this site with just that telescope, so
//    the interface, star map and album look and work as they do here, with or
//    without the internet. Only this worker, /local and its script stay ours.
//
// 2. The star map without a connection:
//   * /StarMap        network first (4 s), then the stored copy
//   * star map assets stale-while-revalidate (scripts, i18n catalogue, icons)
//   * catalogue data  network first, then the stored copy; versioned star
//                     bands (/api/stars_bin?v=...) are immutable, so cache first
//   * other pages     straight to the network; offline, the Nearby telescopes
//                     screen (which links to the star map)
//
// Everything else (video, uploads, telescope APIs, POSTs, range requests) is
// never touched outside local mode: an earlier worker that cached <video>
// range requests broke playback, so anything not listed below is left to the
// browser.

const CACHE_PREFIX = 'seleno-starmap-';
const PAGE_CACHE = CACHE_PREFIX + 'pages-v2';
const ASSET_CACHE = CACHE_PREFIX + 'assets-v1';
const DATA_CACHE = CACHE_PREFIX + 'data-v1';
const CURRENT_CACHES = [PAGE_CACHE, ASSET_CACHE, DATA_CACHE];

const STAR_MAP_PATH = '/StarMap';
const OFFLINE_PATH = '/offline';
const LAUNCHER_PATH = '/local';
const LAUNCHER_SCRIPT = '/static/js/local-launcher.js';
const NETWORK_TIMEOUT_MS = 4000;

const ASSET_PREFIXES = ['/static/js/star_map/', '/static/js/csrf-protection.js', '/i18n/catalog.js', '/static/icons/',
    LAUNCHER_SCRIPT];
const DATA_PATHS = new Set(['/api/stars_bands', '/api/stars_bin', '/api/star_names', '/api/constellations', '/api/planets_table']);

// Vary: Cookie on the language-dependent responses would otherwise stop every
// match: a service worker cannot see the cookie it was stored under
const MATCH = { ignoreVary: true };

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(PAGE_CACHE)
            .then(cache => cache.addAll([OFFLINE_PATH, LAUNCHER_PATH].map(path => new Request(path, { cache: 'reload' }))))
            .then(() => caches.open(ASSET_CACHE))
            .then(cache => cache.add(new Request(LAUNCHER_SCRIPT, { cache: 'reload' })))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys
            .filter(key => key.startsWith(CACHE_PREFIX) && !CURRENT_CACHES.includes(key))
            .map(key => caches.delete(key)));
        if (self.registration.navigationPreload) {
            await self.registration.navigationPreload.enable();
        }
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (url.origin === self.location.origin && !ownPath(url.pathname)) {
        if (localState) {
            event.respondWith(forward(event));
            return;
        }
        if (localState === undefined) {
            // Just started: the stored local mode isn't read yet
            event.respondWith(localReady.then(state => state ? forward(event) : (route(event) || fetch(event.request))));
            return;
        }
    }
    const handled = route(event);
    if (handled) event.respondWith(handled);
});

// The star map's handling (and the launcher's): a promise of the response, or
// null to leave the request to the browser
function route(event) {
    const request = event.request;
    if (request.method !== 'GET' || request.headers.has('range')) return null;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return null;

    if (request.mode === 'navigate') {
        if (url.pathname === STAR_MAP_PATH) return starMapPage(event);
        if (url.pathname === LAUNCHER_PATH) return launcherPage(event);
        return otherPage(event);
    }
    const path = url.pathname;
    if (path === STAR_MAP_PATH) {
        // offline.js fetching the page so it is stored on the visit that installed us
        return networkFirst(event, PAGE_CACHE, fetch(request)).then(res => res || Response.error());
    } else if (path === '/api/stars_bin' && url.searchParams.has('v')) {
        return cacheFirst(event, DATA_CACHE, 'v');
    } else if (DATA_PATHS.has(path)) {
        return networkFirst(event, DATA_CACHE, fetch(request)).then(res => res || Response.error());
    } else if (ASSET_PREFIXES.some(prefix => path.startsWith(prefix))) {
        return staleWhileRevalidate(event, ASSET_CACHE);
    }
    return null;
}

// --- Local mode: a telescope nearby -------------------------------------------

// Not under CACHE_PREFIX: activating a new version mustn't forget the connection
const LOCAL_CACHE = 'seleno-local-v1';
const LOCAL_STATE_KEY = '/__seleno_local__/state';
// Ours even in local mode: this worker, the launcher, its script, the list of
// telescopes, and the app's icons
const OWN_PATHS = ['/sw.js', LAUNCHER_PATH, LAUNCHER_SCRIPT, '/api/local_telescopes', '/static/icons/', '/favicon.ico'];
const PASS_HEADER = 'X-Seleno-Pass';

let localState;  // undefined until read; null when not connected; else {origin, pass, telescope, since}
const localReady = readLocalState();

function ownPath(path) {
    return OWN_PATHS.some(own => own.endsWith('/') ? path.startsWith(own) : path === own);
}

async function readLocalState() {
    try {
        const stored = await (await caches.open(LOCAL_CACHE)).match(LOCAL_STATE_KEY);
        localState = stored ? await stored.json() : null;
    } catch (e) {
        localState = null;
    }
    return localState;
}

async function writeLocalState(state) {
    const cache = await caches.open(LOCAL_CACHE);
    if (state) {
        await cache.put(LOCAL_STATE_KEY, new Response(JSON.stringify(state), { headers: { 'Content-Type': 'application/json' } }));
    } else {
        await cache.delete(LOCAL_STATE_KEY);
    }
    localState = state || null;
}

// The launcher's messages: {type: 'seleno-local', action: 'connect' | 'disconnect' | 'state', ...},
// answered on the MessageChannel port it sends
self.addEventListener('message', event => {
    const data = event.data || {};
    if (data.type !== 'seleno-local') return;
    const reply = message => { if (event.ports && event.ports[0]) event.ports[0].postMessage(message); };
    event.waitUntil((async () => {
        try {
            if (data.action === 'connect') {
                const origin = new URL(data.origin).origin;
                if (!origin.startsWith('https://') || !data.pass) throw new Error('A telescope needs an https address and a pass');
                await writeLocalState({ origin, pass: String(data.pass), telescope: String(data.telescope || ''), since: Date.now() });
            } else if (data.action === 'disconnect') {
                await writeLocalState(null);
            } else {
                await localReady;
            }
            reply({ ok: true, state: localState || null });
        } catch (e) {
            reply({ ok: false, error: String(e && e.message || e) });
        }
    })());
});

// The same request, to the telescope, with the pass. Its cookies (the Pi's
// session, for its CSRF tokens) go with it: tel.<site> is the same site.
async function forward(event) {
    const request = event.request;
    const state = localState || await localReady;
    const url = new URL(request.url);
    const headers = new Headers();
    for (const [name, value] of request.headers) headers.set(name, value);
    headers.set(PASS_HEADER, state.pass);
    const init = { method: request.method, headers, mode: 'cors', credentials: 'include', redirect: 'follow', cache: 'no-store' };
    if (request.method !== 'GET' && request.method !== 'HEAD') init.body = await request.arrayBuffer();
    let response;
    try {
        response = await fetch(state.origin + url.pathname + url.search, init);
    } catch (e) {
        return request.mode === 'navigate' ? lostPage(state) : Response.error();
    }
    if (request.mode === 'navigate' && response.redirected) {
        // Show the page at its own address, so its relative links work
        const final = new URL(response.url);
        return Response.redirect(self.location.origin + final.pathname + final.search, 303);
    }
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers: new Headers(response.headers) });
}

function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function lostPage(state) {
    const name = escapeHtml(state.telescope || 'the telescope');
    const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SELENO</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box;
font-family:system-ui,-apple-system,sans-serif;background:#F4EFE6;color:#14213D;text-align:center}
@media (prefers-color-scheme: dark){body{background:#0d1424;color:#e8e4dc}}main{max-width:420px}p{opacity:.8;line-height:1.5}
a{display:inline-block;margin:6px;padding:10px 18px;border-radius:8px;border:2px solid currentColor;color:inherit;text-decoration:none}</style></head>
<body><main><h1>Can't reach ${name}</h1><p>Check that this phone is on the telescope's Wi-Fi, or on the same Wi-Fi as it, and that the telescope is on.</p>
<a href="javascript:location.reload()">Try again</a><a href="${LAUNCHER_PATH}">Telescopes</a></main></body></html>`;
    return new Response(html, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}

// --- Strategies -------------------------------------------------------------

function cacheable(response) {
    return response && response.status === 200 && response.type === 'basic';
}

// Store a copy of a good response while handing the original on
function storing(event, cacheName, request, networkPromise, versionParam) {
    return networkPromise.then(response => {
        if (cacheable(response)) {
            const copy = response.clone();
            event.waitUntil(caches.open(cacheName).then(async cache => {
                await cache.put(request, copy);
                if (versionParam) await pruneOtherVersions(cache, request, versionParam);
            }));
        }
        return response;
    });
}

// Drop stored copies of the same URL with a different version: a new star
// catalogue or a script bump would otherwise pile up the old ones
async function pruneOtherVersions(cache, request, versionParam) {
    const url = new URL(request.url);
    const version = url.searchParams.get(versionParam);
    const sameFamily = other => {
        if (other.pathname !== url.pathname) return false;
        // Star bands differ by start/end too: any band of another catalogue goes
        if (versionParam === 'v' && url.pathname === '/api/stars_bin') return true;
        const a = new URLSearchParams(url.search), b = new URLSearchParams(other.search);
        a.delete(versionParam); b.delete(versionParam);
        a.sort(); b.sort();
        return a.toString() === b.toString();
    };
    for (const key of await cache.keys()) {
        const other = new URL(key.url);
        if (sameFamily(other) && other.searchParams.get(versionParam) !== version) {
            await cache.delete(key);
        }
    }
}

const TIMED_OUT = Symbol('timed out');
const FAILED = Symbol('failed');

// The network's answer if it comes within the timeout, otherwise the stored
// copy; with neither, wait out the network. Resolves to null on no answer.
async function networkFirst(event, cacheName, networkPromise, fallbackMatch) {
    const request = event.request;
    const network = storing(event, cacheName, request, networkPromise);
    const first = await Promise.race([
        network.catch(() => FAILED),
        new Promise(resolve => setTimeout(() => resolve(TIMED_OUT), NETWORK_TIMEOUT_MS)),
    ]);
    const answered = first !== TIMED_OUT && first !== FAILED;
    // A server error (the proxy up, the app down) is no better than no answer
    if (answered && first.status < 500) return first;

    const cache = await caches.open(cacheName);
    const stored = (await cache.match(request, MATCH))
        || (fallbackMatch ? await cache.match(fallbackMatch, { ...MATCH, ignoreSearch: true }) : null);
    if (stored) return stored;
    if (answered) return first;
    if (first === FAILED) return null;
    return network.catch(() => null);
}

async function cacheFirst(event, cacheName, versionParam) {
    const cache = await caches.open(cacheName);
    const stored = await cache.match(event.request, MATCH);
    if (stored) return stored;
    return storing(event, cacheName, event.request, fetch(event.request), versionParam)
        .catch(() => Response.error());
}

async function staleWhileRevalidate(event, cacheName) {
    const cache = await caches.open(cacheName);
    const stored = await cache.match(event.request, MATCH);
    const url = new URL(event.request.url);
    const versionParam = url.searchParams.has('v') ? 'v' : null;
    const network = storing(event, cacheName, event.request, fetch(event.request), versionParam)
        .catch(() => null);
    if (stored) {
        event.waitUntil(network);
        return stored;
    }
    return (await network) || Response.error();
}

// --- Pages ------------------------------------------------------------------

function navigationResponse(event) {
    // With navigation preload the request is already on its way
    return Promise.resolve(event.preloadResponse)
        .then(preloaded => preloaded || fetch(event.request));
}

async function offlinePage() {
    // Offline, the app opens on Nearby telescopes (which links to the star map)
    const cache = await caches.open(PAGE_CACHE);
    return (await cache.match(LAUNCHER_PATH, MATCH)) || (await cache.match(OFFLINE_PATH, MATCH)) || Response.error();
}

async function launcherPage(event) {
    const response = await networkFirst(event, PAGE_CACHE, navigationResponse(event), LAUNCHER_PATH);
    return response || offlinePage();
}

async function starMapPage(event) {
    const response = await networkFirst(event, PAGE_CACHE, navigationResponse(event), STAR_MAP_PATH);
    return response || offlinePage();
}

async function otherPage(event) {
    try {
        return await navigationResponse(event);
    } catch (e) {
        return offlinePage();
    }
}
