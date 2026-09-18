"""Tests for perf/loadtest.py's pure, network-free logic: percentile math and
summary aggregation. run_session()/run_load_test() themselves make real
WebSocket connections against a running server and are exercised manually
(perf/loadtest.py's own CLI, and the portal's interactive Load Test view) rather
than in this suite -- see README's "Testing" section for why real
network-behavior paths stay out of CI here."""

import asyncio
import re
from pathlib import Path

import pytest

from perf import loadtest
from perf.loadtest import MODE_CHAT, MODE_WARMUP_ONLY, _percentile, compute_summary


def test_percentile_empty():
    assert _percentile([], 50) is None


def test_percentile_single_value():
    assert _percentile([42], 50) == 42
    assert _percentile([42], 95) == 42


def test_percentile_interpolates():
    # 10 evenly-spaced values -- p50 should land in the middle, p95 near
    # (but not at) the top, matching perf/latency_monitor.py's own percentile()
    # exactly (compute_summary is meant to agree with it).
    values = list(range(1, 11))  # 1..10
    assert _percentile(values, 50) == pytest.approx(5.5)
    assert _percentile(values, 95) == pytest.approx(9.55)
    assert _percentile(values, 0) == 1
    assert _percentile(values, 100) == 10


def test_compute_summary_basic():
    results = [
        {"ttfa_ms": 100, "elapsed_ms": 500, "error": None},
        {"ttfa_ms": 300, "elapsed_ms": 900, "error": None},
    ]
    summary = compute_summary(results)
    assert summary["total"] == 2
    assert summary["errors"] == 0
    assert summary["ttfa_ms"]["min"] == 100
    assert summary["ttfa_ms"]["max"] == 300
    assert summary["ttfa_ms"]["mean"] == 200
    assert summary["elapsed_ms"]["mean"] == 700


def test_compute_summary_excludes_errored_sessions_from_latency_stats():
    # A session that errored before any activity signal has ttfa_ms=None --
    # must not silently become a 0 that skews the average down.
    results = [
        {"ttfa_ms": 200, "elapsed_ms": 800, "error": None},
        {"ttfa_ms": None, "elapsed_ms": 5000, "error": "timeout"},
    ]
    summary = compute_summary(results)
    assert summary["total"] == 2
    assert summary["errors"] == 1
    assert summary["ttfa_ms"]["mean"] == 200  # only the successful session counted
    assert summary["elapsed_ms"]["mean"] == 2900  # elapsed_ms is present for both


def test_compute_summary_all_errors_yields_none_stats_not_a_crash():
    results = [{"ttfa_ms": None, "elapsed_ms": None, "error": "boom"}]
    summary = compute_summary(results)
    assert summary["total"] == 1
    assert summary["errors"] == 1
    assert summary["ttfa_ms"] == {"min": None, "mean": None, "p50": None, "p75": None, "p95": None, "p99": None, "max": None}


def test_compute_summary_empty_results():
    summary = compute_summary([])
    assert summary["total"] == 0
    assert summary["errors"] == 0
    assert summary["ttfa_ms"]["mean"] is None


def test_compute_summary_computes_warmup_ms_stats_for_warmup_only_records():
    # MODE_WARMUP_ONLY records have no ttfa_ms key at all -- compute_summary
    # isn't mode-aware, it just computes stats for whichever fields are
    # actually present, so ttfa_ms should come back all-None here rather
    # than erroring on the missing key.
    results = [
        {"warmup_ms": 50, "elapsed_ms": 60, "error": None},
        {"warmup_ms": 150, "elapsed_ms": 165, "error": None},
    ]
    summary = compute_summary(results)
    assert summary["warmup_ms"]["mean"] == 100
    assert summary["warmup_ms"]["min"] == 50
    assert summary["warmup_ms"]["max"] == 150
    assert summary["ttfa_ms"] == {"min": None, "mean": None, "p50": None, "p75": None, "p95": None, "p99": None, "max": None}


def test_compute_summary_computes_platform_startup_and_agent_init_stats():
    # AWS-only fields (see deployers/aws.py's latest_platform_startup_ms
    # docstring) -- compute_summary treats them like any other optional field,
    # present or not, same as warmup_ms itself for MODE_CHAT records.
    results = [
        {"warmup_ms": 500, "agent_init_ms": 300, "platform_startup_ms": 200, "error": None},
        {"warmup_ms": 700, "agent_init_ms": 500, "platform_startup_ms": 200, "error": None},
    ]
    summary = compute_summary(results)
    assert summary["agent_init_ms"]["mean"] == 400
    assert summary["platform_startup_ms"]["mean"] == 200


def test_compute_summary_platform_startup_all_none_on_platforms_without_the_split():
    # Azure/Gemini records never carry these keys at all -- must come back
    # all-None, not a crash or a false zero.
    results = [{"warmup_ms": 500, "error": None}, {"warmup_ms": 700, "error": None}]
    summary = compute_summary(results)
    assert summary["agent_init_ms"]["mean"] is None
    assert summary["platform_startup_ms"]["mean"] is None


