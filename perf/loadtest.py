"""Concurrent session load test.

Two modes, both driving real WebSocket sessions against a running portal
server -- never a server-side simulation of what a session does:

  * MODE_CHAT (default): simulates N users repeatedly opening brand-new
    chat sessions (each session = a fresh WebSocket + create_session,
    exactly like clicking "Chat" in the UI) and sending one message per
    session, with a randomized think-time before Send -- to demonstrate the
    session-warmup and TTFA patterns (see deployers/aws.py) under real
    concurrency instead of one request at a time. The think-time is the
    key independent variable here: a long one gives create_session's
    background warmup ping time to finish before Send, a short one
    doesn't -- so the results directly show whether the pattern is
    actually buying anything, not just that the agent responds.

  * MODE_WARMUP_ONLY: measures session-start cost in isolation, with no LLM
    call at all -- each session opens, sends chat_ws's "warmup_only" message
    (see server.py), waits for "warmup_done", and closes, never sending a real
    chat message.

    Its headline figure is platform_startup_ms: what the agent platform itself
    spent before any of the agent's own code ran. That, not the total, is the
    number this mode exists to produce -- it's the one that can be held up
    against another provider's, because it doesn't move when the agent's
    dependencies or tool set change. agent_init_ms reports the other side (the
    agent's own imports and per-session setup) and cold_start_ms their sum, so
    a run shows both what the provider cost and what the agent cost on top.
    Only AWS reports the split today; on Azure/Gemini platform_startup_ms/
    agent_init_ms are None and only the totals are populated -- see
    deployers/__init__.py for why an unsplit total isn't comparable.

    Every record also carries client_queue_ms: how long that session waited
    inside the portal process before its warmup call was issued. Read it
    before believing anything else in a bad run. This mode starts N sessions
    at once against one agent, so the load generator is itself a shared
    resource, and time spent queueing on it is not the platform's -- but it
    used to be counted as though it were, which turned a stalled laptop into
    a 30s p75 "platform startup" for calls the platform served in ~2s.

Also importable: run_load_test()/compute_summary() are the shared engine
behind both this CLI and the portal's own interactive "Load Test" view
(server.py's /ws/loadtest) -- the portal drives the exact same real
WebSocket sessions against itself (loopback), rather than a separate
server-side simulation, so a load test run from the UI exercises the
identical client-facing path a real browser does.

Usage:
  .venv/bin/python perf/loadtest.py --agent-id <id> --users 5 --iterations 4
  .venv/bin/python perf/loadtest.py --agent-id <id> --mode warmup_only --users 5 --iterations 4

Writes one JSON record per session to --output (default
logs/loadtest_results.json), for visualization or offline analysis.
"""

import argparse
import asyncio
import json
import random
import time
from pathlib import Path

import websockets

# .parent.parent, not .parent: this module lives in perf/ but the
# logs/ directory it reads/writes is at the repo root, next to
# server.py (which is what writes latency.jsonl in the first place).
ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT = ROOT / "logs" / "loadtest_results.json"
DEFAULT_MESSAGE = "What is the current stock price of AAPL?"

MODE_CHAT = "chat"
MODE_WARMUP_ONLY = "warmup_only"

# Above this, a run's client_queue_ms is worth calling out rather than just
# reporting: sessions are then waiting on each other inside the portal
# process for long enough to be a visible share of a ~2s session start, so
# the run is partly measuring the load generator. Healthy runs sit in the
# single or low double digits of milliseconds (a session's queue time is
# just "get a thread, get the client"), and a laptop that stalled badly
# enough to matter produced multi-second values -- so anything in between is
# already a signal, and 250ms is deliberately well below the smallest gap
# that changed a conclusion.
CLIENT_QUEUE_WARN_MS = 250


def _exc_detail(exc):
    """str(exc), except for exception types whose own __str__ is empty --
    confirmed directly that both TimeoutError() and asyncio.TimeoutError()
    stringify to "" with no message at all (the exact shape a real 60s
    asyncio.wait_for(ws.recv(), ...) timeout below raises). A falsy ""
    then reads as *no error* everywhere downstream that checks
    truthiness -- this module's own compute_summary(), and the portal
    frontend's `if (msg.error)` -- found live: a real timeout waiting on a
    genuinely slow/stuck cloud call surfaced as a fake instant "session
    ready in 0.0s" success instead of a visible error. repr(exc) always
    includes the exception's class name, so it's never empty."""
    return str(exc) or repr(exc)


