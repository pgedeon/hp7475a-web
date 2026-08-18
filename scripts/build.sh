#!/usr/bin/env bash
# Production build: backend wheel-ish (editable is fine for single-host) +
# frontend dist bundle copied where the backend serves it.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> backend: install/verify"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -U pip; }
.venv/bin/pip install -e backend

echo "==> backend: tests"
.venv/bin/pytest backend/tests -q

echo "==> frontend: build"
(cd frontend && npm ci && npm run build)

echo "==> staging frontend dist for backend serving"
rm -rf backend/app/frontend_dist
cp -r frontend/dist backend/app/frontend_dist

echo "DONE — run: .venv/bin/uvicorn app.main:app --app-dir backend"