def test_compute_summary_computes_client_queue_stats():
    """The field that says whether the rest of the summary describes the
    platform at all: time a session spent inside the portal process before its
    session-start call went out. Read before anything else in a bad run -- a
    tail here means the run partly measured the load generator, which is
    exactly how a stalled laptop once produced a 30s p75 "platform startup"
    for calls the platform served in ~2s."""
    results = [
        {"warmup_ms": 2000, "client_queue_ms": 10, "error": None},
        {"warmup_ms": 2100, "client_queue_ms": 20, "error": None},
        {"warmup_ms": 2200, "client_queue_ms": 4500, "error": None},
    ]
    summary = compute_summary(results)
    assert summary["client_queue_ms"]["p50"] == 20
    assert summary["client_queue_ms"]["max"] == 4500
    # A queue tail says nothing about the platform figures alongside it, and
    # must not be folded into them: they're now measured from after the call
    # was issued (see deployers/aws.py's _warm_up).
    assert summary["warmup_ms"]["max"] == 2200


def test_compute_summary_client_queue_all_none_on_records_that_never_had_it():
    """Records written before the field existed, and MODE_CHAT records, which
    never carry it. All-None rather than a zero that would read as "the client
    was instant" -- the same convention as the platform-startup split above."""
    summary = compute_summary([{"warmup_ms": 500, "error": None}, {"ttfa_ms": 900, "error": None}])
    assert summary["client_queue_ms"]["mean"] is None


def test_client_queue_warning_threshold_matches_the_portals():
    """The CLI's summary line and the portal's Load Test view must not
    disagree about whether a run was client-bound: the same run viewed two
    ways would then be flagged in one place and pass silently in the other.
    There's no shared module between a Python CLI and a no-build-step browser
    script, so the constant is duplicated -- and pinned here."""
    app_js = (Path(__file__).parent.parent / "static" / "app.js").read_text()
    match = re.search(r"const LOADTEST_CLIENT_QUEUE_WARN_MS = (\d+);", app_js)
    assert match, "LOADTEST_CLIENT_QUEUE_WARN_MS not found in static/app.js -- renamed?"
    assert int(match.group(1)) == loadtest.CLIENT_QUEUE_WARN_MS


def test_compute_summary_computes_p75_and_p99():
    values = list(range(1, 11))  # 1..10, matches test_percentile_interpolates' own fixture
    results = [{"warmup_ms": v, "error": None} for v in values]
    summary = compute_summary(results)
    assert summary["warmup_ms"]["p75"] == round(_percentile(values, 75))
    assert summary["warmup_ms"]["p99"] == round(_percentile(values, 99))


def test_compute_summary_p99_sits_between_the_two_slowest_at_this_sample_size():
    """Not a quirk to fix -- the honest consequence of interpolating p99 over a
    small sample, and the reason the portal's warmup chart says so rather than
    implying a real tail estimate. This is one of two regimes now: a burst run
    stays here (capped at LOADTEST_MAX_TOTAL_SESSIONS=60), while a sequential
    loop run of hundreds of sessions gets a genuine tail estimate and the
    portal's note says so instead (see app.js's loadtestP99Note). Asserted so
    the small-sample claim can't quietly stop being true."""
    values = [100] * 17 + [900]  # 18 sessions, one clear outlier
    summary = compute_summary([{"warmup_ms": v, "error": None} for v in values])
    assert summary["warmup_ms"]["p95"] < summary["warmup_ms"]["p99"] < summary["warmup_ms"]["max"]
    assert summary["warmup_ms"]["p99"] == round(_percentile(values, 99))


def test_run_user_dispatches_to_warmup_only_session_in_that_mode(monkeypatch):
    # A pure dispatch check -- real WebSocket behavior is exercised
    # manually (see this module's own docstring), but which per-session
    # function gets called for which mode is plain, network-free logic
    # worth pinning down directly.
    calls = []

    async def fake_run_session(host, agent_id, user, iteration, think_time_s, message, timeout_s):
        calls.append("chat")
        return {"user": user, "iteration": iteration}

    async def fake_run_session_warmup_only(host, agent_id, user, iteration, timeout_s):
        calls.append("warmup_only")
        return {"user": user, "iteration": iteration}

    monkeypatch.setattr(loadtest, "run_session", fake_run_session)
    monkeypatch.setattr(loadtest, "run_session_warmup_only", fake_run_session_warmup_only)

    asyncio.run(loadtest.run_user("host", "agent", 0, 2, MODE_CHAT, 1.0, 1.0, "hi", 5.0, lambda rec: None))
    assert calls == ["chat", "chat"]

    calls.clear()
    asyncio.run(loadtest.run_user("host", "agent", 0, 2, MODE_WARMUP_ONLY, 1.0, 1.0, "hi", 5.0, lambda rec: None))
    assert calls == ["warmup_only", "warmup_only"]


def test_exc_detail_falls_back_to_repr_for_empty_str_exceptions():
    # Regression test for a real bug found live: a genuine 60s client
    # timeout during a load test surfaced as a fake "0.0s success" instead
    # of a visible error, because TimeoutError()'s own __str__ is "" --
    # falsy, so every downstream truthiness check (this module's own
    # compute_summary, the portal frontend's `if (msg.error)`) treated it
    # as no error at all.
    assert str(TimeoutError()) == ""  # confirms the premise this test guards against
    assert loadtest._exc_detail(TimeoutError()) == repr(TimeoutError())
    assert loadtest._exc_detail(asyncio.TimeoutError()) == repr(asyncio.TimeoutError())
    assert loadtest._exc_detail(asyncio.TimeoutError()) != ""


def test_exc_detail_prefers_a_real_message_when_present():
    assert loadtest._exc_detail(ValueError("connection refused")) == "connection refused"
