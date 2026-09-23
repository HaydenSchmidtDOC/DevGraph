/* Headless test of the Query telemetry card's MCP half, lifted verbatim out of
   index.html: the real summarize/render/fetch functions, the real
   refreshTelemetry that calls them, and the real bootConnect that decides what
   happens when Neo4j is unreachable. Everything they touch is fetch and
   document.getElementById, so a handful of stubs exercises the real code --
   no browser, deterministic, re-runnable.

   The failure this exists to catch is the one that shipped before it: the card
   claimed "Not wired" while a working endpoint sat unread, and every state of
   that card looked the same. So the checks below care about three things --
   that real figures reach the card, that no figure is ever invented (a zero
   call count and a 0.0% error rate are inventions when the store is simply
   unreadable), and that each metric's sample is disclosed rather than
   borrowed from the one beside it. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");

const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
/* The MCP path on its own: scoped so the "this card never reaches for Neo4j"
   checks below are about these functions, not about whatever else the file
   happens to mention. */
const mcpSrc = [
  grab(/^function formatMcpMs\(/m, "\n}"),
  grab(/^function mcpPercentile\(/m, "\n}"),
  grab(/^function summarizeMcpTelemetry\(/m, "\n}"),
  grab(/^function dashMcpRows\(/m, "\n}"),
  grab(/^function setMcpTelemetryUnavailable\(/m, "\n}"),
  grab(/^function renderMcpTelemetry\(/m, "\n}"),
  grab(/^async function attemptMcpTelemetry\(\)/m, "\n}"),
].join("\n");
// ...plus its only two callers, both real.
const refreshSrc = grab(/^async function refreshTelemetry\(\)/m, "\n}");
const consoleSrc = grab(/^async function runCypherAndDisplay\(/m, "\n}");
const src = mcpSrc + "\n" + refreshSrc;
const bootSrc = src + "\n" + grab(/^async function bootConnect\(\)/m, "\n}");

// --- stubs ------------------------------------------------------------
const mkEl = initialClass => {
  const classes = new Set(initialClass ? initialClass.split(" ") : []);
  return {
    textContent: "", title: "", style: {},
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(" ").filter(Boolean).forEach(c => classes.add(c)); },
  };
};
let els = {};
let fetchCalls = [];
let drawn = 0, logLoaded = 0;
let respond = async () => ({ ok: true, status: 200, json: async () => ({}) });
const sandboxGlobals = {
  document: { getElementById: id => els[id] || null },
  fetch: async (url, opts) => { fetchCalls.push({ url, opts }); return respond(); },
  // refreshTelemetry's other two jobs; not under test, but they must survive.
  drawChart: async () => { drawn++; },
  loadQueryLog: async () => { logLoaded++; },
  console,
};
const api = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { attemptMcpTelemetry, refreshTelemetry, summarizeMcpTelemetry, renderMcpTelemetry, formatMcpMs, mcpPercentile };")(
  ...Object.values(sandboxGlobals));

/* Everything bootConnect reaches for besides this card. Nothing here is under
   test -- the point is to let the real boot function run to either of its two
   ends, and to prove the MCP card does not depend on which one. probe is the
   Neo4j reachability check. */
let probe = async () => ({ results: [{ data: [{ row: [1] }] }] });
const bootGlobals = {
  ...sandboxGlobals,
  runCypher: async q => probe(q),
  neo4jConnected: false,
  neo4jStatus: mkEl(""),
  legendStatus: mkEl(""),
  populateRealRepos: async () => {},
  refreshGraph: async () => {},
  loadGitHistory: () => {},
  setHeapUnavailable: () => {},
  attemptMemoryMetrics: async () => {},
  attemptQueryTelemetry: async () => {},
  attemptCommunityDetection: async () => {},
  cy: { add: () => {} },
  buildElements: () => [],
  forceDirectedSettle: () => {},
  focusRotation: () => {},
};
const boot = new Function(...Object.keys(bootGlobals),
  bootSrc + "\nreturn { bootConnect, refreshTelemetry };")(...Object.values(bootGlobals));
// bootConnect fires its card reads without awaiting them, so give those
// promises a turn before asserting.
const flush = () => new Promise(r => setTimeout(r, 0));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
// Exactly the state the card is served in.
const SERVED_NEO4J_NOTE =
  "Active queries (Neo4j) is attempted live on connect via SHOW TRANSACTIONS and stays dashed if this build " +
  "refuses it. Queued transactions (Neo4j) has no source on this build and is never estimated.";
const reset = () => {
  els = {
    mcpPill: mkEl("sample-pill"),
    mcpCallsVal: mkEl("metric-val dim"),
    mcpTopToolsVal: mkEl("metric-val dim"),
    latencyVal: mkEl("metric-val dim"),
    errRateVal: mkEl("metric-val dim"),
    mcpNote: mkEl("placeholder-note"),
    neo4jQNote: mkEl("placeholder-note"),
    activeQVal: mkEl("metric-val dim"),
    queuedQVal: mkEl("metric-val dim"),
    repoSelect: mkEl(""),   // boot path only
  };
  els.mcpPill.textContent = "Checking…";
  els.mcpCallsVal.textContent = "—";
  els.mcpTopToolsVal.textContent = "—";
  els.latencyVal.textContent = "— / — / —";
  els.errRateVal.textContent = "—";
  els.mcpNote.textContent = "Reading MCP tool-call telemetry…";
  els.neo4jQNote.textContent = SERVED_NEO4J_NOTE;
  els.activeQVal.textContent = "—";
  els.queuedQVal.textContent = "—";
  fetchCalls = [];
  drawn = 0;
  logLoaded = 0;
};
const serves = body => async () => ({ ok: true, status: 200, json: async () => body });
const mcpFetches = () => fetchCalls.filter(c => String(c.url).startsWith("/api/mcp-telemetry"));
const MCP_ROWS = ["mcpCallsVal", "mcpTopToolsVal", "latencyVal", "errRateVal"];
const rowState = () => JSON.stringify(Object.fromEntries(
  MCP_ROWS.map(id => [id, els[id].textContent + " [" + els[id].className + "]"])));
// Dashed means a visible em-dash placeholder and nothing else, dimmed.
const allDashed = () => MCP_ROWS.every(id =>
  els[id].textContent.includes("—") && els[id].textContent.replace(/[—\/\s]/g, "") === "" &&
  els[id].classList.contains("dim"));
// No figure anywhere in the MCP half: not a count, not a percentage.
const noFiguresShown = () => MCP_ROWS.every(id => !/\d/.test(els[id].textContent) && !/%/.test(els[id].textContent));

/* One hand-computable store. 8 usable calls; durations sorted are
   [8.4, 12, 40, 65, 120, 300, 900, 2000] so nearest-rank gives
   P50 = index 3 = 65ms, P95 = P99 = index 7 = 2000ms = 2.0s; 2 of 8 recorded
   outcomes failed = 25.0%; search_component 4, impact_analysis 2, then a 1-1
   tie broken by name so find_callers precedes get_source. */
const POPULATED = { entries: [
  { ts: 1, tool: "search_component", duration_ms: 120, ok: true },
  { ts: 2, tool: "search_component", duration_ms: 8.4, ok: true },
  { ts: 3, tool: "search_component", duration_ms: 2000, ok: false },
  { ts: 4, tool: "search_component", duration_ms: 40, ok: true },
  { ts: 5, tool: "impact_analysis", duration_ms: 300, ok: true },
  { ts: 6, tool: "impact_analysis", duration_ms: 65, ok: false },
  { ts: 7, tool: "get_source", duration_ms: 12, ok: true },
  { ts: 8, tool: "find_callers", duration_ms: 900, ok: true },
] };
const populated = () =>
  els.mcpCallsVal.textContent === "8" && els.mcpPill.textContent === "MCP live" &&
  els.latencyVal.textContent === "65ms / 2.0s / 2.0s";

(async () => {
  // 1. real figures reach the card, hand-computed
  reset();
  respond = serves(POPULATED);
  await api.attemptMcpTelemetry();
  check("asks the MCP telemetry endpoint exactly once, for the whole window",
    mcpFetches().length === 1 && mcpFetches()[0].url === "/api/mcp-telemetry?limit=500",
    JSON.stringify(fetchCalls.map(c => c.url)));
  check("never reads MCP telemetry through the Cypher console or the console's own log",
    !fetchCalls.some(c => /\/api\/(cypher|query-log)/.test(String(c.url))),
    JSON.stringify(fetchCalls.map(c => c.url)));
  check("shows the real call total", els.mcpCallsVal.textContent === "8", els.mcpCallsVal.textContent);
  check("ranks the top tools by count, ties by name",
    els.mcpTopToolsVal.textContent === "search_component ×4 · impact_analysis ×2 · find_callers ×1",
    els.mcpTopToolsVal.textContent);
  check("shows nearest-rank P50 / P95 / P99 of the recorded durations",
    els.latencyVal.textContent === "65ms / 2.0s / 2.0s", els.latencyVal.textContent);
  check("shows the error rate over the recorded outcomes",
    els.errRateVal.textContent === "25.0%", els.errRateVal.textContent);
  check("undims every row it filled",
    MCP_ROWS.every(id => !els[id].classList.contains("dim")), rowState());
  check("marks the card live and names MCP as the source",
    els.mcpPill.textContent === "MCP live" && els.mcpPill.className === "wired-pill",
    els.mcpPill.textContent + "|" + els.mcpPill.className);
  check("the note states the window and both sample sizes, and that it is metadata only",
    /8 of up to the last 500 recorded calls/.test(els.mcpNote.textContent) &&
    /metadata only/.test(els.mcpNote.textContent) &&
    /no query text, arguments, or repository/.test(els.mcpNote.textContent) &&
    /cover 8 of those 8 calls/.test(els.mcpNote.textContent) &&
    /error rate covers 8/.test(els.mcpNote.textContent),
    els.mcpNote.textContent);
  check("the note no longer claims the card is unwired",
    !/not wired|needs a query-log store/i.test(els.mcpNote.textContent), els.mcpNote.textContent);
  check("the MCP half never rewrites the Neo4j rows' own explanation",
    els.neo4jQNote.textContent === SERVED_NEO4J_NOTE, els.neo4jQNote.textContent);
  check("the MCP half never writes the Neo4j rows",
    els.activeQVal.textContent === "—" && els.queuedQVal.textContent === "—",
    els.activeQVal.textContent + "|" + els.queuedQVal.textContent);

  // 2. latency formatting: sub-millisecond must not round into a fake 0ms
  for (const [ms, want] of [[0, "0.0ms"], [0.42, "0.4ms"], [9.96, "10.0ms"], [10, "10ms"],
                            [65.4, "65ms"], [999, "999ms"], [1500, "1.5s"], [2000, "2.0s"]]) {
    check(`${ms}ms renders as ${want}`, api.formatMcpMs(ms) === want, api.formatMcpMs(ms));
  }
  // ...and percentiles are real observations, never interpolated averages.
  const sample = [10, 20, 30, 40];
  check("nearest-rank percentiles only ever return a value from the sample",
    [50, 95, 99, 0, 100].every(p => sample.includes(api.mcpPercentile(sample, p))),
    JSON.stringify([50, 95, 99, 0, 100].map(p => api.mcpPercentile(sample, p))));
  check("the median of a four-call sample is the second value, not an average",
    api.mcpPercentile(sample, 50) === 20, String(api.mcpPercentile(sample, 50)));

  /* 3. malformed and non-finite fields drop out of their own sample instead of
     coercing. Every field here is the wrong type in a way a store rebuilt
     field-by-field on the server really can produce. */
  const MALFORMED = [
    { ts: 1, tool: "alpha", duration_ms: 50, ok: true },        // the only fully usable one
    { ts: 2, tool: "bravo", duration_ms: "12.5", ok: "true" },  // strings: a call, no duration, no outcome
    { ts: 3, tool: "charlie", duration_ms: Infinity, ok: 1 },
    { ts: 4, tool: "delta", duration_ms: -1, ok: null },
    { ts: 5, tool: "echo", duration_ms: NaN },
    { ts: 6, tool: "", duration_ms: 5, ok: true },              // unusable records: no tool name
    { ts: 7, tool: 42, duration_ms: 5, ok: true },
    { ts: 8 },
    null,
  ];
  const summary = api.summarizeMcpTelemetry(MALFORMED);
  check("counts only records that name a tool", summary.calls === 5, JSON.stringify(summary));
  check("reports how many records it could not read", summary.skipped === 4, JSON.stringify(summary));
  check("admits only finite, non-negative durations into the latency sample",
    summary.latencySample === 1 && summary.latency === "50ms / 50ms / 50ms", JSON.stringify(summary));
  check("admits only real booleans into the outcome sample",
    summary.okSample === 1 && summary.errors === 0, JSON.stringify(summary));
  check("neither sample can exceed the call count it is reported against",
    summary.latencySample <= summary.calls && summary.okSample <= summary.calls, JSON.stringify(summary));
  reset();
  respond = serves({ entries: MALFORMED });
  await api.attemptMcpTelemetry();
  check("discloses the sample behind each figure rather than implying the call count",
    /cover 1 of those 5 calls/.test(els.mcpNote.textContent) && /error rate covers 1/.test(els.mcpNote.textContent),
    els.mcpNote.textContent);
  check("says how many records were skipped",
    /4 record\(s\) in the store could not be read and were skipped/.test(els.mcpNote.textContent),
    els.mcpNote.textContent);
  check("a string duration is never coerced into the latency figure",
    !/12\.5/.test(els.latencyVal.textContent), els.latencyVal.textContent);

  // 4. per-metric independence: a populated card still dashes an empty sample
  reset();
  respond = serves({ entries: [{ ts: 1, tool: "alpha", duration_ms: "12.5", ok: "true" }] });
  await api.attemptMcpTelemetry();
  check("a call with no usable duration leaves latency dashed while the count shows",
    els.mcpCallsVal.textContent === "1" && els.latencyVal.textContent === "— / — / —" &&
    els.latencyVal.classList.contains("dim"), rowState());
  check("a call with no usable outcome leaves the error rate dashed, not 0.0%",
    els.errRateVal.textContent === "—" && els.errRateVal.classList.contains("dim"), rowState());
  check("the card is still live about what it does know", els.mcpPill.textContent === "MCP live",
    els.mcpPill.textContent);

  /* 5. no readable records. The endpoint returns {"entries": []} both when
     nothing has been recorded and when the store is missing or unreadable, so
     the card must claim neither -- and must not print a zero. */
  reset();
  respond = serves({ entries: [] });
  await api.attemptMcpTelemetry();
  const emptyNote = els.mcpNote.textContent;
  check("an empty store dashes every MCP row", allDashed(), rowState());
  check("an empty store shows no call count and no percentage", noFiguresShown(), rowState());
  check("the pill says there are no records, not that the card is unwired",
    els.mcpPill.textContent === "MCP: no records", els.mcpPill.textContent);
  check("the note names both possibilities instead of asserting either",
    /no connected MCP client has run a DevGraph tool yet/.test(emptyNote) &&
    /store is missing or unreadable/.test(emptyNote) &&
    /cannot tell these apart/.test(emptyNote), emptyNote);
  check("the note never claims a zero", !/\d/.test(emptyNote), emptyNote);
  check("an empty store leaves the Neo4j explanation alone",
    els.neo4jQNote.textContent === SERVED_NEO4J_NOTE, els.neo4jQNote.textContent);

  // ...and an all-malformed store is rendered the same way, because on the
  // wire it is the same thing, plus a count of what could not be read.
  reset();
  respond = serves({ entries: [{ ts: 1 }, { ts: 2, tool: "" }] });
  await api.attemptMcpTelemetry();
  check("an unreadable store renders exactly like an empty one",
    allDashed() && noFiguresShown() && els.mcpPill.textContent === "MCP: no records" &&
    els.mcpNote.textContent.startsWith(emptyNote), rowState() + " | " + els.mcpNote.textContent);
  check("...and adds only how many records it had to skip",
    els.mcpNote.textContent === emptyNote +
      " 2 record(s) in the store could not be read and were skipped.", els.mcpNote.textContent);

  /* 6. every way the read can fail is dashed and labelled unavailable --
     distinct from "no records", because here the card knows the endpoint
     itself did not answer usably. */
  const unavailable = [
    ["the endpoint returns an HTTP error",
      async () => ({ ok: false, status: 500, json: async () => ({ detail: "Neo4jError: procedure not found" }) })],
    ["the request fails outright", async () => { throw new TypeError("Failed to fetch"); }],
    ["the body is not JSON",
      async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError("Unexpected token <"); } })],
    ["the payload has no entries list", serves({})],
    ["entries is not a list", serves({ entries: "search_component" })],
    ["entries is null", serves({ entries: null })],
  ];
  for (const [label, responder] of unavailable) {
    reset();
    respond = responder;
    await api.attemptMcpTelemetry();
    check(`stays dashed when ${label}`, allDashed() && noFiguresShown(), rowState());
    check(`says unavailable in plain words when ${label}`,
      els.mcpPill.textContent === "MCP unavailable" &&
      /unavailable/i.test(els.mcpNote.textContent) &&
      /rather than shown as zero/.test(els.mcpNote.textContent),
      els.mcpPill.textContent + " | " + els.mcpNote.textContent);
    check(`does not claim there are no records when ${label}`,
      !/no connected MCP client/.test(els.mcpNote.textContent), els.mcpNote.textContent);
    check(`never renders relayed error text when ${label}`,
      !/Neo4jError|Failed to fetch|Unexpected token/.test(
        els.mcpNote.textContent + els.mcpPill.title + els.mcpCallsVal.title), els.mcpNote.textContent);
    check(`leaves the Neo4j explanation intact when ${label}`,
      els.neo4jQNote.textContent === SERVED_NEO4J_NOTE, els.neo4jQNote.textContent);
  }

  // 7. a failure after a success must take the live figures back down
  reset();
  respond = serves(POPULATED);
  await api.attemptMcpTelemetry();
  respond = async () => { throw new TypeError("Failed to fetch"); };
  await api.attemptMcpTelemetry();
  check("a later failure re-dashes a card that had gone live",
    allDashed() && noFiguresShown() && els.mcpPill.textContent === "MCP unavailable", rowState());
  check("...and clears the titles it had filled",
    MCP_ROWS.every(id => els[id].title === ""), JSON.stringify(MCP_ROWS.map(id => els[id].title)));

  /* 8. one read per refresh, and no polling. The card is deliberately refreshed
     from exactly two places -- boot and after a console query -- so a timer
     creeping in, or a second call site, is a regression. */
  reset();
  respond = serves(POPULATED);
  await api.refreshTelemetry();
  check("one refresh issues exactly one MCP telemetry request",
    mcpFetches().length === 1, JSON.stringify(fetchCalls.map(c => c.url)));
  check("the refresh still does its existing chart and console-log work",
    drawn === 1 && logLoaded === 1, `drawn=${drawn} logLoaded=${logLoaded}`);
  check("a refresh populates the card", populated(), rowState());
  check("the shared refresh is what reads MCP telemetry",
    /attemptMcpTelemetry\(\)/.test(refreshSrc), refreshSrc);
  check("the card is read once at boot", /^refreshTelemetry\(\);$/m.test(html),
    "index.html no longer calls refreshTelemetry at script scope");
  check("...and again after a console query, through the same shared refresh",
    /refreshTelemetry\(\);/.test(consoleSrc), consoleSrc);
  check("nothing polls it on a timer", !/setInterval/.test(html),
    "index.html now contains a setInterval");

  // 9. the MCP path is independent of the graph, by construction
  check("the MCP path never reaches for Neo4j",
    !/runCypher|neo4jConnected/.test(mcpSrc), mcpSrc);
  check("the MCP path never touches the Neo4j rows or their note",
    !/activeQVal|queuedQVal|neo4jQNote/.test(mcpSrc), mcpSrc);
  check("the MCP path never reads the Cypher console's own log",
    !/api\/query-log/.test(mcpSrc), mcpSrc);
  check("writes text through textContent, never innerHTML", !/innerHTML/.test(mcpSrc), mcpSrc);

  // 10. served markup: the card must start honest and label its sources
  check("the card no longer ships a 'Not wired' pill",
    !/id="mcpPill"[^>]*>Not wired</.test(html) && /id="mcpPill"[^>]*>Checking…</.test(html),
    "the Query telemetry pill is not served as Checking…");
  check("the stale query-log-store copy is gone",
    !/Needs a query-log store/.test(html) && !/dbms\.listQueries/.test(html),
    "index.html still carries the pre-wiring Query telemetry note");
  /* The same lie can hide outside this card -- the sibling query-rate card's
     tooltip and the loadQueryLog comment both used to send the reader to a
     "still unwired" Query telemetry card that now reports live MCP figures.
     Scoped by sentence rather than by counting words, so unrelated "Not wired"
     pills on other cards stay legal and only a claim *about this telemetry*
     fails. */
  const unwiredClaims = (html.match(/[^.<>]*\b(?:un-?wired|not wired)\b[^.<>]*/gi) || [])
    .filter(s => /\bMCP\b|Query telemetry/i.test(s));
  check("nothing on the page still calls MCP tool telemetry unwired",
    unwiredClaims.length === 0, unwiredClaims.join(" || "));
  check("every MCP row says it is MCP, and the call row states the 500-record window",
    /Tool calls \(MCP, last 500\)/.test(html) && /Top tools \(MCP\)/.test(html) &&
    /P50 \/ P95 \/ P99 latency \(MCP\)/.test(html) && /Error rate \(MCP\)/.test(html),
    "the Query telemetry rows are not labelled by source");
  check("the Neo4j rows say they are Neo4j",
    /Active queries \(Neo4j\)/.test(html) && /Queued transactions \(Neo4j\)/.test(html),
    "the Neo4j rows are not labelled by source");
  check("the Neo4j rows' explanation is served as static markup the MCP half cannot own",
    /id="neo4jQNote">[^<]*SHOW TRANSACTIONS/.test(html.replace(/<\/?code>/g, "")) &&
    /never estimated/.test(html),
    "the Neo4j explanation is missing from the served card");
  check("the stale two-value latency row is gone", !/P50 \/ P99 latency</.test(html),
    "index.html still serves the old P50 / P99 row");

  /* 11. the card's source is a local file the server reads, so it must render
     with the database down -- the state this dashboard is most often opened
     in. Boot itself must not read it twice either. */
  reset();
  probe = async () => { throw new TypeError("Failed to fetch"); };
  respond = serves(POPULATED);
  await boot.bootConnect();
  await flush();
  check("boot's Neo4j-unreachable branch does not read MCP telemetry itself",
    mcpFetches().length === 0, JSON.stringify(fetchCalls.map(c => c.url)));
  check("...and leaves the MCP rows exactly as served",
    allDashed() && els.mcpPill.textContent === "Checking…", rowState() + " | " + els.mcpPill.textContent);
  await boot.refreshTelemetry();
  check("the card populates with Neo4j unreachable", populated(), rowState());
  check("the pill is never left stuck on 'Checking…' with Neo4j down",
    els.mcpPill.textContent === "MCP live", els.mcpPill.textContent);
  check("the Neo4j explanation still stands with Neo4j down",
    els.neo4jQNote.textContent === SERVED_NEO4J_NOTE, els.neo4jQNote.textContent);

  // ...and the reachable branch behaves identically for this card.
  reset();
  probe = async () => ({ results: [{ data: [{ row: [1] }] }] });
  respond = serves(POPULATED);
  await boot.bootConnect();
  await flush();
  check("boot's connected branch does not read MCP telemetry either",
    mcpFetches().length === 0, JSON.stringify(fetchCalls.map(c => c.url)));
  await boot.refreshTelemetry();
  check("the card populates with Neo4j connected", populated(), rowState());

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
