#!/usr/bin/env bash
# Verifies deployers/azure.py's mechanism -- see smoke/azure_smoke_test.py's
# docstring. Run this once before trusting Azure deploys live in the UI.
set -euo pipefail
cd "$(dirname "$0")/.."
source ./_setup_env.sh

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Created .env -- fill in FOUNDRY_PROJECT_ENDPOINT if the default doesn't match your setup"
  echo "You also need to be signed in: az login"
  exit 1
fi

exec env PYTHONPATH=. .venv/bin/python smoke/azure_smoke_test.py
