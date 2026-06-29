#!/usr/bin/env bash
# Launch the Polymarket Copy-Trader dashboard.
#   ./run.sh         -> paper mode against the live leaderboard
#   ./run.sh demo    -> offline demo mode (synthetic data, no network/wallet)
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [[ "${1:-}" == "demo" ]]; then
  export DEMO_MODE=true
  export INITIAL_LOOKBACK_MIN=10   # seed the dashboard with the last 10 min of synthetic trades
  echo ">> DEMO MODE (synthetic offline data, paper only)"
fi

# Activate a local venv if present.
if [[ -d ".venv" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec uvicorn app.web.app:app --host "$HOST" --port "$PORT"
