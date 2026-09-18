#!/usr/bin/env python3
"""One-time setup for the "Web search (AWS AgentCore Gateway)" tool -- AWS's own
managed Web Search Tool, attached as an MCP connector target on an AgentCore
Gateway (docs/aws.md's "AWS web search via AgentCore Gateway").

Nothing else in the portal needs this: the keyless DuckDuckGo `web_search` tool
works with no setup, and every other AWS feature works with this never run.

Four resources, in dependency order. They were four aws-cli invocations to copy
out of docs/aws.md, which is a bad fit for three reasons: two of them need an id
that only appears in an earlier one's *response* (gatewayId, gatewayUrl); the
Gateway isn't immediately targetable after creation, so a copy-paste run hits a
timing error the docs don't mention; and step 4 silently decides whether
deployed agents can call the Gateway at all -- omit it and the agent deploys
clean, reports READY, then fails every single invocation with an
AccessDeniedException raised from inside the tool call.

  1. The Gateway's own service role, assumed by the AgentCore *service* to reach
     the Web Search backend on the Gateway's behalf. NOT the agents' role.
  2. The Gateway itself: MCP protocol, AWS_IAM inbound auth -- no Cognito user
     pool, because the only callers are this portal's own runtimes, which
     already hold SigV4 credentials from their execution role.
  3. The Web Search Tool connector, as a target on that Gateway.
  4. Permission for the *shared agent execution role* to invoke this specific
     Gateway. This is the caller-side grant and is deliberately separate from
     step 1's role: AWS's docs are explicit that InvokeGateway on the caller and
     on the Gateway's own service role are two unrelated grants.

Python, where scripts/setup_aws_container.sh is bash, for a reason that isn't
taste: `AWS_IAM` inbound auth is recent enough that a current standalone aws-cli
(2.31.2, checked) still ships an API model without it -- create-gateway there
accepts only CUSTOM_JWT and demands an --authorizer-configuration this setup has
nothing to put in. This repo's pinned boto3 does have it, so driving the calls
from the venv both works and guarantees the same SDK the portal itself deploys
with. It also gets .env parsing and region resolution by importing the deployer
rather than reimplementing them in sed.

Idempotent: safe to re-run. Existing resources are found and reused rather than
recreated, so a re-run after a partial failure completes the rest, and a re-run
with everything in place just reprints the gatewayUrl.
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Re-exec under this repo's venv, so `./scripts/setup_aws_web_search.py` works
# from a plain shell (the shebang can't name the venv: it's a relative path).
#
# Unconditionally, not just when boto3 is missing. Falling back to "whatever
# boto3 is importable" was tried and is actively worse than no fallback: the
# system-wide copy on this machine imports fine and even accepts AWS_IAM, but
# its API model predates connector targets, so the run got as far as creating
# the IAM role and the Gateway before dying on step 3 with a client-side
# "Unknown parameter in targetConfiguration.mcp: connector". A version-specific
# failure three writes deep is exactly what this must not do.
_VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
if _VENV_PYTHON.exists() and Path(sys.executable).resolve() != _VENV_PYTHON.resolve():
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), *sys.argv])

try:
    import boto3
    import botocore.exceptions
except ImportError:  # pragma: no cover - environment bootstrap
    sys.exit("boto3 isn't installed. Run: pip install -r requirements.txt")

sys.path.insert(0, str(ROOT))

# REGION/EXECUTION_ROLE_ARN come from the deployer itself rather than being
# re-read here, so this script and the running portal cannot disagree about
# which region or role they mean -- the disagreement being the whole reason
# AGENTCORE_REGION exists (see .env.example).
from deployers.aws import EXECUTION_ROLE_ARN, REGION  # noqa: E402

# AWS's own connector availability, not a portal limitation. Checked up front
# because in an unsupported region create-gateway *succeeds* and only
# create-gateway-target fails, leaving a useless Gateway behind. Widen as AWS
# adds regions.
SUPPORTED_REGIONS = ("us-east-1", "eu-west-1", "ap-northeast-1")

GATEWAY_NAME = "agent-portal-web-search"
GATEWAY_ROLE_NAME = "agent-portal-web-search-gateway-role"
GATEWAY_ROLE_POLICY_NAME = "agent-portal-web-search-gateway-policy"
TARGET_NAME = "web-search-tool"
INVOKE_POLICY_NAME = "agent-portal-web-search-invoke"
WEB_SEARCH_CONNECTOR_ID = "web-search"
ENV_VAR = "AGENTCORE_WEB_SEARCH_GATEWAY_URL"


def _gateway_trust_policy(account_id):
    """aws:SourceAccount + aws:SourceArn are the standard confused-deputy guard
    for a service principal: without them, a Gateway in any other account could
    ask AgentCore to assume this role."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/*"},
                },
            }
        ],
    }


