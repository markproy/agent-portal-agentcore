"""Shared interface every platform deployer implements, plus the tool and
MCP-server catalogs offered in the create-agent form.

A deployer module exposes:
    SUPPORTS_TRACING: bool                                                     [module-level constant]
    DEPLOYMENT_MODES: tuple[str, ...]                                          [module-level constant]
    REQUIRED_CONFIG: tuple[str, ...]                                           [module-level constant]
    deployment_regions() -> sequence of region names, () if not offered        [sync]
    deploy(name, model, description, agent_instructions, tool_ids, mcp_server_ids) -> resource_id   [sync]
    undeploy(resource_id) -> None                                              [sync]
    list_deployed() -> list[{"name": str, "resource_id": str}]                 [sync]
    async create_session(resource_id, user_id) -> session_state (opaque dict)
    async close_session(session_state) -> None
    async stream_chat(resource_id, message, user_id, session_state)
        -> async generator of {"text": str}, may mutate session_state in place
    async latest_usage(session_state) -> {"input_tokens": int, "output_tokens": int} | None
    async latest_trace_id(session_state) -> str | None
    get_trace(resource_id, trace_id) -> {"trace_id": str, "spans": [{"name", "duration_ms", "lines"}]} | None   [sync]
    async latest_warmup_ms(session_state) -> int | None
    async latest_agent_init_ms(session_state) -> int | None
    async latest_platform_startup_ms(session_state) -> int | None
    async latest_cold_start_ms(session_state) -> int | None
    async latest_client_queue_ms(session_state) -> int | None
    async latest_warmup_error(session_state) -> Exception | None
    async latest_retries(session_state) -> int | None
    async wait_for_ready(session_state) -> int

wait_for_ready() waits for whatever session-warmup/session-creation task
create_session() fired in the background, times it, and records the
result the same way latest_warmup_ms() later reads it back -- without
making any LLM call. stream_chat() calls it inline as its own first step
(a real chat turn still needs to wait out warmup before its real call),
but it's also called standalone by chat_ws's "warmup_only" WebSocket
message, which never calls stream_chat/the LLM at all -- the load test's
platform-startup-only mode (see server.py/loadtest.py) uses exactly this path
to measure pure session-start cost in isolation. Safe to call more than
once per session (e.g. once standalone, then again inline from a later
real stream_chat call): the underlying task is never cleared until
consumed for real, and re-awaiting an already-done asyncio.Task returns
instantly.

latest_platform_startup_ms()/latest_agent_init_ms() split a session start
into the platform's work and ours, which is what makes session-start cost
comparable between platforms at all: platform_startup_ms is everything the
platform did before any of the agent's own code ran (routing the call, and
provisioning or reusing an instance), and agent_init_ms is everything the
agent's own code then spent getting ready (on AWS: the container's imports
and module-level setup, plus get_or_create_agent() building a fresh Agent
and opening its MCP client connections). The point of the split is that
platform_startup_ms doesn't move when an agent's dependencies or tool set
change, so it can be held up against another provider's number; a single
undifferentiated total can't.

Neither number can be read off the platform directly -- none of the three
says when it handed control over -- so both come from the agent measuring
its own startup from the inside and the portal subtracting that from the
round trip it timed. Only AWS does this today: aws_hosted/main.py is our
code and reports its own figures over the warmup response (see
deployers/aws.py's wait_for_ready, including why the container's imports
only count when the container was started for the session being measured).
Both are None on Azure/Gemini, not 0 -- there, warmup_ms/cold_start_ms are
still whole-session-start totals with the agent's own share left inside
them, so they are not comparable to AWS's platform_startup_ms and the UI
labels them accordingly. On AWS both are also None for an agent deployed
before the sentinel existed, or when the warmup ping failed outright: no
honest subtraction without something to subtract.

latest_cold_start_ms() answers a different question from latest_warmup_ms(),
and the pair only makes sense together. The warmup task is fired when the
session opens and runs while the user types their first message, so
warmup_ms -- clocked from when a caller starts awaiting -- is just the tail
of it that turn actually sat through, and shrinks the longer the user takes
to hit Send. cold_start_ms is the duration of the warmup's own call, stamped
by the task itself (see each module's _timed_warm_up /
_timed_create_session), so it doesn't move with user behaviour. The
difference between them is what pre-warming bought: real time the platform
spent that nobody waited for. Reporting only warmup_ms made the chat panel
look like it contradicted the load test, which awaits immediately and so
sees the whole cold start; reporting only cold_start_ms would overstate what
any given turn actually cost. Implemented on all three platforms.

latest_client_queue_ms() is the third member of that family and exists
because the first two were being read as platform numbers when they weren't.
The warmup task is created, then its call is issued some time later -- after
waiting for a thread in asyncio's default executor, after building a client
on first touch of a region, after the event loop gets back to it. That delay
is this process's, not the platform's, and folding it into cold_start_ms (as
these modules originally did, timing from when the task was fired) means a
busy or over-subscribed client reports a slow platform. Found the hard way:
a 20-concurrency burst from a loaded laptop reported a 30s p75
platform_startup_ms for invokes CloudWatch shows AgentCore served in ~2s,
because those invokes sat in the client for up to 20s before being issued.
So cold_start_ms now covers only the call itself, and the delay before it is
reported separately here, where it reads as the client problem it is. It is
None when the call never got far enough to have a queue time (a client that
failed to build, a cancelled task), on the same "None means not measured"
convention as the rest of this interface. Implemented on all three platforms;
non-zero values are normal and small -- it's the tail that matters.

latest_warmup_error() exists because wait_for_ready() returning normally does
not mean the warmup worked. AWS and Azure deliberately swallow a failed warmup
ping (see their _warm_up docstrings): for a chat turn that's right, since the
real call that follows will surface any problem itself and a transient ping
failure only costs that turn its cold start. But the load test's warmup-only
mode has nothing else to look at -- the ping *is* the measurement -- and a
swallowed failure there was being timed and reported as a session start. Found
live: a 40-session run with expired credentials came back all-green at a p50 of
218ms, which is the time it takes to be told a signature is invalid, not the
time it takes to start a session. So the exception is now kept rather than
dropped, and chat_ws's "warmup_only" branch checks it and sends an error frame
instead of warmup_done. Returns the exception itself, not a string, so the
caller formats it the one way it formats every other error. None means the
warmup succeeded, or (Gemini) that failures were never swallowed in the first
place so a failure would already have raised out of wait_for_ready.

latest_warmup_ms()/latest_retries() are the "why was this turn slow"
signals server.py's chat_ws folds into answer_end, alongside the tool-call
sequence it already tracks from stream_chat's own event stream and the
token counts/deltas latest_usage() already provides -- together covering
the four usual causes of "the agent feels slow": a cold session (warmup
wait), a wasteful tool-call trajectory (the ordered call list), a slow LLM
call given how much it had to read/write (token counts), and a throttled
LLM call (retries). Both return None, not 0, when the signal genuinely
isn't available on that platform (Azure/Gemini for latest_retries -- see
their own docstrings) so the UI can tell "not measured here" apart from "a
real zero was measured," rather than a false zero implying a signal that
doesn't actually exist.

deploy/undeploy/list_deployed are synchronous, real cloud calls -- callers
run them via BackgroundTasks/asyncio.to_thread. Chat is async because it's
long-lived per WebSocket connection.

latest_usage() is separate from stream_chat() rather than folded into its
event stream. Callers should call this only after the turn's answer_end,
as a non-blocking follow-up, not inline.

bridge_sync_iterable() bridges sync iterables to async -- boto3 is sync,
so all AWS calls use it.

Trace support (latest_trace_id/get_trace) is implemented via CloudWatch
Logs Insights -- see deployers/aws.py's get_trace.

get_trace() returns {"name", "duration_ms", "lines"} span shape
own span_to_dict already established (see
~/Dev/Gemini/show_traces.py), specifically so the frontend trace panel
can render every platform identically -- only what each deployer can
actually populate differs, not the display code.

DEPLOYMENT_MODES lists the packaging choices a platform actually offers for
the *same* agent code, in the order the create-agent form should present
them -- first entry being that platform's default, i.e. what deploy() uses
when no mode is requested -- and is () for a platform that has no such
choice. Only AWS does today:
AgentCore Runtime's CreateAgentRuntime takes either a code zip or a
container image (see AVAILABLE_DEPLOYMENT_MODES below and deployers/aws.py),
and which one is used measurably changes cold-start behavior, so it's a
first-class choice rather than an internal detail. Gemini deploys a pickled
Agent object to Agent Engine and Azure does a server-side remote build --
neither exposes an alternative -- so both declare (), explicitly, for the
same reason SUPPORTS_TRACING is mandatory below: a module that simply forgot
should fail loudly rather than look like a platform with nothing to offer.
deploy() takes deployment_mode only where DEPLOYMENT_MODES is non-empty
(server.py passes it accordingly), so the two platforms without the concept
keep the plain signature above.

deployment_regions() is the same idea for *where* an agent gets created: the
regions the create-agent form should offer, first entry being the default that
deploy() uses when none is requested, and () for a platform where region isn't
a per-agent choice. Only AWS offers one today -- AgentCore runtimes are
per-region resources and the portal can create them in any region it's
configured for, while Gemini's location and Azure's Foundry project are
process-wide settings that a single agent can't vary (each module's own
docstring says why). A function rather than a constant like DEPLOYMENT_MODES
because it's derived from the environment, which a test can change after
import. Region only ever has to be *chosen* at create time: every later
operation recovers it from the agent's own resource id, which is why nothing
else in this interface mentions it.

REQUIRED_CONFIG names the .env variables the module cannot function without
-- mechanically, exactly the ones it reads as os.environ[...] with no default,
not a curated wish list -- so the portal can grey a platform out in the
create-agent form, with the variable names to go set, instead of offering it
and failing at deploy. It's the module's own answer for the same reason
DEPLOYMENT_MODES is: only the module knows what it reads.

This exists because all three platforms were offered as equally available
while only one of them was actually configured, and every unconfigured one
announced itself as a mid-deploy error. Two consequences worth knowing:

- Those reads are os.environ.get(var, "") rather than os.environ[var], so an
  unset variable now yields a greyed-out platform instead of crashing the whole
  portal at import with a bare KeyError -- which is what a fresh clone with a
  half-filled .env used to get, with nothing on screen naming the variable.
  Nothing downstream has to cope with the empty value, because the platform
  can't be selected: server.py's create_agent rejects it before any deploy()
  call (see _platform_status there).
- () is a legitimate value, for a deployer needing no configuration at all.
  Every module must still set it, same as SUPPORTS_TRACING, so one that
  forgot fails loudly rather than passing for "needs nothing."

SUPPORTS_TRACING distinguishes "not implemented for this platform yet"
from "implemented, but this specific trace hasn't been found/indexed
yet" (a transient state on every platform that's actually implemented,
covered by the frontend's Refresh button) -- server.py's chat_ws checks
this *before* ever calling latest_trace_id(), since a deployer that
hasn't implemented tracing yet has nothing meaningful to try. Every
module below must set it, even the still-unimplemented ones, so a
module that forgets to add it fails loudly (AttributeError) rather than
silently behaving like "not implemented."
"""

