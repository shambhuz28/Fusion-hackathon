"""Independent checks of the avoidance burns in app/data/risk_assessment.json.

  A. Linear (Clohessy-Wiltshire) displacement at TCA vs numerical two-body + J2 propagation of the
     burn, and the Pc reached when the numerically propagated displacement is used instead.
  B. Minimality: a burn 10 % smaller must NOT reach the target Pc in at least 99 % of cases (the planned
     burn is the linear minimum plus a 5 % margin, so it is only what is needed).
  C. Interference: brute-force screening of the new path against every object at 2 s steps over the
     checked window; no approach with Pc >= 1e-6 may have been missed (new or worsened ones).

Usage:
    python ml/verify_maneuvers.py [--samples 200] [--brute 4]
"""

import argparse
import json
import random
import sys
import time

import numpy as np
from sgp4.api import SatrecArray

import live_features as L
import maneuver as M
from live_features import sc

J2, RE, MU = 1.08262668e-3, 6378.137, 398600.4418


def accel(r):
    """Two-body + J2 acceleration (km/s^2) for states N x 3."""
    x, y, z = r[:, 0], r[:, 1], r[:, 2]
    r2 = np.einsum("ij,ij->i", r, r)
    rn = np.sqrt(r2)
    k = 1.5 * J2 * MU * RE ** 2 / rn ** 5
    zz = 5 * z * z / r2
    a = -MU * r / rn[:, None] ** 3
    a[:, 0] += k * x * (zz - 1)
    a[:, 1] += k * y * (zz - 1)
    a[:, 2] += k * z * (zz - 3)
    return a


def propagate(r, v, duration, step=5.0):
    """RK4 two-body + J2 for several states at once (N x 3) over `duration` seconds."""
    n = max(1, int(np.ceil(duration / step)))
    h = duration / n
    for _ in range(n):
        k1v, k1r = accel(r), v
        k2v, k2r = accel(r + h / 2 * k1r), v + h / 2 * k1v
        k3v, k3r = accel(r + h / 2 * k2r), v + h / 2 * k2v
        k4v, k4r = accel(r + h * k3r), v + h * k3v
        r = r + h / 6 * (k1r + 2 * k2r + 2 * k3r + k4r)
        v = v + h / 6 * (k1v + 2 * k2v + 2 * k3v + k4v)
    return r, v


