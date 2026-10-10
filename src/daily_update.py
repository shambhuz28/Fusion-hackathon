"""Daily refresh of the live data: the simulation always covers today 00:00 UTC to 7 days ahead.

  1. Download fresh orbital elements from CelesTrak (active satellites + the debris groups, as run.sh does).
     If CelesTrak refuses or is unreachable, the cached downloads in data/raw/ are used; if there are
     none, the current app/data/leo_objects.json is kept (SGP4 elements stay usable for +-30 days).
  2. Export them (src/export_sim_data.py).
  3. Screen conjunctions from today 00:00 UTC for 7 days (src/screen_conjunctions.py).
  4. ML risk ranking and precomputed avoidance burns (ml/assess_conjunctions.py), when ml/.venv exists.

Everything is built in app/data/staging/ and moved into app/data/ only once all steps succeeded, so the
simulation never loads a half-updated mix. src/server.py runs this automatically when the live screening
does not start today; it can also be run by hand or from a scheduler.

Usage:
    python src/daily_update.py            # update if the screening does not start today
    python src/daily_update.py --force    # rebuild anyway
    python src/daily_update.py --no-download --no-ml
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
APP_DATA = ROOT / "app" / "data"
STAGING = "staging"                       # dataset name: app/data/staging/, data/staging/
STAGING_DIR = APP_DATA / STAGING
RAW = ROOT / "data" / "raw"
DEBRIS_GROUPS = ["cosmos-2251-debris", "fengyun-1c-debris", "iridium-33-debris", "cosmos-1408-debris"]
# Used when CelesTrak refuses the full active group (it allows one download per 2 hours), as in run.sh.
FALLBACK_GROUPS = ["active_gp_partial", "starlink_gp", "kuiper_gp", "qianfan_gp", "hulianwang_gp", "last-30-days_gp"]
FILES = ["leo_objects.json", "conjunctions.json", "risk_assessment.json"]


def today_utc():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def live_window_start():
    """Start of the live screening window (UTC), or None when there is none."""
    try:
        data = json.loads((APP_DATA / "conjunctions.json").read_text())
        return datetime.fromisoformat(data["window_start_utc"].replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError):
        return None


def is_current(day=None):
    """True when the live screening starts on `day` (default: today, UTC)."""
    start = live_window_start()
    return start is not None and start.date() == (day or today_utc()).date()


def ml_python():
    for rel in ("ml/.venv/Scripts/python.exe", "ml/.venv/bin/python"):
        if (ROOT / rel).exists():
            return ROOT / rel
    return None


def run(step, args, python=sys.executable):
    print(f"== {step}", flush=True)
    began = time.time()
    result = subprocess.run([str(python), *map(str, args)], cwd=ROOT)
    print(f"   ({time.time() - began:.0f} s, exit {result.returncode})", flush=True)
    return result.returncode == 0


def download():
    """GP CSV files of satellites and debris to export ([] when nothing is available)."""
    if run("Downloading active satellites from CelesTrak", [SRC / "fetch_positions.py", "--group", "active", "--leo"]):
        active = [RAW / "active_gp.csv"]
    else:
        active = [RAW / f"{g}.csv" for g in FALLBACK_GROUPS if (RAW / f"{g}.csv").exists()]
        print(f"   full active group unavailable; cached groups: {[p.name for p in active] or 'none'}", flush=True)
    debris = []
    for group in DEBRIS_GROUPS:
        run(f"Downloading {group}", [SRC / "fetch_positions.py", "--group", group, "--leo"])
        if (RAW / f"{group}_gp.csv").exists():
            debris.append(RAW / f"{group}_gp.csv")
    return active, debris


def swap_in():
    """Move the staged files into app/data (the risk file goes first, the objects last)."""
    for name in reversed(FILES):
        src = STAGING_DIR / name
        if src.exists():
            os.replace(src, APP_DATA / name)
        elif name == "risk_assessment.json":
            # An assessment of the previous screening no longer matches its conjunctions.
            (APP_DATA / name).unlink(missing_ok=True)
    # The full CSV of every event (git-ignored), next to the live one.
    csv_dir = ROOT / "data" / STAGING
    for csv_path in csv_dir.glob("conjunctions_*.csv"):
        os.replace(csv_path, ROOT / "data" / csv_path.name)
    shutil.rmtree(csv_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="rebuild even if the screening already starts today")
    parser.add_argument("--days", type=int, default=7, help="days to screen from today 00:00 UTC (default 7)")
    parser.add_argument("--no-download", action="store_true", help="keep the current orbital elements")
    parser.add_argument("--no-ml", action="store_true", help="skip the ML ranking")
    args = parser.parse_args()

    start = today_utc()
    if is_current(start) and not args.force:
        print(f"The live screening already starts today ({start:%Y-%m-%d}); nothing to do (--force to rebuild).")
        return 0
    began = time.time()
    end = start + timedelta(days=args.days)
    print(f"Daily update: {start:%Y-%m-%d} to {end:%Y-%m-%d} UTC", flush=True)
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    STAGING_DIR.mkdir(parents=True)

    active, debris = ([], []) if args.no_download else download()
    if active:
        export_args = [SRC / "export_sim_data.py", *active, "--out-dir", STAGING_DIR]
        if debris:
            export_args += ["--debris", *debris]
        if not run("Exporting simulation data", export_args):
            return 1
    else:
        print("== Keeping the current orbital elements (no new download)", flush=True)
        shutil.copy2(APP_DATA / "leo_objects.json", STAGING_DIR / "leo_objects.json")

    if not run(f"Screening conjunctions for {args.days} days from {start:%Y-%m-%d} 00:00 UTC",
               [SRC / "screen_conjunctions.py", "--dataset", STAGING, "--start", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--hours", 24 * args.days]):
        return 1

    python = None if args.no_ml else ml_python()
    if python and (ROOT / "ml" / "models" / "collision_risk_model.joblib").exists():
        if not run("ML risk ranking and avoidance burns",
                   [ROOT / "ml" / "assess_conjunctions.py", "--app-dir", STAGING_DIR, "--print", "10"], python):
            print("   ML step failed; the screening is used without the ML ranking", flush=True)
            (STAGING_DIR / "risk_assessment.json").unlink(missing_ok=True)
    elif not args.no_ml:
        print("== ML step skipped: ml/.venv or the trained model is missing (see README)", flush=True)

    swap_in()
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    print(f"Daily update done in {(time.time() - began) / 60:.1f} min: the simulation now covers "
          f"{start:%Y-%m-%d} to {end:%Y-%m-%d} UTC", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
