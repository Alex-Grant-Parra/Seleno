// The processing engine's hook for the album: decoding RAWs, measuring stars, aligning
// and stacking frames (and lucky imaging), multi-night projects, and developing the
// result, all in this browser tab (in Web Workers), so the server's CPU and memory are
// never the limit. The engine is static/js/processing/ (see client.js there);
// app/processing/ (Python) is the reference it was ported from.
//
// What the album calls:
//
//   engine.measure(frames, {onProgress, signal})
//       frames: library entries. Resolves to {name: metrics} with the keys
//       controllers/album_api.py stores, or {name: {error}}.
//
//   engine.stack(spec, {onProgress, signal})
//       spec: what stack-dialog.js builds; spec.mode 'lucky' for lucky imaging,
//       spec.project {id} or {name} to add the night to a project. Resolves to a
//       result for stacks.saveStackResult(); call result.dispose() once it's saved.
//
//   engine.rebuildProject(id, batches, {onProgress, signal})
//       Re-stacks a project from scratch, resuming from its last checkpoint.
//
//   engine.develop(stack or {entry}, stageElement)
//       Resolves to a session: {render(params), defaults(), auto(), compare(mode),
//       showBackground(on), export(params, format) -> Blob, close()}.

import * as client from '../processing/client.js';

const supported = client.support();

export const engine = {
    available: supported.ok,
    reason: supported.ok ? '' : supported.reason,
    note: _('Runs in this browser tab: closing the tab (or the computer sleeping) stops it.'),
    machine: () => client.machineInfo(),
    measure: (frames, options) => client.measure(frames, options),
    stack: (spec, options) => client.stack(spec, options),
    rebuildProject: (id, batches, options) => client.rebuildProject(id, batches, options),
    develop: (target, stage) => client.develop(target, stage)
};

export function requireEngine(feature) {
    if (engine.available) return true;
    Seleno.toast(`${feature}: ${engine.reason}`, 'warn', 6500);
    return false;
}
