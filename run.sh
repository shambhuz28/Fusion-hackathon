#!/usr/bin/env bash
# Runs the whole project: fetch orbital data, compute positions, export the simulation data,
# screen conjunctions for 7 days, then serve the 3D simulation (src/server.py).
#
#   ./run.sh               full pipeline + server (~5 min before the server starts)
#   ./run.sh --serve-only  just serve the simulation with the existing data
#   PORT=9000 ./run.sh     use another port (default 8765)
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
PORT="${PORT:-8765}"
DEBRIS_GROUPS=(cosmos-2251-debris fengyun-1c-debris iridium-33-debris cosmos-1408-debris)
# Used when CelesTrak refuses the full active group (it allows one download per 2 hours).
FALLBACK_GROUPS=(active_gp_partial starlink_gp kuiper_gp qianfan_gp hulianwang_gp last-30-days_gp)

if [ ! -x "$PY" ]; then
  echo "== Setting up Python environment =="
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ "${1:-}" != "--serve-only" ]; then
  echo "== 1/4 Downloading orbital elements from CelesTrak and computing LEO positions =="
  if "$PY" src/fetch_positions.py --group active --leo; then
    ACTIVE=(data/raw/active_gp.csv)
  else
    echo "Full active group unavailable; using the cached groups instead"
    ACTIVE=()
    for f in "${FALLBACK_GROUPS[@]}"; do
      if [ -f "data/raw/$f.csv" ]; then ACTIVE+=("data/raw/$f.csv"); fi
    done
    if [ ${#ACTIVE[@]} -eq 0 ]; then echo "No cached satellite data in data/raw; try again later." >&2; exit 1; fi
  fi

  DEBRIS=()
  for g in "${DEBRIS_GROUPS[@]}"; do
    "$PY" src/fetch_positions.py --group "$g" --leo || echo "Could not refresh $g"
    if [ -f "data/raw/${g}_gp.csv" ]; then DEBRIS+=("data/raw/${g}_gp.csv"); fi
  done

  echo "== 2/4 Exporting simulation data =="
  if [ ${#DEBRIS[@]} -gt 0 ]; then
    "$PY" src/export_sim_data.py "${ACTIVE[@]}" --debris "${DEBRIS[@]}"
  else
    "$PY" src/export_sim_data.py "${ACTIVE[@]}"
  fi

  echo "== 3/4 Screening conjunctions from today 00:00 UTC for 7 days (~4 min) =="
  "$PY" src/screen_conjunctions.py --start "$(date -u +%Y-%m-%dT00:00:00Z)"

  # Optional: ML risk ranking and avoidance manoeuvres (needs ml/.venv and a trained model, see README).
  if [ -x ml/.venv/bin/python ] && [ -f ml/models/collision_risk_model.joblib ]; then
    echo "== 3b/4 ML risk ranking and avoidance manoeuvres (~10 min) =="
    ml/.venv/bin/python ml/assess_conjunctions.py --print 20
  fi
fi

echo "== 4/4 Starting the simulation server (also screens any chosen date on demand) =="
exec "$PY" src/server.py --port "$PORT"
