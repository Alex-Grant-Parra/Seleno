(function () {
    const mapElement = document.getElementById("world-map");
    if (!mapElement || typeof L === "undefined") {
        return;
    }

    const config = window.MAP_CONFIG || {};
    const statusEl = document.getElementById("map-status");
    const weatherOpacityInput = document.getElementById("weather-opacity");
    const bortleOpacityInput = document.getElementById("bortle-opacity");
    const weatherTimeSlider = document.getElementById("weather-time-slider");
    const weatherTimeValue = document.getElementById("weather-time-value");
    const layerInputs = Array.from(document.querySelectorAll("[data-layer]"));

    const openMeteoTileUrl = (config.openMeteoTileUrl || "").trim();
    const openMeteoAttribution = (config.openMeteoAttribution || "").trim() ||
        "Weather data &copy; Open-Meteo.com";
    const openMeteoBaseUrl = openMeteoTileUrl ||
        "https://map-tiles.open-meteo.com/data_spatial/dwd_icon/latest.json?time_step=current_time_1H";
    const openMeteoMetaUrl = (() => {
        try {
            const url = new URL(openMeteoBaseUrl);
            url.search = "";
            return url.toString();
        } catch (err) {
            return "";
        }
    })();
    const bortleUrl = (config.bortleTileUrl || "").trim();
    const bortleWmsUrl = (config.bortleWmsUrl || "").trim();
    const bortleWmsLayer = (config.bortleWmsLayer || "PostGIS:WA_2015").trim() || "PostGIS:WA_2015";
    const bortleWmsTime = (config.bortleWmsTime || "").trim();
    const bortleFixedUrl = (config.bortleFixedUrl || "").trim();
    const satelliteUrl = (config.satelliteTileUrl || "").trim() ||
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
    const satelliteAttribution = (config.satelliteAttribution || "").trim() ||
        "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics";
    const bortleAttribution = (config.bortleAttribution || "").trim() || "NASA GIBS VIIRS Night Lights";
    const bortleSourceUrl = bortleUrl || bortleWmsUrl || bortleFixedUrl;
    const weatherVariables = {
        rain: "rain",
        wind: "wind_gusts_10m",
        temperature: "temperature_2m"
    };
    const utcFormatter = new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "UTC"
    });

    const map = L.map(mapElement, {
        zoomControl: false,
        minZoom: 2,
        maxZoom: 19,
        worldCopyJump: true,
        maxBounds: L.latLngBounds(L.latLng(-85, -180), L.latLng(85, 180)),
        maxBoundsViscosity: 0.8
    });

    map.setView([20, 0], 2);
    L.control.zoom({ position: "bottomright" }).addTo(map);

    L.tileLayer(satelliteUrl, {
        attribution: satelliteAttribution,
        maxZoom: 19
    }).addTo(map);

    map.createPane("weatherPane");
    map.createPane("bortlePane");
    const weatherPane = map.getPane("weatherPane");
    const bortlePane = map.getPane("bortlePane");
    if (weatherPane) {
        weatherPane.style.zIndex = 450;
    }
    if (bortlePane) {
        bortlePane.style.zIndex = 460;
    }

    let weatherOpacity = weatherOpacityInput ? parseFloat(weatherOpacityInput.value) : 0.7;
    let bortleOpacity = bortleOpacityInput ? parseFloat(bortleOpacityInput.value) : 0.6;
    let weatherTimeline = [];
    let weatherTimeStep = "current_time_1H";

    const overlayLayers = {};

    // Marker used for search / info pin
    let infoMarker = null;

    // Separate from infoMarker so selecting other points never hides it.
    let locationMarker = null;

    function showCurrentLocation(lat, lon, popupHtml) {
        if (locationMarker) {
            locationMarker.setLatLng([lat, lon]);
        } else {
            locationMarker = L.circleMarker([lat, lon], {
                radius: 8,
                color: "#ffffff",
                weight: 2,
                fillColor: "#2e8bff",
                fillOpacity: 1,
                pane: "markerPane",
                interactive: true
            }).addTo(map);
        }
        locationMarker.bindPopup(popupHtml).openPopup();
    }

    function placePin(lat, lon, popupHtml) {
        if (infoMarker) {
            map.removeLayer(infoMarker);
            infoMarker = null;
        }
        infoMarker = L.marker([lat, lon]).addTo(map);
        if (popupHtml) {
            infoMarker.bindPopup(popupHtml).openPopup();
        }
    }

    async function fetchPointWeather(lat, lon) {
        try {
            const apiUrl = `https://api.open-meteo.com/v1/forecast?latitude=${encodeURIComponent(lat)}&longitude=${encodeURIComponent(lon)}&current_weather=true&hourly=precipitation,temperature_2m,wind_speed_10m&timezone=UTC`;
            const resp = await fetch(apiUrl);
            if (!resp.ok) {
                throw new Error(`Open-Meteo API error ${resp.status}`);
            }
            const data = await resp.json();

            const result = {
                latitude: lat,
                longitude: lon,
                temperature: null,
                wind: null,
                precipitation: null
            };

            if (data.current_weather) {
                result.temperature = data.current_weather.temperature;
                // current_weather reports windspeed (m/s or km/h depending on API), key is 'windspeed'
                result.wind = data.current_weather.windspeed || data.current_weather.wind_speed || null;
            }

            if (data.hourly && Array.isArray(data.hourly.time)) {
                const times = data.hourly.time;
                const now = new Date().toISOString().slice(0, 13) + ':00:00Z';
                // Find the index for the current hour (rough match)
                let idx = times.indexOf(now);
                if (idx === -1) {
                    // fallback: find closest by timestamp
                    const target = new Date();
                    let best = 0;
                    let bestDiff = Infinity;
                    for (let i = 0; i < times.length; i++) {
                        const t = new Date(times[i]);
                        const diff = Math.abs(t - target);
                        if (diff < bestDiff) {
                            bestDiff = diff;
                            best = i;
                        }
                    }
                    idx = best;
                }

                if (typeof idx === 'number' && idx >= 0) {
                    if (data.hourly.precipitation && data.hourly.precipitation.length > idx) {
                        result.precipitation = data.hourly.precipitation[idx];
                    }
                    if (data.hourly.temperature_2m && data.hourly.temperature_2m.length > idx) {
                        result.temperature = result.temperature ?? data.hourly.temperature_2m[idx];
                    }
                    if (data.hourly.wind_speed_10m && data.hourly.wind_speed_10m.length > idx) {
                        result.wind = result.wind ?? data.hourly.wind_speed_10m[idx];
                    }
                }
            }

            return result;
        } catch (err) {
            console.error('Point weather fetch failed', err);
            return null;
        }
    }

    // Looked up server-side: the light pollution map server blocks browser requests (no CORS).
    async function fetchBortleInfo(lat, lon) {
        try {
            const response = await fetch(`/map/bortle?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`);
            if (!response.ok) {
                throw new Error(`Bortle lookup failed (${response.status})`);
            }
            const data = await response.json();
            return Number.isFinite(data.bortle) ? data : null;
        } catch (err) {
            console.warn("Bortle lookup failed", err);
            return null;
        }
    }

    function setStatus(messages) {
        if (!statusEl) {
            return;
        }
        const text = messages.filter(Boolean).join(" ");
        statusEl.textContent = text;
        statusEl.classList.toggle("is-visible", Boolean(text));
    }

    function setLayerActive(name, isActive) {
        const layer = overlayLayers[name];
        if (!layer) {
            return;
        }
        if (isActive) {
            map.addLayer(layer);
        } else {
            map.removeLayer(layer);
        }
    }

    function formatWeatherTime(isoString) {
        return `${utcFormatter.format(new Date(isoString))} UTC`;
    }

    // `index` here refers to an index in `weatherTimeline` (0..N-1).
    // We keep track of `nowTimelineIndex` which says which timeline index corresponds to "Now".
    let nowTimelineIndex = -1;
    let weatherOriginalStartIndex = 0;

    function setWeatherTimeReadout(index) {
        if (!weatherTimeSlider || !weatherTimeValue) {
            return;
        }

        const safeIndex = Math.max(0, Math.min(index, Math.max(0, weatherTimeline.length - 1)));
        weatherTimeSlider.value = String(safeIndex);

        if (weatherTimeline.length === 0 || nowTimelineIndex === -1) {
            weatherTimeStep = "current_time_1H";
            weatherTimeValue.textContent = "Now";
            return;
        }

        if (safeIndex === nowTimelineIndex) {
            weatherTimeStep = "current_time_1H";
            weatherTimeValue.textContent = "Now";
            return;
        }

        const selectedTime = weatherTimeline[safeIndex];
        if (!selectedTime) {
            weatherTimeStep = "current_time_1H";
            weatherTimeValue.textContent = "Now";
            return;
        }

        // `valid_times_{n}` expects an index into the original `valid_times` array returned
        // by the open-meteo metadata. We preserved the `weatherOriginalStartIndex` when
        // slicing the timeline so convert back to the original index here.
        const originalIndex = weatherOriginalStartIndex + safeIndex;
        weatherTimeStep = `valid_times_${originalIndex}`;
        weatherTimeValue.textContent = formatWeatherTime(selectedTime);
    }

    async function loadWeatherTimeline() {
        if (!weatherTimeSlider || !weatherTimeValue || !openMeteoMetaUrl) {
            return;
        }

        weatherTimeSlider.disabled = true;
        weatherTimeValue.textContent = "Loading forecast times…";

        try {
            const response = await fetch(openMeteoMetaUrl);
            if (!response.ok) {
                throw new Error(`Open-Meteo metadata error ${response.status}`);
            }

            const data = await response.json();
            const validTimes = Array.isArray(data.valid_times) ? data.valid_times : [];

            // Determine the hour-aligned "now" index within the returned valid_times.
            const nowDate = new Date();
            nowDate.setMinutes(0, 0, 0);
            let nowIndex = -1;
            for (let i = 0; i < validTimes.length; i++) {
                try {
                    const t = new Date(validTimes[i]);
                    if (t.getTime() === nowDate.getTime()) {
                        nowIndex = i;
                        break;
                    }
                } catch (e) {
                    // ignore
                }
            }
            // If exact match not found, find the closest index before or at now.
            if (nowIndex === -1) {
                for (let i = 0; i < validTimes.length; i++) {
                    const t = new Date(validTimes[i]);
                    if (t > nowDate) {
                        nowIndex = Math.max(0, i - 1);
                        break;
                    }
                }
                if (nowIndex === -1 && validTimes.length > 0) {
                    nowIndex = validTimes.length - 1;
                }
            }

            // Provide one day (24h) of past coverage where available, plus up to 7 days (168h) forward.
            const pastHours = 24;
            const futureHours = 168;
            const startIndex = Math.max(0, (nowIndex === -1 ? 0 : nowIndex) - pastHours);
            const endIndex = Math.min(validTimes.length, startIndex + pastHours + futureHours + 1);

            weatherOriginalStartIndex = startIndex;
            weatherTimeline = validTimes.slice(startIndex, endIndex);

            // Track which index in the sliced timeline represents "Now"
            nowTimelineIndex = (nowIndex === -1) ? (weatherTimeline.length - 1) : Math.max(0, nowIndex - startIndex);

            // Configure slider to index into the sliced timeline.
            weatherTimeSlider.min = "0";
            weatherTimeSlider.max = String(Math.max(0, weatherTimeline.length - 1));
            weatherTimeSlider.step = "1";
            weatherTimeSlider.disabled = false;
            setWeatherTimeReadout(nowTimelineIndex);
        } catch (err) {
            console.error("Failed to load weather timeline", err);
            weatherTimeValue.textContent = "Forecast times unavailable";
        }
    }

    const omLib = window.OMWeatherMapLayer;
    const openMeteoReady = Boolean(omLib && omLib.addLeafletProtocolSupport && omLib.omProtocol);

    // Configure tile overlays once, then wire inputs to toggles.
    if (openMeteoReady && openMeteoBaseUrl) {
        const leafletAdapter = omLib.addLeafletProtocolSupport(L);
        leafletAdapter.addProtocol("om", omLib.omProtocol);

        const weatherLayerOptions = {
            opacity: weatherOpacity,
            pane: "weatherPane",
            attribution: openMeteoAttribution,
            maxZoom: 12
        };

        const createWeatherLayer = (name) => {
            const variable = weatherVariables[name];
            if (!variable) {
                return null;
            }

            if (!openMeteoBaseUrl) {
                return null;
            }
            const joiner = openMeteoBaseUrl.includes("?") ? "&" : "?";
            const url = `om://${openMeteoBaseUrl}${joiner}time_step=${encodeURIComponent(weatherTimeStep)}&variable=${encodeURIComponent(variable)}`;
            return leafletAdapter.createTileLayer(url, weatherLayerOptions);
        };

        const refreshWeatherLayers = () => {
            weatherLayerNames.forEach((name) => {
                const oldLayer = overlayLayers[name];
                const wasActive = oldLayer ? map.hasLayer(oldLayer) : false;
                if (wasActive) {
                    map.removeLayer(oldLayer);
                }

                overlayLayers[name] = createWeatherLayer(name);

                if (wasActive && overlayLayers[name]) {
                    map.addLayer(overlayLayers[name]);
                }
            });
        };

        overlayLayers.rain = createWeatherLayer("rain");
        // NOTE: some models (like dwd_icon) don't expose a `wind_speed_10m` variable.
        // Use `wind_gusts_10m` which is available for the default model, or
        // replace with a model that exposes wind speed if you prefer magnitude.
        overlayLayers.wind = createWeatherLayer("wind");
        overlayLayers.temperature = createWeatherLayer("temperature");

        weatherTimeSlider?.addEventListener("input", (event) => {
            const target = event.target;
            const nextIndex = Number(target.value || 0);
            setWeatherTimeReadout(nextIndex);
            refreshWeatherLayers();
        });

        loadWeatherTimeline();
    }

    if (bortleUrl) {
        overlayLayers.bortle = L.tileLayer(bortleUrl, {
            opacity: bortleOpacity,
            pane: "bortlePane",
            attribution: bortleAttribution,
            className: "bortle-layer"
        });
    } else if (bortleWmsUrl) {
        overlayLayers.bortle = L.tileLayer.wms(bortleWmsUrl, {
            layers: bortleWmsLayer,
            format: "image/png",
            transparent: true,
            opacity: bortleOpacity,
            pane: "bortlePane",
            attribution: bortleAttribution,
            ...(bortleWmsTime ? { time: bortleWmsTime } : {}),
            version: "1.3.0"
        });
    } else if (bortleFixedUrl) {
        // Fallback: use a single fixed world image as an overlay. Place a suitable
        // global PNG/SVG at `static/images/bortle_fixed.png` or set BORTLE_FIXED_URL.
        try {
            overlayLayers.bortle = L.imageOverlay(bortleFixedUrl, [[-85, -180], [85, 180]], {
                opacity: bortleOpacity,
                pane: "bortlePane",
                attribution: bortleAttribution,
                className: "bortle-layer"
            });
        } catch (err) {
            console.warn('Failed to create fixed Bortle image overlay', err);
        }
    }

    const weatherLayerNames = ["rain", "wind", "temperature"];
    const messages = [];

    if (!openMeteoReady) {
        messages.push("Open-Meteo map library failed to load; weather layers are unavailable.");
    }
    if (!bortleSourceUrl) {
        messages.push("Add BORTLE_TILE_URL, BORTLE_WMS_URL, or BORTLE_FIXED_URL to enable the Bortle overlay.");
    }
    setStatus(messages);

    layerInputs.forEach((input) => {
        const name = input.getAttribute("data-layer");
        const isWeatherLayer = weatherLayerNames.includes(name);
        const isAvailable = isWeatherLayer ? openMeteoReady : Boolean(bortleSourceUrl);

        if (!isAvailable) {
            input.checked = false;
            input.disabled = true;
            const wrapper = input.closest(".layer-toggle");
            if (wrapper) {
                wrapper.classList.add("is-disabled");
            }
            return;
        }

        if (input.checked) {
            setLayerActive(name, true);
        }

        input.addEventListener("change", (event) => {
            setLayerActive(name, event.target.checked);
        });
    });

    // --- Right-click / context menu: show info at a point ---
    map.on('contextmenu', async (e) => {
        const lat = e.latlng.lat;
        const lon = e.latlng.lng;
        const loadingHtml = `<div style="min-width:160px">Loading data for<br><strong>${lat.toFixed(5)}, ${lon.toFixed(5)}</strong>…</div>`;
        placePin(lat, lon, loadingHtml);

        const [weather, bortleInfo] = await Promise.all([
            fetchPointWeather(lat, lon),
            fetchBortleInfo(lat, lon)
        ]);
        let popupHtml = `<div style="min-width:200px"><strong>Coords:</strong> ${lat.toFixed(5)}, ${lon.toFixed(5)}<br>`;
        if (!weather) {
            popupHtml += `<em>Weather data unavailable</em>`;
        } else {
            popupHtml += `<strong>Temperature:</strong> ${weather.temperature !== null ? weather.temperature + ' °C' : 'N/A'}<br>`;
            popupHtml += `<strong>Wind:</strong> ${weather.wind !== null ? weather.wind + ' m/s' : 'N/A'}<br>`;
            popupHtml += `<strong>Precipitation (hour):</strong> ${weather.precipitation !== null ? weather.precipitation + ' mm' : 'N/A'}<br>`;
        }

        if (bortleInfo) {
            popupHtml += `<strong>Bortle level:</strong> ${bortleInfo.bortle}<br>`;
            if (Number.isFinite(bortleInfo.sqm)) {
                popupHtml += `<strong>Sky brightness:</strong> ${bortleInfo.sqm.toFixed(2)} mag/arcsec²<br>`;
            }
        } else {
            popupHtml += `<strong>Bortle level:</strong> Unavailable at this point<br>`;
        }

        popupHtml += `</div>`;
        if (infoMarker) {
            infoMarker.setLatLng([lat, lon]);
            infoMarker.bindPopup(popupHtml).openPopup();
        } else {
            placePin(lat, lon, popupHtml);
        }
    });

    // --- Coordinate search / pin placement ---
    const coordInput = document.getElementById('coord-search');
    const coordGo = document.getElementById('coord-go');
    function parseCoords(input) {
        if (!input) return null;
        const s = input.trim();
        // Accept comma separated or space separated
        const parts = s.split(/[,\s]+/).filter(Boolean);
        if (parts.length < 2) return null;
        const lat = parseFloat(parts[0]);
        const lon = parseFloat(parts[1]);
        if (Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
            return { lat, lon };
        }
        return null;
    }

    if (coordGo && coordInput) {
        coordGo.addEventListener('click', (ev) => {
            const v = coordInput.value;
            const parsed = parseCoords(v);
            if (!parsed) {
                setStatus(['Invalid coordinates. Use "lat, lon".']);
                return;
            }
            setStatus([]);
            map.setView([parsed.lat, parsed.lon], Math.max(map.getZoom(), 6));
            placePin(parsed.lat, parsed.lon, `<div><strong>Coords:</strong> ${parsed.lat.toFixed(5)}, ${parsed.lon.toFixed(5)}</div>`);
        });
        coordInput.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
                coordGo.click();
            }
        });
    }

    // Try to populate the coord input with the user's current location (if permission granted).
    // Initialize a location worker and perform a unified locate request.
    let locationWorker = null;
    function startLocationWorker() {
        if (locationWorker) return;
        try {
            locationWorker = new Worker('/static/js/locationWorker.js');
            locationWorker.addEventListener('message', async (ev) => {
                const msg = ev.data || {};
                if (msg.type === 'ready') return;

                // Worker requests that the main thread attempt browser geolocation.
                if (msg.type === 'needBrowserGeo') {
                    if (navigator && navigator.geolocation) {
                        navigator.geolocation.getCurrentPosition(
                            (pos) => {
                                try {
                                    locationWorker.postMessage({ cmd: 'browserGeoResult', success: true, latitude: pos.coords.latitude, longitude: pos.coords.longitude, accuracy: pos.coords.accuracy });
                                } catch (err) {
                                    console.warn('Failed to post browser geo result to worker', err);
                                }
                            },
                            (err) => {
                                try {
                                    locationWorker.postMessage({ cmd: 'browserGeoResult', success: false, error: err && err.message ? err.message : String(err) });
                                } catch (e) {
                                    console.warn('Failed to post browser geo error to worker', e);
                                }
                            },
                            { enableHighAccuracy: true, timeout: 10000 }
                        );
                    } else {
                        // No browser support — inform worker so it will fallback
                        try {
                            locationWorker.postMessage({ cmd: 'browserGeoResult', success: false, error: 'no_geolocation' });
                        } catch (e) {
                            console.warn('Failed to notify worker of missing geolocation', e);
                        }
                    }
                    return;
                }

                if (msg.type === 'location') {
                    const lat = msg.latitude;
                    const lon = msg.longitude;
                    if (coordInput) coordInput.value = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
                    const sourceText = msg.source === 'browser' ? 'Using your current location' : 'Using approximate location (IP)';
                    setStatus([sourceText]);
                    try {
                        map.setView([lat, lon], Math.max(map.getZoom(), 6));
                        const title = msg.source === 'browser' ? 'Your location' : 'Approximate location';
                        showCurrentLocation(lat, lon, `<div><strong>${title}</strong><br>${lat.toFixed(5)}, ${lon.toFixed(5)}</div>`);
                    } catch (err) {
                        console.warn('Failed to center map on location', err);
                    }
                    return;
                }

                if (msg.type === 'error') {
                    console.warn('Location worker error:', msg.message);
                    return;
                }
            });
        } catch (err) {
            console.warn('Failed to create location worker', err);
            locationWorker = null;
        }
    }

    function requestLocate() {
        startLocationWorker();
        if (locationWorker) {
            try {
                locationWorker.postMessage({ cmd: 'locate' });
            } catch (err) {
                console.warn('Failed to request locate from worker', err);
            }
        }
    }

    // Kick off unified locate flow
    requestLocate();

    if (weatherOpacityInput) {
        weatherOpacityInput.addEventListener("input", (event) => {
            weatherOpacity = parseFloat(event.target.value);
            weatherLayerNames.forEach((name) => {
                if (overlayLayers[name]) {
                    overlayLayers[name].setOpacity(weatherOpacity);
                }
            });
        });
    }

    if (bortleOpacityInput) {
        bortleOpacityInput.addEventListener("input", (event) => {
            bortleOpacity = parseFloat(event.target.value);
            if (overlayLayers.bortle) {
                overlayLayers.bortle.setOpacity(bortleOpacity);
            }
        });
    }

    // Keep the legend and zoom/attribution controls clear of the time rail.
    const mapWrap = document.querySelector(".map-wrap");
    const timeRail = document.querySelector(".time-rail");
    if (mapWrap && timeRail) {
        const syncRailHeight = () => {
            mapWrap.style.setProperty("--rail-h", `${timeRail.offsetHeight}px`);
        };
        syncRailHeight();
        if (typeof ResizeObserver !== "undefined") {
            new ResizeObserver(syncRailHeight).observe(timeRail);
        }
    }

    map.whenReady(() => {
        const wrap = document.querySelector(".map-wrap");
        if (wrap) {
            wrap.classList.add("is-ready");
        }
    });
})();
