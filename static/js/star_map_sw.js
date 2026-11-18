// Seleno's service worker, served at /sw.js (scope "/"). Two jobs:
//
// 1. The app without a connection. The pages people use at the telescope (the
//    interface, star map, album and Telescope Settings), the site's static
//    files and the CDN files they load are stored as they're visited, so the
//    installed app opens with no internet:
//   * those pages     network first (4 s), then the stored copy
//   * /static/, CDN   network first, then the stored copy (online always fresh)
//   * star map assets stale-while-revalidate (scripts, i18n catalogue, icons)
//   * catalogue data  network first, then the stored copy; versioned star
//                     bands (/api/stars_bin?v=...) are immutable, so cache first
//   * other pages     straight to the network; offline, a small offline page
//                     (the app's start page, "/": the interface, when stored)
//
// 2. A telescope nearby, on its own Wi-Fi (app/offline_access.py on the
//    server). static/js/offline-link.js finds it from the page and tells this
//    worker; from then on the requests that are about the telescope (the
//    paths its /hello lists: the interface's commands and status, tracking,
//    the album's files...) go to the telescope itself at its HTTPS name, with
//    the user's offline pass. Everything else, the star map's own data
//    included, stays the app's: the star map runs on the phone and only what
//    it wants tracked goes to the telescope.
//
// Never touched: video and other range requests, uploads, and POSTs that aren't
// for the telescope. (An earlier worker that cached <video> range requests
// broke playback.)

const CACHE_PREFIX = 'seleno-starmap-';
const PAGE_CACHE = CACHE_PREFIX + 'pages-v2';
const ASSET_CACHE = CACHE_PREFIX + 'assets-v1';
const STATIC_CACHE = CACHE_PREFIX + 'static-v1';
const DATA_CACHE = CACHE_PREFIX + 'data-v1';
const CURRENT_CACHES = [PAGE_CACHE, ASSET_CACHE, STATIC_CACHE, DATA_CACHE];

const STAR_MAP_PATH = '/StarMap';
const OFFLINE_PATH = '/offline';
const NETWORK_TIMEOUT_MS = 4000;
// Pages kept for offline use, and what each is stored as
const APP_PAGES = {
    '/StarMap': '/StarMap',
    '/interface': '/interface/',
    '/interface/': '/interface/',
    '/album': '/album',
    '/interface/telescope_settings': '/interface/telescope_settings/',
    '/interface/telescope_settings/': '/interface/telescope_settings/',
};
const CDN_HOSTS = new Set(['cdn.jsdelivr.net', 'cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com']);

const ASSET_PREFIXES = ['/static/js/star_map/', '/static/js/csrf-protection.js', '/i18n/catalog.js', '/static/icons/'];
const DATA_PATHS = new Set(['/api/stars_bands', '/api/stars_bin', '/api/star_names', '/api/constellations', '/api/planets_table']);

// Vary: Cookie on the language-dependent responses would otherwise stop every
// match: a service worker cannot see the cookie it was stored under
const MATCH = { ignoreVary: true };

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(PAGE_CACHE)
            .then(cache => cache.add(new Request(OFFLINE_PATH, { cache: 'reload' })))
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
    const request = event.request;
    const url = new URL(request.url);

    if (url.origin === self.location.origin && request.mode !== 'navigate') {
        // Just started (the stored telescope isn't read yet), or the page is
        // checking the telescope is still there: hold its requests until we know
        if (telescope === undefined || (probing && telescope && forwards(url.pathname))) {
            event.respondWith(decided().then(() => telescope && forwards(url.pathname)
                ? forward(event) : (route(event) || fetch(request))));
            return;
        }
        if (telescope && forwards(url.pathname)) {
            event.respondWith(forward(event));
            return;
        }
    }
    const handled = route(event);
    if (handled) event.respondWith(handled);
});