def _gateway_role_policy(account_id):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeGateway",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeGateway",
                "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/*",
            },
            {
                # What makes the connector target work at all: the target
                # authenticates as this role (GATEWAY_IAM_ROLE below), so this
                # is the grant that actually reaches AWS's web index.
                "Sid": "InvokeWebSearch",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeWebSearch",
                "Resource": f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1",
            },
        ],
    }


def _invoke_policy(account_id, gateway_id):
    """Scoped to this one gateway id, not gateway/*: the portal's agents have no
    business calling any other Gateway, and this account holds several belonging
    to unrelated projects."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeWebSearchGateway",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeGateway",
                "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/{gateway_id}",
            }
        ],
    }


def ensure_gateway_role(iam, account_id):
    try:
        iam.get_role(RoleName=GATEWAY_ROLE_NAME)
        print(f"==> IAM role '{GATEWAY_ROLE_NAME}' already exists, reusing it.")
        created = False
    except iam.exceptions.NoSuchEntityException:
        print(f"==> Creating IAM role '{GATEWAY_ROLE_NAME}'...")
        iam.create_role(
            RoleName=GATEWAY_ROLE_NAME,
            Description="Assumed by AgentCore so agent-portal's web search Gateway can reach the Web Search Tool",
            AssumeRolePolicyDocument=json.dumps(_gateway_trust_policy(account_id)),
        )
        created = True

    # put_role_policy is an upsert keyed on the policy name, so re-running is
    # safe and this cannot touch any other policy on the role.
    print(f"==> Attaching inline policy '{GATEWAY_ROLE_POLICY_NAME}'...")
    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName=GATEWAY_ROLE_POLICY_NAME,
        PolicyDocument=json.dumps(_gateway_role_policy(account_id)),
    )
    return created


def find_gateway(control):
    """By name, from the account, rather than from anything cached locally --
    the account stays the single source of truth, so a Gateway deleted by hand
    is correctly recreated and one from a previous run is never duplicated.
    Gateway names are unique per region."""
    paginator = control.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gateway in page.get("items", []):
            if gateway.get("name") == GATEWAY_NAME:
                return gateway["gatewayId"]
    return None


def ensure_gateway(control, account_id, role_is_new):
    gateway_id = find_gateway(control)
    if gateway_id:
        print(f"==> Gateway '{GATEWAY_NAME}' already exists ({gateway_id}), reusing it.")
        return gateway_id

    print(f"==> Creating Gateway '{GATEWAY_NAME}' in {REGION}...")
    # create_gateway validates that it can assume the role, and a brand-new role
    # isn't assumable for a few seconds. Retried rather than slept on
    # unconditionally, so the common re-run path stays fast.
    attempts = 6 if role_is_new else 1
    for attempt in range(attempts):
        try:
            return control.create_gateway(
                name=GATEWAY_NAME,
                description="Managed AWS Web Search Tool for agent-portal agents",
                roleArn=f"arn:aws:iam::{account_id}:role/{GATEWAY_ROLE_NAME}",
                protocolType="MCP",
                # No authorizerConfiguration: AWS_IAM has nothing to configure,
                # unlike CUSTOM_JWT which needs a discovery URL and audiences.
                authorizerType="AWS_IAM",
            )["gatewayId"]
        except botocore.exceptions.ClientError as exc:
            retriable = exc.response["Error"]["Code"] == "ValidationException" and attempt < attempts - 1
            if not retriable:
                raise
            print("    role not assumable yet, retrying in 5s...")
            time.sleep(5)


def wait_until_ready(control, gateway_id):
    """create_gateway_target against a still-CREATING Gateway fails, so this is
    load-bearing on a first run, not a cosmetic progress check."""
    for _ in range(60):
        gateway = control.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway["status"]
        if status == "READY":
            return gateway["gatewayUrl"]
        if status in ("CREATE_FAILED", "FAILED", "DELETING"):
            reasons = "; ".join(gateway.get("statusReasons") or []) or "no reason given"
            sys.exit(f"Gateway {gateway_id} is {status}: {reasons}")
        print(f"    gateway is {status}, waiting...")
        time.sleep(5)
    sys.exit(f"Gateway {gateway_id} never became READY. Check the console.")


def ensure_target(control, gateway_id):
    paginator = control.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for target in page.get("items", []):
            if target.get("name") == TARGET_NAME:
                print(f"==> Target '{TARGET_NAME}' already exists ({target['targetId']}), reusing it.")
                return

    print(f"==> Attaching the Web Search Tool connector as target '{TARGET_NAME}'...")
    control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="AWS managed Web Search Tool",
        targetConfiguration={
            "mcp": {
                "connector": {
                    "source": {"connectorId": WEB_SEARCH_CONNECTOR_ID},
                    "configurations": [{"name": "WebSearch", "parameterValues": {}}],
                }
            }
        },
        # GATEWAY_IAM_ROLE: the target authenticates as the Gateway's own
        # service role from step 1, which is why that role needs
        # InvokeWebSearch. No API key or OAuth provider anywhere in this setup.
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )


def main():
    if not EXECUTION_ROLE_ARN:
        sys.exit("Set AGENTCORE_EXECUTION_ROLE_ARN in .env first (see .env.example).")
    if REGION not in SUPPORTED_REGIONS:
        sys.exit(
            f"The AgentCore web search connector isn't available in {REGION}.\n"
            f"Supported: {', '.join(SUPPORTED_REGIONS)}. Set AGENTCORE_REGION in .env to one\n"
            "of those (and make the staging bucket / ECR repository match it)."
        )

    # Everything after the last slash: works for a plain role ARN and for one
    # with a path (arn:aws:iam::123:role/some/path/my-role).
    execution_role_name = EXECUTION_ROLE_ARN.rsplit("/", 1)[-1]
    session = boto3.Session()
    # IAM is global, but pinning the region keeps every client in this script on
    # the one region resolved above rather than on an ambient default.
    iam = session.client("iam", region_name=REGION)
    control = session.client("bedrock-agentcore-control", region_name=REGION)
    account_id = session.client("sts", region_name=REGION).get_caller_identity()["Account"]

    print(f"==> Account {account_id}, region {REGION}, agent execution role {execution_role_name}")

    role_is_new = ensure_gateway_role(iam, account_id)
    gateway_id = ensure_gateway(control, account_id, role_is_new)
    gateway_url = wait_until_ready(control, gateway_id)
    ensure_target(control, gateway_id)

    print(f"==> Granting '{execution_role_name}' invoke access via inline policy '{INVOKE_POLICY_NAME}'...")
    iam.put_role_policy(
        RoleName=execution_role_name,
        PolicyName=INVOKE_POLICY_NAME,
        PolicyDocument=json.dumps(_invoke_policy(account_id, gateway_id)),
    )

    print("")
    print("Web search Gateway is set up. Put this in .env:")
    print("")
    print(f"  {ENV_VAR}={gateway_url}")
    print("")
    print('Then restart the portal. "Web search (AWS AgentCore Gateway)" stops being')
    print("greyed out in the New Agent form once it's set -- the portal reads .env at")
    print("startup, so an unrestarted portal still shows the tool as unconfigured.")
    print("")
    print("Agents already deployed without this tool don't gain it: the tool list is")
    print("baked into each runtime at deploy time. Recreate them to add it.")


if __name__ == "__main__":
    main()
