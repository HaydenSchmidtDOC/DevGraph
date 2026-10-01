/* Headless test of the Database & memory card, lifted verbatim out of
   index.html: the render/poll functions plus the bootConnect branch that
   decides how the card is first read. They reach only fetch, runCypher,
   setInterval and document.getElementById, so a handful of stubs exercises
   the real functions -- no browser, deterministic, re-runnable.

   What this exists to catch: a real reading that never reaches the card,
   and a partial or fabricated reading that does. Each group renders on its
   own, so one group's failure must not dash the others, and none may show a
   value it can't stand behind. */
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
  grab(/^const MEM_POLL_MS\b/m, ";"),
  grab(/^let memPollTimer\b/m, ";"),
  grab(/^const STORE_REASONS\b/m, "};"),
  grab(/^function formatMemBytes\(/m, "\n}"),
  grab(/^function setMemNote\(/m, "\n}"),
  grab(/^function memVal\(/m, "\n}"),
  grab(/^function renderHeap\(/m, "\n}"),
  grab(/^function renderSystem\(/m, "\n}"),
  grab(/^function renderGc\(/m, "\n}"),
  grab(/^function renderPagecache\(/m, "\n}"),
  grab(/^function renderStore\(/m, "\n}"),
  grab(/^function setMemUnavailable\(/m, "\n}"),
  grab(/^async function attemptMemoryMetrics\(/m, "\n}"),
  grab(/^function renderSpark\(/m, "\n}"),
  grab(/^async function attemptMemoryHistory\(/m, "\n}"),
  grab(/^function startMemoryPolling\(/m, "\n}"),
].join("\n");
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
const mkCanvas = () => {
  const ops = [];
  const ctx = new Proxy({}, {
    get: (_t, name) => (typeof name === "string" && /^[a-z]/.test(name) && !["strokeStyle", "lineWidth", "lineJoin", "fillStyle"].includes(name))
      ? (...args) => ops.push([name, ...args]) : undefined,
    set: () => true,
  });
  return { ...mkEl(""), clientWidth: 100, width: 0, height: 0, ops, getContext: () => ctx };
};
let els = {};
let fetchCalls = [];
let routes = {};
let intervals = [];
const fetchStub = async (url, opts) => {
  fetchCalls.push({ url, opts });
  const handler = routes[url.split("?")[0]];
  if (!handler) return { ok: false, status: 404, json: async () => ({}) };
  return handler(url);
};
const sandboxGlobals = {
  document: { getElementById: id => els[id] || null },
  fetch: fetchStub,
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  console,
};
const api = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { attemptMemoryMetrics, renderSpark, attemptMemoryHistory, startMemoryPolling, formatMemBytes };")(
  ...Object.values(sandboxGlobals));

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
const flush = () => new Promise(r => setTimeout(r, 0));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const VALUE_IDS = ["heapVal", "ramVal", "swapVal", "cpuVal", "gcVal", "pageCacheVal", "storeSizeVal"];
const reset = () => {
  els = { memPill: mkEl("sample-pill"), memNote: mkEl("placeholder-note"), repoSelect: mkEl(""),
          heapMeter: mkEl(""), ramMeter: mkEl(""), storeSplitVal: mkEl(""),
          heapSpark: mkCanvas(), ramSpark: mkCanvas(), storeSpark: mkCanvas() };
  for (const id of VALUE_IDS) { els[id] = mkEl("metric-val dim"); els[id].textContent = "—"; }
  els.cpuVal.textContent = "— / —";
  els.heapMeter.style.width = "0%";
  els.ramMeter.style.width = "0%";
  els.memPill.textContent = "Checking…";
  fetchCalls = [];
  intervals = [];
  routes = {};
};
const json = body => async () => ({ ok: true, status: 200, json: async () => body });
const serveStats = body => { routes["/api/database-stats"] = json(body); };
const LIVE_HEAP = { available: true, used_bytes: 536870912, max_bytes: 2147483648, used_percent: 25.0 };
const FULL = {
  ts: 1000,
  heap: LIVE_HEAP,
  system: { available: true, ram_total_bytes: 16e9, ram_free_bytes: 4e9, swap_total_bytes: 8e9,
            swap_free_bytes: 6e9, process_cpu_load: 0.05, system_cpu_load: 0.42 },
  gc: { available: true, collection_count: 1676, collection_time_ms: 17949 },
  pagecache: { available: true, configured: "512.00MiB", hit_ratio_available: false },
  store: { available: true, reason: null, graph_bytes: 4437739, tx_log_bytes: 538968064, total_bytes: 543405803 },
};
const dashed = id => /^—( \/ —)?$/.test(els[id].textContent) && els[id].classList.contains("dim");
const allDashed = () => VALUE_IDS.every(dashed) && els.heapMeter.style.width === "0%" && els.ramMeter.style.width === "0%";
const labelledUnavailable = () =>
  /unavailable/i.test(els.memPill.textContent) && /unavailable/i.test(els.memNote.textContent);
const strokes = id => els[id].ops.filter(o => o[0] === "stroke").length;
const opNames = id => els[id].ops.map(o => o[0]).filter(n => n === "moveTo" || n === "lineTo");

(async () => {
  // 1. a full live snapshot reaches every row
  reset();
  serveStats(FULL);
  await api.attemptMemoryMetrics();
  check("asks the dedicated endpoint, never the Cypher console",
    fetchCalls.length === 1 && fetchCalls[0].url === "/api/database-stats", JSON.stringify(fetchCalls.map(c => c.url)));
  const want = {
    heapVal: "537MB / 2147MB", ramVal: "12.0GB / 16.0GB", swapVal: "2.0GB / 8.0GB", cpuVal: "5% / 42%",
    gcVal: "1676 / 17.9s", pageCacheVal: "512.00MiB", storeSizeVal: "543MB",
  };
  for (const [id, text] of Object.entries(want)) {
    check(`${id} shows ${text}`, els[id].textContent === text && !els[id].classList.contains("dim"),
      els[id].textContent + " | " + els[id].className);
  }
  check("heap meter at the reported percentage", els.heapMeter.style.width === "25%", els.heapMeter.style.width);
  check("RAM meter at used/total", els.ramMeter.style.width === "75%", els.ramMeter.style.width);
  check("store split names graph store and transaction logs",
    els.storeSplitVal.textContent === "graph 4MB · transaction logs 539MB", els.storeSplitVal.textContent);
  check("pill is live", els.memPill.textContent === "Live" && els.memPill.className === "wired-pill",
    els.memPill.textContent + "|" + els.memPill.className);
  check("the note says live and claims nothing is unavailable",
    /live/i.test(els.memNote.textContent) && !/unavailable/i.test(els.memNote.textContent), els.memNote.textContent);
  check("the note is honest that hit ratio has no source",
    /hit ratio/i.test(els.memNote.textContent), els.memNote.textContent);

  // 2. heap meter bounds
  for (const [percent, w] of [[0.4, "0.4%"], [100, "100%"], [140, "100%"], [-5, "0%"]]) {
    reset();
    serveStats({ ...FULL, heap: { ...LIVE_HEAP, used_percent: percent } });
    await api.attemptMemoryMetrics();
    check(`a reported ${percent}% heap renders as ${w}`, els.heapMeter.style.width === w, els.heapMeter.style.width);
  }

  // 3. bad heap readings dash heap only
  const badHeaps = [
    ["reported unavailable", { available: false, used_bytes: null, max_bytes: null, used_percent: null }],
    ["missing", undefined],
    ["strings", { available: true, used_bytes: "537000000", max_bytes: "2e9", used_percent: "25" }],
    ["null max", { ...LIVE_HEAP, max_bytes: null }],
    ["infinite max", { ...LIVE_HEAP, max_bytes: Infinity }],
    ["negative max", { ...LIVE_HEAP, max_bytes: -1 }],
    ["used above max", { available: true, used_bytes: 4000, max_bytes: 1000, used_percent: 400 }],
    ["zero", { available: true, used_bytes: 0, max_bytes: 0, used_percent: 0 }],
  ];
  for (const [label, heap] of badHeaps) {
    reset();
    serveStats({ ...FULL, heap });
    await api.attemptMemoryMetrics();
    check(`heap ${label}: heap dashed, meter empty`, dashed("heapVal") && els.heapMeter.style.width === "0%",
      els.heapVal.textContent + " | " + els.heapMeter.style.width);
    check(`heap ${label}: other rows stay live`, els.ramVal.textContent === "12.0GB / 16.0GB", els.ramVal.textContent);
    check(`heap ${label}: note names heap as unavailable`, /JVM heap[^.]*unavailable/i.test(els.memNote.textContent),
      els.memNote.textContent);
  }

  // 4. every other group fails on its own
  const badGroups = [
    ["system", { available: true, ram_total_bytes: 1000, ram_free_bytes: 4000 }, ["ramVal", "swapVal", "cpuVal"]],
    ["gc", { available: true, collection_count: "1676", collection_time_ms: 1 }, ["gcVal"]],
    ["pagecache", { available: true, configured: "" }, ["pageCacheVal"]],
    ["store", { available: true, reason: null, graph_bytes: 10, tx_log_bytes: 20, total_bytes: 999 }, ["storeSizeVal"]],
  ];
  for (const [group, value, ids] of badGroups) {
    reset();
    serveStats({ ...FULL, [group]: value });
    await api.attemptMemoryMetrics();
    check(`bad ${group}: its rows are dashed`, ids.every(dashed), ids.map(id => els[id].textContent).join(" | "));
    check(`bad ${group}: heap stays live`, els.heapVal.textContent === "537MB / 2147MB", els.heapVal.textContent);
    check(`bad ${group}: the note says unavailable`, /unavailable/i.test(els.memNote.textContent), els.memNote.textContent);
  }
  reset();
  serveStats({ ...FULL, store: { available: false, reason: "not_configured", graph_bytes: null, tx_log_bytes: null, total_bytes: null } });
  await api.attemptMemoryMetrics();
  check("an unconfigured store names the setting that fixes it",
    /DEVGRAPH_NEO4J_DATA_DIR/.test(els.memNote.textContent), els.memNote.textContent);
  check("store split is cleared when store is unavailable", els.storeSplitVal.textContent === "", els.storeSplitVal.textContent);
  reset();
  serveStats({ ...FULL, system: { ...FULL.system, swap_total_bytes: 0, swap_free_bytes: 0, process_cpu_load: null } });
  await api.attemptMemoryMetrics();
  check("no swap reads as 'no swap', not a dash", els.swapVal.textContent === "no swap", els.swapVal.textContent);
  check("a missing process CPU load is a dash beside a real system load", els.cpuVal.textContent === "— / 42%", els.cpuVal.textContent);

  // 5. the whole request failing dashes everything and says so
  const outright = [
    ["HTTP error", async () => ({ ok: false, status: 500, json: async () => ({ detail: "Neo4jError: boom" }) })],
    ["network failure", async () => { throw new Error("Failed to fetch"); }],
    ["non-JSON body", async () => ({ ok: true, status: 200, json: async () => { throw new Error("not json"); } })],
    ["null body", json(null)],
  ];
  for (const [label, handler] of outright) {
    reset();
    routes["/api/database-stats"] = handler;
    await api.attemptMemoryMetrics();
    check(`${label}: every value dashed`, allDashed(), VALUE_IDS.map(id => els[id].textContent).join(" | "));
    check(`${label}: labelled unavailable`, labelledUnavailable(), els.memPill.textContent + " | " + els.memNote.textContent);
    check(`${label}: no raw backend text`, !/Neo4jError/.test(els.memNote.textContent + els.memPill.title), els.memNote.textContent);
  }

  // 6. a later failure takes live values back down
  reset();
  serveStats(FULL);
  await api.attemptMemoryMetrics();
  routes["/api/database-stats"] = async () => { throw new Error("Failed to fetch"); };
  await api.attemptMemoryMetrics();
  check("a later failure re-dashes a live card", allDashed() && labelledUnavailable(),
    VALUE_IDS.map(id => els[id].textContent).join(" | "));

  // 7. source hygiene
  check("never writes the card through innerHTML", !/\.innerHTML\s*\+?=/.test(src), "innerHTML found");
  check("the browser never issues a JMX call itself", !/queryJmx/.test(html), "index.html contains dbms.queryJmx");
  check("the memory path never calls runCypher", !/runCypher/.test(src), "runCypher found in memory path");
  check("the card no longer ships a 'Not wired' pill", !/id="memPill"[^>]*>Not wired</.test(html), "Not wired pill found");

  // 8. sparklines
  reset();
  api.renderSpark("heapSpark", []);
  check("an empty history draws no line", strokes("heapSpark") === 0, JSON.stringify(els.heapSpark.ops));
  reset();
  api.renderSpark("heapSpark", [5]);
  check("a single sample draws no line", strokes("heapSpark") === 0, JSON.stringify(els.heapSpark.ops));
  reset();
  api.renderSpark("heapSpark", [1, 2, 3]);
  check("a continuous history is one path", strokes("heapSpark") === 1 &&
    JSON.stringify(opNames("heapSpark")) === JSON.stringify(["moveTo", "lineTo", "lineTo"]), JSON.stringify(opNames("heapSpark")));
  reset();
  api.renderSpark("heapSpark", [1, 2, null, 4, 5]);
  check("a gap breaks the line instead of bridging it",
    JSON.stringify(opNames("heapSpark")) === JSON.stringify(["moveTo", "lineTo", "moveTo", "lineTo"]), JSON.stringify(opNames("heapSpark")));
  reset();
  api.renderSpark("heapSpark", ["5", 6, 7]);
  check("non-numeric samples are skipped, not coerced",
    JSON.stringify(opNames("heapSpark")) === JSON.stringify(["moveTo", "lineTo"]), JSON.stringify(opNames("heapSpark")));

  reset();
  routes["/api/database-stats/history"] = json({ interval_s: 15, samples: [
    { ts: 1, heap_used_bytes: 10, ram_used_bytes: 20, process_cpu_load: 0.1, store_total_bytes: 30 },
    { ts: 2, heap_used_bytes: 11, ram_used_bytes: 21, process_cpu_load: 0.1, store_total_bytes: 31 },
  ] });
  await api.attemptMemoryHistory();
  check("history asks for the last hour", fetchCalls.some(c => c.url === "/api/database-stats/history?seconds=3600"),
    JSON.stringify(fetchCalls.map(c => c.url)));
  check("history draws all three sparklines",
    strokes("heapSpark") === 1 && strokes("ramSpark") === 1 && strokes("storeSpark") === 1,
    [strokes("heapSpark"), strokes("ramSpark"), strokes("storeSpark")].join(","));
  reset();
  routes["/api/database-stats/history"] = async () => { throw new Error("Failed to fetch"); };
  await api.attemptMemoryHistory();
  check("a failed history fetch leaves sparklines empty",
    strokes("heapSpark") === 0 && strokes("ramSpark") === 0 && strokes("storeSpark") === 0, "drew on failure");

  // 9. polling
  reset();
  serveStats(FULL);
  api.startMemoryPolling();
  api.startMemoryPolling();
  check("polling is scheduled once, every 15 s", intervals.length === 1 && intervals[0].ms === 15000,
    JSON.stringify(intervals.map(i => i.ms)));

  /* 10. boot path. The boot instance keeps its own memPollTimer across
     calls, so the reachable boot runs first: it is the one that must
     schedule polling. */
  reset();
  probe = async () => ({ results: [{ data: [{ row: [1] }] }] });
  serveStats(FULL);
  await boot.bootConnect();
  await flush();
  check("boot reads live stats once Neo4j is reachable",
    els.heapVal.textContent === "537MB / 2147MB" && els.memPill.textContent === "Live", els.heapVal.textContent);
  check("boot starts polling", intervals.length === 1 && intervals[0].ms === 15000, String(intervals.length));
  for (const [label, failure] of [
    ["the probe request fails outright", async () => { throw new Error("Failed to fetch"); }],
    ["the probe comes back with a Neo4j error", async () => ({ errors: [{ message: "ServiceUnavailable" }] })],
  ]) {
    reset();
    probe = failure;
    serveStats(FULL);   // the server offers live Neo4j values; the card must not trust them on this branch
    await boot.bootConnect();
    await flush();
    check(`boot with ${label}: Neo4j rows dashed`,
      ["heapVal", "ramVal", "swapVal", "cpuVal", "gcVal", "pageCacheVal"].every(dashed),
      VALUE_IDS.map(id => els[id].textContent).join(" | "));
    check(`boot with ${label}: store size still shown`, els.storeSizeVal.textContent === "543MB", els.storeSizeVal.textContent);
    check(`boot with ${label}: note says Neo4j is unreachable`,
      /unreachable/i.test(els.memNote.textContent) && !/ServiceUnavailable/.test(els.memNote.textContent + els.memPill.title),
      els.memNote.textContent);
    check(`boot with ${label}: pill is not stuck on Checking…`, els.memPill.textContent !== "Checking…", els.memPill.textContent);
  }

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
