"""Live TTFA (time-to-first-activity) monitor.

Tails logs/latency.jsonl -- written by server.py's chat WebSocket handler,
one JSON record per completed turn, across every platform -- and prints
each turn's TTFA alongside a rolling p95, flagging two independent alert
conditions:

  * a single turn's TTFA above --single-threshold-ms (a hard outlier), and
  * the rolling p95 above --p95-threshold-ms (a sustained regression).

This is the proof-of-work for the latency patterns applied elsewhere in
this repo (see deployers/aws.py's warmup and TTFA-signal work): a customer
watching this while using the portal should see TTFA stay low and flat
rather than spiking on cold starts.

Usage:
  ./run.sh                                  # in one terminal, if not already running
  .venv/bin/python perf/latency_monitor.py       # in another

  .venv/bin/python perf/latency_monitor.py --p95-threshold-ms 1500 --single-threshold-ms 4000
"""

import argparse
import json
import time
from collections import deque
from pathlib import Path

# .parent.parent, not .parent: this module lives in perf/ but the
# logs/ directory it reads/writes is at the repo root, next to
# server.py (which is what writes latency.jsonl in the first place).
ROOT = Path(__file__).parent.parent
DEFAULT_LOG = ROOT / "logs" / "latency.jsonl"

RED = "\033[91m"
RESET = "\033[0m"


def percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def follow(path, history):
    """Yields up to the last `history` existing lines, then newly appended
    ones as they land -- a small from-scratch tail -f, since this is a
    local file rather than a log aggregation service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    for line in path.read_text().splitlines()[-history:]:
        yield line
    with path.open("r") as f:
        f.seek(0, 2)  # end of file -- only new writes from here on
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            yield line.rstrip("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--p95-threshold-ms", type=float, default=2000, help="alert if rolling p95 TTFA exceeds this")
    parser.add_argument(
        "--single-threshold-ms", type=float, default=5000, help="alert if any single turn's TTFA exceeds this"
    )
    parser.add_argument("--window", type=int, default=20, help="rolling sample count used for the p95")
    parser.add_argument("--history", type=int, default=10, help="existing log lines to replay on startup")
    args = parser.parse_args()

    window = deque(maxlen=args.window)
    print(
        f"watching {args.log_file}  (rolling window={args.window} turns, "
        f"p95 alert>{args.p95_threshold_ms:.0f}ms, single-turn alert>{args.single_threshold_ms:.0f}ms)"
    )
    print("-" * 90)

    for raw in follow(args.log_file, args.history):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ttfa = rec.get("ttfa_ms")
        if ttfa is None:
            continue  # a turn that errored before any activity signal arrived

        window.append(ttfa)
        p95 = percentile(list(window), 95)
        p95_str = f"{p95:7.0f}ms" if p95 is not None else "    n/a"

        flags = []
        if ttfa > args.single_threshold_ms:
            flags.append(f"SINGLE-TTFA-ALERT (>{args.single_threshold_ms:.0f}ms)")
        if p95 is not None and p95 > args.p95_threshold_ms:
            flags.append(f"P95-ALERT (>{args.p95_threshold_ms:.0f}ms)")

        when = time.strftime("%H:%M:%S", time.localtime(rec.get("ts", time.time())))
        platform = str(rec.get("platform", "?"))
        agent_name = str(rec.get("agent_name", "?"))[:28]
        line = (
            f"[{when}] {platform:6} {agent_name:28} turn={str(rec.get('turn', '?')):<3} "
            f"ttfa={ttfa:7.0f}ms  total={rec.get('elapsed_ms', 0):7.0f}ms  "
            f"p95(last {len(window)})={p95_str}"
        )
        print(f"{RED}{line}  <-- {', '.join(flags)}{RESET}" if flags else line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
