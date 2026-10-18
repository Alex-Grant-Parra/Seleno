// Frame types, grouping (by night, target or plan), and judging each frame's quality
// against the rest of its group, with the same rules the stacker uses to leave
// frames out (app/processing/stack.py quality_rejections).

export const TYPE_LABELS = {
    light: 'Lights', dark: 'Darks', flat: 'Flats', darkflat: 'Dark flats', bias: 'Bias',
    snapshot: 'Snapshots', test: 'Test shots', bracket: 'Brackets', timelapse: 'Timelapse',
    video: 'Video', processed: 'Processed', other: 'Other', unknown: 'Not sorted'
};
export const TYPE_SINGULAR = {
    light: 'light', dark: 'dark', flat: 'flat', darkflat: 'dark flat', bias: 'bias frame',
    snapshot: 'snapshot', test: 'test shot', bracket: 'bracket', timelapse: 'timelapse frame',
    video: 'video', processed: 'processed image', other: 'other', unknown: 'photo'
};
export const CALIBRATION = ['dark', 'flat', 'darkflat', 'bias'];
export const SETTABLE_TYPES = ['light', 'dark', 'flat', 'darkflat', 'bias', 'snapshot', 'test', 'processed', 'other'];
export const TYPE_ORDER = ['light', 'unknown', 'dark', 'flat', 'darkflat', 'bias', 'bracket', 'timelapse', 'snapshot', 'test', 'video', 'processed', 'other'];

export function typeOf(frame) {
    if (frame.kind === 'video' && !frame.frame_type) return 'video';
    return frame.frame_type || 'unknown';
}

// Lights, and photos nobody has given a type (old uploads), are what gets stacked
export function isLightLike(frame) {
    const type = typeOf(frame);
    return type === 'light' || type === 'unknown';
}

export function frameTime(frame) {
    const t = new Date(frame.captured_at || frame.uploaded_at || 0).getTime();
    return isNaN(t) ? 0 : t;
}

const pad = (n) => String(n).padStart(2, '0');

function dayKey(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// A night runs from noon to noon, named after its evening
export function nightKey(frame) {
    const t = frameTime(frame);
    return t ? dayKey(new Date(t - 12 * 3600 * 1000)) : 'unknown';
}

export function currentNightKey() {
    return dayKey(new Date(Date.now() - 12 * 3600 * 1000));
}

export function nightLabel(key, long = false) {
    if (key === 'unknown') return 'No date';
    const [y, m, d] = key.split('-').map(Number);
    const date = new Date(y, m - 1, d);
    const sameYear = y === new Date().getFullYear();
    const short = date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: sameYear ? undefined : 'numeric' });
    if (key === currentNightKey()) return long ? `tonight, ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })}` : `Tonight, ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}`;
    return long ? `the night of ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: sameYear ? undefined : 'numeric' })}` : short;
}

