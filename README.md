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
