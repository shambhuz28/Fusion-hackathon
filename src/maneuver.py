"""Collision-avoidance manoeuvre planning for one conjunction.

The manoeuvring satellite's path is its SGP4 trajectory plus a small offset produced by impulsive burns,
propagated with the Clohessy-Wiltshire (Hill) equations in its radial / along-track / cross-track frame.
For burns of cm/s to m/s over a few orbits the linearisation error is metres, and the path stays
consistent with the SGP4 model used everywhere else.

A plan has three burns:
  1. avoidance burn before TCA: the smallest burn that gives miss >= MIN_MISS_KM and Pc <= MAX_PC at TCA.
     Its offset at TCA is linear in the burn (CW), so for each burn time the cheapest burn that shifts the
     satellite to a given point of the encounter plane is the pseudo-inverse solution; the cheapest of
     these over the edge of the unsafe region is taken (see safe_shifts, cheapest_burn);
  2. + 3. a two-impulse return after the encounter. return_mode="orbit" (default) ends with zero radial and
     cross-track offset and zero relative velocity: the satellite is back on its original orbit, a constant
     distance ahead of or behind its original position (chosen to minimise delta-v). return_mode="slot" also
     removes that along-track offset (back in its original slot; costs more).

One candidate per burn time is built and they are tried cheapest first (total delta-v). The whole
manoeuvred path is checked against every nearby object; a candidate that brings it closer than
SECONDARY_MIN_KM to any object, and closer than the unmanoeuvred path was, is rejected. Debris and rocket
bodies cannot manoeuvre. Propellant is the rocket equation for an assumed satellite (TLEs carry no mass).
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
MAX_DV_MPS = 2.0                # largest avoidance burn considered
DV_TOLERANCE = 0.002            # relative width at which the bisection on the burn size stops
PLANE_ANGLES = 72               # encounter-plane directions in which the edge of the unsafe region is found
MAX_SHIFT_KM = 256.0            # largest shift of the miss vector considered
SHIFT_TOLERANCE = 0.01          # relative width at which the bisection on the shift stops
RECHECK_LEAD_GAP = 0.2          # orbits; a burn this close in time to a rejected one flies almost the same path
AXIS_NAMES = [("radial, outward", "radial, inward"), ("along-track, prograde", "along-track, retrograde"),
              ("cross-track, +normal", "cross-track, -normal")]
AXIS_KEYS = "RTN"
LEADS_PERIODS = [round(0.1 + 0.05 * k, 2) for k in range(39)]  # burn this many orbits before TCA (0.1 .. 2.0)
G0_M_S2 = 9.80665
# Propellant is reported for this satellite: TLEs give neither mass nor engine, so both are assumptions.
ASSUMED_MASS_KG = 500.0
ASSUMED_ISP_S = 220.0           # hydrazine monopropellant thruster
RETURN_START_PERIODS = 0.25                        # first return burn this long after TCA
RETURN_TRANSFERS = [0.75, 1.25, 1.75, 2.25, 2.75]    # transfer time of the return, in orbits (longer = cheaper)
PATH_STEP_S = 20.0
MAX_SECONDARY_CHECKS = 6
CLOSEST_LISTED = 3              # nearest other objects to the manoeuvred path that are reported


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


def bisect(safe, lo, hi, tolerance):
    """Smallest safe size in (lo, hi], given safe(hi) and that safety switches once on the way up."""
    while hi - lo > tolerance * hi:
        mid = 0.5 * (lo + hi)
        lo, hi = (lo, mid) if safe(mid) else (mid, hi)
    return hi


def safe_shifts(safe_at):
    """Edge of the unsafe region in the encounter plane: for each of PLANE_ANGLES unit directions e, the
    smallest shift s * e (km) of the manoeuvring satellite for which safe_at holds (None if there is none).

    Along a fixed direction the miss distance is convex in s and Pc falls off on both sides of its peak, so
    safety switches once and bisection finds the smallest safe s.
    """
    out = []
    for k in range(PLANE_ANGLES):
        angle = 2 * math.pi * k / PLANE_ANGLES
        e = np.array([math.cos(angle), math.sin(angle)])
        lo, hi = 0.0, MIN_MISS_KM
        while not safe_at(hi * e) and hi < MAX_SHIFT_KM:
            lo, hi = hi, 2 * hi
        found = hi < MAX_SHIFT_KM or safe_at(hi * e)
        out.append(bisect(lambda size: safe_at(size * e), lo, hi, SHIFT_TOLERANCE) * e if found else None)
    return out


def cheapest_burn(reach, plane, shifts, safe):
    """Smallest burn (km/s, Hill frame) at one burn time that makes the encounter safe, or None.

    reach (3x3) maps the burn to the Hill-frame offset at TCA and plane (2x3) projects that offset onto the
    encounter plane, so a = plane @ reach maps the burn to the shift of the satellite in that plane. The
    minimum-norm burn that produces a shift b is pinv(a) @ b, so the cheapest burn is the smallest of these
    over the edge of the unsafe region: the polygon through shifts, on each side of which the smallest burn
    has a closed form. Its size is then set by bisection on the full check, which also accounts for the
    small velocity change at TCA.
    """
    pinv = np.linalg.pinv(plane @ reach)
    burns = []
    for b1, b2 in zip(shifts, shifts[1:] + shifts[:1]):
        if b1 is None or b2 is None:
            continue
        p, d = pinv @ b1, pinv @ (b2 - b1)
        burns.append(p + min(1.0, max(0.0, -(p @ d) / max(d @ d, 1e-30))) * d)
    dv = min(burns, key=np.linalg.norm, default=None)
    max_dv = MAX_DV_MPS / 1000.0
    if dv is None or not 1e-12 < np.linalg.norm(dv) <= max_dv:
        return None
    size = float(np.linalg.norm(dv))
    u = dv / size
    hi = next((h for h in (min(1.2 * size, max_dv), max_dv) if safe(u * h)), None)
    return None if hi is None else u * bisect(lambda x: safe(u * x), 0.0, hi, DV_TOLERANCE)


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

    base = {"manoeuvring": objs[i_m]["name"], "manoeuvring_norad": objs[i_m]["norad_id"],
            "other": objs[i_o]["name"], "other_norad": objs[i_o]["norad_id"],
            "miss_before_km": round(miss0, 4), "pc_before": float(f"{pc0:.4g}"), "period_min": round(period / 60, 2)}
    if miss0 >= MIN_MISS_KM and pc0 <= MAX_PC:
        return {**base, "possible": False, "not_needed": True,
                "reason": f"Already safe: miss >= {MIN_MISS_KM} km and Pc <= {MAX_PC:g} without a burn."}

    def outcome(off):
        """Miss distance, Pc and TCA shift for a Hill-frame offset [x, y, z, vx, vy, vz] at TCA."""
        r1, v1 = rm + basis @ off[:3], vm + basis @ off[3:]
        miss, tau = encounter(r1, v1, ro, vo)
        return miss, sc.collision_probability(r1, v1, ro, vo, cov_m, cov_o, hbr_km), tau

    # Encounter plane (perpendicular to the relative velocity), expressed in the Hill frame.
    w = (vo - vm) / np.linalg.norm(vo - vm)
    e1 = np.cross(w, basis[:, 0]); e1 /= np.linalg.norm(e1)
    plane = np.vstack([e1, np.cross(w, e1)]) @ basis

    def safe_offset(off):
        r1, v1 = rm + basis @ off[:3], vm + basis @ off[3:]
        return (encounter(r1, v1, ro, vo)[0] >= MIN_MISS_KM
                and sc.collision_probability(r1, v1, ro, vo, cov_m, cov_o, hbr_km) <= MAX_PC)

    shifts = safe_shifts(lambda b: safe_offset(np.r_[plane.T @ b, 0.0, 0.0, 0.0]))
    candidates = []
    for lead in LEADS_PERIODS:
        t1 = tca_s - lead * period
        if t1 * 1000 < not_before_ms + 60000 or t1 * 1000 < valid[0][i_m]:
            continue

        dv1 = cheapest_burn(cw_rr_rv(n, tca_s - t1)[1], plane, shifts,
                            lambda dv: safe_offset(cw(np.r_[0.0, 0.0, 0.0, dv], n, tca_s - t1)))
        if dv1 is None:
            continue
        plan = Plan(n, t1, dv1, tca_s, period, return_mode)
        miss, pc, tau = outcome(plan.offset(tca_s))
        candidates.append((plan.total_dv(), lead, plan, miss, pc, tca_ms + tau * 1000))
    candidates.sort(key=lambda c: c[0])
    if not candidates:
        earliest_min = (tca_s * 1000 - max(not_before_ms + 60000, valid[0][i_m])) / 60000
        too_late = earliest_min < LEADS_PERIODS[0] * period / 60
        return {**base, "possible": False,
                "reason": (f"Too late: the earliest allowed burn is {max(earliest_min, 0):.0f} min before TCA, less than "
                           f"{LEADS_PERIODS[0]} orbit." if too_late else
                           f"No burn up to {MAX_DV_MPS} m/s reaches miss >= {MIN_MISS_KM} km and Pc <= {MAX_PC:g}.")}

    rejected = []
    for total, lead, plan, miss, pc, new_tca_ms in candidates:
        if len(rejected) == MAX_SECONDARY_CHECKS:
            break
        if any(abs(lead - r["lead_orbits"]) < RECHECK_LEAD_GAP for r in rejected):
            continue
        check = secondary_check(objs, sats, valid, i_m, plan, exclude=i_o)
        if check["new_conjunctions"]:
            rejected.append({"burn": f"{np.linalg.norm(plan.dv1) * 1000:.3f} m/s {direction_of(plan.dv1)[0]}",
                             "lead_orbits": lead, "creates": check["new_conjunctions"][:3]})
            continue
        return {**base, "possible": True, **describe(plan, lead, miss, pc),
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
    """Closest approaches of the manoeuvred path (t1..t3) to every object whose altitude band overlaps it.

    "new_conjunctions" are the approaches the manoeuvre would cause; "closest" are the nearest objects
    whatever their distance was before, shown as evidence of what the detour passes.
    """
    times = np.arange(plan.t1, plan.t3 + PATH_STEP_S, PATH_STEP_S)
    r0, v0, d_r, d_v = offsets_eci(sats[i_m], plan, times)
    rm, vm = r0 + d_r, v0 + d_v
    alt = np.linalg.norm(rm, axis=1) - 6378.137
    lo, hi = alt.min() - 30, alt.max() + 30
    t_ms = times * 1000
    idx = [k for k, s in enumerate(sats) if k != i_m
           and s.altp * s.radiusearthkm <= hi and s.alta * s.radiusearthkm >= lo
           and valid[0][k] <= t_ms[0] and valid[1][k] >= t_ms[-1]]
    found, nearest = [], []
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
        for j in np.argsort(new_min)[:CLOSEST_LISTED + 1]:
            if sub[j] != exclude and np.isfinite(new_min[j]):
                nearest.append((float(new_min[j]), float(old_min[j]), sub[j], int(t_ms[new_arg[j]])))
    found.sort(key=lambda f: f["min_km"])
    closest = [{"name": objs[k]["name"], "norad": objs[k]["norad_id"], "min_km": round(new, 3),
                "was_km": round(old, 3), "time_ms": t} for new, old, k, t in sorted(nearest)[:CLOSEST_LISTED]]
    return {"objects_checked": len(idx), "new_conjunctions": found, "threshold_km": SECONDARY_MIN_KM,
            "closest": closest}


def direction_of(dv):
    """Key ("+T", "-R", ...) of the largest RTN component, and a name listing every component over 20 %."""
    order = sorted(range(3), key=lambda k: -abs(dv[k]))
    name = " + ".join(AXIS_NAMES[k][int(dv[k] < 0)] for k in order if abs(dv[k]) >= 0.2 * np.linalg.norm(dv))
    return ("-" if dv[order[0]] < 0 else "+") + AXIS_KEYS[order[0]], name


def propellant_kg(dv_km_s):
    """Tsiolkovsky rocket equation for the assumed satellite."""
    return ASSUMED_MASS_KG * (1 - math.exp(-dv_km_s * 1000 / (ASSUMED_ISP_S * G0_M_S2)))


def describe(plan, lead, miss, pc):
    key, name = direction_of(plan.dv1)
    labels = ["Avoidance burn"] + (["Return burn"] if len(plan.burns) == 2 else ["Return burn 1", "Return burn 2"])
    return {
        "direction": key, "direction_name": name, "lead_orbits": lead,
        "total_dv_mps": round(plan.total_dv() * 1000, 4),
        "propellant_kg": round(propellant_kg(plan.total_dv()), 5),
        "assumed_mass_kg": ASSUMED_MASS_KG, "assumed_isp_s": ASSUMED_ISP_S,
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
