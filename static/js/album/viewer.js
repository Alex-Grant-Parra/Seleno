// The full-size viewer: zoom (wheel, buttons, double-click), pan (drag), an on-screen
// auto stretch for faint detail, and blink (flick through frames to spot satellites,
// clouds or a bumped mount). Keys: ← → move, space blinks, s stretch, x reject,
// f fit, 1 actual size, Esc closes.

const STRETCH_MAX_SIDE = 4096;
const BLINK_MS = 600;

const $ = (id) => document.getElementById(id);

let list = [];
let index = 0;
let options = {};
let zoom = 'fit';
let scale = 1;
let offset = { x: 0, y: 0 };
let stretch = false;
let playing = null;
let shown = null;              // {name, source: Image, full: bool}
const cache = new Map();       // name -> {preview: Image, full: Image}
let wired = false;

function mtf(m, x) {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    return ((m - 1) * x) / ((2 * m - 1) * x - m);
}

// Black point and midtones from the picture's own sky, so faint detail shows
function stretchLut(img) {
    const side = 512;
    const k = Math.min(1, side / Math.max(img.naturalWidth, img.naturalHeight));
    const w = Math.max(1, Math.round(img.naturalWidth * k)), h = Math.max(1, Math.round(img.naturalHeight * k));
    const c = document.createElement('canvas');
    c.width = w;
    c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h).data;
    const hist = new Uint32Array(256);
    for (let i = 0; i < d.length; i += 4) hist[(d[i] * 54 + d[i + 1] * 183 + d[i + 2] * 19) >> 8]++;
    const n = w * h;
    let acc = 0, med = 0;
    for (let v = 0; v < 256; v++) { acc += hist[v]; if (acc >= n / 2) { med = v; break; } }
    const dev = new Uint32Array(256);
    for (let v = 0; v < 256; v++) dev[Math.abs(v - med)] += hist[v];
    acc = 0;
    let mad = 0;
    for (let v = 0; v < 256; v++) { acc += dev[v]; if (acc >= n / 2) { mad = v; break; } }
    const black = Math.max(0, (med - 2.8 * 1.4826 * Math.max(mad, 0.5)) / 255);
    const xm = Math.min(0.999, Math.max(1e-4, (med / 255 - black) / Math.max(1e-6, 1 - black)));
    const mid = Math.min(0.9999, Math.max(1e-4, mtf(0.25, xm)));
    const lut = new Uint8ClampedArray(256);
    for (let v = 0; v < 256; v++) lut[v] = Math.round(255 * mtf(mid, (v / 255 - black) / Math.max(1e-6, 1 - black)));
    return lut;
}

function loadImage(url) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.decoding = 'async';
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error(`Couldn't load ${url}`));
        img.src = url;
    });
}

function entry(frame) {
    if (!cache.has(frame.name)) cache.set(frame.name, {});
    return cache.get(frame.name);
}

function preview(frame) {
    const e = entry(frame);
    if (!e.previewPromise) e.previewPromise = loadImage(frame.preview).then(img => (e.preview = img)).catch(() => null);
    return e.previewPromise;
}

function full(frame) {
    const e = entry(frame);
    if (frame.kind !== 'jpeg' && frame.kind !== 'image') return preview(frame);
    if (!e.fullPromise) e.fullPromise = loadImage(frame.url).then(img => (e.full = img)).catch(() => null);
    return e.fullPromise;
}

function stage() {
    return $('viewerStage');
}

function draw(img, isFull) {
    const canvas = $('viewerCanvas');
    if (!img) return;
    const frameNow = list[index];
    // The same photo at another resolution (preview to full, stretch on or off): keep the view
    const sameView = shown && shown.name === frameNow.name && canvas.width > 0;
    const previousWidth = canvas.width;
    let w = img.naturalWidth, h = img.naturalHeight;
    const ratio = stretch ? Math.min(1, STRETCH_MAX_SIDE / Math.max(w, h)) : 1;
    const cw = Math.round(w * ratio), ch = Math.round(h * ratio);
    canvas.width = cw;
    canvas.height = ch;
    const ctx = canvas.getContext('2d', { willReadFrequently: stretch });
    ctx.drawImage(img, 0, 0, cw, ch);
    if (stretch) {
        const lut = stretchLut(img);
        const data = ctx.getImageData(0, 0, cw, ch);
        const d = data.data;
        for (let i = 0; i < d.length; i += 4) {
            d[i] = lut[d[i]];
            d[i + 1] = lut[d[i + 1]];
            d[i + 2] = lut[d[i + 2]];
        }
        ctx.putImageData(data, 0, 0);
    }
    // The size the canvas stands for, so zoom levels are in the photo's own pixels
    const realW = (isFull ? w : frameNow.width) || w;
    canvas.dataset.realWidth = realW;
    shown = { name: frameNow.name, img, isFull };
    if (sameView) {
        scale = (scale * previousWidth) / cw;
        render();
    } else {
        applyZoom(false);
    }
}

function applyZoom(keepCentre = true) {
    const canvas = $('viewerCanvas');
    const box = stage().getBoundingClientRect();
    const realW = Number(canvas.dataset.realWidth) || canvas.width;
    const fit = Math.min(box.width / canvas.width, box.height / canvas.height);
    const centre = { x: (box.width / 2 - offset.x) / scale, y: (box.height / 2 - offset.y) / scale };
    const next = zoom === 'fit' ? fit : (Number(zoom) * realW) / canvas.width;
    if (zoom === 'fit' || !keepCentre) {
        scale = next;
        offset = { x: (box.width - canvas.width * scale) / 2, y: (box.height - canvas.height * scale) / 2 };
    } else {
        scale = next;
        offset = { x: box.width / 2 - centre.x * scale, y: box.height / 2 - centre.y * scale };
    }
    render();
}

