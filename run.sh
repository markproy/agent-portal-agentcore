#!/usr/bin/env bash
# Runs the portal: sets up the environment (once) and starts the FastAPI
# server, opening http://127.0.0.1:8910 in your browser.
set -euo pipefail
cd "$(dirname "$0")"
source ./_setup_env.sh

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Created .env -- fill in AGENTCORE_EXECUTION_ROLE_ARN and AGENTCORE_STAGING_BUCKET"
  echo "then re-run ./run.sh"
  echo "You also need AWS credentials configured (aws configure or AWS_PROFILE)."
  exit 1
fi

exec .venv/bin/python server.py
