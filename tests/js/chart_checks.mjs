// Checks for static/app.js's load-test chart renderers -- the SVG-building
// functions, which are pure string math and the densest logic in the
// frontend. Run by tests/test_static_charts.py under node, or directly:
//
//   node tests/js/chart_checks.mjs
//
// Why this file exists at all: app.js is a no-build-step, no-framework
// script that reaches for `document` at load time, so it can't be imported
// under node, and this repo has no JS test tooling to add one. The chart
// functions themselves don't touch the DOM -- they take data and return SVG
// strings -- so they're extracted textually below and run in isolation.
// Ugly, and worth it: three separate rounds of changes to these charts each
// shipped a bug that a rendered-output assertion would have caught
// instantly (a y-axis that hid the very difference the chart existed to
// show, an x-axis that manufactured a rising trend out of concurrency, a
// value label clipping out of the viewBox), and each time the alternative
// was eyeballing a chart in a browser after a multi-minute live load test.
//
// Assertions here are about *rendered geometry and encoding*, not
// implementation: which marks appear, where they sit, what the axes say.
// That's what makes them worth keeping -- they'd survive a rewrite of the
// internals and still catch a chart that lies.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const APP_JS = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "static", "app.js");
const src = readFileSync(APP_JS, "utf8");

// Textual extraction, brace-matched. Skips the parameter list before hunting
// for the body's opening brace, since a destructured default like
// ({ charts = true } = {}) has braces of its own.
function grabFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found in static/app.js -- renamed or removed?`);
  let i = src.indexOf("(", start);
  let parens = 0;
  for (; i < src.length; i++) {
    if (src[i] === "(") parens++;
    else if (src[i] === ")" && --parens === 0) break;
  }
  i = src.indexOf("{", i);
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces reading ${name}`);
}

// Module-level consts are single statements terminated by a blank line.
function grabConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`${name} not found in static/app.js`);
  return src.slice(start, src.indexOf("\n\n", start));
}

const FUNCTIONS = [
  "loadtestRunOrder",
  "loadtestRunOrderAxisSvg",
  "loadtestDotRadius",
  "loadtestMedianLineSvg",
  "loadtestIterationAxisSvg",
  "loadtestHasIterationAxis",
  "loadtestP99Note",
  "loadtestScatterSvg",
  "loadtestMarkerSvg",
  "loadtestOverlayScatterSvg",
  "loadtestBarSvg",
  "loadtestGroupedBarSvg",
  "loadtestComparisonBarChartCard",
  "loadtestComparisonWarmupScatterCard",
  "loadtestComparisonCalloutHtml",
  "loadtestPrimaryMetric",
  "loadtestWarmupPoints",
  "loadtestWarmupMs",
  "loadtestWarmupStats",
  "loadtestStatTile",
  "loadtestClientQueueCalloutHtml",
  "renderWarmupOnlyLoadtestResults",
  "renderChatLoadtestResults",
  "renderComparisonResults",
];
const CONSTS = [
  "LOADTEST_AGENT_SHAPES",
  "LOADTEST_WARMUP_NOTE",
  "LOADTEST_PLATFORM_STARTUP_METRIC",
  "LOADTEST_PLATFORM_STARTUP_NOTE",
  "LOADTEST_WARMUP_PERCENTILES",
  "LOADTEST_MAX_ITERATION_AXIS_GROUPS",
  "LOADTEST_MEDIAN_LINE_MIN_POINTS",
  "LOADTEST_P99_SAMPLE_FLOOR",
  "LOADTEST_CLIENT_QUEUE_WARN_MS",
  "loadtestFmtMs",
  "loadtestChartLegendHtml",
];

// app.js's own escapeHtml, which the extracted functions call.
const escapeHtml = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const body = [...FUNCTIONS.map(grabFunction), ...CONSTS.map(grabConst)].join("\n");
// The consts come back too, so a check about a threshold can read the real
// value instead of restating the number and passing after someone changes it.
const chart = new Function("escapeHtml", `${body}\nreturn {${[...FUNCTIONS, ...CONSTS].join(",")}};`)(escapeHtml);

let failures = 0;
function check(label, condition, detail = "") {
  if (condition) {
    console.log(`ok   ${label}`);
  } else {
    failures++;
    console.log(`FAIL ${label}${detail ? `\n       ${detail}` : ""}`);
  }
}

// --- fixtures -----------------------------------------------------------
// Shaped like a real run: 6 concurrent users x 3 iterations, and per-user
// warmup times that differ. compute_summary's own stat dict shape.
const stats = (p50) => ({ min: p50 - 200, mean: p50, p50, p75: p50 + 300, p95: p50 + 2100, p99: p50 + 2500, max: p50 + 2500 });

function sessions({ users = 6, iterations = 3, base = 2600, step = 400, errored = [] } = {}) {
  const out = [];
  for (let it = 0; it < iterations; it++) {
    for (let u = 0; u < users; u++) {
      out.push({
        user: u,
        iteration: it,
        cold_start_ms: base + u * step,
        warmup_ms: base + u * step - 100,
        agent_init_ms: 100 + u,
        platform_startup_ms: base + u * step - (100 + u),
        ttfa_ms: 900 + u * 50,
        elapsed_ms: 4000 + u * 60,
        error: errored.some(([eu, ei]) => eu === u && ei === it) ? "boom" : null,
      });
    }
  }
  return out;
}

// The order the portal actually receives results in: concurrent sessions
// complete fastest-first, so arrival order IS ascending duration.
const byArrival = (rs) => [...rs].sort((a, b) => a.cold_start_ms - b.cold_start_ms);

