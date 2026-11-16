// The interface page, end to end: Basic mode's four steps against the fake telescope
// (live H.264 focus sweep, a test shot it "uploads", a capture plan), then Advanced.
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    try {

    Seleno.setMode('basic');
    check('selected', await until(() => selectedTelescopeId === 'e2e-scope'));
    check('cameraChoices', await until(() => window.cameraChoices && window.cameraChoices.iso.length === 6));
    check('chipCamera', await until(() => txt('chipCamera').includes('Camera ready')), txt('chipCamera'));
    check('chipMount', txt('chipMount').includes('tracking'), txt('chipMount'));
    check('chipSky', await until(() => !/Sky —/.test(txt('chipSky'))), txt('chipSky'));
    check('chipAccess', txt('chipAccess') === 'Owner', txt('chipAccess'));
    check('advHiddenInBasic', !visible($q('#chipDisk')) && !visible($q('#planCard')));
    await shot('basic-start');

    // 1. Target
    $q('#basicTargetEmpty [data-action="pick-target"]').click();
    check('pickerList', await until(() => $$q('#targetList .ws-pick').length >= 8), $$q('#targetList .ws-pick').length);
    check('pickerHasM42', $$q('#targetList .ws-pick').some(b => b.textContent.includes('M42')));
    check('pickerHasPlanets', $$q('#targetList .ws-pick').some(b => b.textContent.includes('Jupiter')));
    await shot('basic-picker');
    $$q('#targetList .ws-pick').find(b => b.textContent.includes('M42')).querySelector('.ws-pick-main').click();
    check('targetSet', await until(() => currentTarget && currentTarget.designation === 'M42'));
    check('targetCard', await until(() => txt('basicTargetName') === 'M42' && txt('basicTargetAlt') !== '—'),
        [txt('basicTargetName'), txt('basicTargetDesc'), txt('basicTargetAlt'), txt('basicTargetBest'), txt('basicTargetMoon')]);
    check('verdict', !txt('basicTargetVerdict').includes('—'), txt('basicTargetVerdict'));
    check('presetFromTarget', $q('#presetButtons [data-preset="bright"]').classList.contains('sel'));
    check('presetLine', txt('presetSettingsLine').includes('30 s') && txt('presetSettingsLine').includes('ISO 800'), txt('presetSettingsLine'));
    await sleep(800);
    check('goto', await goToTarget(currentTarget), txt('trackingNow'));
    check('targetStepDone', $q('[data-step="target"]').classList.contains('done'));
    await sleep(500);

    // 2. Focus, on the fake telescope's focus sweep
    if (!liveViewActive) $q('#btnLiveToggleBasic').click();
    check('livePlaying', await until(() => liveViewPlayer.playingH264, 15000));
    setZoomLevel(5);
    check('focusMeasured', await until(() => focusHistory.length >= 14, 20000), focusHistory.length);
    const hfrs = focusHistory.map(e => e.hfr);
    check('focusRange', Math.max(...hfrs) / Math.min(...hfrs) > 1.6, [Math.min(...hfrs).toFixed(2), Math.max(...hfrs).toFixed(2)]);
    check('focusBig', /px/.test(txt('focusHfrBig')), txt('focusHfrBig'));
    check('focusAdvice', txt('focusAdvice').length > 5 && !txt('focusAdvice').includes('Start live view'), txt('focusAdvice'));
    check('focusSpark', $$q('#focusSpark polyline').length === 1 && $$q('#focusSpark circle').length === 1);
    check('overlayMarker', $$q('#liveOverlay circle').length >= 1 && txt('liveOverlay').includes('HFR'), txt('liveOverlay'));
    await shot('basic-focus');
    $q('#btnFocusGood').click();
    check('focusStepDone', $q('[data-step="focus"]').classList.contains('done'));

    // 3. A test shot: the fake telescope "uploads" it a second later
    check('testShotEnabled', !$q('#btnTestShot').disabled);
    $q('#btnTestShot').click();
    check('testShot', await until(() => visible($q('#testShotBody')) && txt('testShotVerdict') && !txt('testShotVerdict').includes('Checking'), 30000),
        [txt('testShotVerdict'), txt('btnTestShot')]);
    check('testChecks', $$q('#testShotChecks .ws-check').length >= 2, $$q('#testShotChecks .ws-check').map(n => n.textContent));
    check('exposureStep', $q('[data-step="exposure"]').classList.contains('done'));
    await shot('basic-testshot');

    // 4. Capture
    $q('#capCount').value = '20';
    $q('#capCount').dispatchEvent(new Event('input'));
    check('estimate', await until(() => txt('capEstimate').startsWith('About'), 8000), txt('capEstimate'));
    $q('#btnStartCapture').click();
    check('planRunning', await until(() => visible($q('#captureRunning')), 10000), [txt('basicPlanTitle'), txt('basicPlanProgress')]);
    check('captureStep', await until(() => $q('[data-step="capture"]').classList.contains('cur')));
    check('tonight', await until(() => $$q('#tonightThumbs img').length >= 1, 20000), $$q('#tonightThumbs img').length);
    await shot('basic-capturing');

    // Advanced
    Seleno.setMode('advanced');
    await sleep(400);
    check('advChips', visible($q('#chipDisk')) && txt('chipDisk').includes('free') && txt('chipUploads').includes('18 RAWs'), [txt('chipDisk'), txt('chipUploads'), txt('chipClock')]);
    check('advMountChip', txt('chipMount').includes('sidereal'), txt('chipMount'));
    check('advGrid', txt('telescopeStatusGrid').includes('Mount'), txt('telescopeStatusGrid'));
    check('advLog', $q('#commandLog').childNodes.length > 4, $q('#commandLog').childNodes.length);
    check('advFocusKv', txt('focusKvHfr').includes('/'), txt('focusKvHfr'));
    check('advHistogram', await until(() => !txt('liveHistogramStats').includes('No image') && !txt('liveHistogramStats').includes('Waiting')), txt('liveHistogramStats'));
    check('advClip', txt('liveClipStats').includes('stars measured'), txt('liveClipStats'));
    check('advPlan', visible($q('#planBox')), txt('planProgress'));
    check('cameraSelects', $q('#isoSelect').options.length === 6 && $q('#isoSelect').value === '800', $q('#isoSelect').value);
    check('cameraStatus', await until(() => txt('cameraStatusLine').includes('74%')), txt('cameraStatusLine'));
    // A 30 s preset uses the camera's own 30 s setting, so there's no bulb row
    check('shutterFromPreset', $q('#shutterSpeedSelect').value === '30' && !visible($q('#bulbRow')), $q('#shutterSpeedSelect').value);
    check('lastFrame', visible($q('#lastFrameBody')), txt('lastFrameName'));
    const canvas = $q('#liveViewCanvas');
    const r = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new MouseEvent('mousemove', { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, bubbles: true }));
    check('cursor', await until(() => txt('cursorChip').includes('RGB')), txt('cursorChip'));
    check('liveStats', await until(() => txt('liveStatsChip').includes('fps'), 9000), txt('liveStatsChip'));
    $q('#overlaySelect').value = 'stars';
    $q('#overlaySelect').dispatchEvent(new Event('change'));
    await sleep(900);
    check('overlayStars', $$q('#liveOverlay circle').length >= 3, $$q('#liveOverlay circle').length);
    $q('#displayStretch').value = '60';
    $q('#displayStretch').dispatchEvent(new Event('input'));
    check('displayStretch', canvas.style.filter.includes('brightness'), canvas.style.filter);

    $q('#tabBtnMount').click();
    $q('#btnCentre').click();
    check('job', await until(() => txt('jobStatusLine').includes('done'), 9000), txt('jobStatusLine'));
    check('solveLine', txt('lastSolveLine').includes('1.98'), txt('lastSolveLine'));

    $q('#tabBtnTarget').click();
    $q('#searchObject').value = 'M27';
    $q('#btnSearch').click();
    check('search', await until(() => txt('searchResult').includes('NGC6853')), txt('searchResult').slice(0, 120));
    check('advSkyKv', txt('advSkyKv').includes('Transit') && txt('advSkyKv').includes('Astro dark'), txt('advSkyKv'));
    check('planets', $q('#planetSelect').options.length === 3, $q('#planetSelect').options.length);
    $q('#manualRA').value = '05 35 17.3';
    $q('#manualDEC').value = '-05 23 28';
    $q('#btnGotoCoords').click();
    check('gotoCoords', await until(() => txt('trackingNow').includes('RA 05 35')), txt('trackingNow'));
    await shot('advanced');

    $q('#tabBtnUploads').click();
    check('uploads', await until(() => txt('uploadQueueSummary').includes('18')), txt('uploadQueueSummary'));
    $q('#tabBtnDevice').click();
    check('deviceTab', visible($q('#tabDevice')) && txt('telescopeName') === 'e2e-scope', txt('telescopeName'));

    // The plan builder still works with its old element IDs
    $q('#btnOpenBuilder').click();
    check('builder', await until(() => $q('#captureModal').classList.contains('show') && $$q('#addStepMenu button').length > 5), $$q('#addStepMenu button').length);
    window.CapturePlanBuilder.loadPlan({ name: 'E2E builder', steps: [{ type: 'light', count: 3, exposure_s: 2 }] });
    check('builderEstimate', await until(() => /Frames3/.test(txt('planEstimate'))), txt('planEstimate'));
    bootstrap.Modal.getInstance($q('#captureModal')).hide();
    await sleep(400);

    Seleno.setMode('basic');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
