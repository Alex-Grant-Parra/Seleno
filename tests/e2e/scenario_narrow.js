// Both pages at phone width: nothing wider than the screen, in either mode
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const overflow = () => document.documentElement.scrollWidth - window.innerWidth;
    try {
        Seleno.setMode('basic');
        check('interfaceReady', await until(() => selectedTelescopeId === 'e2e-scope' && !txt('chipCamera').includes('—')));
        await sleep(500);
        check('interfaceBasicFits', overflow() <= 0, overflow());
        await shot('interface-basic');
        Seleno.setMode('advanced');
        await sleep(400);
        check('interfaceAdvancedFits', overflow() <= 0, overflow());
        await shot('interface-advanced');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
