# Latency: diagnosing and measuring it

What each number in the portal's latency card actually means, and the
interactive load test that turns those single-turn numbers into percentiles
(including a side-by-side comparison of two agents). Part of
[Agent Portal](../README.md).

## Diagnosing "why is this agent slow"

"Slow" collapses at least four different, independently-diagnosable
causes: a cold session (container/agent-object warmup), a wasteful
tool-call trajectory, a slow LLM call given how much it had to read/write,
or a throttled LLM call. Rather than a single elapsed-time number, every
turn's **trace panel** ("Show Trace" -> click a turn) opens with a
**Latency breakdown** card covering all four, built from data that's
already flowing through the existing per-turn WebSocket messages -- no
separate fetch, and it renders as soon as `answer_end` lands, well before
the trace transcript below it has finished indexing:

- **Time to first activity** / **total turn time** -- the existing TTFA/
  elapsed numbers, now with the rest of the story alongside them instead
  of standing alone as a single stat under the chat bubble.
- **Session warmup wait** -- how long this turn spent waiting on the
  session-warmup task fired when "Chat" was opened (see the latency
  patterns worked into `deployers/aws.py`). Large on turn 1 of a cold session, ~0 on every
  turn after -- directly separates "the container/session was cold"
  from "the LLM call itself was slow," which used to be indistinguishable
  from the outside. On AWS, this number itself splits further into
  **platform startup** (everything AgentCore Runtime did before a single
  line of this agent's own code ran -- routing and provisioning the
  `invoke_agent_runtime` call, plus starting the container) and **agent
  initialization** (this agent's own cost: its module imports, and
  `get_or_create_agent()` building the session's `Agent` object and
  reconnecting its MCP clients -- the FRED and AWS Web Search Gateway
  connections, the slowest part). That split is what makes the number
  comparable: platform startup doesn't move when this agent's dependencies
  or tool set change, so it's the one figure that can be held up against
  another provider's, while agent initialization is the part that's really
  this portal's own design (a fresh `Agent` + MCP reconnect per
  `session_id`, on top of a heavy `strands` import), not AWS's.

  Measured entirely inside `aws_hosted/main.py`, as durations rather than
  timestamps, so no cross-machine clock sync is involved: the container
  times its own `get_or_create_agent()` (`session_init_ms`) and reads its
  own process age off `/proc/self/stat` + `/proc/uptime` at the end of
  module import (`module_init_ms`, which therefore covers the interpreter
  and `opentelemetry-instrument` boot too, not just our imports). Both ride
  back as the one line of "content" on the warmup sentinel's response
  stream, which `deployers/aws.py`'s `_warm_up()` parses instead of
  discarding. `deployers/aws.py` then derives
  `agent_init_ms = session_init_ms + module_init_ms` and
  `platform_startup_ms = cold_start_ms - agent_init_ms`. Two details there
  are easy to get wrong and are load-bearing: the import cost counts *only*
  when the container was started for the session being measured
  (`container_age_ms <= cold_start_ms`) -- a pre-warmed instance already
  paid its imports before this request arrived, so charging them to this
  session would inflate the agent's share and shrink the platform's -- and
  the subtraction is from `cold_start_ms` (the warmup invoke's own duration),
  not `warmup_ms` (only the part *this caller* waited on), so the number
  doesn't move with when a caller happened to start waiting and the chat
  panel and load test report the same quantity.

  Where `cold_start_ms`'s clock starts is part of that, and got this wrong
  once. It runs from the moment the invoke is issued -- after the boto3
  client is in hand -- not from when the warmup task was created. Timing from
  task creation charges this process's own delay to the platform: waiting for
  a free thread in asyncio's default executor, and first touch of a region
  building the client, resolving credentials and loading the service model.
  That is small on an idle machine and unbounded on a busy one, and it landed
  inside `platform_startup_ms` via the subtraction above. A real
  20-concurrency load-test burst from a stalled laptop reported a **30s p75
  "platform startup"** for invokes CloudWatch showed AgentCore had served in
  ~2s, with zero throttles or errors. The delay is now measured on its own as
  `client_queue_ms` (below) rather than being hidden in the platform's half.

  The split requires the hosted agent to report its own init time on the warmup ping -- see `aws_hosted/main.py`.
- **Tool calls** -- the turn's ordered tool-call sequence, collapsed to
  `name x N` per repeated name (e.g. `get_stock_price x2`) rather than a
  flat list, so an actually-repeated call is easy to spot at a glance.
  Repetition isn't proof of a wasted call (the same tool for two different
  tickers is legitimate), so this is left for a human to judge rather than
  the portal guessing at "wasteful."
