"""Independent checks of the avoidance manoeuvre plans (src/maneuver.py).

The planner moves the satellite with the linear Clohessy-Wiltshire equations. Here each plan is flown again
with a different model: the satellite's state is integrated numerically under two-body gravity, with the
plan's burns applied as velocity jumps, next to an unburned copy. The difference of the two, in the radial /
along-track / cross-track frame, is compared with the planner's path.

  A. Path: the planner's offsets agree with the integrated ones along the whole detour.
  B. Encounter: the integrated path misses the other object by MIN_MISS_KM and has Pc <= MAX_PC. The burn is
     sized to sit exactly on a limit, so the second model may land slightly on either side: 2 % of the miss
     distance and 10 % of Pc are allowed.
  C. Return: after the last burn the integrated satellite is back on its original orbit: same semi-major
     axis (so the same period), and what is left of the radial and cross-track offset over the next orbit
     is under 1 % of the detour (the linearisation error of the CW model).
  D. Minimum fuel: 3 % less avoidance burn in the same direction is no longer safe, and no burn of the
     same size in 2,000 random directions does better than the chosen one.
  E. Only satellites manoeuvre: a debris-debris pair gets no plan.

Usage:
    python tests/verify_maneuver.py [--events 6]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import _venv  # noqa: E402,F401  (re-runs with .venv/bin/python if packages are missing)

import argparse
import json
import math
from datetime import datetime

import numpy as np
from scipy.integrate import solve_ivp

import maneuver  # noqa: E402
import screen_conjunctions as sc  # noqa: E402

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def two_body(t, y):
    r = y[:3]
    return np.r_[y[3:], -maneuver.MU_KM3_S2 * r / np.linalg.norm(r) ** 3]


def fly(r0, v0, t0, times, burns=()):
    """Two-body states at `times` (s) from (r0, v0) at t0; burns are (time, ECI delta-v) velocity jumps."""
    times = np.asarray(times, dtype=float)
    out, y, t = np.zeros((times.size, 6)), np.r_[r0, v0], t0
    for t_stop, dv in sorted(burns, key=lambda b: b[0]) + [(times[-1], None)]:
        if t_stop > t:
            sol = solve_ivp(two_body, (t, t_stop), y, dense_output=True, rtol=1e-11, atol=1e-11, method="DOP853")
            sel = (times >= t) & (times <= t_stop)
            out[sel] = sol.sol(times[sel]).T
            y, t = sol.y[:, -1], t_stop
        if dv is not None:
            y = y + np.r_[0.0, 0.0, 0.0, dv]
    return out


def semi_major_axis(y):
    r, v2 = np.linalg.norm(y[:3]), y[3:] @ y[3:]
    return 1 / (2 / r - v2 / maneuver.MU_KM3_S2)


def verify_plan(p, sats, index, objs, tca_ms, hbr_km):
    sat = sats[index[p["manoeuvring_norad"]]]
    other = sats[index[p["other_norad"]]]
    path = p["path"]
    t0 = path["t0_ms"] / 1000.0
    times = t0 + np.arange(len(path["offset_km"])) * path["step_ms"] / 1000.0
    r, v, _ = maneuver.states([sat], times * 1000)
    r, v = r[0], v[0]

    # The burns act in the frame of the unburned satellite, which the burned one shares to first order.
    ref = fly(r[0], v[0], t0, times)
    burn_list = []
    for b in p["burns"]:
        t_b = b["time_ms"] / 1000.0
        at_burn = fly(r[0], v[0], t0, [t_b])[0] if t_b > t0 else ref[0]
        burn_list.append((t_b, maneuver.rtn(at_burn[:3], at_burn[3:]) @ (np.array(b["dv_rtn_mps"]) / 1000.0)))
    burned = fly(r[0], v[0], t0, times, burn_list)

    # A: offsets in each satellite's own radial / along-track / cross-track frame.
    worst = scale = 0.0
    for k in range(len(times)):
        planned = maneuver.rtn(r[k], v[k]).T @ np.array(path["offset_km"][k])
        flown = maneuver.rtn(ref[k, :3], ref[k, 3:]).T @ (burned[k, :3] - ref[k, :3])
        worst, scale = max(worst, np.linalg.norm(planned - flown)), max(scale, np.linalg.norm(flown))

    # B: the encounter, with the integrated offset added to the SGP4 state at TCA (the short-encounter Pc is
    # only valid there, so both paths are integrated to exactly that time).
    t_enc = tca_ms / 1000.0
    at_ref = fly(r[0], v[0], t0, [t_enc])[0]
    at_burned = fly(r[0], v[0], t0, [t_enc], [b for b in burn_list if b[0] < t_enc])[0]
    (rm, vm), (ro, vo) = [(a[0, 0], b[0, 0]) for a, b, _ in (maneuver.states([sat], [tca_ms]),
                                                             maneuver.states([other], [tca_ms]))]
    basis, basis_ref = maneuver.rtn(rm, vm), maneuver.rtn(at_ref[:3], at_ref[3:])
    r1 = rm + basis @ (basis_ref.T @ (at_burned[:3] - at_ref[:3]))
    v1 = vm + basis @ (basis_ref.T @ (at_burned[3:] - at_ref[3:]))
    miss, _ = maneuver.encounter(r1, v1, ro, vo)
    ages = [(t_enc - (s.jdsatepoch + s.jdsatepochF - maneuver.JD_UNIX) * 86400) / 86400 for s in (sat, other)]
    covs = [sc.covariance(a, b, age, objs[index[n]]["group"] == "Debris")
            for a, b, age, n in ((rm, vm, ages[0], p["manoeuvring_norad"]), (ro, vo, ages[1], p["other_norad"]))]
    pc = sc.collision_probability(r1, v1, ro, vo, covs[0], covs[1], hbr_km)

    # C: back on the original orbit after the last burn.
    da = abs(semi_major_axis(burned[-1]) - semi_major_axis(ref[-1]))
    tail = [maneuver.rtn(ref[j, :3], ref[j, 3:]).T @ (burned[j, :3] - ref[j, :3])
            for j in range(len(times)) if times[j] > p["back_on_orbit_ms"] / 1000.0]
    off_orbit = max(math.hypot(x[0], x[2]) for x in tail)
    return worst, scale, miss, pc, da, off_orbit


def check_minimum(objs, sats, valid, index, e, p, not_before_ms, hbr_km, rng):
    """D: re-evaluate the planner's safety test around the chosen avoidance burn."""
    i_m, i_o = index[p["manoeuvring_norad"]], index[p["other_norad"]]
    res = sc.refine(sats[i_m], sats[i_o], maneuver.JD_UNIX, 0.0, e["tca_ms"] / 1000.0)
    tca_s = res[0]
    (rm, vm), (ro, vo) = [(a[0, 0], b[0, 0]) for a, b, _ in (maneuver.states([sats[i_m]], [tca_s * 1000]),
                                                             maneuver.states([sats[i_o]], [tca_s * 1000]))]
    n = math.sqrt(maneuver.MU_KM3_S2 / np.linalg.norm(rm) ** 3)
    basis = maneuver.rtn(rm, vm)
    ages = [(tca_s - (s.jdsatepoch + s.jdsatepochF - maneuver.JD_UNIX) * 86400) / 86400 for s in (sats[i_m], sats[i_o])]
    cov_m = sc.covariance(rm, vm, ages[0], objs[i_m]["group"] == "Debris")
    cov_o = sc.covariance(ro, vo, ages[1], objs[i_o]["group"] == "Debris")
    t1 = p["burns"][0]["time_ms"] / 1000.0
    dv1 = np.array(p["burns"][0]["dv_rtn_mps"]) / 1000.0

    def safe(dv):
        off = maneuver.cw(np.r_[0.0, 0.0, 0.0, dv], n, tca_s - t1)
        r1, v1 = rm + basis @ off[:3], vm + basis @ off[3:]
        return (maneuver.encounter(r1, v1, ro, vo)[0] >= maneuver.MIN_MISS_KM
                and sc.collision_probability(r1, v1, ro, vo, cov_m, cov_o, hbr_km) <= maneuver.MAX_PC)

    dirs = rng.normal(size=(2000, 3))
    dirs /= np.linalg.norm(dirs, axis=1)[:, None]
    size = np.linalg.norm(dv1)
    better = sum(safe(u * size * 0.97) for u in dirs)
    return safe(dv1 * 1.01), safe(dv1 * 0.97), better


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--events", type=int, default=6, help="conjunctions to plan and verify")
    args = parser.parse_args()

    meta = json.loads((ROOT / "app" / "data" / "conjunctions.json").read_text())
    objs, sats = sc.load_objects()
    valid = sc.load_validity()[:2]
    index = {o["norad_id"]: k for k, o in enumerate(objs)}
    hbr_km = 20.0 / 1000
    not_before_ms = datetime.fromisoformat(meta["window_start_utc"].replace("Z", "+00:00")).timestamp() * 1000
    events = [dict(zip(meta["fields"], row)) for row in meta["rows"]]
    events = [e for e in events if e["norad_1"] in index and e["norad_2"] in index]
    movable = lambda e: any(maneuver.can_manoeuvre(objs[index[e[k]]]) for k in ("norad_1", "norad_2"))
    risky = sorted((e for e in events if e["risk"] != "green" and movable(e)), key=lambda e: -e["pc"])
    # Highest Pc, plus satellite-debris events, spread over the screening window.
    pick = risky[:args.events // 2] + [e for e in risky if e["debris"]][:args.events - args.events // 2]

    rng = np.random.default_rng(0)
    worst_rel = worst_abs = worst_da = worst_off = worst_off_rel = 0.0
    min_miss, max_pc, n_plans, not_minimal = math.inf, 0.0, 0, []
    for e in pick:
        result = maneuver.plan_conjunction(objs, sats, valid, e["norad_1"], e["norad_2"], e["tca_ms"], not_before_ms)
        for p in result["plans"]:
            if not p["possible"]:
                continue
            n_plans += 1
            err, scale, miss, pc, da, off = verify_plan(p, sats, index, objs, result["tca_ms"], hbr_km)
            worst_rel, worst_abs = max(worst_rel, err / scale), max(worst_abs, err)
            min_miss, max_pc, worst_da, worst_off = min(min_miss, miss), max(max_pc, pc), max(worst_da, da), max(worst_off, off)
            worst_off_rel = max(worst_off_rel, off / scale)
            ok_more, ok_less, better = check_minimum(objs, sats, valid, index, e, p, not_before_ms, hbr_km, rng)
            if not ok_more or ok_less or better:
                not_minimal.append((p["manoeuvring"], ok_more, ok_less, better))
            print(f"  {p['manoeuvring']} avoids {p['other']}: {p['total_dv_mps']:.4f} m/s total, burn "
                  f"{p['lead_orbits']} orbits before TCA, {p['direction_name']}; path error {err * 1000:.1f} m "
                  f"of {scale:.2f} km; miss {miss:.3f} km, Pc {pc:.2e}")

    check("A CW path vs two-body integration", n_plans > 0 and worst_rel < 0.02,
          f"{n_plans} plans, largest difference {worst_abs * 1000:.1f} m, {worst_rel * 100:.2f} % of the detour")
    check("B encounter with the integrated path", min_miss >= 0.98 * maneuver.MIN_MISS_KM and max_pc <= 1.1 * maneuver.MAX_PC,
          f"smallest miss {min_miss:.3f} km (limit {maneuver.MIN_MISS_KM}), largest Pc {max_pc:.2e} (limit {maneuver.MAX_PC:g})")
    check("C back on the original orbit", worst_da < 0.005 and worst_off_rel < 0.01,
          f"semi-major axis differs by at most {worst_da * 1000:.2f} m; off-orbit distance afterwards at most "
          f"{worst_off * 1000:.1f} m, {worst_off_rel * 100:.2f} % of the detour")
    check("D avoidance burn is the smallest safe one", not not_minimal,
          f"{n_plans} plans: 3 % less is unsafe and no direction of 2,000 does better" if not not_minimal
          else f"not minimal: {not_minimal}")

    debris = next((e for e in events if not movable(e)), None)
    if debris:
        result = maneuver.plan_conjunction(objs, sats, valid, debris["norad_1"], debris["norad_2"], debris["tca_ms"], not_before_ms)
        check("E debris-debris pair gets no plan", not result["possible"] and not result["plans"], result.get("reason", ""))

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
