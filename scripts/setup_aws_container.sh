#!/usr/bin/env bash
# One-time setup for AWS *container-mode* deploys (docs/aws.md's "Container
# deploys"). Code-zip mode -- the default -- needs none of this; run this only
# if you want the container option in the New Agent form to work.
#
# Two things have to exist, and missing either one fails at a different time:
#
#   1. An ECR repository to push agent images to. Missing, the portal refuses
#      the deploy up front with a clear error.
#   2. Permission for the agents' shared execution role to *pull* from that
#      repository. Missing, the deploy looks fine -- image builds, image
#      pushes -- and then the runtime lands in CREATE_FAILED minutes later on
#      an opaque pull error. This is the one that wastes your time, which is
#      why it lives in a script next to the repository creation instead of
#      being a command in the docs you might not scroll to.
#
# Idempotent: safe to re-run. Both steps report "already ..." and change
# nothing if they've been done. Re-run it after changing AGENTCORE_REGION in
# .env -- ECR repositories are regional, so a region change needs a new one.
set -euo pipefail
cd "$(dirname "$0")/.."

# Read from .env the same way the portal does, so this script and the running
# portal can't disagree about which region/role/repository they mean. Values
# are extracted key-by-key rather than by sourcing the file: .env is data, and
# sourcing it would execute anything in it.
env_value() {
  local key="$1" default="${2-}" value=""
  if [ -f .env ]; then
    value="$(sed -n "s/^${key}=//p" .env | tail -n 1 | sed 's/[[:space:]]*$//')"
  fi
  # A real export wins over .env, matching python-dotenv's override=False.
  value="${!key-${value}}"
  printf '%s' "${value:-$default}"
}

# Same precedence as deployers/aws.py's _resolve_region(): AGENTCORE_REGION
# first, AWS_REGION only as a fallback. If this script read AWS_REGION directly
# it would happily create the ECR repository in whichever region some unrelated
# tool exported, while the portal deployed to the one .env asked for.
REGION="$(env_value AGENTCORE_REGION)"
[ -n "$REGION" ] || REGION="$(env_value AWS_REGION)"
ROLE_ARN="$(env_value AGENTCORE_EXECUTION_ROLE_ARN)"
REPOSITORY="$(env_value AGENTCORE_ECR_REPOSITORY agent-portal-agents)"

if [ -z "$REGION" ] || [ -z "$ROLE_ARN" ]; then
  echo "Set AGENTCORE_REGION and AGENTCORE_EXECUTION_ROLE_ARN in .env first (see .env.example)." >&2
  exit 1
fi

# Everything after the last slash: works for a plain role ARN and for one with
# a path (arn:aws:iam::123:role/some/path/my-role).
ROLE_NAME="${ROLE_ARN##*/}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
POLICY_NAME="agent-portal-ecr-pull"

echo "==> Account $ACCOUNT_ID, region $REGION, role $ROLE_NAME, repository $REPOSITORY"

if aws ecr describe-repositories --repository-names "$REPOSITORY" --region "$REGION" >/dev/null 2>&1; then
  echo "==> ECR repository '$REPOSITORY' already exists in $REGION, reusing it."
else
  echo "==> Creating ECR repository '$REPOSITORY' in $REGION..."
  aws ecr create-repository --repository-name "$REPOSITORY" --region "$REGION" \
    --query 'repository.repositoryUri' --output text
fi

# put-role-policy is an upsert on POLICY_NAME alone, so this cannot touch the
# role's main policy (agent-portal-agentcore-execution-policy) -- a separate,
# purpose-named inline policy rather than an edit of that one, so it's obvious
# what container mode added and removable on its own.
#
# Region is wildcarded in the resource ARN on purpose: deploying the portal
# against a second region needs another repository (see this file's header),
# and a role that only trusts one region's copy would fail the pull with the
# same opaque error this script exists to prevent.
echo "==> Granting '$ROLE_NAME' pull access via inline policy '$POLICY_NAME'..."
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" \
  --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PullAgentPortalImages",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "arn:aws:ecr:*:${ACCOUNT_ID}:repository/${REPOSITORY}"
    },
    {
      "Sid": "EcrAuthTokenHasNoResourceForm",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    }
  ]
}
EOF
)"

echo ""
echo "Container mode is set up. The remaining requirement is local: Docker has"
echo "to be running when you deploy (the image is built on your machine and"
echo "pushed from it). Check with: docker info"
echo "Then pick 'Container image (ECR)' under Deployment in the New Agent form."