export function targetKey(frame) {
    return String(frame.target || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

export function groupKeyOf(frame, by) {
    if (by === 'target') return `target:${targetKey(frame)}`;
    if (by === 'plan') return `plan:${frame.plan_id || ''}`;
    return `night:${nightKey(frame)}`;
}

// [{key, label, frames}] for one way of grouping
export function groupFrames(frames, by, plans = {}) {
    const groups = new Map();
    frames.forEach(frame => {
        const key = groupKeyOf(frame, by);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(frame);
    });
    const out = [...groups.entries()].map(([key, list]) => {
        const value = key.slice(key.indexOf(':') + 1);
        let label;
        if (by === 'night') label = nightLabel(value);
        else if (by === 'target') label = value ? (list.find(f => f.target) || {}).target : 'No target';
        else label = value ? ((plans[value] && plans[value].name) || value) : 'Not from a plan';
        return { key, value, label, frames: list, latest: Math.max(...list.map(frameTime)) };
    });
    if (by === 'target') out.sort((a, b) => (a.value === '') - (b.value === '') || b.frames.length - a.frames.length);
    else out.sort((a, b) => (a.value === '' || a.value === 'unknown') - (b.value === '' || b.value === 'unknown') || b.latest - a.latest);
    return out;
}

export function countTypes(frames) {
    const counts = {};
    frames.forEach(f => { const t = typeOf(f); counts[t] = (counts[t] || 0) + 1; });
    return counts;
}

export function mostCommon(values) {
    const counts = new Map();
    values.filter(Boolean).forEach(v => counts.set(v, (counts.get(v) || 0) + 1));
    let best = null;
    counts.forEach((n, v) => { if (!best || n > best[1]) best = [v, n]; });
    return best ? best[0] : null;
}

function median(values) {
    const v = values.filter(x => x !== null && x !== undefined && isFinite(x)).sort((a, b) => a - b);
    if (!v.length) return null;
    const m = v.length >> 1;
    return v.length % 2 ? v[m] : 0.5 * (v[m - 1] + v[m]);
}

export function robust(values) {
    const med = median(values);
    if (med === null) return { median: null, sigma: null };
    const mad = median(values.filter(x => x !== null && x !== undefined && isFinite(x)).map(x => Math.abs(x - med)));
    return { median: med, sigma: 1.4826 * mad };
}

function metric(frame, key) {
    const m = frame.metrics;
    const v = m ? m[key] : null;
    return v === null || v === undefined || !isFinite(v) ? null : Number(v);
}

// Typical values among a group's lights, for judging each one
export function groupStats(frames) {
    const lights = frames.filter(f => isLightLike(f) && f.metrics);
    return {
        count: lights.length,
        fwhm: robust(lights.map(f => metric(f, 'fwhm'))),
        ecc: robust(lights.map(f => metric(f, 'ecc'))),
        stars: robust(lights.map(f => metric(f, 'stars'))),
        background: robust(lights.map(f => metric(f, 'background_pct')))
    };
}

// {level: ok|warn|bad|off, reason} for each frame of a group, by name
export function evaluate(frames) {
    const stats = groupStats(frames);
    const enough = stats.count >= 5;
    const out = new Map();
    frames.forEach(frame => {
        if (frame.flag === 'reject') {
            out.set(frame.name, { level: 'bad', reason: 'Rejected', user: true });
            return;
        }
        if (frame.flag === 'pick') {
            out.set(frame.name, { level: 'ok', reason: 'Accepted', user: true });
            return;
        }
        if (!frame.metrics || !isLightLike(frame)) {
            out.set(frame.name, { level: 'off', reason: frame.metrics ? '' : 'Not measured' });
            return;
        }
        const fwhm = metric(frame, 'fwhm');
        const ecc = metric(frame, 'ecc');
        const stars = metric(frame, 'stars');
        if (stars !== null && stars < 5) {
            out.set(frame.name, { level: 'bad', reason: 'Hardly any stars' });
            return;
        }
        if (enough) {
            const f = stats.fwhm, e = stats.ecc, s = stats.stars;
            // Trailing first: trailed stars are also wide, and "trailed" says why
            if (ecc !== null && e.median !== null && ecc > Math.max(0.6, e.median + 3 * e.sigma)) {
                out.set(frame.name, { level: 'bad', reason: 'Stars trailed' });
                return;
            }
            if (fwhm !== null && f.median !== null && fwhm > f.median + Math.max(3 * f.sigma, 0.15 * f.median)) {
                out.set(frame.name, { level: 'bad', reason: 'Blurred' });
                return;
            }
            if (stars !== null && s.median !== null && stars < 0.5 * s.median) {
                out.set(frame.name, { level: 'bad', reason: 'Few stars: cloud?' });
                return;
            }
            if ((fwhm !== null && f.median !== null && fwhm > f.median + Math.max(1.5 * f.sigma, 0.08 * f.median)) ||
                (ecc !== null && e.median !== null && ecc > Math.max(0.5, e.median + 1.5 * e.sigma)) ||
                (stars !== null && s.median !== null && stars < 0.75 * s.median)) {
                out.set(frame.name, { level: 'warn', reason: 'Below average' });
                return;
            }
            out.set(frame.name, { level: 'ok', reason: '' });
            return;
        }
        // Too few frames to compare: absolute limits only
        if (ecc !== null && ecc >= 0.6) out.set(frame.name, { level: 'bad', reason: 'Stars trailed' });
        else if (fwhm !== null && fwhm > 6.5) out.set(frame.name, { level: 'bad', reason: 'Out of focus' });
        else if ((ecc !== null && ecc >= 0.45) || (fwhm !== null && fwhm > 4)) out.set(frame.name, { level: 'warn', reason: '' });
        else out.set(frame.name, { level: 'ok', reason: '' });
    });
    return out;
}

// A plain sentence about one frame, for Basic mode's inspector
export function describe(frame, quality, frames) {
    if (frame.flag === 'reject') return 'You marked this one as bad, so stacking leaves it out.';
    if (!frame.metrics) return isLightLike(frame) ? "Its stars haven't been measured yet." : '';
    const stats = groupStats(frames);
    const fwhm = metric(frame, 'fwhm');
    const ecc = metric(frame, 'ecc');
    if (quality.level === 'bad') {
        if (quality.reason === 'Blurred') return `Blurrier than the rest of the night (stars ${fwhm.toFixed(1)} px against ${stats.fwhm.median.toFixed(1)}). Stacking leaves it out.`;
        if (quality.reason === 'Stars trailed') return 'The stars are stretched: the mount slipped or something knocked it. Stacking leaves it out.';
        if (quality.reason === 'Few stars: cloud?') return 'Far fewer stars than the others: probably cloud or haze. Stacking leaves it out.';
        return 'Stacking leaves this one out.';
    }
    const round = ecc !== null && ecc < 0.45;
    if (stats.count >= 5 && fwhm !== null && fwhm <= stats.fwhm.median - 0.5 * (stats.fwhm.sigma || 0)) {
        return `${round ? 'Sharp, round stars.' : 'Sharp stars.'} One of the best photos of the night.`;
    }
    if (quality.level === 'warn') return 'A little softer than the best ones, but still worth stacking.';
    return round ? 'Round stars and good focus.' : 'Fine to stack.';
}

// Limits a night suggests: median + 3 sigma (and the stacker's floors)
export function suggestLimits(frames) {
    const stats = groupStats(frames);
    if (!stats.count) return null;
    const f = stats.fwhm, e = stats.ecc, s = stats.stars;
    return {
        fwhm: f.median !== null ? +(f.median + Math.max(3 * f.sigma, 0.15 * f.median)).toFixed(2) : null,
        ecc: e.median !== null ? +Math.max(0.6, e.median + 3 * e.sigma).toFixed(2) : null,
        stars: s.median !== null ? Math.round(0.5 * s.median) : null
    };
}

export function overLimits(frames, limits) {
    return frames.filter(f => {
        if (!isLightLike(f) || !f.metrics || f.flag === 'reject') return false;
        const fwhm = metric(f, 'fwhm'), ecc = metric(f, 'ecc'), stars = metric(f, 'stars');
        return (limits.fwhm && fwhm !== null && fwhm > limits.fwhm) ||
            (limits.ecc && ecc !== null && ecc > limits.ecc) ||
            (limits.stars && stars !== null && stars < limits.stars);
    });
}

// FWHM, eccentricity and background of a group's lights in time order, each series on
// its own scale; red dots on frames over the limits
export function drawQualityChart(svg, frames, limits) {
    const ns = 'http://www.w3.org/2000/svg';
    // Drawn at the chart's real size, so the dots stay round
    const W = Math.max(200, Math.round(svg.clientWidth || 800)), H = Math.max(60, Math.round(svg.clientHeight || 130)), pad = 8;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.innerHTML = '';
    const lights = frames.filter(f => isLightLike(f) && f.metrics).sort((a, b) => frameTime(a) - frameTime(b));
    if (lights.length < 2) return lights.length;
    const x = (i) => pad + (i / (lights.length - 1)) * (W - 2 * pad);
    const series = [
        { key: 'fwhm', color: 'var(--ws-series-1)', band: [0, 0.6] },
        { key: 'ecc', color: 'var(--ws-series-2)', band: [0.55, 0.85] },
        { key: 'background_pct', color: 'var(--ws-series-3)', band: [0.8, 1] }
    ];
    const over = new Set(limits ? overLimits(lights, limits).map(f => f.name) : []);
    series.forEach(s => {
        const values = lights.map(f => metric(f, s.key));
        const finite = values.filter(v => v !== null);
        if (!finite.length) return;
        let lo = Math.min(...finite), hi = Math.max(...finite);
        if (s.key === 'fwhm' && limits && limits.fwhm) hi = Math.max(hi, limits.fwhm);
        if (hi - lo < 1e-6) { lo -= 0.5; hi += 0.5; }
        const top = pad + s.band[0] * (H - 2 * pad), bottom = pad + s.band[1] * (H - 2 * pad);
        // higher is worse for FWHM and eccentricity: draw them rising
        const y = (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top);
        if (s.key === 'fwhm' && limits && limits.fwhm) {
            const line = document.createElementNS(ns, 'line');
            Object.entries({ x1: 0, x2: W, y1: y(limits.fwhm), y2: y(limits.fwhm), stroke: 'var(--danger)', 'stroke-dasharray': '6 5', 'vector-effect': 'non-scaling-stroke' })
                .forEach(([k, v]) => line.setAttribute(k, v));
            svg.appendChild(line);
        }
        const points = values.map((v, i) => v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`).filter(Boolean).join(' ');
        const line = document.createElementNS(ns, 'polyline');
        Object.entries({ points, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' })
            .forEach(([k, v]) => line.setAttribute(k, v));
        svg.appendChild(line);
        if (s.key === 'fwhm') {
            lights.forEach((f, i) => {
                if (!over.has(f.name) || values[i] === null) return;
                const c = document.createElementNS(ns, 'circle');
                Object.entries({ cx: x(i), cy: y(values[i]), r: 4, fill: 'var(--danger)' }).forEach(([k, v]) => c.setAttribute(k, v));
                const title = document.createElementNS(ns, 'title');
                title.textContent = `${f.name}: FWHM ${values[i].toFixed(2)} px`;
                c.appendChild(title);
                svg.appendChild(c);
            });
        }
    });
    return lights.length;
}
