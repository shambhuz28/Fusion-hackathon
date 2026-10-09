"""Fetch CelesTrak orbital elements and compute current satellite positions with SGP4.

Downloads the GP (OMM) CSV for a CelesTrak group, propagates every object to a
single instant with SGP4, and writes one CSV row per satellite containing its
orbital parameters plus its position (TEME x/y/z, velocity, lat/lon/alt).

Usage:
    python src/fetch_positions.py                      # active satellites, now
    python src/fetch_positions.py --time 2026-10-09T12:00:00Z
    python src/fetch_positions.py --group stations --refresh
    python src/fetch_positions.py --leo                # LEO satellites only
    python src/fetch_positions.py --input a.csv b.csv --name merged --leo
"""

import argparse
import csv
import io
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import _venv  # noqa: F401  (re-runs with .venv/bin/python if packages are missing)
import numpy as np
from sgp4 import omm
from sgp4.api import Satrec, SatrecArray, jday
from sgp4.propagation import gstime

import telemetry

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=csv"
# CelesTrak asks clients not to re-download the same group more than once per 2 hours.
CACHE_MAX_AGE_S = 2 * 3600

WGS84_A_KM = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

SGP4_ERRORS = {
    0: "",
    1: "mean eccentricity out of range",
    2: "mean motion below zero",
    3: "perturbed eccentricity out of range",
    4: "semi-latus rectum below zero",
    6: "orbit decayed",
}

OUTPUT_FIELDS = [
    "norad_id", "name", "intl_designator", "epoch_utc", "epoch_age_days",
    "inclination_deg", "raan_deg", "eccentricity", "arg_perigee_deg",
    "mean_anomaly_deg", "mean_motion_rev_per_day", "bstar",
    "mean_motion_dot", "mean_motion_ddot", "rev_at_epoch", "element_set_no",
    "period_min", "semi_major_axis_km", "perigee_alt_km", "apogee_alt_km",
    "orbit_class",
    "position_time_utc",
    "x_teme_km", "y_teme_km", "z_teme_km",
    "vx_teme_km_s", "vy_teme_km_s", "vz_teme_km_s", "speed_km_s",
    "x_ecef_km", "y_ecef_km", "z_ecef_km",
    "latitude_deg", "longitude_deg", "altitude_km",
    "sgp4_error",
]


def download_gp_csv(group, raw_path, refresh):
    """Return the GP CSV text for `group`, using a cached copy if it is fresh."""
    if raw_path.exists() and not refresh:
        age = time.time() - raw_path.stat().st_mtime
        if age < CACHE_MAX_AGE_S:
            print(f"Using cached {raw_path} ({age / 60:.0f} min old)")
            telemetry.info("Using cached {group} elements, {age_min} min old", group=group, age_min=round(age / 60))
            return raw_path.read_text()

    url = GP_URL.format(group=group)
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "fusion-hackathon/0.1"})
    try:
        with telemetry.span("Download {group} from CelesTrak", group=group, url=url) as span:
            with urllib.request.urlopen(req, timeout=600) as resp:
                text = resp.read().decode("utf-8")
            span.set_attribute("bytes", len(text))
    except urllib.error.HTTPError as err:
        # CelesTrak answers 403 when the group was already fetched in the last 2 hours.
        body = err.read().decode("utf-8", "replace").strip()
        telemetry.warn("CelesTrak refused {group} ({status})", group=group, status=err.code, body=body,
                       cached_copy=raw_path.exists())
        if raw_path.exists():
            print(f"CelesTrak refused ({err.code}): {body}\nFalling back to cached {raw_path}")
            return raw_path.read_text()
        raise RuntimeError(f"CelesTrak refused ({err.code}): {body}") from err

    check_gp_csv(text)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = raw_path.with_suffix(".tmp")
    tmp_path.write_text(text)
    tmp_path.replace(raw_path)
    return text


