# Fusion-hackathon
# Problem Statement :

Low Earth Orbit is increasingly congested; conjunction (near-collision) warnings between satellites and debris are growing faster than ground teams can manually evaluate and respond to each one. Expected Solution: Using public two-line element (TLE) orbital data, build a system that predicts upcoming close approaches between objects, ranks them by collision probability, and recommends an automated avoidance maneuver (timing + delta-v direction) for at least one sample conjunction event. 

Dataset / Reference: 
https://zenodo.org/records/4463683
https://celestrak.org/NORAD/elements/

## Current satellite positions (SGP4)

`src/fetch_positions.py` downloads the CelesTrak GP elements for a group (default `active`),
propagates every object to one instant with SGP4, and writes one CSV row per satellite with
its orbital parameters and position.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/fetch_positions.py                          # active satellites, now
.venv/bin/python src/fetch_positions.py --time 2026-10-09T12:00:00Z
.venv/bin/python src/fetch_positions.py --group stations
.venv/bin/python src/fetch_positions.py --input some_gp.csv      # offline, from a saved GP CSV
.venv/bin/python src/fetch_positions.py --leo                       # LEO only -> data/active_leo_positions_latest.csv
.venv/bin/python src/fetch_positions.py --input a.csv b.csv --name active --leo   # merge saved GP CSVs
```

Output (in `data/`):

- `raw/<group>_gp.csv` — raw CelesTrak download (cached; CelesTrak returns 403 if a group is re-downloaded within 2 hours)
- `<group>_positions_<UTC time>.csv` and `<group>_positions_latest.csv` — one row per satellite:
  - identity: `norad_id`, `name`, `intl_designator`, `epoch_utc`, `epoch_age_days`
  - elements: inclination, RAAN, eccentricity, argument of perigee, mean anomaly, mean motion, B*, ...
  - derived: `period_min`, `semi_major_axis_km`, `perigee_alt_km`, `apogee_alt_km`, `orbit_class` (LEO/MEO/GEO/HEO)
  - position at `position_time_utc`: TEME `x/y/z` (km) and `vx/vy/vz` (km/s), ECEF `x/y/z`, `latitude_deg`, `longitude_deg`, `altitude_km`
  - `sgp4_error`: empty when propagation succeeded

Note: `table.php?...&FORMAT=csv` returns an HTML page; the machine-readable endpoint is `gp.php?GROUP=active&FORMAT=csv`.

## 3D LEO simulation

`app/` animates every LEO satellite around a rotating Earth. Each browser frame uses
SGP4 positions (satellite.js 6 from full-precision OMM elements, computed in a Web Worker for all satellites ~50 times a second).

```bash
.venv/bin/python src/export_sim_data.py          # data/raw/active_gp.csv -> app/data/leo_objects.json
.venv/bin/python src/export_sim_data.py data/raw/active_gp.csv --debris data/raw/cosmos-2251-debris_gp.csv   # + debris (red)
python3 -m http.server 8765 -d app               # then open http://127.0.0.1:8765
```

- Time controls: pause, 1×, 10×, 60×, 300×, 1000×, jump to now
- Colour by constellation (Starlink, OneWeb, Kuiper, ...) or by altitude; toggle groups on/off
- Search by name or NORAD ID, or click a satellite: shows its orbit path, velocity arrow,
  live altitude, speed, lat/lon, inclination, period, perigee/apogee
- The Earth is rotated by GMST, so satellites sit over the correct ground location

App layout (`app/`):

| File | Responsibility |
| --- | --- |
| `index.html` | Page markup only (panel, info card), loads `js/main.js` |
| `css/style.css` | All styles |
| `js/main.js` | Entry point: loads data, wires modules, render loop |
| `js/config.js` | Constants: Earth radius, μ, scene scale, colours, speeds |
| `js/state.js` | Shared state: sim time, speed, colour mode, latest SGP4 results, visible groups |
| `js/data.js` | Loads `data/leo_objects.json` |
| `js/orbit.js` | Orbit maths: ECI→scene, period, perigee/apogee, element age |
| `js/scene.js` | Renderer, camera, controls, Earth, atmosphere, stars |
| `js/leoBoundary.js` | 2,000 km LEO boundary shell, grid and label |
| `js/satellites.js` | Point cloud of all satellites; position and colour updates |
| `js/colors.js` | Constellation and altitude colour scales |
| `js/propagator.js` | Talks to the SGP4 worker |
| `js/sim-worker.js` | Web Worker running SGP4 (satellite.js) for every satellite |
| `js/selection.js` | Selected satellite: marker, velocity arrow, orbit path, click picking, info card |
| `js/ui.js` | Control panel: clock, speeds, legend, stats, search |
| `js/orbitPath.js` | One-period orbit path line, shared by selection and conjunctions |
| `js/conjunctions.js` | Conjunction panel: critical list, countdowns, analysis mode (both orbits, TCA point, live distance) |

## Debris

`data/cosmos2251_debris_table.csv` is the CelesTrak COSMOS 2251 debris table (588 objects, from the saved
page via `src/table_html_to_csv.py`). Positions come from the GP elements:

```bash
.venv/bin/python src/fetch_positions.py --group cosmos-2251-debris --leo   # -> data/cosmos-2251-debris_leo_positions_latest.csv
```

## Conjunction screening

`src/screen_conjunctions.py` screens every object in `app/data/leo_objects.json` against every other one:

1. SGP4 for all objects every 20 s over the window (default 24 h)
2. KD-tree finds nearby pairs at each step; a straight-line check keeps pairs that come within 15 km
3. Each candidate is refined with exact SGP4 states to its time of closest approach (TCA) and miss distance
4. Probability of collision (Pc): 2D Foster/Chan method with an **assumed** covariance (TLEs have none):
   1-sigma RTN = [0.1, 0.5, 0.2] km + [0.1, 1.5, 0.1] km per day of element age (debris ×2); hard-body radius 20 m
5. Risk: red (critical) = Pc ≥ 1e-4 or miss < 0.2 km; yellow (watch) = Pc ≥ 1e-5 or miss < 1 km

Pairs slower than 0.1 km/s relative to each other (formation flying) are skipped.

```bash
.venv/bin/python src/screen_conjunctions.py                # 7 days from now, ~4 min on 7 cores
.venv/bin/python src/screen_conjunctions.py --hours 24 --screen-km 2
```

Outputs: `data/conjunctions_latest.csv` (every event < 5 km) and `app/data/conjunctions.json`
(red + yellow events, shown in the simulation's Conjunction screening panel).

Validated: a 1 s brute-force search found the same 651 events as the screener over 20 minutes (none missed),
and satellite.js reproduces the miss distances and TCAs to the metre and millisecond.

### Debris check

Debris groups loaded: COSMOS 2251 (588), Fengyun-1C (1,970 in LEO), Iridium 33 (109), Cosmos 1408 (3), downloaded with
`fetch_positions.py --group <name>-debris --leo` and passed to `export_sim_data.py --debris ...`.
`app/data/conjunctions.json` keeps **every** debris approach under 5 km (not only red/yellow).

In the simulation: select any satellite to see its debris check (green = no debris within 5 km in the
screening window; otherwise count, next approach, closest miss and highest Pc, with a button to analyse it).
The conjunction list then shows only that satellite's events; the filter "Satellite vs debris" lists all of them.

### Debris-vs-debris red zones

A critical conjunction between two debris objects cannot be avoided: neither can manoeuvre. If they
collide, the fragments spread along both orbits and all return through the collision point once per orbit.
The screener therefore turns every critical (red) debris-debris event into a **red zone**: a 100 km sphere
around the predicted collision point. For each zone it lists the active satellites that pass through that
sphere between TCA and one orbital period later (`debris_zones` in `src/screen_conjunctions.py`).

In the simulation the zone is drawn as a red sphere from one hour before TCA until the end of that orbit
(pulsing after TCA). When satellites are exposed, a banner names them; the analysis card lists each one's
closest distance and time, and selecting an exposed satellite shows the warning in its card. The filter
"Debris vs debris red zones" lists all of them.

In the current 7-day run: 26 red zones, 20 with at least one active satellite passing through (up to 194).
Windows screened on demand by `src/server.py` carry their red zones too.
The satellite lists were checked against a 1-second brute-force search of every active object for two zones:
same satellites, distances within 0.34 km. The zone is a simplified indicator, not a fragment-cloud model:
the 100 km radius and the one-orbit window are chosen values, and a predicted close approach is not a collision.

```bash
.venv/bin/python src/screen_conjunctions.py --zones-only   # redo only the zones of the existing app file (~20 s)
```

### 7-day look-ahead

The default window is 7 days (`--hours 168`), split into day-long segments screened in parallel (`--workers`).
In the simulation: a per-day chart of events (click a day to jump there), a look-ahead filter
(next 1 h / 6 h / 24 h / 3 days / whole window), and time controls +1 h, +1 day, +1 week, a UTC date picker, and a 6 h/s speed.

Same-constellation pairs of actively managed constellations (Starlink, OneWeb, Kuiper, Qianfan, Guowang, Iridium) are
only screened while both element sets are at most 1.5 days old: they manoeuvre to keep their spacing, which TLEs cannot
predict, and a 7-day run showed their sub-1 km events growing ~10x by day 6 while other pairings stayed flat.
`data/conjunctions_latest.csv` is large for 7 days and is git-ignored.

## Accuracy and verification

All SGP4 paths use the same full-precision OMM elements from CelesTrak (not TLE text, which rounds eccentricity
and B* and moved some fast-decaying debris by up to ~280 m). `app/data/leo_objects.json` stores epochs on whole
milliseconds (satellite.js parses them with a JavaScript Date) with the mean anomaly compensated.

```bash
.venv/bin/python tests/verify_conjunctions.py      # ~1 min, 8 checks
.venv/bin/python tests/verify_browser_sgp4.py      # browser SGP4 vs Python, needs Node.js
```

Last results:

| Check | Result |
| --- | --- |
| Positions CSVs vs screener inputs, all 17,528 objects | max 0.06 m, 0.9 mm/s |
| Raw OMM vs exported records over 7 days | median 2 mm, max 0.18 m (objects about to re-enter) |
| Browser (satellite.js 6.0.2) vs Python, 140,204 states over 7 days | max 0.31 m, 0.36 mm/s |
| TCA and miss distance vs dense 1 ms sampling (394 events, all days, segment boundaries, debris) | max 0.15 m, 1 ms |
| Miss vector perpendicular to relative velocity, RTN components | within TCA rounding, 0.1 m |
| Pc vs exact disk integral (adaptive quadrature) / vs Monte Carlo (2e7 samples) | 0.05 % (storage rounding) / within 2.4 sigma |
| Completeness: brute force at 1 s steps, 3 windows of 20 min (start, 24 h segment boundary, day 5) | 752 / 934 / 1,776 events, 0 missing, 0 extra, 0 duplicates |

These are numerical checks of the computation. The physical accuracy is limited by the data: TLE/OMM
positions are typically good to about 1 km at epoch and degrade by 1-3 km per day, and Pc depends on the
assumed covariance.

## Running

```bash
./run.sh               # download -> positions -> export -> 7-day screening (~4 min) -> server
./run.sh --serve-only  # just start the server with the existing data
```

`run.sh` starts `src/server.py`, which serves the simulation and screens conjunctions **on demand for any date**:
when the clock leaves the screened window (date picker, +1 day/week, playback), the panel requests
`/api/screen?start=...&hours=24` (or 3/7 days) and shows the result; results are cached in `data/cache/`.
If port 8765 is busy, the next free port is used. The Python scripts also work with plain `python3`
(they re-run themselves with `.venv/bin/python`, see `src/_venv.py`).

### Validity of the orbital data

SGP4 elements are only meaningful near their epoch. Each object is shown and screened only within
±30 days of its epoch and never before its estimated launch (`epoch - REV_AT_EPOCH / MEAN_MOTION`, checked
against the launch year); the clock is limited to the loaded data's range (shown in the date picker) and
playback pauses at its edges. Past dates (e.g. the 2009 Iridium 33 / Cosmos 2251 collision) need historical
element sets.

The render loop is isolated per module: an error in one part is reported once and never stops the animation.

## Historical replay: could the system have predicted past collisions?

`src/history.py` rebuilds the catalogue as it was before a known collision, using only element sets published
before a cutoff (default 24 h before the event), from Space-Track's `gp_history` (free account; credentials from
the environment, never stored), then screens and reports like the live data.

```bash
python3 src/history.py list
SPACETRACK_USER='...' SPACETRACK_PASS='...' python3 src/history.py run iridium-cosmos-2009   # first run downloads
python3 src/history.py run iridium-cosmos-2009 --lead-hours 6                                 # later runs use the cache
```

Open `http://127.0.0.1:8765/?dataset=history/iridium-cosmos-2009` (or pick it in the "Data" selector): the banner
shows the prediction for 72 h / 24 h / 6 h of warning and "Go to collision" opens the analysis at TCA.