import asyncio
import os
import queue
import threading

from dotenv import load_dotenv

# Needed here, not just in submodules --
# this __init__ module builds AVAILABLE_MCP_SERVERS by reading each entry's
# URL env var from os.environ at *import* time, and since this is the
# package's own __init__, it runs before any submodule's own load_dotenv()
# call. Without this, .env's value never makes it in: a real bug caught by
# actually restarting the server, not from the test suite (which sets env
# vars directly, bypassing dotenv loading entirely).
load_dotenv()

# The shapes .env.example uses for a value only the account owner can supply:
# "your-gcp-project-id", "gs://your-bucket-name", "gateway-xxxxxxxxxx.gateway...",
# "<account-id>". A test pins this list against the real file, asserting that
# every placeholder it ships is actually detected -- so the list can't silently
# fall behind a new one.
#
# Matching on shape is deliberately *all* this does. Comparing a value to what
# .env.example ships for the same key looks stricter and was tried first, but
# it's wrong: that file also ships legitimate defaults meant to be kept
# (GOOGLE_CLOUD_LOCATION=us-central1, AWS_REGION=us-east-1), and a user who
# sensibly keeps one had their platform greyed out for a variable that was
# correctly set. Caught by reading the live /api/config, not by the tests.
_PLACEHOLDER_MARKERS = ("your-", "your_", "xxxx", "<")