def check_gp_csv(text):
    """Raise if `text` is not a complete GP CSV (wrong header or a row cut off mid-download)."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("OBJECT_NAME"):
        raise RuntimeError(f"Unexpected response from CelesTrak: {text[:200]!r}")
    n_cols = len(lines[0].split(","))
    bad = [i + 1 for i, line in enumerate(lines) if line and len(next(csv.reader([line]))) != n_cols]
    if bad or not text.endswith("\n"):
        raise RuntimeError(f"Incomplete GP CSV (malformed lines: {bad[:5]}); download was likely cut off")


def load_records(texts):
    """Parse GP CSV texts into (OMM field dicts, Satrecs), one per NORAD ID, keeping the newest epoch."""
    newest = {}
    for text in texts:
        for fields in omm.parse_csv(io.StringIO(text)):
            prev = newest.get(fields["NORAD_CAT_ID"])
            if prev is None or fields["EPOCH"] > prev["EPOCH"]:
                newest[fields["NORAD_CAT_ID"]] = fields
    records = sorted(newest.values(), key=lambda f: int(f["NORAD_CAT_ID"]))
    sats = []
    for fields in records:
        sat = Satrec()
        omm.initialize(sat, fields)
        sats.append(sat)
    return records, sats


def teme_to_ecef(r_teme, jd_ut1):
    """Rotate TEME positions (N x 3, km) to Earth-fixed by GMST; polar motion ignored."""
    g = gstime(jd_ut1)
    c, s = math.cos(g), math.sin(g)
    x, y, z = r_teme[:, 0], r_teme[:, 1], r_teme[:, 2]
    return np.column_stack([c * x + s * y, -s * x + c * y, z])


def ecef_to_geodetic(r_ecef):
    """Convert ECEF positions (N x 3, km) to WGS84 latitude, longitude (deg) and altitude (km)."""
    x, y, z = r_ecef[:, 0], r_ecef[:, 1], r_ecef[:, 2]
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - WGS84_E2))
    for _ in range(5):
        n = WGS84_A_KM / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
        alt = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - WGS84_E2 * n / (n + alt)))
    n = WGS84_A_KM / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    alt = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), alt


def orbit_class(perigee_km, apogee_km, period_min, ecc):
    if ecc > 0.25:
        return "HEO"
    if apogee_km < 2000:
        return "LEO"
    if abs(period_min - 1436) < 30:
        return "GEO"
    if perigee_km < 35000:
        return "MEO"
    return "OTHER"


def parse_time(value):
    if value is None:
        return datetime.now(timezone.utc)
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def fmt(value, digits):
    return "" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", default="active", help="CelesTrak group (default: active)")
    parser.add_argument("--time", help="UTC instant to propagate to, ISO 8601 (default: now)")
    parser.add_argument("--data-dir", default="data", help="output directory (default: data)")
    parser.add_argument("--refresh", action="store_true", help="ignore the 2-hour download cache")
    parser.add_argument("--input", nargs="+", help="use local GP CSV file(s) instead of downloading; "
                        "duplicates are merged, keeping the newest epoch")
    parser.add_argument("--leo", action="store_true", help="keep only LEO objects (apogee < 2000 km, e <= 0.25)")
    parser.add_argument("--name", help="output file prefix (default: the group name)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.input:
        texts = [Path(path).read_text() for path in args.input]
        for text in texts:
            check_gp_csv(text)
    else:
        raw_path = data_dir / "raw" / f"{args.group}_gp.csv"
        texts = [download_gp_csv(args.group, raw_path, args.refresh)]

    records, sats = load_records(texts)
    print(f"Parsed {len(sats)} unique objects")

    t = parse_time(args.time)
    jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
    errors, r, v = SatrecArray(sats).sgp4(np.array([jd]), np.array([fr]))
    errors, r, v = errors[:, 0], r[:, 0, :], v[:, 0, :]

    r_ecef = teme_to_ecef(r, jd + fr)
    lat, lon, alt = ecef_to_geodetic(r_ecef)
    speed = np.linalg.norm(v, axis=1)

    stamp = t.strftime("%Y%m%dT%H%M%SZ")
    prefix = (args.name or args.group) + ("_leo" if args.leo else "")
    out_path = data_dir / f"{prefix}_positions_{stamp}.csv"
    latest_path = data_dir / f"{prefix}_positions_latest.csv"
    t_iso = t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    rows = []
    for i, (fields, sat) in enumerate(zip(records, sats)):
        ok = errors[i] == 0
        period_min = 2 * math.pi / sat.no_kozai
        a_km = sat.a * sat.radiusearthkm
        perigee = sat.altp * sat.radiusearthkm
        apogee = sat.alta * sat.radiusearthkm
        epoch = datetime.fromisoformat(fields["EPOCH"]).replace(tzinfo=timezone.utc)

        row = {
            "norad_id": fields["NORAD_CAT_ID"],
            "name": fields["OBJECT_NAME"],
            "intl_designator": fields["OBJECT_ID"],
            "epoch_utc": fields["EPOCH"],
            "epoch_age_days": fmt((t - epoch).total_seconds() / 86400, 3),
            "inclination_deg": fields["INCLINATION"],
            "raan_deg": fields["RA_OF_ASC_NODE"],
            "eccentricity": fields["ECCENTRICITY"],
            "arg_perigee_deg": fields["ARG_OF_PERICENTER"],
            "mean_anomaly_deg": fields["MEAN_ANOMALY"],
            "mean_motion_rev_per_day": fields["MEAN_MOTION"],
            "bstar": fields["BSTAR"],
            "mean_motion_dot": fields["MEAN_MOTION_DOT"],
            "mean_motion_ddot": fields["MEAN_MOTION_DDOT"],
            "rev_at_epoch": fields["REV_AT_EPOCH"],
            "element_set_no": fields["ELEMENT_SET_NO"],
            "period_min": fmt(period_min, 3),
            "semi_major_axis_km": fmt(a_km, 3),
            "perigee_alt_km": fmt(perigee, 3),
            "apogee_alt_km": fmt(apogee, 3),
            "orbit_class": orbit_class(perigee, apogee, period_min, sat.ecco),
            "position_time_utc": t_iso,
            "sgp4_error": SGP4_ERRORS.get(int(errors[i]), f"code {errors[i]}"),
        }
        values = {
            "x_teme_km": r[i, 0], "y_teme_km": r[i, 1], "z_teme_km": r[i, 2],
            "vx_teme_km_s": v[i, 0], "vy_teme_km_s": v[i, 1], "vz_teme_km_s": v[i, 2],
            "speed_km_s": speed[i],
            "x_ecef_km": r_ecef[i, 0], "y_ecef_km": r_ecef[i, 1], "z_ecef_km": r_ecef[i, 2],
            "latitude_deg": lat[i], "longitude_deg": lon[i], "altitude_km": alt[i],
        }
        for key, value in values.items():
            row[key] = fmt(float(value), 6) if ok else ""
        if args.leo and row["orbit_class"] != "LEO":
            continue
        rows.append(row)

    data_dir.mkdir(parents=True, exist_ok=True)
    for path in (out_path, latest_path):
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    failed = sum(1 for row in rows if row["sgp4_error"])
    print(f"Wrote {len(rows)} objects at {t_iso} ({failed} SGP4 errors)")
    telemetry.info("Wrote {objects} of {parsed} objects, {sgp4_errors} SGP4 errors", objects=len(rows),
                   parsed=len(sats), sgp4_errors=failed, leo_only=args.leo, output=str(latest_path))
    print(f"Wrote {out_path} and {latest_path}")
    return 0


if __name__ == "__main__":
    telemetry.configure("fetch-positions")
    try:
        with telemetry.span("Fetch positions"):
            code = main()
        sys.exit(code)
    except RuntimeError as err:  # e.g. CelesTrak's 2-hour download limit
        sys.exit(f"Error: {err}")
