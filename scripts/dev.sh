#!/usr/bin/env bash
# Development runner: backend (uvicorn, reload) + frontend (vite dev) together.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "==> creating venv + installing backend deps"
  python3 -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -e backend
fi

if [ ! -d frontend/node_modules ]; then
  echo "==> installing frontend deps"
  (cd frontend && npm install)
fi

echo "==> starting backend on http://127.0.0.1:8750"
(.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8750 --reload --app-dir backend) &
BACK_PID=$!

echo "==> starting frontend dev server on http://127.0.0.1:5173"
(cd frontend && npm run dev) &
FRONT_PID=$!

trap 'kill $BACK_PID $FRONT_PID 2>/dev/null || true' EXIT INT TERM
wait
