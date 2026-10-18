// An editor session on the main thread: shows develop renders from a worker on a canvas
// in the editor's stage (latest settings win while one is rendering), compares before
// and after, and exports at full size.

import { previewFromHalf } from './combine.js';
import * as develop from './develop.js';

let nextId = 1;

export async function openSession(pool, target, stage) {
    const id = `dev-${Date.now()}-${nextId++}`;
    let planetary = false;
    let crop = null;
    if (target.stack) {
        const s = target.stack;
        planetary = s.kind === 'planetary';
        crop = s.report && s.report.crop;
        const response = await fetch(s.preview_url, { credentials: 'same-origin' });
        if (!response.ok) throw new Error(`Couldn't load the stack's preview (${response.status})`);
        const data = new Uint16Array(await response.arrayBuffer());
        const preview = previewFromHalf(data, s.preview_width, s.preview_height);
        await pool.run('devOpen', {
            id, preview: preview.planes, width: preview.width, height: preview.height, planetary,
            masterUrl: s.has_master ? s.master_url : null, fullWidth: s.width, fullHeight: s.height
        }, { worker: 0 });
    } else {
        await pool.run('devOpenFrame', { id, entry: target.entry }, { worker: 0 });
    }

    const canvas = document.createElement('canvas');
    canvas.id = 'editorCanvas';
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'The developed image');
    stage.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    let compare = 'after';
    let showBackground = false;
    let last = null;
    let pending = null;
    let running = false;
    let closed = false;

    const draw = () => {
        if (!last) return;
        const { width: w, height: h } = last;
        canvas.width = w;
        canvas.height = h;
        if (showBackground && last.background) {
            ctx.putImageData(new ImageData(new Uint8ClampedArray(last.background), w, h), 0, 0);
            return;
        }
        const after = new ImageData(new Uint8ClampedArray(last.rgba), w, h);
        if (compare === 'after' || !last.before) {
            ctx.putImageData(after, 0, 0);
            return;
        }
        const before = new ImageData(new Uint8ClampedArray(last.before), w, h);
        ctx.putImageData(before, 0, 0);
        if (compare === 'split') {
            ctx.putImageData(after, 0, 0, Math.floor(w / 2), 0, w - Math.floor(w / 2), h);
            ctx.fillStyle = 'rgba(255,255,255,0.8)';
            ctx.fillRect(Math.floor(w / 2), 0, 1, h);
        }
    };

    const pump = async () => {
        if (running || !pending || closed) return;
        running = true;
        const params = pending;
        pending = null;
        try {
            const out = await pool.run('devRender', { id, params, before: compare !== 'after', showBackground }, { worker: 0 });
            last = out;
            session.resolved = out.resolved;
            draw();
            stage.dispatchEvent(new CustomEvent('develop-rendered', { detail: out.resolved }));
        } catch (error) {
            stage.dispatchEvent(new CustomEvent('develop-error', { detail: error }));
            console.error(error);
        } finally {
            running = false;
            pump();
        }
    };

    let current = null;
    const session = {
        canvas,
        planetary,
        render(params) {
            current = params;
            pending = params;
            pump();
        },
        defaults() {
            return develop.cleanParams({});
        },
        auto() {
            const p = develop.autoParams(planetary);
            if (crop) p.crop = { ...crop };
            return p;
        },
        compare(mode) {
            compare = mode;
            if (current) this.render(current);
        },
        showBackground(on) {
            showBackground = !!on;
            if (current) this.render(current);
        },
        async export(params, format) {
            return pool.run('devExport', { id, params, format }, { worker: 0 });
        },
        close() {
            closed = true;
            canvas.remove();
            pool.run('devClose', { id }, { worker: 0 }).catch(() => {});
        }
    };
    return session;
}
