# Star map work — handoff note

Paste this into a new session to resume. Everything below is already written to
disk in `/home/alex/Server` and is uncommitted.

## Files touched

| File | State |
|---|---|
| `static/js/star_map.js` | modified — mobile/touch, perf rewrite, constellations, star names |
| `templates/star_map.html` | modified — viewport, mobile CSS, new toggles, click-region fixes |
| `controllers/star_map.py` | modified — catalogue-backed endpoints, `/api/stars_bin`, `/api/stars_bands`, `/api/star_names` |
| `controllers/interface.py` | modified — `search_object` consults the IAU name overlay |
| `app/star_catalog.py` | **new** — in-memory magnitude-sorted catalogue + binary encoder + name overlay |
| `Server.py` | modified — background warm-up of the catalogue at startup |
| `scripts/build_constellations.py` | **new** — generates constellation line data |
| `scripts/build_star_names.py` | **new** — generates star name / magnitude overlay |
| `static/data/constellations.json` | **new** — 24 figures, 8.8 KB |
| `static/data/star_names.json` | **new** — 505 entries, 22.2 KB |
| `license.md` | modified — §13 third-party attribution (d3-celestial, IAU-CSN, Yale BSC5) |

Cache-buster in the template is at `?v=2026-09-20-5`. Bump it on any further
`star_map.js` change.

## What is done and verified

**1. Mobile / touch support.** One finger rotates, two fingers pinch-zoom (with
the pinch midpoint also rotating), quick tap picks an object with an 18 px hit
radius. `touch-action: none` on the canvas, `gesture*` suppressed for iOS, added
the missing viewport meta. Below 820 px wide (or 480 px tall) the controls
collapse into a ☰ bottom sheet with safe-area insets. Canvas now uses a
devicePixelRatio backing store (capped 2.5×). Long-press the magnitude slider
for the custom-value menu. Driven by `html.sm-touch` / `html.sm-compact`
classes set from JS.

**2. Performance.** Server was doing a 5.1 s full-ORM scan per star request and
the client re-downloaded everything three times as ~26 MB of JSON.

- `app/star_catalog.py` reads all three tables once into flat arrays sorted by
  magnitude (1.8 s, warmed in a background thread at boot).
- `/api/stars_bands` returns disjoint index ranges; `/api/stars_bin` serves each
  as binary (20-byte header, float32 ra/dec/mag, newline-joined names), gzipped,
  cached in memory, ETag + `max-age=604800`.
- Whole catalogue is now **2.92 MB on the wire** (was ~26 MB × 3), 455 ms cold
  / 39 ms cached. Client ingest of 286,112 objects: 674 ms, chunked.
- Client stores stars in `Float32Array`s; "brighter than X" is a binary search,
  not an array rebuild. Draw loop projects inline, tests `z` first, culls
  off-screen, uses rects for sub-pixel stars, and because the array is
  magnitude-sorted the fill colour is set **8 times a frame instead of 236,000**.
- At mag 20.4, zoom 1, 1280×800: 236,333 in range → **46,995 actually drawn**,
  8 style changes, 40 arcs. Zoom 4 → 2,799 drawn.
- Draws coalesced to one per rAF; while dragging only the brightest
  `interactionBudget` stars draw (self-tuning), full detail 180 ms after the
  view settles. Sidereal time cached. Search-highlight loop no longer redraws
  forever. Telescope poll was 1 s despite its comment — now 5 s.

**3. Unknown magnitudes.** `V-Mag IS NULL` used to become 30, and HDSTARTable
uses 20/30/40/50 as "not recorded" placeholders — so 49,778 objects were
unreachable and any that surfaced claimed to be magnitude 30. They are now
*unknown*: NaN on the wire, `null` in JSON with `magUnknown: true`, sorted to
the catalogue tail, excluded from the magnitude scale, shown via an
"Unknown magnitude" toggle that draws them in blue. IndexTable/NGCtable
magnitudes are genuine to 20.41, so the placeholder rule is HD-only.

**4. Click regions.** One `<label>` had wrapped four checkboxes (clicking
"Equatorial Grid" toggled Horizon Grid) and labels were full-width flex
children. Each control now has its own label sized to its content.
Also `user-select: none` on the filter panel so double-clicking doesn't
highlight captions; inputs stay selectable.

**5. Constellations.** `scripts/build_constellations.py` pulls figures from
d3-celestial (BSD-3-Clause), keeps rank 1 (22 figures) plus Ursa Minor and Crux
= **24**, and snaps every vertex onto a star in the local catalogue —
**383/383 snapped**. Rendered under the stars, great-circle interpolated at ~2°
per step, clipped at the horizon, one `beginPath`/`stroke` per frame (~1 ms,
166 subpaths). Toggles: "Constellations" and "Constellation names". Verified by
rendering the figures to an image and inspecting them.