def is_configured(env_var):
    """True only when env_var holds a value somebody actually filled in -- i.e.
    it's non-empty and doesn't still look like one of .env.example's fill-me-ins
    (see _PLACEHOLDER_MARKERS). Both of those produced a real broken deploy in
    this project, the second one twice.

    What this deliberately does *not* do is check that the value works -- no
    network call, no credential check, nothing that could make rendering the
    create-agent form slow or fail. A filled-in but wrong value still gets
    through: a real hostname with a typo in it passes this check fine, and
    only connecting to it could have caught that. The job here is narrower
    and covers the far more common case -- not offering a
    platform or tool whose one-time setup was never done at all."""
    value = os.environ.get(env_var, "").strip()
    if not value:
        return False
    return not any(marker in value for marker in _PLACEHOLDER_MARKERS)


def unconfigured_reason(env_vars):
    """A one-line, user-facing "why is this greyed out" message for a group of
    required variables, or "" when they're all really set.

    Names the variables instead of describing the problem in the abstract,
    because the fix is always the same concrete action: go set these in .env.
    Used by server.py for platforms, tools, and MCP servers alike, so the
    create-agent form can disable what cannot work with the reason attached,
    rather than accepting the choice and failing at deploy (or, worse, at the
    agent's first invoke)."""
    missing = [env_var for env_var in env_vars if not is_configured(env_var)]
    if not missing:
        return ""
    if len(missing) == 1:
        return f"Set {missing[0]} in .env — it's unset or still a placeholder."
    return f"Set {', '.join(missing)} in .env — they're unset or still placeholders."


