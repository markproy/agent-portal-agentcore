"""Agent Portal backend: FastAPI app serving the static UI, agent CRUD
(deploys are real cloud calls so they run as background tasks with status
polling), and a per-agent chat WebSocket.

Run: ./run.sh -- opens http://127.0.0.1:8910 in your browser.
"""

import asyncio
import json
import os
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from perf import loadtest
from deployers import (
    AVAILABLE_DEPLOYMENT_MODES,
    AVAILABLE_MCP_SERVERS,
    AVAILABLE_TOOLS,
    PLATFORMS,
    tool_allowed_for_platform,
    unconfigured_reason,
)
from deployers import aws as aws_deployer

ROOT = Path(__file__).parent

DEPLOYERS = {"aws": aws_deployer}

# The interactive Load Test view drives real WebSocket chat sessions against
# *this same server*, exactly the way perf/loadtest.py's CLI already does against
# a running portal -- so it needs the address this process is itself
# listening on, matching the literal host/port passed to uvicorn.run() at
# the bottom of this file (kept as one named constant so the two can't
# silently drift apart).
SELF_HOST = "127.0.0.1:8910"

# It fires real requests against a real hosted agent (real cloud
# cost/quota), so these stay deliberately tight rather than "generous."
# Confirmed live why the combined cap matters, not just per-field ones: a
# single request with users=9999/iterations=9999 was silently accepted and
# clamped down to a real 50 x 50 = 2500-session run under an earlier,
# looser version of these caps (each field individually "reasonable"), and
# had to be cancelled by hand mid-run. The total-session cap closes that gap
# directly instead of trusting the product of two per-field caps to stay
# small. static/app.js's number inputs mirror these as min/max so the form
# itself steers away from it first.
#
# Concurrency is what protects any one hosted agent from a runaway burst, so
# it stays at 20. The *total* is what costs money, and what a session costs
# depends on the mode -- hence two ceilings rather than one: a
# platform-startup-only session makes no LLM call at all on AWS/Gemini (see
# perf/loadtest.py's MODE_WARMUP_ONLY), so 500 of them are cheap enough to be
# worth it for the sample size, while 500 full chat turns are 500 real LLM
# calls and stay off the table. The per-field iteration cap is no longer the
# binding one for either mode -- the product cap is -- but it's kept as the
# outer bound on a single field.
LOADTEST_MAX_USERS = 40
LOADTEST_MAX_ITERATIONS = 500
LOADTEST_MAX_TOTAL_SESSIONS = 60
LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY = 500

# Every turn's TTFA/total-latency, one JSON object per line -- consumed by
# perf/latency_monitor.py's live p95/threshold view. Provider-agnostic (platform
# and agent name are just whatever's in the row), so this already covers
# Azure/Gemini turns too, not just AWS.
LATENCY_LOG_PATH = ROOT / "logs" / "latency.jsonl"


def _exc_detail(exc):
    """str(exc), except for exception types whose own __str__ is empty --
    confirmed directly that both TimeoutError() and asyncio.TimeoutError()
    stringify to "" with no message at all. A falsy "" then reads as *no
    error* everywhere downstream that checks truthiness (this server's own
    error-vs-not branching, the load test's compute_summary(), the
    frontend's `if (msg.error)`) -- found live: a real 60s client timeout
    during a load test's "warmup_only" call surfaced as a fake instant
    "session ready in 0.0s" success instead of a visible error, because
    nothing in the chain ever checked for this. repr(exc) always includes
    the exception's class name, so it's never empty."""
    return str(exc) or repr(exc)


def _log_latency(
    agent, turn, ttfa_ms, elapsed_seconds, warmup_ms=None, tool_calls=(), agent_init_ms=None,
    platform_startup_ms=None, cold_start_ms=None, client_queue_ms=None,
):
    """Best-effort: a logging failure (disk full, permissions) should never
    interrupt a chat turn, so this only ever logs and moves on."""
    try:
        LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "agent_id": agent["id"],
            "agent_name": agent["name"],
            "platform": agent["platform"],
            "turn": turn,
            "ttfa_ms": ttfa_ms,
            "elapsed_ms": round(elapsed_seconds * 1000),
            "warmup_ms": warmup_ms,
            "agent_init_ms": agent_init_ms,
            "platform_startup_ms": platform_startup_ms,
            "cold_start_ms": cold_start_ms,
            # Kept alongside the rest so a latency record can be re-read later
            # knowing whether this process was itself the slow part -- see
            # deployers/__init__.py's latest_client_queue_ms.
            "client_queue_ms": client_queue_ms,
            "tool_call_count": len(tool_calls),
            "tool_calls": list(tool_calls),
        }
        with LATENCY_LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


# Original config for the two real agents already deployed earlier in
# ~/Dev/Gemini (see its common.py/tools.py) -- backfilled here so seeding
# shows accurate info for them instead of "(imported, unknown)". Any other
# pre-existing reasoningEngine found on startup falls back to the generic
# unknown-config path below.
_STOCK_ANALYSIS_INSTRUCTIONS = (
    "You are a concise stock analysis assistant. You have three tools: "
    "search_web for current news/events, get_stock_price for the latest price, "
    "and get_price_history for performance/trend questions over a lookback window "
    "(use this instead of web search whenever a question involves 'how has X performed', "
    "'over the last N days/weeks/months', or comparing performance between tickers). "
    "Use them whenever a question needs current information you don't already know. "
    "Cite concrete numbers and be direct."
)
# Keyed by (platform, name) for future-proofing if the same agent name appears across platforms.
KNOWN_LEGACY_AGENTS = {
    "aws": {
        "stockagent_stock_analysis_agent": {
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "description": "Stock analysis agent (AgentCore Runtime, imported from ~/Dev/AWS).",
            "agent_instructions": _STOCK_ANALYSIS_INSTRUCTIONS,
            "tools": ["web_search", "stock_data"],
        },
    },
}