- **Tokens** -- input/output token counts for the turn, with input's delta
  against the *previous* turn in the same session (keyed by turn number,
  not arrival order -- usage messages resolve as separate background
  tasks and can arrive out of order across turns, see `_send_usage`'s
  docstring in `server.py`) -- surfaces a growing conversation context as
  a real, visible contributor to a turn getting slower, not just an
  invisible number. Also a turn-average output rate (tokens / total turn
  time) -- an honest "how dense was the output," not a precise generation-
  speed benchmark, since total turn time includes real tool round-trips
  and network latency, not just token generation.
- **Retries** -- AWS only for now: botocore reports how many attempts an
  `invoke_agent_runtime` call took via its own `ResponseMetadata`
  (confirmed directly against a real response, not assumed from docs --
  see `deployers/aws.py`'s `_stream_events`). Not a precise "this call was
  throttled" signal (botocore also retries some transient network/5xx
  errors), but a real, honest one: any retries mean something made the
  call slower than a single clean round trip. Other platform SDKs may not
  expose an equivalent count through the clients this portal uses, so
  `latest_retries()` returns `None` for them (not a false `0`) and the row
  is simply omitted -- see `deployers/__init__.py`'s interface docstring.
- **Client queue** (`client_queue_ms`) -- how long the session waited inside
  *this* process before its session-start call went out, on all three
  platforms. Not a platform metric and not part of any total above: it's the
  measurement's own honesty check, and the field to read first when a run
  looks bad (see the 30s-p75 incident above). Healthy values are single or
  low double-digit milliseconds -- it's just "get a thread, get the client" --
  so it's the tail that matters, not the median. The chat panel shows a row
  for it only past `LOADTEST_CLIENT_QUEUE_WARN_MS` (250ms); the load test
  reports it on every run, since whether the other figures describe the
  platform at all depends on this one being small. `None` where the call never
  went out, same "not measured, not a real zero" convention as everything
  else here.

Every deployer implements five small additions to the shared interface for
this: `latest_warmup_ms(session_state)`, `latest_agent_init_ms(session_state)`,
`latest_platform_startup_ms(session_state)`, `latest_client_queue_ms(session_state)`,
and `latest_retries(session_state)`, all plain `session_state` reads (unlike `latest_usage`, they need no
extra wait -- see `deployers/__init__.py`), populated during `stream_chat`
by timing the warmup-task await and, for AWS, reading the retry count off
the real API response. `server.py`'s `chat_ws` folds all of this into the
existing `answer_end`/`usage` messages rather than inventing a new message
type, and `_log_latency` writes the same fields to `logs/latency.jsonl`
(alongside `perf/loadtest.py`/`perf/latency_monitor.py` from earlier work) so the
richer story is available outside the UI too.

Verified live against a real deployed AWS agent, not just reasoned about:
a first turn showed `warmup_ms: 9870` out of an 18.0s total (session was
genuinely cold), `retries: 0` (not throttled), and two real tool calls;
the very next turn against the same warm session showed `warmup_ms: 0`
and TTFA dropping from ~11s to ~1s, with `input_tokens_delta: +1035`
showing the growing context -- cleanly separating "this turn was slow
because the session was cold" from "this turn was fast once warm," exactly
the distinction a single elapsed-time number can't make.

## Interactive load test

The **Load Test** button (next to New Agent) drives a small, real
concurrency test against one or two active agents from the browser,
without touching a terminal: pick an agent (or check "Compare 2 agents"
and pick two), a test mode, concurrent sessions, and -- in full-chat mode
only -- iterations per session, hit **Start Test**, and watch a live
progress bar/log fill in per agent as real sessions complete, followed by
a results view once every session is done.

