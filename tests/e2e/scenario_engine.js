// The processing engine in a real browser: Web Workers, OPFS and the Cache API.
// Measures photos, stacks the fixture night through the stack dialog (JPEGs, with
// darks and bias), opens the result in the live editor, starts a project and checks a
// second run skips what's in it, lucky-images a SER video, and (with E2E_CR2) reads a
// real Canon RAW. Runs after (or without) the album scenario.
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const library = async () => (await (await fetch('/album/api/library')).json()).frames;
    const stacks = async () => (await (await fetch('/album/api/stacks')).json()).stacks;
    const fixtureNight = () => $$q('#groupNav button').find(b => (b.querySelector('.ws-count') || {}).textContent === '12');
    const tiles = () => $$q('#groupContent .ws-tiles .ws-tile');
    try {
        const { engine } = await import('/static/js/album/engine.js');
        check('engineAvailable', engine.available);
        check('opfs', !!(navigator.storage && navigator.storage.getDirectory));
        Seleno.setMode('advanced');
        check('nav', await until(() => fixtureNight(), 10000), $$q('#groupNav button').map(b => b.textContent));
        fixtureNight().click();
        check('tiles', await until(() => tiles().length >= 10, 10000), tiles().length);

        // Measure three photos
        tiles().slice(0, 3).forEach(t => t.dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true })));
        const names = tiles().slice(0, 3).map(t => t.dataset.name);
        $q('#btnBulkMeasure').click();
        let measured = [];
        check('measured', await until(async () => {
            const lib = await library();
            measured = names.map(n => lib.find(f => f.name === n).metrics || {});
            return measured.every(m => m.width === 900 && m.source === 'jpeg');
        }, 60000), measured);
        check('measuredStars', measured.every(m => m.stars > 50 && m.fwhm > 1.5 && m.fwhm < 6), measured.map(m => [m.stars, m.fwhm, m.ecc]));
        $q('#btnClearSelection').click();

        // Stack the night through the dialog
        const before = (await stacks()).length;
        $q('#btnStackGroup').click();
        check('dialog', await until(() => $q('#stackDialog').open));
        check('footNote', txt('stackFootNote').includes('closing the tab'), txt('stackFootNote'));
        $q('#stackName').value = 'E2E engine stack';
        $q('#btnStackStart').click();
        let made = null;
        const t0 = Date.now();
        check('stacked', await until(async () => {
            made = (await stacks()).find(s => s.name === 'E2E engine stack' && s.state === 'ready');
            return !!made;
        }, 180000), (await stacks()).map(s => [s.name, s.state]));
        R.checks.stackSeconds = Math.round((Date.now() - t0) / 1000);
        if (made) {
            const full = (await (await fetch(`/album/api/stacks/${made.id}`)).json()).stack;
            const report = full.report || {};
            check('stackCount', (await stacks()).length === before + 1);
            R.checks.frameReasons = (report.frames || []).map(f => `${f.name}: ${f.accepted ? 'in' : f.reason}`);
            check('stackUsed', made.frames_used >= 8 && made.width === 900 && made.height === 600, [made.frames_used, made.width, made.height]);
            check('stackMode', report.combine && report.combine.mode === 'memory', report.combine);
            check('stackMasters', report.masters && report.masters.dark && report.masters.dark.frames === 4 && report.masters.bias.frames === 4, report.masters);
            const blurred = (report.frames || []).find(f => f.name === 'IMG_0405.JPG');
            check('blurredLeftOut', !blurred || (!blurred.accepted && /blurred|sharpest|stretched/.test(blurred.reason || '')), blurred);
            check('stackMaster', made.has_master && made.master_bytes > 900 * 600 * 12, [made.has_master, made.master_bytes]);
            const head = await (await fetch(made.master_url, { headers: { Range: 'bytes=0-8' } })).text();
            check('masterIsFits', head === 'SIMPLE  =', head);
            const thumb = await fetch(made.thumb_url);
            check('thumb', thumb.ok && (await thumb.blob()).size > 1000);

            // The live editor on it
            location.hash = `#edit=${made.id}`;
            check('editorLive', await until(() => $q('#editorCanvas') && $q('#editorCanvas').width > 0 && !$q('#editorControls').disabled, 20000), txt('editorSaveStatus'));
            const rendered = new Promise(resolve => $q('#editorStage').addEventListener('develop-rendered', resolve, { once: true }));
            const slider = $q('[data-param="stretch.target"]');
            slider.value = '0.3';
            slider.dispatchEvent(new Event('input', { bubbles: true }));
            check('rerendered', await Promise.race([rendered.then(() => true), sleep(20000).then(() => false)]));
            await shot('editor-live');
            $q('#btnSaveJpegToAlbum').click();
            check('exportedToAlbum', await until(() => txt('editorSaveStatus').startsWith('Saved to the album'), 30000), txt('editorSaveStatus'));
            location.hash = '';
            await until(() => !$q('#viewPhotos').hidden);
        }

        // A project: start one with the night, then a second run skips what's in it
        fixtureNight().click();
        await until(() => tiles().length >= 10);
        $q('#btnStackGroup').click();
        await until(() => $q('#stackDialog').open);
        check('projectChoice', await until(() => $q('#stackProject option[value="new"]')));
        $q('#stackName').value = 'E2E project';
        $q('#stackProject').value = 'new';
        $q('#stackProject').dispatchEvent(new Event('change'));
        $q('#btnStackStart').click();
        let project = null;
        check('projectMade', await until(async () => {
            const list = (await (await fetch('/album/api/projects')).json()).projects;
            project = list.find(p => p.name === 'E2E project' && p.generation === 1);
            return !!project;
        }, 180000));
        if (project) {
            check('projectFrames', project.frames_total >= 6 && project.nights.length === 1, [project.frames_total, project.nights]);
            const sum = await fetch(`/album/projects/${project.id}/current/sum.fits`, { headers: { Range: 'bytes=0-8' } });
            check('projectTotals', sum.status === 206 && (await sum.text()) === 'SIMPLE  =');
            check('projectStack', await until(async () => {
                const p = (await (await fetch(`/album/api/projects/${project.id}`)).json()).project;
                return p.stack_id && (await stacks()).some(s => s.id === p.stack_id);
            }, 30000));
            $q('#btnStackGroup').click();
            await until(() => $q('#stackDialog').open);
            await until(() => $q(`#stackProject option[value="${project.id}"]`));
            $q('#stackProject').value = project.id;
            $q('#stackProject').dispatchEvent(new Event('change'));
            check('projectNote', txt('stackProjectNote').includes('frames so far'), txt('stackProjectNote'));
            $q('#btnStackStart').click();
            check('projectSkips', await until(() => document.body.textContent.includes('already in the project'), 90000));
            $q('.ws-toolbar .ws-tab[data-view="stacks"]').click();
            check('projectListed', await until(() => txt('projectList').includes('E2E project'), 10000), txt('projectList').slice(0, 200));
            await shot('projects');
        }

        // Lucky imaging on a SER video
        $q('.ws-toolbar .ws-tab[data-view="photos"]').click();
        await until(() => !$q('#viewPhotos').hidden);
        const all = $$q('#groupNav button').find(b => b.textContent.startsWith('All photos'));
        all.click();
        let serTile = null;
        check('serShown', await until(() => (serTile = $q('#groupContent [data-name="saturn.ser"]')), 10000));
        if (serTile) {
            serTile.click();
            check('luckyButton', await until(() => visible($q('#btnLucky'))));
            $q('#btnLucky').click();
            check('luckyDialog', await until(() => $q('#luckyDialog').open));
            $q('#luckyName').value = 'E2E Saturn';
            $q('#btnLuckyStart').click();
            let lucky = null;
            check('luckyDone', await until(async () => {
                lucky = (await stacks()).find(s => s.name === 'E2E Saturn' && s.state === 'ready');
                return !!lucky;
            }, 120000), (await stacks()).map(s => s.name));
            if (lucky) {
                check('luckyKind', lucky.kind === 'planetary' && lucky.frames_total === 60 && lucky.frames_used === 15, [lucky.kind, lucky.frames_used, lucky.frames_total]);
                const full = (await (await fetch(`/album/api/stacks/${lucky.id}`)).json()).stack;
                const kept = (full.report.frames || []).filter(f => f.accepted).map(f => f.frame);
                check('luckySharpest', kept.filter(i => i % 4 === 0).length >= 13, kept);
            }
        }

        // A real Canon RAW, when the server has one
        const lib = await library();
        const cr2 = lib.find(f => f.name === 'IMG_5000.CR2');
        R.checks.realRaw = !!cr2;
        if (cr2) {
            const out = await engine.measure([{ name: cr2.name, url: cr2.url, kind: 'raw' }]);
            const m = out[cr2.name] || {};
            check('cr2Measured', m.source === 'raw' && m.width === 5634 && m.height === 3752 && m.stars >= 0, m);
        }
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})();