# tool_id -> (label, description) shown as a checkbox in the create-agent
# form. The actual callables each id maps to live in tools.py and are
# assembled per-platform (each SDK wants a plain list of functions).
#
# An entry's optional "platforms" key restricts which platform(s) it's
# offered for -- absent means all three, same as before this key existed.
# "web_search_aws" is the first tool that isn't uniformly available: it
# isn't a plain callable in tools.py at all, but a connection to an AWS
# Bedrock AgentCore Gateway target (see deployers/aws.py and docs/aws.md's "AWS
# web search via AgentCore Gateway"), so it only makes sense for AWS agents.
AVAILABLE_TOOLS = {
    "web_search": {
        "label": "Web search",
        "description": "Look up current news, events, or general facts on the web (DuckDuckGo, keyless).",
    },
    "web_search_aws": {
        "label": "Web search (AWS AgentCore Gateway)",
        "description": (
            "AWS's own managed web search, via an AgentCore Gateway connector -- broader "
            "coverage, source citations, and a knowledge graph for factual questions, vs. "
            "the basic DuckDuckGo lookup above. AWS agents only; requires one-time Gateway "
            "setup (see README)."
        ),
        "platforms": ["aws"],
        # An entry's optional "requires_env" names the .env variable its
        # one-time setup produces, so server.py can report the tool as
        # unavailable (and the form can grey it out with the reason) instead of
        # accepting it. Worth doing for this one specifically because the
        # failure it prevents is the worst kind in this project: an agent
        # configured with an unreachable Gateway deploys perfectly, reaches
        # READY, and then dies on *every* invoke -- aws_hosted/main.py turns any
        # tool that won't load into a fatal error rather than an agent missing
        # one tool. Confirmed live from CloudWatch, not theorized.
        "requires_env": "AGENTCORE_WEB_SEARCH_GATEWAY_URL",
    },
    "stock_data": {
        "label": "Stock data",
        "description": (
            "Get the latest stock price and historical performance for a ticker, and build "
            "line-chart image URLs comparing series over time."
        ),
    },
}


