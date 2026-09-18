#!/usr/bin/env bash
# Verifies agent_engines.create() works when handed a dynamically-built
# Agent object -- see smoke/smoke_test.py's docstring. Run this once before
# trusting deployers/gemini.py's real deploy path.
set -euo pipefail
cd "$(dirname "$0")/.."
source ./_setup_env.sh

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Created .env -- fill in GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION"
  echo "if the defaults don't match your setup, then re-run ./smoke/smoke_test.sh"
  echo "You also need to be signed in: gcloud auth application-default login"
  exit 1
fi

exec env PYTHONPATH=. .venv/bin/python smoke/smoke_test.py
