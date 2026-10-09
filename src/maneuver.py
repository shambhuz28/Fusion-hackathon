"""Collision-avoidance manoeuvre planning for one conjunction.

The manoeuvring satellite's path is its SGP4 trajectory plus a small offset produced by impulsive burns,
propagated with the Clohessy-Wiltshire (Hill) equations in its radial / along-track / cross-track frame.
For burns of cm/s to m/s over a few orbits the linearisation error is metres, and the path stays
consistent with the SGP4 model used everywhere else.

A plan has three burns:
  1. avoidance burn before TCA (direction R, T or N, either sign), sized to give miss >= MIN_MISS_KM and
     Pc <= MAX_PC at TCA;
  2. + 3. a two-impulse return after the encounter. return_mode="orbit" (default) ends with zero radial and
     cross-track offset and zero relative velocity: the satellite is back on its original orbit, a constant
     distance ahead of or behind its original position (chosen to minimise delta-v). return_mode="slot" also
     removes that along-track offset (back in its original slot; costs more).

Candidates are tried cheapest first (total delta-v). The whole manoeuvred path is checked against every
nearby object; a candidate that brings it closer than SECONDARY_MIN_KM to any object, and closer than the
unmanoeuvred path was, is rejected. Debris and rocket bodies cannot manoeuvre.
"""

import math

import numpy as np
from scipy.optimize import minimize_scalar
from sgp4.api import SatrecArray

import screen_conjunctions as sc

JD_UNIX = 2440587.5           # Julian date of 1970-01-01T00:00Z: SGP4 time = JD_UNIX + ms / 86400000
MU_KM3_S2 = 398600.4418
MIN_MISS_KM = 1.0
MAX_PC = 1e-6
SECONDARY_MIN_KM = 2.0
DV_STEPS_MPS = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0]
DIRECTIONS = {"+T": (1, 1.0), "-T": (1, -1.0), "+R": (0, 1.0), "-R": (0, -1.0), "+N": (2, 1.0), "-N": (2, -1.0)}
DIRECTION_NAMES = {"+T": "along-track, prograde", "-T": "along-track, retrograde", "+R": "radial, outward",
                   "-R": "radial, inward", "+N": "cross-track, +normal", "-N": "cross-track, -normal"}
LEADS_PERIODS = [0.1, 0.25, 0.5, 1.0, 1.5, 2.0]  # burn this many orbits before TCA
RETURN_START_PERIODS = 0.25                        # first return burn this long after TCA
RETURN_TRANSFERS = [0.75, 1.25, 1.75, 2.25, 2.75]    # transfer time of the return, in orbits (longer = cheaper)
PATH_STEP_S = 20.0
MAX_SECONDARY_CHECKS = 6


def can_manoeuvre(obj):
    name = obj["name"].upper()
    return obj["group"] not in ("Debris", "Rocket body") and " DEB" not in name and "R/B" not in name


def states(sats, times_ms):
    """SGP4 positions/velocities (TEME, km, km/s) of sats at times_ms: arrays (n_sats, n_times, 3) + error flags."""
    t = np.asarray(times_ms, dtype=np.float64)
    err, r, v = SatrecArray(list(sats)).sgp4(np.full(t.size, JD_UNIX), t / 86400000.0)
    return r, v, err != 0


def rtn(r, v):
    rh = r / np.linalg.norm(r)
    nh = np.cross(r, v); nh /= np.linalg.norm(nh)
    return np.column_stack([rh, np.cross(nh, rh), nh])


def cw(state, n, t):
    """Clohessy-Wiltshire propagation of [x, y, z, vx, vy, vz] (radial, along, cross) by t seconds."""
    x, y, z, vx, vy, vz = state
    c, s = math.cos(n * t), math.sin(n * t)
    return np.array([
        (4 - 3 * c) * x + s / n * vx + 2 / n * (1 - c) * vy,
        6 * (s - n * t) * x + y + 2 / n * (c - 1) * vx + (4 * s - 3 * n * t) / n * vy,
        c * z + s / n * vz,
        3 * n * s * x + c * vx + 2 * s * vy,
        6 * n * (c - 1) * x - 2 * s * vx + (4 * c - 3) * vy,
        -n * s * z + c * vz,
    ])


def cw_rr_rv(n, t):
    c, s = math.cos(n * t), math.sin(n * t)
    rr = np.array([[4 - 3 * c, 0, 0], [6 * (s - n * t), 1, 0], [0, 0, c]])
    rv = np.array([[s / n, 2 / n * (1 - c), 0], [2 / n * (c - 1), (4 * s - 3 * n * t) / n, 0], [0, 0, s / n]])
    return rr, rv


