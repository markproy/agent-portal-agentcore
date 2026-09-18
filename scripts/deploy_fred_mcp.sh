#!/usr/bin/env bash
# Deploys the FRED (economic data) remote MCP server to Cloud Run --
# stefanoamorelli/fred-mcp-server, pinned to a specific commit (not HEAD)
# for reproducibility. This is a one-time (or occasional, e.g. to pick up
# an upstream update) operation, separate from running the portal itself.
#
# What this needs, one-time, in the target GCP project:
#   - Secret Manager, Cloud Run, Cloud Build, Artifact Registry APIs enabled
#   - A Secret Manager secret named `fred-api-key` holding a real FRED API
#     key -- get one free at https://fred.stlouisfed.org/docs/api/api_key.html
#     (just an email signup, a couple minutes)
#   - The Cloud Run service's default compute service account granted
#     roles/secretmanager.secretAccessor on that secret
#
# This script handles all of the above except getting the API key itself,
# which only you can do (it's tied to your email). Run it once with
# FRED_API_KEY set the first time; safe to re-run to redeploy/update.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT (same project the portal itself uses)}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="fred-mcp-server"
SECRET_NAME="fred-api-key"
EXECUTION_SA_ROLE_MEMBER="serviceAccount:$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

# Pinned commit, not a branch -- see this file's docstring for why.
FRED_MCP_REPO="https://github.com/stefanoamorelli/fred-mcp-server.git"
FRED_MCP_COMMIT="cb1db30d6080df1ce24e6e6b193a67ea92a39fc2"

echo "==> Enabling required APIs (no-op if already enabled)..."
gcloud services enable secretmanager.googleapis.com run.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project="$PROJECT"

if ! gcloud secrets describe "$SECRET_NAME" --project="$PROJECT" >/dev/null 2>&1; then
  echo "==> Secret '$SECRET_NAME' doesn't exist yet."
  echo "    Get a free key from https://fred.stlouisfed.org/docs/api/api_key.html"
  read -r -s -p "    Paste your FRED API key (input hidden): " FRED_API_KEY
  echo
  TMPFILE="$(mktemp)"
  trap 'rm -f "$TMPFILE"' EXIT
  printf '%s' "$FRED_API_KEY" > "$TMPFILE"
  gcloud secrets create "$SECRET_NAME" --project="$PROJECT" \
    --data-file="$TMPFILE" --replication-policy=automatic
  rm -f "$TMPFILE"
  trap - EXIT
else
  echo "==> Secret '$SECRET_NAME' already exists, reusing it."
fi

echo "==> Granting the Cloud Run service account access to the secret..."
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --project="$PROJECT" \
  --member="$EXECUTION_SA_ROLE_MEMBER" --role="roles/secretmanager.secretAccessor" >/dev/null

echo "==> Fetching fred-mcp-server @ $FRED_MCP_COMMIT..."
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
git clone --quiet "$FRED_MCP_REPO" "$WORKDIR"
git -C "$WORKDIR" checkout --quiet "$FRED_MCP_COMMIT"

echo "==> Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT" \
  --source="$WORKDIR" \
  --region="$REGION" \
  --allow-unauthenticated \
  --set-env-vars=TRANSPORT=http \
  --set-secrets="FRED_API_KEY=${SECRET_NAME}:latest" \
  --min-instances=0 \
  --max-instances=2 \
  --memory=256Mi \
  --cpu=1

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
echo ""
echo "Deployed. MCP endpoint: ${SERVICE_URL}/mcp"
echo "Set FRED_MCP_SERVER_URL=${SERVICE_URL}/mcp in .env"
