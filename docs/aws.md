# AWS (Bedrock AgentCore Runtime)

How the AWS deployer works, the one-time AgentCore Gateway setup behind the
AWS-native web search tool, and how AWS traces reach the portal's trace panel.
Part of [Agent Portal](../README.md).

## How AWS deploy works

Deploys directly via boto3's `bedrock-agentcore-control` control-plane API
(`CreateAgentRuntime`/`GetAgentRuntime`/`UpdateAgentRuntime`/
`DeleteAgentRuntime`/`ListAgentRuntimes`) -- confirmed by inspecting the
real botocore service model before writing any code against it. This is
deliberately **not** the `agentcore` CLI + CDK path `~/Dev/AWS` itself
uses: that provisions a whole IAM role + CDK stack per deployed runtime,
which doesn't fit a portal deploying many differently-configured agents.
Instead, every portal-created AWS agent shares one IAM execution role and
one S3 staging bucket, set up once (the create-role/create-bucket commands
are in the main README's [How agent deploy
works](../README.md#how-agent-deploy-works)) -- shared across all portal-created agents.
`aws_hosted/main.py` is one static file parameterized per agent via an
`agent_config.json` written into the deployment artifact -- see [Why AWS agent config ships in the artifact](#why-aws-agent-config-ships-in-the-artifact).
See `deployers/aws.py`.

AgentCore's CodeZip build has no dependency-installation step at
all -- `deploy()` has to `uv pip install --target` the agent's
dependencies into the zip itself before upload, cross-targeted at the
container's actual platform since it's built from a developer's machine.
Several real, previously-unverified issues surfaced building this and got
fixed from live evidence, not guessed:

- This project's `uv`-managed venvs have no `pip` module installed inside
  them at all -- `uv pip install --target` replicates pip's semantics
  without needing pip present.
- Any `boto3` client construction failed immediately with
  `MissingDependencyException` on this machine's custom `aws login`
  credential provider, which needs `botocore[crt]` -- not just plain
  `boto3`.
- `CreateAgentRuntime` rejected a two-item `entryPoint` (`["python3",
  "main.py"]`) with an opaque "Invalid entrypoint value" error even though
  neither string violated any of the stated rules; a single-item
  `["main.py"]` (letting the managed Python runtime infer the
  interpreter) was accepted.
- A real failed deploy told us directly which CPU architecture AgentCore
  Runtime actually uses: "Your artifact contains binary files that are
  incompatible with Linux ARM64" -- it runs on Graviton, not x86_64, so
  the zip is now cross-targeted at `aarch64-unknown-linux-gnu`.
- `DeleteAgentRuntime` is asynchronous -- a resource can still briefly
  appear in `list_deployed()` right after a successful delete call before
  actually disappearing. `undeploy()` itself doesn't need to wait (nothing
  else depends on the delete being instant), but `smoke/aws_smoke_test.py`
  polls rather than checking once, since it does need to assert on the
  end state.

Verified via `smoke/aws_smoke_test.py` and a second pass through the real server API +
WebSocket, confirming multi-turn continuity (AgentCore Runtime sessions
are just a client-generated `runtimeSessionId` string threaded through
every call -- `aws_hosted/main.py`'s own in-process, session-keyed LRU
cache of `Agent` objects is what actually keeps conversation history, not
a platform-managed session store).

### Why AWS agent config ships in the artifact

Each agent's model, instructions, tool ids, MCP server URLs, and web search
Gateway URL travel in an `agent_config.json` written into the deployment
artifact (next to `main.py`, in the zip and in the image alike, by
`_agent_config`/`_write_agent_config`). `environmentVariables` carries only
`AWS_REGION`, which boto3 itself reads.

The obvious design is env vars, but two AgentCore-specific failures moved away from them:

- **A V2 runtime caps the entire `environmentVariables` payload at 1024
  bytes.** Instructions are a system prompt typed into a textarea and
  routinely exceed that on their own -- one agent's payload came to 1713
  bytes. Shortening the prompt only moves the wall, since every other field
  counts against the same 1024, so V2 was effectively unavailable to any
  agent with a realistic prompt.
- **AgentCore rejects control characters in env var values outright:**
  "Environment variable value contains invalid control characters
  (0x00-0x1F, 0x7F). Key: 'AGENT_INSTRUCTIONS'". A prompt from a textarea
  nearly always contains newlines, so essentially every deploy from the
  create-agent form failed -- with the newline named nowhere in the portal's
  own error surface. Working around it needed a matched escape/unescape pair
  in two separately-deployed files; JSON carries newlines natively, so that
  problem and both halves of that code are gone.

The tradeoff is real: env vars can be read back with `DescribeAgentRuntime`
and changed with `UpdateAgentRuntime`, while this file needs a rebuilt
artifact. It costs nothing here, because editing an agent in the portal
already recreates its runtime from a fresh artifact, and the portal's DB is
the source of truth for how an agent is configured either way.

`aws_hosted/main.py` indexes `CONFIG["..."]` directly rather than
defaulting missing keys, so a deployer that stopped writing one fails at
container startup instead of silently running an agent with no system
prompt. A test pins the written keys against the read ones
(`test_aws_agent_config_matches_what_the_hosted_agent_reads`), since the two
sides are separate files.

### Regions: the create picks one, everything else uses the ARN

Region is chosen per agent at create time and recorded on it; every later
operation recovers it from the agent's own ARN. `_resolve_region()` decides the
*default* -- what a create that doesn't name a region uses, and which region
startup seeding enumerates: `AGENTCORE_REGION` if set, else `AWS_REGION`, else
`us-east-1`. `deploy(region=...)` overrides it for one agent (see "Deploying
into more than one region" below). Every call about an *existing* agent -- invoke (chat,
warmup, load test), delete, and the CloudWatch Logs query behind the trace
panel -- takes its region from that runtime's own ARN instead
(`_region_of()`), so agents in more than one region work at the same time
from a single portal.

That split exists because of a real failure: an agent created while the
resolved region was `us-west-2` kept its `us-west-2` ARN in the portal's DB,
and after the portal moved to `us-east-1` every invoke of it answered
`ResourceNotFoundException: No endpoint or agent found with qualifier
'DEFAULT' for agent 'arn:aws:bedrock-agentcore:us-west-2:...'`. That error
reads like a broken runtime endpoint; the runtime was healthy and the
request was simply being sent to the wrong region's endpoint. Confirmed
fixed end-to-end afterward -- a `us-west-2` agent invoked from a
`us-east-1`-configured portal streams a real tool-using answer.

`AGENTCORE_REGION` is checked first because `AWS_REGION` turned out not to be
the portal's variable to read. It's a standard AWS SDK name, so other tooling
on the machine may export it for unrelated reasons -- here something exported
`us-west-2`, and since `load_dotenv()` uses `override=False`, that beat
`.env`'s `us-east-1` and silently split this portal's own agents across two
regions over several deploys. Nothing surfaced it: every deploy succeeded, in
the wrong place. `AGENTCORE_REGION` is project-owned, so nothing else sets it
and `.env` actually takes effect; `AWS_REGION` still works as a fallback but
can no longer override a deliberate choice. `scripts/setup_aws_container.sh`
and `scripts/setup_aws_web_search.py` resolve it the same way, so the ECR
repository and the web search Gateway can't land in a region the portal never
deploys to.

Two knock-on notes. Startup seeding only sees the resolved region, so an
agent elsewhere has to already be in the DB (or be re-added) to be usable --
`ListAgentRuntimes` is per-region. And the S3 staging bucket must be in the
resolved region too: it's the portal's own upload target during deploy, and a
bucket in another region fails `CreateAgentRuntime` with an opaque `S3
operation failed: Moved Permanently (Status Code: 301)`.

### Deploying into more than one region

Set `AGENTCORE_REGIONS` (comma-separated) and the New Agent form grows a Region
select, offering `AGENTCORE_REGION` plus whatever else is listed. Recreate
prefills it with where the agent already is, so an edit rebuilds an agent in
place; changing the select is how you deliberately move one. Each agent's card
and the load test's picker show its region, so two agents in different regions
can be compared head to head.

Leaving `AGENTCORE_REGIONS` unset keeps the portal exactly as it was: one
region, no Region select, no new setup.

Nothing about operating an agent needs configuration per region -- chat, delete,
trace and load test all read the region off the agent's ARN, and the IAM
execution role is global (its policies already wildcard the region:
`arn:aws:logs:*`, `arn:aws:ecr:*`). What *does* need one-time setup per region
is what a create writes to:

| Resource | Per region? | Setup |
| --- | --- | --- |
| S3 staging bucket (code-zip mode) | Yes | `aws s3api create-bucket --bucket <name> --region <region> --create-bucket-configuration LocationConstraint=<region>`, then `AGENTCORE_STAGING_BUCKET_<REGION_WITH_UNDERSCORES>=<name>` |
| ECR repository (container mode) | Yes | `AGENTCORE_REGION=<region> ./scripts/setup_aws_container.sh` |
| CloudWatch Transaction Search (traces) | Yes | Enabled per region -- see "How AWS traces work" |
| IAM execution role | No | Already region-agnostic |
| AgentCore Gateway (web search) | No | One gateway serves agents in any region |

The staging bucket really is per region, tested rather than assumed: creating a
`us-west-2` runtime from a `us-east-1` bucket fails with `ValidationException:
S3 operation failed: Moved Permanently (Service: S3, Status Code: 301)`, because
AgentCore reads the object with a client bound to its own region. Because that
message names neither the bucket nor the variable that fixes it -- and arrives
only after the zip has been built and uploaded -- `_check_staging_bucket()`
compares the two up front and fails with the `create-bucket` command and the
variable name instead. With the regional bucket in place the same deploy
succeeds in ~70s.

The shared Gateway works across regions because the hosted agent signs for the
gateway's region, not its own -- confirmed end to end, with a `us-west-2` agent
answering from a live search through the `us-east-1` gateway. SigV4 binds a
signature to a region, so an agent
in `us-west-2` signing for `us-west-2` would be rejected by a `us-east-1`
gateway; `aws_hosted/main.py`'s `_gateway_region()` parses the region out of the
gateway URL's own hostname
(`...gateway.bedrock-agentcore.us-east-1.amazonaws.com`) so there's no second
setting to keep in sync. The `agent-portal-web-search-invoke` policy names the
gateway's ARN and says nothing about the caller's region, so no IAM change is
needed either.

Two things the portal can't check for you:

- **The model list is US inference profiles.** The New Agent form offers
  `us.`-prefixed models, which only resolve in US regions. Picking a European or
  Asian region with one of those deploys cleanly and then fails every invoke.
- **Traces need Transaction Search enabled in that region** before an agent
  there produces spans. The agent works; the trace panel just stays empty.

## Container deploys

`CreateAgentRuntime`'s `agentRuntimeArtifact` is a union of exactly two
alternatives -- read off the real service model, not assumed:
`codeConfiguration` (a zip on S3 run by AgentCore's managed Python runtime)
or `containerConfiguration` (a `containerUri` pointing at an image in ECR).
The portal offers both. The New Agent form's **Deployment** field shows up for
AWS only (it's driven by each deployer module's `DEPLOYMENT_MODES`, so
platforms without the concept simply don't render it), the choice is stored on
the agent as `deployment_mode` and shown as a badge on its card and in the
load test's agent picker, and code zip stays the default.

The point is comparability. Both modes are built from the same
`aws_hosted/main.py` + `tools.py` and the same generated
`agent_config.json`, and every other argument to `CreateAgentRuntime` --
role, network/protocol config, and the whole `environmentVariables` map --
is produced by one shared `_create_runtime()`.
So a code-vs-container load test measures AgentCore's own packaging and
start-up behavior rather than two differently-configured agents. That is what
makes the interesting comparison possible: a container-backed runtime (which
the platform can keep warm) against a code-zip runtime's cold start, both
running byte-identical agent code.

The container itself needs nothing new from the agent code, because AgentCore
invokes an image over HTTP on port 8080 (`POST /invocations`, `GET /ping`) --
exactly what `BedrockAgentCoreApp` in `aws_hosted/main.py` already serves in
code-zip mode. `aws_hosted/Dockerfile` is therefore a thin wrapper: install
`requirements.txt`, copy the two source files, and start under
`opentelemetry-instrument` so traces work the same way in both modes (see
[How AWS traces work](#how-aws-traces-work)).

One-time setup, before the first container deploy:

```
./scripts/setup_aws_container.sh
```

It reads `AGENTCORE_REGION`, `AGENTCORE_EXECUTION_ROLE_ARN`, and
`AGENTCORE_ECR_REPOSITORY` straight out of `.env` (so it can't disagree with
the running portal about which region or role is meant), is idempotent, and
does the two things container mode needs:

1. **Creates the ECR repository** images are pushed to. ECR repositories are
   regional, so re-run the script after changing `AGENTCORE_REGION`.
2. **Grants the shared execution role permission to pull from it**, as a *new*
   inline policy (`agent-portal-ecr-pull`) that leaves the execution policy
   from the main README untouched.

Step 2 is the one worth automating rather than documenting. A runtime pulls its
image as that role, and the pull happens *after* the portal has already built
and pushed successfully -- so a missing policy doesn't fail the deploy, it
produces a `CREATE_FAILED` runtime minutes later on an opaque image error, with
nothing on screen connecting it to IAM. A missing *repository*, by contrast, is
caught up front before any build is paid for.

`AGENTCORE_ECR_REPOSITORY` overrides the repository name and
`AGENTCORE_CONTAINER_BUILDER` the builder command (default `docker`; anything
Docker-CLI-compatible works). Container mode also needs a running local Docker
daemon, since the image is built on the developer's machine -- which is why a
container deploy takes noticeably longer than a code-zip one.

Details worth knowing, each chosen for a reason rather than by convention:

- **Built for `linux/arm64`.** Same Graviton finding as the zip's
  `aarch64-unknown-linux-gnu` target above; an x86_64 image is rejected.
- **One immutable tag per deploy** (`<runtime-name>-<nanoseconds>`, never
  `:latest`). A runtime resolves its image at create time, so a mutable tag
  would let one agent's redeploy silently move the image another agent is
  supposedly running -- fatal for a mode-vs-mode comparison.
- **The build context is a temp staging directory** holding exactly the four
  files that ship, not the repo root (which carries `.venv`, `logs/`, and the
  portal's SQLite DB). Same shape as the zip build, and it keeps "what's in the
  image" readable without a `.dockerignore`.
- **The ECR password goes over stdin**, never in argv where `ps` would show
  it, and login happens after a successful build (tokens are short-lived, and
  an image that never built isn't worth authenticating for).
- The repository URI comes back from `describe_repositories` rather than being
  assembled from an account id, so nothing is guessed about registry hostnames
  (they differ in the China/GovCloud partitions).

## AWS web search via AgentCore Gateway

A second, more capable alternative to the simplified local `web_search`
tool (DuckDuckGo via `tools.py`, no auth, five results, best-effort): AWS's
own managed **Web Search Tool**, exposed as an MCP connector target on an
AgentCore Gateway -- an Amazon-operated web index spanning tens of billions
of documents, a knowledge graph for factual questions, source
citations/publish dates, and optional domain/date filtering, with queries
never leaving AWS infrastructure. AWS's own docs cover the feature in full:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html

Only available in `us-east-1`, `eu-west-1`, and `ap-northeast-1` as of this
writing (AWS's own connector availability, not a portal limitation) --
matches this project's default `AWS_REGION`.

Gated to AWS agents only, via `AVAILABLE_TOOLS`'s optional `"platforms"`
allowlist in `deployers/__init__.py`: the create-agent form only shows the
"Web search (AWS AgentCore Gateway)" checkbox once the AWS platform is
selected, and `server.py`'s `create_agent` rejects it server-side for any
other platform even if the API is called directly.

One-time setup, on top of the shared execution role/staging bucket above, is
a script (idempotent, resolves region/role by importing the deployer so it
can't disagree with the portal):

```
./scripts/setup_aws_web_search.py
```

It prints the `gatewayUrl` to put in `AGENTCORE_WEB_SEARCH_GATEWAY_URL`;
restart the portal afterwards, since `.env` is read at startup. What it
creates, and why this is a script rather than four commands to copy:

1. **The Gateway's own service role**
   (`agent-portal-web-search-gateway-role`). **Not** the same role as
   `agent-portal-agentcore-execution` -- it's assumed by the AgentCore
   *service* itself, to reach the Web Search backend on the Gateway's
   behalf, not by your deployed agents. Its trust policy is scoped with
   `aws:SourceAccount` + `aws:SourceArn`, the standard confused-deputy
   guard for a service principal.

2. **The Gateway** (`agent-portal-web-search`), MCP protocol with `AWS_IAM`
   (SigV4) inbound auth -- no Cognito user pool, since the only callers are
   this portal's own AgentCore Runtime agents, already authenticating with
   their execution role. Unlike `CUSTOM_JWT` there's no
   `authorizerConfiguration` to supply.

3. **The Web Search Tool connector**, as a target named `web-search-tool`
   with `credentialProviderType: GATEWAY_IAM_ROLE` -- the target
   authenticates as the role from step 1, which is why that role is the one
   needing `InvokeWebSearch`. No API key or OAuth provider is involved
   anywhere in this setup. The tool arrives at the agent named
   `web-search-tool___WebSearch` (Gateway targets prefix their tools), and
   takes `query` plus optional `maxResults` and domain/published-date
   filters.

4. **Invoke permission for the shared execution role** -- the one every
   portal-deployed AWS agent actually runs as -- scoped to this one gateway
   id rather than `gateway/*`. This is the caller-side grant, and is
   deliberately separate from step 1's role: AWS's docs are explicit that
   `InvokeGateway` on the caller and on the Gateway's own service role are
   two unrelated grants.

Three reasons it's scripted. Steps 3 and 4 need an id that only appears in
step 2's *response* (`gatewayId`, `gatewayUrl`). The Gateway isn't targetable
the instant it's created, and a new IAM role isn't immediately assumable, so
a copy-paste run hits timing errors -- the script polls for `READY` and
retries. And step 4 silently decides whether agents can call the Gateway at
all: omit it and the agent deploys clean, reports `READY`, then fails *every*
invocation with an `AccessDeniedException` raised from inside the tool call.

The script is Python where `setup_aws_container.sh` is bash, for a reason
that isn't taste: `AWS_IAM` inbound auth is recent enough that a current
standalone aws-cli (2.31.2, checked) still ships an API model without it --
`create-gateway` there accepts only `CUSTOM_JWT` and demands an
`--authorizer-configuration` this setup has nothing to put in. The machine's
system-wide boto3 was likewise new enough for `AWS_IAM` but too old to model
connector targets, which is why the script re-execs under this repo's `.venv`
unconditionally instead of using whichever boto3 happens to import.

`aws_hosted/main.py` connects to the Gateway using
[`mcp-proxy-for-aws`](https://github.com/aws/mcp-proxy-for-aws)'s
`aws_iam_streamablehttp_client`, which SigV4-signs the MCP streamable-HTTP
requests using the runtime's own execution-role credentials -- the same
no-separate-credential pattern the runtime already uses to call Bedrock
itself. A plain `MCPClient.load_servers()` connection (as used for the
config's `mcp_servers`/FRED elsewhere in this file) can't do this signing,
since the Gateway is IAM-authorized rather than open or OAuth/API-key like
FRED. Deploying an agent with this tool selected before
`AGENTCORE_WEB_SEARCH_GATEWAY_URL` is set fails loudly at deploy time
(`deployers/aws.py`'s `deploy()`) rather than silently shipping an agent
missing the tool it was configured with.

## How AWS traces work

Every chat turn is looked up afterward via **CloudWatch Logs Insights**
against the runtime's own log group -- no X-Ray API calls and no
`agentcore` CLI needed to read anything back, unlike `~/Dev/AWS/
show_traces.py`. This only works because
`aws_hosted/main.py`'s `entryPoint` is
`["opentelemetry-instrument", "main.py"]` rather than just `["main.py"]`:
Strands' own tracer plus AWS's botocore/Bedrock-runtime OTEL
auto-instrumentation then write structured `gen_ai.*` event records
straight into the same CloudWatch log group the app's own logs already go
to (`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, log stream
`otel-rt-logs`), keyed by a W3C trace ID. `deploy()`'s `InvokeAgentRuntime`
call generates that trace ID itself and passes it as the `traceParent`
header (AgentCore/X-Ray honor a caller-supplied trace ID), so a turn's
trace can be looked up directly by ID afterward instead of searching by
time window or session ID -- see `deployers/aws.py`'s
`_new_trace_parent()`/`get_trace()`.

Two real, confirmed findings shaped this design:

- **AgentCore Runtime never emits named/timed OTEL spans here** -- only
  the content-bearing `gen_ai.*` events. Verified directly (not assumed)
  by diffing a working CDK-deployed agent against a portal-deployed one,
  testing the highest-signal candidate difference (Python 3.14 vs 3.12
  managed runtime) with a real fresh deploy + chat + 15-minute wait, and
  still seeing zero spans. This is a real AWS platform limitation as of
  this writing, not a bug in this code -- so AWS's trace panel shows a
  single flat conversation transcript with no per-call duration, unlike
  Other platforms use span-based tracing
  natively).
- **`gen_ai.choice`'s event for a tool-use decision fires *before* the
  corresponding tool-call/result events for that same round**, not after
  -- an initial implementation that split the transcript into one "span"
  per round on that boundary produced an awkward, empty-looking first
  card. `get_trace()` instead renders one flat chronological transcript,
  the same approach `~/Dev/AWS/show_traces.py` already uses.
- A tool result is *also* re-emitted as a `gen_ai.user.message` event
  (Bedrock's own conversation-format quirk) -- a verbatim duplicate of the
  separate `gen_ai.tool.message` event. `get_trace()` filters
  `gen_ai.user.message` down to its `"text"` content parts only, skipping
  `toolResult` parts, to avoid rendering the same tool result twice.
- **A real turn-completion race**: querying immediately after a tool-use
  round's events have indexed, but before the final round's have, used to
  return a truthy-but-incomplete transcript -- missing the final answer
  entirely, confirmed live with a real deployed agent. Fixed by only
  treating a trace as ready once a `gen_ai.choice` event with a
  non-`tool_use` `finish_reason` (e.g. `end_turn`) has been seen;
  otherwise `get_trace()` returns `None` ("not ready yet") so the caller's
  existing retry/Refresh flow handles it the same as indexing lag.
- That `end_turn` check alone turned out not to be enough: the records
  index *independently and out of order*, so a query can see the final
  choice event before the user's own question. Reproduced live -- a query
  at t+79s returned a single line (the answer) for a turn whose full
  four-line transcript was there moments later. A trace now also has to
  contain a `"You: "` line to count as ready.
- Indexing lag here is **60-90 seconds**, not the "a few seconds" the UI
  used to imply: on one measured turn, `get_trace()` returned `None` at
  t+14s and t+47s and the full transcript at t+79s. `server.py`'s
  `_TRACE_RETRY_DELAYS` is sized for that real number.

Verified end-to-end against real freshly-deployed test agents (each
cleaned up via `undeploy()` afterward): a tool-calling turn's full
transcript -- user message, tool call, tool result, and final answer --
renders correctly, and a premature query genuinely returns "not ready"
instead of a partial result.