| Event | Data from before | Predicted TCA (UTC) | Miss | Pc | Risk | Rank by Pc |
| --- | --- | --- | --- | --- | --- | --- |
| Iridium 33 × Cosmos 2251, 10 Feb 2009 (reported 16:55:59.67-.99) | 24 h | 16:55:59.806 | 584 m | 2.7e-4 | critical | 32 of 5,171 |
| Cerise × Ariane 1 debris, 24 Jul 1996 (date only) | 24 h | 09:48:02.451 | 1,054 m | 5.5e-5 | watch | 192 of 3,120 |
| Yunhai 1-02 × Zenit-2 debris, 18 Mar 2021 (reported 07:41) | 24 h | 07:41:19.365 | 216 m | 7.8e-5 | watch | 746 of 17,488 |

For Iridium-Cosmos the result matches CelesTrak SOCRATES' own prediction from that day (584 m, 16:55:59.806).
With 72 h of warning the Yunhai fragment had no element set yet, so that collision was not predictable then.
Space-Track data is for your own use: `data/history/` and `app/data/history/` are git-ignored.

## Telemetry (Logfire)

The pipeline scripts report to [Logfire](https://logfire.pydantic.dev) so a run can be watched live:
each script is a trace, the screener adds one span per day-long segment (coarse-screen time, candidate
and event counts, refinement time), and CPU and memory are recorded while it runs. See `src/telemetry.py`.

```bash
.venv/bin/logfire auth                                  # once per machine (.venv\Scripts\logfire on Windows)
.venv/bin/logfire projects use --org <org> <project>    # once per clone; writes .logfire/ (git-ignored)
```

Without the `logfire` package or without credentials the scripts run exactly as before and send nothing.