class Plan:
    """Burn schedule in the manoeuvring satellite's Hill frame; offset(t) gives its displacement.

    Return options after the encounter (the cheapest valid one is kept):
      * single burn exactly k orbits after the avoidance burn: the CW motion is then back on the original
        orbit with the burn's velocity, so one opposite burn ends it (total = 2 x avoidance burn). For radial
        and cross-track burns this is also back in the original slot; for along-track burns a constant
        along-track offset remains;
      * two-impulse return (t2 -> t3) to the original orbit at the cheapest along-track position
        (return_mode "orbit") or exactly to the original slot ("slot").
    """

    def __init__(self, n, t1, dv1, tca, period, return_mode="orbit"):
        self.n, self.t1, self.dv1 = n, t1, np.asarray(dv1, dtype=float)
        start = np.r_[0.0, 0.0, 0.0, self.dv1]
        options = []

        # Single burn k orbits after t1, the first such time at least 0.1 orbit after TCA.
        k = max(1, math.ceil((tca + 0.1 * period - t1) / period))
        t2 = t1 + k * 2 * math.pi / n
        s2 = cw(start, n, t2 - t1)
        if abs(s2[0]) < 1e-9 and abs(s2[2]) < 1e-9 and (return_mode == "orbit" or abs(s2[1]) < 1e-6):
            options.append(([(t1, self.dv1, start), (t2, -s2[3:], np.r_[s2[:3], 0, 0, 0])], s2[1]))

        # Two-impulse returns: first burn RETURN_START_PERIODS after TCA, transfer of several lengths.
        t2 = tca + RETURN_START_PERIODS * period
        s2 = cw(start, n, t2 - t1)
        for transfer in RETURN_TRANSFERS:
            t3 = t2 + transfer * period
            rr, rv = cw_rr_rv(n, t3 - t2)

            def burns(y_end):
                v_needed = np.linalg.solve(rv, np.array([0.0, y_end, 0.0]) - rr @ s2[:3])
                return v_needed, v_needed - s2[3:], -cw(np.r_[s2[:3], v_needed], n, t3 - t2)[3:]

            if return_mode == "slot":
                y_end = 0.0
            else:   # the cost is convex in y_end; take the cheapest end position along the orbit
                y_end = float(minimize_scalar(lambda y: sum(np.linalg.norm(v) for v in burns(y)[1:]),
                                              bounds=(-2000.0, 2000.0), method="bounded", options={"xatol": 1e-4}).x)
            v_needed, dv2, dv3 = burns(y_end)
            options.append(([(t1, self.dv1, start), (t2, dv2, np.r_[s2[:3], v_needed]),
                             (t3, dv3, np.r_[0.0, y_end, 0.0, 0.0, 0.0, 0.0])], y_end))

        cost = lambda opt: sum(np.linalg.norm(dv) for _, dv, _ in opt[0])
        self.burns, self.y_end = min(options, key=cost)
        self.t3 = self.burns[-1][0]

    def total_dv(self):
        return float(sum(np.linalg.norm(dv) for _, dv, _ in self.burns))

    def offset(self, t):
        """Hill-frame [x, y, z, vx, vy, vz] (km, km/s) at time t (seconds since 1970)."""
        if t <= self.t1:
            return np.zeros(6)
        t0, _, state = max((b for b in self.burns if b[0] <= t), key=lambda b: b[0])
        return cw(state, self.n, t - t0)


def encounter(r_m, v_m, r_o, v_o):
    """Miss distance and time offset of the straight-line closest approach (short encounter)."""
    dr, dv = r_o - r_m, v_o - v_m
    tau = -(dr @ dv) / (dv @ dv)
    return float(np.linalg.norm(dr + dv * tau)), tau


