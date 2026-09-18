"""Entry point for every portal-created Bedrock AgentCore Runtime agent.
One static file, not generated per agent -- model/instructions/tools are read
from agent_config.json, written into the deployment artifact per-deploy by
deploy(). (azure_hosted/main.py still uses env vars for this; see
../deployers/aws.py's _agent_config for the two AgentCore-specific failures
that moved AWS off them -- V2's 1024-byte env payload cap, and AgentCore
rejecting the newlines in any real system prompt.)

This is what actually runs inside the AgentCore Runtime container. Unlike
~/Dev/AWS's own hosted agent (scaffolded via `agentcore create`, deployed
via the agentcore CLI + CDK), this is deployed directly via boto3's
bedrock-agentcore-control CreateAgentRuntime -- see ../deployers/aws.py.
"""

import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent, tool
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from strands.models.bedrock import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

from tools import get_price_history, get_stock_price, search_web

app = BedrockAgentCoreApp()
log = app.logger

_TOOL_FUNCS = {
    "web_search": [search_web],
    "stock_data": [get_stock_price, get_price_history],
}

# Mirrors deployers/aws.py's WEB_SEARCH_TOOL_ID -- no shared import, same
# reason as _WARMUP_SENTINEL below (this file is zipped and deployed
# standalone). Not in _TOOL_FUNCS: it isn't a plain callable, it's an MCP
# connection to an AgentCore Gateway target (see _build_mcp_clients).
_WEB_SEARCH_TOOL_ID = "web_search_aws"

# Mirrors deployers/aws.py's AGENT_CONFIG_FILENAME. Written into the artifact
# next to this file by both deployment modes, so it's always a sibling whether
# this is running from an unzipped CodeZip package or from /app in a container.
_CONFIG_PATH = Path(__file__).parent / "agent_config.json"


def _load_config():
    """Read once at import: it's a small file that cannot change under a running
    runtime (the artifact is immutable -- a new config means a new deploy), so
    re-reading per session would buy nothing.

    Missing keys are a bug in the deployer, not a user-recoverable state, so
    this deliberately doesn't paper over them with defaults -- an agent that
    silently ran with no system prompt or the wrong model would be far harder to
    diagnose than one that fails at startup with a KeyError naming the field."""
    return json.loads(_CONFIG_PATH.read_text())


CONFIG = _load_config()


def _gateway_region(gateway_url):
    """The region to sign an AgentCore Gateway call for, taken from the
    gateway's own hostname rather than from AWS_REGION.

    SigV4 binds the signature to a region, so a signature made for the region
    the *agent* runs in is rejected by a gateway in another one. That's a real
    configuration, not a hypothetical: the gateway is one-time shared setup
    (docs/aws.md's "AWS web search via AgentCore Gateway") and the portal can
    create agents in several regions, so an agent outside the gateway's region
    is the normal case rather than the exception -- and the failure it would
    cause lands at invoke time, on an agent whose deploy looked entirely
    successful, which is the worst failure shape this project has.

    Read off the URL because the URL already carries it
    (...gateway.bedrock-agentcore.us-east-1.amazonaws.com), so there's nothing
    to keep in sync: a gateway URL and a region parsed out of it can't disagree
    the way a separately-configured pair could. Falls back to AWS_REGION for a
    URL that isn't that shape (a VPC endpoint, or a future hostname format),
    which is exactly what this did before and is right whenever the gateway is
    in fact local to the agent."""
    host = urlparse(gateway_url).hostname or ""
    labels = host.split(".")
    # "<id>.gateway.bedrock-agentcore.<region>.amazonaws.com": the region is
    # the label after the service name. Matched on the service label rather
    # than by position so a hostname with an extra label doesn't yield some
    # unrelated label as a "region".
    if "bedrock-agentcore" in labels:
        index = labels.index("bedrock-agentcore") + 1
        if index < len(labels) and labels[index] != "amazonaws":
            return labels[index]
    return os.environ.get("AWS_REGION", "us-east-1")


def _build_mcp_clients():
    clients = []

    urls = CONFIG["mcp_servers"]
    if urls:
        config = {"mcpServers": {f"mcp_{i}": {"url": url} for i, url in enumerate(urls)}}
        clients.extend(MCPClient.load_servers(config))

    tool_ids = CONFIG["tools"]
    gateway_url = CONFIG["web_search_gateway_url"]
    if _WEB_SEARCH_TOOL_ID in tool_ids and gateway_url:
        # A plain streamablehttp_client (as used above for the plain MCP servers)
        # can't sign requests -- the Gateway is IAM (SigV4) authorized, not
        # OAuth/API-key, so this connects via mcp-proxy-for-aws instead,
        # signing with the runtime's own execution-role credentials (the same
        # ones already used for every Bedrock call this agent makes -- no
        # separate credential to manage). "bedrock-agentcore" is the service
        # name AWS's own docs specify for signing calls to an AgentCore
        # Gateway, confirmed against the Web Search Tool connector setup
        # guide, not guessed.
        clients.append(
            MCPClient(
                lambda: aws_iam_streamablehttp_client(
                    endpoint=gateway_url,
                    aws_service="bedrock-agentcore",
                    aws_region=_gateway_region(gateway_url),
                )
            )
        )

    return clients


