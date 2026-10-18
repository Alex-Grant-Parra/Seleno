// The album at phone width
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const overflow = () => document.documentElement.scrollWidth - window.innerWidth;
    try {
        Seleno.setMode('basic');
        check('albumReady', await until(() => $$q('#groupContent .ws-tile').length > 0, 10000));
        $q('#groupContent .ws-tile').click();
        await sleep(500);
        check('albumBasicFits', overflow() <= 0, overflow());
        await shot('album-basic');
        Seleno.setMode('advanced');
        await sleep(400);
        check('albumAdvancedFits', overflow() <= 0, overflow());
        await shot('album-advanced');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
