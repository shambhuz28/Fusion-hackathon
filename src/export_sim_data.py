"""Export LEO objects for the browser simulation and the conjunction screener.

Reads one or more CelesTrak GP CSV files, keeps LEO objects (apogee < 2000 km,
e <= 0.25) and writes app/data/leo_objects.json with their full-precision OMM
elements. Both the browser (satellite.js json2satrec) and src/screen_conjunctions.py
(sgp4 omm.initialize) propagate these same records, so they agree to the millimetre.

Why OMM rather than TLE: the TLE text format rounds eccentricity to 7 digits and B* to
5 significant digits, which moved some fast-decaying debris by up to ~280 m.

Each object also gets a validity window: VALIDITY_DAYS either side of its element epoch (SGP4
predictions degrade by km per day and are meaningless months away), and never before its estimated
launch (epoch - REV_AT_EPOCH / MEAN_MOTION). The browser hides an object outside its window and the
screener ignores it, so the 2026 catalogue is not shown in, say, 2009.

Epochs are written on whole milliseconds because satellite.js parses EPOCH with a
JavaScript Date (millisecond resolution). The dropped sub-millisecond part is moved
into the mean anomaly (M - n * dt), which describes the same orbit to within millimetres.

Objects from files passed with --debris are labelled "Debris" in the simulation.

Usage:
    python src/export_sim_data.py                          # data/raw/active_gp.csv
    python src/export_sim_data.py data/raw/*_gp.csv        # merge several groups
    python src/export_sim_data.py data/raw/active_gp.csv --debris data/raw/cosmos-2251-debris_gp.csv
"""

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _venv  # noqa: F401  (re-runs with .venv/bin/python if packages are missing)
from fetch_positions import check_gp_csv, load_records, orbit_class

OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "leo_objects.json"

OMM_FIELDS = [
    "OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER", "MEAN_ANOMALY", "EPHEMERIS_TYPE", "CLASSIFICATION_TYPE", "NORAD_CAT_ID",
    "ELEMENT_SET_NO", "REV_AT_EPOCH", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT",
]
VALIDITY_DAYS = 30
DAY_MS = 86400000

TEXT_FIELDS = {"OBJECT_NAME", "OBJECT_ID", "EPOCH", "CLASSIFICATION_TYPE"}
INT_FIELDS = {"EPHEMERIS_TYPE", "NORAD_CAT_ID", "ELEMENT_SET_NO", "REV_AT_EPOCH"}

# Name prefix -> constellation label shown in the simulation legend.
CONSTELLATIONS = [
    ("STARLINK", "Starlink"),
    ("ONEWEB", "OneWeb"),
    ("KUIPER", "Kuiper"),
    ("QIANFAN", "Qianfan"),
    ("HULIANWANG", "Guowang"),
    ("FLOCK", "Planet"),
    ("LEMUR", "Spire"),
    ("IRIDIUM", "Iridium"),
]
STATIONS = {"25544", "48274"}  # ISS, Tiangong (CSS)


def constellation(norad_id, name):
    if norad_id in STATIONS:
        return "Station"
    for prefix, label in CONSTELLATIONS:
        if name.startswith(prefix):
            return label
    return "Other"