function render() {
    const canvas = $('viewerCanvas');
    canvas.style.transform = `translate(${offset.x}px, ${offset.y}px) scale(${scale})`;
    canvas.classList.toggle('pixelated', scale > 2);
    document.querySelectorAll('#viewerZoomSeg button').forEach(b => b.classList.toggle('on', String(b.dataset.zoom) === String(zoom)));
}

async function show(i) {
    if (!list.length) return;
    index = (i + list.length) % list.length;
    const frame = list[index];
    $('viewerName').textContent = frame.name;
    const quality = options.quality ? options.quality(frame) : null;
    $('viewerInfo').textContent = [
        `${index + 1} of ${list.length}`,
        frame.captured_at ? Seleno.fmt.dateTime(frame.captured_at) : '',
        Seleno.fmt.exposure(frame.exposure_s) !== '—' ? Seleno.fmt.exposure(frame.exposure_s) : '',
        frame.iso ? `ISO ${frame.iso}` : '',
        quality && quality.reason ? quality.reason : ''
    ].filter(Boolean).join(' · ');
    $('viewerReject').textContent = frame.flag === 'reject' ? 'Rejected' : 'Reject';
    $('viewerReject').classList.toggle('danger', frame.flag === 'reject');
    const quick = await preview(frame);
    if (list[index] !== frame) return;
    if (quick) draw(quick, false);
    if (!playing) {
        const big = await full(frame);
        if (list[index] === frame && big && big !== quick && !playing) draw(big, true);
    }
    // Load the neighbours' previews, for smooth blinking
    [1, -1, 2].forEach(d => preview(list[(index + d + list.length) % list.length]));
    if (options.onChange) options.onChange(frame);
}

function setPlaying(on) {
    if (on && list.length < 2) return;
    clearInterval(playing);
    playing = on ? setInterval(() => show(index + 1), BLINK_MS) : null;
    $('viewerPlay').textContent = on ? 'Pause' : 'Blink';
    if (!on) show(index);
}

function toggleStretch() {
    stretch = !stretch;
    $('viewerStretch').setAttribute('aria-pressed', stretch ? 'true' : 'false');
    $('viewerStretch').classList.toggle('sel', stretch);
    if (shown) draw(shown.img, shown.isFull);
}

function setZoom(value) {
    zoom = value;
    applyZoom(true);
}

function wire() {
    if (wired) return;
    wired = true;
    const dialog = $('viewer');
    dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
        setPlaying(false);
        clearInterval(playing);
        playing = null;
    });
    $('viewerPrev').addEventListener('click', () => show(index - 1));
    $('viewerNext').addEventListener('click', () => show(index + 1));
    $('viewerPlay').addEventListener('click', () => setPlaying(!playing));
    $('viewerStretch').addEventListener('click', toggleStretch);
    $('viewerReject').addEventListener('click', () => rejectCurrent());
    document.querySelectorAll('#viewerZoomSeg button').forEach(b => b.addEventListener('click', () => setZoom(b.dataset.zoom === 'fit' ? 'fit' : Number(b.dataset.zoom))));

    const el = stage();
    let drag = null;
    el.addEventListener('pointerdown', (e) => {
        drag = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
        el.setPointerCapture(e.pointerId);
        el.classList.add('dragging');
    });
    el.addEventListener('pointermove', (e) => {
        if (!drag) return;
        offset = { x: drag.ox + e.clientX - drag.x, y: drag.oy + e.clientY - drag.y };
        render();
    });
    const end = () => { drag = null; el.classList.remove('dragging'); };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = el.getBoundingClientRect();
        const px = e.clientX - rect.left, py = e.clientY - rect.top;
        const factor = Math.exp(-e.deltaY * 0.0015);
        const next = Math.min(40, Math.max(0.02, scale * factor));
        offset = { x: px - ((px - offset.x) / scale) * next, y: py - ((py - offset.y) / scale) * next };
        scale = next;
        const canvas = $('viewerCanvas');
        zoom = (scale * canvas.width) / (Number(canvas.dataset.realWidth) || canvas.width);
        render();
        const frame = list[index];
        if (shown && !shown.isFull && !playing) full(frame).then(img => { if (img && list[index] === frame && !shown.isFull) draw(img, true); });
    }, { passive: false });
    el.addEventListener('dblclick', () => setZoom(zoom === 'fit' ? 1 : 'fit'));
    window.addEventListener('resize', () => { if (dialog.open) applyZoom(true); });
    dialog.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight') show(index + 1);
        else if (e.key === 'ArrowLeft') show(index - 1);
        else if (e.key === ' ') { e.preventDefault(); setPlaying(!playing); }
        else if (e.key === 's') toggleStretch();
        else if (e.key === 'x') rejectCurrent();
        else if (e.key === 'f') setZoom('fit');
        else if (e.key === '1') setZoom(1);
        else return;
        e.preventDefault();
    });
}

async function rejectCurrent() {
    const frame = list[index];
    if (!frame || !options.onReject) return;
    await options.onReject(frame);
    $('viewerReject').textContent = frame.flag === 'reject' ? 'Rejected' : 'Reject';
    $('viewerReject').classList.toggle('danger', frame.flag === 'reject');
}

// Open on frames[start]; blink = start blinking straight away
export function openViewer(frames, start = 0, opts = {}) {
    wire();
    list = frames.slice();
    options = opts;
    zoom = 'fit';
    const dialog = $('viewer');
    if (!dialog.open) dialog.showModal();
    $('viewerPlay').disabled = list.length < 2;
    show(start).then(() => { if (opts.blink) setPlaying(true); });
}