The iterations field is hidden in platform-startup-only mode, where it never
described anything real: a warmup-only "iteration" is a whole separate
session (fresh WebSocket, fresh warmup, see `perf/loadtest.py`'s
`run_user`), so nothing iterates *within* a session there and concurrent
sessions is already the knob for how many session starts get measured.
Consequence worth knowing: that caps a platform-startup-only run at 20
measured session starts, so its p95/p99 rest on very few points -- read
them as "the slowest one or two", which is what the results view says
anyway.

Two test modes, each with its own results view:

- **Full chat turn** (default) -- each session sends one real chat message
  and waits for the full answer, exercising the real LLM call. Results:
  summary stat tiles plus two scatter charts (TTFA and total turn time,
  success in green and errors in red, per-session hover detail) -- two
  charts, not one dual-axis chart, since TTFA and total turn time are two
  genuinely different measures even though they share a unit.
- **Platform startup only** -- isolates session-start cost with **no
  LLM call at all**: each session opens, waits for the same warmup
  create_session() already fires in the background (chat_ws's
  `"warmup_only"` message), and closes, without ever sending a chat
  message.

  Its headline is **platform startup**, not the total. That's the whole
  point of the mode: the total bundles this agent's own imports and
  per-session setup into the provider's number, and that half moves whenever
  the agent's dependencies or tool set change -- so a total can't answer
  "how does this platform compare to that one," only "how does this exact
  agent compare to itself." Results, in order: a **Platform startup
  latency** bar chart across the full p50/p75/p95/p99 ladder, the
  per-session platform-startup scatter behind it, the p50/p95 tiles for both
  halves of the split, then the **Full session warmup** ladder and scatter
  -- because that total is still what a user actually waits through. Where a
  platform doesn't report the split, the full warmup leads
  instead, and the chart note says outright that it can't be compared
  against another provider's platform number. There's no TTFA/token/
  tool-call story to show at all once no LLM was ever called, so each metric
  that does exist gets shown two ways: distribution first, then every
  individual session behind it. The percentiles were originally four stat
  tiles; a bar chart of the
  same four numbers makes the *shape* of the tail legible (how far p95 and
  p99 stand off from p50) in a way four side-by-side numerals don't, and
  each bar keeps its value printed above it, so nothing is lost in the
  trade -- reading the ladder shouldn't require hovering four times. Only
  the Sessions/Errors tiles remain as tiles, since neither is a latency.

  p99 is included for completeness, and labeled on screen with what it can
  and can't mean *for the run being shown* -- which depends on the run's
  shape. A burst run is capped at `LOADTEST_MAX_TOTAL_SESSIONS = 60`
  sessions, and an interpolated p99 over ≤60 samples always lands between
  the two slowest values -- effectively "the worst one, nudged" rather than
  the independent tail estimate a p99 implies at thousands of samples. A
  sequential loop run (up to
  `LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY = 500`) does put real
  observations above p99, so above 100 sessions the note stops disclaiming
  and says it's a real tail estimate. Either way it's a real number, not a
  fake one, and `tests/test_loadtest.py` asserts that `p95 < p99 < max` at
  the small sample size so that half of the caveat can't quietly stop being
  true. Defaults to 1
  iteration per session (vs. chat mode's 3) -- this mode is
  naturally a "how many concurrent cold starts can this handle" check, so
  one start per simulated user is the more natural unit here than repeated
  turns per user. The split itself (`platform_startup_ms` vs.
  `agent_init_ms`) is AWS-only today -- see [Diagnosing "why is this agent
  slow"](#diagnosing-why-is-this-agent-slow) above for exactly how each half
  is measured and why the other platforms don't report it yet. This is the
  piece that actually answers "is this slow because of AWS's own cold start,
  or because of this portal's own imports and per-session MCP reconnection
  design" -- a question the total alone can't answer, since it bundles both
  together.

  Every run also reports **client queue** (`client_queue_ms`) as a tile beside
  Sessions/Errors, and says so loudly in a callout above the charts once its
  p95 crosses `CLIENT_QUEUE_WARN_MS`/`LOADTEST_CLIENT_QUEUE_WARN_MS` (250ms,
  deliberately mirrored between `perf/loadtest.py` and `static/app.js` so the
  CLI and the UI can't disagree about whether a run was client-bound; pinned
  by a test). This mode starts N sessions at once against one agent from one
  process, so the load generator is itself a shared resource -- and time spent
  queueing on it is not the platform's. It is no longer *inside* any figure on
  screen, which is the fix; the callout exists because a machine busy enough
  to queue its own requests was also a poor place to measure from, so the
  tails should be read as upper bounds. In a comparison run it's judged on the
  worse of the two agents, since both share the one portal process: if it
  saturated, neither side's numbers are clean.

  **LLM-free on AWS**: the hosted container short-circuits on a sentinel string
  *before* ever calling the model, so this mode measures pure session-start
  cost with no inference charge.

### Run shape: burst vs. loop

The form offers two shapes over the same engine, because "does this hold up
under simultaneous users" and "what does session start actually cost" are
different questions and want different runs:

- **Burst** -- N sessions at once, the original shape. Concurrency is the
  variable under test.
- **Loop** -- N sessions one after another. Concurrency is fixed at 1 and the
  *sample size* is the variable, which is what the platform-startup metric
  needs: 20 concurrent sessions can't produce a meaningful p99 and say
  nothing about whether session start drifts over a long run.

A loop run is not a second execution path. It's `run_load_test(users=1,
iterations=N)` -- `run_user` is already a sequential loop -- so the records,
the summaries, the charts, and the CLI's own output are all unchanged. The
shape selector is presentation over the same `users` x `iterations` call.

Two things in the results view do change with a long run, both because they
were written for n≤60: the per-session scatter's x-axis stops drawing one
divider and label per iteration past
`LOADTEST_MAX_ITERATION_AXIS_GROUPS = 12` groups (500 of each over ~550px is
not an axis) and falls back to plain run order, and dots shrink with the
sample count so 500 of them don't merge into a solid band. At n≥100 the
scatter also gains a rolling-median line, which is the actual payoff of a
long run -- "did session start degrade over 500 sessions" is invisible in a
dot cloud and obvious as a line through it.

This is still explicitly **not** a large-scale load-testing tool -- it's for
an interactive "does this hold up" or "what does this cost" check, not for
generating serious traffic against a real hosted agent (real cloud
cost/quota). `server.py`'s `/ws/loadtest` clamps concurrent sessions to 20,
iterations to 500, and -- this is the one that actually matters -- their
*product*: 60 total sessions for a chat run
(`LOADTEST_MAX_TOTAL_SESSIONS`), 500 for a platform-startup-only run
(`LOADTEST_MAX_TOTAL_SESSIONS_WARMUP_ONLY`), independent of the two
per-field caps. The two ceilings differ because what a session *costs*
differs: a platform-startup-only session makes no LLM call at all on
AWS, while 500 full chat turns is 500 real LLM calls. An unrecognized
mode gets the conservative 60.

`LOADTEST_MAX_USERS` stays at 20 regardless. Concurrency is what protects an
individual hosted agent from a runaway run, and raising the sample-size
ceiling is not a reason to raise the concurrency one -- a loop run's
concurrency is 1.

That combined cap exists because of a real mistake made building this: an
earlier version capped each field individually at 50, and a single
`users=9999/iterations=9999` request was silently accepted and clamped
down to a genuine 50 x 50 = 2500-session run against a real agent,
which had to be cancelled by hand mid-run. Confirmed live afterward that
the combined cap actually holds: the same oversized request now clamps to
20 x 3 = 60 sessions in chat mode.

Because a loop run can last half an hour where a burst lasted forty seconds,
the run itself became something you have to be able to watch and get out of:

- **Interim summaries.** Every `LOADTEST_PROGRESS_SUMMARY_EVERY = 10`
  sessions the server sends a `progress_summary` built with the *same*
  `compute_summary` the final `done` message uses, so a number read mid-run
  and the same number at the end can't disagree about what they mean. The
  portal re-renders its charts from these.
- **Stop.** A `stop` frame ends the run and keeps every session it completed,
  summarized as a shorter run. Before this the only way out mid-run was
  closing the tab, which discarded the whole run.
- **Bounded progress, not a log.** The per-session list is capped at the last
  5 rows (with errors kept in their own capped tail, since successes would
  otherwise flush them out of view within seconds) plus running p50/p95,
  error count, elapsed and estimated remaining. A 500-row scrolling list is
  not progress reporting.
- **Download JSON.** The run's records, in the same shape
  `perf/loadtest.py --output` writes, so a portal run and a CLI run stay
  interchangeable artifacts. Client-side from what the page already holds --
  the server keeps no run history.

The engine itself is `perf/loadtest.py` -- the same module the standalone CLI
load-test script already used, refactored so its core (`run_load_test`)
takes an `on_result` callback invoked as each session completes rather
than only returning a final list, without changing the CLI's own behavior.
`/ws/loadtest` calls it with a callback that pushes progress over the
WebSocket instead of printing to a terminal. Each simulated "session" is a
**real** WebSocket connection this server opens against its own
`/ws/agents/{agent_id}` endpoint (`SELF_HOST`, loopback) -- the identical
client-facing path a real browser's Chat view takes, driven N times
concurrently, not a separate server-side simulation of what a session
does. Session results are routed through an `asyncio.Queue` to one single
consumer loop rather than sent to the WebSocket directly from
`run_load_test`'s own concurrent worker tasks, since Starlette's
`WebSocket.send_json` isn't safe to call from multiple coroutines at once
and up to 20 of them can finish a session at essentially the same moment.
Disconnecting mid-test (closing the tab, clicking Back) cancels the
still-running load generation rather than leaving it firing real requests
against a real hosted agent with nobody watching.

A session that errors (confirmed live with a real `429 RESOURCE_EXHAUSTED`
Vertex AI quota error, not a simulated one) still reports a real
`elapsed_ms` -- how long it ran before failing -- but never a fabricated
`ttfa_ms`, since no activity signal ever actually arrived. `compute_summary()`
excludes those from the TTFA percentile/mean math rather than treating a
`null` as a `0` that would silently skew it down, and the TTFA chart omits
them with a note rather than plotting a fake point; the total-turn-time
chart includes them (colored red), since a real duration exists for that
metric either way.

Platform-startup-only mode has a failure of its own that isn't a raised
exception. AWS treats the warmup ping as best-effort and swallows
its errors on purpose -- right for a chat turn, whose real call surfaces any
problem itself -- so `wait_for_ready` returns an ordinary-looking duration for
a session that never started. In this mode the ping *is* the measurement, so
that duration was being reported as a session start: found live, a 40-session
run with expired credentials came back all-green at a p50 of 218ms, which is
how long it takes to be told a signature is invalid. The deployers now keep the
exception (`latest_warmup_error`, see `deployers/__init__.py`) instead of
dropping it, and `chat_ws`'s `warmup_only` branch sends the same `error` frame a
raised failure would, so a run against a broken endpoint reads as `errors=N/N`
with the platform's own message.

### Comparing two agents

Checking "Compare 2 agents" swaps the single Agent dropdown for two and
runs the *same* test config (mode/concurrency/iterations/message -- one
shared set of parameters, not independently configurable per side) against
each, **one agent fully at a time, not concurrently**. Nothing about the
two agents needs to match -- comparing across platforms (e.g. an AWS agent
and comparing two different deployments on the
*same* platform (a different runtime version, a larger container, a
different model) are both just "pick two active agents," with no
special-casing for either case.

Agents run sequentially rather than concurrently on purpose, and it's a
real tradeoff, not a free choice: the load generator and the real
hosted-agent calls it drives both run in this *same local process* (the
laptop running the portal), so two agents' tests running at once compete
for that one machine's own CPU, network stack, and (for a shared client
like AWS's, see below) connection pool -- confounding "which agent is
actually faster" with "how much local capacity did the other agent's
simultaneous test happen to consume." Running one agent fully before the
next isolates each agent's numbers from the other's local resource
footprint, at the real cost of the two runs no longer sharing identical
wall-clock conditions (one could, in principle, catch a moment of
different cloud-side load than the other). Confirmed this matters, not
guessed: **AWS's `deployers/aws.py` uses one shared `boto3` client for
every `invoke_agent_runtime` call in the whole process**, and botocore's
own default `Config().max_pool_connections` is 10 (checked directly against
the installed library) -- at the load test's own concurrency levels, and
especially with two AWS agents sharing that one pool at the same moment,
sessions beyond the 10th were queueing for a free pooled connection before
their real network call even started, and that queueing time was being
measured as if it were AgentCore's own cold-start latency. Raised to 50
(`_MAX_POOL_CONNECTIONS`) so the pool itself is never the bottleneck being
measured -- but sequential comparison removes the *cross-agent* version of
this class of confound entirely.

`/ws/loadtest`'s protocol is genuinely one shape for both 1 and 2 agents,
not two parallel ones: it takes a list of 1-2 `agent_ids` (not a singular
`agent_id`), tags every `session_result` with which `agent_id` produced it,
and `done` carries a `summaries` dict keyed by agent (a one-entry dict for
the single-agent case) instead of one `summary`. One `run_load_test()` call
per agent, run in sequence -- but a single agent's own run is still wrapped
so a failure there can't abort a still-pending agent later in the sequence;
that agent's summary simply reflects however many sessions it completed
before failing. The `LOADTEST_MAX_*` caps are applied per agent, not
combined across the comparison: a two-agent run can fire up to 2x the real
session count a single-agent run can (just not at the same moment now),
but neither individual agent ever exceeds the already-reviewed
single-agent ceiling, which is the safety property that actually matters
(protecting any one hosted agent/cloud resource from runaway concurrency).

The results view leads with a plain-language callout ("Agent A was 2.3x
faster than Agent B, TTFA p50: 1.2s vs 2.8s") based on the mode's primary
metric (platform startup for the platform-startup-only mode -- see below --
`ttfa_ms` for a full chat turn), then a grouped bar chart per metric (two
bars per group, one per agent) as the headline visual, then -- in
platform-startup-only mode -- both agents' raw sessions overlaid on a single
chart, then each agent's own numbers underneath as per-agent detail.

### The platform-startup-only headline is the platform's number, not the total

Comparing two platforms, the quantity under test is the platform's own
latency. The full warmup isn't that: it's platform startup *plus* each
agent's own imports and session construction. Leading with the total meant a
run comfortably inside a platform latency target read as if it might be over
it, no chart on screen could settle which half the extra came from, and two
agents with different dependency sets would differ for a reason that has
nothing to do with the platform being measured.

So when both agents report the split, comparison leads with a grouped
**Platform startup** chart (`platform_startup_ms`, p50-p99), and the **Full
session warmup** chart follows immediately as the second card. Both stay
because both are real: the first is the platform's latency, the second is
what a user actually waits through, and having them one card apart makes the
difference between them readable instead of hidden inside one bar. The
callout and the overlaid per-session scatter both switch to platform startup
along with the headline -- the scatter especially, since its caption claims
to show "the sessions behind those percentiles" and would otherwise be off
by the initialization time.

`platform_startup_ms` is AWS-only and derived by subtraction, so the switch
is conditional on *both* agents reporting it (`renderComparisonResults`'
`platformOnly`). In any run where one side has no split,
the view falls back to full warmup throughout. One-sided is treated as
unavailable rather than partially applied -- a platform-startup bar next to a
platform-plus-agent bar is a worse chart than an honest total.

That overlaid warmup chart replaced a pair of per-agent scatter charts, and
the reason is the y-axis: each single-agent chart auto-scales to its own
max, so two of them side by side made a 3x difference look like no
difference at all -- both clouds filling their own plot, identically,
with only the axis labels to tell you otherwise. One shared axis makes the
gap the thing you see first. The original objection to overlaying still
holds and is respected rather than overridden: sessions are *not* paired
across agents. Left-to-right is each agent's own run order, and the two
agents don't even run at the same time, so the chart is built and captioned
as one distribution against another, and nothing in it invites reading a
point against the point above it. Pairing is what was wrong; sharing a
scale isn't.

Agent identity is encoded twice in that chart, by color (accent blue /
warn amber -- the same pair the bars use) and by shape (circle vs diamond),
which is what makes it readable in grayscale or to anyone who can't
separate those two hues. Encoding agent identity in the fill means
per-session status can't also live there, so an errored session is drawn
as a red outline instead. That color pair is deliberately distinct from
the existing success/error green/red: reusing green/red for "which agent"
would collide with that pair's existing meaning in the very same results
view (each agent's *chat-mode* detail section still uses the success/error
legend for its own per-session status). Amber does now carry a second meaning
in this view -- "this run was partly bound by the portal," on the client-queue
tile's value and that callout's left border -- but deliberately never on
anything that encodes a series: no dot, swatch, bar, marker or line, so
nothing in the chart itself is ambiguous about which agent it belongs to.

Session-start-only comparison is also the one place the per-agent detail
isn't just the single-agent view reused wholesale. The agent-initialization
scatter is dropped there outright -- a ~100ms quantity, plotted twice, two
charts below a headline that's about a multi-second difference -- while its
stat tiles stay, so every number a solo run reports is still on screen for
both agents. Chat-mode comparison still reuses the single-agent view as-is
(its two scatters, TTFA and total turn time, are per-agent), which is why
`renderWarmupOnlyLoadtestResults` takes a `comparison` flag rather than the
scatters being pulled out of it for everyone.

That flag drops the per-agent **Warmup latency** bar chart too, so warmup
percentiles live in exactly one place in this view: the grouped chart up top,
which already shows both agents' full ladder side by side. A per-agent copy
underneath -- as its own bar chart, or as the stat tiles that preceded it --
is the same numbers a second time on one screen, directly below a chart that
compares them better than either copy can alone.

Which is why the grouped platform-startup-only charts carry **p50/p75/p95/p99**
where the chat-mode grouped charts carry p50/p75/p95: they're the only place
this view shows percentiles at all, so dropping the per-agent charts would
otherwise have quietly taken p99 out of the two-agent view entirely. Eight
bars still leave room for all eight value labels at this width. The notes sit
on the card whose numbers they're about -- the p99 caveat and the
runtime-startup explanation on the headline card, `LOADTEST_WARMUP_NOTE` on
the full-warmup card below it -- rather than on the overlaid scatter, which
keeps only what's specific to it (the shared axis, run order, omitted
sessions). The p99 caveat appears once even though both ladders reach p99:
the same paragraph twice, one card apart, reads as a rendering bug.

What each agent's detail keeps is what's genuinely per-agent and shown
nowhere else: its session/error counts, and -- of the AWS split -- the
agent-initialization half. The runtime-startup stat tiles are dropped there
when the headline chart is charting that same quantity for both agents at
four percentiles; what's left below is the half no chart in the view shows,
which is also what a reader needs to get from the headline number back to the
total.

Every bar in both charts carries its value printed above it, grouped bars
included -- except when a group gets too narrow for the labels to fit
without colliding, where they're dropped and the hover tooltips carry the
numbers alone.

Verified live end-to-end, not just reasoned about, including after
switching from concurrent to sequential comparison: a real two-agent
warmup-only comparison against two different AWS agents at 12 concurrent
sessions each (above the old, since-fixed 10-connection pool ceiling)
showed agent A's all 12 sessions completing (last at t=12.1s) before
agent B's *first* session started (t=19.2s) -- zero interleaving, sequential
exactly as intended -- with warmup times climbing smoothly from 9.2s to
11.9s across agent A's 12 concurrent sessions and no artificial cliff at
the old 10-connection boundary. A separate real chat-mode comparison
(from before this change) showed a genuine ~14x TTFA p50
difference (2.0s vs 28.5s -- consistent with this project's own
earlier-documented orchestration-overhead findings under
concurrency, not a fluke). Every validation path (same agent picked twice,
more than 2 agent_ids, a missing/inactive agent, a malformed request)
returns a clear error rather than starting a broken test.
