"""Export LEO satellites as TLEs for the browser simulation (app/index.html).

Reads one or more CelesTrak GP CSV files, keeps LEO objects (apogee < 2000 km,
e <= 0.25), converts each to a TLE and writes app/data/leo_tle.json, which the
browser propagates with SGP4 (satellite.js).

Usage:
    python src/export_sim_data.py                          # data/raw/active_gp.csv
    python src/export_sim_data.py data/raw/*_gp.csv        # merge several groups
"""

import json
import math
import sys
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


def main():
    paths = sys.argv[1:] or ["data/raw/active_gp.csv"]
    texts = []
    for path in paths:
        text = Path(path).read_text()
        check_gp_csv(text)
        texts.append(text)
    records, sats = load_records(texts)

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
        out.append([int(norad_id), name, constellation(norad_id, name), line1, line2])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [Path(p).name for p in paths],
        "fields": ["norad_id", "name", "group", "line1", "line2"],
        "satellites": out,
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"Wrote {len(out)} LEO satellites to {OUT_PATH}")


if __name__ == "__main__":
    main()
