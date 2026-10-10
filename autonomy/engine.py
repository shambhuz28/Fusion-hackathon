"""Decentralised collision avoidance: satellites share their orbit data over inter-satellite links.

Every active satellite broadcasts a message every STEP_S seconds. Satellites within LINK_KM of each other
(a 100 km diameter bubble) receive each other's messages:

    position (and altitude), velocity, orbital elements (= its predicted path), fuel level, position uncertainty

Debris and rocket bodies have no radio: the network cannot see them (reported in the summary).

A receiver assesses new data (a pair it has not heard in the last RECHECK_MIN minutes): it predicts the closest
approach of the sender to itself over the next HORIZON_H hours. If the encounter is safe it discards the data;
if not (Pc >= PC_KEEP with the two broadcast uncertainties, or miss < MISS_KEEP_KM) it keeps it as a threat. A kept
threat is monitored; it is avoided when the satellites can collide: Pc >= PC_MANOEUVRE or miss < MISS_KEEP_KM.
Both satellites hold the same data, so they agree without further messages on who manoeuvres. Each one's detour is
planned: its velocity and direction relative to the encounter set the delta-v it needs, its mass and thruster turn
that into propellant (rocket equation), and the one for which the detour costs the smallest share of its remaining
fuel manoeuvres (ties: lower NORAD ID). A satellite that would drop below its fuel reserve cannot; if neither can,
the threat stays a warning.
The manoeuvre is a temporary detour that rejoins the original orbit right after the encounter (src/maneuver.py),
and its safety check only uses objects the manoeuvring satellite has itself heard from.

The 24 h prediction for a pair is the same SGP4 closest-approach computation as src/screen_conjunctions.py on
the same elements the sender broadcasts, so it is looked up from that verified screening (app/data/
conjunctions.json) instead of being recomputed for each of ~10^6 contacts; the receiver's risk decision is
recomputed here with the broadcast uncertainties.

Fuel levels and uncertainties are simulated (TLEs carry neither), seeded by NORAD ID so runs are repeatable.

Usage:
    python autonomy/engine.py [--hours 6] [--start ISO] [--link-km 50]
Writes autonomy/data/run.json, played by autonomy/index.html.
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import _venv  # noqa: E402,F401  (re-runs with .venv/bin/python if packages are missing)

import numpy as np  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402
from sgp4.api import SatrecArray  # noqa: E402

import maneuver  # noqa: E402
import screen_conjunctions as sc  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "run.json"
JD_UNIX = maneuver.JD_UNIX

STEP_S = 10.0                 # broadcast interval and simulation step
LINK_KM = 50.0                # radio range: a 100 km diameter bubble around each satellite
RECHECK_MIN = 60.0            # data from the same neighbour is re-assessed at most this often
HORIZON_H = 24.0              # how far ahead a receiver predicts the encounter with the sender
PC_KEEP = 1e-5                # keep the data (threat, monitored) if Pc reaches this ...
PC_MANOEUVRE = 1e-4           # ... and avoid it with a temporary detour if Pc reaches this ...
MISS_KEEP_KM = 0.2            # ... or the predicted miss is this small
FUEL_RESERVE = 0.05           # a satellite below 5 % fuel does not manoeuvre
PROPELLANT_FRACTION = 0.05    # tank capacity as a share of the satellite's mass (assumed)
G0_M_S2 = 9.80665
SIGMA_RANGE_M = (30.0, 150.0)  # broadcast 1-sigma radial/cross-track position uncertainty (GNSS-class)
SIGMA_ALONG_FACTOR = 2.5      # along-track uncertainty is larger
HBR_KM = 0.02
PROCESSING_S = 60.0           # time from receiving the data to the earliest possible burn
COVERAGE_MIN_WARNING_MIN = 10.0


def attributes(norad):
    """Simulated fuel fraction and broadcast position uncertainty (km), repeatable per satellite."""
    rng = np.random.default_rng(norad)
    fuel = float(rng.choice([rng.uniform(0.0, FUEL_RESERVE), rng.uniform(0.15, 1.0)], p=[0.08, 0.92]))
    sigma = float(rng.uniform(*SIGMA_RANGE_M)) / 1000.0
    return fuel, sigma


def physical(obj):
    """Assumed mass (kg) and thruster specific impulse (s): TLEs carry neither, so these are per type."""
    norad, group = obj["norad_id"], obj["group"]
    if norad == 25544:
        return 420000.0, 300.0                                 # ISS
    if norad == 48274:
        return 100000.0, 300.0                                 # Tiangong
    if group == "Starlink":
        return (300.0 if norad < 56000 else 800.0), 1500.0      # v1.x / v2 mini, Hall-effect thrusters
    if group in ("OneWeb", "Kuiper"):
        return (150.0 if group == "OneWeb" else 600.0), 1500.0  # electric propulsion
    if group in ("Qianfan", "Guowang"):
        return (300.0 if group == "Qianfan" else 500.0), 220.0
    if group == "Iridium":
        return 860.0, 220.0
    if group in ("Planet", "Spire"):
        return 5.0, 70.0                                       # cubesats, cold gas
    rng = np.random.default_rng(norad + 7)
    mass = float(math.exp(rng.uniform(math.log(5.0), math.log(1500.0))))
    return mass, (70.0 if mass < 50 else 220.0)


def propellant_kg(mass_kg, isp_s, dv_mps):
    """Tsiolkovsky rocket equation."""
    return mass_kg * (1.0 - math.exp(-dv_mps / (isp_s * G0_M_S2)))


def covariance(r, v, sigma_km):
    b = sc.rtn_basis(r, v)
    return b @ np.diag([sigma_km, SIGMA_ALONG_FACTOR * sigma_km, sigma_km]) ** 2 @ b.T


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=6.0, help="simulated duration (default 6)")
    parser.add_argument("--start", help="ISO 8601 UTC start (default: start of the screened window)")
    parser.add_argument("--link-km", type=float, default=LINK_KM, help="radio range in km (default 50 = 100 km bubble)")
    args = parser.parse_args()
    started = time.time()

    objs, sats = sc.load_objects()
    valid_from, valid_to, _ = sc.load_validity()
    conj = json.loads((sc.APP_DATA / "conjunctions.json").read_text())
    window_start = datetime.fromisoformat(conj["window_start_utc"].replace("Z", "+00:00")).timestamp() * 1000
    window_end = window_start + conj["window_hours"] * 3600e3
    t0 = (datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp() * 1000 if args.start else window_start)
    t_end = t0 + args.hours * 3600e3
    if not window_start <= t0 < window_end:
        raise SystemExit("The start must lie inside the screened window of app/data/conjunctions.json.")

    # Radio-equipped satellites; everything else is silent.
    active = [k for k, o in enumerate(objs) if maneuver.can_manoeuvre(o)]
    norad = [objs[k]["norad_id"] for k in active]
    local = {k: j for j, k in enumerate(active)}
    fuel, sigma = map(np.array, zip(*(attributes(n) for n in norad)))
    fuel_start = fuel.copy()
    mass, isp = map(np.array, zip(*(physical(objs[k]) for k in active)))
    capacity = PROPELLANT_FRACTION * mass                      # kg of propellant when full
    n_act = len(active)

    # Screened encounters per pair (both objects active), for the receiver's prediction.
    fields = conj["fields"]
    pair_events = defaultdict(list)
    index = {o["norad_id"]: k for k, o in enumerate(objs)}
    for row in conj["rows"]:
        e = dict(zip(fields, row))
        a, b = index.get(e["norad_1"]), index.get(e["norad_2"])
        if a in local and b in local:
            pair_events[frozenset((local[a], local[b]))].append(e)
    for evs in pair_events.values():
        evs.sort(key=lambda e: e["tca_ms"])

    steps = np.arange(t0, t_end + 1, STEP_S * 1000)
    per_minute = defaultdict(lambda: [0, 0, 0, 0, 0])   # minute -> contacts, messages, assessed, discarded, threats
    last_check, heard = {}, defaultdict(set)
    prev = set()
    threats, feed, discarded_samples = [], [], []
    threat_pairs = {}
    totals = defaultdict(int)
    arr = SatrecArray([sats[k] for k in active])
    vf = np.array([valid_from[k] for k in active]); vt = np.array([valid_to[k] for k in active])

    def assess(a, b, t_ms, dist):
        """Receiver-side check of the pair (a, b) with data received at t_ms; returns True if kept."""
        upcoming = [e for e in pair_events.get(frozenset((a, b)), ())
                    if t_ms < e["tca_ms"] <= min(t_ms + HORIZON_H * 3600e3, window_end)]
        if not upcoming:
            return False
        e = upcoming[0]
        ga, gb = active[a], active[b]
        res = sc.refine(sats[ga], sats[gb], JD_UNIX, 0.0, e["tca_ms"] / 1000)
        if not res:
            return False
        tca_s, ra, va, rb, vb = res
        miss = float(np.linalg.norm(rb - ra))
        pc = sc.collision_probability(ra, va, rb, vb, covariance(ra, va, sigma[a]), covariance(rb, vb, sigma[b]), HBR_KM)
        key = frozenset((a, b))
        if pc < PC_KEEP and miss >= MISS_KEEP_KM:
            if len(discarded_samples) < 200:
                discarded_samples.append({"t_ms": int(t_ms), "a": norad[a], "b": norad[b], "miss_km": round(miss, 3),
                                          "pc_onboard": float(f"{pc:.3g}"), "pc_ground": e["pc"], "tca_ms": e["tca_ms"]})
            return False
        if key in threat_pairs:
            return True
        threat_pairs[key] = len(threats)
        decide(a, b, t_ms, dist, tca_s * 1000, miss, pc, e,
               {a: float(np.linalg.norm(va)), b: float(np.linalg.norm(vb))})
        return True

    def decide(a, b, t_ms, dist, tca_ms, miss, pc, e, speed):
        """Both satellites hold the same data and apply the same rule: each one's detour is planned, and the one
        for which it costs the smallest share of its remaining fuel manoeuvres."""
        rec = {"id": len(threats), "detected_ms": int(t_ms), "detected_distance_km": round(dist, 2),
               "a": norad[a], "b": norad[b], "tca_ms": int(round(tca_ms)), "miss_km": round(miss, 4),
               "pc_onboard": float(f"{pc:.4g}"), "pc_ground": e["pc"], "risk_ground": e["risk"],
               "rel_speed_km_s": e["rel_speed_km_s"], "fuel_before": {str(norad[a]): round(float(fuel[a]), 3),
                                                                     str(norad[b]): round(float(fuel[b]), 3)},
               "warning_min": round((tca_ms - t_ms) / 60000, 1)}
        if pc < PC_MANOEUVRE and miss >= MISS_KEEP_KM:
            rec.update({"status": "monitor", "options": [],
                        "reason": f"Pc {pc:.1e} is below the {PC_MANOEUVRE:g} manoeuvre threshold: kept and watched"})
            threats.append(rec)
            return

        options = []
        for m in (a, b):
            o = b if m == a else a
            fuel_kg, reserve_kg = fuel[m] * capacity[m], FUEL_RESERVE * capacity[m]
            opt = {"norad": norad[m], "mass_kg": round(float(mass[m]), 1), "isp_s": float(isp[m]),
                   "speed_km_s": round(speed[m], 3), "fuel_fraction": round(float(fuel[m]), 3),
                   "fuel_kg": round(float(fuel_kg), 3), "feasible": False}
            options.append(opt)
            if fuel[m] < FUEL_RESERVE:
                opt["why"] = f"fuel {fuel[m] * 100:.0f} % is below its {FUEL_RESERVE * 100:.0f} % reserve"
                continue
            # Onboard knowledge: itself, the other satellite and every satellite it has heard from.
            subset = [active[m], active[o]] + [active[k] for k in sorted(heard[m] - {o})]
            sub_valid = (np.array([valid_from[k] for k in subset]), np.array([valid_to[k] for k in subset]))
            plan = maneuver.plan_for([objs[k] for k in subset], [sats[k] for k in subset], sub_valid, 0, 1,
                                     tca_ms, t_ms + PROCESSING_S * 1000, HBR_KM)
            if not plan["possible"]:
                opt["why"] = plan["reason"]
                continue
            prop = propellant_kg(mass[m], isp[m], plan["total_dv_mps"])
            plan["known_objects_checked"] = len(subset) - 1
            opt.update({"dv_mps": plan["total_dv_mps"], "propellant_kg": round(prop, 4),
                        "share_of_fuel": round(prop / fuel_kg, 5), "plan": plan})
            if fuel_kg - prop < reserve_kg:
                opt["why"] = f"needs {prop:.2f} kg of its {fuel_kg:.2f} kg, leaving less than the reserve"
                continue
            opt["feasible"] = True

        feasible = sorted((o for o in options if o["feasible"]), key=lambda o: (o["share_of_fuel"], o["norad"]))
        if feasible:
            best = feasible[0]
            m = a if best["norad"] == norad[a] else b
            fuel[m] -= best["propellant_kg"] / capacity[m]
            rec.update({"status": "avoided", "manoeuvring": best["norad"], "plan": best["plan"],
                        "fuel_used_fraction": round(best["propellant_kg"] / capacity[m], 5)})
        else:
            rec.update({"status": "warning",
                        "reason": "; ".join(f"{objs[active[a if o['norad'] == norad[a] else b]]['name']}: {o['why']}"
                                            for o in options)})
        # The chosen plan is kept once (as rec["plan"]); the options keep only their numbers.
        rec["options"] = [{k: v for k, v in o.items() if k != "plan"} for o in options]
        threats.append(rec)

    print(f"Simulating {n_act} radio-equipped satellites ({len(objs) - n_act} silent objects) for {args.hours:g} h "
          f"from {iso(t0)}: broadcast every {STEP_S:g} s, range {args.link_km:g} km")
    chunk = 180
    for c0 in range(0, len(steps), chunk):
        ts = steps[c0:c0 + chunk]
        err, r, v = arr.sgp4(np.full(ts.size, JD_UNIX), ts / 86400000.0)
        bad = (err != 0) | (ts[None, :] < vf[:, None]) | (ts[None, :] > vt[:, None])
        for k, t_ms in enumerate(ts):
            pos = np.where(bad[:, k][:, None], 1e9 + np.arange(n_act)[:, None] * 1e4, r[:, k, :])
            pairs = cKDTree(pos).query_pairs(args.link_km, output_type="ndarray")
            keys = set((pairs[:, 0] * n_act + pairs[:, 1]).tolist()) if len(pairs) else set()
            minute = int((t_ms - t0) // 60000)
            m = per_minute[minute]
            m[0] += len(keys)
            m[1] += 2 * len(keys)                # each satellite of a pair receives the other's broadcast
            for key in keys - prev:              # new contacts; continuing ones repeat known data
                a, b = divmod(key, n_act)
                if last_check.get(key, -math.inf) > t_ms - RECHECK_MIN * 60000:
                    continue
                last_check[key] = t_ms
                heard[a].add(b); heard[b].add(a)
                m[2] += 2                        # both receivers assess the new data
                dist = float(np.linalg.norm(pos[a] - pos[b]))
                if assess(a, b, t_ms, dist):
                    m[4] += 1
                else:
                    m[3] += 2
            prev = keys
        print(f"  {iso(ts[-1])[:16]}  threats so far {len(threats)}  ({time.time() - started:.0f} s)", flush=True)

    # Ground truth: screened critical encounters between two radio-equipped satellites inside the run.
    detected = {frozenset((t["a"], t["b"])): t for t in threats}
    coverage = {"critical_pairs": 0, "detected_in_time": 0, "never_in_range": 0, "too_late": 0, "discarded_onboard": 0}
    first_contact = {}
    for key, t_ms in last_check.items():
        a, b = divmod(key, n_act)
        first_contact.setdefault(frozenset((norad[a], norad[b])), t_ms)
    for key, evs in pair_events.items():
        for e in evs:
            if e["risk"] != "red" or not t0 <= e["tca_ms"] <= t_end:
                continue
            coverage["critical_pairs"] += 1
            pair = frozenset((e["norad_1"], e["norad_2"]))
            hit = detected.get(pair)
            if hit and hit["detected_ms"] <= e["tca_ms"] - COVERAGE_MIN_WARNING_MIN * 60000:
                coverage["detected_in_time"] += 1
            elif hit or (pair in first_contact and first_contact[pair] > e["tca_ms"] - COVERAGE_MIN_WARNING_MIN * 60000):
                coverage["too_late"] += 1
            elif pair in first_contact:
                coverage["discarded_onboard"] += 1
            else:
                coverage["never_in_range"] += 1

    for t in threats:
        names = {str(norad[j]): objs[active[j]]["name"] for j in range(n_act) if norad[j] in (t["a"], t["b"])}
        t["names"] = names
        feed.append({"t_ms": t["detected_ms"], "threat": t["id"], "kind": "detected"})
        if t["status"] == "avoided":
            for bnum, burn in enumerate(t["plan"]["burns"]):
                feed.append({"t_ms": burn["time_ms"], "threat": t["id"], "kind": "burn", "burn": bnum})
        feed.append({"t_ms": t["tca_ms"], "threat": t["id"], "kind": "tca"})
    feed.sort(key=lambda f: f["t_ms"])

    minutes = sorted(per_minute)
    totals = np.array([per_minute[k] for k in minutes]).sum(axis=0) if minutes else np.zeros(5)
    summary = {"radio_satellites": n_act, "silent_objects": len(objs) - n_act,
               "messages": int(totals[1]), "contacts": int(totals[0]), "assessed": int(totals[2]),
               "discarded": int(totals[3]), "threats": len(threats),
               "avoided": sum(t["status"] == "avoided" for t in threats),
               "monitored": sum(t["status"] == "monitor" for t in threats),
               "warnings": sum(t["status"] == "warning" for t in threats), "coverage": coverage}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_ms": int(t0), "end_ms": int(t_end),
        "config": {"step_s": STEP_S, "link_km": args.link_km, "recheck_min": RECHECK_MIN, "horizon_h": HORIZON_H,
                   "pc_keep": PC_KEEP, "pc_manoeuvre": PC_MANOEUVRE, "miss_keep_km": MISS_KEEP_KM, "fuel_reserve": FUEL_RESERVE,
                   "propellant_fraction": PROPELLANT_FRACTION, "sigma_range_m": SIGMA_RANGE_M,
                   "sigma_along_factor": SIGMA_ALONG_FACTOR, "processing_s": PROCESSING_S},
        "summary": summary,
        "per_minute": {"start_ms": int(t0), "fields": ["contacts", "messages", "assessed", "discarded", "threats"],
                       "rows": [per_minute[k] for k in range(int((t_end - t0) // 60000) + 1)]},
        "satellites": {"fields": ["norad", "fuel_start", "fuel_end", "sigma_m", "mass_kg", "isp_s", "capacity_kg"],
                       "rows": [[norad[j], round(float(fuel_start[j]), 3), round(float(fuel[j]), 3),
                                 round(float(sigma[j]) * 1000, 1), round(float(mass[j]), 1), float(isp[j]),
                                 round(float(capacity[j]), 3)] for j in range(n_act)]},
        "threats": threats, "feed": feed, "discarded_samples": discarded_samples,
    }, separators=(",", ":")))
    print(f"\n{summary['messages']:,} messages, {summary['assessed']:,} assessments of new data, "
          f"{summary['discarded']:,} discarded, {summary['threats']} threats kept "
          f"({summary['avoided']} avoided, {summary['monitored']} monitored, {summary['warnings']} warnings) "
          f"in {time.time() - started:.0f} s")
    print(f"Coverage of {coverage['critical_pairs']} screened critical encounters between radio satellites: "
          f"{coverage['detected_in_time']} detected >= {COVERAGE_MIN_WARNING_MIN:g} min ahead, "
          f"{coverage['too_late']} too late, {coverage['never_in_range']} never within range, "
          f"{coverage['discarded_onboard']} judged safe onboard")
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
