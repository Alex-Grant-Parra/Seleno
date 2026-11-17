// Seleno offline service worker, served at /sw.js (scope "/").
//
// Its only job is to keep the star map working without a connection:
//   * /StarMap        network first (4 s), then the stored copy
//   * star map assets stale-while-revalidate (scripts, i18n catalogue, icons)
//   * catalogue data  network first, then the stored copy; versioned star
//                     bands (/api/stars_bin?v=...) are immutable, so cache first
//   * other pages     straight to the network; offline, a small offline page
//
// Everything else (video, uploads, telescope APIs, POSTs, range requests) is
// never touched: an earlier worker that cached <video> range requests broke
// playback, so anything not listed below is left to the browser.

const CACHE_PREFIX = 'seleno-starmap-';
const PAGE_CACHE = CACHE_PREFIX + 'pages-v1';
const ASSET_CACHE = CACHE_PREFIX + 'assets-v1';
const DATA_CACHE = CACHE_PREFIX + 'data-v1';
const CURRENT_CACHES = [PAGE_CACHE, ASSET_CACHE, DATA_CACHE];

const STAR_MAP_PATH = '/StarMap';
const OFFLINE_PATH = '/offline';
const NETWORK_TIMEOUT_MS = 4000;

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
    if (request.method !== 'GET' || request.headers.has('range')) return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    if (request.mode === 'navigate') {
        event.respondWith(url.pathname === STAR_MAP_PATH ? starMapPage(event) : otherPage(event));
        return;
    }
    const path = url.pathname;
    if (path === STAR_MAP_PATH) {
        // offline.js fetching the page so it is stored on the visit that installed us
        event.respondWith(networkFirst(event, PAGE_CACHE, fetch(request)).then(res => res || Response.error()));
    } else if (path === '/api/stars_bin' && url.searchParams.has('v')) {
        event.respondWith(cacheFirst(event, DATA_CACHE, 'v'));
    } else if (DATA_PATHS.has(path)) {
        event.respondWith(networkFirst(event, DATA_CACHE, fetch(request)).then(res => res || Response.error()));
    } else if (ASSET_PREFIXES.some(prefix => path.startsWith(prefix))) {
        event.respondWith(staleWhileRevalidate(event, ASSET_CACHE));
    }
});

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
    const cache = await caches.open(PAGE_CACHE);
    return (await cache.match(OFFLINE_PATH, MATCH)) || Response.error();
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