def plan_for(objs, sats, valid, i_m, i_o, tca_ms, not_before_ms, hbr_km, return_mode="orbit"):
    """Best safe plan with object i_m manoeuvring to avoid object i_o; dict for the browser."""
    sat_m, sat_o = sats[i_m], sats[i_o]
    tca_s = tca_ms / 1000.0
    (rm, vm), (ro, vo) = [(r[0, 0], v[0, 0]) for r, v, _ in (states([sat_m], [tca_ms]), states([sat_o], [tca_ms]))]
    n = math.sqrt(MU_KM3_S2 / np.linalg.norm(rm) ** 3)       # mean motion of the (near-circular) orbit
    period = 2 * math.pi / n
    basis = rtn(rm, vm)
    age_m = (tca_s - (sat_m.jdsatepoch + sat_m.jdsatepochF - JD_UNIX) * 86400) / 86400
    age_o = (tca_s - (sat_o.jdsatepoch + sat_o.jdsatepochF - JD_UNIX) * 86400) / 86400
    cov_m = sc.covariance(rm, vm, age_m, objs[i_m]["group"] == "Debris")
    cov_o = sc.covariance(ro, vo, age_o, objs[i_o]["group"] == "Debris")
    miss0, _ = encounter(rm, vm, ro, vo)
    pc0 = sc.collision_probability(rm, vm, ro, vo, cov_m, cov_o, hbr_km)

    def outcome(plan):
        off = plan.offset(tca_s)
        r1, v1 = rm + basis @ off[:3], vm + basis @ off[3:]
        miss, tau = encounter(r1, v1, ro, vo)
        return miss, sc.collision_probability(r1, v1, ro, vo, cov_m, cov_o, hbr_km), tau

    candidates = []
    for lead in LEADS_PERIODS:
        t1 = tca_s - lead * period
        if t1 * 1000 < not_before_ms + 60000 or t1 * 1000 < valid[0][i_m]:
            continue
        for key, (axis, sign) in DIRECTIONS.items():
            for dv_mps in DV_STEPS_MPS:
                dv1 = np.zeros(3)
                dv1[axis] = sign * dv_mps / 1000.0
                best = Plan(n, t1, dv1, tca_s, period, return_mode)
                miss, pc, tau = outcome(best)
                if miss >= MIN_MISS_KM and pc <= MAX_PC:
                    candidates.append((best.total_dv(), key, dv_mps, lead, best, miss, pc, tca_ms + tau * 1000))
                    break   # larger delta-v in this direction only costs more
    candidates.sort(key=lambda c: c[0])
    base = {"manoeuvring": objs[i_m]["name"], "manoeuvring_norad": objs[i_m]["norad_id"],
            "other": objs[i_o]["name"], "other_norad": objs[i_o]["norad_id"],
            "miss_before_km": round(miss0, 4), "pc_before": float(f"{pc0:.4g}"), "period_min": round(period / 60, 2)}
    if not candidates:
        earliest_min = (tca_s * 1000 - max(not_before_ms + 60000, valid[0][i_m])) / 60000
        too_late = earliest_min < LEADS_PERIODS[0] * period / 60
        return {**base, "possible": False,
                "reason": (f"Too late: the earliest allowed burn is {max(earliest_min, 0):.0f} min before TCA, less than "
                           f"{LEADS_PERIODS[0]} orbit." if too_late else
                           f"No burn up to {DV_STEPS_MPS[-1]} m/s reaches miss >= {MIN_MISS_KM} km and Pc <= {MAX_PC:g}.")}

    rejected = []
    for total, key, dv_mps, lead, plan, miss, pc, new_tca_ms in candidates[:MAX_SECONDARY_CHECKS]:
        check = secondary_check(objs, sats, valid, i_m, plan, exclude=i_o)
        if check["new_conjunctions"]:
            rejected.append({"burn": f"{dv_mps} m/s {key}", "lead_orbits": lead, "creates": check["new_conjunctions"][:3]})
            continue
        return {**base, "possible": True, **describe(plan, key, dv_mps, lead, miss, pc),
                "tca_after_ms": int(round(new_tca_ms)), "secondary": check,
                "rejected": rejected, "path": path_samples(sat_m, plan)}
    return {**base, "possible": False, "rejected": rejected,
            "reason": f"Every candidate burn ({len(rejected)} cheapest checked) would create a new close approach "
                      f"(< {SECONDARY_MIN_KM} km) with another object."}


def offsets_eci(sat, plan, times_s):
    """ECI offsets (km, km/s) of the manoeuvred path from the SGP4 path at times_s."""
    r, v, _ = states([sat], np.asarray(times_s) * 1000)
    d_r, d_v = np.zeros((len(times_s), 3)), np.zeros((len(times_s), 3))
    for k, t in enumerate(times_s):
        off = plan.offset(t)
        if off.any():
            b = rtn(r[0, k], v[0, k])
            d_r[k], d_v[k] = b @ off[:3], b @ off[3:]
    return r[0], v[0], d_r, d_v


