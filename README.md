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
SGP4 positions (satellite.js, computed in a Web Worker for all satellites ~50 times a second).

```bash
.venv/bin/python src/export_sim_data.py          # data/raw/active_gp.csv -> app/data/leo_tle.json
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
| `js/data.js` | Loads `data/leo_tle.json` |
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

`src/screen_conjunctions.py` screens every object in `app/data/leo_tle.json` against every other one:

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

### 7-day look-ahead

The default window is 7 days (`--hours 168`), split into day-long segments screened in parallel (`--workers`).
In the simulation: a per-day chart of events (click a day to jump there), a look-ahead filter
(next 1 h / 6 h / 24 h / 3 days / whole window), and time controls +1 h, +1 day, +1 week, a UTC date picker, and a 6 h/s speed.

Same-constellation pairs of actively managed constellations (Starlink, OneWeb, Kuiper, Qianfan, Guowang, Iridium) are
trusted for the first 24 h only: they manoeuvre to keep their spacing, which TLEs cannot predict, and a 7-day run showed
their sub-1 km events growing ~10x by day 6 while other pairings stayed flat. They remain in the CSV
(`intra_constellation` column) but are left out of the counts and the app file. `data/conjunctions_latest.csv` is ~130 MB
for 7 days and is git-ignored.
