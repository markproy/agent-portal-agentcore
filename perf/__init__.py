"""Latency measurement tools, kept together because they deliberately agree
with each other: perf/loadtest.py's _percentile mirrors perf/latency_monitor.py's
percentile exactly, so the two never disagree about what "p95" means.

perf/loadtest.py is imported by server.py (it's the engine behind the portal's
own interactive Load Test view, not just a CLI), which is why this is a
real package rather than a scripts directory."""
