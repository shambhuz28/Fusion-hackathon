#!/usr/bin/env bash
# Crosslink simulation: satellites share orbit data within 50 km and avoid collisions on their own.
#
#   autonomy/run.sh              run the network engine if there is no run yet, then serve the page
#   autonomy/run.sh --rerun      run the engine again (e.g. after ./run.sh refreshed the satellite data)
#   HOURS=12 autonomy/run.sh --rerun   simulate a longer window (default 6 h)
#
# Needs the main project's data (app/data/leo_objects.json, app/data/conjunctions.json): run ./run.sh once first.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -f app/data/leo_objects.json ] || [ ! -f app/data/conjunctions.json ]; then
  echo "Run ./run.sh in the project first: the simulation uses its satellite data and screening." >&2
  exit 1
fi
if [ "${1:-}" = "--rerun" ] || [ ! -f autonomy/data/run.json ]; then
  echo "== Simulating the satellite network (~1 min for 6 h) =="
  "$PY" autonomy/engine.py --hours "${HOURS:-6}"
fi
exec "$PY" autonomy/serve.py --port "${PORT:-8770}"
