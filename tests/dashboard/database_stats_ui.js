/* Headless test of the Database & memory card's heap path, lifted verbatim
   out of index.html: attemptMemoryMetrics plus the bootConnect branch that
   decides whether it runs at all. Both reach only fetch, runCypher and
   document.getElementById, so a handful of stubs exercises the real
   functions -- no browser, deterministic, re-runnable.

   The failure this exists to catch is exactly the one that shipped: the card
   asked for a shape the server never returns, and every failure mode looked
   the same as "not wired yet". So the checks below care about two things --
   that a real reading actually reaches the card, and that no partial or
   fabricated reading ever does. */
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
const src = [
  grab(/^function setMemNote\(/m, "\n}"),
  grab(/^function setHeapUnavailable\(/m, "\n}"),
  grab(/^async function attemptMemoryMetrics\(\)/m, "\n}"),
].join("\n");
/* The boot path decides whether the heap read is even attempted, so it gets
   the same treatment: the real bootConnect, driven over stubs. */
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
let respond = async () => ({ ok: true, status: 200, json: async () => ({}) });
const sandboxGlobals = {
  document: { getElementById: id => els[id] || null },
  fetch: async (url, opts) => { fetchCalls.push({ url, opts }); return respond(); },
  console,
};
const api = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { attemptMemoryMetrics };")(...Object.values(sandboxGlobals));

/* Everything bootConnect reaches for besides the card itself. Nothing here
   is under test -- the point is to let the real boot function run to either
   of its two ends. probe is what the Neo4j reachability check does. */
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
  attemptQueryTelemetry: async () => {},
  attemptCommunityDetection: async () => {},
  cy: { add: () => {} },
  buildElements: () => [],
  forceDirectedSettle: () => {},
  focusRotation: () => {},
};
const boot = new Function(...Object.keys(bootGlobals),
  bootSrc + "\nreturn { bootConnect };")(...Object.values(bootGlobals));
// bootConnect fires the three card reads without awaiting them (they run in
// parallel by design), so give those promises a turn before asserting.
const flush = () => new Promise(r => setTimeout(r, 0));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
// Same initial classes/markup state the card is served with.
const reset = () => {
  els = {
    heapVal: mkEl("metric-val dim"),
    heapMeter: mkEl(""),
    memPill: mkEl("sample-pill"),
    memNote: mkEl("placeholder-note"),
    repoSelect: mkEl(""),   // boot path only
  };
  els.heapVal.textContent = "—";
  els.heapMeter.style.width = "0%";
  els.memPill.textContent = "Checking…";
  fetchCalls = [];
};
const serves = heap => async () => ({ ok: true, status: 200, json: async () => ({ heap }) });
const LIVE = { available: true, used_bytes: 536870912, max_bytes: 2147483648, used_percent: 25.0 };
const dashed = () =>
  els.heapVal.textContent === "—" && els.heapVal.classList.contains("dim") &&
  els.heapMeter.style.width === "0%";
const labelledUnavailable = () =>
  /unavailable/i.test(els.memPill.textContent) && /unavailable/i.test(els.memNote.textContent);

(async () => {
  // 1. a real reading reaches the card
  reset();
  respond = serves(LIVE);
  await api.attemptMemoryMetrics();
  check("asks the dedicated endpoint exactly once",
    fetchCalls.length === 1 && fetchCalls[0].url === "/api/database-stats",
    JSON.stringify(fetchCalls.map(c => c.url)));
  check("never routes the JMX read through the Cypher console endpoint",
    !fetchCalls.some(c => c.url === "/api/cypher"), JSON.stringify(fetchCalls.map(c => c.url)));
  check("shows live used and maximum heap",
    els.heapVal.textContent === "537MB / 2147MB", els.heapVal.textContent);
  check("undims the value once it is real",
    !els.heapVal.classList.contains("dim"), els.heapVal.className);
  check("fills the meter to the reported percentage",
    els.heapMeter.style.width === "25%", els.heapMeter.style.width);
  check("marks the card live", els.memPill.textContent === "Live" && els.memPill.className === "wired-pill",
    els.memPill.textContent + "|" + els.memPill.className);
  check("the note no longer claims heap is unwired",
    !/unavailable/i.test(els.memNote.textContent) && /live/i.test(els.memNote.textContent),
    els.memNote.textContent);

  // 2. meter bounds: the width written to the DOM can never leave 0-100
  for (const [percent, want] of [[0.4, "0.4%"], [100, "100%"], [140, "100%"], [-5, "0%"]]) {
    reset();
    respond = serves({ ...LIVE, used_percent: percent });
    await api.attemptMemoryMetrics();
    check(`a reported ${percent}% renders as a meter width of ${want}`,
      els.heapMeter.style.width === want, els.heapMeter.style.width);
  }

  // 3. every way the reading can be missing leaves the card dashed
  const missing = [
    ["the server reports the reading unavailable",
      serves({ available: false, used_bytes: null, max_bytes: null, used_percent: null })],
    ["the response has no heap object at all", async () => ({ ok: true, status: 200, json: async () => ({}) })],
    ["the endpoint returns an HTTP error",
      async () => ({ ok: false, status: 500, json: async () => ({}) })],
    ["the request fails outright", async () => { throw new Error("Failed to fetch"); }],
    ["the body is not JSON",
      async () => ({ ok: true, status: 200, json: async () => { throw new Error("not json"); } })],
    ["the values are not numbers", serves({ available: true, used_bytes: "537000000", max_bytes: "2e9", used_percent: "25" })],
    ["a value is null", serves({ ...LIVE, max_bytes: null })],
    ["a value is not finite", serves({ ...LIVE, max_bytes: Infinity })],
    ["the percentage is not a number", serves({ ...LIVE, used_percent: null })],
    ["the maximum is negative (unbounded heap)", serves({ ...LIVE, max_bytes: -1 })],
    ["used exceeds the maximum", serves({ available: true, used_bytes: 4000, max_bytes: 1000, used_percent: 400 })],
    ["the reading is zero", serves({ available: true, used_bytes: 0, max_bytes: 0, used_percent: 0 })],
  ];
  for (const [label, responder] of missing) {
    reset();
    respond = responder;
    await api.attemptMemoryMetrics();
    check(`stays dashed when ${label}`, dashed(),
      JSON.stringify({ val: els.heapVal.textContent, cls: els.heapVal.className, meter: els.heapMeter.style.width }));
    check(`says so in plain words when ${label}`, labelledUnavailable(),
      els.memPill.textContent + " | " + els.memNote.textContent);
  }

  // 4. a failure after a success must take the live values back down
  reset();
  respond = serves(LIVE);
  await api.attemptMemoryMetrics();
  respond = async () => { throw new Error("Failed to fetch"); };
  await api.attemptMemoryMetrics();
  check("a later failure re-dashes a card that had gone live", dashed() && labelledUnavailable(),
    JSON.stringify({ val: els.heapVal.textContent, cls: els.heapVal.className, pill: els.memPill.textContent }));

  // 5. no raw backend error text is rendered, and no markup is injected
  reset();
  respond = async () => ({ ok: false, status: 500, json: async () => ({ detail: "Neo4jError: procedure not found" }) });
  await api.attemptMemoryMetrics();
  check("never renders the server's raw error text",
    !/Neo4jError/.test(els.memNote.textContent + els.memPill.textContent), els.memNote.textContent);
  check("writes the note through textContent, never innerHTML", !/\.innerHTML\s*\+?=/.test(src), src);

  // 6. the removed browser-side JMX parsing must not come back
  check("the browser no longer issues the JMX Cypher call itself",
    !/queryJmx/.test(html), "index.html still contains a dbms.queryJmx call");
  check("the heap path does not call runCypher", !/runCypher/.test(src), src);
  check("the card no longer ships the pre-wiring 'Not wired' heap pill",
    !/id="memPill"[^>]*>Not wired</.test(html), "the Database & memory pill still says Not wired");

  /* 7. boot path. The heap read only happens on the branch where the Neo4j
     probe succeeds, so an unreachable server used to leave the card on its
     served "Checking…" pill -- indistinguishable from a reading still in
     flight, and never resolved. Both branches have to land somewhere
     honest. */
  const checking = () => els.memPill.textContent === "Checking…";
  for (const [label, failure] of [
    ["the probe request fails outright", async () => { throw new Error("Failed to fetch"); }],
    ["the probe comes back with a Neo4j error",
      async () => ({ errors: [{ message: "ServiceUnavailable" }] })],
  ]) {
    reset();
    probe = failure;
    // A live reading is on offer; the card must not end up showing one on a
    // branch where the database itself never answered.
    respond = serves(LIVE);
    await boot.bootConnect();
    await flush();
    check(`boot leaves the card dashed when ${label}`, dashed(),
      JSON.stringify({ val: els.heapVal.textContent, meter: els.heapMeter.style.width }));
    check(`boot labels the card unavailable when ${label}`, labelledUnavailable(),
      els.memPill.textContent + " | " + els.memNote.textContent);
    check(`boot never leaves the pill stuck on "Checking…" when ${label}`, !checking(),
      els.memPill.textContent);
  }
  check("the unreachable boot path says why, without raw backend text",
    /unreachable/i.test(els.memNote.textContent) && !/ServiceUnavailable/.test(els.memNote.textContent + els.memPill.title),
    els.memNote.textContent + " | " + els.memPill.title);

  // ...and the reachable branch still actually reads the heap.
  reset();
  probe = async () => ({ results: [{ data: [{ row: [1] }] }] });
  respond = serves(LIVE);
  await boot.bootConnect();
  await flush();
  check("boot reads the heap once Neo4j is reachable",
    els.heapVal.textContent === "537MB / 2147MB" && els.memPill.textContent === "Live" &&
    fetchCalls.some(c => c.url === "/api/database-stats"),
    els.heapVal.textContent + " | " + els.memPill.textContent + " | " + JSON.stringify(fetchCalls.map(c => c.url)));

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
