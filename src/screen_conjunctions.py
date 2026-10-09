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

Pairs inside one actively managed constellation (e.g. Starlink-Starlink) are trusted only for the
first INTRA_CUTOFF_HOURS: those satellites manoeuvre constantly to keep their spacing, which TLEs
cannot predict, so later predictions of their relative phasing are spurious (in a 7-day run their
sub-1 km events grew from ~900 on day 1 to ~9,500 on day 6, while all other pairings stayed flat).
They stay in the CSV (flag intra_constellation) but are left out of the counts and the app file.

Critical debris-debris conjunctions become "red zones": neither object can manoeuvre, and a
collision would scatter fragments that all return through the collision point once per orbit.
For each one, the active satellites that pass within ZONE_RADIUS_KM of that point during the
following orbit are listed as exposed (see debris_zones).

Outputs:
  data/conjunctions_latest.csv (+ timestamped copy): every event under --screen-km
  app/data/conjunctions.json: red and yellow events, plus every event involving debris,
                              plus the debris-debris red zones

The window is split into day-long segments screened in parallel worker processes.
With Logfire credentials (see src/telemetry.py) each run is traced: one span per segment
with its coarse-screen and refinement timings, plus CPU and memory metrics.

Usage:
    python src/screen_conjunctions.py                     # 7 days from now
    python src/screen_conjunctions.py --hours 24 --screen-km 2
    python src/screen_conjunctions.py --zones-only        # redo only the red zones of the existing app file
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

import telemetry

ROOT = Path(__file__).resolve().parent.parent
OBJECTS_PATH = ROOT / "app" / "data" / "leo_objects.json"
APP_OUT = ROOT / "app" / "data" / "conjunctions.json"
DATA_DIR = ROOT / "data"

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
INTRA_CUTOFF_HOURS = 24

PC_RED = 1e-4
PC_YELLOW = 1e-5

ZONE_RADIUS_KM = 100.0        # red zone around the collision point of a critical debris-debris conjunction
ZONE_STEP_S = 10.0
ZONE_MAX_LISTED = 50          # exposed satellites written per zone (closest first); the count is always exact


def load_objects():
    """The simulation's objects, initialised from the same OMM records the browser uses."""
    data = json.loads(OBJECTS_PATH.read_text())
    objs, sats = [], []
    for group, values in data["objects"]:
        fields = dict(zip(data["omm_fields"], values))
        sat = Satrec()
        omm.initialize(sat, {k: str(v) for k, v in fields.items()})
        sats.append(sat)
        objs.append({"norad_id": fields["NORAD_CAT_ID"], "name": fields["OBJECT_NAME"], "group": group})
    return objs, sats


def epoch_seconds(sat, t0_jd):
    """Seconds from the window start to the satellite's element epoch."""
    return ((sat.jdsatepoch + sat.jdsatepochF) - t0_jd) * 86400.0


def sgp4_at(sat, jd0, fr0, sec):
    err, r, v = sat.sgp4(jd0, fr0 + sec / 86400.0)
    return err, np.array(r), np.array(v)


def coarse_screen(sats, jd0, fr0, k_start, k_end, dt):
    """Candidate encounters between grid steps k_start..k_end-1 as arrays (i, j, t_seconds, approx_miss_km)."""
    n = len(sats)
    sat_array = SatrecArray(sats)
    radius = MAX_REL_SPEED_KM_S * dt / 2 + COARSE_KM
    far = np.column_stack([np.full(n, 1e7) + np.arange(n) * 1e4, np.zeros(n), np.zeros(n)])
    found = {"i": [], "j": [], "t": [], "d": []}

    for start in range(k_start, k_end, CHUNK_STEPS):
        m = min(CHUNK_STEPS, k_end - start)
        secs = (start + np.arange(m)) * dt
        err, r, v = sat_array.sgp4(np.full(m, jd0), fr0 + secs / 86400.0)
        for k in range(m):
            pos = np.where((err[:, k] != 0)[:, None], far, r[:, k, :])
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