def _build_tools():
    tool_ids = CONFIG["tools"]
    funcs = []
    for tool_id in tool_ids:
        funcs.extend(_TOOL_FUNCS.get(tool_id, []))
    # Rebuilt per new session (see agent_factory below) -- each session
    # gets its own MCP client connections, matching how each session
    # already gets its own local-tool-bound Agent instance.
    return [tool(f) for f in funcs] + _build_mcp_clients()


def _load_model():
    return BedrockModel(
        model_id=CONFIG["model"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _process_age_ms():
    """How long this OS process has been alive, in milliseconds.

    Read from /proc rather than from a time.monotonic() stamp at the top of
    this file, so it covers everything the platform launched on our behalf and
    not just this module's own imports: the interpreter boot, and
    opentelemetry-instrument's startup (the runtime's entryPoint is
    ["opentelemetry-instrument", "main.py"] -- see deployers/aws.py), both of
    which are our dependencies' cost rather than AgentCore's and so belong on
    our side of the split.

    Returns None where /proc isn't available (running this file on a mac),
    which callers treat as "not measured" rather than as a zero -- same
    convention as app_init_ms's own None elsewhere in this project."""
    try:
        stat = Path("/proc/self/stat").read_text()
        # Fields 1-2 (pid and comm) are skipped by slicing past the last ")":
        # comm is the executable name in parentheses and can itself contain
        # both spaces and parentheses, so splitting the whole line on
        # whitespace would misalign every field after it. What's left starts
        # at field 3, putting starttime (field 22) at index 19.
        start_ticks = float(stat[stat.rindex(")") + 1 :].split()[19])
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
        return round((uptime_seconds - start_ticks / os.sysconf("SC_CLK_TCK")) * 1000)
    except Exception:
        return None


# Reuses one Agent per session_id so each session keeps its own in-process
# conversation history (best-effort; resets on cold start) -- same
# LRU-cache pattern as ~/Dev/AWS/hosted/.../main.py, bounded to 128
# sessions so a long-running process can't leak history or grow unbounded.
def agent_factory():
    cache = OrderedDict()

    def get_or_create_agent(session_id):
        if session_id in cache:
            cache.move_to_end(session_id)
            return cache[session_id]
        if len(cache) >= 128:
            cache.popitem(last=False)
        cache[session_id] = Agent(
            model=_load_model(),
            system_prompt=CONFIG["instructions"],
            tools=_build_tools(),
            conversation_manager=NullConversationManager(),
        )
        return cache[session_id]

    return get_or_create_agent


get_or_create_agent = agent_factory()

# Everything above this line is our own one-time startup: the imports, the
# config read, and the factory closure. Stamped here, at the end of the
# module body, so the warmup response can report it and the portal can put it
# on our side of the platform-vs-agent split rather than leaving it inside
# AgentCore's number (see _process_age_ms, and deployers/aws.py's
# wait_for_ready for how it's used -- notably that it only counts when the
# container was in fact started for the session being measured).
_MODULE_INIT_MS = _process_age_ms()


def _extract_prompt(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    return prompt


# Mirrors deployers/aws.py's own copy of this exact string (no shared
# import -- this file is zipped and deployed standalone, see deploy() in
# that module). A throwaway InvokeAgentRuntime call carrying this as the
# prompt is a warmup ping fired right when a chat session opens, not a real
# user message: it exists purely to force get_or_create_agent's session
# construction -- including its MCP client connections, the slowest part --
# to happen now instead of during the user's first real Send, without
# adding anything to that session's conversation history.
_WARMUP_SENTINEL = "--WARMUP--"


@app.entrypoint
async def invoke(payload, context):
    session_id = getattr(context, "session_id", "default-session")
    prompt = _extract_prompt(payload)
    if prompt == _WARMUP_SENTINEL:
        # Everything this agent's own code spends on starting a session,
        # reported so deployers/aws.py can subtract it from the warmup ping's
        # total round trip and be left with AgentCore's own contribution --
        # what it spent routing, and provisioning or reusing an instance,
        # before any of our code ran at all. That subtraction is the whole
        # point of the sentinel: it's the one number that can be compared
        # across agent platforms, because it doesn't move when this agent's
        # dependencies or tool set change.
        #
        # container_age_ms is read *before* the session work below, so it's
        # the age at the moment this invoke arrived. wait_for_ready needs it
        # to decide whether module_init_ms happened inside the round trip it
        # measured (a container started for this session) or long before it (a
        # pre-warmed one) -- see that function.
        #
        # Yielding all this is safe here: the warmup sentinel never reaches a
        # real chat turn, so it's never mistaken for conversation content.
        container_age_ms = _process_age_ms()
        started = time.monotonic()
        get_or_create_agent(session_id)
        yield {
            "session_init_ms": round((time.monotonic() - started) * 1000),
            "module_init_ms": _MODULE_INIT_MS,
            "container_age_ms": container_age_ms,
        }
        return

    agent = get_or_create_agent(session_id)

    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
