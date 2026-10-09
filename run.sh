#!/usr/bin/env bash
# Runs the whole project: fetch orbital data, compute positions, export the simulation data,
# screen conjunctions for 7 days, then serve the 3D simulation.
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

  echo "== 3/4 Screening conjunctions for the next 7 days (~4 min) =="
  "$PY" src/screen_conjunctions.py
fi

# Use the next free port if the requested one is taken (e.g. the simulation is already running).
port_free() { python3 -c "import socket, sys; s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1])))" "$1" 2>/dev/null; }
REQUESTED_PORT="$PORT"
while ! port_free "$PORT"; do
  PORT=$((PORT + 1))
  if [ "$PORT" -gt $((REQUESTED_PORT + 20)) ]; then
    echo "No free port between $REQUESTED_PORT and $PORT; set PORT=... to choose one." >&2
    exit 1
  fi
done
if [ "$PORT" != "$REQUESTED_PORT" ]; then
  echo "Port $REQUESTED_PORT is already in use (is the simulation already running there?); using $PORT instead."
fi

echo "== 4/4 Simulation running at http://127.0.0.1:$PORT (Ctrl+C to stop) =="
python3 -m http.server "$PORT" -d app --bind 127.0.0.1