const awsSummary = (p50 = 2800) => ({
  total: 18,
  errors: 0,
  cold_start_ms: stats(p50),
  warmup_ms: stats(p50 - 100),
  agent_init_ms: stats(100),
  platform_startup_ms: stats(p50 - 100),
});
const nonAwsSummary = (p50 = 900) => ({
  total: 18,
  errors: 0,
  cold_start_ms: stats(p50),
  warmup_ms: stats(p50 - 100),
  agent_init_ms: { min: null, mean: null, p50: null, p75: null, p95: null, p99: null, max: null },
  platform_startup_ms: { min: null, mean: null, p50: null, p75: null, p95: null, p99: null, max: null },
});
const chatSummary = () => ({ total: 18, errors: 0, ttfa_ms: stats(950), elapsed_ms: stats(4100) });
// Deliberately lopsided halves (2s of platform, 3s of agent init) rather than
// the realistic ~100ms of init: with a 100ms difference, a chart showing the
// wrong quantity looks right, which is exactly the bug these checks exist to
// catch.
const splitSummary = (platformStartup, agentInit) => ({
  total: 18,
  errors: 0,
  cold_start_ms: stats(platformStartup + agentInit),
  warmup_ms: stats(platformStartup + agentInit),
  agent_init_ms: stats(agentInit),
  platform_startup_ms: stats(platformStartup),
});

