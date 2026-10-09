"""Export LEO objects for the browser simulation and the conjunction screener.

Reads one or more CelesTrak GP CSV files, keeps LEO objects (apogee < 2000 km,
e <= 0.25) and writes app/data/leo_objects.json with their full-precision OMM
elements. Both the browser (satellite.js json2satrec) and src/screen_conjunctions.py
(sgp4 omm.initialize) propagate these same records, so they agree to the millimetre.

Why OMM rather than TLE: the TLE text format rounds eccentricity to 7 digits and B* to
5 significant digits, which moved some fast-decaying debris by up to ~280 m.

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

import telemetry

OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "leo_objects.json"

OMM_FIELDS = [
    "OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER", "MEAN_ANOMALY", "EPHEMERIS_TYPE", "CLASSIFICATION_TYPE", "NORAD_CAT_ID",
    "ELEMENT_SET_NO", "REV_AT_EPOCH", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT",
]
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


def read_gp(path):
    text = Path(path).read_text()
    check_gp_csv(text)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", default=["data/raw/active_gp.csv"], help="GP CSV files of satellites")
    parser.add_argument("--debris", nargs="+", default=[], help="GP CSV files of debris objects")
    args = parser.parse_args()

    texts = [read_gp(path) for path in args.inputs]
    debris_texts = [read_gp(path) for path in args.debris]
    debris_ids = {fields["NORAD_CAT_ID"] for fields in load_records(debris_texts)[0]}
    records, sats = load_records(texts + debris_texts)

    out = []
    for fields, sat in zip(records, sats):
        period_min = 2 * math.pi / sat.no_kozai
        perigee = sat.altp * sat.radiusearthkm
        apogee = sat.alta * sat.radiusearthkm
        if orbit_class(perigee, apogee, period_min, sat.ecco) != "LEO":
            continue
        norad_id = fields["NORAD_CAT_ID"]
        group = "Debris" if norad_id in debris_ids else constellation(norad_id, fields["OBJECT_NAME"])
        rec = millisecond_omm(fields)
        out.append([group, [rec[k] for k in OMM_FIELDS]])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [Path(p).name for p in args.inputs + args.debris],
        "omm_fields": OMM_FIELDS,
        "objects": out,   # [group, [OMM values in omm_fields order]]
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    n_debris = sum(1 for row in out if row[0] == "Debris")
    print(f"Wrote {len(out)} LEO objects ({len(out) - n_debris} satellites, {n_debris} debris) to {OUT_PATH}")
    telemetry.info("Exported {objects} LEO objects: {satellites} satellites, {debris} debris", objects=len(out),
                   satellites=len(out) - n_debris, debris=n_debris, parsed=len(records), sources=payload["sources"])


if __name__ == "__main__":
    telemetry.configure("export-sim-data")
    with telemetry.span("Export simulation data"):
        main()
