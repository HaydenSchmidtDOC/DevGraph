/* Headless test that the canvas's default view is bounded: the node ceiling
   is seeded at CANVAS_NODE_CAP for a large scope, so the state query carries
   a LIMIT, the canvas says how much it shows, only releasing the slider at
   its max lifts the cap, and live refreshes on save are coalesced. Drives the
   real functions out of index.html. */
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
  grab(/^function escapeCypherStr\(/m, "\n"),
  grab(/^let REGISTERED_REPO_IDS = /m, "\n"),
  grab(/^function allReposClause\(/m, "\n}"),
  grab(/^const CANVAS_NODE_CAP = /m, "\n"),
  grab(/^let nodeCeilingUncapped = /m, "\n"),
  grab(/^let scopeNodeTotal = /m, "\n"),
  grab(/^function seededNodeCeiling\(/m, "\n}"),
  grab(/^function canvasCapNotice\(/m, "\n}"),
  grab(/^function currentStateQuery\(/m, "\n}"),
  grab(/^\/\* ── Live refresh ── \*\//m, "/* ── end Live refresh ── */"),
].join("\n");

const els = {
  repoSelect: { value: "big" },
  ctlNodeCeiling: { max: 52021 }, ctlRelCeiling: { max: 136000 },
};
const timers = [];
let refreshes = 0, release = null;
const globals = {
  document: { getElementById: id => els[id] || null },
  state: { isolatedEntity: null, isolatedRel: null, nodeCeiling: 0, relCeiling: 136000 },
  neo4jConnected: true,
  loadSchemaTypes: async () => {},
  refreshGraph: async () => { refreshes++; await new Promise(r => { release = r; }); },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
};
const api = new Function(...Object.keys(globals), src +
  "\nreturn { CANVAS_NODE_CAP, seededNodeCeiling, canvasCapNotice, currentStateQuery, scheduleLiveRefresh," +
  " liveRefreshDelay, setUncapped: v => { nodeCeilingUncapped = v; }, setTotal: v => { scopeNodeTotal = v; } };")(
  ...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

(async () => {
  // 1. the default ceiling of a large scope is the cap, and the query says LIMIT
  const seeded = api.seededNodeCeiling(52021);
  check("a large scope is seeded at the canvas cap", seeded === api.CANVAS_NODE_CAP, seeded);
  check("a small scope is seeded at its total", api.seededNodeCeiling(200) === 200, api.seededNodeCeiling(200));
  globals.state.nodeCeiling = seeded;
  let q = api.currentStateQuery();
  check("the default query of a large scope carries a node LIMIT", q.includes(`WITH n LIMIT ${api.CANVAS_NODE_CAP}`), q);
  globals.state.isolatedRel = "CALLS";
  q = api.currentStateQuery();
  check("...with a relationship type isolated too", q.includes(`WITH n LIMIT ${api.CANVAS_NODE_CAP}`), q);
  globals.state.isolatedRel = null;

  // 2. the canvas says how much of the scope it shows
  const note = api.canvasCapNotice(1500, 52021);
  check("a capped view names what it shows of the total", note.includes((1500).toLocaleString()) && note.includes((52021).toLocaleString()) && /narrow the view/.test(note), note);
  check("an uncapped view has no notice", api.canvasCapNotice(200, 200) === null, api.canvasCapNotice(200, 200));

  // 3. only releasing the slider at its max asks for everything
  api.setUncapped(true);
  check("an explicit max follows the total", api.seededNodeCeiling(52021) === 52021, api.seededNodeCeiling(52021));
  api.setUncapped(false);
  check("the slider release handler is what lifts the cap",
    /ctlNodeCeiling"\)\.addEventListener\("change", e => \{ nodeCeilingUncapped = \+e\.target\.value >= \+e\.target\.max;/.test(html),
    "change handler does not set nodeCeilingUncapped");

  // 4. live refreshes are coalesced: one per window, never two at once
  api.setTotal(52021);
  check("a large scope waits longer between live refreshes", api.liveRefreshDelay() > 10000, api.liveRefreshDelay());
  api.setTotal(200);
  check("a small scope refreshes soon", api.liveRefreshDelay() <= 2000, api.liveRefreshDelay());
  for (let i = 0; i < 5; i++) api.scheduleLiveRefresh();
  check("five saves in a window start one timer", timers.length === 1, timers.length);
  const running = timers.shift().fn();
  await new Promise(r => setImmediate(r));
  check("...which refreshes once", refreshes === 1, refreshes);
  api.scheduleLiveRefresh();
  timers.shift().fn();  // fires while the first refresh is still running
  await new Promise(r => setImmediate(r));
  check("a save during a refresh does not start a second one", refreshes === 1, refreshes);
  release();
  await running;
  check("...but queues one more after it", timers.length === 1, timers.length);
  timers.shift().fn();
  await new Promise(r => setImmediate(r));
  check("...which then runs", refreshes === 2, refreshes);
  release();

  if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
  console.log("\nall checks passed");
})();