def debris_zones(events, objs, sats, t0):
    """Red zones for the critical debris-debris events in `events`.

    Returns one dict per zone: the event's key (tca_ms, norad_1, norad_2), the exposure window, and
    the active satellites passing within ZONE_RADIUS_KM of the collision point (midpoint of the two
    objects at TCA) between TCA and one orbital period later, as [norad_id, closest_km, time_ms].
    """
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    t0_ms = int(t0.timestamp() * 1000)
    index = {o["norad_id"]: k for k, o in enumerate(objs)}
    active = np.array([k for k, o in enumerate(objs) if o["group"] != "Debris"])
    earth = sats[0].radiusearthkm
    perigee = np.array([(1 + sats[k].altp) * earth for k in active])
    apogee = np.array([(1 + sats[k].alta) * earth for k in active])

    zones = []
    for e in events:
        a, b = index.get(e["norad_1"]), index.get(e["norad_2"])
        if e["risk"] != "red" or a is None or b is None or not objs[a]["group"] == objs[b]["group"] == "Debris":
            continue
        tca = (e["tca_ms"] - t0_ms) / 1000.0
        err_a, ra, _ = sgp4_at(sats[a], jd0, fr0, tca)
        err_b, rb, _ = sgp4_at(sats[b], jd0, fr0, tca)
        if err_a or err_b:
            continue
        point = (ra + rb) / 2
        window_s = 2 * math.pi / min(sats[a].no_kozai, sats[b].no_kozai) * 60.0   # the longer of the two periods

        # Only satellites whose altitude range reaches the zone can pass through it (30 km slack
        # for the difference between mean and osculating radius).
        reach = ZONE_RADIUS_KM + 30.0
        radius = np.linalg.norm(point)
        near = active[(perigee - reach <= radius) & (apogee + reach >= radius)]
        closest = np.full(len(near), np.inf)
        when = np.zeros(len(near))
        if len(near):
            sat_array = SatrecArray([sats[k] for k in near])
            secs = tca + np.arange(0.0, window_s + ZONE_STEP_S, ZONE_STEP_S)
            for start in range(0, len(secs), CHUNK_STEPS):
                chunk = secs[start:start + CHUNK_STEPS]
                err, r, v = sat_array.sgp4(np.full(len(chunk), jd0), fr0 + chunk / 86400.0)
                dr = r - point
                # Closest point to the zone centre on the straight line through each sample.
                tau = np.clip(-np.einsum("nkx,nkx->nk", dr, v) / np.einsum("nkx,nkx->nk", v, v),
                              -ZONE_STEP_S / 2, ZONE_STEP_S / 2)
                dist = np.linalg.norm(dr + v * tau[..., None], axis=2)
                dist[err != 0] = np.inf
                k_min = np.argmin(dist, axis=1)
                rows = np.arange(len(near))
                better = dist[rows, k_min] < closest
                closest[better] = dist[rows, k_min][better]
                when[better] = (chunk[k_min] + tau[rows, k_min])[better]
        inside = np.flatnonzero(closest < ZONE_RADIUS_KM)
        inside = inside[np.argsort(closest[inside])]
        zones.append({
            "tca_ms": e["tca_ms"], "norad_1": e["norad_1"], "norad_2": e["norad_2"],
            "window_min": round(window_s / 60, 1),
            "n_satellites": int(len(inside)),
            "satellites": [[objs[near[n]]["norad_id"], round(float(closest[n]), 1), t0_ms + int(round(when[n] * 1000))]
                           for n in inside[:ZONE_MAX_LISTED]],
        })
    return zones


def zones_payload(events, objs, sats, t0):
    with telemetry.span("Debris-debris red zones") as span:
        zones = debris_zones(events, objs, sats, t0)
        exposed = sum(1 for z in zones if z["n_satellites"])
        span.set_attribute("zones", len(zones))
        span.set_attribute("zones_with_exposed_satellites", exposed)
    print(f"{len(zones)} debris-debris red zones, {exposed} with active satellites passing within {ZONE_RADIUS_KM:g} km")
    return {"radius_km": ZONE_RADIUS_KM, "zones": zones}