def secondary_check(objs, sats, valid, i_m, plan, exclude=None):
    """Closest approaches of the manoeuvred path (t1..t3) to every object whose altitude band overlaps it."""
    times = np.arange(plan.t1, plan.t3 + PATH_STEP_S, PATH_STEP_S)
    r0, v0, d_r, d_v = offsets_eci(sats[i_m], plan, times)
    rm, vm = r0 + d_r, v0 + d_v
    alt = np.linalg.norm(rm, axis=1) - 6378.137
    lo, hi = alt.min() - 30, alt.max() + 30
    t_ms = times * 1000
    idx = [k for k, s in enumerate(sats) if k != i_m
           and s.altp * s.radiusearthkm <= hi and s.alta * s.radiusearthkm >= lo
           and valid[0][k] <= t_ms[0] and valid[1][k] >= t_ms[-1]]
    found = []
    for chunk in range(0, len(idx), 4000):
        sub = idx[chunk:chunk + 4000]
        r, v, bad = states([sats[k] for k in sub], t_ms)
        for path, vel, label in ((rm, vm, "new"), (r0, v0, "old")):
            dr = r - path[None]
            dv = v - vel[None]
            dv2 = np.maximum(np.einsum("ijk,ijk->ij", dv, dv), 1e-12)
            tau = np.clip(-np.einsum("ijk,ijk->ij", dr, dv) / dv2, -PATH_STEP_S / 2, PATH_STEP_S / 2)
            d = np.linalg.norm(dr + dv * tau[..., None], axis=2)
            d[bad] = np.inf
            dmin = d.min(axis=1)
            if label == "new":
                new_min, new_arg = dmin, d.argmin(axis=1)
            else:
                old_min = dmin
        for j, k in enumerate(sub):
            if new_min[j] < SECONDARY_MIN_KM and new_min[j] < old_min[j] - 0.05 and k != exclude:
                found.append({"name": objs[k]["name"], "norad": objs[k]["norad_id"],
                              "min_km": round(float(new_min[j]), 3), "was_km": round(float(old_min[j]), 3),
                              "time_ms": int(t_ms[new_arg[j]])})
    found.sort(key=lambda f: f["min_km"])
    return {"objects_checked": len(idx), "new_conjunctions": found, "threshold_km": SECONDARY_MIN_KM}


def describe(plan, key, dv_mps, lead, miss, pc):
    labels = ["Avoidance burn"] + (["Return burn"] if len(plan.burns) == 2 else ["Return burn 1", "Return burn 2"])
    return {
        "direction": key, "direction_name": DIRECTION_NAMES[key], "lead_orbits": lead,
        "total_dv_mps": round(plan.total_dv() * 1000, 4),
        "burns": [{"label": label, "time_ms": int(round(t * 1000)),
                   "dv_rtn_mps": [round(float(x) * 1000, 4) for x in dv], "dv_mps": round(float(np.linalg.norm(dv)) * 1000, 4)}
                  for (t, dv, _), label in zip(plan.burns, labels)],
        "miss_after_km": round(miss, 4), "pc_after": float(f"{pc:.4g}"),
        "back_on_orbit_ms": int(round(plan.t3 * 1000)),
        # Along-track position on the original orbit afterwards: + ahead of, - behind the original slot.
        "slot_offset_km": round(float(plan.y_end), 3),
    }


def path_samples(sat, plan):
    """Offsets of the manoeuvred path from the SGP4 path, for drawing and animating it in the browser."""
    # One extra orbit after the last burn shows the satellite back on its original orbit (offset (0, y_end, 0)).
    times = np.arange(plan.t1, plan.t3 + 2 * math.pi / plan.n + PATH_STEP_S, PATH_STEP_S)
    _, _, d_r, _ = offsets_eci(sat, plan, times)
    return {"t0_ms": int(round(plan.t1 * 1000)), "step_ms": int(PATH_STEP_S * 1000),
            "offset_km": [[round(float(x), 5) for x in row] for row in d_r]}


def plan_conjunction(objs, sats, valid, n1, n2, tca_ms, not_before_ms, hbr_m=20.0, return_mode="orbit"):
    """Plans for each object of the pair that can manoeuvre; the cheapest safe one is recommended."""
    index = {o["norad_id"]: k for k, o in enumerate(objs)}
    if n1 not in index or n2 not in index:
        raise ValueError("unknown NORAD ID")
    a, b = index[n1], index[n2]
    # Refine the TCA from the given (millisecond-rounded) time with the screener's method.
    res = sc.refine(sats[a], sats[b], JD_UNIX, 0.0, tca_ms / 1000.0)
    if res:
        tca_ms = res[0] * 1000
    plans = [plan_for(objs, sats, valid, m, o, tca_ms, not_before_ms, hbr_m / 1000, return_mode)
             for m, o in ((a, b), (b, a)) if can_manoeuvre(objs[m])]
    if not plans:
        return {"possible": False, "plans": [], "tca_ms": int(tca_ms),
                "reason": "Neither object can manoeuvre (debris or rocket body): the collision risk remains."}
    plans.sort(key=lambda p: (not p["possible"], p.get("total_dv_mps", math.inf)))
    return {"possible": plans[0]["possible"], "plans": plans, "tca_ms": int(tca_ms), "return_mode": return_mode}