_FALLBACK_MODEL_BY_PLATFORM = {
    "aws": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def seed_existing_agents():
    """One-time-per-startup discovery pass: import any already-deployed
    agent this portal doesn't know about yet, so the list isn't empty just
    because the portal itself didn't create them."""
    for platform, deployer in DEPLOYERS.items():
        try:
            engines = deployer.list_deployed()
        except Exception as exc:
            print(f"[seed] skipping {platform}: {exc}")
            continue
        for engine in engines:
            if db.get_agent_by_resource_id(engine["resource_id"]):
                continue
            known = KNOWN_LEGACY_AGENTS.get(platform, {}).get(engine["name"])
            if known:
                db.insert_seeded_agent(
                    name=engine["name"],
                    platform=platform,
                    model=known["model"],
                    description=known["description"],
                    agent_instructions=known["agent_instructions"],
                    tools=known["tools"],
                    platform_resource_id=engine["resource_id"],
                )
            else:
                db.insert_seeded_agent(
                    name=engine["name"],
                    platform=platform,
                    model=_FALLBACK_MODEL_BY_PLATFORM.get(platform, "unknown"),
                    description="(imported — original config unknown)",
                    agent_instructions="",
                    tools=["web_search", "stock_data"],
                    platform_resource_id=engine["resource_id"],
                )
            print(f"[seed] imported {platform} agent {engine['name']!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Discovery seeding is a convenience for an account whose deployed
    # agents were all created by this portal's own lineage of projects. On
    # an account that also hosts a lot of unrelated pre-existing agents it
    # works against the user: the portal fills up with rows it knows
    # nothing about ("(imported — original config unknown)"), Chat often
    # can't work against them because their payload contract isn't this
    # portal's, and -- the part that actually matters -- the Delete button
    # on such a row issues a real DeleteAgentRuntime against another
    # project's resource. AGENT_PORTAL_NO_SEED keeps the list to agents
    # this portal created itself; clearing the imported rows out of the DB
    # is otherwise undone by the very next restart.
    if os.environ.get("AGENT_PORTAL_NO_SEED", "").lower() in ("1", "true"):
        print("[seed] skipped: AGENT_PORTAL_NO_SEED is set")
    else:
        await asyncio.to_thread(seed_existing_agents)
    # Opening the browser here (lifespan startup), rather than before
    # calling uvicorn.run() below, guarantees uvicorn has already bound
    # the socket -- it does that before running the ASGI app's lifespan
    # startup -- so there's no "connection refused" window. Same fix
    # ~/Dev/AWS/demo_web.py already uses. AGENT_PORTAL_NO_BROWSER lets
    # this be suppressed for repeated dev-loop restarts without changing
    # the normal ./run.sh experience.
    if os.environ.get("AGENT_PORTAL_NO_BROWSER", "").lower() not in ("1", "true"):
        webbrowser.open("http://127.0.0.1:8910")
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class CreateAgentRequest(BaseModel):
    name: str
    platform: str
    model: str
    description: str = ""
    agent_instructions: str = ""
    tools: list[str] = []
    mcp_servers: list[str] = []
    # Only meaningful on a platform that offers more than one way to package
    # the same agent (today: AWS -- code zip vs container image, see
    # deployers/__init__.py's DEPLOYMENT_MODES). None means "that platform's
    # own default", which is what every request from a platform without the
    # concept sends.
    deployment_mode: str | None = None
    # Which region to create the agent in, on a platform that offers the choice
    # (today: AWS -- see deployers/__init__.py's deployment_regions()). None
    # means that platform's default region, which is what every request from a
    # platform without the concept sends. Only ever needed at create time:
    # every later operation reads the region back off the agent's resource id.
    region: str | None = None
    # AWS only: AgentCore Runtime platform version ("V1"/"V2"). None means
    # "let the platform decide", which is what every non-AWS platform sends,
    # since the concept doesn't exist there. Independent of deployment_mode
    # above -- the pair is the point (a V1 container against a V2 code zip is
    # exactly the comparison this portal exists to make).
    runtime_version: str | None = None


def _run_deploy(agent_id, req: CreateAgentRequest):
    deployer = DEPLOYERS[req.platform]
    # Each passed only when actually requested:
    # plain deploy() signature described in deployers/__init__.py rather than
    # growing parameters they'd ignore. For deployment_mode and region,
    # create_agent has already rejected a value the platform doesn't offer, so
    # anything here is something this deployer accepts; a runtime version is
    # meaningless on Vertex AI Agent Engine and Azure AI Foundry, hence the
    # platform check on that one.
    extra = {}
    if req.deployment_mode:
        extra["deployment_mode"] = req.deployment_mode
    if req.region:
        extra["region"] = req.region
    if req.platform == "aws" and req.runtime_version:
        extra["runtime_version"] = req.runtime_version
    try:
        # AgentCore requires description to be non-empty; fall back to the agent name.
        description = req.description.strip() or req.name
        resource_id = deployer.deploy(
            req.name, req.model, description, req.agent_instructions, req.tools, req.mcp_servers, **extra
        )
        db.set_status(agent_id, db.STATUS_ACTIVE, platform_resource_id=resource_id)
    except Exception as exc:
        db.set_status(agent_id, db.STATUS_FAILED, error_message=_exc_detail(exc))


def _still_deployed(deployer, resource_id):
    """Whether the platform still has this resource.

    Prefers the deployer's own resource_exists() when it has one, because
    list_deployed() is not always able to answer: AWS's is per-region (there is
    no cross-region ListAgentRuntimes), so for an agent deployed outside the
    default region it can't contain the resource at all and every check would
    read as "already gone" -- silently skipping the wait that the recreate flow
    below depends on. resource_exists() asks about the one resource by id, in
    its own region.

    getattr rather than requiring it everywhere."""
    exists = getattr(deployer, "resource_exists", None)
    if exists:
        return exists(resource_id)
    return any(r["resource_id"] == resource_id for r in deployer.list_deployed())


def _wait_until_actually_gone(deployer, resource_id, attempts=180, interval_seconds=2):
    """Confirms the resource is actually gone from the platform itself, not just
    that undeploy()'s API call was accepted (see _still_deployed for how that's
    asked).

    AWS's delete_agent_runtime() is documented as asynchronous (see
    docs/aws.md's "How AWS deploy works"): a resource can still briefly report
    as present right after a successful delete call, before actually
    disappearing. Gemini's/Azure's own delete calls already block
    until the platform confirms completion, so this is normally a no-op
    single check for them -- checked generically here, rather than only
    for AWS, so that guarantee doesn't quietly become load-bearing without
    being verified. This closes a real gap the Edit -> Recreate flow
    depends on: it fires a new create only once the old agent's row is
    gone from this portal's own DB (server.py's delete_agent endpoint,
    polled by static/app.js's waitForDeleted()), and without this check
    that row disappeared as soon as undeploy() merely returned -- which
    for AWS could be before the old AgentRuntimeName was actually freed,
    risking a real naming collision on the recreate.

    The budget is 6 minutes because that is what a real AgentCore delete costs:
    the previous 30-attempt / 60s budget was set when a DELETING runtime was
    (wrongly) read as already gone by resource_exists, so it never had to cover
    an actual teardown. It stays well under static/app.js's waitForDeleted(),
    which is what decides when the recreate's create call fires.

    Best-effort and silently gives up on timeout (still lets the delete
    complete either way -- see the caller) rather than raising, since a
    slow-to-reflect platform view shouldn't hang this portal's own
    accounting of a delete that the platform already accepted."""
    for _ in range(attempts):
        try:
            still_there = _still_deployed(deployer, resource_id)
        except Exception:
            return  # can't check -- don't block the delete on it
        if not still_there:
            return
        time.sleep(interval_seconds)


def _run_undeploy(agent_id, platform, resource_id):
    deployer = DEPLOYERS[platform]
    try:
        deployer.undeploy(resource_id)
        _wait_until_actually_gone(deployer, resource_id)
        db.delete_agent(agent_id)
    except Exception as exc:
        db.set_status(agent_id, db.STATUS_ACTIVE, error_message=f"Delete failed: {exc}")


@app.get("/")
async def index():
    return HTMLResponse((ROOT / "static" / "index.html").read_text())


def _config_status(env_vars):
    """The {available, unavailable_reason} pair the frontend greys a choice out
    with. One shape for platforms, tools, and MCP servers alike, so the form has
    a single way to render "you can't pick this, and here's the fix" -- see
    deployers/__init__.py's unconfigured_reason()."""
    reason = unconfigured_reason(env_vars)
    return {"available": not reason, "unavailable_reason": reason}


def _platform_status(platform_id):
    """Whether a platform can actually be used right now, which is two separate
    questions: is it implemented at all (PLATFORMS' own static "available"), and
    is *this* checkout configured for it (its deployer's REQUIRED_CONFIG).

    Evaluated per request rather than once at import so that filling in .env and
    reloading the page is enough -- and because the answer is about local setup,
    which isn't a property of the code the way "implemented" is."""
    if not PLATFORMS[platform_id]["available"]:
        return {"available": False, "unavailable_reason": "Not implemented in this portal yet."}
    return _config_status(DEPLOYERS[platform_id].REQUIRED_CONFIG)


def _tool_status(tool):
    """A tool is unavailable when the one-time setup behind it hasn't been done
    -- only meaningful for a tool that *has* external setup, which its optional
    "requires_env" declares. A plain callable in tools.py needs nothing and is
    always available."""
    required = tool.get("requires_env")
    return _config_status((required,) if required else ())


@app.get("/api/config")
async def get_config():
    """Everything the create-agent form renders, with each choice annotated by
    whether it can actually be used (see _platform_status/_tool_status). The
    form disables what can't work and shows the reason, rather than accepting
    the choice and failing at deploy time -- or at the agent's first invoke,
    which is what an unconfigured remote tool used to produce.

    Each platform's own deployment_modes and regions are merged in from its
    deployer module rather than duplicated into PLATFORMS, so the form can show
    the Deployment and Region fields for whichever platforms actually offer
    those choices without hardcoding "aws" in the frontend."""
    platforms = {
        platform_id: {
            **meta,
            **_platform_status(platform_id),
            "deployment_modes": list(DEPLOYERS[platform_id].DEPLOYMENT_MODES),
            "regions": list(DEPLOYERS[platform_id].deployment_regions()),
        }
        for platform_id, meta in PLATFORMS.items()
    }
    return {
        "platforms": platforms,
        "tools": {tool_id: {**tool, **_tool_status(tool)} for tool_id, tool in AVAILABLE_TOOLS.items()},
        "mcp_servers": {
            server_id: {**server, **_config_status((server["env_var"],))}
            for server_id, server in AVAILABLE_MCP_SERVERS.items()
        },
        "deployment_modes": AVAILABLE_DEPLOYMENT_MODES,
    }


@app.get("/api/agents")
async def list_agents():
    return db.list_agents()


@app.post("/api/agents", status_code=201)
async def create_agent(req: CreateAgentRequest, background_tasks: BackgroundTasks):
    if req.platform not in DEPLOYERS:
        raise HTTPException(400, f"Unknown platform {req.platform!r}")
    platform_status = _platform_status(req.platform)
    if not platform_status["available"]:
        raise HTTPException(400, f"Platform {req.platform!r} isn't available: {platform_status['unavailable_reason']}")
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    bad_tools = [t for t in req.tools if not tool_allowed_for_platform(t, req.platform)]
    if bad_tools:
        raise HTTPException(400, f"Tool(s) {bad_tools} aren't available on platform {req.platform!r}")
    # Re-checked here and not left to the form, because the form isn't the only
    # caller and because of what this specific mistake costs: a tool or MCP
    # server pointed at a placeholder endpoint deploys cleanly, reaches READY,
    # and then fails every single invoke -- the hosted agent treats a tool that
    # won't load as fatal rather than starting without it. Cheaper to refuse the
    # create than to leave a permanently broken agent that looks healthy.
    for tool_id in req.tools:
        reason = _tool_status(AVAILABLE_TOOLS[tool_id])["unavailable_reason"]
        if reason:
            raise HTTPException(400, f"Tool {tool_id!r} isn't configured: {reason}")
    for server_id in req.mcp_servers:
        if server_id not in AVAILABLE_MCP_SERVERS:
            raise HTTPException(400, f"Unknown MCP server {server_id!r}")
        reason = unconfigured_reason((AVAILABLE_MCP_SERVERS[server_id]["env_var"],))
        if reason:
            raise HTTPException(400, f"MCP server {server_id!r} isn't configured: {reason}")
    # Rejected rather than ignored: a request asking for a container deploy on
    # a platform that can't do one, silently answered with that platform's
    # default packaging, would leave a row this portal labels as something it
    # isn't. The form only offers modes the platform supports; the API itself
    # otherwise wouldn't check (same reasoning as the tool allowlist above).
    supported_modes = DEPLOYERS[req.platform].DEPLOYMENT_MODES
    if req.deployment_mode and req.deployment_mode not in supported_modes:
        raise HTTPException(
            400, f"Deployment mode {req.deployment_mode!r} isn't available on platform {req.platform!r}"
        )
    # Rejected rather than passed through, because a region is expensive to get
    # wrong: a typo'd one is a well-formed create that fails only after a full
    # container build and push, and a region this account has no ECR repository
    # or model access in fails the same way or later, at first invoke. The
    # allowlist is the deployer's own (see deployment_regions()).
    supported_regions = DEPLOYERS[req.platform].deployment_regions()
    if req.region and req.region not in supported_regions:
        raise HTTPException(
            400, f"Region {req.region!r} isn't available on platform {req.platform!r}"
        )
    agent_id = db.create_agent(
        req.name, req.platform, req.model, req.description, req.agent_instructions, req.tools, req.mcp_servers,
        # Stored so the agent list, and the load test's own agent picker, can
        # tell two otherwise-identical agents apart -- which is the whole
        # point of being able to deploy one of each. "" for a platform with no
        # choice to record, matching the column's own convention (see db.py);
        # otherwise the mode this deploy will actually use, which for an
        # unspecified one is the platform's own first-listed default (the same
        # value deploy() itself falls back to) rather than a blank that would
        # under-report a real code-zip deploy.
        deployment_mode=(req.deployment_mode or supported_modes[0]) if supported_modes else "",
        # Same treatment for the same reasons, plus one specific to region: a
        # create that fails has no resource id to read a region back off, and
        # that row is exactly where knowing the region matters (see db.py).
        # First-listed is the deployer's own default, so an unspecified region
        # records where the deploy will really go rather than a blank.
        region=(req.region or supported_regions[0]) if supported_regions else "",
    )
    background_tasks.add_task(_run_deploy, agent_id, req)
    return db.get_agent(agent_id)


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = db.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@app.delete("/api/agents/{agent_id}", status_code=202)
async def delete_agent(agent_id: str, background_tasks: BackgroundTasks):
    agent = db.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if not agent["platform_resource_id"]:
        # Never finished deploying (or already failed) -- nothing hosted to
        # tear down, just drop the row.
        db.delete_agent(agent_id)
        return {"status": "deleted"}
    db.set_status(agent_id, db.STATUS_DELETING)
    background_tasks.add_task(_run_undeploy, agent_id, agent["platform"], agent["platform_resource_id"])
    return {"status": "deleting"}


async def _send_usage(websocket, deployer, session_state, turn, usage_by_turn):
    """Fetches token usage (may involve a short wait -- see
    deployers/__init__.py's interface docstring) and sends it tagged with
    the turn it belongs to, since by the time this resolves the user may
    already be mid-way through the next turn. Best-effort: a WebSocket
    that's since closed, or a platform that never got usage for this turn,
    just means no message goes out -- not worth surfacing as an error for
    a nice-to-have stat.

    Also computes input_tokens_delta against turn-1's usage (keyed by turn
    number in usage_by_turn, not "whichever usage arrived most recently" --
    these run as unawaited background tasks per turn, so out-of-order
    resolution is possible if turns come in quick succession; keying by
    turn number sidesteps that instead of trusting arrival order) -- this is
    the "is the conversation's context growing turn over turn" signal for
    the latency breakdown, since a ballooning input token count on an
    otherwise-unremarkable turn is itself a real explanation for why that
    turn was slower than the last one."""
    try:
        usage = await deployer.latest_usage(session_state)
        if usage:
            prev = usage_by_turn.get(turn - 1)
            payload = {"type": "usage", "turn": turn, **usage}
            if prev and usage.get("input_tokens") is not None and prev.get("input_tokens") is not None:
                payload["input_tokens_delta"] = usage["input_tokens"] - prev["input_tokens"]
            usage_by_turn[turn] = usage
            await websocket.send_json(payload)
    except Exception:
        pass


# How long after a turn the automatic attempts keep going, and the gap
# before each one. Measured directly rather than guessed: on AgentCore the
# gen_ai.* event records take **60-90 seconds** to become queryable in
# CloudWatch Logs Insights (reproduced end to end -- not_ready at t+14s and
# t+47s, the full trace at t+79s). One immediate attempt, which is what this
# used to do, therefore *always* lost that race, leaving a Refresh button as
# the only way to ever see a trace and no hint of how long to wait -- so in
# practice the trace panel looked permanently broken.
#
# The first attempt is still immediate: Gemini's and Azure's telemetry can
# genuinely be ready by then, and there's no reason to make them wait on
# AWS's indexing latency. The later ones cost nothing but a query when the
# trace is already in hand, since the loop stops as soon as one lands.
_TRACE_RETRY_DELAYS = (0, 15, 15, 20, 20, 30, 30, 30)


async def _fetch_and_send_trace_until_ready(websocket, deployer, resource_id, session_state, turns, turn):
    """Automatic attempts on the schedule above, stopping at the first one
    that lands. Only the *last* failure tells the client "not ready, here's
    a Refresh button" -- the ones before it report "indexing", which the
    client shows as a plain wait message, because offering a manual retry
    while an automatic one is already queued invites exactly the clicking
    that won't help."""
    for i, delay in enumerate(_TRACE_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        last = i == len(_TRACE_RETRY_DELAYS) - 1
        try:
            if await _fetch_and_send_trace(
                websocket, deployer, resource_id, session_state, turns, turn, indexing=not last
            ):
                return
        except (WebSocketDisconnect, RuntimeError):
            # The chat panel was closed (or the server is shutting down)
            # while this was still waiting on telemetry -- nothing left to
            # send a trace to, so stop rather than finishing the schedule.
            return


async def _fetch_and_send_trace(websocket, deployer, resource_id, session_state, turns, turn, indexing=False):
    """One attempt. Returns True if a trace was actually sent, so the
    retry loop above knows to stop. Also called directly by the client's
    Refresh button (not backgrounded, so a click gets an immediate answer),
    where indexing=False makes a failure Refresh-able again.

    Resolving *and* caching the trace id both happen in here, in the
    background, rather than the id being captured inline right after
    answer_end -- AWS's id is free (already in session_state, no I/O),
    but some platforms need an async search.
    latest_trace_id), and awaiting that inline would block the whole
    WebSocket from accepting the next message. turns[turn]["trace_id"] is
    still cached once it's found, since session_state's own
    latest-trace-id can get overwritten by later turns -- a refresh needs
    the id for *this specific* turn, not whichever was most recent when
    the click happened."""
    turn_data = turns.setdefault(turn, {})
    trace_id = turn_data.get("trace_id")
    pending = "indexing" if indexing else "not_ready"
    if not trace_id:
        if not deployer.SUPPORTS_TRACING:
            await websocket.send_json({"type": "trace_unavailable", "turn": turn, "reason": "not_supported"})
            return True  # never going to change; stop retrying
        trace_id = await deployer.latest_trace_id(session_state)
        if trace_id:
            turn_data["trace_id"] = trace_id
    if not trace_id:
        await websocket.send_json({"type": "trace_unavailable", "turn": turn, "reason": pending})
        return False
    try:
        result = await asyncio.to_thread(deployer.get_trace, resource_id, trace_id)
    except Exception as exc:
        await websocket.send_json({"type": "trace_unavailable", "turn": turn, "reason": "error", "detail": _exc_detail(exc)})
        return True  # a real failure, not a wait -- don't paper over it with retries
    if result:
        await websocket.send_json({"type": "trace", "turn": turn, **result})
        return True
    await websocket.send_json({"type": "trace_unavailable", "turn": turn, "reason": pending})
    return False


@app.websocket("/ws/agents/{agent_id}")
async def chat_ws(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    agent = db.get_agent(agent_id)
    if not agent or agent["status"] != db.STATUS_ACTIVE:
        await websocket.send_json({"type": "error", "detail": "Agent is not active"})
        await websocket.close()
        return

    deployer = DEPLOYERS[agent["platform"]]
    resource_id = agent["platform_resource_id"]
    user_id = "portal-user"
    try:
        session_state = await deployer.create_session(resource_id, user_id)
    except Exception as exc:
        await websocket.send_json({"type": "error", "detail": f"Could not start session: {exc}"})
        await websocket.close()
        return

    # Every platform's agent lifecycle is different (AWS rebuilds its Agent
    # object per session; Azure/Gemini never see a "session started" event
    # at all -- Azure's container builds one Agent at startup and hands
    # continuity to Foundry's own conversation store, and Gemini's Agent is
    # pickled once at deploy time, forever), so there's no uniform place in
    # the deployed agent code to tell it what day it is without either
    # baking in a date that goes stale (Gemini especially -- deployed once,
    # can sit for weeks) or writing three different platform-specific
    # hacks. This is the one place that *does* uniformly know "a session
    # just started" for all three: right here, once, prepended onto the
    # first message of the conversation rather than every turn.
    date_context = (
        f"[Context: today's date is {date.today().isoformat()}. Use this for any "
        "relative date range you need to compute for a tool call -- do not guess "
        "or rely on your own sense of the current date.]\n\n"
    )

    turn = 0
    turns = {}  # turn -> {"trace_id": str | None}
    # turn -> usage dict, populated as latest_usage() resolves for each turn
    # (see _send_usage's docstring on why this is keyed by turn number).
    usage_by_turn = {}
    try:
        while True:
            data = await websocket.receive_json()
            if data["type"] == "get_trace":
                await _fetch_and_send_trace(websocket, deployer, resource_id, session_state, turns, data["turn"])
                continue
            if data["type"] == "warmup_only":
                # Waits out the same session-warmup task create_session()
                # already fired in the background, but never calls
                # stream_chat -- no LLM call happens at all. This is what
                # the load test's platform-startup-only mode drives: a real
                # WebSocket session going through exactly the same warmup
                # path a real chat turn would, just without ever sending a
                # message, so it isolates pure session-start cost from
                # everything an actual LLM call would add on top.
                #
                # wait_for_ready() can genuinely raise -- confirmed live: a
                # real transient Vertex AI "Reasoning Engine Execution
                # failed... Service Unavailable" during create_session()
                # propagated all the way up through here uncaught, which
                # silently killed the whole connection with no message ever
                # sent back (the outer except Exception below only prints a
                # server-side traceback, nothing to the client) -- the load
                # test then saw the connection drop, not a clean error.
                # Matching the user_message/stream_chat handling below.
                #
                # A raise isn't the only way this fails, either: AWS and Azure
                # swallow a failed warmup ping on purpose (fine for a chat
                # turn, which has a real call behind it), so wait_for_ready
                # returns a perfectly normal-looking duration for a session
                # that never started. That has to be an error *here*, on the
                # one path where the ping is the entire measurement -- see
                # deployers/__init__.py's latest_warmup_error.
                try:
                    warmup_ms = await deployer.wait_for_ready(session_state)
                    warmup_error = await deployer.latest_warmup_error(session_state)
                    if warmup_error is not None:
                        raise warmup_error
                    agent_init_ms = await deployer.latest_agent_init_ms(session_state)
                    platform_startup_ms = await deployer.latest_platform_startup_ms(session_state)
                    cold_start_ms = await deployer.latest_cold_start_ms(session_state)
                    client_queue_ms = await deployer.latest_client_queue_ms(session_state)
                    await websocket.send_json(
                        {
                            "type": "warmup_done",
                            "warmup_ms": warmup_ms,
                            # The headline number for this mode: what the
                            # platform itself spent before the agent's own code
                            # ran, which is the only one of these that means
                            # the same thing across providers (see
                            # deployers/__init__.py). None where it isn't
                            # measured, which the client shows as such rather
                            # than falling back to a total that would look
                            # like a much slower platform.
                            "platform_startup_ms": platform_startup_ms,
                            "agent_init_ms": agent_init_ms,
                            # Near-identical to warmup_ms on this path (this
                            # branch awaits immediately, with no user typing
                            # to overlap), which is exactly why it's worth
                            # reporting: it's the same quantity the chat
                            # panel now shows, so the two screens can be
                            # compared directly instead of appearing to
                            # disagree.
                            "cold_start_ms": cold_start_ms,
                            # The load test's own honesty check: this is time
                            # spent inside this process before the warmup call
                            # went out, so it is exactly the part of a slow run
                            # that is not the platform's fault. Worth a lot in
                            # this mode specifically, where N sessions start at
                            # once and can queue behind each other -- see
                            # deployers/__init__.py.
                            "client_queue_ms": client_queue_ms,
                        }
                    )
                except Exception as exc:
                    await websocket.send_json({"type": "error", "detail": _exc_detail(exc)})
                continue
            if data["type"] != "user_message":
                continue
            user_input = data["text"]
            if date_context:
                user_input = date_context + user_input
                date_context = None
            turn += 1
            this_turn = turn
            await websocket.send_json({"type": "answer_start", "turn": this_turn})
            started = time.monotonic()
            ttfa_ms = None
            tool_calls_this_turn = []  # ordered list of {"name": str} -- the trajectory, for spotting wasted/repeated calls
            try:
                async for event in deployer.stream_chat(resource_id, user_input, user_id, session_state):
                    # Time-to-first-activity: the first sign of life of any
                    # kind, not just answer text -- a tool-call decision or a
                    # reasoning token is computation with no network wait
                    # attached yet, so on turns that open with a tool call
                    # (most agentic turns do) it lands well before any text
                    # would. Generic across providers: Azure/Gemini never
                    # emit tool_call/reasoning here today, so this reduces to
                    # first-text for them, same as before.
                    if ttfa_ms is None and (event.get("text") or event.get("tool_call") or event.get("reasoning")):
                        ttfa_ms = round((time.monotonic() - started) * 1000)
                    if event.get("tool_call"):
                        tool_calls_this_turn.append(event["tool_call"].get("name"))
                        await websocket.send_json(
                            {"type": "tool_call", "turn": this_turn, "name": event["tool_call"].get("name")}
                        )
                    elif event.get("reasoning"):
                        await websocket.send_json({"type": "reasoning_delta", "turn": this_turn})
                    elif event.get("text"):
                        await websocket.send_json({"type": "answer_delta", "text": event["text"]})
            except Exception as exc:
                await websocket.send_json({"type": "error", "detail": _exc_detail(exc)})
            elapsed_seconds = round(time.monotonic() - started, 1)
            # Both resolve instantly (plain session_state reads -- see their
            # docstrings), unlike latest_usage/latest_trace_id, so they're
            # fetched inline here rather than as a background follow-up.
            warmup_ms = await deployer.latest_warmup_ms(session_state)
            agent_init_ms = await deployer.latest_agent_init_ms(session_state)
            platform_startup_ms = await deployer.latest_platform_startup_ms(session_state)
            cold_start_ms = await deployer.latest_cold_start_ms(session_state)
            client_queue_ms = await deployer.latest_client_queue_ms(session_state)
            retries = await deployer.latest_retries(session_state)
            _log_latency(
                agent, this_turn, ttfa_ms, elapsed_seconds, warmup_ms, tool_calls_this_turn,
                agent_init_ms, platform_startup_ms, cold_start_ms, client_queue_ms,
            )
            await websocket.send_json(
                {
                    "type": "answer_end",
                    "turn": this_turn,
                    "elapsed_seconds": elapsed_seconds,
                    "ttfa_ms": ttfa_ms,
                    "warmup_ms": warmup_ms,
                    "platform_startup_ms": platform_startup_ms,
                    "agent_init_ms": agent_init_ms,
                    "cold_start_ms": cold_start_ms,
                    "client_queue_ms": client_queue_ms,
                    "tool_calls": tool_calls_this_turn,
                    "retries": retries,
                }
            )
            # Token usage isn't always ready by answer_end (Azure's lands
            # on a trailing event well after the visible response is
            # done (some platforms deliver usage after the turn ends), so
            # this runs after, as a background task that never blocks the
            # UI from re-enabling input for the next message.
            asyncio.create_task(_send_usage(websocket, deployer, session_state, this_turn, usage_by_turn))
            # Both resolving *and* fetching the trace happen in the
            # background task -- see _fetch_and_send_trace's docstring for
            # why the id itself can't be captured inline here.
            turns[this_turn] = {}
            asyncio.create_task(
                _fetch_and_send_trace_until_ready(websocket, deployer, resource_id, session_state, turns, this_turn)
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        import traceback

        traceback.print_exc()
    finally:
        try:
            await deployer.close_session(session_state)
        except Exception:
            pass


def _max_total_sessions(mode):
    """The total-session ceiling for a mode -- see the constants' own comment
    for why platform-startup-only gets a much higher one. Anything that isn't
    recognizably that mode (including a missing or bogus value) gets the
    conservative chat ceiling, so an unknown mode can never buy the larger
    budget by accident."""
    return (
        LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY
        if mode == loadtest.MODE_WARMUP_ONLY
        else LOADTEST_MAX_TOTAL_SESSIONS
    )


def _clamp_loadtest_params(users, iterations, mode=loadtest.MODE_CHAT):
    """Clamps both individually to their own caps, then clamps iterations
    further so their *product* respects the mode's total-session ceiling too
    -- see those constants' own comment for why the combined cap is the one
    that actually matters (confirmed live: two individually-reasonable-
    looking per-field caps still let their product run away). iterations
    is what gets clamped down to fit, not users, since users is what
    actually exercises concurrency and is more useful to preserve.

    A sequential loop run (the Load Test view's "Loop" run shape) arrives here
    as users=1 with iterations as the whole session count, so it's this
    function's iterations clamp -- not a separate code path -- that bounds it."""
    users = max(1, min(int(users), LOADTEST_MAX_USERS))
    iterations = max(1, min(int(iterations), LOADTEST_MAX_ITERATIONS))
    max_total = _max_total_sessions(mode)
    if users * iterations > max_total:
        iterations = max(1, max_total // users)
    return users, iterations


LOADTEST_MAX_COMPARE_AGENTS = 2

# How often a run in progress reports aggregate numbers, in completed
# sessions. Small enough that even the shortest run (a handful of sessions)
# gets one or two, since the frontend draws its charts from these rather than
# waiting for "done" -- and cheap regardless: compute_summary is a sort over
# at most a few hundred floats.
LOADTEST_PROGRESS_SUMMARY_EVERY = 5


@app.websocket("/ws/loadtest")
async def loadtest_ws(websocket: WebSocket):
    """Drives perf/loadtest.py's own run_load_test() -- real concurrent sessions
    against this same server (see SELF_HOST), either a full chat turn per
    session (mode="chat") or session-start only with no LLM call at all
    (mode="warmup_only", see chat_ws's own "warmup_only" message) -- and
    streams progress back over this WebSocket as each simulated session
    completes, finishing with an aggregate summary. This is a genuinely
    different shape from chat_ws: it's this server acting as its *own*
    client against /ws/agents/{agent_id}, not proxying anything -- the
    same real path a browser's Chat view takes, just driven N times
    concurrently instead of once by a human.

    Takes a *list* of 1-2 agent_ids (agent_ids, not the old singular
    agent_id) so the same test config (mode/users/iterations/message) can
    be run against two agents for a side-by-side comparison -- one agent
    fully at a time, not concurrently (see run() below for why: the load
    generation and the real hosted-agent calls it drives both run in this
    same local process, so two agents' tests running at once compete for
    this machine's own CPU/network/connection-pool capacity, confounding
    the comparison with local resource contention rather than isolating
    each agent's real behavior). One run_load_test() call per agent;
    every session_result is tagged with which agent_id produced it, and
    the final done message carries one summary per agent (summaries,
    keyed by agent_id) rather than a single summary. This does mean the
    single-agent case is no longer a structurally different message shape
    from the two-agent case -- it's simply a one-entry summaries dict --
    one consistent protocol rather than two parallel ones for what's
    otherwise identical machinery.

    Every session result is put onto a queue rather than sent to this
    websocket directly from within run_load_test's own concurrent worker
    tasks (concurrent *within* one agent's own test -- users simulated
    sessions against that one agent -- even though the agents themselves
    now run one after another): Starlette's WebSocket.send_json isn't safe
    to call from multiple coroutines at once, and up to LOADTEST_MAX_USERS
    of them can finish a session at essentially the same moment. Routing
    every send through this one loop, the only place that ever calls
    send_json, avoids that entirely rather than adding a lock around sends
    scattered across those worker tasks.

    The caps (see _clamp_loadtest_params) are applied per agent, not
    combined across the comparison -- a two-agent run can fire up to 2x the
    real session count a single-agent run can, but neither individual
    agent ever exceeds the already-reviewed single-agent ceiling, which is
    the safety property that actually matters here (protecting any one
    hosted agent/cloud resource from runaway concurrency).

    Sends started -> session_result per session -> a progress_summary every
    LOADTEST_PROGRESS_SUMMARY_EVERY sessions -> done. Accepts one further
    client message after the start: {"type": "stop"}, which ends the run early
    and still reports what completed (see watch_client below). A sequential
    loop run -- the Load Test view's "Loop" run shape, users=1 with the whole
    session count as iterations -- needs no special handling here beyond
    those two additions; it's the same engine call with different numbers."""
    await websocket.accept()
    run_task = None
    watch_task = None
    try:
        data = await websocket.receive_json()
        if data.get("type") != "start":
            await websocket.send_json({"type": "error", "detail": "expected a 'start' message"})
            return
        agent_ids = data.get("agent_ids")
        if not isinstance(agent_ids, list) or not (1 <= len(agent_ids) <= LOADTEST_MAX_COMPARE_AGENTS):
            await websocket.send_json(
                {"type": "error", "detail": f"expected 1-{LOADTEST_MAX_COMPARE_AGENTS} agent_ids"}
            )
            return
        if len(agent_ids) == 2 and agent_ids[0] == agent_ids[1]:
            await websocket.send_json({"type": "error", "detail": "Pick two different agents to compare"})
            return
        for agent_id in agent_ids:
            agent = db.get_agent(agent_id) if agent_id else None
            if not agent or agent["status"] != db.STATUS_ACTIVE:
                await websocket.send_json({"type": "error", "detail": f"Agent {agent_id} is not active"})
                return

        # Mode first: it decides which total-session ceiling the clamp applies.
        mode = data.get("mode") if data.get("mode") in (loadtest.MODE_CHAT, loadtest.MODE_WARMUP_ONLY) else loadtest.MODE_CHAT
        users, iterations = _clamp_loadtest_params(data.get("users", 5), data.get("iterations", 4), mode)
        think_min = max(0.0, float(data.get("think_min", 1.0)))
        think_max = max(think_min, float(data.get("think_max", 10.0)))
        message = (data.get("message") or "").strip() or loadtest.DEFAULT_MESSAGE
        per_agent_total = users * iterations
        total = per_agent_total * len(agent_ids)

        queue = asyncio.Queue()

        def make_on_result(agent_id):
            async def on_result(rec):
                await queue.put({**rec, "agent_id": agent_id})

            return on_result

        async def run_for_agent(agent_id):
            # A single bad *session* never raises here -- run_session/
            # run_session_warmup_only already catch broadly and record the
            # failure on that session's own result instead (see
            # perf/loadtest.py). This except is for something raising above that
            # per-session isolation entirely (a real bug, not a flaky
            # cloud call) -- swallowed so it doesn't abort a still-pending
            # agent's turn in the sequence below; that agent's summary
            # simply reflects however many sessions it completed before
            # failing.
            try:
                await loadtest.run_load_test(
                    SELF_HOST, agent_id, users, iterations, make_on_result(agent_id),
                    mode=mode, think_min=think_min, think_max=think_max, message=message, timeout_s=60.0,
                )
            except asyncio.CancelledError:
                raise  # a real cancellation (disconnect) must still propagate normally
            except Exception:
                pass

        async def run():
            # One agent fully at a time, not asyncio.gather()'d concurrently
            # -- confirmed live that concurrent load generation and the real
            # hosted-agent calls it drives both run in this *same* local
            # process, sharing this machine's CPU, network stack, and (for
            # AWS specifically, until _MAX_POOL_CONNECTIONS was raised) a
            # literal shared connection pool. Comparing two agents at once
            # doubled that local contention on top of whatever a single
            # agent's own concurrency already added, confounding "which
            # agent is actually faster" with "how much local capacity did
            # the other agent's simultaneous test consume." Running them
            # one after another costs the fairness of identical wall-clock
            # conditions between the two -- a real tradeoff, made
            # deliberately in favor of each agent's numbers reflecting only
            # its own test.
            try:
                for agent_id in agent_ids:
                    await run_for_agent(agent_id)
            finally:
                await queue.put(None)  # sentinel: every session (across every agent) has completed, or run() raised

        run_task = asyncio.create_task(run())

        await websocket.send_json(
            {
                "type": "started",
                "agent_ids": agent_ids,
                "total": total,
                "per_agent_total": per_agent_total,
                "users": users,
                "iterations": iterations,
                "message": message,
                "mode": mode,
            }
        )
        # A "stop" from the browser, and a disconnect, are the same thing to
        # this run: end it now. Until a loop run could last half an hour
        # (LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY sequential sessions),
        # navigating away was a fine enough way out; it isn't once the answer
        # to "I've seen enough" is throwing away every session so far. The
        # sessions already completed are kept and summarized either way, so
        # stopping is a shorter run, not a lost one.
        stopped = False

        async def watch_client():
            nonlocal stopped
            try:
                while True:
                    incoming = await websocket.receive_json()
                    if incoming.get("type") == "stop":
                        stopped = True
                        run_task.cancel()
                        return
            except Exception:
                # A disconnect, or any frame this endpoint doesn't speak.
                # run()'s own finally still queues the sentinel, so the drain
                # loop below ends on its own rather than hanging on a client
                # that's gone.
                run_task.cancel()

        watch_task = asyncio.create_task(watch_client())  # cancelled in this handler's finally

        results = []
        completed_by_agent = {agent_id: 0 for agent_id in agent_ids}

        def summaries_now():
            return {
                agent_id: loadtest.compute_summary([r for r in results if r["agent_id"] == agent_id])
                for agent_id in agent_ids
            }

        while True:
            rec = await queue.get()
            if rec is None:
                break
            results.append(rec)
            completed_by_agent[rec["agent_id"]] += 1
            await websocket.send_json(
                {
                    "type": "session_result",
                    "completed": len(results),
                    "total": total,
                    "agent_completed": completed_by_agent[rec["agent_id"]],
                    "agent_total": per_agent_total,
                    **rec,
                }
            )
            # Interim aggregates, built with the same compute_summary the done
            # message uses -- so a number read mid-run and the same number at
            # the end can't disagree about what it means. A long run is
            # otherwise a blind wait: the frontend renders its charts from
            # these, rather than only from "done".
            if len(results) % LOADTEST_PROGRESS_SUMMARY_EVERY == 0:
                await websocket.send_json(
                    {"type": "progress_summary", "agent_ids": agent_ids, "summaries": summaries_now()}
                )
        try:
            await run_task  # propagates a real exception from run_load_test itself, if there was one
        except asyncio.CancelledError:
            if not stopped:
                raise  # a cancellation nobody asked for is a real error, not a stop
        await websocket.send_json(
            {"type": "done", "agent_ids": agent_ids, "summaries": summaries_now(), "stopped": stopped}
        )
    except WebSocketDisconnect:
        # The browser navigated away mid-test -- cancel the still-running
        # load generation (both agents' worth, if comparing) rather than
        # leaving it firing real requests against real hosted agents with
        # nobody watching. Awaiting the cancelled task (not just calling
        # .cancel() and moving on) matters: confirmed live that skipping
        # this leaves an "asyncio: Task exception was never retrieved"
        # warning logged on every disconnect, since nothing ever collects
        # the CancelledError it raises.
        if run_task is not None:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "detail": _exc_detail(exc)})
        except Exception:
            pass
    finally:
        # The watcher is parked on a receive that will never complete once
        # we're done with this socket -- left alone it would outlive the
        # handler and, on close, cancel an already-finished run_task.
        if watch_task is not None:
            watch_task.cancel()
            try:
                await watch_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8910, log_level="info")
