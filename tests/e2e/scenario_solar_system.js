// Solar-system objects carry a kind, and each kind has its own checkbox:
// hiding the dwarf planets hides Pluto (drawn and clickable) and nothing else
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const D = Math.PI / 180;
    const find = name => planetsList.find(p => p.name === name);
    // Where a body is on the screen, the way the click picker works it out
    const screenAt = obj => {
        const { altDeg, azDeg } = radecToAltAz(obj.ra, obj.dec, new Date(), parseFloat(latInput.value), parseFloat(lonInput.value));
        let [x, y, z] = altazToXYZ(altDeg, azDeg);
        [x, y, z] = horizonToView([x, y, z]);
        if (z <= 0) return null;
        const [cx, cy] = project([x, y, z]);
        return cx > 40 && cy > 40 && cx < width - 40 && cy < height - 40 ? [cx, cy] : null;
    };
    // Summed colour of a small square round a screen point
    const brightness = ([cx, cy]) => {
        const s = canvas.width / width;
        const d = ctx.getImageData(Math.round(cx * s) - 3, Math.round(cy * s) - 3, 7, 7).data;
        let sum = 0;
        for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
        return sum;
    };
    try {
        check('bodiesLoaded', await until(() => find('Pluto') && find('Mars'), 15000), planetsList.map(p => p.name));
        const pluto = find('Pluto'), mars = find('Mars');
        check('plutoKind', pluto.kind === 'dwarf_planet', pluto.kind);
        check('marsKind', mars.kind === 'planet', mars.kind);
        check('moonKind', find('Moon').kind === 'moon', find('Moon').kind);
        check('toggleShown', visible(showDwarfPlanets) && showDwarfPlanets.checked);
        // Helios' dwarf planets and asteroids, each kind with its own checkbox
        const vesta = find('Vesta'), sedna = find('Sedna'), eris = find('Eris');
        check('smallBodies', vesta && vesta.kind === 'asteroid' && sedna && sedna.kind === 'tno'
              && eris && eris.kind === 'dwarf_planet', planetsList.map(p => `${p.name}:${p.kind}`));
        check('smallBodyToggles', visible(showAsteroids) && visible(showTnos));
        check('asteroidsFollowLimit', !bodyWithinMag(sedna, 4.0) && bodyWithinMag(eris, 4.0));
        showAsteroids.checked = false;
        check('asteroidToggle', !bodyShown(vesta) && bodyShown(eris) && bodyShown(sedna));
        showAsteroids.checked = true;

        // Pluto (mag ~14.6) is shown at the default limit of 4: planets and
        // dwarf planets ignore the limit unless the advanced setting says so
        autoMagnitudeZoom.checked = false;
        magnitudeZoomEnabled = false;
        magFilter.value = '4.0';
        check('followMagOffByDefault', bodiesFollowMag && !bodiesFollowMag.checked);
        check('plutoIgnoresLimit', bodyWithinMag(pluto, 4.0));
        zoom = 3;
        let at = null;
        for (let ry = 0; ry < 360 && !at; ry += 10) {
            for (let rx = -1.2; rx <= 1.2 && !at; rx += 0.2) {
                rotY = ry * D; rotX = rx;
                at = screenAt(pluto);
            }
        }
        check('plutoOnScreen', at, at);
        draw();
        await until(() => planetImages[pluto.icon], 5000);
        draw();
        const shown = brightness(at);
        check('plutoPicked', selectObjectAt(at[0], at[1]) && window.currentStarData && window.currentStarData.name === 'Pluto',
              window.currentStarData && window.currentStarData.name);
        await shot('pluto-shown');

        // Following the limit: Pluto goes at mag 4, comes back at the slider's top
        bodiesFollowMag.checked = true;
        bodiesFollowMag.dispatchEvent(new Event('change'));
        draw();
        const followed = brightness(at);
        check('plutoFollowsLimit', shown - followed > 2000 && !bodyWithinMag(pluto, 4.0) && bodyWithinMag(mars, 4.0),
              { shown, followed });
        let saved = null;
        try { saved = localStorage.getItem('starMap.bodiesFollowMag'); } catch {}
        check('followMagRemembered', saved === '1', saved);
        magFilter.value = magFilter.max;
        draw();
        check('plutoWithinRaisedLimit', bodyWithinMag(pluto, parseFloat(magFilter.value)), magFilter.value);
        bodiesFollowMag.checked = false;
        bodiesFollowMag.dispatchEvent(new Event('change'));
        magFilter.value = '4.0';
        draw();

        showDwarfPlanets.checked = false;
        showDwarfPlanets.dispatchEvent(new Event('change'));
        draw();
        const hidden = brightness(at);
        check('plutoHidden', shown - hidden > 2000, { shown, hidden });
        check('plutoNotShown', !bodyShown(pluto) && bodyShown(mars));
        window.currentStarData = null;
        const picked = selectObjectAt(at[0], at[1]);
        check('plutoNotPicked', !(picked && window.currentStarData && window.currentStarData.name === 'Pluto'),
              window.currentStarData && window.currentStarData.name);
        await shot('pluto-hidden');

        // The planets checkbox no longer covers Pluto, and vice versa
        showDwarfPlanets.checked = true;
        showPlanets.checked = false;
        check('planetsToggleSeparate', bodyShown(pluto) && !bodyShown(mars) && !bodyShown(find('Moon')));
        showPlanets.checked = true;

        // Offline, the stored table carries the kinds too
        const table = await loadPlanetTable(true);
        const offline = planetsFromTable(new Date());
        check('offlineKinds', offline && offline.find(p => p.name === 'Pluto').kind === 'dwarf_planet', table && table.count);
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
