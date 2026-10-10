"""Collision avoidance manoeuvre planning: the smallest impulsive burn that makes a conjunction safe,
checked against every other object so the new path does not create another close approach.

Model
-----
* Relative motion after a burn uses the Clohessy-Wiltshire (CW) equations about the satellite's own
  SGP4 trajectory: an impulse dv (radial, along-track, cross-track) at time t_b displaces the
  satellite at time t by Phi_rv(t - t_b) @ dv in its RTN frame. ml/verify_maneuvers.py compares this
  with numerical two-body + J2 propagation.
* At TCA the displacement moves the miss vector in the encounter plane. Pc after the burn uses the
  screener's assumed covariance; the burn must bring Pc down to PC_TARGET.
* Minimum burn: with W = C^-1/2 (combined covariance in the encounter plane) and A the map from dv to
  the change of the 2D miss vector, Pc <= PC_TARGET needs |W (m - A dv)| >= d_req. For each of 2,000
  burn directions u the smallest size s with |W (m - s A u)| = d_req is a quadratic root; the best
  direction wins (and the best one in the opposite sense is kept as an alternative). Several burn
  times (fractions of an orbit before TCA) are tried; candidates are ranked by |dv|.
* Interference check: from the burn until CHECK_ORBITS_AFTER_TCA orbits after TCA, the new path is
  screened against all objects (20 s steps, KD-tree, straight-line closest approach, exact refinement).
  A candidate is rejected if any approach has Pc >= PC_HIGH and is new or worse than without the burn.
  The next candidate is then tried; if none is safe the event gets a COLLISION WARNING.

Directions are reported as seen from the satellite facing its direction of travel with Earth below:
+T forward (prograde, speeds up), -T backward (retrograde), +N left, -N right, +R up, -R down.
"""

import math
import multiprocessing as mp
import os

import numpy as np
from scipy.spatial import cKDTree
from sgp4.api import SatrecArray

import live_features as L
from live_features import sc

