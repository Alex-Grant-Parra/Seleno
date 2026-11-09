// The star map tints the selected telescope's dead zones, where the telescope
// says it never points, and warns about objects inside them
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const D = Math.PI / 180;
    // The canvas colour at a telescope (alt, az), with the zones shown or not
    const colourAt = (alt, az, shown) => {
        showDeadZones.checked = shown;
        draw();
        const p = project(deadZonePoint(deadZoneMapper(parseFloat(latInput.value), parseFloat(lonInput.value), new Date()), alt, az));
        const scale = canvas.width / width;
        return Array.from(ctx.getImageData(Math.round(p[0] * scale), Math.round(p[1] * scale), 1, 1).data);
    };
    try {
        check('zonesLoaded', await until(() => deadZones.length === 2, 15000), deadZones.length);
        check('siteFromStatus', deadZoneSite && Math.abs(deadZoneSite.lat - 51.4623) < 1e-6, deadZoneSite);
        check('toggleShown', visible(showDeadZones));
        // Right-drag turns the sky without the browser's menu in the way
        const menu = new MouseEvent('contextmenu', { clientX: 200, clientY: 300, button: 2, bubbles: true, cancelable: true });
        canvas.dispatchEvent(menu);
        check('noContextMenu', menu.defaultPrevented);

        // From the telescope's own site, facing east, a little up
        latInput.value = '51.46'; lonInput.value = '-2.55';
        rotY = 105 * D; rotX = 0.3; zoom = 1;
        const off = colourAt(15, 105, false), on = colourAt(15, 105, true);
        check('privacyTinted', on[0] - off[0] > 15 && on[2] - off[2] > 25, { off, on });
        const outside = colourAt(50, 105, true), outsideOff = colourAt(50, 105, false);
        check('aboveIsClear', Math.abs(outside[2] - outsideOff[2]) < 4, { outside, outsideOff });
        const zone = deadZoneAtMapAltAz(15, 105);
        check('hitTest', zone && zone.name === 'Next door', zone);
        check('hitTestOutside', deadZoneAtMapAltAz(50, 105) === null);
        showDeadZones.checked = true;
        draw();
        await shot('east-privacy');

        // The locked pier zone, through north
        rotY = 0; rotX = 0.15;
        const pierOff = colourAt(5, 0, false), pierOn = colourAt(5, 0, true);
        check('pierTinted', pierOn[0] - pierOff[0] > 20, { pierOff, pierOn });
        showDeadZones.checked = true;
        draw();
        await shot('north-pier');

        // From somewhere else (London): carried across through RA/Dec, still drawn
        latInput.value = '51.51'; lonInput.value = '-0.13';
        rotY = 105 * D; rotX = 0.3;
        const far = colourAt(15, 105, true), farOff = colourAt(15, 105, false);
        check('otherPlaceTinted', far[2] - farOff[2] > 25, { far, farOff });
        showDeadZones.checked = true;
        draw();
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