def tool_allowed_for_platform(tool_id, platform):
    """False for an unknown tool_id, or one whose "platforms" allowlist
    doesn't include this platform. Used both to filter the create-agent
    form's checkboxes and, server-side in server.py, to reject a request
    that picks a platform-restricted tool for the wrong platform (the form
    already prevents this, but the API itself doesn't otherwise check)."""
    tool = AVAILABLE_TOOLS.get(tool_id)
    if tool is None:
        return False
    allowed_platforms = tool.get("platforms")
    return allowed_platforms is None or platform in allowed_platforms

# mcp_server_id -> (label, url, description) shown as a checkbox in the
# create-agent form, alongside AVAILABLE_TOOLS -- an agent can mix any
# number of local tools with any number of MCP servers in one tool list.
# Deliberately just a static dict for now, not a DB-backed CRUD registry --
# fine for a handful of manually-deployed remote servers; revisit as a real
# "AI gateway" registry (add/configure/delete/test servers from the UI) once
# there's more than one or two of these.
#
# "env_var" names where the server's URL comes from, and is the source of both
# "url" below and the availability check server.py applies: a remote MCP server
# is exactly as usable as its endpoint, and an agent pointed at a placeholder
# URL fails the same fatal way the AgentCore Gateway one does (see
# "requires_env" above).
#
# Empty for now -- no remote MCP server ships pre-configured. Add an entry
# here (label, env_var, url read from that env var, description) to wire one
# in; the create-agent form, server.py's validation/availability checks, and
# mcp_urls_for() below all key off this dict generically, so nothing else
# needs to change.
AVAILABLE_MCP_SERVERS = {}

# "available" here means *implemented in this portal*, and is a different
# question from whether this particular checkout is set up to use it -- that
# second one is answered per-platform from its own REQUIRED_CONFIG, at request
# time, by server.py's _platform_status(). Both axes are kept because they need
# different words in the UI ("coming soon" vs. "set these variables in .env")
# and because only one of them can change without a code change.
PLATFORMS = {    "aws": {"label": "AWS (Strands + Bedrock AgentCore Runtime)", "available": True},
}

# deployment_mode -> (label, description) for the create-agent form's
# Deployment select, shown only for a platform whose own DEPLOYMENT_MODES
# lists any of these (see that constant's note above). The ids are what
# deploy() receives and what the DB stores per agent; which of them a given
# platform supports is the deployer module's own answer, not this dict's, so
# a platform can add a mode without this catalog gaining a meaning it
# doesn't have elsewhere.
AVAILABLE_DEPLOYMENT_MODES = {
    "code": {
        "label": "Code zip (managed runtime)",
        "description": (
            "Dependencies are vendored into a zip on S3 and run by AgentCore's own managed "
            "Python runtime -- nothing to build locally, and no container registry involved."
        ),
    },
    "container": {
        "label": "Container image (ECR)",
        "description": (
            "The same agent code, built here as a linux/arm64 image and pushed to ECR. Needs "
            "Docker running locally and a one-time ECR repository (see docs/aws.md); slower to "
            "deploy, and started differently by the platform -- which is the point when "
            "comparing startup behavior."
        ),
    },
}


def mcp_urls_for(mcp_server_ids):
    """Resolves selected MCP server ids to their configured URLs."""
    return [
        AVAILABLE_MCP_SERVERS[server_id]["url"]
        for server_id in mcp_server_ids
        if server_id in AVAILABLE_MCP_SERVERS and AVAILABLE_MCP_SERVERS[server_id]["url"]
    ]


async def bridge_sync_iterable(sync_iterable_fn):
    """Runs a blocking/synchronous iterable-producing call on a background
    thread and yields its items asynchronously, via a thread + queue.Queue
    bridge -- for SDKs that only expose a synchronous streaming call with
    no async client (boto3)."""
    q = queue.Queue()
    sentinel = object()

    def worker():
        try:
            for item in sync_iterable_fn():
                q.put(item)
        except Exception as exc:
            q.put(exc)
        finally:
            q.put(sentinel)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = await asyncio.to_thread(q.get)
        if item is sentinel:
            return
        if isinstance(item, Exception):
            raise item
        yield item
