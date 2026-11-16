// The drawing map, opened from a telescope's page: at the telescope's site, a drag across
// the sky marks an exclusion zone, which is named and saved to the telescope
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const D = Math.PI / 180;
    const screenAt = (alt, az) => project(horizonToView(altazToXYZ(alt, az)));
    const mouse = (target, type, [x, y]) => target.dispatchEvent(new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true }));
    const drag = (from, to) => {
        mouse(canvas, 'mousedown', from);
        mouse(window, 'mousemove', [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2]);
        mouse(window, 'mousemove', to);
        mouse(window, 'mouseup', to);
        canvas.dispatchEvent(new MouseEvent('click', { clientX: to[0], clientY: to[1], bubbles: true }));
    };
    try {
        check('loaded', await until(() => zoneEditorZones.length >= 2 && txt('ze-connection') === 'Connected'
              && getComputedStyle(loading).display === 'none', 20000), zoneEditorZones.length);
        check('atTheTelescopesSite', latInput.value === '51.46' && lonInput.value === '-2.55' && latInput.readOnly,
              [latInput.value, lonInput.value]);
        check('noTrackingPanel', !visible($q('#tracking-info')));
        // Right-drag turns the sky without the browser's menu in the way
        const menu = new MouseEvent('contextmenu', { clientX: 200, clientY: 300, button: 2, bubbles: true, cancelable: true });
        canvas.dispatchEvent(menu);
        check('noContextMenu', menu.defaultPrevented);
        const before = zoneEditorZones.length;

        // Looking around isn't drawing
        rotY = 300 * D; rotX = 0.3; zoom = 1; draw();
        drag(screenAt(30, 290), screenAt(10, 310));
        check('dragTurnsTheSky', zoneEditorZones.length === before && Math.abs(rotY - 300 * D) > 1e-6);

        // Draw one in the west-north-west, from 30 deg up at az 290 to 10 deg up at az 310
        rotY = 300 * D; rotX = 0.3; draw();
        $q('#ze-draw').click();
        check('drawMode', $q('#ze-draw').getAttribute('aria-pressed') === 'true');
        drag(screenAt(30, 290), screenAt(10, 310));
        check('added', zoneEditorZones.length === before + 1, zoneEditorZones.length);
        const zone = zoneEditorZones[zoneEditorZones.length - 1];
        check('box', Math.abs(zone.az[0] - 290) <= 1 && Math.abs(zone.az[1] - 310) <= 1 && zone.alt[0] === -90
              && Math.abs(zone.alt[1] - 30) <= 1, zone);
        check('formOpen', visible($q('#ze-form')) && zoneEditorSelected === zoneEditorZones.length - 1);
        $q('#ze-name').value = 'Shed roof';
        $q('#ze-name').dispatchEvent(new Event('input'));
        $q('#ze-reason').value = 'physical';
        $q('#ze-reason').dispatchEvent(new Event('change'));
        check('named', zone.name === 'Shed roof' && zone.reason === 'physical');
        draw();
        await shot('drawn');

        // Drawn across north: the azimuths wrap
        rotY = 0; rotX = 0.3; draw();
        $q('#ze-to-ground').checked = false;
        drag(screenAt(40, 350), screenAt(30, 10));
        const north = zoneEditorZones[zoneEditorZones.length - 1];
        check('wrapsThroughNorth', Math.abs(north.az[0] - 350) <= 1 && Math.abs(north.az[1] - 10) <= 1
              && Math.abs(north.alt[0] - 30) <= 1, north);
        $q('#ze-delete').click();
        check('deleted', zoneEditorZones.length === before + 1);

        $q('#ze-save').click();
        check('saved', await until(() => txt('ze-status').includes('Saved')), txt('ze-status'));
        check('savedOnTelescope', zoneEditorZones.some(z => z.name === 'Shed roof' && z.reason === 'physical'));
        check('notDirty', !zoneEditorDirty);

        // Picking from the list turns the view to the zone
        $q('#ze-draw').click();
        const items = $$q('#ze-list .ze-item');
        items[items.length - 1].click();
        check('lookedAt', Math.abs(((rotY / D) % 360) - 300) < 2, rotY / D);
        draw();
        await shot('saved');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
