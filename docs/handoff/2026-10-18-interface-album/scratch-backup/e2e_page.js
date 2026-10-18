// Drives the interface page in the end-to-end test and posts what it saw
(async () => {
    const R = { errors: [], checks: {} };
    window.addEventListener('error', e => R.errors.push(`error: ${e.message} @ ${e.filename}:${e.lineno}`));
    const origError = console.error;
    console.error = (...args) => { R.errors.push('console.error: ' + args.map(a => (a && a.stack) || String(a)).join(' ')); origError(...args); };
    window.alert = (message) => R.errors.push('alert: ' + message);
    window.confirm = () => true;
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const until = async (fn, ms = 8000) => {
        const start = Date.now();
        while (Date.now() - start < ms) {
            try { if (fn()) return true; } catch (e) { /* not yet */ }
            await sleep(100);
        }
        return false;
    };
    const text = id => document.getElementById(id).textContent;

    try {
        R.checks.selected = await until(() => selectedTelescopeId === 'e2e-scope');
        R.checks.cameraSelects = await until(() => document.getElementById('isoSelect').options.length === 4);
        R.checks.presets = ['shutterSpeedSelect', 'isoSelect', 'apertureSelect', 'photoFormat'].map(id => document.getElementById(id).value);
        R.checks.deviceTab = !document.getElementById('deviceTabItem').classList.contains('d-none');
        R.checks.statusRendered = await until(() => text('telescopeStatusGrid').includes('Mount'));
        R.checks.statusText = text('telescopeStatusGrid');
        R.checks.planets = await until(() => document.getElementById('planetSelect').options.length === 3, 5000);
        R.checks.cameraStatus = await until(() => text('cameraStatusLine').includes('75%'), 5000);

        // Live view: real H.264 through the relay, decoded with WebCodecs
        toggleLiveView();
        R.checks.h264Playing = await until(() => liveViewPlayer.playingH264, 12000);
        await sleep(1500);
        const canvas = document.getElementById('liveViewCanvas');
        R.checks.canvasSize = [canvas.width, canvas.height];
        const ctx = canvas.getContext('2d');
        const sample = [];
        for (const [fx, fy] of [[0.1, 0.1], [0.5, 0.5], [0.9, 0.9]]) {
            sample.push([...ctx.getImageData(Math.floor(canvas.width * fx), Math.floor(canvas.height * fy), 1, 1).data]);
        }
        R.checks.pixels = sample;
        R.checks.overlayHidden = document.getElementById('liveViewOverlay').classList.contains('d-none');
        R.checks.badge = text('liveViewBadge');
        R.checks.histogram = text('liveHistogramStats');

        // A command, and a job that's polled until done
        R.checks.estop = await telescopeAction('estop_status');
        await telescopeAction('park');
        R.checks.jobDone = await until(() => text('jobStatusLine').includes('done'), 8000);
        R.checks.jobLine = text('jobStatusLine');

        // Plan builder
        window.CapturePlanBuilder.open();
        await sleep(400);
        R.checks.addMenuItems = document.querySelectorAll('#addStepMenu button').length;
        R.checks.presetItems = await until(() => document.querySelectorAll('#presetMenu button').length === 4, 4000);
        window.CapturePlanBuilder.loadPlan({ name: 'E2E deep sky', steps: [
            { type: 'goto', name: 'M42', ra: 83.82, dec: -5.39, centre: true },
            { type: 'light', count: 5, exposure_s: 2, iso: '800' },
            { type: 'dark', count: 3, exposure_s: 2 }
        ] });
        R.checks.stepCards = document.querySelectorAll('#planSteps .plan-step').length;
        R.checks.estimateShown = await until(() => text('planEstimate').includes('Frames8'), 5000);
        R.checks.estimate = text('planEstimate');
        // Add a step from the menu, then edit a field and see the estimate follow
        [...document.querySelectorAll('#addStepMenu button')].find(b => b.textContent.startsWith('Bias')).click();
        R.checks.stepCardsAfterAdd = document.querySelectorAll('#planSteps .plan-step').length;
        R.checks.estimateFollows = await until(() => text('planEstimate').includes('Frames38'), 5000);
        // An invalid value shows the server's error
        const lightCount = document.querySelectorAll('#planSteps .plan-step')[1].querySelector('input[type=number]');
        lightCount.value = '5000';
        lightCount.dispatchEvent(new Event('input'));
        R.checks.errorShown = await until(() => !document.getElementById('planErrors').classList.contains('d-none'), 5000);
        R.checks.errorText = text('planErrors');
        lightCount.value = '5';
        lightCount.dispatchEvent(new Event('input'));
        await until(() => document.getElementById('planErrors').classList.contains('d-none'), 5000);

        window.CapturePlanBuilder.start();
        R.checks.planShown = await until(() => !document.getElementById('planBox').classList.contains('d-none'), 8000);
        R.checks.planTitle = text('planTitle');
        planControl('pause');
        R.checks.paused = await until(() => text('planState') === 'paused', 6000);

        // Stop live view
        toggleLiveView();
        R.checks.liveViewStopped = await until(() => !liveViewPlayer.isRunning, 4000);
    } catch (e) {
        R.errors.push('exception: ' + e + ' ' + (e.stack || ''));
    }
    await fetch('/e2e-result', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(R) });
})();
