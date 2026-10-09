"""All-vs-all conjunction screening for the objects in app/data/leo_objects.json.

Pipeline:
  1. Propagate every object with SGP4 on a fixed time grid (default 20 s steps, 24 h).
  2. At each step, a KD-tree finds pairs closer than the distance two objects can cover
     in half a step; a straight-line relative-motion check keeps pairs whose closest
     approach within that step is under COARSE_KM.
  3. Each candidate is refined with exact SGP4 states to its time of closest approach (TCA)
     and miss distance; events under --screen-km are kept.
  4. Probability of collision (Pc) uses the 2D short-encounter method (Foster/Chan, integrated
     numerically over the hard-body disk) with an
     ASSUMED covariance, because TLEs carry none: 1-sigma RTN errors grow with element age.

Pairs with relative speed below MIN_REL_SPEED_KM_S (formation flying, e.g. freshly launched
trains) are skipped: the short-encounter Pc model does not apply to them.

Pairs inside one actively managed constellation (e.g. Starlink-Starlink) are only screened while both
element sets are at most INTRA_MAX_AGE_DAYS old: those satellites manoeuvre constantly to keep their
spacing, which TLEs cannot predict, so older predictions of their relative phasing are spurious (in a
7-day run their sub-1 km events grew from ~900 on day 1 to ~9,500 on day 6, while all other pairings
stayed flat). The rule follows the data's age, so it holds for any window start.

Outputs (for --dataset history/<event>: app/data/history/<event>/ and data/history/<event>/):
  data/conjunctions_latest.csv (+ timestamped copy): every event under --screen-km
  app/data/conjunctions.json: red and yellow events, plus every event involving debris

The window is split into segments screened in parallel worker processes. Each object is only used
inside its validity window (from leo_objects.json). The same code serves on-demand requests from
src/server.py (any start date inside the clock range).

Usage:
    python src/screen_conjunctions.py                     # 7 days from now
    python src/screen_conjunctions.py --hours 24 --screen-km 2
"""

import argparse
import csv
import json
import math
import multiprocessing as mp
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _venv  # noqa: F401  (re-runs with .venv/bin/python if packages are missing)
import numpy as np
from scipy.spatial import cKDTree
from fetch_positions import ecef_to_geodetic
from sgp4 import omm
from sgp4.api import Satrec, SatrecArray, jday

ROOT = Path(__file__).resolve().parent.parent
APP_DATA = ROOT / "app" / "data"
OBJECTS_PATH = APP_DATA / "leo_objects.json"
APP_OUT = APP_DATA / "conjunctions.json"
DATA_DIR = ROOT / "data"


def dataset_dirs(dataset=""):
    """(app data dir, csv output dir) for a dataset: "" = live data, "history/<event>" = a past event."""
    if dataset and (".." in Path(dataset).parts or Path(dataset).is_absolute()):
        raise ValueError(f"invalid dataset {dataset!r}")
    return APP_DATA / dataset, DATA_DIR / dataset

MAX_REL_SPEED_KM_S = 16.0     # upper bound for two LEO objects (head-on)
COARSE_KM = 15.0              # straight-line pre-filter; covers curvature error over one step
MIN_REL_SPEED_KM_S = 0.1
CHUNK_STEPS = 180

# Assumed 1-sigma position error (km) in RTN: sigma = base + growth * element age (days).
SIGMA_BASE_KM = np.array([0.1, 0.5, 0.2])
SIGMA_GROWTH_KM_PER_DAY = np.array([0.1, 1.5, 0.1])
DEBRIS_SIGMA_FACTOR = 2.0     # debris elements are less precise

APP_FIELDS = ["tca_ms", "norad_1", "norad_2", "miss_km", "miss_radial_km", "miss_along_km", "miss_cross_km",
              "rel_speed_km_s", "altitude_km", "pc", "risk", "debris"]

MANAGED_CONSTELLATIONS = {"Starlink", "OneWeb", "Kuiper", "Qianfan", "Guowang", "Iridium"}
INTRA_MAX_AGE_DAYS = 1.5


def untrusted_intra_pair(objs, sats, a, b, t_sec, jd0, fr0):
    """Same managed constellation and either element set older than INTRA_MAX_AGE_DAYS at t_sec."""
    ga, gb = objs[a]["group"], objs[b]["group"]
    if ga != gb or ga not in MANAGED_CONSTELLATIONS:
        return False
    oldest = max(t_sec - epoch_seconds(sats[a], jd0 + fr0), t_sec - epoch_seconds(sats[b], jd0 + fr0)) / 86400
    return oldest > INTRA_MAX_AGE_DAYS