const svgs = (html) => html.match(/<svg[\s\S]*?<\/svg>/g) || [];
const circles = (svg) => [...svg.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="4" class="([^"]*)"/g)].map((m) => ({ x: +m[1], y: +m[2], cls: m[3] }));
// A diamond's first point is its top vertex, 5px above center.
const diamonds = (svg) => [...svg.matchAll(/<polygon points="([\d.]+),([\d.]+) [^"]*" class="([^"]*)"/g)].map((m) => ({ x: +m[1], y: +m[2] + 5, cls: m[3] }));
const rects = (svg) => [...svg.matchAll(/<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"[^>]*class="([^"]*)"><title>([^<]*)<\/title>/g)]
  .map((m) => ({ x: +m[1], y: +m[2], w: +m[3], h: +m[4], cls: m[5], title: m[6] }));
const texts = (svg, cls) => [...svg.matchAll(/<text x="([\d.]+)" y="([\d.]+)" class="([^"]*)" text-anchor="(\w+)"[^>]*>([^<]*)</g)]
  .map((m) => ({ x: +m[1], y: +m[2], cls: m[3], anchor: m[4], text: m[5] }))
  .filter((t) => t.cls === cls);
// Both axes' labels carry class="chart-axis-label"; the anchor is what tells
// them apart (y-axis ticks are right-aligned beside the axis, x-axis labels
// are centered under their slot).
const xLabels = (svg) => texts(svg, "chart-axis-label").filter((t) => t.anchor === "middle");
const yLabels = (svg) => texts(svg, "chart-axis-label").filter((t) => t.anchor === "end");

// --- run order, not completion order -----------------------------------
// The bug: with concurrent sessions, arrival order is ascending duration, so
// plotting arrival order drew a rising line for a perfectly steady agent.
{
  const arrival = byArrival(sessions());
  check(
    "fixture is degenerate in arrival order (guards the premise)",
    arrival.every((r, i) => i === 0 || arrival[i - 1].cold_start_ms <= r.cold_start_ms)
  );

  const ordered = chart.loadtestRunOrder(arrival);
  check(
    "run order is (iteration, user)",
    ordered.map((r) => `${r.iteration}.${r.user}`).join(" ") ===
      "0.0 0.1 0.2 0.3 0.4 0.5 1.0 1.1 1.2 1.3 1.4 1.5 2.0 2.1 2.2 2.3 2.4 2.5",
    ordered.map((r) => `${r.iteration}.${r.user}`).join(" ")
  );
  check("run order copies rather than mutating its input", arrival[0].cold_start_ms === 2600);

  // nonAwsSummary, so the full-warmup scatter is svg #1: a platform that
  // reports the split renders its own two cards above it (checked below).
  const scatter = svgs(chart.renderWarmupOnlyLoadtestResults(arrival, nonAwsSummary(), 700))[1];
  const ys = circles(scatter).map((c) => c.y);
  check("warmup scatter plots every session", ys.length === 18, `${ys.length}`);
  check("y is no longer a monotonic slope (the fake trend is gone)", !ys.every((y, i) => i === 0 || ys[i - 1] >= y), ys.join(","));
  check(
    "the same wave repeats once per iteration",
    JSON.stringify(ys.slice(0, 6)) === JSON.stringify(ys.slice(6, 12)) && JSON.stringify(ys.slice(6, 12)) === JSON.stringify(ys.slice(12)),
    ys.join(",")
  );

  const iterLabels = xLabels(scatter);
  check("x-axis names each iteration once, in order", iterLabels.map((t) => t.text).join(",") === "0,1,2", iterLabels.map((t) => t.text).join(","));
  const dividers = [...scatter.matchAll(/<line x1="([\d.]+)" y1="10" x2="[\d.]+" y2="140" class="chart-gridline"/g)].map((m) => +m[1]);
  check("one divider between each pair of waves", dividers.length === 2, `${dividers.length}`);
  check(
    "each iteration label sits between its own dividers",
    iterLabels[0].x < dividers[0] && iterLabels[1].x > dividers[0] && iterLabels[1].x < dividers[1] && iterLabels[2].x > dividers[1],
    `labels ${iterLabels.map((t) => t.x)} dividers ${dividers}`
  );

  const oneWaveHtml = chart.renderWarmupOnlyLoadtestResults(sessions({ iterations: 1 }), nonAwsSummary(), 700);
  const oneWave = svgs(oneWaveHtml)[1];
  check("a single-iteration run draws no dividers", !/y1="10" x2="[\d.]+" y2="140"/.test(oneWave));
  check("a single-iteration run draws no iteration labels", xLabels(oneWave).length === 0, xLabels(oneWave).map((t) => t.text).join(","));
  // Session-start-only mode has no iterations knob at all -- one session
  // start per simulated session is the whole unit there -- so single-wave is
  // the only shape that mode ever renders, and a title or note promising an
  // iteration axis the chart deliberately doesn't draw is a standing lie.
  check("...and no title or note claims an iteration axis either", !oneWaveHtml.includes("iteration)") && !oneWaveHtml.includes("by iteration"),
    (oneWaveHtml.match(/[^.]*by iteration[^.]*/g) || []).join(" | "));
  const manyWaveHtml = chart.renderWarmupOnlyLoadtestResults(sessions(), nonAwsSummary(), 700);
  check("a multi-wave run does say so, in both the title and the note",
    manyWaveHtml.includes("(per session, by iteration)") && manyWaveHtml.includes("grouped by iteration"));

  // A dropped session makes waves uneven -- grouping must come from the
  // points' own iteration values, not arithmetic on the user count.
  const gappy = svgs(chart.renderWarmupOnlyLoadtestResults(
    sessions().filter((r) => !(r.iteration === 1 && r.user === 2)), nonAwsSummary(), 700))[1];
  check("uneven waves still group into 3 iterations", xLabels(gappy).map((t) => t.text).join(",") === "0,1,2", xLabels(gappy).map((t) => t.text).join(","));

  const chatHtml = chart.renderChatLoadtestResults(byArrival(sessions()), chatSummary(), 700);
  check("chat mode's charts are ordered by iteration too", (chatHtml.match(/\(per session, by iteration\)/g) || []).length === 2);
  const chatYs = circles(svgs(chatHtml)[0]).map((c) => c.y);
  check("chat TTFA chart repeats per wave rather than sloping", JSON.stringify(chatYs.slice(0, 6)) === JSON.stringify(chatYs.slice(6, 12)));
  check("no chart still claims completion order", !chatHtml.includes("completion order"));

  const noIteration = chart.loadtestScatterSvg([{ y: 1, status: "ok", title: "t" }, { y: 2, status: "ok", title: "t" }], 700);
  check("points with no iteration field draw no axis and don't crash", !noIteration.includes("NaN") && !/y1="10" x2="[\d.]+" y2="140"/.test(noIteration));
}

// --- long loop runs -----------------------------------------------------
// A loop run is users=1 x N iterations, so every session is its own iteration
// group. Everything in these charts that scales with the group count or the
// point count breaks at N=500 without the gates below: 500 dividers, 500 axis
// numbers, and 500 r=4 dots merged into one solid band.
{
  check("dot radius shrinks with the run", [18, 80, 81, 200, 201, 500].map(chart.loadtestDotRadius).join(",") === "4,4,2.5,2.5,1.5,1.5",
    [18, 80, 81, 200, 201, 500].map(chart.loadtestDotRadius).join(","));

  const loopPoints = (n, valueFor = () => 2600) =>
    Array.from({ length: n }, (_, i) => ({ y: valueFor(i), status: "ok", title: `s${i}`, iteration: i, user: 0 }));

  const long = chart.loadtestScatterSvg(loopPoints(500, (i) => 2600 + (i % 7) * 50), 700);
  check("a 500-session scatter draws no per-iteration dividers", !/y1="10" x2="[\d.]+" y2="140"/.test(long));
  // Not xLabels(): the run-order axis anchors its end labels start/end so they
  // don't overhang the plot, so "an x-axis label" here is any axis label that
  // isn't a right-anchored y-axis tick.
  const runOrderLabels = (svg) => texts(svg, "chart-axis-label").filter((t) => t.anchor !== "end" || t.y > 150);
  check("...and 3 run-order labels instead of 500", runOrderLabels(long).length === 3, `${runOrderLabels(long).length}`);
  check("...numbered 1, middle, last -- 1-based, as a session count reads",
    runOrderLabels(long).map((t) => t.text).join(",") === "1,250,500", runOrderLabels(long).map((t) => t.text).join(","));
  check("...anchored so the end labels don't overhang the plot",
    long.includes('text-anchor="start"') && long.includes('text-anchor="end"'));
  check("...with every dot drawn small", (long.match(/r="1.5"/g) || []).length === 500, `${(long.match(/r="1.5"/g) || []).length}`);

  // The gate is on group count, not point count: 12 groups of many sessions
  // each is still a legible per-iteration axis, and that's the burst shape
  // this axis was designed for.
  check("12 iteration groups keep the per-iteration axis", chart.loadtestHasIterationAxis(loopPoints(12)));
  check("13 groups do not", !chart.loadtestHasIterationAxis(loopPoints(13)));
  const wideBurst = chart.loadtestScatterSvg(
    sessions({ users: 20, iterations: 5 }).map((r, i) => ({ y: r.cold_start_ms, status: "ok", title: "t", iteration: r.iteration, user: r.user })),
    700
  );
  check("a 100-session burst of 5 iterations keeps its dividers", (wideBurst.match(/y1="10" x2="[\d.]+" y2="140"/g) || []).length === 4,
    `${(wideBurst.match(/y1="10" x2="[\d.]+" y2="140"/g) || []).length}`);

  // A title promising "by iteration" over a run-order axis is the same
  // standing lie the single-wave case already guards against.
  const loopHtml = chart.renderWarmupOnlyLoadtestResults(
    Array.from({ length: 300 }, (_, i) => ({ user: 0, iteration: i, cold_start_ms: 2600 + (i % 5) * 40, warmup_ms: 2500, agent_init_ms: null, platform_startup_ms: null, error: null })),
    { ...nonAwsSummary(2600), total: 300 },
    700
  );
  check("a long loop run's titles and notes don't claim an iteration axis",
    !loopHtml.includes("by iteration") && !loopHtml.includes("grouped by iteration"),
    (loopHtml.match(/[^.]*by iteration[^.]*/g) || []).join(" | "));

  // The payoff of a long run: drift is invisible in a 500-dot cloud and
  // obvious as a line through it.
  const polyline = (svg) => {
    const m = svg.match(/<polyline points="([^"]+)" class="chart-median-line[^"]*"/);
    return m ? m[1].split(" ").map((p) => p.split(",").map(Number)) : null;
  };
  check("no median line on a short run", polyline(chart.loadtestScatterSvg(loopPoints(50), 700)) === null);
  check("a median line appears at 100 sessions", polyline(chart.loadtestScatterSvg(loopPoints(100, (i) => 2600 + (i % 9) * 60), 700)) !== null);

  const drifting = polyline(chart.loadtestScatterSvg(loopPoints(300, (i) => 2000 + i * 10), 700));
  check("...that rises when session start degrades over the run", drifting[0][1] > drifting[drifting.length - 1][1],
    `${drifting[0][1]} -> ${drifting[drifting.length - 1][1]}`);
  check("...one vertex per session", drifting.length === 300, `${drifting.length}`);
  check("...staying inside the plot", drifting.every(([x, y]) => x >= 46 && x <= 690 && y >= 10 && y <= 140));

  // A steady agent with one 30s outlier: the median has to ignore it, which
  // is the reason it's a median and not a mean.
  const spiky = polyline(chart.loadtestScatterSvg(loopPoints(200, (i) => (i === 100 ? 30000 : 2600)), 700));
  check("...and ignoring a lone outlier rather than bending toward it",
    new Set(spiky.map(([, y]) => y)).size === 1, `${new Set(spiky.map(([, y]) => y)).size} distinct y values`);

  check("p99's caveat calls itself out as small-sample below 100 sessions",
    chart.loadtestP99Note(20).includes('"the worst one"') && chart.loadtestP99Note(20).includes("20 session"));
  check("...and reads it as a real tail estimate at or above 100",
    chart.loadtestP99Note(500).includes("real tail estimate") && !chart.loadtestP99Note(500).includes('"the worst one"'));
  check("...with the boundary exactly at the sample floor",
    chart.loadtestP99Note(99).includes('"the worst one"') && chart.loadtestP99Note(100).includes("real tail estimate"));
  check("...and singular for a one-session run", chart.loadtestP99Note(1).includes("(1 session)"), chart.loadtestP99Note(1));
}

// --- client queue time: is this run's own machine in the numbers? --------
// The run this guards against reported a 30s p75 "platform startup" for
// invokes the platform had served in ~2s: the portal's clock started when the
// warmup task was created, so a stalled laptop's own queueing was charged to
// the platform. The measurement is fixed (deployers/aws.py's _warm_up), and
// what's left is telling the reader when the machine was loaded enough that
// the tails below are still suspect -- silently, on a good run.
{
  const queueStats = (p50, p95) => ({ min: p50 - 1, mean: p50, p50, p75: p50, p95, p99: p95, max: p95 + 100 });
  const withQueue = (q) => ({ ...nonAwsSummary(2600), client_queue_ms: q });
  const callout = chart.loadtestClientQueueCalloutHtml;

  check("a run from before the field existed says nothing", callout([nonAwsSummary()]) === "");
  check("...and neither does an all-null one (a platform that never measured it)",
    callout([withQueue({ min: null, mean: null, p50: null, p75: null, p95: null, p99: null, max: null })]) === "");
  check("a healthy run says nothing either -- queue time is milliseconds", callout([withQueue(queueStats(8, 40))]) === "");
  // The threshold is a tail test, deliberately: a client-side queue is a
  // contention effect, so a run can have a flat median and still be measuring
  // its own load generator at p95.
  check("...even with a slow max, as long as the tail is fine", callout([withQueue(queueStats(8, 40))]) === "");
  check("the boundary is inclusive at the threshold",
    callout([withQueue(queueStats(60, chart.LOADTEST_CLIENT_QUEUE_WARN_MS))]) !== "" &&
      callout([withQueue(queueStats(60, chart.LOADTEST_CLIENT_QUEUE_WARN_MS - 1))]) === "");

  const bad = callout([withQueue(queueStats(1400, 9000))]);
  check("a client-bound run is called out with all three of its numbers",
    bad.includes("1.4s") && bad.includes("9.0s") && bad.includes("9.1s"), bad);
  check("...saying plainly which side of the split it's on",
    bad.includes("bound by the portal, not the platform"));
  // The distinction that makes the callout honest rather than alarming: the
  // wait is no longer inside the figures on screen (that was the bug), so the
  // warning is about the machine being a poor vantage point, not about the
  // numbers being inflated by this amount.
  check("...and that it is not itself inside the figures below", bad.includes("<i>not</i> counted in any figure below"));

  // Both agents in a comparison share one portal process, so a saturated
  // process taints both sides -- judging on the better one would pass a run
  // where neither agent's numbers are clean.
  const mixed = callout([withQueue(queueStats(8, 40)), withQueue(queueStats(1400, 9000))]);
  check("a comparison is judged on its worst side, not its first", mixed.includes("9.0s"), mixed);
  check("...in either argument order", callout([withQueue(queueStats(1400, 9000)), withQueue(queueStats(8, 40))]) === mixed);
  check("a null side doesn't hide a bad one", callout([nonAwsSummary(), withQueue(queueStats(1400, 9000))]) === mixed);

  const healthyView = chart.renderWarmupOnlyLoadtestResults(sessions(), withQueue(queueStats(12, 45)), 700);
  check("the tile is shown on a good run too, next to Errors", healthyView.includes(">Client queue p95<"));
  check("...with the other two figures as its sub-label", healthyView.includes("p50 12ms / max 145ms"));
  check("...and no warning colour or callout", !healthyView.includes("loadtest-callout warn") &&
    !/loadtest-stat-tile warn[\s\S]*?Client queue/.test(healthyView));
  const boundView = chart.renderWarmupOnlyLoadtestResults(sessions(), withQueue(queueStats(1400, 9000)), 700);
  check("a client-bound run colours that tile and leads with the callout",
    boundView.startsWith('<div class="loadtest-callout warn">') && boundView.includes('<div class="loadtest-stat-tile warn">'));
  check("...still exactly once, not once per chart card", (boundView.match(/loadtest-callout warn/g) || []).length === 1,
    `${(boundView.match(/loadtest-callout warn/g) || []).length}`);
  check("a run with no queue figure shows no tile rather than an empty one",
    !chart.renderWarmupOnlyLoadtestResults(sessions(), nonAwsSummary(), 700).includes("Client queue"));

  // In the comparison view it has to precede the plain-language verdict: "A is
  // 4x faster than B" read off a client-bound run is the wrong conclusion, so
  // the caveat can't come after it.
  const comparison = chart.renderComparisonResults(
    ["a", "b"], ["A", "B"], { a: sessions({ base: 10600 }), b: sessions() },
    { a: { ...awsSummary(10762), client_queue_ms: queueStats(1400, 9000) },
      b: { ...awsSummary(2753), client_queue_ms: queueStats(9, 30) } }, "warmup_only", 900);
  check("the comparison view's callout precedes the faster/slower verdict",
    comparison.startsWith('<div class="loadtest-callout warn">') &&
      comparison.indexOf("bound by the portal") < comparison.indexOf("faster"));
  check("...appearing once for the pair, not once per agent", (comparison.match(/bound by the portal/g) || []).length === 1,
    `${(comparison.match(/bound by the portal/g) || []).length}`);
  // Kept per agent, unlike the platform-startup tiles the grouped chart
  // already charts: nothing else on this screen carries queue time, and which
  // of the two agents' sessions did the waiting is real information.
  check("...while each agent keeps its own queue tile", (comparison.match(/>Client queue p95</g) || []).length === 2,
    `${(comparison.match(/>Client queue p95</g) || []).length}`);
}

// --- single-agent warmup: the percentile bar chart ----------------------
{
  // nonAwsSummary for the bar mechanics, so the ladder under test is the full
  // warmup at svg #0. awsSummary's own layout -- platform startup first, the
  // full warmup below it -- is checked further down.
  const view = chart.renderWarmupOnlyLoadtestResults(sessions(), nonAwsSummary(2800), 700);
  check("the percentile card is titled 'Full session warmup'", view.includes('<span class="chart-card-title">Full session warmup</span>'));
  check("the per-percentile stat tiles are gone", !/Full warmup p\d/.test(view), (view.match(/Full warmup \w+/g) || []).join(","));
  check("Sessions and Errors tiles remain", view.includes(">Sessions<") && view.includes(">Errors<"));

  const bars = rects(svgs(view)[0]).filter((r) => r.cls === "chart-bar");
  check("one bar per percentile", bars.length === 4, `${bars.length}`);
  check("x-axis reads p50 p75 p95 p99", xLabels(svgs(view)[0]).map((t) => t.text).join(",") === "p50,p75,p95,p99",
    xLabels(svgs(view)[0]).map((t) => t.text).join(","));
  check("bars ascend with the percentile", bars.every((b, i) => i === 0 || bars[i - 1].h < b.h), bars.map((b) => b.h).join(","));
  check("bars sit on the baseline", bars.every((b) => Math.abs(b.y + b.h - 140) < 0.05));
  check("the tallest bar fills the plot height", Math.max(...bars.map((b) => b.h)) === 118, `${Math.max(...bars.map((b) => b.h))}`);
  check(
    "bars are centered in their slots",
    bars.every((b, i) => Math.abs(b.x + b.w / 2 - (46 + ((700 - 56) / 4) * (i + 0.5))) < 0.1)
  );
  check("hover titles still carry the numbers", bars[0].title === "p50: 2.8s", bars[0].title);

  const values = texts(svgs(view)[0], "chart-bar-value");
  check("every bar is labeled with its own value", values.map((t) => t.text).join(",") === "2.8s,3.1s,4.9s,5.3s", values.map((t) => t.text).join(","));
  check("no value label clips off the top of the viewBox", Math.min(...values.map((t) => t.y)) >= 8, `${Math.min(...values.map((t) => t.y))}`);
  check("each value label is centered over its own bar", values.every((t, i) => Math.abs(t.x - (bars[i].x + bars[i].w / 2)) < 0.1));

  check("the per-session scatter is still shown below it", view.includes("Full session warmup (per session, by iteration)"));
  check("the long warmup explainer appears exactly once", (view.match(/Session warmup, full cost \(once\)/g) || []).length === 1);
  check("p99's sample-size caveat is on screen next to it", view.includes("rather than as a tail estimate"));
  check("a platform without the split says its total can't be compared across providers",
    view.includes("can't be compared against another provider's platform number"));
  check("...and shows no platform-startup card at all", !view.includes("Platform startup") && !view.includes("Agent initialization"));

  // The whole point of the mode: on a platform that reports the split, the
  // platform's own number is the headline and the total follows it. A view
  // that led with the total would answer a different question than the one
  // this mode exists for.
  const aws = chart.renderWarmupOnlyLoadtestResults(sessions(), awsSummary(2800), 700);
  check("the AWS view leads with platform startup, not the total",
    aws.indexOf('<span class="chart-card-title">Platform startup latency</span>') > -1 &&
      aws.indexOf("Platform startup latency") < aws.indexOf("Full session warmup"),
    `${aws.indexOf("Platform startup latency")} vs ${aws.indexOf("Full session warmup")}`);
  check("...with its own per-session scatter directly under it",
    aws.indexOf("Platform startup (per session, by iteration)") < aws.indexOf("Full session warmup"));
  check("...the split's two halves as tiles", aws.includes("Platform startup p50") && aws.includes("Agent initialization p50"));
  check("...and the full warmup still there, below", aws.includes("Full session warmup (per session, by iteration)"));
  const awsHeadlineVals = texts(svgs(aws)[0], "chart-bar-value").map((x) => x.text);
  check("the AWS view's headline bars are the platform half alone, not the total",
    awsHeadlineVals[0] === "2.7s", awsHeadlineVals.join(","));
  check("p99's caveat rides on that first ladder only", (aws.match(/as a tail estimate/g) || []).length === 1);

  // Every session failed: no percentile has a value.
  const allErrors = chart.renderWarmupOnlyLoadtestResults(
    [{ user: 0, iteration: 0, cold_start_ms: null, warmup_ms: null, error: "boom" }],
    { total: 1, errors: 1, cold_start_ms: stats(0), warmup_ms: { mean: null }, agent_init_ms: { mean: null }, platform_startup_ms: { mean: null } },
    700
  );
  check("an all-errors run renders without NaN", !allErrors.includes("NaN"));
  const nullStats = { min: null, mean: null, p50: null, p75: null, p95: null, p99: null, max: null };
  const nullBars = chart.loadtestBarSvg(["p50", "p75", "p95", "p99"].map((p) => ({ label: p, value: nullStats[p], title: p })), 700);
  check("missing percentiles render as '--' rather than a zero-height bar", (nullBars.match(/>--</g) || []).length === 4 && !nullBars.includes('class="chart-bar"'));
}

// --- comparison: grouped bars, value labels, one overlaid scatter -------
{
  const names = ["v1-analyst (aws)", "v2-warmup (aws)"];
  const grouped = chart.loadtestComparisonBarChartCard(
    "Full session warmup", names,
    { p50: 10762, p75: 11180, p95: 11671 }, { p50: 2753, p75: 3132, p95: 3420 }, 900
  );
  const groupedValues = texts(grouped, "chart-bar-value");
  check("grouped bars label all six values", groupedValues.map((t) => t.text).join(",") === "10.8s,2.8s,11.2s,3.1s,11.7s,3.4s", groupedValues.map((t) => t.text).join(","));
  check("no grouped value label clips off the top", Math.min(...groupedValues.map((t) => t.y)) >= 8, `${Math.min(...groupedValues.map((t) => t.y))}`);
  const groupedBars = [...grouped.matchAll(/<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*class="chart-bar agent-([ab])"/g)]
    .map((m) => ({ center: +m[1] + +m[2] / 2, side: m[3] }));
  check("each grouped label is centered over its own bar", groupedBars.every((b, i) => Math.abs(b.center - groupedValues[i].x) < 0.1));
  check("agent A's bar precedes agent B's in every group", groupedBars.map((b) => b.side).join("") === "ababab");

  const narrow = chart.loadtestComparisonBarChartCard("t", ["A", "B"], { p50: 1, p75: 2, p95: 3 }, { p50: 1, p75: 2, p95: 3 }, 240);
  check("value labels are suppressed when too narrow to fit", !narrow.includes("chart-bar-value"));
  check("...and the hover tooltips still work there", (narrow.match(/<title>/g) || []).length === 6);
  const oneSided = chart.loadtestComparisonBarChartCard("t", ["A", "B"], { p50: 100, p75: 200, p95: 300 }, { p50: null, p75: null, p95: null }, 900);
  check("a missing side draws neither bar nor label", (texts(oneSided, "chart-bar-value").length === 3));

  // The overlaid scatter: the whole point is the shared y-axis.
  const fast = sessions({ base: 2600 });
  const slow = sessions({ base: 10600, errored: [[3, 1]] });
  const withDrop = [...fast, { user: 99, iteration: 2, cold_start_ms: null, warmup_ms: null, error: "dropped" }];
  const card = chart.loadtestComparisonWarmupScatterCard(["a", "b"], names, { a: slow, b: withDrop }, 700);
  const plot = svgs(card).find((s) => s.includes('viewBox="0 0 700 190"'));
  const a = circles(plot).filter((c) => c.cls.includes("agent-a"));
  const b = diamonds(plot).filter((d) => d.cls.includes("agent-b"));
  check("agent A plots as circles, agent B as diamonds", a.length === 18 && b.length === 18, `${a.length} circles / ${b.length} diamonds`);
  check("the session with no warmup value is omitted", a.length + b.length === 36);
  check("the shared axis puts the slower agent's whole cloud above the faster one's", Math.max(...a.map((p) => p.y)) < Math.min(...b.map((p) => p.y)),
    `A max ${Math.max(...a.map((p) => p.y))} vs B min ${Math.min(...b.map((p) => p.y))}`);
  const axisTop = Math.max(...yLabels(plot).map((t) => +t.text.replace(/,/g, "")));
  check("the y-axis is scaled to the global max, not either agent's own", axisTop === 12600, `${axisTop}`);
  check("every marker stays inside the plot area", [...a, ...b].every((p) => p.y >= 10 && p.y <= 170));
  check("both series span the full plot width independently",
    Math.min(...a.map((p) => p.x)) === 46 && Math.max(...a.map((p) => p.x)) === 690 &&
    Math.min(...b.map((p) => p.x)) === 46 && Math.max(...b.map((p) => p.x)) === 690);
  check("an errored session is marked by outline, not by fill", (plot.match(/chart-marker agent-a errored/g) || []).length === 1);
  check("iterations are labeled once for the pair, not once per agent", xLabels(plot).map((t) => t.text).join(",") === "0,1,2",
    xLabels(plot).map((t) => t.text).join(","));
  check("the dropped session is called out in the note", /1 errored session\(s\) omitted/.test(card));
  check("the legend names both agents", card.includes(names[0]) && card.includes(names[1]));
  check("the legend's shapes are the plot's shapes",
    /viewBox="0 0 12 12"[^>]*><circle[^>]*agent-a/.test(card) && /viewBox="0 0 12 12"[^>]*><polygon[^>]*agent-b/.test(card));
  check("the legend explains the error outline", card.includes("Errored session"));
  // A side that lost sessions must not cost the other side its axis. Short
  // series deliberately first, so reading iterations off series[0] rather
  // than the longest one would drop the labels entirely.
  const lopsided = chart.loadtestComparisonWarmupScatterCard(["a", "b"], ["A", "B"], { a: sessions().slice(0, 2), b: sessions() }, 700);
  const lopsidedLabels = xLabels(svgs(lopsided).find((s) => s.includes('viewBox="0 0 700 190"')));
  check("iteration labels come from the longest series, not the first",
    lopsidedLabels.map((t) => t.text).join(",") === "0,1,2", lopsidedLabels.map((t) => t.text).join(","));

  const single = chart.loadtestOverlayScatterSvg(
    [{ colorClass: "agent-a", shape: "circle", points: [{ y: 5, iteration: 0, status: "ok", title: "t" }] },
     { colorClass: "agent-b", shape: "diamond", points: [] }], 700);
  check("a one-point series is centered, not divided by zero", single.includes('cx="368.0"') && !single.includes("NaN"));
  const emptyOverlay = chart.loadtestOverlayScatterSvg([{ colorClass: "agent-a", shape: "circle", points: [] }], 700);
  check("an empty overlay still renders axes without NaN", !emptyOverlay.includes("NaN") && emptyOverlay.includes("chart-gridline"));
  check("all-zero values don't divide by zero",
    !chart.loadtestOverlayScatterSvg([{ colorClass: "agent-a", shape: "circle", points: [{ y: 0, iteration: 0, status: "ok", title: "t" }] }], 700).includes("NaN"));
  check("an unmeasured container falls back to 600px", chart.loadtestOverlayScatterSvg([{ colorClass: "agent-a", shape: "circle", points: [] }], 0).includes('viewBox="0 0 600 190"'));
  check("titles are HTML-escaped", chart.loadtestMarkerSvg("circle", 1, 1, "agent-a", '<script>&').includes("&lt;script&gt;&amp;"));
}

// --- what each view is and isn't --------------------------------------
{
  const names = ["A", "B"];
  const resultsByAgent = { a: sessions({ base: 10600 }), b: sessions({ base: 2600 }) };
  const summaries = { a: awsSummary(10762), b: awsSummary(2753) };
  const comparison = chart.renderComparisonResults(["a", "b"], names, resultsByAgent, summaries, "warmup_only", 900);
  check("warmup comparison shows exactly three charts: platform bars, full-warmup bars, overlay", (comparison.match(/class="chart-card"/g) || []).length === 3,
    `${(comparison.match(/class="chart-card"/g) || []).length}`);
  check("...the overlaid one being the shared-axis scatter", comparison.includes("(per session, both agents)"));
  check("no per-agent warmup scatter is repeated underneath", !comparison.includes("(per session, by iteration)"));
  check("the agent-initialization scatter is dropped from the comparison", !comparison.includes("Agent initialization time"));

  // Warmup percentiles live in exactly one place in this view: the grouped
  // chart up top. Repeating them per agent below -- as tiles or as a second
  // bar chart -- is the same numbers a third time on one screen.
  check("no per-agent percentile chart is repeated underneath",
    !comparison.includes("Platform startup latency") && !comparison.includes("(per session, by iteration)"));
  check("...nor a per-agent fallback to percentile stat tiles", !/Full warmup p\d/.test(comparison), (comparison.match(/Full warmup \w+/g) || []).join(","));
  const groupedCard = svgs(comparison)[0];
  check("the grouped chart carries the full p50-p99 ladder, so p99 isn't lost with them",
    xLabels(groupedCard).map((t) => t.text).join(",") === "p50,p75,p95,p99", xLabels(groupedCard).map((t) => t.text).join(","));
  check("...as 8 bars, still one per agent per percentile", rects(groupedCard).length === 8, `${rects(groupedCard).length}`);
  check("...still wide enough to label every one of them", texts(groupedCard, "chart-bar-value").length === 8,
    `${texts(groupedCard, "chart-bar-value").length}`);
  check("the full-warmup chart carries that same ladder", xLabels(svgs(comparison)[1]).map((t) => t.text).join(",") === "p50,p75,p95,p99",
    xLabels(svgs(comparison)[1]).map((t) => t.text).join(","));
  // Both notes move up to that card with the numbers they describe -- it's
  // the view's first mention of warmup, and now its only percentiles.
  check("the long warmup explainer appears exactly once", (comparison.match(/Session warmup, full cost \(once\)/g) || []).length === 1,
    `${(comparison.match(/Session warmup, full cost \(once\)/g) || []).length}`);
  check("p99's caveat appears exactly once, on the card with the p99 bars",
    (comparison.match(/as a tail estimate/g) || []).length === 1 &&
      comparison.indexOf("as a tail estimate") < comparison.indexOf("per session, both agents"),
    `${(comparison.match(/as a tail estimate/g) || []).length} occurrence(s)`);
  check("chat mode's grouped charts stay on p50/p75/p95, with no p99 caveat",
    !chart.renderComparisonResults(["a", "b"], names, resultsByAgent, { a: chatSummary(), b: chatSummary() }, "chat", 900).includes("as a tail estimate"));
  check("the per-agent agent-init half stays as stat tiles", (comparison.match(/Agent initialization p50/g) || []).length === 2);
  check("...without repeating platform startup, which the headline chart already charts", !comparison.includes("Platform startup p50"));
  check("Sessions and Errors tiles stay per agent", (comparison.match(/>Sessions</g) || []).length === 2 && (comparison.match(/>Errors</g) || []).length === 2);
  check("the plain-language callout leads, on platform startup", comparison.startsWith('<div class="loadtest-callout">') && comparison.includes("4.0x faster") && comparison.includes("platform startup p50"),
    comparison.slice(0, 200));
  check("both agents are labeled with their color dot", (comparison.match(/agent-color-dot/g) || []).length === 2);

  // Chat-mode comparison is deliberately untouched by all of the above.
  const chatComparison = chart.renderComparisonResults(
    ["a", "b"], names, resultsByAgent, { a: chatSummary(), b: chatSummary() }, "chat", 900);
  check("chat comparison keeps its 2 grouped bar charts + 2 scatters per agent", (chatComparison.match(/class="chart-card"/g) || []).length === 6,
    `${(chatComparison.match(/class="chart-card"/g) || []).length}`);
  check("chat comparison has no overlaid chart", !chatComparison.includes("both agents"));

  const soloEmpty = chart.renderWarmupOnlyLoadtestResults([], { total: 0, errors: 0, cold_start_ms: stats(0), warmup_ms: { mean: null }, agent_init_ms: { mean: null }, platform_startup_ms: { mean: null } }, 700);
  check("a zero-session run renders without crashing", soloEmpty.includes("Full session warmup") && !soloEmpty.includes("NaN"));
}

// --- the comparison headline is the platform's number, not the total ------
// The bug this exists for: "full session warmup" bundles AgentCore Runtime's
// own startup together with this agent's session construction, so a run
// meeting a p75 < 2s target on the runtime reads as over budget, and no chart
// on screen can settle which half the extra came from.
{
  const names = ["v1 (aws)", "v2 (aws)"];
  const results = { a: sessions({ base: 10600 }), b: sessions({ base: 2600 }) };
  const summaries = { a: splitSummary(9000, 3000), b: splitSummary(1800, 3000) };
  const view = chart.renderComparisonResults(["a", "b"], names, results, summaries, "warmup_only", 900);

  check("the headline chart is platform startup", view.indexOf('<span class="chart-card-title">Platform startup</span>') > -1);
  check("...and it leads: nothing about the full warmup precedes it",
    view.indexOf("Platform startup<") < view.indexOf("Full session warmup"), `${view.indexOf("Platform startup<")} vs ${view.indexOf("Full session warmup")}`);
  check("the full warmup is kept, one card down, and says what it adds up",
    view.includes("Full session warmup (platform startup + agent initialization)"));

  const [platformCard, warmupCard] = svgs(view);
  const vals = (svg) => texts(svg, "chart-bar-value").map((t) => t.text);
  check("the headline bars are the platform half alone", vals(platformCard).slice(0, 2).join(",") === "9.0s,1.8s", vals(platformCard).join(","));
  check("...so a p75 under the 2s target is readable as such", vals(platformCard)[3] === "2.1s", vals(platformCard).join(","));
  check("the second card's bars are the two halves together", vals(warmupCard).slice(0, 2).join(",") === "12.0s,4.8s", vals(warmupCard).join(","));
  check("the two cards therefore disagree, which is the whole point", vals(platformCard)[0] !== vals(warmupCard)[0]);
  check("the headline note says the agent's own init is excluded",
    view.includes("per-session setup are excluded"));

  // The scatter has to plot what the headline chart measures, or "the sessions
  // behind those percentiles" is a lie by 100ms.
  const plot = svgs(view).find((g) => g.includes('viewBox="0 0 900 190"'));
  check("the overlaid scatter is titled for platform startup too", view.includes("Platform startup (per session, both agents)"));
  check("...and its points are platform-startup values", /platform startup 2\.5s/.test(view) && !/: warmup /.test(view),
    (view.match(/iter 0: [a-z ]+ [\d.]+s/g) || []).slice(0, 2).join(" | "));
  check("its markers still land inside the plot", circles(plot).every((c) => c.y >= 10 && c.y <= 170));

  // Azure/Gemini never report the split, so there's nothing to separate and
  // this view stays exactly as it was.
  const nonAws = chart.renderComparisonResults(["a", "b"], names, results, { a: nonAwsSummary(1200), b: nonAwsSummary(900) }, "warmup_only", 900);
  check("platforms without the split keep the full-warmup headline", nonAws.includes('<span class="chart-card-title">Full session warmup</span>') && !nonAws.includes("Platform startup"));
  check("...as two charts, not three", (nonAws.match(/class="chart-card"/g) || []).length === 2, `${(nonAws.match(/class="chart-card"/g) || []).length}`);
  check("...with the full-warmup scatter and its own callout wording", nonAws.includes("Full session warmup (per session, both agents)") && nonAws.includes("warmup p50"));
  check("...and p99's caveat still on screen once", (nonAws.match(/as a tail estimate/g) || []).length === 1);

  // One side missing the split would pit a platform number against a
  // platform-plus-agent one, so both sides have to have it or neither does.
  const mixed = chart.renderComparisonResults(["a", "b"], names, results, { a: splitSummary(9000, 3000), b: nonAwsSummary(900) }, "warmup_only", 900);
  check("a one-sided split falls back to the full warmup rather than half-comparing",
    mixed.includes('<span class="chart-card-title">Full session warmup</span>') && !mixed.includes("Platform startup<"));
}

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