def millisecond_omm(fields):
    """OMM values with the epoch truncated to whole milliseconds and the mean anomaly compensated."""
    epoch = datetime.strptime(fields["EPOCH"], "%Y-%m-%dT%H:%M:%S.%f")
    dropped_us = epoch.microsecond % 1000
    rec = {}
    for key in OMM_FIELDS:
        value = fields[key]
        rec[key] = value if key in TEXT_FIELDS else int(value) if key in INT_FIELDS else float(value)
    rec["EPOCH"] = (epoch - timedelta(microseconds=dropped_us)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    deg_per_s = rec["MEAN_MOTION"] * 360.0 / 86400.0
    rec["MEAN_ANOMALY"] = (rec["MEAN_ANOMALY"] - deg_per_s * dropped_us * 1e-6) % 360.0
    return rec


def validity_window_ms(fields):
    """(valid_from, valid_to) in Unix ms: +-VALIDITY_DAYS around the epoch, not before the estimated launch."""
    epoch = datetime.strptime(fields["EPOCH"], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    epoch_ms = int(epoch.timestamp() * 1000)
    valid_from, valid_to = epoch_ms - VALIDITY_DAYS * DAY_MS, epoch_ms + VALIDITY_DAYS * DAY_MS
    # Revolutions since launch / revolutions per day = days since launch. The counter wraps at 100,000, so the
    # estimate is only trusted when it agrees with the launch year in the international designator.
    # Space-Track records carry the real launch and decay dates; use them when present.
    if fields.get("LAUNCH_DATE"):
        launch = datetime.strptime(fields["LAUNCH_DATE"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        valid_from = max(valid_from, int(launch.timestamp() * 1000))
    else:
        revs, rev_per_day = int(fields["REV_AT_EPOCH"]), float(fields["MEAN_MOTION"])
        launch_ms = epoch_ms - revs / rev_per_day * DAY_MS
        launch_year = int(fields["OBJECT_ID"][:4]) if fields["OBJECT_ID"][:4].isdigit() else None
        if launch_year is not None and datetime.fromtimestamp(launch_ms / 1000, timezone.utc).year == launch_year:
            valid_from = max(valid_from, int(launch_ms))
    if fields.get("DECAY_DATE"):
        decay = datetime.strptime(fields["DECAY_DATE"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        valid_to = min(valid_to, int(decay.timestamp() * 1000) + DAY_MS)
    return valid_from, valid_to


def read_gp(path):
    text = Path(path).read_text()
    check_gp_csv(text)
    return text


def group_of(fields, debris_ids):
    """Legend group: debris (from --debris files or Space-Track's OBJECT_TYPE), rocket body, or constellation."""
    norad_id = fields["NORAD_CAT_ID"]
    object_type = (fields.get("OBJECT_TYPE") or "").upper()
    if norad_id in debris_ids or object_type == "DEBRIS":
        return "Debris"
    if object_type == "ROCKET BODY":
        return "Rocket body"
    return constellation(norad_id, fields["OBJECT_NAME"])


def export(texts, debris_texts=(), out_dir=OUT_PATH.parent, sources=(), extra=None):
    """Write <out_dir>/leo_objects.json from GP CSV texts; returns the number of objects written."""
    debris_ids = {fields["NORAD_CAT_ID"] for fields in load_records(debris_texts)[0]}
    records, sats = load_records(list(texts) + list(debris_texts))

    out = []
    for fields, sat in zip(records, sats):
        period_min = 2 * math.pi / sat.no_kozai
        perigee = sat.altp * sat.radiusearthkm
        apogee = sat.alta * sat.radiusearthkm
        if orbit_class(perigee, apogee, period_min, sat.ecco) != "LEO":
            continue
        group = group_of(fields, debris_ids)
        rec = millisecond_omm(fields)
        out.append([group, [rec[k] for k in OMM_FIELDS], *validity_window_ms(fields)])

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": list(sources),
        "omm_fields": OMM_FIELDS,
        "validity_days": VALIDITY_DAYS,
        # The simulation clock is limited to this range (the newest epochs +- VALIDITY_DAYS).
        "clock_range_ms": [max(row[3] for row in out) - 2 * VALIDITY_DAYS * DAY_MS, max(row[3] for row in out)],
        **(extra or {}),
        "objects": out,   # [group, [OMM values in omm_fields order], valid_from_ms, valid_to_ms]
    }
    out_path = out_dir / "leo_objects.json"
    out_path.write_text(json.dumps(payload, separators=(",", ":")))
    n_debris = sum(1 for row in out if row[0] == "Debris")
    print(f"Wrote {len(out)} LEO objects ({len(out) - n_debris} satellites/rocket bodies, {n_debris} debris) to {out_path}")
    return len(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", default=["data/raw/active_gp.csv"], help="GP CSV files of satellites")
    parser.add_argument("--debris", nargs="+", default=[], help="GP CSV files of debris objects")
    parser.add_argument("--out-dir", default=str(OUT_PATH.parent), help="output directory (default app/data)")
    args = parser.parse_args()
    export([read_gp(p) for p in args.inputs], [read_gp(p) for p in args.debris], args.out_dir,
           [Path(p).name for p in args.inputs + args.debris])


if __name__ == "__main__":
    main()
