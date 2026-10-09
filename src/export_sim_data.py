"""Export LEO satellites as TLEs for the browser simulation (app/index.html).

Reads one or more CelesTrak GP CSV files, keeps LEO objects (apogee < 2000 km,
e <= 0.25), converts each to a TLE and writes app/data/leo_tle.json, which the
browser propagates with SGP4 (satellite.js).

Objects from files passed with --debris are labelled "Debris" in the simulation.

Usage:
    python src/export_sim_data.py                          # data/raw/active_gp.csv
    python src/export_sim_data.py data/raw/*_gp.csv        # merge several groups
    python src/export_sim_data.py data/raw/active_gp.csv --debris data/raw/cosmos-2251-debris_gp.csv
"""

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from sgp4.exporter import export_tle

from fetch_positions import check_gp_csv, load_records, orbit_class

OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "leo_tle.json"

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
        line1, line2 = export_tle(sat)
        norad_id = fields["NORAD_CAT_ID"]
        name = fields["OBJECT_NAME"]
        group = "Debris" if norad_id in debris_ids else constellation(norad_id, name)
        out.append([int(norad_id), name, group, line1, line2])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [Path(p).name for p in args.inputs + args.debris],
        "fields": ["norad_id", "name", "group", "line1", "line2"],
        "satellites": out,
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    n_debris = sum(1 for row in out if row[2] == "Debris")
    print(f"Wrote {len(out)} LEO objects ({len(out) - n_debris} satellites, {n_debris} debris) to {OUT_PATH}")


if __name__ == "__main__":
    main()
