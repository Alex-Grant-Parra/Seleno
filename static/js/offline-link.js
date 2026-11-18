// The seleno.org app talking straight to a telescope on its own Wi-Fi
// (app/offline_access.py on the server, core/networking/phone_link.py on the
// telescope). Loaded early on every page by base.html.
//
// Online, it fetches the user's offline passes (/interface/offline_passes) and
// keeps them on the phone. It then looks for one of those telescopes nearby,
// at https://<its name>:8443/hello, and when one answers tells the service
// worker (static/js/star_map_sw.js), which sends the telescope's requests to it
// from then on: the interface's telescope list has just that telescope, and
// works as it does online. Looking is automatic when seleno.org can't be
// reached (the telescope's Wi-Fi has no internet), when the last page was
// connected, or once the browser allows talking to the local network.
// (SelenoLink.connectNearby() looks on demand: the tests, the console.)
(function () {
    'use strict';

    const STORE = 'seleno.offline.v1';
    const PROBE_MS = 2500;
    const PASSES_MS = 4000;
    const RELOADED = 'seleno.offline.reloaded';

    function load() {
        try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; }
    }

    function save(data) {
        try { localStorage.setItem(STORE, JSON.stringify(data)); } catch (e) { /* private mode */ }
    }

    const worker = ('serviceWorker' in navigator && window.isSecureContext) ? navigator.serviceWorker : null;

    function tell(action, state) {
        const controller = worker && worker.controller;
        if (!controller) return Promise.resolve(null);
        return new Promise(resolve => {
            const channel = new MessageChannel();
            const timer = setTimeout(() => resolve(null), 3000);
            channel.port1.onmessage = event => { clearTimeout(timer); resolve(event.data); };
            controller.postMessage({ type: 'seleno-telescope', action, state }, [channel.port2]);
        });
    }

    function timeout(ms) {
        if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) return AbortSignal.timeout(ms);
        const controller = new AbortController();
        setTimeout(() => controller.abort(), ms);
        return controller.signal;
    }

    function usable(entry, now) {
        return entry && entry.host && entry.pass && entry.exp * 1000 > now && entry.nbf * 1000 <= now + 300000;
    }

    async function probe(entry) {
        const origin = `https://${entry.host}:${entry.api_port || 8443}`;
        const response = await fetch(`${origin}/hello`, { mode: 'cors', cache: 'no-store', credentials: 'omit',
            signal: timeout(PROBE_MS) });
        const hello = await response.json();
        if (!response.ok || hello.service !== 'seleno-telescope' || hello.telescope !== entry.telescope_id) {
            throw new Error('Not this telescope');
        }
        return { telescope_id: entry.telescope_id, name: hello.name || entry.telescope_id, origin,
            pass: entry.pass, paths: hello.paths || {}, role: entry.role };
    }

    async function findNearby(entries) {
        const now = Date.now();
        const candidates = (entries || []).filter(entry => usable(entry, now));
        if (!candidates.length) return null;
        try {
            return await Promise.any(candidates.map(probe));
        } catch (e) {
            return null;
        }
    }

    async function refreshPasses(data) {
        // true when seleno.org answered (logged in or not)
        try {
            const response = await fetch('/interface/offline_passes', { cache: 'no-store', credentials: 'same-origin',
                headers: { Accept: 'application/json' }, signal: timeout(PASSES_MS) });
            if (response.status === 401) return true;
            const body = await response.json();
            if (body.status === 'success') {
                data.passes = body.telescopes || [];
                data.fetched = Date.now();
                save(data);
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    async function localNetworkAllowed() {
        try {
            const status = await navigator.permissions.query({ name: 'local-network-access' });
            return status.state === 'granted';
        } catch (e) {
            return false;  // a browser without that permission doesn't ask either
        }
    }

    const data = load();
    const link = {
        state: null,
        ready: null,
        passes: () => (load().passes || []),
        connectNearby,
        disconnect,
    };
    window.SelenoLink = link;

    async function connect(state, reload) {
        await tell('connect', state);
        link.state = state;
        const stored = load();
        stored.last = state.telescope_id;
        save(stored);
        if (reload) {
            try {
                if (!sessionStorage.getItem(RELOADED)) {
                    sessionStorage.setItem(RELOADED, '1');
                    location.reload();
                }
            } catch (e) { location.reload(); }
        }
        return state;
    }

    async function connectNearby() {
        // From a tap, so the browser may ask about the local network
        const stored = load();
        if (navigator.onLine) await refreshPasses(stored);
        const found = await findNearby(load().passes);
        if (!found) return null;
        try { sessionStorage.removeItem(RELOADED); } catch (e) {}
        return connect(found, true);
    }

    async function disconnect() {
        await tell('none');
        link.state = null;
        const stored = load();
        stored.last = null;
        save(stored);
        location.reload();
    }

    async function start() {
        const passes = data.passes || [];
        if (data.last) {
            // Connected last time: is it still there? (the worker waits for the answer)
            await tell('probing');
            const entry = passes.find(p => p.telescope_id === data.last);
            const found = entry ? await findNearby([entry]) : null;
            if (found) return connect(found, false);
            await tell('none');
            data.last = null;
            save(data);
        }
        try { sessionStorage.removeItem(RELOADED); } catch (e) {}
        const reached = navigator.onLine ? await refreshPasses(data) : false;
        if (!(load().passes || []).length) return null;
        if (reached && !(await localNetworkAllowed())) return null;
        const found = await findNearby(load().passes);
        return found ? connect(found, true) : null;
    }

    link.ready = start().catch(() => null);

    // The menu: who's connected
    document.addEventListener('DOMContentLoaded', () => {
        const badge = document.getElementById('offlineLinkBadge');
        const show = () => {
            const state = link.state;
            if (badge) {
                badge.classList.toggle('d-none', !state);
                if (state) {
                    badge.querySelector('[data-name]').textContent = state.name;
                }
            }
        };
        show();
        link.ready.then(show);
        if (badge) {
            const off = badge.querySelector('[data-disconnect]');
            if (off) off.addEventListener('click', disconnect);
        }
    });
})();