def update_zones_only():
    """Recompute the red zones of the existing app file from its own events, without re-screening."""
    data = json.loads(APP_OUT.read_text())
    t0 = datetime.fromisoformat(data["window_start_utc"].replace("Z", "+00:00"))
    events = [dict(zip(data["fields"], row)) for row in data["rows"]]
    objs, sats = load_objects()
    data["debris_zones"] = zones_payload(events, objs, sats, t0)
    APP_OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"Updated {APP_OUT.relative_to(ROOT)}")


_WORKER = {}


def init_worker(t0, args, trace):
    """Runs once in each worker process. Forked workers inherit the parsed objects; on platforms
    without fork (Windows) the worker starts empty and loads them itself."""
    if "sats" not in _WORKER:
        objs, sats = load_objects()
        _WORKER.update(objs=objs, sats=sats, t0=t0, args=args)
    _WORKER["trace"] = trace
    telemetry.configure("screen-conjunctions")


def screen_segment(segment):
    """Screen grid steps [k_start, k_end) and return refined events (runs in a worker process)."""
    hours = [round(k * _WORKER["args"].step / 3600, 1) for k in segment]
    with telemetry.attach_context(_WORKER.get("trace")):
        with telemetry.span("Screen hours {from_h}-{to_h}", from_h=hours[0], to_h=hours[1]) as span:
            events = segment_events(segment, span)
    telemetry.flush()
    return events