def pc_with_displacement(ctx, plan, disp_teme):
    """Pc at the original TCA after shifting the manoeuvring satellite by disp_teme (km)."""
    cat, jd0, fr0 = ctx["cat"], ctx["jd0"], ctx["fr0"]
    man, other, t = plan["maneuver_norad"], plan["other_norad"], plan["t_tca"]
    best = None
    for _ in range(4):   # re-centre on the new closest approach (shift is tiny vs. the closing speed)
        rm, vm = L.state_at(cat.sat(man), jd0, fr0, t)
        ro, vo = L.state_at(cat.sat(other), jd0, fr0, t)
        rm = rm + disp_teme
        dr, dv = ro - rm, vo - vm
        t += -dr @ dv / (dv @ dv)
        best = (rm, vm, ro, vo, t)
    rm, vm, ro, vo, t = best
    cov = [sc.covariance(rm, vm, L.element_age_days(cat.sat(man), jd0, fr0, t), cat.is_debris(man)),
           sc.covariance(ro, vo, L.element_age_days(cat.sat(other), jd0, fr0, t), cat.is_debris(other))]
    return sc.collision_probability(rm, vm, ro, vo, *cov, ctx["hbr_km"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--brute", type=int, default=4, help="plans checked by brute force (C)")
    args = parser.parse_args()
    random.seed(1)

    cat = L.Catalogue()
    data, events, t0, jd0, fr0 = L.load_screening()
    ctx = {"cat": cat, "jd0": jd0, "fr0": fr0, "t0": t0, "hbr_km": data["hbr_m"] / 1000.0}
    risk = json.loads((L.ROOT / "app" / "data" / "risk_assessment.json").read_text())
    target = risk["maneuver"]["pc_target"]
    plans = []
    for key, m in risk["maneuvers"].items():
        if m["status"] != "ok":
            continue
        tca_ms = int(key.split(":")[0])
        m = dict(m, t_tca=tca_ms / 1000 - t0.timestamp(), t_burn=m["burn_ms"] / 1000 - t0.timestamp(),
                 dv=np.array(m["dv_rtn_m_s"]) / 1000.0)
        plans.append(m)
    sample = random.sample(plans, min(args.samples, len(plans)))
    print(f"{len(plans)} planned burns; checking {len(sample)}")
    results = []

    # A. CW vs numerical J2, and B. minimality
    t = time.time()
    rel_err, pcs_num, not_minimal = [], [], 0
    for p in sample:
        sat = cat.sat(p["maneuver_norad"])
        rb, vb = L.state_at(sat, jd0, fr0, p["t_burn"])
        basis_b = sc.rtn_basis(rb, vb)
        r0 = np.array([rb, rb]); v0 = np.array([vb, vb + basis_b @ p["dv"]])
        rr, _ = propagate(r0, v0, p["t_tca"] - p["t_burn"])
        disp_num = rr[1] - rr[0]
        rt, vt = L.state_at(sat, jd0, fr0, p["t_tca"])
        disp_cw = sc.rtn_basis(rt, vt) @ M.cw_displacement(M.mean_motion(sat), p["t_tca"] - p["t_burn"], p["dv"])[0]
        rel_err.append(np.linalg.norm(disp_num - disp_cw) / np.linalg.norm(disp_num))
        pcs_num.append(pc_with_displacement(ctx, p, disp_num))
        if pc_with_displacement(ctx, p, 0.9 * disp_cw) <= target:
            not_minimal += 1
    rel_err, pcs_num = np.array(rel_err), np.array(pcs_num)
    ok_a = np.median(rel_err) < 0.02 and np.mean(pcs_num <= 2 * target) > 0.95
    results.append(ok_a)
    print(f"[{'PASS' if ok_a else 'FAIL'}] A. CW vs J2 displacement at TCA: median error {100 * np.median(rel_err):.2f} %, "
          f"95th pct {100 * np.percentile(rel_err, 95):.2f} %, max {100 * rel_err.max():.2f} %; "
          f"Pc with the J2 displacement <= target for {100 * np.mean(pcs_num <= target):.0f} % "
          f"(<= 2x target {100 * np.mean(pcs_num <= 2 * target):.0f} %), max {pcs_num.max():.2e} "
          f"({time.time() - t:.0f} s)")
    ok_b = not_minimal <= 0.01 * len(sample)
    results.append(ok_b)
    print(f"[{'PASS' if ok_b else 'FAIL'}] B. Minimal burn: a burn 10 % smaller still reaches the target for "
          f"{not_minimal} of {len(sample)} (allowed: 1 %)")

    # C. brute-force interference check at 2 s steps
    t = time.time()
    arr = SatrecArray(cat.sats)
    missed = 0
    checked = 0
    for p in random.sample(plans, min(args.brute, len(plans))):
        man_idx = cat.index[p["maneuver_norad"]]
        sat = cat.sat(p["maneuver_norad"])
        n = M.mean_motion(sat)
        t_end = p["checked_until_ms"] / 1000 - t0.timestamp()
        secs = np.arange(p["t_burn"], t_end, 2.0)
        worst = {}
        for start in range(0, len(secs), 150):
            s = secs[start:start + 150]
            err, r, v = arr.sgp4(np.full(len(s), jd0), fr0 + s / 86400.0)
            disp, dvel = M.cw_displacement(n, s - p["t_burn"], p["dv"])
            b = M.rtn_bases(r[man_idx], v[man_idx])
            new_r = r[man_idx] + np.einsum("kij,kj->ki", b, disp)
            new_v = v[man_idx] + np.einsum("kij,kj->ki", b, dvel)
            d = np.linalg.norm(r - new_r[None], axis=2)
            d[man_idx] = np.inf
            d[err != 0] = np.inf
            for j, k in zip(*np.nonzero(d < 3.0)):
                worst[(j, round(s[k] / 300))] = min(worst.get((j, round(s[k] / 300)), (np.inf,)), (d[j, k], s[k]))
        for (j, _), (_, tk) in worst.items():
            other = cat.objs[j]["norad_id"]
            new = M.refine_pair(ctx, p["maneuver_norad"], other, float(tk),
                                {"t_burn": p["t_burn"], "dv_rtn": p["dv"]})
            if new is None or new["pc"] < M.PC_HIGH:
                continue
            if other == p["other_norad"] and abs(new["t"] - p["t_tca"]) < 600:
                continue
            base = M.refine_pair(ctx, p["maneuver_norad"], other, float(tk), None)
            if base and base["pc"] >= M.PC_HIGH and new["pc"] <= 1.5 * base["pc"]:
                continue
            missed += 1
            print(f"    missed: {p['maneuver_name']} vs {cat.objs[j]['name']} miss {new['miss_km']:.3f} km Pc {new['pc']:.2e}")
        checked += 1
    ok_c = missed == 0
    results.append(ok_c)
    print(f"[{'PASS' if ok_c else 'FAIL'}] C. Brute-force 2 s interference screening of {checked} burns vs all "
          f"{len(cat.sats)} objects: {missed} approaches with Pc >= {M.PC_HIGH:g} missed by the planner "
          f"({time.time() - t:.0f} s)")
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
