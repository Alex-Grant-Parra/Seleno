// Shared by the workspace pages (interface and album): the Basic/Advanced toggle and
// small helpers. The mode is remembered in localStorage['seleno_mode'] and set as
// data-ui-mode on <html> and <body>; .basic-only and .adv-only hide by mode.
(function () {
    'use strict';

    const MODE_KEY = 'seleno_mode';
    const MODES = ['basic', 'advanced'];

    function storedMode() {
        try {
            const mode = localStorage.getItem(MODE_KEY);
            return MODES.includes(mode) ? mode : 'basic';
        } catch (e) {
            return 'basic';
        }
    }

    function applyMode(mode) {
        document.documentElement.setAttribute('data-ui-mode', mode);
        if (document.body) document.body.setAttribute('data-ui-mode', mode);
        document.querySelectorAll('[data-mode-toggle] button[data-mode]').forEach(btn => {
            const on = btn.dataset.mode === mode;
            btn.classList.toggle('on', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    function setMode(mode) {
        if (!MODES.includes(mode)) return;
        try { localStorage.setItem(MODE_KEY, mode); } catch (e) { /* private window */ }
        applyMode(mode);
        document.dispatchEvent(new CustomEvent('seleno:mode', { detail: { mode } }));
    }

    function mode() {
        return document.documentElement.getAttribute('data-ui-mode') || storedMode();
    }

    // Build an element: el('button', {className: 'ws-btn', text: 'Go', onclick: fn}, [children])
    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        Object.entries(attrs || {}).forEach(([key, value]) => {
            if (value === undefined || value === null || value === false) return;
            if (key === 'className') node.className = value;
            else if (key === 'text') node.textContent = value;
            else if (key === 'html') node.innerHTML = value;
            else if (key === 'dataset') Object.assign(node.dataset, value);
            else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
            else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? '' : value);
        });
        (Array.isArray(children) ? children : [children]).forEach(child => {
            if (child === null || child === undefined || child === false) return;
            node.appendChild(typeof child === 'string' || typeof child === 'number' ? document.createTextNode(String(child)) : child);
        });
        return node;
    }

    // JSON in and out. Resolves to the body; rejects with an Error carrying .status and .body
    async function api(url, options = {}) {
        const init = { method: options.method || (options.json !== undefined ? 'POST' : 'GET'), headers: {} };
        if (options.json !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(options.json);
        } else if (options.body !== undefined) {
            init.body = options.body;
            if (options.contentType) init.headers['Content-Type'] = options.contentType;
        }
        if (options.signal) init.signal = options.signal;
        const response = await fetch(url, init);
        if (response.status === 401) {
            const error = new Error('You need to log in again.');
            error.status = 401;
            throw error;
        }
        let body = null;
        const type = response.headers.get('Content-Type') || '';
        if (type.includes('application/json')) {
            body = await response.json();
        }
        if (!response.ok || (body && body.status === 'error')) {
            const error = new Error((body && body.message) || `${response.status} ${response.statusText}`);
            error.status = response.status;
            error.body = body;
            throw error;
        }
        return body;
    }

    function toast(message, level = 'info', ms = 4200) {
        let box = document.querySelector('.ws-toasts');
        if (!box) {
            box = el('div', { className: 'ws-toasts', role: 'status', 'aria-live': 'polite' });
            document.body.appendChild(box);
        }
        const item = el('div', { className: `ws-toast ${level}`, text: message });
        box.appendChild(item);
        setTimeout(() => item.remove(), ms);
    }

    const pad = (n) => String(n).padStart(2, '0');

    const fmt = {
        bytes(bytes) {
            if (bytes === undefined || bytes === null || isNaN(bytes)) return '—';
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let value = Number(bytes);
            let unit = 0;
            while (value >= 1024 && unit < units.length - 1) {
                value /= 1024;
                unit++;
            }
            return `${value.toFixed(unit && value < 100 ? 1 : 0)} ${units[unit]}`;
        },
        duration(seconds) {
            seconds = Math.max(0, Math.round(seconds || 0));
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = seconds % 60;
            if (h) return `${h} h ${m} min`;
            if (m) return s && m < 10 ? `${m} min ${s} s` : `${m} min`;
            return `${s} s`;
        },
        exposure(seconds) {
            if (seconds === undefined || seconds === null || seconds === '') return '—';
            const s = Number(seconds);
            if (!isFinite(s) || s <= 0) return '—';
            if (s < 0.3) return `1/${Math.round(1 / s)} s`;
            return `${+s.toFixed(s < 10 ? 1 : 0)} s`;
        },
        time(value) {
            const d = value instanceof Date ? value : new Date(value);
            return isNaN(d) ? '—' : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
        },
        dayMonth(value) {
            const d = value instanceof Date ? value : new Date(value);
            return isNaN(d) ? '—' : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
        },
        dateTime(value) {
            const d = value instanceof Date ? value : new Date(value);
            return isNaN(d) ? '—' : `${d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        },
        deg(value, digits = 0) {
            return value === undefined || value === null || isNaN(value) ? '—' : `${Number(value).toFixed(digits)}°`;
        },
        pct(value, digits = 0) {
            return value === undefined || value === null || isNaN(value) ? '—' : `${(Number(value) * 100).toFixed(digits)}%`;
        },
        num(value, digits = 2) {
            return value === undefined || value === null || isNaN(value) ? '—' : Number(value).toFixed(digits);
        },
        // Right ascension in degrees as "05 35 17.3", declination as "−05 23 28"
        ra(deg) {
            if (deg === undefined || deg === null || isNaN(deg)) return '—';
            let h = ((Number(deg) % 360) + 360) % 360 / 15;
            const hh = Math.floor(h);
            const m = (h - hh) * 60;
            const mm = Math.floor(m);
            const ss = (m - mm) * 60;
            return `${pad(hh)} ${pad(mm)} ${ss.toFixed(1).padStart(4, '0')}`;
        },
        dec(deg) {
            if (deg === undefined || deg === null || isNaN(deg)) return '—';
            const sign = deg < 0 ? '−' : '+';
            const a = Math.abs(Number(deg));
            const dd = Math.floor(a);
            const m = (a - dd) * 60;
            const mm = Math.floor(m);
            const ss = Math.round((m - mm) * 60);
            return `${sign}${pad(dd)} ${pad(mm)} ${pad(Math.min(ss, 59))}`;
        }
    };

    // "05 35 17.3" or "5h35m17s" or "83.82" (degrees) to degrees; null if unreadable
    function parseRA(text) {
        const s = String(text || '').trim();
        if (!s) return null;
        if (/^-?\d+(\.\d+)?$/.test(s) && !s.includes(' ')) return Number(s) % 360;
        const parts = s.replace(/[hms:]/gi, ' ').trim().split(/\s+/).map(Number);
        if (parts.some(isNaN) || parts[0] < 0 || parts[0] >= 24) return null;
        return (parts[0] + (parts[1] || 0) / 60 + (parts[2] || 0) / 3600) * 15;
    }

    function parseDec(text) {
        const s = String(text || '').trim().replace(/[−–]/g, '-');
        if (!s) return null;
        if (/^[-+]?\d+(\.\d+)?$/.test(s)) {
            const v = Number(s);
            return v >= -90 && v <= 90 ? v : null;
        }
        const negative = s.startsWith('-');
        const parts = s.replace(/^[-+]/, '').replace(/[°'"′″dms:]/gi, ' ').trim().split(/\s+/).map(Number);
        if (parts.some(isNaN)) return null;
        const v = parts[0] + (parts[1] || 0) / 60 + (parts[2] || 0) / 3600;
        if (v > 90) return null;
        return negative ? -v : v;
    }

    // A tiny line chart as SVG: series [{values: [...], color, width}], optional lines
    function sparkline(svg, series, options = {}) {
        const w = options.width || 300;
        const h = options.height || 64;
        const pad = options.pad === undefined ? 4 : options.pad;
        svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
        svg.setAttribute('preserveAspectRatio', 'none');
        svg.innerHTML = '';
        const all = series.flatMap(s => s.values.filter(v => v !== null && v !== undefined && isFinite(v)));
        if (!all.length) return;
        let lo = options.min !== undefined ? options.min : Math.min(...all);
        let hi = options.max !== undefined ? options.max : Math.max(...all);
        if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
        const n = Math.max(...series.map(s => s.values.length));
        const x = (i) => n <= 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad);
        const y = (v) => h - pad - ((v - lo) / (hi - lo)) * (h - 2 * pad);
        const ns = 'http://www.w3.org/2000/svg';
        (options.lines || []).forEach(line => {
            const l = document.createElementNS(ns, 'line');
            l.setAttribute('x1', 0); l.setAttribute('x2', w);
            l.setAttribute('y1', y(line.value)); l.setAttribute('y2', y(line.value));
            l.setAttribute('stroke', line.color || 'currentColor');
            l.setAttribute('stroke-dasharray', '5 4');
            l.setAttribute('vector-effect', 'non-scaling-stroke');
            svg.appendChild(l);
        });
        series.forEach(s => {
            const pts = [];
            s.values.forEach((v, i) => {
                if (v !== null && v !== undefined && isFinite(v)) pts.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
            });
            const p = document.createElementNS(ns, 'polyline');
            p.setAttribute('points', pts.join(' '));
            p.setAttribute('fill', 'none');
            p.setAttribute('stroke', s.color || 'currentColor');
            p.setAttribute('stroke-width', s.width || 2);
            p.setAttribute('stroke-linejoin', 'round');
            p.setAttribute('vector-effect', 'non-scaling-stroke');
            svg.appendChild(p);
            (s.marks || []).forEach(i => {
                const v = s.values[i];
                if (v === null || v === undefined) return;
                const c = document.createElementNS(ns, 'circle');
                c.setAttribute('cx', x(i)); c.setAttribute('cy', y(v)); c.setAttribute('r', s.markRadius || 4);
                c.setAttribute('fill', s.markColor || s.color || 'currentColor');
                svg.appendChild(c);
            });
        });
        return { x, y, lo, hi };
    }

    window.Seleno = { el, api, toast, fmt, parseRA, parseDec, sparkline, mode, setMode };

    applyMode(storedMode());
    document.addEventListener('DOMContentLoaded', () => {
        applyMode(storedMode());
        document.querySelectorAll('[data-mode-toggle] button[data-mode]').forEach(btn => {
            btn.addEventListener('click', () => setMode(btn.dataset.mode));
        });
    });
})();
