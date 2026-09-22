// Star map - solar-system objects: server ephemerides and their icons.

function getPlanetsMagRange() {
    if (!planetsList || planetsList.length === 0) return null;
    let pmin = Infinity, pmax = -Infinity;
    for (const p of planetsList) {
        const m = (p && typeof p.mag === 'number') ? p.mag : null;
        if (m == null || isNaN(m)) continue;
        if (m < pmin) pmin = m;
        if (m > pmax) pmax = m;
    }
    if (pmin === Infinity || pmax === -Infinity) return null;
    return { min: pmin, max: pmax };
}

// --- Dynamic planet updates (server-driven ephemerides) ---
let planetsRefreshTimer = null;

async function fetchPlanetsForDate(date) {
    try {
        const iso = date.toISOString();
        const res = await fetch(`/api/planets?datetime=${encodeURIComponent(iso)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return data; // [{name, ra, dec, mag, icon, type:'planet'}]
    } catch (err) {
        console.error('Failed to fetch planets:', err);
        return null;
    }
}

function replacePlanetsInScene(newPlanets) {
    if (!Array.isArray(newPlanets)) return;
    // Remove existing planet entries in-place (preserve stars array reference)
    for (let i = stars.length - 1; i >= 0; i--) {
        if (stars[i] && stars[i].type === 'planet') stars.splice(i, 1);
    }
    // Insert fresh planets
    const newList = [];
    for (const p of newPlanets) {
        if (!p || typeof p.ra !== 'number' || typeof p.dec !== 'number') continue;
        const obj = {
            name: p.name,
            ra: p.ra,   // degrees
            dec: p.dec, // degrees
            mag: p.mag,
            icon: p.icon,
            type: 'planet'
        };
        try { obj.xyz = radecToXYZ(obj.ra, obj.dec); } catch { obj.xyz = radecToXYZ(0,0); }
        stars.push(obj);
        newList.push(obj);
    }
    planetsList = newList;
}

async function refreshPlanetsForCurrentTime(force = false) {
    // Throttle planet updates to avoid excessive queries during live time mode
    const now = Date.now();
    if (!force && (now - lastPlanetUpdateTime) < PLANET_UPDATE_THROTTLE_MS) {
        // Skip planet update but still trigger draw for sky rotation
        draw();
        return;
    }
    
    let selectedDate = new Date();
    try { if (timeControl && timeControl.value) selectedDate = new Date(timeControl.value); } catch {}
    selectedDate.setSeconds(0, 0);
    const updated = await fetchPlanetsForDate(new Date(selectedDate.toISOString()));
    if (updated) {
        replacePlanetsInScene(updated);
        lastPlanetUpdateTime = now;
        draw();
    }
}

function schedulePlanetsRefresh() {
    if (planetsRefreshTimer) clearTimeout(planetsRefreshTimer);
    planetsRefreshTimer = setTimeout(() => {
        planetsRefreshTimer = null;
        refreshPlanetsForCurrentTime(true); // Force update for manual keyboard adjustments
    }, 250); // debounce rapid keypresses
}

// Planets are kept as a small object list; stars live in the typed arrays
let planetsList = [];    // updated when planets are replaced

function updatePlanetsList() {
    planetsList = [];
    for (let i = 0; i < stars.length; i++) {
        if (stars[i] && stars[i].type === 'planet') planetsList.push(stars[i]);
    }
}

// Preload planet icons with proper error handling
function preloadPlanetImages() {
    const loadPromises = [];
    console.log('Starting to preload planet images...');
    
    for (const obj of stars) {
        if (obj.type === "planet" && obj.icon && !planetImages[obj.icon]) {
            console.log(`Loading planet icon for ${obj.name}: ${obj.icon}`);
            const loadPromise = new Promise((resolve, reject) => {
                const img = new Image();
                img.onload = () => {
                    planetImages[obj.icon] = img;
                    console.log(`Successfully loaded icon for ${obj.name}`);
                    resolve();
                };
                img.onerror = () => {
                    console.warn(`Failed to load planet icon: ${obj.icon} for ${obj.name}`);
                    resolve(); // Continue even if image fails to load
                };
                img.src = obj.icon;
            });
            loadPromises.push(loadPromise);
        }
    }
    
    console.log(`Loading ${loadPromises.length} planet images...`);
    return Promise.all(loadPromises).then(() => {
        console.log('All planet images loaded. Cache contains:', Object.keys(planetImages));
    });
}
