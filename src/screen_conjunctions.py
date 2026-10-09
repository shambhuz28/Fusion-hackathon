"""All-vs-all conjunction screening for the objects in app/data/leo_tle.json.

Pipeline:
  1. Propagate every object with SGP4 on a fixed time grid (default 20 s steps, 24 h).
  2. At each step, a KD-tree finds pairs closer than the distance two objects can cover
     in half a step; a straight-line relative-motion check keeps pairs whose closest
     approach within that step is under COARSE_KM.
  3. Each candidate is refined with exact SGP4 states to its time of closest approach (TCA)
     and miss distance; events under --screen-km are kept.
  4. Probability of collision (Pc) uses the 2D short-encounter method (Foster/Chan) with an
     ASSUMED covariance, because TLEs carry none: 1-sigma RTN errors grow with element age.

Pairs with relative speed below MIN_REL_SPEED_KM_S (formation flying, e.g. freshly launched
trains) are skipped: the short-encounter Pc model does not apply to them.

Pairs inside one actively managed constellation (e.g. Starlink-Starlink) are trusted only for the
first INTRA_CUTOFF_HOURS: those satellites manoeuvre constantly to keep their spacing, which TLEs
cannot predict, so later predictions of their relative phasing are spurious (in a 7-day run their
sub-1 km events grew from ~900 on day 1 to ~9,500 on day 6, while all other pairings stayed flat).
They stay in the CSV (flag intra_constellation) but are left out of the counts and the app file.

Outputs:
  data/conjunctions_latest.csv (+ timestamped copy): every event under --screen-km
  app/data/conjunctions.json: red and yellow events, plus every event involving debris

The window is split into day-long segments screened in parallel worker processes.

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

import numpy as np
from scipy.spatial import cKDTree
from sgp4.api import Satrec, SatrecArray, jday

ROOT = Path(__file__).resolve().parent.parent
TLE_PATH = ROOT / "app" / "data" / "leo_tle.json"
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


def load_objects():
    data = json.loads(TLE_PATH.read_text())
    objs = [dict(zip(data["fields"], row)) for row in data["satellites"]]
    sats = [Satrec.twoline2rv(o["line1"], o["line2"]) for o in objs]
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


def collision_probability(ri, vi, rj, vj, cov_i, cov_j, hbr_km):
    """2D Pc in the encounter plane; small hard-body approximation (HBR << sigma)."""
    miss = rj - ri
    vrel = vj - vi
    d = np.linalg.norm(miss)
    x = miss / d if d > 1e-9 else np.cross(vrel, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(vrel, x); y /= np.linalg.norm(y)
    b = np.column_stack([x, y])
    c2 = b.T @ (cov_i + cov_j) @ b
    mu = np.array([d, 0.0])
    det = np.linalg.det(c2)
    density = math.exp(-0.5 * mu @ np.linalg.solve(c2, mu)) / (2 * math.pi * math.sqrt(det))
    return min(1.0, math.pi * hbr_km ** 2 * density)


def risk_level(pc, miss_km):
    if pc >= PC_RED or miss_km < 0.2:
        return "red"
    if pc >= PC_YELLOW or miss_km < 1.0:
        return "yellow"
    return "green"


_WORKER = {}


def screen_segment(segment):
    """Screen grid steps [k_start, k_end) and return refined events (runs in a worker process)."""
    objs, sats, t0, args = _WORKER["objs"], _WORKER["sats"], _WORKER["t0"], _WORKER["args"]
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    i, j, t, d = coarse_screen(sats, jd0, fr0, *segment, args.step)
    # The straight-line estimate is within ~1 km of the exact miss, so far candidates can be skipped.
    near = d < args.screen_km + 3
    candidates = dedupe(i[near], j[near], t[near], d[near], args.step)

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
            "altitude_km": round(float(np.linalg.norm(ri)) - 6378.137, 1),
            "pc": float(f"{pc:.4g}"),
            "risk": risk_level(pc, miss),
            "debris": "Debris" in (oa["group"], ob["group"]),
            "intra_constellation": oa["group"] == ob["group"] and oa["group"] in MANAGED_CONSTELLATIONS,
        })
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=168, help="screening window length (default 168 = 7 days)")
    parser.add_argument("--workers", type=int, default=min(7, mp.cpu_count()), help="parallel worker processes")
    parser.add_argument("--step", type=float, default=20, help="coarse time step in seconds (default 20)")
    parser.add_argument("--screen-km", type=float, default=5, help="keep events with miss distance below this (default 5)")
    parser.add_argument("--hbr-m", type=float, default=20, help="combined hard-body radius in metres (default 20)")
    parser.add_argument("--start", help="window start, ISO 8601 UTC (default: now)")
    args = parser.parse_args()

    started = time.time()
    t0 = (datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start
          else datetime.now(timezone.utc)).replace(microsecond=0)
    steps = int(args.hours * 3600 / args.step) + 1
    seg_steps = int(24 * 3600 / args.step)
    segments = [(k, min(k + seg_steps, steps)) for k in range(0, steps, seg_steps)]

    objs, sats = load_objects()
    print(f"Screening {len(sats)} objects, {args.hours:g} h from {t0.isoformat()} in {args.step:g} s steps, "
          f"{len(segments)} segment(s) on {min(args.workers, len(segments))} worker(s)")

    # Workers are forked after these globals are set, so each one inherits the parsed objects.
    _WORKER.update(objs=objs, sats=sats, t0=t0, args=args)
    with mp.get_context("fork").Pool(min(args.workers, len(segments))) as pool:
        results = []
        for n_done, seg_events in enumerate(pool.imap_unordered(screen_segment, segments), 1):
            results.extend(seg_events)
            print(f"  {n_done}/{len(segments)} segments done ({time.time() - started:.0f} s)", flush=True)

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
        # Compact rows; names and groups are looked up from leo_tle.json by NORAD ID.
        "fields": APP_FIELDS,
        "rows": [[e[f] for f in APP_FIELDS] for e in trusted if e["risk"] != "green" or e["debris"]],
    }, separators=(",", ":")))
    print(f"Wrote data/conjunctions_latest.csv and {APP_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