// The app's own handling: a promise of the response, or null to leave the
// request to the browser
function route(event) {
    const request = event.request;
    if (request.method !== 'GET' || request.headers.has('range')) return null;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) {
        return CDN_HOSTS.has(url.hostname) ? networkFirstAny(event, STATIC_CACHE) : null;
    }
    const path = url.pathname;
    if (request.mode === 'navigate') {
        return APP_PAGES[path] ? appPage(event, APP_PAGES[path]) : otherPage(event);
    }
    if (path === STAR_MAP_PATH) {
        // offline.js fetching the page so it is stored on the visit that installed us
        return networkFirst(event, PAGE_CACHE, fetch(request)).then(res => res || Response.error());
    } else if (path === '/api/stars_bin' && url.searchParams.has('v')) {
        return cacheFirst(event, DATA_CACHE, 'v');
    } else if (DATA_PATHS.has(path)) {
        return networkFirst(event, DATA_CACHE, fetch(request)).then(res => res || Response.error());
    } else if (ASSET_PREFIXES.some(prefix => path.startsWith(prefix))) {
        return staleWhileRevalidate(event, ASSET_CACHE);
    } else if (path.startsWith('/static/') && !path.startsWith('/static/videos/')) {
        return networkFirst(event, STATIC_CACHE, fetch(request)).then(res => res || Response.error());
    }
    return null;
}

// --- A telescope nearby -------------------------------------------------------

// Not under CACHE_PREFIX: a new version of this worker mustn't forget it
const TELESCOPE_CACHE = 'seleno-telescope-v1';
const TELESCOPE_KEY = '/__seleno_telescope__';
const PASS_HEADER = 'X-Seleno-Pass';
const PROBE_WAIT_MS = 4000;

// undefined until read; null when none; else {telescope_id, name, origin, pass, paths: {exact, prefixes}}
let telescope;
let probing = null;  // {promise, resolve} while the page checks
const telescopeRead = readTelescope();

async function readTelescope() {
    try {
        const stored = await (await caches.open(TELESCOPE_CACHE)).match(TELESCOPE_KEY);
        telescope = stored ? await stored.json() : null;
    } catch (e) {
        telescope = null;
    }
    return telescope;
}

async function writeTelescope(state) {
    const cache = await caches.open(TELESCOPE_CACHE);
    if (state) {
        await cache.put(TELESCOPE_KEY, new Response(JSON.stringify(state), { headers: { 'Content-Type': 'application/json' } }));
    } else {
        await cache.delete(TELESCOPE_KEY);
    }
    telescope = state || null;
}

function decided() {
    const waits = [telescopeRead];
    if (probing) {
        waits.push(Promise.race([probing.promise, new Promise(resolve => setTimeout(resolve, PROBE_WAIT_MS))]));
    }
    return Promise.all(waits);
}

function forwards(path) {
    const paths = (telescope && telescope.paths) || {};
    const bare = path.length > 1 ? path.replace(/\/+$/, '') : path;
    return (paths.exact || []).includes(bare) || (paths.prefixes || []).some(prefix => path.startsWith(prefix));
}

// static/js/offline-link.js: {type: 'seleno-telescope', action: 'probing' | 'connect' | 'none' | 'state', state}
self.addEventListener('message', event => {
    const data = event.data || {};
    if (data.type !== 'seleno-telescope') return;
    const reply = message => { if (event.ports && event.ports[0]) event.ports[0].postMessage(message); };
    event.waitUntil((async () => {
        try {
            await telescopeRead;
            if (data.action === 'probing') {
                if (!probing) {
                    let resolve;
                    const promise = new Promise(r => { resolve = r; });
                    probing = { promise, resolve };
                    setTimeout(() => finishProbe(), PROBE_WAIT_MS * 2);
                }
            } else if (data.action === 'connect') {
                const state = data.state || {};
                const origin = new URL(state.origin).origin;
                if (!origin.startsWith('https://') || !state.pass) throw new Error('A telescope needs an https address and a pass');
                await writeTelescope({ telescope_id: String(state.telescope_id || ''), name: String(state.name || ''),
                    origin, pass: String(state.pass), paths: state.paths || {}, since: Date.now() });
                finishProbe();
            } else if (data.action === 'none') {
                await writeTelescope(null);
                finishProbe();
            }
            reply({ ok: true, state: telescope ? { telescope_id: telescope.telescope_id, name: telescope.name, origin: telescope.origin } : null });
        } catch (e) {
            finishProbe();
            reply({ ok: false, error: String(e && e.message || e) });
        }
    })());
});