def segment_events(segment, span):
    objs, sats, t0, args = _WORKER["objs"], _WORKER["sats"], _WORKER["t0"], _WORKER["args"]
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    with telemetry.span("Coarse screen: SGP4 grid and KD-tree"):
        i, j, t, d = coarse_screen(sats, jd0, fr0, *segment, args.step)
    # The straight-line estimate is within ~1 km of the exact miss, so far candidates can be skipped.
    near = d < args.screen_km + 3
    candidates = dedupe(i[near], j[near], t[near], d[near], args.step)
    span.set_attribute("coarse_candidates", len(i))
    span.set_attribute("refined_candidates", len(candidates))

    refine_started = time.time()
    events = []
    for a, b, tc, _ in candidates:
        res = refine(sats[a], sats[b], jd0, fr0, float(tc))
        if res is None:
            continue
        tca, ri, vi, rj, vj = res
        miss = float(np.linalg.norm(rj - ri))
        if miss >= args.screen_km or not 0 <= tca <= args.hours * 3600:
            continue
        oa, ob = objs[a], objs[b]
        age_a = (tca - epoch_seconds(sats[a], jd0 + fr0)) / 86400
        age_b = (tca - epoch_seconds(sats[b], jd0 + fr0)) / 86400
        pc = collision_probability(
            ri, vi, rj, vj,
            covariance(ri, vi, age_a, oa["group"] == "Debris"),
            covariance(rj, vj, age_b, ob["group"] == "Debris"),
            args.hbr_m / 1000,
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
    span.set_attribute("events", len(events))
    span.set_attribute("red_events", sum(1 for e in events if e["risk"] == "red"))
    span.set_attribute("refine_seconds", round(time.time() - refine_started, 1))
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=168, help="screening window length (default 168 = 7 days)")
    parser.add_argument("--workers", type=int, default=min(7, mp.cpu_count()), help="parallel worker processes")
    parser.add_argument("--step", type=float, default=20, help="coarse time step in seconds (default 20)")
    parser.add_argument("--screen-km", type=float, default=5, help="keep events with miss distance below this (default 5)")
    parser.add_argument("--hbr-m", type=float, default=20, help="combined hard-body radius in metres (default 20)")
    parser.add_argument("--start", help="window start, ISO 8601 UTC (default: now)")
    parser.add_argument("--zones-only", action="store_true",
                        help="only recompute the debris-debris red zones of the existing app/data/conjunctions.json")
    args = parser.parse_args()
    if args.zones_only:
        return update_zones_only()

    started = time.time()
    t0 = (datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start
          else datetime.now(timezone.utc)).replace(microsecond=0)
    steps = int(args.hours * 3600 / args.step) + 1
    seg_steps = int(24 * 3600 / args.step)
    segments = [(k, min(k + seg_steps, steps)) for k in range(0, steps, seg_steps)]

    objs, sats = load_objects()
    print(f"Screening {len(sats)} objects, {args.hours:g} h from {t0.isoformat()} in {args.step:g} s steps, "
          f"{len(segments)} segment(s) on {min(args.workers, len(segments))} worker(s)")

    # Workers forked after these globals are set inherit the parsed objects (see init_worker).
    _WORKER.update(objs=objs, sats=sats, t0=t0, args=args)
    start_method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    n_workers = min(args.workers, len(segments))
    telemetry.info("Screening {objects} objects for {hours} h on {workers} workers", objects=len(sats),
                   hours=args.hours, workers=n_workers, segments=len(segments), step_s=args.step,
                   screen_km=args.screen_km, window_start=t0.isoformat(), start_method=start_method)
    with mp.get_context(start_method).Pool(n_workers, initializer=init_worker,
                                           initargs=(t0, args, telemetry.get_context())) as pool:
        results = []
        for n_done, seg_events in enumerate(pool.imap_unordered(screen_segment, segments), 1):
            results.extend(seg_events)
            print(f"  {n_done}/{len(segments)} segments done ({time.time() - started:.0f} s)", flush=True)
            telemetry.info("{done}/{total} segments done, {events} events so far", done=n_done,
                           total=len(segments), events=len(results))

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

    cutoff_ms = int((t0 + timedelta(hours=INTRA_CUTOFF_HOURS)).timestamp() * 1000)
    trusted = [e for e in events if not (e["intra_constellation"] and e["tca_ms"] > cutoff_ms)]
    counts = {level: sum(1 for e in trusted if e["risk"] == level) for level in ("red", "yellow", "green")}
    counts["debris"] = sum(1 for e in trusted if e["debris"])
    counts["intra_after_cutoff"] = len(events) - len(trusted)
    print(f"{len(events)} conjunctions under {args.screen_km:g} km; {len(trusted)} after dropping "
          f"intra-constellation pairs beyond {INTRA_CUTOFF_HOURS} h "
          f"(red {counts['red']}, yellow {counts['yellow']}, green {counts['green']}) in {time.time() - started:.0f} s")
    telemetry.info("{conjunctions} conjunctions: {red} red, {yellow} yellow, {green} green", conjunctions=len(events),
                   trusted=len(trusted), seconds=round(time.time() - started), **counts)

    zones = zones_payload(trusted, objs, sats, t0)

    stamp = t0.strftime("%Y%m%dT%H%M%SZ")
    DATA_DIR.mkdir(exist_ok=True)
    for path in (DATA_DIR / f"conjunctions_{stamp}.csv", DATA_DIR / "conjunctions_latest.csv"):
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(events[0].keys()) if events else ["tca_utc"])
            writer.writeheader()
            writer.writerows(events)

    APP_OUT.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_start_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": args.hours,
        "step_s": args.step,
        "screen_km": args.screen_km,
        "hbr_m": args.hbr_m,
        "n_objects": len(sats),
        "thresholds": {"pc_red": PC_RED, "pc_yellow": PC_YELLOW},
        "counts": counts,
        "intra_cutoff_hours": INTRA_CUTOFF_HOURS,
        "covariance_model": "assumed RTN sigma (km) = [0.1, 0.5, 0.2] + [0.1, 1.5, 0.1] * element age (days); debris x2",
        # Compact rows; names and groups are looked up from leo_objects.json by NORAD ID.
        "fields": APP_FIELDS,
        "rows": [[e[f] for f in APP_FIELDS] for e in trusted if e["risk"] != "green" or e["debris"]],
        "debris_zones": zones,
    }, separators=(",", ":")))
    print(f"Wrote data/conjunctions_latest.csv and {APP_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    telemetry.configure("screen-conjunctions", system_metrics=True)
    with telemetry.span("Screen conjunctions"):
        main()
