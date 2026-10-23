// Basic mode on the interface page: four guided steps (target, focus, exposure,
// capture), exposure presets, test shots with plain checks, and a one-card capture
// plan with darks and bias. Builds on the globals in interface.js.
(function () {
    'use strict';

    const el = Seleno.el;

    // What each kind of target wants. Exposures are starting points the test shot checks.
    const PRESETS = {
        moon: {
            label: _('The Moon'), exposure_s: 1 / 250, iso: 100, kind: 'bracket',
            bracket: [1 / 1000, 1 / 250, 1 / 60], count: 5, countLabel: _('Brackets'), unit: _('× 3 exposures'),
            note: _('Short exposures freeze the turbulence. Each bracket is three exposures, so one of them is right whatever the phase.')
        },
        planet: {
            label: _('A planet'), exposure_s: 1 / 60, iso: 400, kind: 'video', count: 60, countLabel: _('Video length (s)'), unit: 's',
            note: _('Planets are small: zoom the live view in on it, then record a video. The album keeps the sharpest frames (lucky imaging).')
        },
        bright: {
            label: _('Bright nebula or cluster'), exposure_s: 30, iso: 800, kind: 'light', count: 40, darks: 20, bias: 30,
            countLabel: _('Photos'), note: ''
        },
        faint: {
            label: _('Galaxy or faint nebula'), exposure_s: 60, iso: 1600, kind: 'light', count: 60, darks: 20, bias: 30,
            countLabel: _('Photos'), note: _('A minute per photo needs steady tracking: the test shot checks the stars are round.')
        },
        star: {
            label: _('A bright star'), exposure_s: 2, iso: 200, kind: 'light', count: 30, darks: 10, bias: 30,
            countLabel: _('Photos'), note: _('Bright stars fill up fast: short exposures keep their colour, and split close doubles.')
        }
    };
    const STEPS = ['target', 'focus', 'exposure', 'capture'];
    const SESSION_PREFIX = 'seleno_session_';

    let session = { focusDone: false, exposureDone: false, preset: null };
    let tracking = null;
    let targetsCache = null;
    let pendingTest = null;           // {since, requestedAt, exposure_s, deadline}
    let lastCheckedFrame = null;
    let estimateTimer = null;
    let serverClockOffsetMs = 0;

    function key() {
        return SESSION_PREFIX + (selectedTelescopeId || 'none');
    }

    function saveSession() {
        try { sessionStorage.setItem(key(), JSON.stringify(session)); } catch (error) { /* private window */ }
    }

    function loadSession() {
        try {
            const saved = JSON.parse(sessionStorage.getItem(key()) || 'null');
            session = Object.assign({ focusDone: false, exposureDone: false, preset: null }, saved || {});
        } catch (error) {
            session = { focusDone: false, exposureDone: false, preset: null };
        }
    }

    // --- steps ---------------------------------------------------------------------------

    function stepDone(step) {
        if (step === 'target') return !!currentTarget;
        if (step === 'focus') return session.focusDone;
        if (step === 'exposure') return session.exposureDone;
        if (step === 'capture') return !!currentPlan;
        return false;
    }

    function renderSteps() {
        const current = STEPS.find(step => !stepDone(step)) || 'capture';
        document.querySelectorAll('#sessionSteps .ws-step').forEach(btn => {
            const step = btn.dataset.step;
            const done = stepDone(step);
            btn.classList.toggle('done', done && step !== current);
            btn.classList.toggle('cur', step === current);
            btn.querySelector('.ws-badge').textContent = done && step !== current ? '✓' : String(STEPS.indexOf(step) + 1);
        });
        const note = (step, text) => { const n = document.querySelector(`[data-step-note="${step}"]`); if (n) n.textContent = text; };
        note('target', currentTarget ? targetLabel(currentTarget) : _("Pick something that's up tonight"));
        note('focus', session.focusDone ? _('Done') : _('Make the stars as small as they\'ll go'));
        note('exposure', session.exposureDone ? (session.preset ? PRESETS[session.preset].label : _('Done')) : _('Take a test shot and read the checks'));
        note('capture', currentPlan ? _('{name}: {state}', { name: currentPlan.name, state: (currentPlan.last_status || {}).state || currentPlan.state }) : _('Set how many photos, then start'));
    }

    function targetLabel(target) {
        const common = target.common && target.common !== target.name ? `, ${target.common}` : '';
        return `${target.name}${common}`;
    }

    // --- the target ------------------------------------------------------------------------

    function renderTargetCard() {
        const has = !!currentTarget;
        $('basicTargetEmpty').classList.toggle('ws-hidden', has);
        $('basicTargetInfo').classList.toggle('ws-hidden', !has);
        if (!has) return;
        $('basicTargetName').textContent = currentTarget.designation && currentTarget.designation !== currentTarget.name
            ? currentTarget.designation : currentTarget.name;
        const desc = [];
        if (currentTarget.designation && currentTarget.designation !== currentTarget.name) desc.push(currentTarget.name);
        else if (currentTarget.common) desc.push(currentTarget.common);
        if (currentTarget.kind) desc.push(currentTarget.kind);
        if (currentTarget.size_arcmin) desc.push(_('{size} across', { size: currentTarget.size_arcmin >= 1 ? Math.round(currentTarget.size_arcmin) + '′' : Math.round(currentTarget.size_arcmin * 60) + '″' }));
        $('basicTargetDesc').textContent = desc.join(' · ');
        const isTracking = tracking && tracking.name && sameTarget(tracking, currentTarget);
        $('btnGotoTarget').classList.toggle('ws-hidden', !!isTracking);
        $('btnGotoTarget').disabled = !telescopeAccess.control;
        $('btnCentreTarget').disabled = !telescopeAccess.control || !!currentTarget.moving;

        const sky = latestSky && latestSky.target;
        if (!latestSky || !latestSky.site) {
            ['basicTargetAlt', 'basicTargetBest', 'basicTargetAbove', 'basicTargetMoon'].forEach(id => { $(id).textContent = '—'; });
            setVerdict('off', latestSky && latestSky.message ? _("The telescope's location isn't set") : _('Checking the sky…'));
            return;
        }
        if (!sky) return;
        $('basicTargetAlt').textContent = sky.rising ? _('{alt}° and rising', { alt: Math.round(sky.alt) }) : _('{alt}° and sinking', { alt: Math.round(sky.alt) });
        $('basicTargetBest').textContent = sky.best_time ? _('{bestTime} at {bestAlt}°', { bestTime: Seleno.fmt.time(sky.best_time), bestAlt: Math.round(sky.best_alt) }) : '—';
        $('basicTargetAbove').textContent = sky.minutes_above ? _('{duration} more', { duration: Seleno.fmt.duration(sky.minutes_above * 60) }) : _('Not tonight');
        $('basicTargetMoon').textContent = _('{illumination}% lit, {moonSeparation}° away', { illumination: Math.round(latestSky.moon.illumination * 100), moonSeparation: Math.round(sky.moon_separation) });
        setVerdict(sky.verdict[0], sky.verdict[1]);
    }

    function setVerdict(level, text) {
        const chip = $('basicTargetVerdict');
        chip.querySelector('.ws-dot').className = `ws-dot ${level}`;
        chip.querySelector('span:last-child').textContent = text;
    }

    function sameTarget(a, b) {
        const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, '');
        return norm(a.name) === norm(b.name) || norm(a.name) === norm(b.designation) ||
            (b.track_name && norm(a.name) === norm(b.track_name)) ||
            (a.ra !== undefined && b.ra !== undefined && Math.abs(Number(a.ra) - Number(b.ra)) < 0.05 && Math.abs(Number(a.dec) - Number(b.dec)) < 0.05);
    }

    function loadTargets(force) {
        if (!selectedTelescopeId) return Promise.resolve(null);
        if (targetsCache && !force && Date.now() - targetsCache.at < 5 * 60 * 1000) return Promise.resolve(targetsCache.data);
        return Seleno.api(`/interface/api/targets?telescope_id=${encodeURIComponent(selectedTelescopeId)}`)
            .then(data => {
                targetsCache = { at: Date.now(), data };
                return data;
            });
    }

    function describeTargetPlacement(t) {
        if (t.alt === undefined) return t.kind || '';
        if (t.never_rises) return _("Never rises from this telescope's site");
        if (t.good_tonight === false) {
            if (t.best_alt === null || t.best_alt < 30) return _('Not good tonight: {dark}', { dark: t.best_alt === null ? _('not up while it is dark') : _('only reaches {bestAlt}° while it is dark', { bestAlt: Math.max(0, Math.round(t.best_alt)) }) });
            return _('Not good tonight: high for under an hour while it is dark');
        }
        if (t.alt >= 20) return t.rising ? _('{alt}° now and rising', { alt: Math.round(t.alt) }) : _('{alt}° now and sinking', { alt: Math.round(t.alt) });
        if (t.best_alt !== null && t.best_alt >= 25 && t.best_time) return _('Up later: best {bestTime} at {bestAlt}°', { bestTime: Seleno.fmt.time(t.best_time), bestAlt: Math.round(t.best_alt) });
        if (t.alt > 0) return _('Low ({alt}°)', { alt: Math.round(t.alt) });
        return _('Not well placed tonight');
    }

    function openPicker() {
        if (!selectedTelescopeId) {
            Seleno.toast(_('Choose a telescope first (top left).'), 'warn');
            return;
        }
        const modal = bootstrap.Modal.getOrCreateInstance($('targetPickerModal'));
        modal.show();
        const list = $('targetList');
        const note = $('pickerNote');
        list.innerHTML = '';
        note.textContent = _("Loading what's up tonight…");
        loadTargets().then(data => {
            if (!data) return;
            note.textContent = data.site
                ? _('Sorted by how well placed they are tonight{end}.', { end: data.night && data.night.start ? _(' (dark {start} – {end})', { start: Seleno.fmt.time(data.night.start), end: Seleno.fmt.time(data.night.end) }) : '' })
                : _("The telescope's location isn't set, so the list can't say what's up. Its owner can set it on the telescope's settings page.");
            data.targets.forEach(t => {
                const dim = t.good_tonight === false;
                const verdict = t.verdict || ['off', ''];
                // A card rather than one button, so the Wikipedia link can sit beside the choice
                list.appendChild(el('div', { className: `ws-pick${dim ? ' dim' : ''}` }, [
                    el('button', { type: 'button', className: 'ws-pick-main', onclick: () => chooseTarget(t) }, [
                        el('span', { className: 'ws-strong' }, [el('span', { className: `ws-dot ${verdict[0]}`, style: { marginRight: '6px' } }),
                            t.designation === t.name ? t.name : `${t.designation} · ${t.name}`]),
                        el('span', { className: 'ws-small ws-muted' }, `${t.kind || ''}${t.size_arcmin ? ` · ${t.size_arcmin >= 1 ? Math.round(t.size_arcmin) + '′' : '<1′'}` : ''}`),
                        el('span', { className: 'ws-small' }, describeTargetPlacement(t)),
                        t.blurb ? el('span', { className: 'ws-small ws-muted' }, t.blurb) : null
                    ]),
                    t.wiki ? el('a', { className: 'ws-small ws-pick-link', href: t.wiki, target: '_blank', rel: 'noopener' }, _('Read about it on Wikipedia ↗')) : null
                ]));
            });
        }).catch(error => { note.textContent = _('Couldn\'t load targets: {message}', { message: error.message }); });
    }

    function pickerSearch() {
        const text = $('pickerSearch').value.trim();
        const box = $('pickerSearchResult');
        if (!text) return;
        box.textContent = _('Searching…');
        searchCatalogue(text).then(star => {
            if (!star) {
                box.textContent = _('Nothing called “{text}” in the catalogue.', { text });
                return;
            }
            renderSearchResult(box, star, target => [
                el('button', { className: 'ws-btn sm primary', type: 'button', onclick: () => chooseTarget({ ...target, preset: null }) }, _('Choose this'))
            ]);
        }).catch(() => { box.textContent = _('The search failed.'); });
    }

    function chooseTarget(t) {
        const target = {
            name: t.name,
            track_name: t.track_name || '',
            designation: t.designation || t.name,
            common: t.common || '',
            kind: t.kind || '',
            size_arcmin: t.size_arcmin,
            ra: t.ra,
            dec: t.dec,
            moving: !!t.moving,
            preset: t.preset || null
        };
        bootstrap.Modal.getOrCreateInstance($('targetPickerModal')).hide();
        session.focusDone = false;
        session.exposureDone = false;
        if (target.preset && PRESETS[target.preset]) choosePreset(target.preset);
        saveSession();
        setCurrentTarget(target);
        if (telescopeAccess.control && (t.alt === undefined || t.alt > 0)) {
            goToTarget(target).then(ok => { if (ok) Seleno.toast(_('Going to {designation}. The mount tracks it once it gets there.', { designation: target.designation }), 'ok'); });
        } else if (!telescopeAccess.control) {
            Seleno.toast(_("You can't move this telescope now: that needs a booking that covers now."), 'warn');
        } else {
            Seleno.toast(_('{designation} is below the horizon. Go to it once it has risen.', { designation: target.designation }), 'warn');
        }
    }

    // --- exposure presets --------------------------------------------------------------------

    function parseShutter(text) {
        const s = String(text).trim();
        const frac = s.match(/^(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)/);
        if (frac) return Number(frac[1]) / Number(frac[2]);
        const n = parseFloat(s.replace(/["s]/g, ''));
        return isNaN(n) ? null : n;
    }

    // The camera setting that gives `seconds`: a fixed shutter speed if one is close,
    // else bulb with that many seconds
    function shutterFor(seconds) {
        const choices = (window.cameraChoices && window.cameraChoices.shutterSpeed) || [];
        const bulb = choices.find(isBulbShutter);
        let best = null;
        choices.forEach(choice => {
            const value = parseShutter(choice);
            if (!value) return;
            const err = Math.abs(Math.log(value / seconds));
            if (!best || err < best.err) best = { choice, value, err };
        });
        if (seconds >= 1 && bulb && (!best || best.err > 0.05 || seconds > 30)) return { shutter: bulb, exposure_s: seconds };
        if (best) return { shutter: best.choice, exposure_s: best.value };
        return bulb ? { shutter: bulb, exposure_s: seconds } : { shutter: null, exposure_s: seconds };
    }

    function isoFor(iso) {
        const choices = (window.cameraChoices && window.cameraChoices.iso) || [];
        let best = null;
        choices.forEach(choice => {
            const value = parseFloat(choice);
            if (!value) return;
            const err = Math.abs(Math.log(value / iso));
            if (!best || err < best.err) best = { choice, err };
        });
        return best ? best.choice : String(iso);
    }

    // A RAW + JPEG format: RAWs calibrate and stack exactly, JPEGs show up quickly
    function rawJpegFormat() {
        const choices = (window.cameraChoices && window.cameraChoices.imageFormat) || [];
        return choices.find(c => /raw/i.test(c) && /(jpe?g|large|fine|\bL\b)/i.test(c) && !/small|medium|\bS\d?\b|\bM\b/i.test(c))
            || choices.find(c => /raw/i.test(c) && /\+/.test(c)) || null;
    }

    function presetSettings(name) {
        const preset = PRESETS[name];
        const shutter = shutterFor(preset.exposure_s);
        return { preset, shutter, iso: isoFor(preset.iso), format: rawJpegFormat() };
    }

    function renderPreset() {
        document.querySelectorAll('#presetButtons [data-preset]').forEach(btn => btn.classList.toggle('sel', btn.dataset.preset === session.preset));
        const line = $('presetSettingsLine');
        const note = $('presetNote');
        $('btnTestShot').disabled = !session.preset || !telescopeAccess.control;
        if (!session.preset) {
            line.textContent = _('Choose one above');
            note.textContent = '';
            return;
        }
        const { preset, shutter, iso, format } = presetSettings(session.preset);
        line.textContent = [Seleno.fmt.exposure(shutter.exposure_s), _('ISO {iso}', { iso }), format ? 'RAW + JPEG' : ''].filter(Boolean).join(' · ');
        note.textContent = preset.note || '';
        $('btnTestShot').textContent = _('Take a {exposureS} test shot', { exposureS: Seleno.fmt.exposure(shutter.exposure_s) });
        renderCaptureForm();
    }

    function choosePreset(name, options = {}) {
        if (!PRESETS[name]) return;
        const changed = session.preset !== name;
        session.preset = name;
        if (changed) session.exposureDone = false;
        saveSession();
        renderPreset();
        renderSteps();
        if (!options.quiet) applyPresetToCamera();
        const preset = PRESETS[name];
        $('capCount').value = preset.count;
    }

    function applyPresetToCamera() {
        if (!session.preset || !telescopeAccess.control || !window.cameraChoices) return Promise.resolve();
        const { shutter, iso, format } = presetSettings(session.preset);
        const updates = [];
        if (iso) updates.push(updateSetting('iso', iso, { quiet: true }));
        if (shutter.shutter) updates.push(updateSetting('shutterSpeed', shutter.shutter, { quiet: true }));
        if (format) updates.push(updateSetting('imageFormat', format, { quiet: true }));
        if (shutter.shutter && isBulbShutter(shutter.shutter)) $('bulbSeconds').value = shutter.exposure_s;
        return Promise.all(updates);
    }

    // --- test shots ------------------------------------------------------------------------

    // Plain-language verdicts on a frame's numbers (the album's quality measurements, or
    // the pixel levels of the preview when the stars haven't been measured)
    function frameChecks(m, pixelScaleArcsec) {
        const out = [];
        if (!m) return out;
        if (m.stars !== undefined && m.stars !== null && m.stars < 5) {
            out.push({ level: 'bad', title: _('Hardly any stars'), detail: _('Clouds, the cover still on, badly out of focus, or a target with no stars (the Moon or a planet).') });
            return out;
        }
        if (m.ecc !== undefined && m.ecc !== null) {
            if (m.ecc < 0.45) out.push({ level: 'ok', title: _('Stars are round'), detail: _('Tracking is steady (eccentricity {ecc}).', { ecc: m.ecc.toFixed(2) }) });
            else if (m.ecc < 0.6) out.push({ level: 'warn', title: _('Stars are slightly stretched'), detail: _('Eccentricity {ecc}. A shorter exposure, a better polar alignment or less wind helps.', { ecc: m.ecc.toFixed(2) }) });
            else out.push({ level: 'bad', title: _('Stars are trailing'), detail: _('Eccentricity {ecc}. Check the mount is tracking and polar aligned, or shorten the exposure.', { ecc: m.ecc.toFixed(2) }) });
        }
        if (m.fwhm !== undefined && m.fwhm !== null) {
            let size = `${m.fwhm.toFixed(1)} px`;
            if (pixelScaleArcsec) size += ` (${(m.fwhm * pixelScaleArcsec).toFixed(1)}″)`;
            if (m.fwhm <= 4.0) out.push({ level: 'ok', title: _('In focus'), detail: _('Stars are {size} across.', { size }) });
            else if (m.fwhm <= 6.5) out.push({ level: 'warn', title: _('Focus could be sharper'), detail: _('Stars are {size} across. Seeing may be poor, or refocus with the focus helper.', { size }) });
            else out.push({ level: 'bad', title: _('Out of focus'), detail: _('Stars are {size} across. Refocus before capturing.', { size }) });
        }
        const bg = m.background_display;
        if (bg !== undefined && bg !== null) {
            const pct = Math.round(bg * 100);
            if (bg < 0.08) out.push({ level: 'warn', title: _('Underexposed'), detail: _('The sky sits at {pct}% of the histogram. Expose longer or raise the ISO so faint detail clears the noise.', { pct }) });
            else if (bg > 0.55) out.push({ level: 'bad', title: _('Overexposed'), detail: _('The sky sits at {pct}%. Shorten the exposure or lower the ISO; light pollution or twilight may be washing it out.', { pct }) });
            else out.push({ level: 'ok', title: _('Exposure is right'), detail: _('The sky sits at {pct}% of the histogram.', { pct }) });
        }
        const clipped = m.clipped_pct || 0;
        const n = m.stars || 0;
        if (clipped > 0.5) out.push({ level: 'bad', title: _('Highlights are clipped'), detail: _('{clipped}% of the picture is pure white. Shorten the exposure.', { clipped: clipped.toFixed(2) }) });
        else if (n && (m.saturated_stars || 0) > 0.1 * n) out.push({ level: 'warn', title: _('Many bright stars are clipped'), detail: _('Normal for long exposures; shorten it if star colour matters.') });
        else if (m.clipped_pct !== undefined) out.push({ level: 'ok', title: _('Nothing important is clipped'), detail: _('{clipped}% of pixels at full brightness.', { clipped: clipped.toFixed(2) }) });
        return out;
    }

    // Sky level and clipping of an image, from its pixels
    function pixelStats(img) {
        const side = 800;
        const scale = Math.min(1, side / Math.max(img.naturalWidth, img.naturalHeight));
        const w = Math.max(1, Math.round(img.naturalWidth * scale));
        const h = Math.max(1, Math.round(img.naturalHeight * scale));
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, w, h);
        const data = ctx.getImageData(0, 0, w, h).data;
        const hist = new Uint32Array(256);
        let n = 0;
        for (let i = 0; i < data.length; i += 4) {
            hist[(data[i] * 54 + data[i + 1] * 183 + data[i + 2] * 19) >> 8]++;
            n++;
        }
        let acc = 0, median = 0, clipped = 0;
        for (let v = 0; v < 256; v++) {
            acc += hist[v];
            if (!median && acc >= n / 2) median = v;
            if (v >= 250) clipped += hist[v];
        }
        return { background: median / 255, clippedPct: (100 * clipped) / n };
    }

    function loadImage(url) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = reject;
            img.src = url;
        });
    }

    function renderChecks(container, checks) {
        container.innerHTML = '';
        checks.forEach(check => container.appendChild(el('div', { className: 'ws-check' }, [
            el('span', { className: `ws-dot ${check.level}` }),
            el('div', {}, [el('b', {}, check.title), el('div', { className: 'ws-muted ws-small' }, check.detail)])
        ])));
    }

    function verdictOf(checks) {
        if (checks.some(c => c.level === 'bad')) return ['bad', _('Needs fixing'), _('Fix what the red check says, then take another test shot.')];
        if (checks.some(c => c.level === 'warn')) return ['warn', _('Nearly there'), _('Worth a look, but you can capture with this.')];
        return ['ok', _('Looks good'), _("You're ready to capture.")];
    }

    async function showTestShot(frame) {
        lastCheckedFrame = frame.name;
        $('testShotEmpty').classList.add('ws-hidden');
        $('testShotBody').classList.remove('ws-hidden');
        $('testShotTime').textContent = Seleno.fmt.time(frame.captured_at || frame.uploaded_at);
        $('testShotThumb').src = frame.thumb;
        $('testShotLink').href = `/album?frame=${encodeURIComponent(frame.name)}`;
        $('testShotVerdict').textContent = _('Checking…');
        $('testShotVerdictSub').textContent = '';
        let metrics = frame.metrics;
        let partial = false;
        if (!metrics) {
            try {
                const img = await loadImage(frame.preview);
                const stats = pixelStats(img);
                frame.pixel_stats = stats;
                metrics = { background_display: stats.background, clipped_pct: stats.clippedPct };
                partial = true;
            } catch (error) {
                metrics = null;
            }
        }
        const checks = frameChecks(metrics);
        if (partial) {
            checks.push({ level: 'off', title: _('Star shape and focus not measured yet'), detail: _('Measuring stars arrives with in-browser processing. Meanwhile zoom into the test shot in the album to check they look round and sharp.') });
        }
        renderChecks($('testShotChecks'), checks);
        renderChecks($('lastFrameChecks'), checks);
        const verdict = verdictOf(checks.filter(c => c.level !== 'off'));
        $('testShotVerdict').textContent = verdict[1];
        $('testShotVerdict').style.color = verdict[0] === 'ok' ? 'var(--success)' : verdict[0] === 'warn' ? 'var(--warning)' : 'var(--danger)';
        $('testShotVerdictSub').textContent = verdict[2];
        if (verdict[0] !== 'bad') {
            session.exposureDone = true;
            saveSession();
            renderSteps();
        }
        renderLastFrame(frame);
    }

    function takeTestShot() {
        if (!session.preset && Seleno.mode() === 'basic') return;
        const btn = $('btnTestShot');
        const settings = session.preset && Seleno.mode() === 'basic' ? presetSettings(session.preset) : null;
        const start = settings ? applyPresetToCamera() : Promise.resolve();
        const exposure = settings ? settings.shutter.exposure_s : (isBulbShutter($('shutterSpeedSelect').value) ? parseFloat($('bulbSeconds').value) : parseShutter($('shutterSpeedSelect').value) || 1);
        btn.disabled = true;
        $('btnAdvTestShot').disabled = true;
        const requestedAt = Date.now();
        const sinceIso = new Date(requestedAt + serverClockOffsetMs - 5000).toISOString();
        pendingTest = { since: sinceIso, deadline: requestedAt + (exposure + 180) * 1000 };
        let countdown = Math.ceil(exposure);
        btn.textContent = _('Exposing… {countdown} s', { countdown });
        const timer = setInterval(() => {
            countdown -= 1;
            btn.textContent = countdown > 0 ? _('Exposing… {countdown} s', { countdown }) : _('Waiting for the photo to arrive…');
        }, 1000);
        start
            .then(() => takePhoto(settings ? { shutter: settings.shutter.shutter, exposure_s: settings.shutter.exposure_s, frame_type: 'test' } : { frame_type: 'test' }))
            .then(() => waitForTestShot())
            .catch(error => {
                pendingTest = null;
                if (error.message !== 'Bad exposure') Seleno.toast(_('The test shot failed: {message}', { message: error.message }), 'bad');
            })
            .finally(() => {
                clearInterval(timer);
                btn.disabled = !session.preset || !telescopeAccess.control;
                $('btnAdvTestShot').disabled = false;
                renderPreset();
            });
    }

    function waitForTestShot() {
        return new Promise(resolve => {
            const tick = () => {
                if (!pendingTest) return resolve(null);
                if (Date.now() > pendingTest.deadline) {
                    Seleno.toast(_("The test shot hasn't arrived yet. It may still be uploading; it'll show up in the album."), 'warn', 7000);
                    pendingTest = null;
                    return resolve(null);
                }
                Seleno.api(`/interface/api/latest_captures?telescope_id=${encodeURIComponent(selectedTelescopeId)}&since=${encodeURIComponent(pendingTest.since)}&limit=5`)
                    .then(data => {
                        const frame = (data.frames || []).find(f => f.kind !== 'video');
                        if (frame) {
                            pendingTest = null;
                            showTestShot(frame);
                            pollLatestCaptures();
                            return resolve(frame);
                        }
                        setTimeout(tick, 3000);
                    })
                    .catch(() => setTimeout(tick, 4000));
            };
            setTimeout(tick, 2500);
        });
    }

    // --- capture ---------------------------------------------------------------------------

    function nightLabel(date = new Date()) {
        const evening = new Date(date.getTime() - 12 * 3600 * 1000);
        return evening.toLocaleDateString(Seleno.locale(), { day: 'numeric', month: 'short' });
    }

    function buildPlan() {
        if (!currentTarget || !session.preset) return null;
        const { preset, shutter, iso, format } = presetSettings(session.preset);
        const count = Math.max(1, Math.min(2000, parseInt($('capCount').value, 10) || preset.count));
        const steps = [];
        const label = currentTarget.designation || currentTarget.name;
        const isTracking = tracking && tracking.name && sameTarget(tracking, currentTarget);
        if (!isTracking) {
            if (currentTarget.moving) steps.push({ type: 'goto', target: currentTarget.track_name || currentTarget.name });
            else steps.push({ type: 'goto', name: label, ra: Number(currentTarget.ra), dec: Number(currentTarget.dec), centre: true });
        }
        const camera = { iso };
        if (format) camera.image_format = format;
        if (preset.kind === 'light') {
            const light = { type: 'light', count, exposure_s: shutter.exposure_s, label, ...camera };
            if ($('capDither').checked) {
                light.dither_every = 3;
                light.dither_arcmin = 2;
            }
            steps.push(light);
            if ($('capDarks').checked) steps.push({ type: 'dark', count: preset.darks, exposure_s: shutter.exposure_s, label, ...camera });
            if ($('capBias').checked) steps.push({ type: 'bias', count: preset.bias, label, ...camera });
        } else if (preset.kind === 'bracket') {
            steps.push({ type: 'bracket', exposures_s: preset.bracket, repeat: count, label, ...camera });
        } else if (preset.kind === 'video') {
            steps.push({ type: 'video', duration_s: Math.min(600, count), quality: 'original', label });
        }
        return {
            name: `${label} ${nightLabel()}`,
            on_error: 'skip_step',
            upload: 'jpeg_first',
            park_at_end: false,
            steps
        };
    }

    function renderCaptureForm() {
        const preset = session.preset ? PRESETS[session.preset] : null;
        const light = !preset || preset.kind === 'light';
        $('capDarksRow').classList.toggle('ws-hidden', !light);
        $('capBiasRow').classList.toggle('ws-hidden', !light);
        $('capDitherRow').classList.toggle('ws-hidden', !light);
        $('capCountLabel').textContent = preset ? preset.countLabel : _('Photos');
        if (preset) {
            $('capDarkCount').textContent = preset.darks || 20;
            $('capBiasCount').textContent = preset.bias || 30;
            const exposure = presetSettings(session.preset).shutter.exposure_s;
            $('capExposureLabel').textContent = preset.kind === 'light' ? `× ${Seleno.fmt.exposure(exposure)}` : preset.unit;
        }
        $('btnStartCapture').disabled = !(preset && currentTarget && telescopeAccess.control);
        scheduleEstimate();
    }

    function scheduleEstimate() {
        clearTimeout(estimateTimer);
        estimateTimer = setTimeout(runEstimate, 350);
    }

    function runEstimate() {
        const line = $('capEstimate');
        const plan = buildPlan();
        if (!plan) {
            line.textContent = !currentTarget ? _('Choose a target first.') : _('Choose what you are photographing first.');
            return;
        }
        Seleno.api('/interface/capture/estimate', { json: { plan, telescope_id: selectedTelescopeId } })
            .then(data => {
                const est = data.estimate;
                const parts = [_('About {seconds} and {bytes}.', { seconds: Seleno.fmt.duration(est.seconds), bytes: Seleno.fmt.bytes(est.bytes) })];
                const endsAt = Date.now() + est.seconds * 1000;
                const booking = latestStatus && latestStatus.booking;
                const next = latestStatus && latestStatus.next_booking_start;
                if (booking && booking.end) {
                    const end = new Date(booking.end).getTime();
                    parts.push(endsAt > end ? _('That runs past your booking (ends {end}): take fewer photos.', { end: Seleno.fmt.time(end) })
                        : end - endsAt > 20 * 60 * 1000 ? _('Ends well before your booking does.') : _('Ends just before your booking ({end}).', { end: Seleno.fmt.time(end) }));
                } else if (next && endsAt > new Date(next).getTime()) {
                    parts.push(_('Someone\'s booking starts at {next}; the plan stops then.', { next: Seleno.fmt.time(next) }));
                }
                const target = latestSky && latestSky.target;
                if (target && target.above_to && endsAt > new Date(target.above_to).getTime() + 10 * 60 * 1000) {
                    parts.push(_('{name} drops below 30° at {aboveTo}.', { name: currentTarget.designation || currentTarget.name, aboveTo: Seleno.fmt.time(target.above_to) }));
                }
                if (data.errors && data.errors.length) parts.push(data.errors[0]);
                line.textContent = parts.join(' ');
            })
            .catch(error => { line.textContent = _('Couldn\'t estimate: {message}', { message: error.message }); });
    }

    function startCapture() {
        const plan = buildPlan();
        if (!plan) return;
        if (!session.focusDone || !session.exposureDone) {
            const question = !session.focusDone && !session.exposureDone
                ? _("You haven't focused or checked a test shot yet. Start capturing anyway?")
                : !session.focusDone ? _("You haven't focused yet. Start capturing anyway?")
                : _("You haven't checked a test shot yet. Start capturing anyway?");
            if (!confirm(question)) return;
        }
        const btn = $('btnStartCapture');
        btn.disabled = true;
        btn.textContent = _('Starting…');
        Seleno.api('/interface/capture/start', { json: { plan, telescope_id: selectedTelescopeId } })
            .then(data => {
                logLine(_('✓ started plan {name}', { name: plan.name }), 'ok');
                Seleno.toast(_('Capturing. It keeps going if you close this page.'), 'ok', 6000);
                renderPlan(data.plan);
                pollTelescopeStatus();
                loadPlanHistory();
            })
            .catch(error => {
                const errors = error.body && error.body.errors;
                Seleno.toast(_('Couldn\'t start: {message}', { message: errors && errors.length ? errors.join('; ') : error.message }), 'bad', 8000);
            })
            .finally(() => {
                btn.textContent = _('Start capturing');
                renderCaptureForm();
            });
    }

    function renderBasicPlan(plan) {
        $('captureForm').classList.toggle('ws-hidden', !!plan);
        $('captureRunning').classList.toggle('ws-hidden', !plan);
        if (!plan) return;
        const status = plan.last_status || {};
        const state = status.state || plan.state;
        const progress = planProgressText(plan);
        $('basicPlanTitle').textContent = `${plan.name} · ${state}`;
        $('basicPlanProgress').textContent = progress.text;
        $('basicPlanBar').style.width = progress.fraction !== null ? `${Math.round(progress.fraction * 100)}%` : '4%';
        const coverWait = state === 'waiting' && status.waiting === 'cover';
        $('basicCoverPrompt').classList.toggle('ws-hidden', !coverWait);
        $('basicCoverBtn').classList.toggle('ws-hidden', !coverWait);
        $('basicPauseBtn').classList.toggle('ws-hidden', state !== 'running');
        $('basicResumeBtn').classList.toggle('ws-hidden', state !== 'paused');
    }

    // --- focus ---------------------------------------------------------------------------

    function renderBasicFocus(detail) {
        const history = detail ? detail.history : [];
        const result = detail ? detail.result : null;
        const advice = $('focusAdvice');
        const sub = $('focusAdviceSub');
        if (!liveViewActive) {
            $('focusHfrBig').textContent = '—';
            advice.textContent = _('Start live view, then click a bright star to zoom in on it.');
            sub.textContent = _('Turn the focuser slowly. Smaller is sharper; the green dot marks the best point.');
            Seleno.sparkline($('focusSpark'), [{ values: [] }]);
            return;
        }
        if (!result || result.hfr === null) {
            $('focusHfrBig').textContent = '—';
            advice.textContent = _('No stars found in the picture.');
            sub.textContent = liveZoom > 1 ? _('Point at a brighter star, or check the cover is off.') : _('Click a bright star to zoom in on it.');
            return;
        }
        const values = history.map(e => e.hfr);
        let best = 0;
        values.forEach((v, i) => { if (v < values[best]) best = i; });
        Seleno.sparkline($('focusSpark'), [{ values, color: 'var(--accent)', width: 2.5, marks: [best], markColor: '#34d399' }], { width: 320, height: 64 });
        const now = values[values.length - 1];
        $('focusHfrBig').textContent = `${now.toFixed(1)} px`;
        const n = values.length;
        const mean = (a) => a.reduce((s, v) => s + v, 0) / a.length;
        const recent = n >= 6 ? mean(values.slice(n - 3)) : null;
        const before = n >= 6 ? mean(values.slice(n - 6, n - 3)) : null;
        const bestValue = values[best];
        if (n < 4) {
            advice.textContent = _('Measuring…');
            sub.textContent = _('Turn the focuser slowly one way and watch the number.');
        } else if (now <= bestValue * 1.04) {
            advice.textContent = _('That\'s the sharpest so far: {bestValue} px.', { bestValue: bestValue.toFixed(1) });
            sub.textContent = _('Keep turning slowly the same way; if it grows again, come back to here.');
        } else if (best < n - 3 && now > bestValue * 1.12 && recent > before) {
            advice.textContent = _('Best so far {bestValue} px. You\'ve just gone past it.', { bestValue: bestValue.toFixed(1) });
            sub.textContent = _('Turn the focuser back slowly. Smaller is sharper; the green dot marks the best point.');
        } else if (recent !== null && recent < before * 0.97) {
            advice.textContent = _('Getting smaller: keep turning the same way.');
            sub.textContent = _('Best so far {bestValue} px.', { bestValue: bestValue.toFixed(1) });
        } else if (recent !== null && recent > before * 1.03) {
            advice.textContent = _('Getting bigger: turn the other way.');
            sub.textContent = _('Best so far {bestValue} px.', { bestValue: bestValue.toFixed(1) });
        } else {
            advice.textContent = _('Steady at {now} px.', { now: now.toFixed(1) });
            sub.textContent = _('Best so far {bestValue} px. Small turns, then wait a moment for the picture to settle.', { bestValue: bestValue.toFixed(1) });
        }
    }

    // --- tonight's frames --------------------------------------------------------------------

    function renderTonight(frames) {
        const box = $('tonightThumbs');
        box.innerHTML = '';
        const nightOf = (t) => new Date(t - 12 * 3600 * 1000).toDateString();
        const now = nightOf(Date.now());
        const tonight = frames.filter(f => nightOf(new Date(f.captured_at || f.uploaded_at).getTime()) === now);
        $('tonightEmpty').classList.toggle('ws-hidden', tonight.length > 0);
        tonight.slice(0, 10).forEach(f => {
            box.appendChild(el('a', { href: `/album?frame=${encodeURIComponent(f.name)}`, title: `${f.name}${f.frame_type ? ' · ' + f.frame_type : ''}` },
                el('img', { className: 'ws-thumb', src: f.thumb, alt: f.name, loading: 'lazy', style: { width: '96px' } })));
        });
        const evening = new Date(Date.now() - 12 * 3600 * 1000);
        const day = `${evening.getFullYear()}-${String(evening.getMonth() + 1).padStart(2, '0')}-${String(evening.getDate()).padStart(2, '0')}`;
        $('tonightLink').href = `/album?night=${day}`;
    }

    // --- wiring ------------------------------------------------------------------------------

    function scrollToStep(step) {
        const card = document.querySelector(`[data-step-card="${step}"]`);
        if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.animate([{ boxShadow: '0 0 0 3px var(--accent)' }, { boxShadow: '0 0 0 0 transparent' }], { duration: 1200 });
        }
        if (step === 'target' && !currentTarget) openPicker();
        if (step === 'focus' && !liveViewActive && telescopeAccess.control) startLiveView();
    }

    function refreshAll() {
        renderSteps();
        renderTargetCard();
        renderPreset();
        renderCaptureForm();
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-action="pick-target"]').forEach(btn => btn.addEventListener('click', openPicker));
        document.querySelectorAll('#sessionSteps .ws-step').forEach(btn => btn.addEventListener('click', () => scrollToStep(btn.dataset.step)));
        document.querySelectorAll('#presetButtons [data-preset]').forEach(btn => btn.addEventListener('click', () => choosePreset(btn.dataset.preset)));
        $('pickerSearchBtn').addEventListener('click', pickerSearch);
        $('pickerSearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') pickerSearch(); });
        $('btnGotoTarget').addEventListener('click', () => goToTarget(currentTarget));
        $('btnCentreTarget').addEventListener('click', () => telescopeAction('centre_target', { source: solveSource() }).catch(() => {}));
        $('btnTestShot').addEventListener('click', takeTestShot);
        $('btnAdvTestShot').addEventListener('click', takeTestShot);
        $('btnFocusGood').addEventListener('click', () => {
            session.focusDone = true;
            saveSession();
            renderSteps();
            scrollToStep('exposure');
        });
        ['capCount', 'capDarks', 'capBias', 'capDither'].forEach(id => $(id).addEventListener('input', scheduleEstimate));
        $('btnStartCapture').addEventListener('click', startCapture);
        $('btnCaptureBuilder').addEventListener('click', () => {
            const plan = buildPlan();
            if (!window.CapturePlanBuilder) return;
            window.CapturePlanBuilder.open();
            if (plan) window.CapturePlanBuilder.loadPlan(plan);
        });
        $('basicCoverBtn').addEventListener('click', () => planControl('resume'));
        $('basicPauseBtn').addEventListener('click', () => planControl('pause'));
        $('basicResumeBtn').addEventListener('click', () => planControl('resume'));
        $('basicStopBtn').addEventListener('click', () => { if (confirm(_('Stop capturing?'))) planControl('stop'); });
        renderBasicFocus(null);
        refreshAll();
    });

    document.addEventListener('seleno:telescope', () => {
        loadSession();
        targetsCache = null;
        refreshAll();
        Seleno.api(`/interface/api/latest_captures?telescope_id=${encodeURIComponent(selectedTelescopeId)}&limit=1`)
            .then(data => { if (data.now) serverClockOffsetMs = new Date(data.now).getTime() - Date.now(); })
            .catch(() => {});
    });
    document.addEventListener('seleno:target', refreshAll);
    document.addEventListener('seleno:sky', () => { renderTargetCard(); scheduleEstimate(); });
    document.addEventListener('seleno:camera', () => { renderPreset(); if (Seleno.mode() === 'basic') applyPresetToCamera(); });
    document.addEventListener('seleno:tracking', (event) => { tracking = event.detail; renderTargetCard(); scheduleEstimate(); });
    document.addEventListener('seleno:plan', (event) => { renderBasicPlan(event.detail); renderSteps(); });
    document.addEventListener('seleno:focus', (event) => renderBasicFocus(event.detail));
    document.addEventListener('seleno:status', () => { renderTargetCard(); renderPreset(); });
    document.addEventListener('seleno:captures', (event) => {
        const frames = event.detail || [];
        renderTonight(frames);
        if (frames[0] && frames[0].name !== lastCheckedFrame) $('lastFrameChecks').innerHTML = '';
    });

    window.SelenoGuide = { frameChecks, PRESETS };
})();
