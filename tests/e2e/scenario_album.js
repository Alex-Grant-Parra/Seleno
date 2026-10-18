// The album, end to end: Basic library and inspector, flags, the viewer and blink,
// Advanced quality chart and table, the stack dialog, saved stacks and the editor,
// and saving a stack result through the real uploader.
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    try {
    // The fixture night has 12 lights (the interface scenario's test shots make another, newer night)
    const fixtureNight = () => $$q('#groupNav button').find(b => (b.querySelector('.ws-count') || {}).textContent === '12');
    const tilesOf = (i = 0) => { const grids = $$q('#groupContent .ws-tiles'); return grids[i] ? [...grids[i].querySelectorAll('.ws-tile')] : []; };

    Seleno.setMode('basic');
    check('navLoaded', await until(() => fixtureNight(), 10000));
    fixtureNight().click();
    check('loaded', await until(() => tilesOf().length > 0, 10000), txt('groupTitle'));
    check('title', txt('groupTitle').startsWith('M42'), txt('groupTitle'));
    check('summary', txt('groupSummary').includes('12 photos') && txt('groupSummary').includes('4 darks') && txt('groupSummary').includes('4 bias'), txt('groupSummary'));
    check('badSummary', txt('groupSummary').includes('2 photos look bad'), txt('groupSummary'));
    const nav = $$q('#groupNav button').map(b => b.textContent);
    check('nav', nav.length >= 5 && nav.some(t => t.startsWith('M42')) && nav.some(t => t.startsWith('All photos')), nav);
    check('lightTiles', tilesOf().length === 12, tilesOf().length);
    const flags = $$q('#groupContent .ws-flag').map(n => n.textContent);
    check('badFrames', flags.includes('Blurred') && flags.includes('Stars trailed'), flags);
    check('rawBadges', $$q('#groupContent .ws-tiles .ws-badge-raw').length === 6, $$q('#groupContent .ws-tiles .ws-badge-raw').length);
    check('thumbsLoaded', await until(() => tilesOf().every(t => t.querySelector('img') && t.querySelector('img').naturalWidth > 0), 10000));
    const rows = $$q('#groupContent .ws-grouprow').map(r => r.textContent);
    check('calibrationRows', rows.some(t => t.includes('4 darks')) && rows.some(t => t.includes('4 bias')), rows);

    tilesOf()[1].click();
    check('inspector', await until(() => visible($q('#inspectorBody')) && txt('inspectorName') === 'IMG_0402.JPG'), txt('inspectorName'));
    check('sentence', txt('inspectorSentence').length > 10, txt('inspectorSentence'));
    check('inspectorTarget', $q('#inspectorTarget').value === 'M42', $q('#inspectorTarget').value);
    check('inspectorPreview', await until(() => $q('#inspectorImg').naturalWidth > 0, 8000));
    $q('#inspectorNotes').value = 'Slight haze after 21:30';
    $q('#inspectorNotes').dispatchEvent(new Event('input'));
    $q('#btnSaveMeta').click();
    check('notesSaved', await until(() => txt('metaStatus') === 'Saved'), txt('metaStatus'));
    // Moving on with unsaved notes saves them to the right photo
    $q('#inspectorNotes').value = 'Wind gusts';
    $q('#inspectorNotes').dispatchEvent(new Event('input'));
    tilesOf()[2].click();
    check('notesFollow', await until(() => txt('inspectorName') === 'IMG_0403.JPG' && $q('#inspectorNotes').value === ''), $q('#inspectorNotes').value);
    let serverNotes = null;
    check('notesOnServer', await until(async () => {
        const lib = await (await fetch('/album/api/library')).json();
        serverNotes = lib.frames.find(f => f.name === 'IMG_0402.JPG').notes;
        return serverNotes === 'Wind gusts';
    }), serverNotes);
    // The calibration rows open
    $$q('#groupContent .ws-grouprow .ws-btn').find(b => b.closest('.ws-grouprow').textContent.includes('darks')).click();
    check('darksShown', await until(() => $$q('#groupContent .ws-tiles').length >= 2));
    await shot('basic');

    // Select two with ctrl-click and mark them bad
    tilesOf()[5].dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
    tilesOf()[6].dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
    check('bulkBar', visible($q('#bulkBar')) && txt('bulkCount') === '2 selected', txt('bulkCount'));
    $q('#btnBulkReject').click();
    check('rejected', await until(() => $$q('#groupContent .ws-flag').filter(n => n.textContent === 'Rejected').length === 2));

    // The viewer: full size, stretched, then blinking
    tilesOf()[0].click();
    $q('#btnViewFull').click();
    check('viewer', await until(() => $q('#viewer').open && $q('#viewerCanvas').width >= 900, 8000), $q('#viewerCanvas').width);
    $q('#viewerStretch').click();
    await sleep(600);
    check('stretched', $q('#viewerStretch').getAttribute('aria-pressed') === 'true');
    await shot('viewer');
    const before = txt('viewerName');
    $q('#viewerPlay').click();
    await sleep(1600);
    check('blink', txt('viewerName') !== before, [before, txt('viewerName')]);
    $q('#viewerPlay').click();
    $q('#viewerReject').click();
    check('rejectInViewer', await until(() => txt('viewerReject') === 'Rejected'));
    $q('#viewerReject').click();
    await until(() => txt('viewerReject') === 'Reject');
    $q('#viewer').close();

    // Advanced: the quality chart, filters and the table
    Seleno.setMode('advanced');
    await sleep(300);
    check('chart', $$q('#qualityChart polyline').length === 3, $$q('#qualityChart polyline').length);
    check('limits', $q('#limitFwhm').value !== '' && $q('#limitEcc').value !== '', [$q('#limitFwhm').value, $q('#limitEcc').value, $q('#limitStars').value]);
    check('overLimit', /Reject [12] frame/.test(txt('btnRejectByLimits')), txt('btnRejectByLimits'));
    check('typeChips', $$q('#typeChips .ws-chip').map(c => c.textContent).join(' ').includes('Darks 4'), $$q('#typeChips .ws-chip').map(c => c.textContent));
    $q('#layoutSeg [data-layout="table"]').click();
    check('table', await until(() => $$q('#groupContent tbody tr').length === 20), $$q('#groupContent tbody tr').length);
    // Sort by FWHM, worst at the bottom
    const fwhmHeader = $$q('#groupContent thead th').find(th => th.textContent.startsWith('FWHM'));
    fwhmHeader.click();
    await sleep(100);
    const fwhmColumn = $$q('#groupContent thead th').findIndex(th => th.textContent.startsWith('FWHM')) + 1;
    const fwhms = $$q(`#groupContent tbody tr td:nth-child(${fwhmColumn})`).map(td => td.textContent);
    const measured = fwhms.filter(v => v !== '—').map(Number);
    check('sorted', measured.length === 12 && measured.every((v, i) => i === 0 || v >= measured[i - 1]) && fwhms.slice(12).every(v => v === '—'), fwhms);
    // Hide the darks with their chip
    $$q('#typeChips .ws-chip').find(c => c.textContent.startsWith('Darks')).click();
    check('filtered', await until(() => $$q('#groupContent tbody tr').length === 16), $$q('#groupContent tbody tr').length);
    $$q('#groupContent tbody tr').find(tr => tr.dataset.name === 'IMG_0402.JPG').click();
    check('advInspector', txt('inspectorMetrics').includes('Eccentricity') && txt('inspectorKv').includes('Plan'), txt('inspectorKv'));
    $q('#exifDetails').open = true;
    $q('#exifDetails').dispatchEvent(new Event('toggle'));
    check('exif', await until(() => txt('exifSections').includes('Camera')), txt('exifSections').slice(0, 80));
    check('histogram', await until(() => !txt('albumHistogramStats').includes('No image')), txt('albumHistogramStats'));
    await shot('advanced-table');
    // Set a type on the legacy photo through the bulk bar
    $q('.ws-side button:last-child').click();
    await until(() => txt('groupTitle') === 'All photos');
    const legacyRow = $$q('#groupContent tbody tr').find(tr => tr.dataset.name.startsWith('photo'));
    legacyRow.querySelector('input[type=checkbox]').click();
    $q('#bulkType').value = 'light';
    $q('#bulkType').dispatchEvent(new Event('change'));
    check('setType', await until(async () => {
        const data = await (await fetch('/album/api/library')).json();
        return data.frames.find(f => f.name === legacyRow.dataset.name).frame_type === 'light';
    }));
    $q('#btnClearSelection').click();
    $$q('#typeChips .ws-chip').filter(c => !c.classList.contains('on')).forEach(c => c.click());
    check('chipsBack', $$q('#typeChips .ws-chip').every(c => c.classList.contains('on')));
    $q('#layoutSeg [data-layout="grid"]').click();

    // The stack dialog for the night
    Seleno.setMode('basic');
    fixtureNight().click();
    await until(() => txt('groupTitle').startsWith('M42'));
    $q('#btnStackGroup').click();
    check('stackDialog', await until(() => $q('#stackDialog').open));
    const frameRows = txt('stackFrameRows');
    check('stackLights', frameRows.includes('10 lights') && frameRows.includes('2 rejected'), frameRows);
    check('stackDarks', frameRows.includes('4 darks') && frameRows.includes('Same night'), frameRows);
    check('stackBias', frameRows.includes('4 bias'), frameRows);
    check('stackFlatsOffered', frameRows.includes('3 from'), frameRows);
    check('stackEstimate', txt('stackEstimate').startsWith('About'), txt('stackEstimate'));
    check('stackSource', txt('stackSource').includes('JPEGs'), txt('stackSource'));
    check('stackWaitsForEngine', $q('#btnStackStart').disabled && txt('stackFootNote').includes("isn't built yet"), txt('stackFootNote'));
    await shot('stack-basic');
    Seleno.setMode('advanced');
    await sleep(200);
    $$q('#stackFrameRows .ws-btn').find(b => b.textContent === 'Use them').click();
    check('flatsUsed', txt('stackFrameRows').includes('3 flats'), txt('stackFrameRows'));
    check('settingsShown', visible($q('#stackDialog [data-setting="method"]')) && $q('#stackDialog [data-setting="method"]').value === 'winsorized');
    await shot('stack-advanced');
    $q('#stackDialog').close();

    // Saved stacks and the editor
    $q('.ws-toolbar .ws-tab[data-view="stacks"]').click();
    check('stackCards', await until(() => $$q('#stackCards .ws-stack-card').length === 1), $$q('#stackCards .ws-stack-card').length);
    await shot('stacks');
    $$q('#stackCards .ws-btn').find(b => b.textContent === 'Edit').click();
    check('editor', await until(() => !$q('#viewEditor').hidden && $q('#editorImg').naturalWidth > 0, 8000));
    check('editorHash', location.hash.startsWith('#edit='), location.hash);
    check('editorInfo', txt('editorInfo').includes('4 darks'), txt('editorInfo'));
    check('editorReport', txt('editorReport').includes('Left out'), txt('editorReport').slice(0, 100));
    check('editorWaits', $q('#editorControls').disabled && visible($q('#editorEngineNote')), txt('editorEngineNote'));
    check('editorSliders', $q('[data-param="stretch.target"]').value === '0.2', $q('[data-param="stretch.target"]').value);
    await shot('editor');
    $q('#btnSaveJpegToAlbum').click();
    check('savedToAlbum', await until(() => txt('editorSaveStatus').startsWith('Saved'), 8000), txt('editorSaveStatus'));

    // A result saved through the real uploader, with a master big enough for two chunks
    const { saveStackResult } = await import('/static/js/album/stacks.js');
    const jpeg = await new Promise(resolve => {
        const c = document.createElement('canvas');
        c.width = 60; c.height = 40;
        c.getContext('2d').fillRect(0, 0, 30, 20);
        c.toBlob(resolve, 'image/jpeg', 0.8);
    });
    const header = new TextEncoder().encode('SIMPLE  =                    T'.padEnd(2880, ' '));
    const master = new Blob([header, new Uint8Array(40 * 1024 * 1024)]);
    const preview = new Uint16Array(4 * 3 * 3).fill(0x3c00);
    const saved = await saveStackResult({
        name: 'E2E upload', target: 'M42', kind: 'stack', width: 900, height: 600, linear: true,
        frames_used: 10, frames_total: 12, total_exposure_s: 300, settings: { method: 'winsorized' },
        report: { frames: [] }, preview: { width: 4, height: 3, data: preview }, auto: jpeg, thumb: jpeg, master
    });
    check('uploaded', saved.state === 'ready' && saved.has_master && saved.master_bytes === master.size, saved);
    $q('.ws-toolbar .ws-tab[data-view="activity"]').click();
    check('activity', await until(() => txt('jobHistory').includes('Saving E2E upload')), txt('jobHistory'));
    $q('.ws-toolbar .ws-tab[data-view="stacks"]').click();
    check('twoStacks', await until(() => $$q('#stackCards .ws-stack-card').length === 2));
    $q('.ws-toolbar .ws-tab[data-view="photos"]').click();
    check('processedFrame', await until(async () => {
        const data = await (await fetch('/album/api/library')).json();
        return data.frames.some(f => f.frame_type === 'processed');
    }));
    Seleno.setMode('basic');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
