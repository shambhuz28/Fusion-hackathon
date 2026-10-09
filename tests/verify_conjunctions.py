"""Independent checks of the derived positions and the conjunction screening results.

  A. Positions CSVs (OMM -> SGP4) vs the TLEs used by the screener and the simulation.
  B. TCA and miss distance re-derived by dense 1 ms sampling (all days, segment boundaries, debris).
  C. Completeness: brute-force 1 s screening in three windows; nothing may be missing or duplicated.
  D. Geometry: miss vector perpendicular to the relative velocity, RTN components, relative speed.
  E. Pc vs exact 2D integration over the hard-body disk and a Monte Carlo estimate.

Usage:
    python tests/verify_conjunctions.py [--samples 300]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import _venv  # noqa: E402,F401  (re-runs with .venv/bin/python if packages are missing)

import argparse
import csv
import json
import math
import random
import time
from datetime import datetime, timezone

import numpy as np
from scipy import integrate
from scipy.spatial import cKDTree
from sgp4.api import SatrecArray, jday

import screen_conjunctions as sc  # noqa: E402

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def load():
    meta = json.loads((ROOT / "app" / "data" / "conjunctions.json").read_text())
    objs, sats = sc.load_objects()
    index = {o["norad_id"]: k for k, o in enumerate(objs)}
    t0 = datetime.fromisoformat(meta["window_start_utc"].replace("Z", "+00:00"))
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    events = []
    with (ROOT / "data" / "conjunctions_latest.csv").open() as f:
        for r in csv.DictReader(f):
            events.append({
                "tca_ms": int(r["tca_ms"]), "n1": int(r["norad_1"]), "n2": int(r["norad_2"]),
                "miss": float(r["miss_km"]), "rtn": [float(r["miss_radial_km"]), float(r["miss_along_km"]), float(r["miss_cross_km"])],
                "vrel": float(r["rel_speed_km_s"]), "pc": float(r["pc"]), "debris": r["debris"] == "True",
                "g1": r["group_1"], "g2": r["group_2"],
            })
    return meta, objs, sats, index, t0, jd0, fr0, events


def sec_of(ms, t0):
    return (ms - t0.timestamp() * 1000) / 1000


# ---------------------------------------------------------------- A
def check_positions(sats, index):
    worst_r = worst_v = 0.0
    n = 0
    for path in sorted((ROOT / "data").glob("*_leo_positions_latest.csv")):
        with path.open() as f:
            for row in csv.DictReader(f):
                k = index.get(int(row["norad_id"]))
                if k is None or not row["x_teme_km"]:
                    continue
                t = datetime.fromisoformat(row["position_time_utc"].replace("Z", "+00:00"))
                jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
                e, r, v = sats[k].sgp4(jd, fr)
                if e:
                    continue
                dr = math.dist(r, [float(row[c]) for c in ("x_teme_km", "y_teme_km", "z_teme_km")])
                dv = math.dist(v, [float(row[c]) for c in ("vx_teme_km_s", "vy_teme_km_s", "vz_teme_km_s")])
                worst_r, worst_v, n = max(worst_r, dr), max(worst_v, dv), n + 1
    check("A positions CSV vs screening OMM records", worst_r < 0.01 and worst_v < 1e-5,
          f"{n} objects, max position diff {worst_r * 1000:.2f} m, max velocity diff {worst_v * 1e6:.3f} mm/s")


# ---------------------------------------------------------------- B + D
def dense_min(sa, sb, jd0, fr0, s):
    """Minimum distance by sampling ±1 s around s at 1 ms (independent of the screener's Newton step)."""
    ts = s + np.arange(-1000, 1001) / 1000.0
    arr = SatrecArray([sa, sb])
    e, r, _ = arr.sgp4(np.full(ts.size, jd0), fr0 + ts / 86400.0)
    d = np.linalg.norm(r[0] - r[1], axis=1)
    k = int(np.argmin(d))
    return d[k], ts[k]


def check_events(meta, sats, index, t0, jd0, fr0, events, samples):
    day_ms = 86400000
    t0ms = t0.timestamp() * 1000
    pick = []
    by_day = {}
    for e in events:
        by_day.setdefault(int((e["tca_ms"] - t0ms) // day_ms), []).append(e)
    rng = random.Random(1)
    for d, evs in by_day.items():
        pick += rng.sample(evs, min(len(evs), samples // len(by_day)))
    pick += [e for e in events if any(abs(e["tca_ms"] - (t0ms + k * day_ms)) < 120000 for k in range(1, 8))][:40]
    pick += rng.sample([e for e in events if e["debris"]], 40)
    pick += sorted(events, key=lambda e: -e["pc"])[:20]

    worst_miss = worst_t = worst_perp = worst_rtn = worst_vrel = 0.0
    for e in pick:
        a, b = sats[index[e["n1"]]], sats[index[e["n2"]]]
        s = sec_of(e["tca_ms"], t0)
        dmin, tmin = dense_min(a, b, jd0, fr0, s)
        worst_miss = max(worst_miss, abs(dmin - e["miss"]))
        worst_t = max(worst_t, abs(tmin - s))
        _, ri, vi = sc.sgp4_at(a, jd0, fr0, s)
        _, rj, vj = sc.sgp4_at(b, jd0, fr0, s)
        dr, dv = rj - ri, vj - vi
        # Offset of the miss vector along the relative velocity, minus what the 1 ms rounding of the
        # stored TCA can explain; exactly at TCA it would be zero.
        along = abs(dr @ dv) / np.linalg.norm(dv)
        worst_perp = max(worst_perp, along - np.linalg.norm(dv) * 0.0005)
        worst_rtn = max(worst_rtn, abs(math.hypot(*e["rtn"]) - e["miss"]))
        worst_vrel = max(worst_vrel, abs(np.linalg.norm(dv) - e["vrel"]))
    check("B TCA/miss vs dense 1 ms sampling", worst_miss < 0.002 and worst_t <= 0.002,
          f"{len(pick)} events (all days, segment boundaries, debris, top Pc): max miss diff {worst_miss * 1000:.2f} m, "
          f"max TCA diff {worst_t * 1000:.1f} ms")
    check("D geometry", worst_perp < 0.001 and worst_rtn < 0.001 and worst_vrel < 0.001,
          f"miss vector perpendicular to v_rel within {max(worst_perp, 0) * 1000:.2f} m beyond TCA rounding, "
          f"max |RTN| - miss {worst_rtn * 1000:.2f} m, "
          f"max rel-speed diff {worst_vrel * 1000:.2f} m/s")
    return pick


# ---------------------------------------------------------------- C
def brute_force(meta, objs, sats, t0, jd0, fr0, events, start_s, minutes):
    """1 s steps, linear closest approach within each step, exact SGP4 refinement of hits."""
    arr = SatrecArray(sats)
    valid_from, valid_to, _ = sc.load_validity()   # same per-object validity windows as the screener
    t0_ms = t0.timestamp() * 1000
    screen_km = meta["screen_km"]
    radius = sc.MAX_REL_SPEED_KM_S * 0.5 + screen_km + 1
    best = {}
    for c0 in range(0, minutes * 60, 120):
        secs = start_s + c0 + np.arange(120.0)
        err, r, v = arr.sgp4(np.full(secs.size, jd0), fr0 + secs / 86400.0)
        t_ms = t0_ms + secs * 1000
        bad = (err != 0) | (t_ms[None, :] < valid_from[:, None]) | (t_ms[None, :] > valid_to[:, None])
        for k in range(secs.size):
            p = np.where(bad[:, k][:, None], 1e9 + np.arange(len(sats))[:, None] * 1e4, r[:, k, :])
            pairs = cKDTree(p).query_pairs(radius, output_type="ndarray")
            if not len(pairs):
                continue
            i, j = pairs[:, 0], pairs[:, 1]
            dr, dv = p[j] - p[i], v[j, k] - v[i, k]
            dv2 = np.einsum("ij,ij->i", dv, dv)
            ok = dv2 > sc.MIN_REL_SPEED_KM_S ** 2
            i, j, dr, dv, dv2 = i[ok], j[ok], dr[ok], dv[ok], dv2[ok]
            tau = np.clip(-np.einsum("ij,ij->i", dr, dv) / dv2, -0.5, 0.5)
            dmin = np.linalg.norm(dr + dv * tau[:, None], axis=1)
            for a, b, dd, tt in zip(i, j, dmin, secs[k] + tau):
                if dd < screen_km + 0.5 and dd < best.get((a, b), (99, 0))[0]:
                    best[(a, b)] = (dd, tt)
    found = {}
    for (a, b), (_, tt) in best.items():
        res = sc.refine(sats[a], sats[b], jd0, fr0, float(tt))
        tca_ms = t0_ms + res[0] * 1000 if res else 0
        if res and sc.untrusted_intra_pair(objs, sats, a, b, float(res[0]), jd0, fr0):
            continue   # skipped by the screener by design (stale same-constellation elements)
        if res and np.linalg.norm(res[3] - res[1]) < screen_km and start_s <= res[0] <= start_s + minutes * 60 \
                and valid_from[a] <= tca_ms <= valid_to[a] and valid_from[b] <= tca_ms <= valid_to[b]:
            found[(objs[a]["norad_id"], objs[b]["norad_id"])] = res[0]
    lo, hi = (t0.timestamp() + start_s) * 1000, (t0.timestamp() + start_s + minutes * 60) * 1000
    screened = {}
    for e in events:
        if lo <= e["tca_ms"] <= hi:
            screened.setdefault((e["n1"], e["n2"]), []).append(e)
    # Ignore borderline cases within 50 ms of the window edges.
    missing = [p for p, t in found.items() if p not in screened and lo + 50 < (t0.timestamp() + t) * 1000 < hi - 50]
    extra = [p for p in screened if p not in found
             and all(lo + 50 < e["tca_ms"] < hi - 50 for e in screened[p])]
    dupes = [p for p, evs in screened.items() if len(evs) > 1]
    return len(found), len(screened), missing, extra, dupes


def check_completeness(meta, objs, sats, t0, jd0, fr0, events):
    windows = [("start", 0), ("segment boundary at 24 h", 24 * 3600 - 600), ("day 5", 5 * 86400 + 3600)]
    for label, start in windows:
        t = time.time()
        nb, ns, missing, extra, dupes = brute_force(meta, objs, sats, t0, jd0, fr0, events, start, 20)
        check(f"C completeness, 20 min at {label}", not missing and not extra and not dupes,
              f"brute force {nb} events, screener {ns}; missing {len(missing)}, extra {len(extra)}, "
              f"duplicates {len(dupes)} ({time.time() - t:.0f} s)")


# ---------------------------------------------------------------- E
def exact_pc(mu, c2, R):
    """Integral of the 2D Gaussian over the disk of radius R (polar coordinates)."""
    inv = np.linalg.inv(c2)
    norm = 1 / (2 * math.pi * math.sqrt(np.linalg.det(c2)))

    def f(theta, rho):
        x = np.array([rho * math.cos(theta), rho * math.sin(theta)]) - mu
        return norm * math.exp(-0.5 * x @ inv @ x) * rho
    return integrate.dblquad(f, 0, R, 0, 2 * math.pi, epsabs=0, epsrel=1e-8)[0]


def check_pc(meta, sats, index, t0, jd0, fr0, pick, objs):
    hbr = meta["hbr_m"] / 1000
    rel_worst = 0.0
    mc_checked = 0
    mc_worst_sigma = 0.0
    rng = np.random.default_rng(0)
    for e in pick:
        a, b = index[e["n1"]], index[e["n2"]]
        s = sec_of(e["tca_ms"], t0)
        _, ri, vi = sc.sgp4_at(sats[a], jd0, fr0, s)
        _, rj, vj = sc.sgp4_at(sats[b], jd0, fr0, s)
        age_a = (s - sc.epoch_seconds(sats[a], jd0 + fr0)) / 86400
        age_b = (s - sc.epoch_seconds(sats[b], jd0 + fr0)) / 86400
        ci = sc.covariance(ri, vi, age_a, objs[a]["group"] == "Debris")
        cj = sc.covariance(rj, vj, age_b, objs[b]["group"] == "Debris")
        miss, vrel = rj - ri, vj - vi
        vhat = vrel / np.linalg.norm(vrel)
        plane = miss - (miss @ vhat) * vhat
        x = plane / np.linalg.norm(plane)
        B = np.column_stack([x, np.cross(vhat, x)])
        c2 = B.T @ (ci + cj) @ B
        mu = np.array([np.linalg.norm(plane), 0.0])
        exact = exact_pc(mu, c2, hbr)
        if exact > 1e-12:
            rel_worst = max(rel_worst, abs(e["pc"] - exact) / exact)
        if exact > 2e-4 and mc_checked < 8:   # Monte Carlo is only practical for larger Pc
            n = 20_000_000
            pts = rng.multivariate_normal(mu, c2, size=n)
            hits = np.count_nonzero(np.einsum("ij,ij->i", pts, pts) < hbr * hbr)
            sigma = math.sqrt(exact * n) / n
            mc_worst_sigma = max(mc_worst_sigma, abs(hits / n - exact) / sigma)
            mc_checked += 1
    check("E Pc vs exact disk integral", rel_worst < 1e-3,
          f"{len(pick)} events, max relative difference {rel_worst * 100:.3f}% (stored Pc rounded to 4 digits)")
    check("E exact integral vs Monte Carlo (2e7 samples)", mc_worst_sigma < 4,
          f"{mc_checked} events, worst deviation {mc_worst_sigma:.1f} sigma")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--skip-brute-force", action="store_true")
    args = parser.parse_args()

    t = time.time()
    meta, objs, sats, index, t0, jd0, fr0, events = load()
    print(f"Loaded {len(events)} events, {len(sats)} objects, window {meta['window_start_utc']} ({time.time() - t:.0f} s)")
    check_positions(sats, index)
    pick = check_events(meta, sats, index, t0, jd0, fr0, events, args.samples)
    check_pc(meta, sats, index, t0, jd0, fr0, pick, objs)
    if not args.skip_brute_force:
        check_completeness(meta, objs, sats, t0, jd0, fr0, events)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
