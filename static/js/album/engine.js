// The processing engine plugs in here: decoding RAWs, measuring stars, aligning and
// stacking frames, and developing the result, all in the browser (in Web Workers),
// so the server's CPU and memory are never the limit. app/processing/ (Python) is the
// tested reference it is ported from.
//
// Until the engine is written, `available` is false and the album offers everything
// that works without it: browsing, flags and types, the quality chart for numbers
// already measured, the viewer and blink, downloads, and saved stacks.
//
// What the album calls, once the engine provides it:
//
//   engine.measure(frames, {onProgress, signal})
//       frames: library entries ({name, url, raw_url, kind, ...}). Resolves to
//       {name: metrics} with the keys controllers/album_api.py stores (stars, fwhm,
//       hfr, ecc, snr, background, noise, background_pct, background_display,
//       noise_pct, clipped_pct, saturated_stars, width, height, source).
//
//   engine.stack(spec, {onProgress, signal})
//       spec: what stack-dialog.js builds ({name, target, frames: {light, dark, flat,
//       darkflat, bias: [entries]}, settings}). onProgress({fraction, message}).
//       Resolves to a result for stacks.saveStackResult(): {name, target, kind, width,
//       height, linear, frames_used, frames_total, total_exposure_s, settings,
//       report, preview: {width, height, data: Uint16Array of float16 RGB},
//       auto: Blob (JPEG), thumb: Blob (JPEG), master: Blob (FITS) or null}.
//
//   engine.develop(stack, canvas)
//       Resolves to a session: {render(params), defaults(), auto(), export(params,
//       format) -> Blob, close()}; params as app/processing/develop.py's.

export const engine = {
    available: false,
    reason: "This runs in your browser, and the in-browser processing isn't built yet."
};

export function requireEngine(feature) {
    if (engine.available) return true;
    Seleno.toast(`${feature}: ${engine.reason}`, 'warn', 6500);
    return false;
}