PC_RED = 1e-4
PC_YELLOW = 1e-5


def load_objects(path=OBJECTS_PATH):
    """The simulation's objects, initialised from the same OMM records the browser uses."""
    data = json.loads(Path(path).read_text())
    objs, sats = [], []
    for group, values, *_ in data["objects"]:
        fields = dict(zip(data["omm_fields"], values))
        sat = Satrec()
        omm.initialize(sat, {k: str(v) for k, v in fields.items()})
        sats.append(sat)
        objs.append({"norad_id": fields["NORAD_CAT_ID"], "name": fields["OBJECT_NAME"], "group": group})
    return objs, sats


def load_validity(path=OBJECTS_PATH):
    """(valid_from_ms, valid_to_ms) arrays aligned with load_objects(), and the clock range."""
    data = json.loads(Path(path).read_text())
    rows = data["objects"]
    return (np.array([r[2] for r in rows], dtype=np.float64), np.array([r[3] for r in rows], dtype=np.float64),
            tuple(data["clock_range_ms"]))


def epoch_seconds(sat, t0_jd):
    """Seconds from the window start to the satellite's element epoch."""
    return ((sat.jdsatepoch + sat.jdsatepochF) - t0_jd) * 86400.0


def sgp4_at(sat, jd0, fr0, sec):
    err, r, v = sat.sgp4(jd0, fr0 + sec / 86400.0)
    return err, np.array(r), np.array(v)


def coarse_screen(sats, jd0, fr0, k_start, k_end, dt, valid=None, t0_ms=0.0):
    """Candidate encounters between grid steps k_start..k_end-1 as arrays (i, j, t_seconds, approx_miss_km).
    valid = (valid_from_ms, valid_to_ms): objects outside their validity window are ignored."""
    n = len(sats)
    sat_array = SatrecArray(sats)
    radius = MAX_REL_SPEED_KM_S * dt / 2 + COARSE_KM
    far = np.column_stack([np.full(n, 1e7) + np.arange(n) * 1e4, np.zeros(n), np.zeros(n)])
    found = {"i": [], "j": [], "t": [], "d": []}

    for start in range(k_start, k_end, CHUNK_STEPS):
        m = min(CHUNK_STEPS, k_end - start)
        secs = (start + np.arange(m)) * dt
        err, r, v = sat_array.sgp4(np.full(m, jd0), fr0 + secs / 86400.0)
        bad = err != 0
        if valid is not None:
            t_ms = t0_ms + secs * 1000.0
            bad |= (t_ms[None, :] < valid[0][:, None]) | (t_ms[None, :] > valid[1][:, None])
        for k in range(m):
            pos = np.where(bad[:, k][:, None], far, r[:, k, :])
            pairs = cKDTree(pos).query_pairs(radius, output_type="ndarray")
            if not len(pairs):
                continue
            i, j = pairs[:, 0], pairs[:, 1]
            dr = pos[j] - pos[i]
            dv = v[j, k, :] - v[i, k, :]
            dv2 = np.einsum("ij,ij->i", dv, dv)
            moving = dv2 > MIN_REL_SPEED_KM_S ** 2
            i, j, dr, dv, dv2 = i[moving], j[moving], dr[moving], dv[moving], dv2[moving]
            tau = np.clip(-np.einsum("ij,ij->i", dr, dv) / dv2, -dt / 2, dt / 2)
            dmin = np.linalg.norm(dr + dv * tau[:, None], axis=1)
            keep = dmin < COARSE_KM
            found["i"].append(i[keep]); found["j"].append(j[keep])
            found["t"].append(secs[k] + tau[keep]); found["d"].append(dmin[keep])
    return [np.concatenate(found[key]) if found[key] else np.array([]) for key in ("i", "j", "t", "d")]


def dedupe(i, j, t, d, dt):
    """Merge candidates of the same pair found at adjacent steps (keep the closest)."""
    order = np.lexsort((t, j, i))
    i, j, t, d = i[order], j[order], t[order], d[order]
    out = []
    for idx in range(len(i)):
        if out and out[-1][0] == i[idx] and out[-1][1] == j[idx] and t[idx] - out[-1][2] < 2 * dt:
            if d[idx] < out[-1][3]:
                out[-1] = (i[idx], j[idx], t[idx], d[idx])
        else:
            out.append((i[idx], j[idx], t[idx], d[idx]))
    return out


