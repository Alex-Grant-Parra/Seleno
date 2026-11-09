// A telescope's own page (My Telescopes > the telescope): its exclusion zones as a table,
// locked ones read-only, a new one saved to the telescope, and the way to the drawing map
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const rows = () => $$q('#tz-rows tr');
    const field = (tr, key) => tr.querySelector(`[data-key="${key}"]`);
    try {
        check('ready', await until(() => rows().length >= 2, 15000), rows().length);
        const before = rows().length;
        check('connected', txt('tz-connection') === 'Connected', txt('tz-connection'));
        const pier = rows().find(tr => field(tr, 'name').value === 'Pier');
        check('lockedIsReadOnly', pier && field(pier, 'az0').disabled && !!pier.dataset.locked);
        const door = rows().find(tr => field(tr, 'name').value === 'Next door');
        check('unlockedIsEditable', door && !field(door, 'az0').disabled && field(door, 'reason').value === 'privacy');
        check('drawLink', /\/telescopes\/\d+\/zones\/map$/.test($q('#tz-draw').getAttribute('href')), $q('#tz-draw').getAttribute('href'));

        $q('#tz-add').click();
        const added = rows()[rows().length - 1];
        const set = (key, value) => { const el = field(added, key); el.value = value; el.dispatchEvent(new Event('input')); };
        set('name', 'Bedroom window'); set('az0', '200'); set('az1', '230'); set('alt0', '-90'); set('alt1', '25');
        check('undoShown', visible($q('#tz-revert')));
        await shot('editing');
        $q('#tz-save').click();
        check('saved', await until(() => txt('tz-note').includes('Saved') && rows().length === before + 1), txt('tz-note'));
        check('savedRow', rows().some(tr => field(tr, 'name').value === 'Bedroom window'));

        // A half-filled row stays on the page with a warning
        $q('#tz-add').click();
        $q('#tz-save').click();
        await sleep(200);
        check('incompleteRowWarned', txt('tz-note').includes('Fill in'), txt('tz-note'));
        $q('#tz-revert').click();
        check('undo', rows().length === before + 1, rows().length);
        await shot('saved');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