**6. Star names — the current task, nearly done.** Two real bugs found:

- The database names only **40** stars, so Mizar/Alphard/Alpheratz etc. could
  not be searched or identified.
- **103 naked-eye stars were completely invisible** because the HD catalogue
  records no magnitude for them. Every one is a variable: Algol (2.09),
  T CrB (2.0), δ Cephei (3.75), β Lyrae, λ Tauri, η Aquilae, μ Cephei,
  χ Cygni, ζ Geminorum, R Hydrae, Y CVn…

`scripts/build_star_names.py` merges the **IAU Catalog of Star Names** (451
entries, CC BY) with the **Yale Bright Star Catalogue V/50** and writes
`static/data/star_names.json`: 505 entries = 407 proper names + 103 fallback
magnitudes. The overlay only ever *fills* a missing magnitude, never overrides
one.

Verified working:
- `app/star_catalog.py` recovers 103 magnitudes at build time (known count
  236,334 → 236,437). Algol is now index 50.
- `/star_info/<name>` returns `friendlyName`, `bayer`, `variableId`,
  `magSource`.
- `/api/star_names` serves 407 labelled stars (24.8 KB, `max-age=86400`).
- Client "Star names" toggle: 407/407 resolved to catalogue rows, 72 labels
  drawn at mag ≤ 5 for London at 22:00 (Capella, Arcturus, Procyon, Betelgeuse,
  Spica, Pollux, Regulus, Alioth, Elnath, Alkaid, Dubhe, Castor, Alphard…),
  with an occupancy grid preventing overlap.

## Search by proper name — done and verified

`controllers/interface.py` (`search_object`, the `else` branch) calls
`star_catalog.find_by_proper_name(norm)` before the old `query_by_common_name`
path, and applies the overlay's magnitude and name to the result. The route is
`/interface/search_object` (the blueprint carries `url_prefix="/interface"`),
which is what `star_map.js:3315` and `interface.js:633` already call.

Verified results:

| query | resolves to | V-Mag | name |
|---|---|---|---|
| Mizar | HD116656 | 2.4 | Mizar (ζ UMa) |
| Algol | HD19356 | **2.09** (was 30.0) | Algol (β Per) |
| Sheliak | HD174638 | **3.6** (was 30.0) | Sheliak (β Lyr) |
| Alpheratz | HD358 | 2.15 | Alpheratz (α And) |
| Rasalhague | HD159561 | 2.14 | Rasalhague (α Oph) |
| Zubeneschamali | HD135742 | 2.74 | Zubeneschamali (β Lib) |
| alpha cen | HD128620 | 0.33 | Rigil Kentaurus (prefix match) |
| HD48915 | HD48915 | −1.58 | Sirius |
| M42 / NGC1976 | NGC1976 | 4.0 | Orion Nebula (unchanged path) |
| jupiter | Jupiter | −1.84 | (ephemeris path, unchanged) |
| nonsensestar | — | — | NOT FOUND |

All work on the star map is complete. This note is only a record now — delete
it whenever you like, nothing references it.

## How to re-verify everything

No browser is available in this environment (the only Chromium is snap-confined
and will not launch). Testing was done two ways:

- **Backend** — Flask test client against the blueprints. Scratch scripts lived
  in `/tmp/claude-1000/-home-alex-Server/<session>/scratchpad/` and are probably
  gone; they were short and easy to rewrite.
- **Client** — a DOM/canvas stub (`dom_stub.js`) plus `new Function(src)` to
  load `star_map.js` in Deno (`/home/alex/.local/bin/deno`), driving real
  exported catalogue data. `deno check --no-lock static/js/star_map.js` is the
  quick syntax gate.

Regenerating the data files:

```bash
venv/bin/python scripts/build_constellations.py
venv/bin/python scripts/build_star_names.py
```

Both fetch from the network; both take a `--cache`/`--cache-dir` for local
copies.

## Things noticed but deliberately not changed

- η Carinae has no IAU proper name; it gets V=6.21 from BSC5, which is fair for
  a star that has ranged from −1 to 7.9 over two centuries.
- 44 IAU-named stars (exoplanet hosts like "Absolutno") have no HD row at all,
  so they are not in the overlay.
- The security middleware rate-limits to 120 requests/60 s per IP. Catalogue
  loading is 8 band requests, so there is headroom, but telescope polling
  (now 5 s) shares that budget.
- Nothing has been committed. Nothing was written to `instance/Data.db`.
