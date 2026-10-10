#!/usr/bin/env bash
# Runs the whole project in one command:
#   setup -> fetch orbital data -> positions -> simulation data -> 7-day conjunction screening -> ML risk ranking
#   -> historical replays -> satellite-communication (crosslink) run -> server for both simulations.
#
#   ./run.sh                 everything, then the server (first run also installs the Python environments)
#   ./run.sh --serve-only    just start the server with the existing data
#   ./run.sh --skip-ml       everything except the ML step (it takes ~10 min)
#   ./run.sh --verify        also run the accuracy tests before starting the server
#   PORT=9000 ./run.sh       use another port (default 8765)
#   HOURS=12 ./run.sh        simulate a longer satellite-communication window (default 6 h)
#
# Optional steps (ML, replays, satellite communication) never stop the run: a failure is reported and skipped.
# Then open http://127.0.0.1:8765 (main simulation); "Satellite communication" opens /autonomy/.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
ML_PY=ml/.venv/bin/python
PORT="${PORT:-8765}"
HOURS="${HOURS:-6}"
DEBRIS_GROUPS=(cosmos-2251-debris fengyun-1c-debris iridium-33-debris cosmos-1408-debris)
# Used when CelesTrak refuses the full active group (it allows one download per 2 hours).
FALLBACK_GROUPS=(active_gp_partial starlink_gp kuiper_gp qianfan_gp hulianwang_gp last-30-days_gp)
HISTORY_EVENTS=(iridium-cosmos-2009 cerise-1996 yunhai-2021)

SERVE_ONLY=0 SKIP_ML=0 VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --serve-only) SERVE_ONLY=1 ;;
    --skip-ml) SKIP_ML=1 ;;
    --verify) VERIFY=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see ./run.sh --help)" >&2; exit 2 ;;
  esac
done

step() { echo; echo "== $* =="; }
warn() { echo "!! $* (skipped; the rest of the run continues)" >&2; }

# Create a virtual environment and keep it in sync with its requirements file (reinstalls when the file changes).
ensure_env() {
  local dir="$1" reqs="$2"
  # Each command fails the function explicitly: `set -e` does not apply when it is called as a condition.
  if [ ! -x "$dir/bin/python" ]; then
    echo "Creating $dir"
    python3 -m venv "$dir" || return 1
  fi
  if ! cmp -s "$reqs" "$dir/.installed-requirements.txt"; then
    echo "Installing $reqs into $dir"
    "$dir/bin/pip" install -q --upgrade pip || return 1
    "$dir/bin/pip" install -q -r "$reqs" || return 1
    cp "$reqs" "$dir/.installed-requirements.txt" || return 1
  fi
}

step "Setup: Python environment"
ensure_env .venv requirements.txt

if [ "$SERVE_ONLY" -eq 0 ]; then
  step "1/7 Downloading orbital elements from CelesTrak and computing LEO positions"
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

  step "2/7 Exporting simulation data"
  if [ ${#DEBRIS[@]} -gt 0 ]; then
    "$PY" src/export_sim_data.py "${ACTIVE[@]}" --debris "${DEBRIS[@]}"
  else
    "$PY" src/export_sim_data.py "${ACTIVE[@]}"
  fi

  step "3/7 Screening conjunctions from today 00:00 UTC for 7 days (~4 min)"
  "$PY" src/screen_conjunctions.py --start "$(date -u +%Y-%m-%dT00:00:00Z)"

  step "4/7 ML risk ranking and avoidance manoeuvres (~10 min)"
  if [ "$SKIP_ML" -eq 1 ]; then
    echo "Skipped (--skip-ml)."
  elif [ ! -f ml/models/collision_risk_model.joblib ]; then
    warn "No trained model in ml/models (train it with ml/train.py, see README)"
  elif ! ensure_env ml/.venv ml/requirements.txt; then
    warn "Could not install ml/requirements.txt"
  elif ! "$ML_PY" ml/assess_conjunctions.py --print 20; then
    warn "ML ranking failed"
  fi

  step "5/7 Historical replays (past collisions)"
  for ev in "${HISTORY_EVENTS[@]}"; do
    if [ -f "app/data/history/$ev/leo_objects.json" ]; then
      echo "$ev: already built"
    elif [ -f "data/history/$ev/spacetrack_gp_history.csv" ] || [ -n "${SPACETRACK_USER:-}" ]; then
      "$PY" src/history.py run "$ev" || warn "Replay $ev failed"
    else
      echo "$ev: needs a Space-Track login once (SPACETRACK_USER / SPACETRACK_PASS), see README"
    fi
  done

  step "6/7 Satellite communication: simulating the crosslink network for ${HOURS} h (~1.5 min)"
  "$PY" autonomy/engine.py --hours "$HOURS" || warn "Satellite-communication run failed"

  if [ "$VERIFY" -eq 1 ]; then
    step "Accuracy tests"
    "$PY" tests/verify_conjunctions.py || warn "verify_conjunctions reported failures"
    "$PY" tests/verify_maneuver.py || warn "verify_maneuver reported failures"
    "$PY" tests/verify_browser_sgp4.py || warn "verify_browser_sgp4 reported failures (needs Node.js and network)"
  fi
fi

for f in app/data/leo_objects.json app/data/conjunctions.json; do
  if [ ! -f "$f" ]; then echo "$f is missing: run ./run.sh without --serve-only first." >&2; exit 1; fi
done
[ -f autonomy/data/run.json ] || echo "Note: no satellite-communication run yet (autonomy/data/run.json); its page will say so."

step "7/7 Starting the server: main simulation + satellite communication (/autonomy/)"
exec "$PY" src/server.py --port "$PORT"
