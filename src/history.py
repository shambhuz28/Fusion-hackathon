"""Replays known collisions with the data that was available before them.

For an event, one Space-Track query fetches the history of LEO element sets up to the collision
(cached in data/history/<event>/). For a warning time (--lead-hours, default 24), only element sets
published before the cutoff (collision - lead) are kept, the newest per object: an honest test of what
could have been predicted. That catalogue snapshot is exported and screened like the live data, and a
report says whether the colliding pair was flagged, with predicted TCA, miss distance, Pc and rank.

Space-Track credentials come from the environment and are never written to disk:
    SPACETRACK_USER=... SPACETRACK_PASS=... python src/history.py run iridium-cosmos-2009

Usage:
    python src/history.py list
    python src/history.py run <event> [--lead-hours 24] [--refresh]
The replay then appears in the simulation at http://127.0.0.1:8765/?dataset=history/<event>
"""

import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _venv  # noqa: F401  (re-runs with .venv/bin/python if packages are missing)
import numpy as np
from sgp4 import omm
from sgp4.api import Satrec, SatrecArray, jday

import screen_conjunctions as sc
import spacetrack
from export_sim_data import export
from fetch_positions import check_gp_csv

ROOT = Path(__file__).resolve().parent.parent

# Known accidental collisions between catalogued objects. `start`/`end` bound the reported collision time.
EVENTS = {
    "iridium-cosmos-2009": {
        "name": "Iridium 33 × Cosmos 2251",
        "norad": [24946, 22675],
        "start": "2009-02-10T16:55:59.670Z", "end": "2009-02-10T16:55:59.990Z",
        "note": "Active Iridium 33 and defunct Cosmos 2251 collided at ~790 km, 11.6 km/s. CelesTrak's SOCRATES "
                "had predicted a 584 m miss for 16:55:59.806 UTC.",
        "source": "https://celestrak.org/events/collision/",
    },
    "cerise-1996": {
        "name": "Cerise × Ariane 1 debris",
        "norad": [23606, 18208],
        "start": "1996-07-24T00:00:00Z", "end": "1996-07-25T00:00:00Z",
        "note": "First confirmed collision between two catalogued objects; only the date is published, so the "
                "replay finds the time.",
        "source": "https://en.wikipedia.org/wiki/Cerise_(satellite)",
    },
    "yunhai-2021": {
        "name": "Yunhai 1-02 × Zenit-2 debris",
        "norad": [44547, 48078],
        "start": "2021-03-18T07:40:00Z", "end": "2021-03-18T07:42:00Z",
        "note": "18 SPCS reported Yunhai 1-02's breakup at 07:41 UTC; the fragment (1996-051Q) reportedly had no "
                "element set before the event.",
        "source": "https://www.abs-cbn.com/overseas/08/19/21/chinese-satellite-that-fell-apart-was-hit-by-debris-from-russian-rocket",
    },
}

FIELDS = spacetrack.GP_FIELDS
LOOKBACK_DAYS = 10            # objects need an element set this recent to be in the snapshot
REPORT_LEADS_HOURS = [72, 24, 6]