PC_HIGH = 1e-6              # high-risk level (Kelvins / ML label threshold)
PC_TARGET = 1e-6            # the burn brings the event's Pc just below the high-risk level
MAX_DV_M_S = 1.0            # larger burns are treated as not possible
MIN_PREP_S = 15 * 60        # a burn needs at least 15 min of preparation after the assessment time
LEAD_ORBITS = (0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
CHECK_ORBITS_AFTER_TCA = 2.0
CHECK_STEP_S = 20.0
CHECK_SCREEN_KM = 5.0
MAX_CANDIDATES = 4          # alternatives tried when a burn interferes with other objects
DV_MARGIN = 1.05            # 5 % above the linear minimum: covers the CW linearisation (see verify_maneuvers.py)


def _fibonacci_sphere(k):
    """k nearly uniform unit vectors (burn directions in RTN)."""
    i = np.arange(k) + 0.5
    z = 1 - 2 * i / k
    phi = math.pi * (1 + 5 ** 0.5) * i
    rho = np.sqrt(1 - z * z)
    return np.column_stack([rho * np.cos(phi), rho * np.sin(phi), z])


_DIRS = _fibonacci_sphere(2000)


def _smallest_burn(wm, g, d_req):
    """For each direction (columns of g), the burn size s > 0 with |wm - s g| = d_req (inf if none)."""
    gg = np.einsum("ik,ik->k", g, g)
    wg = wm @ g
    disc = wg ** 2 + gg * (d_req ** 2 - wm @ wm)     # > 0 because |wm| < d_req
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (wg + np.sqrt(disc)) / gg
    return np.where(gg > 1e-12 * gg.max(), s, np.inf)


# ---------- Clohessy-Wiltshire relative motion ----------

def cw_phi_rv(n, tau):
    """Position response (RTN, km) to a unit impulse (RTN, km/s) after tau seconds: 3x3 matrix."""
    s, c = math.sin(n * tau), math.cos(n * tau)
    return np.array([
        [s / n, 2 * (1 - c) / n, 0.0],
        [2 * (c - 1) / n, (4 * s - 3 * n * tau) / n, 0.0],
        [0.0, 0.0, s / n],
    ])


def cw_displacement(n, tau, dv_rtn):
    """RTN displacement (km) and velocity change (km/s) at times tau (array, s) after an impulse dv_rtn."""
    tau = np.asarray(tau, float)
    s, c = np.sin(n * tau), np.cos(n * tau)
    vr, vt, vn = dv_rtn
    dr = np.stack([vr * s / n + 2 * vt * (1 - c) / n,
                   2 * vr * (c - 1) / n + vt * (4 * s - 3 * n * tau) / n,
                   vn * s / n], axis=-1)
    dvel = np.stack([vr * c + 2 * vt * s, -2 * vr * s + vt * (4 * c - 3), vn * c], axis=-1)
    return np.where((tau >= 0)[..., None], dr, 0.0), np.where((tau >= 0)[..., None], dvel, 0.0)


def rtn_bases(r, v):
    """RTN bases for arrays of states (N x 3) -> (N x 3 x 3), columns R, T, N."""
    rh = r / np.linalg.norm(r, axis=-1, keepdims=True)
    nh = np.cross(r, v)
    nh /= np.linalg.norm(nh, axis=-1, keepdims=True)
    th = np.cross(nh, rh)
    return np.stack([rh, th, nh], axis=-1)


def mean_motion(sat):
    return sat.no_kozai / 60.0   # rad/s


def period_s(sat):
    return 2 * math.pi / mean_motion(sat)


# ---------- minimum burn for one conjunction ----------

def required_mahalanobis(c2, hbr_km, pc_target):
    """Mahalanobis miss distance at which the small-body Pc approximation equals pc_target."""
    arg = hbr_km ** 2 / (2 * math.sqrt(np.linalg.det(c2)) * pc_target)
    return math.sqrt(2 * math.log(arg)) if arg > 1 else 0.0


def plan_candidates(ctx, man, other, t_tca, t_now, pc_target=PC_TARGET):
    """Burn options (smallest first) for satellite `man` to clear its conjunction with `other`.

    Returns (status, candidates): status 'ok', 'not_needed', 'too_late' or 'too_large'.
    """
    cat, jd0, fr0, hbr = ctx["cat"], ctx["jd0"], ctx["fr0"], ctx["hbr_km"]
    sat_m, sat_o = cat.sat(man), cat.sat(other)
    rm, vm = L.state_at(sat_m, jd0, fr0, t_tca)
    ro, vo = L.state_at(sat_o, jd0, fr0, t_tca)
    if rm is None or ro is None:
        return "too_late", []
    cov = (sc.covariance(rm, vm, L.element_age_days(sat_m, jd0, fr0, t_tca), cat.is_debris(man))
           + sc.covariance(ro, vo, L.element_age_days(sat_o, jd0, fr0, t_tca), cat.is_debris(other)))
    basis, miss2, c2 = L.encounter_plane(rm, vm, ro, vo, cov)
    d_req = required_mahalanobis(c2, hbr, pc_target) * 1.0005
    evals, evecs = np.linalg.eigh(c2)
    w = evecs @ np.diag(evals ** -0.5) @ evecs.T
    wm = w @ miss2
    if np.linalg.norm(wm) >= d_req:
        return "not_needed", []

    n, period = mean_motion(sat_m), period_s(sat_m)
    rtn_tca = sc.rtn_basis(rm, vm)
    cands = []
    for lead in LEAD_ORBITS:
        tau = lead * period
        t_burn = t_tca - tau
        if t_burn < t_now + MIN_PREP_S:
            continue
        # The secondary-minus-primary miss vector changes by minus the primary's displacement:
        # whitened miss after a burn s*u is wm - s * g(u), with g = W A u.
        g = w @ basis.T @ rtn_tca @ cw_phi_rv(n, tau) @ _DIRS.T         # 2 x K
        s = _smallest_burn(wm, g, d_req)
        best = int(np.argmin(s))
        if not np.isfinite(s[best]):
            continue
        cands.append({"lead_orbits": lead, "t_burn": t_burn, "dv_rtn": _DIRS[best] * s[best] * DV_MARGIN})
        # The best burn in the opposite sense (e.g. pass below instead of above) is an alternative.
        opposite = _DIRS @ _DIRS[best] < 0
        alt = np.flatnonzero(opposite)[int(np.argmin(s[opposite]))]
        if np.isfinite(s[alt]):
            cands.append({"lead_orbits": lead, "t_burn": t_burn, "dv_rtn": _DIRS[alt] * s[alt] * DV_MARGIN})
    if not cands:
        return "too_late", []
    cands.sort(key=lambda c: np.linalg.norm(c["dv_rtn"]))
    if np.linalg.norm(cands[0]["dv_rtn"]) * 1000 > MAX_DV_M_S:
        return "too_large", cands[:1]
    cands = [c for c in cands if np.linalg.norm(c["dv_rtn"]) * 1000 <= MAX_DV_M_S]
    return "ok", cands


def evaluate_at_tca(ctx, man, other, t_tca, cand):
    """Exact TCA, miss and Pc between `man` (with the burn) and `other`, refined with SGP4 states."""
    return refine_pair(ctx, man, other, t_tca, cand)


def refine_pair(ctx, man, other, t_guess, cand):
    """Iterate to the closest approach of `man` (displaced by the burn `cand`, or None) and `other`."""
    cat, jd0, fr0 = ctx["cat"], ctx["jd0"], ctx["fr0"]
    sat_m, sat_o = cat.sat(man), cat.sat(other)
    n = mean_motion(sat_m)

    def states(t):
        rm, vm = L.state_at(sat_m, jd0, fr0, t)
        ro, vo = L.state_at(sat_o, jd0, fr0, t)
        if rm is None or ro is None:
            return None
        if cand is not None:
            dr, dvel = cw_displacement(n, t - cand["t_burn"], cand["dv_rtn"])
            m = sc.rtn_basis(rm, vm)
            rm, vm = rm + m @ dr, vm + m @ dvel
        return rm, vm, ro, vo

    t = t_guess
    for _ in range(6):
        st = states(t)
        if st is None:
            return None
        rm, vm, ro, vo = st
        dr, dv = ro - rm, vo - vm
        tau = -dr @ dv / (dv @ dv)
        t += tau
        if abs(tau) < 1e-3:
            break
    st = states(t)
    if st is None:
        return None
    rm, vm, ro, vo = st
    cov_m = sc.covariance(rm, vm, L.element_age_days(sat_m, jd0, fr0, t), cat.is_debris(man))
    cov_o = sc.covariance(ro, vo, L.element_age_days(sat_o, jd0, fr0, t), cat.is_debris(other))
    pc = sc.collision_probability(rm, vm, ro, vo, cov_m, cov_o, ctx["hbr_km"])
    return {"t": t, "miss_km": float(np.linalg.norm(ro - rm)), "pc": pc,
            "rel_speed_km_s": float(np.linalg.norm(vo - vm))}


# ---------- interference check against all objects (parallel over time) ----------

_W = {}


def _init_worker(t0_iso, plans, max_rel):
    from datetime import datetime
    from sgp4.api import jday
    cat = L.Catalogue()
    t0 = datetime.fromisoformat(t0_iso)
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    valid_from, valid_to, _ = sc.load_validity()
    _W.update(cat=cat, jd0=jd0, fr0=fr0, max_rel=max_rel, array=SatrecArray(cat.sats),
              t0_ms=t0.timestamp() * 1000.0, valid_from=valid_from, valid_to=valid_to,
              plan_id=np.array([p["plan_id"] for p in plans]),
              man=np.array([p["man_idx"] for p in plans]),
              t_burn=np.array([p["t_burn"] for p in plans]),
              t_end=np.array([p["t_end"] for p in plans]),
              n=np.array([p["n"] for p in plans]),
              dv=np.array([p["dv_rtn"] for p in plans]).reshape(-1, 3))


def _cw_batch(n, tau, dv):
    """Vectorised cw_displacement for arrays of plans (n, tau: P; dv: P x 3)."""
    s, c = np.sin(n * tau), np.cos(n * tau)
    vr, vt, vn = dv[:, 0], dv[:, 1], dv[:, 2]
    dr = np.stack([vr * s / n + 2 * vt * (1 - c) / n,
                   2 * vr * (c - 1) / n + vt * (4 * s - 3 * n * tau) / n, vn * s / n], axis=1)
    dvel = np.stack([vr * c + 2 * vt * s, -2 * vr * s + vt * (4 * c - 3), vn * c], axis=1)
    after = (tau >= 0)[:, None]
    return np.where(after, dr, 0.0), np.where(after, dvel, 0.0)


def _screen_steps(k_range):
    """Close approaches of every planned path (with and without its burn) during grid steps k_range."""
    w = _W
    k_start, k_end = k_range
    dt = CHECK_STEP_S
    radius = w["max_rel"] * dt / 2 + 15.0
    found = []
    for start in range(k_start, k_end, 90):
        m = min(90, k_end - start)
        secs = (start + np.arange(m)) * dt
        err, r, v = w["array"].sgp4(np.full(m, w["jd0"]), w["fr0"] + secs / 86400.0)
        for k in range(m):
            t = secs[k]
            act = np.flatnonzero((w["t_burn"] - dt <= t) & (t <= w["t_end"] + dt))
            if not len(act):
                continue
            t_ms = w["t0_ms"] + t * 1000.0
            # Objects are only used inside their validity window, as in the screener.
            ok = (err[:, k] == 0) & (w["valid_from"] <= t_ms) & (t_ms <= w["valid_to"])
            pos = np.where(ok[:, None], r[:, k, :], 1e9 + np.arange(len(ok))[:, None])
            tree = cKDTree(pos)
            man = w["man"][act]
            rm, vm = r[man, k, :], v[man, k, :]
            disp, dvel = _cw_batch(w["n"][act], t - w["t_burn"][act], w["dv"][act])
            bases = rtn_bases(rm, vm)
            moved_r = rm + np.einsum("aij,aj->ai", bases, disp)
            moved_v = vm + np.einsum("aij,aj->ai", bases, dvel)
            for with_burn, q_r, q_v in ((1, moved_r, moved_v), (0, rm, vm)):
                lists = tree.query_ball_point(q_r, radius)
                counts = np.fromiter((len(x) for x in lists), int, len(lists))
                if not counts.sum():
                    continue
                a_idx = np.repeat(np.arange(len(act)), counts)
                j_idx = np.fromiter((j for x in lists for j in x), int, counts.sum())
                keep = j_idx != man[a_idx]
                a_idx, j_idx = a_idx[keep], j_idx[keep]
                dr = r[j_idx, k, :] - q_r[a_idx]
                dv = v[j_idx, k, :] - q_v[a_idx]
                dv2 = np.einsum("ij,ij->i", dv, dv)
                moving = dv2 > 1e-4
                a_idx, j_idx, dr, dv, dv2 = a_idx[moving], j_idx[moving], dr[moving], dv[moving], dv2[moving]
                tau = np.clip(-np.einsum("ij,ij->i", dr, dv) / dv2, -dt / 2, dt / 2)
                dmin = np.linalg.norm(dr + dv * tau[:, None], axis=1)
                near = dmin < CHECK_SCREEN_KM + 3
                for a, j, tt, d in zip(a_idx[near], j_idx[near], tau[near], dmin[near]):
                    found.append((int(w["plan_id"][act[a]]), bool(with_burn), int(j), float(t + tt), float(d)))
    return found


def check_interference(ctx, plans, workers=None):
    """For each plan, the approaches its burn creates or worsens (Pc >= PC_HIGH). Parallel over time."""
    if not plans:
        return {}
    t_lo = min(p["t_burn"] for p in plans)
    t_hi = max(p["t_end"] for p in plans)
    k_lo, k_hi = int(t_lo // CHECK_STEP_S), int(t_hi // CHECK_STEP_S) + 2
    workers = workers or max(1, min(os.cpu_count() - 1, 14))
    # Only steps covered by some plan, cut into pieces for the workers.
    covered = np.zeros(k_hi - k_lo, bool)
    for p in plans:
        covered[int(p["t_burn"] // CHECK_STEP_S) - k_lo: int(p["t_end"] // CHECK_STEP_S) - k_lo + 2] = True
    pieces, piece = [], 240
    for s in range(0, len(covered), piece):
        if covered[s:s + piece].any():
            pieces.append((k_lo + s, min(k_hi, k_lo + s + piece)))

    lean = [{k: p[k] for k in ("plan_id", "man_idx", "t_burn", "t_end", "n", "dv_rtn")} for p in plans]
    with mp.get_context("spawn").Pool(workers, initializer=_init_worker,
                                      initargs=(ctx["t0"].isoformat(), lean, sc.MAX_REL_SPEED_KM_S)) as pool:
        raw = [hit for chunk in pool.imap_unordered(_screen_steps, pieces) for hit in chunk]

    # Keep the closest coarse hit per (plan, burn?, object, encounter) and refine it exactly.
    by_plan = {p["plan_id"]: p for p in plans}
    best = {}
    for plan_id, with_burn, j, t, d in raw:
        key = (plan_id, with_burn, j, round(t / 120))
        if key not in best or d < best[key][1]:
            best[key] = (t, d)
    approaches = {}
    for (plan_id, with_burn, j, _), (t, d) in best.items():
        p = by_plan[plan_id]
        other = ctx["cat"].objs[j]["norad_id"]
        res = refine_pair(ctx, p["man"], other, t, p if with_burn else None)
        if res is None or res["miss_km"] >= CHECK_SCREEN_KM or not p["t_burn"] <= res["t"] <= p["t_end"]:
            continue
        approaches.setdefault((plan_id, other), {"with": [], "without": []})[
            "with" if with_burn else "without"].append(res)

    conflicts = {p["plan_id"]: [] for p in plans}
    for (plan_id, other), sides in approaches.items():
        p = by_plan[plan_id]
        for new in sides["with"]:
            if other == p["other"] and abs(new["t"] - p["t_tca"]) < 600:
                continue          # the conjunction being avoided is checked separately
            if new["pc"] < PC_HIGH:
                continue
            before = [b for b in sides["without"] if abs(b["t"] - new["t"]) < 300]
            pc_before = max((b["pc"] for b in before), default=0.0)
            if pc_before >= PC_HIGH and new["pc"] <= 1.5 * pc_before:
                continue          # an existing approach, not made worse by the burn
            conflicts[plan_id].append({"norad": other, "t": new["t"], "miss_km": new["miss_km"],
                                       "pc": new["pc"], "pc_before": pc_before})
    return conflicts


# ---------- presentation ----------

def describe_direction(dv_rtn_m_s):
    """Plain-language components of a burn: forward/backward, left/right, up/down."""
    r, t, n = dv_rtn_m_s
    parts = []
    for value, pos, neg in ((t, "Forward (prograde)", "Backward (retrograde)"),
                            (n, "Left", "Right"), (r, "Up (away from Earth)", "Down (towards Earth)")):
        parts.append({"axis": pos if value >= 0 else neg, "m_s": abs(value)})
    parts.sort(key=lambda x: -x["m_s"])
    total = math.sqrt(r * r + t * t + n * n)
    main = [p["axis"].split(" (")[0] for p in parts if p["m_s"] >= 0.25 * total]
    return " + ".join(main), parts
