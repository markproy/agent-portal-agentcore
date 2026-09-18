# Agent Portal for AWS Bedrock AgentCore Runtime

A local web app for creating, chatting with, and performance-testing hosted agents on [AWS Bedrock AgentCore Runtime](https://aws.amazon.com/bedrock/agentcore/).

You configure an agent through a form (name, model, instructions, tools), click Create, and within a few minutes it's a real hosted runtime you can chat with — no CLI, no CloudFormation, no per-agent IAM role. The portal also has a built-in load test for measuring cold-start latency and throughput across one or two agents, side by side.

---

## What you get

**Agent management**
- Create, edit (Recreate), and delete agents from a browser UI
- Two deployment modes per agent: **code zip** (default, fastest to deploy) or **container image** (ECR, slower deploy, different startup behavior)
- V1 (warm pool) and V2 (snapshot resume) runtime versions — V2 typically has significantly shorter session warmup

**Chat**
- Streaming responses rendered as Markdown
- Per-turn latency breakdown: warmup wait, first-token time, full response time
- Trace panel: full conversation transcript pulled from CloudWatch Logs

**Load test**
- **Platform-startup-only mode**: measures pure session-start cost with no LLM call — directly comparable across agents
- **Full chat mode**: real end-to-end latency including the model call
- Burst shape (N concurrent sessions) or Loop shape (one session at a time, up to 500 — enough for real percentiles)
- Live progress, p50/p75/p95/p99 charts, optional head-to-head comparison of two agents
- Stop mid-run and keep partial results

**Tools available to agents**
- Web search (DuckDuckGo, no setup needed)
- Stock prices and price history (yfinance)
- [AWS managed web search](#optional-aws-managed-web-search) via AgentCore Gateway (one-time setup, better results)
- Remote MCP servers (FRED economic data included; others configurable)

---

## Prerequisites

- Python 3.12+
- AWS credentials configured (`aws configure` or `AWS_PROFILE`)
- Bedrock model access enabled in your target region (at minimum: Claude Haiku)
- Docker (only for container-mode deploys — not needed for code-zip)

---

## One-time AWS setup

Two resources are shared across all portal-created agents:

**1. IAM execution role**

```bash
aws iam create-role \
  --role-name agent-portal-agentcore-execution \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam put-role-policy \
  --role-name agent-portal-agentcore-execution \
  --policy-name agent-portal-agentcore-execution-policy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        "Resource": [
          "arn:aws:bedrock:*:<account-id>:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      },
      {
        "Effect": "Allow",
        "Action": [
          "logs:CreateLogGroup", "logs:CreateLogStream",
          "logs:DescribeLogStreams", "logs:FilterLogEvents",
          "logs:GetLogEvents", "logs:PutLogEvents"
        ],
        "Resource": "arn:aws:logs:<region>:<account-id>:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        "Effect": "Allow",
        "Action": "logs:DescribeLogGroups",
        "Resource": "*"
      },
      {
        "Effect": "Allow",
        "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
        "Resource": "*"
      }
    ]
  }'
```

**2. S3 staging bucket** (must be in the same region you deploy agents into)

```bash
aws s3api create-bucket \
  --bucket <your-bucket-name> \
  --region us-east-1
# Add --create-bucket-configuration LocationConstraint=<region> for regions other than us-east-1
```

---

## Getting started

```bash
git clone https://github.com/markproy/agent-portal-agentcore.git
cd agent-portal-agentcore

python3 -m venv .venv && source .venv/bin/activate
pip install uv
uv pip install -r requirements.txt

cp .env.example .env
# Edit .env: set AGENTCORE_EXECUTION_ROLE_ARN and AGENTCORE_STAGING_BUCKET

./run.sh
```

The portal opens at `http://127.0.0.1:8910`.

**Seed three demo agents** (optional, takes ~4 min):

```bash
python scripts/seed_demo_agents.py
```

This creates `stock-analyst`, `trip-planner`, and `research-assistant` — all V2 code-zip agents with web search pre-configured, ready to chat with right away.

---

## Multi-region

To deploy agents into additional regions, add them to `.env`:

```
AGENTCORE_REGIONS=us-east-1,us-west-2
AGENTCORE_STAGING_BUCKET_US_WEST_2=your-us-west-2-bucket-name
```

Each region needs its own staging bucket. The New Agent form adds the extra regions as selectable deploy targets. Existing agents are unaffected — the portal reads each agent's region from its ARN, so chat, load test, and trace all follow an agent wherever it lives.

---

## Optional: container-mode deploys

Code zip is the default and needs no extra setup. Container mode builds a Docker image locally and pushes to ECR — useful for comparing cold-start behavior between the two artifact types.

One-time setup (idempotent):

```bash
./scripts/setup_aws_container.sh
```

This creates the ECR repository and grants the execution role permission to pull from it. Docker must be running. See [docs/aws.md](docs/aws.md#container-deploys) for details.

---

## Optional: AWS managed web search

The **Web search (AWS AgentCore Gateway)** tool uses AWS's own managed web search connector — better result quality and source citations than the default DuckDuckGo search.

One-time setup:

```bash
python scripts/setup_aws_web_search.py
```

This creates an AgentCore Gateway, attaches the Web Search Tool connector, and grants the execution role permission to invoke it. Set `AGENTCORE_WEB_SEARCH_GATEWAY_URL` in `.env` to the printed URL afterward.

Skip this and the tool stays greyed out in the form — the keyless DuckDuckGo **Web search** tool always works with no setup.

---

## MCP servers

Agents can mix local tools with remote MCP servers in the same tool-calling loop. The **FRED** server (Federal Reserve economic data) is pre-configured; deploy it with:

```bash
./scripts/deploy_fred_mcp.sh
```

Requires a free FRED API key from [stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html). The script deploys to Cloud Run, stores the key in Secret Manager, and prints the URL to put in `FRED_MCP_SERVER_URL`.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

The test suite covers the API/WebSocket protocol against a fully in-memory fake deployer (no real cloud calls), the SQLite data layer, load test math, and a slice of the frontend chart rendering via Node.js. Runs in CI on every PR.

For real-cloud smoke testing (deploys a throwaway agent, chats with it, deletes it):

```bash
./smoke/aws_smoke_test.sh
```

---

## Architecture

```
browser  ←→  server.py (FastAPI)  ←→  deployers/aws.py (boto3)  →  Bedrock AgentCore
              │                                                         Runtime
              ├── SQLite (agent_portal.db)
              └── perf/loadtest.py (load test engine, shared with CLI)
```

- **`server.py`** — FastAPI: agent CRUD, per-agent chat WebSocket, load test WebSocket, static file serving. Deploys run as background tasks (they take minutes); the frontend polls while any agent is `creating` or `deleting`.
- **`deployers/aws.py`** — all AgentCore interaction: create/update/delete runtimes, InvokeAgentRuntime streaming, CloudWatch trace queries. One shared IAM role and one S3 bucket across all agents.
- **`aws_hosted/main.py`** — the static entry point baked into every deployed agent zip/container. Reads model/instructions/tools from `agent_config.json` at runtime; reports session startup metrics back on a warmup ping so the portal can split cold-start time into platform cost vs. agent init cost.
- **`tools.py`** — tool implementations (`search_web`, `get_stock_price`, `get_price_history`). Copied into each deployment artifact so it runs inside the hosted container.
- **`static/`** — vanilla JS + CSS, no build step.
- **`perf/`** — `loadtest.py` powers both the portal's Load Test view and a standalone CLI (`python -m perf.loadtest --help`).

---

## Docs

- **[docs/aws.md](docs/aws.md)** — how deploy works, tracing, container mode, web search gateway
- **[docs/latency.md](docs/latency.md)** — interpreting the per-turn latency card, and the interactive load test