function finishProbe() {
    if (probing) {
        probing.resolve();
        probing = null;
    }
}

// The same request, to the telescope, with the pass
async function forward(event) {
    const request = event.request;
    const state = telescope;
    const url = new URL(request.url);
    const headers = new Headers();
    for (const [name, value] of request.headers) {
        if (!['cookie', 'origin', 'referer'].includes(name.toLowerCase())) headers.set(name, value);
    }
    headers.set(PASS_HEADER, state.pass);
    const init = { method: request.method, headers, mode: 'cors', credentials: 'omit', redirect: 'follow', cache: 'no-store' };
    if (request.method !== 'GET' && request.method !== 'HEAD') init.body = await request.arrayBuffer();
    try {
        const response = await fetch(state.origin + url.pathname + url.search, init);
        return new Response(response.body, { status: response.status, statusText: response.statusText, headers: response.headers });
    } catch (e) {
        const body = JSON.stringify({ status: 'error', message: `Can't reach ${state.name || 'the telescope'}: is this phone on its Wi-Fi?` });
        return new Response(body, { status: 503, headers: { 'Content-Type': 'application/json' } });
    }
}

// --- Strategies -------------------------------------------------------------

function cacheable(response) {
    return response && response.status === 200 && response.type === 'basic';
}

// Store a copy of a good response while handing the original on
function storing(event, cacheName, request, networkPromise, versionParam, accept = cacheable) {
    return networkPromise.then(response => {
        if (accept(response)) {
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
async function networkFirst(event, cacheName, networkPromise, fallbackMatch, accept = cacheable) {
    const request = event.request;
    const network = storing(event, cacheName, fallbackMatch ? new Request(fallbackMatch) : request, networkPromise,
        null, accept);
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

// CDN files: their responses are cross-origin (opaque when loaded without CORS)
function networkFirstAny(event, cacheName) {
    const ok = response => response && (response.type === 'opaque' || (response.status === 200 && response.type === 'cors'));
    return networkFirst(event, cacheName, fetch(event.request), null, ok).then(res => res || Response.error());
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
    const cache = await caches.open(PAGE_CACHE);
    return (await cache.match(OFFLINE_PATH, MATCH)) || Response.error();
}

// A page the app keeps: stored under one key whatever its query, so the copy
// is there however it was opened. Only a page that loaded (200) is stored: a
// login page or an error never replaces a good copy.
async function appPage(event, key) {
    const response = await networkFirst(event, PAGE_CACHE, navigationResponse(event), key,
        res => res && res.status === 200 && res.type === 'basic' && !res.redirected);
    return response || offlinePage();
}

// The installed app opens at "/", a page that isn't kept. With no connection
// (on a telescope's Wi-Fi, say) it goes on to the interface, which finds the
// telescope, when that's stored; otherwise the offline page
const START_PATH = '/';
const START_OFFLINE = '/interface/';

async function otherPage(event) {
    const start = new URL(event.request.url).pathname === START_PATH;
    try {
        if (!start) return await navigationResponse(event);
        const TIMED_OUT = {};
        const first = await Promise.race([
            navigationResponse(event),
            new Promise(resolve => setTimeout(() => resolve(TIMED_OUT), NETWORK_TIMEOUT_MS)),
        ]);
        if (first !== TIMED_OUT && first.status < 500) return first;
        throw new Error('no answer');
    } catch (e) {
        if (start && await (await caches.open(PAGE_CACHE)).match(START_OFFLINE, { ...MATCH, ignoreSearch: true })) {
            return Response.redirect(new URL(START_OFFLINE, self.location.origin).href, 302);
        }
        return offlinePage();
    }
}
