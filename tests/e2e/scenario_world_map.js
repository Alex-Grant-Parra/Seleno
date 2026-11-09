// The world map: phone layout (full-screen map, search bar, bottom sheet) at
// phone width, and the floating panels on desktop.
(async () => {
    const R = { checks: {}, failures: [] };
    const check = (name, ok, detail) => {
        R.checks[name] = detail === undefined ? !!ok : detail;
        if (!ok) R.failures.push(`${name}${detail === undefined ? '' : ': ' + JSON.stringify(detail)}`);
    };
    const overflow = () => document.documentElement.scrollWidth - window.innerWidth;
    const box = (s) => $q(s).getBoundingClientRect();
    const tap = (s) => $q(s).click();
    try {
        check('mapReady', await until(() => $q('.map-wrap.is-ready')));
        await sleep(800);
        check('fitsWidth', overflow() <= 0, overflow());
        // Weather comes from Open-Meteo: forecast times load and the rain layer draws tiles
        check('forecastTimesLoad', await until(() => txt('weather-time-value') === 'Now', 15000), txt('weather-time-value'));
        check('forecastSliderEnabled', !$q('#weather-time-slider').disabled);
        tap('[data-layer="rain"]');
        check('rainTilesDraw', await until(() => $$q('.leaflet-weather-pane .leaflet-tile-loaded').length > 0, 20000),
            $$q('.leaflet-weather-pane .leaflet-tile').length);
        await sleep(1000);
        await shot('rain');
        tap('[data-layer="rain"]');
        // Same breakpoints as map.css
        const landscape = window.innerWidth <= 900 && window.innerHeight <= 500 && window.innerWidth > window.innerHeight;
        const phone = window.innerWidth <= 900 && !landscape;

        if (landscape) {
            const sheet = box('.map-sheet');
            check('sideColumn', sheet.right >= window.innerWidth - 1 && sheet.width <= window.innerWidth / 2 + 1,
                [sheet.left, sheet.width]);
            check('searchLeftOfColumn', box('.map-search').right <= sheet.left, [box('.map-search').right, sheet.left]);
            check('opacityShownInColumn', visible($q('.map-opacity')));
            await shot('landscape');
        }

        if (phone) {
            const wrap = box('.map-wrap');
            const header = box('.site-header');
            check('mapUnderHeader', Math.abs(wrap.top - header.bottom) <= 1, [wrap.top, header.bottom]);
            check('mapFillsScreen', wrap.bottom >= window.innerHeight - 1 && wrap.width >= window.innerWidth - 1,
                [wrap.bottom, wrap.width]);
            check('pageHeadingHidden', box('.map-header').height <= 1);
            check('searchOnTop', box('.map-search').top - wrap.top < 30);
            const sheet = box('.map-sheet');
            check('sheetAtBottom', Math.abs(sheet.bottom - wrap.bottom) <= 1);
            check('sheetCompact', sheet.height < window.innerHeight * 0.35, sheet.height);
            check('opacityHiddenWhenCollapsed', !visible($q('.map-opacity')));
            check('legendHiddenWithoutBortle', !visible($q('.map-legend')));
            check('attributionAboveSheet', box('.leaflet-control-attribution').bottom <= sheet.top + 1,
                [box('.leaflet-control-attribution').bottom, sheet.top]);
            await shot('phone-collapsed');

            tap('[data-layer="bortle"]');
            await sleep(300);
            check('bortleTileOn', $q('[data-layer="bortle"]').closest('.layer-toggle').classList.contains('is-on'));
            check('legendShownWithBortle', visible($q('.map-legend')));
            await sleep(1500);
            await shot('phone-bortle');

            tap('#map-sheet-handle');
            await sleep(400);
            check('sheetExpands', $q('#map-sheet').classList.contains('is-expanded'));
            check('opacityShownWhenExpanded', visible($q('.map-opacity')));
            check('expandedBelowSearch', box('.map-sheet').top >= box('.map-search').bottom,
                [box('.map-sheet').top, box('.map-search').bottom]);
            await shot('phone-expanded');
            tap('#map-sheet-handle');
            await sleep(300);
            check('sheetCollapses', !$q('#map-sheet').classList.contains('is-expanded'));
        }

        // Search for a point: the popup shows the conditions there
        $q('#coord-search').value = 'nonsense';
        $q('#coord-form').requestSubmit();
        check('badCoordsFlagged', $q('#coord-form').classList.contains('is-invalid'));
        $q('#coord-search').value = '51.4769, 0.0005';
        $q('#coord-form').requestSubmit();
        check('pointPopup', await until(() => $q('.leaflet-popup .point-info-rows'), 15000));
        await sleep(600);
        const popup = box('.leaflet-popup');
        if (landscape) {
            check('popupClearOfColumn', popup.right <= box('.map-sheet').left + 1, [popup.right, box('.map-sheet').left]);
        }
        if (phone) {
            check('popupClearOfSheet', popup.bottom <= box('.map-sheet').top + 1, [popup.bottom, box('.map-sheet').top]);
        }
        check('popupClearOfSearch', popup.top >= box('.map-search').bottom - 1, [popup.top, box('.map-search').bottom]);
        await shot(landscape ? 'landscape-point' : phone ? 'phone-point' : 'desktop-point');
    } catch (error) {
        R.failures.push(`crashed: ${error.stack || error}`);
    }
    return R;
})()