def refine(sat_i, sat_j, jd0, fr0, t):
    """Iterate to the exact TCA with SGP4 states; returns (t, r_i, v_i, r_j, v_j) or None."""
    for _ in range(4):
        ei, ri, vi = sgp4_at(sat_i, jd0, fr0, t)
        ej, rj, vj = sgp4_at(sat_j, jd0, fr0, t)
        if ei or ej:
            return None
        dr, dv = rj - ri, vj - vi
        tau = -dr @ dv / (dv @ dv)
        t += tau
        if abs(tau) < 1e-3:
            break
    ei, ri, vi = sgp4_at(sat_i, jd0, fr0, t)
    ej, rj, vj = sgp4_at(sat_j, jd0, fr0, t)
    if ei or ej:
        return None
    return t, ri, vi, rj, vj


def rtn_basis(r, v):
    """Columns: radial, transverse (along-track), normal unit vectors."""
    rh = r / np.linalg.norm(r)
    nh = np.cross(r, v); nh /= np.linalg.norm(nh)
    th = np.cross(nh, rh)
    return np.column_stack([rh, th, nh])


def covariance(r, v, age_days, debris):
    sigma = SIGMA_BASE_KM + SIGMA_GROWTH_KM_PER_DAY * max(age_days, 0.0)
    if debris:
        sigma = sigma * DEBRIS_SIGMA_FACTOR
    m = rtn_basis(r, v)
    return m @ np.diag(sigma ** 2) @ m.T


# Quadrature over the hard-body disk: Gauss-Legendre in radius, uniform (spectrally accurate) in angle.
_GL_X, _GL_W = np.polynomial.legendre.leggauss(16)
_RHO, _RHO_W = (_GL_X + 1) / 2, _GL_W / 2
_THETA = np.linspace(0, 2 * math.pi, 64, endpoint=False)
_COS, _SIN = np.cos(_THETA), np.sin(_THETA)


def disk_probability(mu, c2, radius):
    """Exact probability that a 2D Gaussian (mean mu, covariance c2) falls inside a disk at the origin."""
    inv = np.linalg.inv(c2)
    rho = radius * _RHO[:, None]
    x = rho * _COS - mu[0]
    y = rho * _SIN - mu[1]
    q = inv[0, 0] * x * x + 2 * inv[0, 1] * x * y + inv[1, 1] * y * y
    density = np.exp(-0.5 * q) / (2 * math.pi * math.sqrt(np.linalg.det(c2)))
    return float(radius * np.sum(_RHO_W[:, None] * density * rho) * (2 * math.pi / _THETA.size))