async def run_session(host, agent_id, user, iteration, think_time_s, message, timeout_s):
    uri = f"ws://{host}/ws/agents/{agent_id}"
    record = {"user": user, "iteration": iteration, "think_time_s": round(think_time_s, 2)}
    t_connect = time.monotonic()
    try:
        async with websockets.connect(uri) as ws:
            await asyncio.sleep(think_time_s)
            t_send = time.monotonic()
            await ws.send(json.dumps({"type": "user_message", "text": message}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
                msg = json.loads(raw)
                if msg["type"] == "answer_end":
                    record["ttfa_ms"] = msg.get("ttfa_ms")
                    record["elapsed_ms"] = round(msg["elapsed_seconds"] * 1000)
                    record["error"] = None
                    break
                if msg["type"] == "error":
                    record["ttfa_ms"] = None
                    record["elapsed_ms"] = round((time.monotonic() - t_send) * 1000)
                    record["error"] = msg.get("detail") or "the server reported an error with no detail message"
                    break
    except Exception as exc:
        record.setdefault("ttfa_ms", None)
        record.setdefault("elapsed_ms", round((time.monotonic() - t_connect) * 1000))
        record["error"] = _exc_detail(exc)
    record["ts"] = time.time()
    return record


async def run_session_warmup_only(host, agent_id, user, iteration, timeout_s):
    """Same shape as run_session's record (user/iteration/elapsed_ms/error/
    ts), but with warmup_ms instead of think_time_s/ttfa_ms -- no chat
    message is ever sent, so those concepts don't apply. elapsed_ms here is
    the whole connect-to-warmup-done round trip, which absent a real chat
    call should track warmup_ms closely; a gap between the two would itself
    be a signal (e.g. real connection-setup overhead outside the warmup
    task chat_ws's "warmup_only" branch actually measures)."""
    uri = f"ws://{host}/ws/agents/{agent_id}"
    record = {"user": user, "iteration": iteration}
    t_connect = time.monotonic()
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "warmup_only"}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
                msg = json.loads(raw)
                if msg["type"] == "warmup_done":
                    record["warmup_ms"] = msg.get("warmup_ms")
                    # This mode's headline number -- see the module docstring.
                    record["platform_startup_ms"] = msg.get("platform_startup_ms")
                    record["agent_init_ms"] = msg.get("agent_init_ms")
                    # The warmup call's own duration. On this path it tracks
                    # warmup_ms closely (nothing overlaps the wait here), but
                    # it's the figure that's directly comparable to what the
                    # chat panel reports, where they differ a lot.
                    record["cold_start_ms"] = msg.get("cold_start_ms")
                    # How long the session waited inside the server process
                    # before its warmup call went out. This is the field to
                    # read first when a run looks bad: it is the part of a
                    # slow session that is this machine's fault rather than
                    # the platform's, and at high concurrency it is where a
                    # thread-pool or event-loop bottleneck shows up. A run
                    # whose platform_startup_ms tail moves while this stays
                    # flat is measuring the platform; one where both move
                    # together is measuring the load generator.
                    record["client_queue_ms"] = msg.get("client_queue_ms")
                    record["error"] = None
                    break
                if msg["type"] == "error":
                    record["warmup_ms"] = None
                    record["error"] = msg.get("detail") or "the server reported an error with no detail message"
                    break
    except Exception as exc:
        record.setdefault("warmup_ms", None)
        record["error"] = _exc_detail(exc)
    record["ts"] = time.time()
    record["elapsed_ms"] = round((time.monotonic() - t_connect) * 1000)
    return record


async def run_user(host, agent_id, user, iterations, mode, think_min, think_max, message, timeout_s, on_result):
    for i in range(iterations):
        if mode == MODE_WARMUP_ONLY:
            rec = await run_session_warmup_only(host, agent_id, user, i, timeout_s)
        else:
            think_time = random.uniform(think_min, think_max)
            rec = await run_session(host, agent_id, user, i, think_time, message, timeout_s)
        result = on_result(rec)
        if asyncio.iscoroutine(result):
            await result


async def run_load_test(
    host, agent_id, users, iterations, on_result, mode=MODE_CHAT, think_min=1.0, think_max=10.0,
    message=DEFAULT_MESSAGE, timeout_s=60.0,
):
    """Runs `users` concurrent simulated users, `iterations` sessions each,
    calling on_result(record) as every individual session completes --
    not just once at the very end -- so a live caller (server.py's
    /ws/loadtest) can stream progress instead of blocking until the whole
    test is done. on_result may be sync or async. Doesn't return or
    accumulate results itself: the CLI wants a printed line + a list to
    write out, the portal wants a pushed WebSocket message + a running
    summary -- there's no one shared accumulation shape that serves both
    without the other having to unwrap it, so that's left to the caller.

    mode selects MODE_CHAT (a real chat message per session, the default)
    vs MODE_WARMUP_ONLY (session-start cost only, no LLM call at all --
    see run_session_warmup_only); think_min/think_max/message only apply
    to MODE_CHAT."""
    await asyncio.gather(
        *[
            run_user(host, agent_id, u, iterations, mode, think_min, think_max, message, timeout_s, on_result)
            for u in range(users)
        ]
    )


def _percentile(values, pct):
    """Interpolated percentile -- mirrors perf/latency_monitor.py's own
    percentile() exactly, so the two tools agree on what "p95" means
    rather than each picking a slightly different convention."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def compute_summary(results):
    """Aggregate stats over a completed (or in-progress) load test's
    records -- one shared place for this so the CLI and the portal's
    /ws/loadtest "done" message can't drift into disagreeing about what a
    load test's own summary numbers mean. Computes stats for whichever of
    ttfa_ms/elapsed_ms/warmup_ms actually appear in the records rather than
    being mode-aware itself: MODE_CHAT records have no warmup_ms key (so
    that block is harmlessly all-None) and MODE_WARMUP_ONLY records have no
    ttfa_ms (same treatment) -- callers decide what to display based on
    which fields are actually populated, not by threading mode through
    here too."""
    errors = [r for r in results if r.get("error")]

    # p99 is reported for completeness (the portal's warmup chart shows the
    # full p50/p75/p95/p99 ladder), but read it knowing the sample size it
    # comes from -- which now depends on how the run was shaped. A burst of
    # concurrent sessions is capped low enough (server.py's
    # LOADTEST_MAX_TOTAL_SESSIONS=60) that an interpolated p99 always lands
    # between the two slowest values and is effectively "the worst one,
    # nudged"; a sequential loop run of hundreds of session starts
    # (LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY=500, and no cap at all from
    # this module's own CLI) puts real observations above p99 and makes it an
    # actual tail estimate. Either way it's a real number, not a fake one --
    # the portal's chart note says which regime the run it's showing is in.
    def stats(field):
        values = [r[field] for r in results if r.get(field) is not None]
        if not values:
            return {"min": None, "mean": None, "p50": None, "p75": None, "p95": None, "p99": None, "max": None}
        return {
            "min": round(min(values)),
            "mean": round(sum(values) / len(values)),
            "p50": round(_percentile(values, 50)),
            "p75": round(_percentile(values, 75)),
            "p95": round(_percentile(values, 95)),
            "p99": round(_percentile(values, 99)),
            "max": round(max(values)),
        }

    return {
        "total": len(results),
        "errors": len(errors),
        "ttfa_ms": stats("ttfa_ms"),
        "elapsed_ms": stats("elapsed_ms"),
        "warmup_ms": stats("warmup_ms"),
        "platform_startup_ms": stats("platform_startup_ms"),
        "agent_init_ms": stats("agent_init_ms"),
        "cold_start_ms": stats("cold_start_ms"),
        "client_queue_ms": stats("client_queue_ms"),
    }


async def main_async(args):
    results = []

    def on_result(rec):
        results.append(rec)
        status = "ERROR: " + rec["error"] if rec["error"] else "ok"
        if args.mode == MODE_WARMUP_ONLY:
            # "n/a" rather than a total standing in for it, on a platform that
            # doesn't report the split -- see this module's docstring.
            platform = rec.get("platform_startup_ms")
            platform_text = f"{platform}ms" if platform is not None else "n/a"
            print(
                f"user={rec['user']} iter={rec['iteration']} platform_startup={platform_text} "
                f"agent_init={rec.get('agent_init_ms')}ms session_start={rec['warmup_ms']}ms  {status}"
            )
        else:
            print(
                f"user={rec['user']} iter={rec['iteration']} think={rec['think_time_s']:5.1f}s "
                f"ttfa={rec['ttfa_ms']}ms total={rec['elapsed_ms']}ms  {status}"
            )

    await run_load_test(
        args.host, args.agent_id, args.users, args.iterations, on_result,
        mode=args.mode, think_min=args.think_min, think_max=args.think_max, message=args.message,
        timeout_s=args.timeout,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2))
    summary = compute_summary(results)
    print(f"\nwrote {len(results)} records to {args.output}")
    if args.mode == MODE_WARMUP_ONLY:
        # cold_start_ms, not warmup_ms: the warmup's whole duration rather
        # than the part a caller waited on. They're within a millisecond of
        # each other on this path (nothing overlaps the wait here), but this
        # is the figure that lines up with the portal's stat tiles and with
        # the chat panel's "Session warmup, full cost" row -- falls back for
        # results produced before the field existed.
        session_start = summary["cold_start_ms"] if summary["cold_start_ms"]["mean"] is not None else summary["warmup_ms"]
        platform = summary["platform_startup_ms"]
        if platform["mean"] is not None:
            # Platform first, because it's what this mode is for: the
            # provider's own cost, comparable across providers.
            print(
                f"platform startup p50={platform['p50']}ms p75={platform['p75']}ms "
                f"p95={platform['p95']}ms  errors={summary['errors']}/{summary['total']}"
            )
            print(
                f"  this agent's own init p50={summary['agent_init_ms']['p50']}ms, "
                f"whole session start p50={session_start['p50']}ms"
            )
        else:
            print(
                f"whole session start p50={session_start['p50']}ms p75={session_start['p75']}ms "
                f"p95={session_start['p95']}ms  errors={summary['errors']}/{summary['total']}"
            )
            print("  platform startup not reported by this platform -- includes the agent's own init")
        queue = summary["client_queue_ms"]
        if queue["mean"] is not None:
            # Printed on every run, not only when it looks bad: whether the
            # numbers above describe the platform or this machine depends on
            # this one being small, so it belongs in the output of a good run
            # too rather than being something to go looking for after a bad
            # one. See deployers/__init__.py's latest_client_queue_ms.
            note = (
                "  <-- client-bound: the figures above are partly this machine's, not the platform's"
                if queue["p95"] >= CLIENT_QUEUE_WARN_MS
                else ""
            )
            print(
                f"  client queue p50={queue['p50']}ms p95={queue['p95']}ms max={queue['max']}ms{note}"
            )
    else:
        print(
            f"ttfa p50={summary['ttfa_ms']['p50']}ms p95={summary['ttfa_ms']['p95']}ms  "
            f"total p50={summary['elapsed_ms']['p50']}ms p95={summary['elapsed_ms']['p95']}ms  "
            f"errors={summary['errors']}/{summary['total']}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1:8910")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--mode", choices=[MODE_CHAT, MODE_WARMUP_ONLY], default=MODE_CHAT,
        help=f"{MODE_CHAT}: real chat message per session (default). "
        f"{MODE_WARMUP_ONLY}: session-start cost only, no LLM call at all.",
    )
    parser.add_argument("--users", type=int, default=5, help="concurrent simulated users")
    parser.add_argument("--iterations", type=int, default=4, help="sessions per user")
    parser.add_argument("--think-min", type=float, default=1.0, help="min think-time before Send (s)")
    parser.add_argument("--think-max", type=float, default=10.0, help="max think-time before Send (s)")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--timeout", type=float, default=60.0, help="per-response-message wait timeout (s)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
