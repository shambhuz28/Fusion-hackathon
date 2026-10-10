"""ML risk assessment and collision avoidance planning for the app's screened conjunctions.

  1. Every conjunction in app/data/conjunctions.json (from src/screen_conjunctions.py) becomes a
     Kelvins-style CDM row (ml/live_features.py) and is scored by the trained model
     (ml/models/collision_risk_model.joblib): risk probability and YES/NO "will be high risk".
  2. Conjunctions are ranked by that probability (then by Pc).
  3. For every YES conjunction the smallest burn that brings Pc below the target is planned
     (ml/avoidance.py), checked so that it does not bring the satellite close to any other object;
     if no burn is safe, the conjunction gets a COLLISION WARNING.

Writes app/data/risk_assessment.json (read by the simulation's conjunction panel) and prints the
ranked list.

Usage:
    python ml/assess_conjunctions.py                 # all conjunctions, prints the top 40
    python ml/assess_conjunctions.py --print 0       # print every conjunction
    python ml/assess_conjunctions.py --pc-target 1e-8
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import live_features as L
import avoidance as M
from predict import load_model, predict_conjunctions

OUT_PATH = L.ROOT / "app" / "data" / "risk_assessment.json"
REPORT_PATH = Path(__file__).resolve().parent / "models" / "model_report.json"


def utc(t0, sec):
    return t0 + timedelta(seconds=float(sec))


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def score_events(cat, events, data, t0, jd0, fr0):
    """ML probability and decision for every event (index-aligned with `events`)."""
    cdms, kept = L.build_cdms(cat, events, data, jd0, fr0, t0)
    bundle = load_model()
    table = predict_conjunctions(cdms, bundle, as_of_days=0.0).set_index("event_id")
    prob = np.full(len(events), np.nan)
    yes = np.zeros(len(events), bool)
    prob[table.index.to_numpy()] = table["risk_probability"].to_numpy()
    yes[table.index.to_numpy()] = table["decision"].to_numpy() == "YES"
    return prob, yes, bundle


def plan_all(ctx, events, order, yes, pc_target, workers, max_plans=0):
    """Burn plans for every YES event: list of candidate options per event, verified in rounds."""
    cat, t0 = ctx["cat"], ctx["t0"]
    window_end = ctx["window_hours"] * 3600.0
    results, pending = {}, {}
    for k in order:
        if not yes[k]:
            continue
        if max_plans and len(results) + len(pending) >= max_plans:
            break
        e = events[k]
        t_tca = e["tca_ms"] / 1000.0 - t0.timestamp()
        p, s = L.primary_secondary(cat, e)
        if not cat.can_manoeuvre(p):
            results[k] = {"status": "warning", "reason": "Neither object can manoeuvre (debris or rocket bodies)"}
            continue
        options, statuses = [], []
        for man, other in [(p, s)] + ([(s, p)] if cat.can_manoeuvre(s) else []):
            status, cands = M.plan_candidates(ctx, man, other, t_tca, 0.0, pc_target)
            statuses.append(status)
            for c in cands if status == "ok" else []:
                c.update(man=man, other=other, t_tca=t_tca)
                options.append(c)
        if "not_needed" in statuses:
            results[k] = {"status": "not_needed", "reason": f"Pc is already below {pc_target:.0e} with the assumed covariance"}
            continue
        if not options:
            reason = ("Too close to TCA to plan a burn (needs 15 min of preparation)" if "too_late" in statuses
                      else f"Needed burn exceeds the {M.MAX_DV_M_S:g} m/s limit")
            results[k] = {"status": "warning", "reason": reason}
            continue
        options.sort(key=lambda c: np.linalg.norm(c["dv_rtn"]))
        pending[k] = {"options": options[:M.MAX_CANDIDATES], "tried": 0, "rejected": []}

    for round_no in range(M.MAX_CANDIDATES):
        batch = []
        for k, st in pending.items():
            while st["tried"] < len(st["options"]):
                cand = st["options"][st["tried"]]
                st["tried"] += 1
                if verify_at_tca(ctx, cand, pc_target):
                    sat_m = cat.sat(cand["man"])
                    cand.update(plan_id=k, man_idx=cat.index[cand["man"]], n=M.mean_motion(sat_m),
                                t_end=min(window_end, cand["t_tca"] + M.CHECK_ORBITS_AFTER_TCA * M.period_s(sat_m)))
                    batch.append(cand)
                    break
        if not batch:
            break
        t = time.time()
        conflicts = M.check_interference(ctx, batch, workers)
        n_bad = sum(1 for c in batch if conflicts[c["plan_id"]])
        print(f"  round {round_no + 1}: checked {len(batch)} burns against all objects, "
              f"{n_bad} interfere ({time.time() - t:.0f} s)", flush=True)
        for cand in batch:
            k = cand["plan_id"]
            if conflicts[k]:
                pending[k]["rejected"].append((cand, conflicts[k]))
            else:
                results[k] = plan_record(ctx, events[k], cand, pending[k]["rejected"], "ok")
                del pending[k]

    for k, st in pending.items():
        if st["rejected"]:
            cand, conf = st["rejected"][0]
            rec = plan_record(ctx, events[k], cand, st["rejected"], "warning")
            rec["reason"] = (f"Every burn found ({len(st['rejected'])}) would bring the satellite close to "
                             f"another object")
            results[k] = rec
        else:
            results[k] = {"status": "warning", "reason": "No burn reached the target Pc"}
    return results


def verify_at_tca(ctx, cand, pc_target):
    """Exact Pc after the burn; scales the burn up (up to +30 %) if the linear estimate fell short."""
    for _ in range(6):
        res = M.refine_pair(ctx, cand["man"], cand["other"], cand["t_tca"], cand)
        if res is None:
            return False
        if res["pc"] <= pc_target:
            cand["after"] = res
            return np.linalg.norm(cand["dv_rtn"]) * 1000 <= M.MAX_DV_M_S
        cand["dv_rtn"] = cand["dv_rtn"] * 1.05
    return False


def plan_record(ctx, e, cand, rejected, status):
    cat, t0 = ctx["cat"], ctx["t0"]
    dv = cand["dv_rtn"] * 1000.0                      # m/s
    direction, parts = M.describe_direction(dv)
    before = M.refine_pair(ctx, cand["man"], cand["other"], cand["t_tca"], None)
    rec = {
        "status": status,
        "maneuver_norad": cand["man"], "maneuver_name": cat.obj(cand["man"])["name"],
        "other_norad": cand["other"],
        "burn_ms": int(round(utc(t0, cand["t_burn"]).timestamp() * 1000)),
        "lead_orbits": cand["lead_orbits"],
        "lead_min": round((cand["t_tca"] - cand["t_burn"]) / 60, 1),
        "dv_m_s": round(float(np.linalg.norm(dv)), 5),
        "dv_rtn_m_s": [round(float(x), 5) for x in dv],
        "direction": direction,
        "components": [{"axis": p["axis"], "m_s": round(p["m_s"], 5)} for p in parts],
        "before": {"miss_km": round(before["miss_km"], 4), "pc": float(f"{before['pc']:.3g}")} if before else None,
        "after": {"miss_km": round(cand["after"]["miss_km"], 4), "pc": float(f"{cand['after']['pc']:.3g}"),
                  "tca_ms": int(round(utc(t0, cand["after"]["t"]).timestamp() * 1000))},
        "checked_until_ms": int(round(utc(t0, cand["t_end"]).timestamp() * 1000)),
        "rejected_burns": len(rejected),
    }
    conflicts = rejected[0][1] if status == "warning" else []
    rec["conflicts"] = [{"norad": c["norad"], "name": cat.obj(c["norad"])["name"],
                         "tca_ms": int(round(utc(t0, c["t"]).timestamp() * 1000)),
                         "miss_km": round(c["miss_km"], 4), "pc": float(f"{c['pc']:.3g}")}
                        for c in sorted(conflicts, key=lambda c: -c["pc"])[:5]]
    return rec


def short_plan(rec):
    if rec is None:
        return ""
    if rec["status"] == "ok":
        return (f"burn {rec['dv_m_s'] * 100:.1f} cm/s {rec['direction']}, "
                f"{rec['lead_min']:.0f} min before TCA -> Pc {rec['after']['pc']:.1e}")
    if rec["status"] == "not_needed":
        return "no burn needed"
    return f"COLLISION WARNING: {rec['reason']}"


def print_ranked(cat, events, order, prob, yes, plans, t0, limit):
    print(f"\n{'#':>4} {'TCA (UTC)':<19} {'Objects':<44} {'Miss km':>8} {'Pc':>8} {'ML prob':>7} "
          f"{'Decision':>8}  Avoidance")
    print("-" * 150)
    for rank, k in enumerate(order[:limit] if limit else order, 1):
        e = events[k]
        names = f"{cat.obj(e['norad_1'])['name'][:20]} x {cat.obj(e['norad_2'])['name'][:20]}"
        when = datetime.fromtimestamp(e["tca_ms"] / 1000, timezone.utc)
        print(f"{rank:>4} {iso(when):<19} {names:<44} {e['miss_km']:>8.3f} {e['pc']:>8.1e} {prob[k]:>7.3f} "
              f"{'YES' if yes[k] else 'NO':>8}  {short_plan(plans.get(k))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", type=int, default=40, dest="n_print",
                        help="rows of the ranked list to print (0 = all)")
    parser.add_argument("--pc-target", type=float, default=M.PC_TARGET,
                        help=f"Pc to reach with the burn (default {M.PC_TARGET:g})")
    parser.add_argument("--workers", type=int, help="processes for the interference check")
    parser.add_argument("--max-plans", type=int, default=0,
                        help="plan burns for only the N riskiest YES conjunctions (default: all)")
    args = parser.parse_args()
    started = time.time()

    cat = L.Catalogue()
    data, events, t0, jd0, fr0 = L.load_screening()
    print(f"{len(events)} screened conjunctions from {data['window_start_utc']} "
          f"({data['window_hours']:g} h, {len(cat.sats)} objects)")

    print("Scoring with the ML model ...")
    prob, yes, bundle = score_events(cat, events, data, t0, jd0, fr0)
    pcs = np.array([e["pc"] for e in events])
    order = sorted(range(len(events)), key=lambda k: (-np.nan_to_num(prob[k], nan=-1), -pcs[k]))
    print(f"  {int(yes.sum())} of {len(events)} predicted high risk (YES) by {bundle['model_name']} "
          f"({time.time() - started:.0f} s)")

    print("Planning avoidance burns for the YES conjunctions ...")
    ctx = {"cat": cat, "jd0": jd0, "fr0": fr0, "t0": t0, "hbr_km": data["hbr_m"] / 1000.0,
           "window_hours": data["window_hours"]}
    plans = plan_all(ctx, events, order, yes, args.pc_target, args.workers, args.max_plans)
    status = [p["status"] for p in plans.values()]
    counts = {s: status.count(s) for s in ("ok", "not_needed", "warning")}
    print(f"  {counts['ok']} burns planned, {counts['not_needed']} not needed, "
          f"{counts['warning']} COLLISION WARNINGS ({time.time() - started:.0f} s)")

    print_ranked(cat, events, order, prob, yes, plans, t0, args.n_print)

    rank = np.empty(len(events), int)
    rank[order] = np.arange(1, len(events) + 1)
    report = json.loads(REPORT_PATH.read_text()) if REPORT_PATH.exists() else {}
    best = report.get("model_name")
    OUT_PATH.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "screening_generated_utc": data["generated_utc"],
        "assessed_at_utc": data["window_start_utc"],
        "model": {"name": bundle["model_name"], "trained_at": bundle["trained_at"],
                  "high_risk_pc": 10 ** bundle["high_risk_log10"],
                  "test": report.get("test", {}).get(best) if best else None},
        "maneuver": {"pc_target": args.pc_target, "pc_high": M.PC_HIGH, "max_dv_m_s": M.MAX_DV_M_S,
                     "min_prep_min": M.MIN_PREP_S / 60, "lead_orbits": list(M.LEAD_ORBITS),
                     "check_orbits_after_tca": M.CHECK_ORBITS_AFTER_TCA, "check_step_s": M.CHECK_STEP_S},
        "counts": {"events": len(events), "yes": int(yes.sum()), **counts},
        "fields": ["tca_ms", "norad_1", "norad_2", "ml_prob", "ml_yes", "rank"],
        "rows": [[e["tca_ms"], e["norad_1"], e["norad_2"],
                  None if math.isnan(prob[k]) else float(f"{prob[k]:.4g}"), int(yes[k]), int(rank[k])]
                 for k, e in enumerate(events)],
        # Keyed "tca_ms:norad_1:norad_2", as in conjunctions.json.
        "maneuvers": {f"{events[k]['tca_ms']}:{events[k]['norad_1']}:{events[k]['norad_2']}": rec
                      for k, rec in plans.items()},
    }, separators=(",", ":")))
    print(f"\nWrote {OUT_PATH.relative_to(L.ROOT)} in {time.time() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