def collision_probability(ri, vi, rj, vj, cov_i, cov_j, hbr_km):
    """2D short-encounter Pc (Foster/Chan): the combined covariance and the miss vector are projected
    onto the encounter plane (perpendicular to the relative velocity) and the Gaussian is integrated
    over the hard-body disk."""
    miss = rj - ri
    vrel = vj - vi
    vhat = vrel / np.linalg.norm(vrel)
    miss_plane = miss - (miss @ vhat) * vhat          # exactly in the encounter plane
    d = np.linalg.norm(miss_plane)
    x = miss_plane / d if d > 1e-9 else np.cross(vhat, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(vhat, x)
    b = np.column_stack([x, y])
    c2 = b.T @ (cov_i + cov_j) @ b
    return min(1.0, disk_probability(np.array([d, 0.0]), c2, hbr_km))


def geodetic_altitude_km(r):
    """WGS84 height above the ellipsoid, as in the positions CSVs. A rotation about the z axis does not
    change it, so the TEME position can be used directly."""
    return float(ecef_to_geodetic(np.asarray(r)[None, :])[2][0])


def risk_level(pc, miss_km):
    if pc >= PC_RED or miss_km < 0.2:
        return "red"
    if pc >= PC_YELLOW or miss_km < 1.0:
        return "yellow"
    return "green"


_WORKER = {}


def _init_worker(path):
    """Pool initializer: each worker process loads the objects once."""
    objs, sats = load_objects(path)
    valid_from, valid_to, _ = load_validity(path)
    _WORKER.update(objs=objs, sats=sats, valid=(valid_from, valid_to))


def make_pool(workers, objects_path=OBJECTS_PATH):
    # "spawn" is safe from threaded parents (src/server.py); workers load the objects in _init_worker.
    return mp.get_context("spawn").Pool(workers, initializer=_init_worker, initargs=(str(objects_path),))


def screen_segment(task):
    """Screen grid steps [k_start, k_end) and return refined events (runs in a worker process)."""
    t0_iso, k_start, k_end, p = task
    objs, sats, valid = _WORKER["objs"], _WORKER["sats"], _WORKER["valid"]
    t0 = datetime.fromisoformat(t0_iso)
    t0_ms = t0.timestamp() * 1000
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    i, j, t, d = coarse_screen(sats, jd0, fr0, k_start, k_end, p["step"], valid, t0_ms)
    # The straight-line estimate is within ~1 km of the exact miss, so far candidates can be skipped.
    near = d < p["screen_km"] + 3
    candidates = dedupe(i[near], j[near], t[near], d[near], p["step"])

    events = []
    skipped_intra = 0
    for a, b, tc, _ in candidates:
        if untrusted_intra_pair(objs, sats, a, b, float(tc), jd0, fr0):
            skipped_intra += 1
            continue
        res = refine(sats[a], sats[b], jd0, fr0, float(tc))
        if res is None:
            continue
        tca, ri, vi, rj, vj = res
        miss = float(np.linalg.norm(rj - ri))
        tca_ms = t0_ms + tca * 1000
        if miss >= p["screen_km"] or not 0 <= tca <= p["hours"] * 3600 \
                or not (valid[0][a] <= tca_ms <= valid[1][a] and valid[0][b] <= tca_ms <= valid[1][b]):
            continue
        oa, ob = objs[a], objs[b]
        age_a = (tca - epoch_seconds(sats[a], jd0 + fr0)) / 86400
        age_b = (tca - epoch_seconds(sats[b], jd0 + fr0)) / 86400
        pc = collision_probability(
            ri, vi, rj, vj,
            covariance(ri, vi, age_a, oa["group"] == "Debris"),
            covariance(rj, vj, age_b, ob["group"] == "Debris"),
            p["hbr_m"] / 1000,
        )
        miss_rtn = rtn_basis(ri, vi).T @ (rj - ri)
        tca_dt = t0 + timedelta(seconds=tca)
        events.append({
            "tca_utc": tca_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "tca_ms": int(round(tca_dt.timestamp() * 1000)),
            "norad_1": oa["norad_id"], "name_1": oa["name"], "group_1": oa["group"],
            "norad_2": ob["norad_id"], "name_2": ob["name"], "group_2": ob["group"],
            "miss_km": round(miss, 4),
            "miss_radial_km": round(float(miss_rtn[0]), 4),
            "miss_along_km": round(float(miss_rtn[1]), 4),
            "miss_cross_km": round(float(miss_rtn[2]), 4),
            "rel_speed_km_s": round(float(np.linalg.norm(vj - vi)), 4),
            "altitude_km": round(geodetic_altitude_km(ri), 1),
            "pc": float(f"{pc:.4g}"),
            "risk": risk_level(pc, miss),
            "debris": "Debris" in (oa["group"], ob["group"]),
            "intra_constellation": oa["group"] == ob["group"] and oa["group"] in MANAGED_CONSTELLATIONS,
        })
    return events, skipped_intra


DEFAULT_PARAMS = {"step": 20.0, "screen_km": 5.0, "hbr_m": 20.0}


def run_screening(pool, workers, t0, hours, params=None, progress=None):
    """Screen [t0, t0 + hours] on the pool. Returns (events, trusted, counts); see module docstring."""
    p = {**DEFAULT_PARAMS, **(params or {}), "hours": hours}
    steps = int(hours * 3600 / p["step"]) + 1
    # Enough segments to keep every worker busy, none shorter than one hour of steps.
    n_seg = max(workers, math.ceil(hours / 24))
    seg_steps = max(int(3600 / p["step"]), math.ceil(steps / n_seg))
    t0_iso = t0.isoformat()
    tasks = [(t0_iso, k, min(k + seg_steps, steps), p) for k in range(0, steps, seg_steps)]
    results = []
    skipped_intra = 0
    for n_done, (seg_events, seg_skipped) in enumerate(pool.imap_unordered(screen_segment, tasks), 1):
        results.extend(seg_events)
        skipped_intra += seg_skipped
        if progress:
            progress(n_done, len(tasks))

    # An encounter on a segment boundary can be found by both neighbours: keep one.
    results.sort(key=lambda e: (e["norad_1"], e["norad_2"], e["tca_ms"]))
    events = []
    for e in results:
        prev = events[-1] if events else None
        if prev and (prev["norad_1"], prev["norad_2"]) == (e["norad_1"], e["norad_2"]) \
                and e["tca_ms"] - prev["tca_ms"] < 60000:
            continue
        events.append(e)
    events.sort(key=lambda e: e["tca_ms"])

    counts = {level: sum(1 for e in events if e["risk"] == level) for level in ("red", "yellow", "green")}
    counts["debris"] = sum(1 for e in events if e["debris"])
    counts["intra_skipped_candidates"] = skipped_intra
    return events, events, counts


def app_payload(t0, hours, trusted, counts, n_objects, params=None):
    """The JSON the simulation loads (app/data/conjunctions.json or the server's /api/screen)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_start_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": hours,
        "step_s": p["step"],
        "screen_km": p["screen_km"],
        "hbr_m": p["hbr_m"],
        "n_objects": n_objects,
        "thresholds": {"pc_red": PC_RED, "pc_yellow": PC_YELLOW},
        "counts": counts,
        "intra_max_age_days": INTRA_MAX_AGE_DAYS,
        "covariance_model": "assumed RTN sigma (km) = [0.1, 0.5, 0.2] + [0.1, 1.5, 0.1] * element age (days); debris x2",
        # Compact rows; names and groups are looked up from leo_objects.json by NORAD ID.
        "fields": APP_FIELDS,
        "rows": [[e[f] for f in APP_FIELDS] for e in trusted if e["risk"] != "green" or e["debris"]],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=168, help="screening window length (default 168 = 7 days)")
    parser.add_argument("--workers", type=int, default=min(7, mp.cpu_count()), help="parallel worker processes")
    parser.add_argument("--step", type=float, default=20, help="coarse time step in seconds (default 20)")
    parser.add_argument("--screen-km", type=float, default=5, help="keep events with miss distance below this (default 5)")
    parser.add_argument("--hbr-m", type=float, default=20, help="combined hard-body radius in metres (default 20)")
    parser.add_argument("--start", help="window start, ISO 8601 UTC (default: now)")
    parser.add_argument("--dataset", default="", help='"" for the live data, or history/<event> (see src/history.py)')
    args = parser.parse_args()
    app_dir, csv_dir = dataset_dirs(args.dataset)
    objects_path = app_dir / "leo_objects.json"

    started = time.time()
    t0 = (datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start
          else datetime.now(timezone.utc)).replace(microsecond=0)
    lo, hi = load_validity(objects_path)[2]
    if not lo <= t0.timestamp() * 1000 <= hi:
        fmt = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        raise SystemExit(f"Start {t0:%Y-%m-%d %H:%M} UTC is outside the loaded elements' range ({fmt(lo)} - {fmt(hi)} UTC).")
    params = {"step": args.step, "screen_km": args.screen_km, "hbr_m": args.hbr_m}
    n_objects = len(load_objects(objects_path)[0])
    print(f"Screening {n_objects} objects, {args.hours:g} h from {t0.isoformat()} in {args.step:g} s steps "
          f"on {args.workers} worker(s)")
    with make_pool(args.workers, objects_path) as pool:
        events, trusted, counts = run_screening(
            pool, args.workers, t0, args.hours, params,
            progress=lambda n, total: print(f"  {n}/{total} segments done ({time.time() - started:.0f} s)", flush=True))
    print(f"{len(events)} conjunctions under {args.screen_km:g} km "
          f"(red {counts['red']}, yellow {counts['yellow']}, green {counts['green']}); "
          f"{counts['intra_skipped_candidates']} same-constellation candidates with elements older than "
          f"{INTRA_MAX_AGE_DAYS} days skipped; {time.time() - started:.0f} s")

    stamp = t0.strftime("%Y%m%dT%H%M%SZ")
    csv_dir.mkdir(parents=True, exist_ok=True)
    for path in (csv_dir / f"conjunctions_{stamp}.csv", csv_dir / "conjunctions_latest.csv"):
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(events[0].keys()) if events else ["tca_utc"])
            writer.writeheader()
            writer.writerows(events)
    app_out = app_dir / "conjunctions.json"
    app_out.write_text(json.dumps(app_payload(t0, args.hours, trusted, counts, n_objects, params), separators=(",", ":")))
    print(f"Wrote {(csv_dir / 'conjunctions_latest.csv').relative_to(ROOT)} and {app_out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