def parse_utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ Space-Track
def fetch_history(event_id, refresh=False):
    """CSV text of LEO element sets with epochs in [collision - max lead - LOOKBACK_DAYS, collision]."""
    ev = EVENTS[event_id]
    path = ROOT / "data" / "history" / event_id / "spacetrack_gp_history.csv"
    if path.exists() and not refresh:
        print(f"Using cached {path.relative_to(ROOT)}")
        return path.read_text()
    lo = parse_utc(ev["start"]) - timedelta(hours=max(REPORT_LEADS_HOURS), days=LOOKBACK_DAYS)
    hi = parse_utc(ev["end"])
    print(f"Querying Space-Track gp_history {iso(lo)} .. {iso(hi)} UTC (LEO only); this can take a minute")
    text = spacetrack.query_csv(f"class/gp_history/EPOCH/{iso(lo)}--{iso(hi)}/{spacetrack.LEO_FILTER}"
                                f"/orderby/NORAD_CAT_ID%20asc,EPOCH%20asc/predicates/{','.join(FIELDS)}/format/csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"Saved {text.count(chr(10)) - 1} element sets to {path.relative_to(ROOT)}")
    return text


def snapshot(history_csv, cutoff):
    """CelesTrak-style GP CSV: per object, the newest element set with epoch < cutoff (not decayed by then)."""
    newest = {}
    for row in csv.DictReader(io.StringIO(history_csv)):
        epoch = parse_utc(row["EPOCH"] + "Z") if not row["EPOCH"].endswith("Z") else parse_utc(row["EPOCH"])
        if epoch >= cutoff or epoch < cutoff - timedelta(days=LOOKBACK_DAYS):
            continue
        if row.get("DECAY_DATE") and row["DECAY_DATE"] < cutoff.strftime("%Y-%m-%d"):
            continue
        if "." not in row["EPOCH"]:
            row["EPOCH"] += ".000000"
        prev = newest.get(row["NORAD_CAT_ID"])
        if prev is None or row["EPOCH"] > prev["EPOCH"]:
            newest[row["NORAD_CAT_ID"]] = row
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(newest[k] for k in sorted(newest, key=int))
    text = out.getvalue()
    check_gp_csv(text)
    return text, newest


# ------------------------------------------------------------------ pair prediction
def satrec_of(row):
    sat = Satrec()
    omm.initialize(sat, row)
    return sat


def predict_pair(snap_rows, ev, cutoff, window_start, window_end):
    """Closest approach of the event pair between window_start and window_end (the reported collision window,
    +-1 h), propagated from the snapshot's elements (all published before cutoff)."""
    missing = [n for n in ev["norad"] if str(n) not in snap_rows]
    if missing:
        return {"predictable": False, "missing_norad": missing}
    rows = [snap_rows[str(n)] for n in ev["norad"]]
    sa, sb = (satrec_of(r) for r in rows)
    jd0, fr0 = jday(cutoff.year, cutoff.month, cutoff.day, cutoff.hour, cutoff.minute, cutoff.second)
    # Coarse 10 s sweep, then the screener's exact refinement around the best few minima.
    t = np.arange((window_start - cutoff).total_seconds(), (window_end - cutoff).total_seconds(), 10.0)
    _, r, _ = SatrecArray([sa, sb]).sgp4(np.full(t.size, jd0), fr0 + t / 86400)
    d = np.linalg.norm(r[0] - r[1], axis=1)
    best = None
    for k in np.argsort(d)[:20]:
        res = sc.refine(sa, sb, jd0, fr0, float(t[k]))
        if res and (best is None or np.linalg.norm(res[3] - res[1]) < np.linalg.norm(best[3] - best[1])):
            best = res
    tca, ri, vi, rj, vj = best
    debris = [str(rows[k].get("OBJECT_TYPE", "")).upper() == "DEBRIS" for k in range(2)]
    age_a = (tca - sc.epoch_seconds(sa, jd0 + fr0)) / 86400
    age_b = (tca - sc.epoch_seconds(sb, jd0 + fr0)) / 86400
    pc = sc.collision_probability(ri, vi, rj, vj, sc.covariance(ri, vi, age_a, debris[0]),
                                  sc.covariance(rj, vj, age_b, debris[1]), sc.DEFAULT_PARAMS["hbr_m"] / 1000)
    miss = float(np.linalg.norm(rj - ri))
    return {
        "predictable": True,
        "tca_utc": (cutoff + timedelta(seconds=tca)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "miss_km": round(miss, 4), "rel_speed_km_s": round(float(np.linalg.norm(vj - vi)), 3),
        "pc": float(f"{pc:.4g}"), "risk": sc.risk_level(pc, miss),
        "element_epochs": [rows[0]["EPOCH"], rows[1]["EPOCH"]],
        "element_age_days_at_tca": [round(age_a, 2), round(age_b, 2)],
    }


# ------------------------------------------------------------------ run
def run(event_id, lead_hours, refresh, workers):
    ev = EVENTS[event_id]
    start, end = parse_utc(ev["start"]), parse_utc(ev["end"])
    history = fetch_history(event_id, refresh)
    dataset = f"history/{event_id}"
    app_dir, csv_dir = sc.dataset_dirs(dataset)

    # 1. How the pair's prediction changes with warning time.
    leads = []
    for lead in sorted(set(REPORT_LEADS_HOURS + [lead_hours]), reverse=True):
        cutoff = (start - timedelta(hours=lead)).replace(microsecond=0)
        _, rows = snapshot(history, cutoff)
        leads.append({"lead_hours": lead, "cutoff_utc": iso(cutoff) + "Z",
                      **predict_pair(rows, ev, cutoff, start - timedelta(hours=1), end + timedelta(hours=1))})

    # 2. Full catalogue snapshot at the chosen lead, exported and screened like the live data.
    cutoff = (start - timedelta(hours=lead_hours)).replace(microsecond=0)
    snap_text, rows = snapshot(history, cutoff)
    csv_dir.mkdir(parents=True, exist_ok=True)
    (csv_dir / "gp_snapshot.csv").write_text(snap_text)
    export([snap_text], out_dir=app_dir, sources=[f"space-track gp_history before {iso(cutoff)}Z"],
           extra={"event": {"id": event_id, **ev, "cutoff_utc": iso(cutoff) + "Z", "lead_hours": lead_hours}})
    hours = math.ceil((end - cutoff).total_seconds() / 3600) + 1
    subprocess.run([sys.executable, str(ROOT / "src" / "screen_conjunctions.py"), "--dataset", dataset,
                    "--start", iso(cutoff) + "Z", "--hours", str(hours), "--workers", str(workers)], check=True)

    # 3. Was the pair flagged by the full screening, and how did it rank?
    with (csv_dir / "conjunctions_latest.csv").open() as f:
        screened = list(csv.DictReader(f))
    pair = {str(n) for n in ev["norad"]}
    # The pair can pass several times in the window; the closest pass is the candidate collision.
    passes = [e for e in screened if {e["norad_1"], e["norad_2"]} == pair
              and start - timedelta(hours=1) <= parse_utc(e["tca_utc"]) <= end + timedelta(hours=1)]
    hit = min(passes, key=lambda e: float(e["miss_km"]), default=None)
    by_pc = sorted(screened, key=lambda e: -float(e["pc"]))
    day = [e for e in screened if abs((parse_utc(e["tca_utc"]) - start).total_seconds()) <= 43200]
    screening = {"events_screened": len(screened), "critical": sum(e["risk"] == "red" for e in screened),
                 "window_hours": hours, "flagged": hit is not None}
    if hit:
        screening.update({
            "tca_utc": hit["tca_utc"], "miss_km": float(hit["miss_km"]), "pc": float(hit["pc"]), "risk": hit["risk"],
            "rank_by_pc": by_pc.index(hit) + 1,
            "rank_by_pc_within_12h": sorted(day, key=lambda e: -float(e["pc"])).index(hit) + 1,
            "events_within_12h": len(day),
        })
    report = {"event": {"id": event_id, **ev}, "lead_hours": lead_hours, "cutoff_utc": iso(cutoff) + "Z",
              "catalogue_objects": len(rows), "screening": screening, "by_lead": leads}
    (app_dir / "report.json").write_text(json.dumps(report, indent=2))
    print_report(report)


def print_report(rep):
    ev, s = rep["event"], rep["screening"]
    print(f"\n=== {ev['name']} ===  reported {ev['start']} .. {ev['end']}")
    print(f"Catalogue before {rep['cutoff_utc']} ({rep['lead_hours']} h warning): {rep['catalogue_objects']} objects")
    if s["flagged"]:
        print(f"Screening FLAGGED the pair: TCA {s['tca_utc']}, miss {s['miss_km'] * 1000:.0f} m, Pc {s['pc']:.2e} "
              f"({s['risk']}); rank {s['rank_by_pc']} of {s['events_screened']} by Pc "
              f"({s['rank_by_pc_within_12h']} of {s['events_within_12h']} within +-12 h); "
              f"{s['critical']} critical events in the {s['window_hours']} h window")
    else:
        print(f"Screening did NOT flag the pair (no approach < 5 km in the {s['window_hours']} h window)")
    print("Warning time | elements before | predicted TCA            | miss     | Pc       | risk")
    for r in rep["by_lead"]:
        if not r["predictable"]:
            print(f"{r['lead_hours']:>10} h | {r['cutoff_utc']} | not predictable: no element set yet for NORAD "
                  f"{', '.join(map(str, r['missing_norad']))}")
        else:
            print(f"{r['lead_hours']:>10} h | {r['cutoff_utc']} | {r['tca_utc']} | {r['miss_km'] * 1000:6.0f} m | "
                  f"{r['pc']:.2e} | {r['risk']}")
    print(f"Replay: http://127.0.0.1:8765/?dataset=history/{ev['id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("event", choices=sorted(EVENTS))
    r.add_argument("--lead-hours", type=float, default=24, help="warning time: use only data from before this (default 24)")
    r.add_argument("--refresh", action="store_true", help="query Space-Track again instead of the cache")
    r.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.cmd == "list":
        for k, ev in EVENTS.items():
            print(f"{k:22} {ev['name']:32} {ev['start'][:10]}  NORAD {ev['norad'][0]} x {ev['norad'][1]}")
        return
    run(args.event, args.lead_hours, args.refresh, args.workers)


if __name__ == "__main__":
    main()
