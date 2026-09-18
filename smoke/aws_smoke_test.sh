#!/usr/bin/env bash
# Verifies deployers/aws.py's mechanism -- see smoke/aws_smoke_test.py's
# docstring. Run this once before trusting AWS deploys live in the UI.
set -euo pipefail
cd "$(dirname "$0")/.."
source ./_setup_env.sh

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Created .env -- fill in AWS_REGION/AGENTCORE_EXECUTION_ROLE_ARN/AGENTCORE_STAGING_BUCKET"
  echo "if the defaults don't match your setup"
  echo "You also need to be signed in: aws login"
  exit 1
fi

exec env PYTHONPATH=. .venv/bin/python smoke/aws_smoke_test.py
