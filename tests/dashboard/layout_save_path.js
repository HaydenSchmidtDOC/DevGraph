/* Headless test of the layout save/load path, lifted verbatim out of
   index.html. collectLayout/saveCachedLayout/layoutScopeId only touch
   cy.nodes(), n.id(), n.position(), document.getElementById and fetch, so a
   handful of stubs is enough to exercise them for real -- no browser, no
   cytoscape, deterministic, and re-runnable. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");

// Pull out just the functions under test plus their module-level state.
const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
const src = [
  "let cachedPositions = new Map();",
  "let cachedLayoutScope = null;",
  grab(/^function layoutScopeId\(\)/m, "}"),
  "let saveLayoutTimer = null;",
  grab(/^function collectLayout\(\)/m, "\n}"),
  grab(/^function saveCachedLayout\(immediate\)/m, "\n}"),
].join("\n");

// --- stubs ------------------------------------------------------------
let selectedRepo = "devgraph";
let nodes = [];
const calls = [];
const sandboxGlobals = {
  document: { getElementById: id => (id === "repoSelect" ? { value: selectedRepo } : null) },
  cy: { nodes: () => ({ forEach: fn => nodes.forEach(fn) }) },
  navigator: { sendBeacon: (url, blob) => { calls.push({ via: "beacon", url, size: blob.size }); return blob.size <= 64 * 1024; } },
  Blob: class { constructor(parts) { this.size = parts.join("").length; } },
  fetch: async (url, opts) => { calls.push({ via: "fetch", url, method: opts.method, body: opts.body, keepalive: !!opts.keepalive }); return { ok: true }; },
  setTimeout, clearTimeout, console,
};
const runner = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { saveCachedLayout, collectLayout, layoutScopeId };");
const api = runner(...Object.values(sandboxGlobals));

// --- helpers ----------------------------------------------------------
const mk = (id, x, y) => ({ id: () => id, position: () => ({ x, y }) });
const K = (name, file) => `live:Function\u001fdevgraph\u001f${name}\u001f${file}`;
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const settle = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // 1. shape of the collected payload
  nodes = [mk(K("a", "x.py"), 10.4, -3.6), mk(K("b", "y.py"), 0, 0), mk("live-anon:57", 5, 5)];
  const collected = api.collectLayout();
  check("rounds coordinates to whole pixels",
    JSON.stringify(collected[K("a", "x.py")]) === "[10,-4]", JSON.stringify(collected));
  check("excludes unkeyed (live-anon) nodes, which do not survive a reindex",
    !("live-anon:57" in collected), JSON.stringify(Object.keys(collected)));
  check("keeps a node sitting at the origin",
    JSON.stringify(collected[K("b", "y.py")]) === "[0,0]", JSON.stringify(collected));

  // 2. the debounced save actually issues a PUT
  calls.length = 0;
  api.saveCachedLayout();
  check("debounces rather than sending immediately", calls.length === 0, JSON.stringify(calls));
  await settle(1500);
  check("issues exactly one PUT after the debounce", calls.length === 1, JSON.stringify(calls.map(c => c.via)));
  check("PUTs to the selected repo's layout endpoint",
    calls[0] && calls[0].url === "/api/repos/devgraph/layout", calls[0] && calls[0].url);
  check("body round-trips as the positions map",
    calls[0] && JSON.stringify(JSON.parse(calls[0].body)) === JSON.stringify(collected),
    calls[0] && calls[0].body);
  check("normal save does NOT set keepalive (64KB cap would reject a real layout)",
    calls[0] && calls[0].keepalive === false, JSON.stringify(calls[0]));

  // 3. a burst of saves collapses to one request
  calls.length = 0;
  api.saveCachedLayout(); api.saveCachedLayout(); api.saveCachedLayout();
  await settle(1500);
  check("a burst of saves collapses into one PUT", calls.length === 1, JSON.stringify(calls.map(c => c.via)));

  // 4. small unload payload prefers sendBeacon
  calls.length = 0;
  api.saveCachedLayout(true);
  check("small unload payload goes via sendBeacon",
    calls.length === 1 && calls[0].via === "beacon", JSON.stringify(calls));

  // 5. large unload payload must fall back to fetch, not silently vanish
  nodes = [];
  for (let i = 0; i < 1500; i++) nodes.push(mk(K("fn" + i, "devgraph/indexer/some/long/path/extractor.py"), i, i));
  const bigBody = JSON.stringify(api.collectLayout());
  calls.length = 0;
  api.saveCachedLayout(true);
  check(`large unload payload (${Math.round(bigBody.length / 1024)}KB) falls back to a real fetch`,
    calls.some(c => c.via === "fetch"), JSON.stringify(calls.map(c => c.via + ":" + (c.size || ""))));

  // 6. repo scope follows the selector
  selectedRepo = "__all__";
  calls.length = 0;
  nodes = [mk(K("a", "x.py"), 1, 1)];
  api.saveCachedLayout();
  await settle(1500);
  check("All Repos view saves under its own reserved scope",
    calls[0] && calls[0].url === "/api/repos/__all__/layout", calls[0] && calls[0].url);

  // 7. nothing to save issues nothing
  nodes = [mk("live-anon:1", 0, 0)];
  calls.length = 0;
  api.saveCachedLayout();
  await settle(1500);
  check("saves nothing when no node is server-keyed", calls.length === 0, JSON.stringify(calls));

  runSyncChecks();
  runLabelChecks();
  runRotationChecks();
  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();

/* ---------------------------------------------------------------------
   Query-driven highlighting and the node inspector both consume the same
   identity key the layout cache does, and both silently degrade when they
   fall out of step with it: highlighting stops matching any node (leaving
   the query looking like it returned only relationships), and the
   inspector reports a live node as a static demo element. Neither throws,
   so both are invisible without a check like this. */
const hl = (() => {
  const src2 = [
    grab(/^function stableNodeId\(n\)/m, "\n}"),
    grab(/^function extractStateMatchedIds\(json\)/m, "\n}"),
    grab(/^function edgeDetailsQuery\(ele\)/m, "\n}"),
    grab(/^function nodeDetailsQuery\(ele\)/m, "\n}"),
    grab(/^function escapeCypherStr\(s\)/m, "}"),
  ].join("\n");
  return new Function(src2 +
    "\nreturn { stableNodeId, extractStateMatchedIds, edgeDetailsQuery, nodeDetailsQuery };")();
})();

const SEP = "\u001f";
const node = (internalId, label, repo, name, file) => ({
  id: internalId, labels: [label],
  key: [label, repo, name].concat(file === undefined ? [] : [file]).join(SEP),
  properties: { repo_id: repo, name, ...(file === undefined ? {} : { file }) },
});
const ele = data => ({ id: () => data.id, data: k => data[k] });

function runSyncChecks() {
  const a = node(11, "Function", "devgraph", "main", "a.py");
  const b = node(22, "Function", "devgraph", "main", "b.py");
  const json = { results: [{
    columns: ["n", "r", "m", "matchedId"],
    data: [
      { row: [null, null, null, 11], graph: { nodes: [a, b], relationships: [{ id: 7 }] } },
    ],
  }] };

  const m = hl.extractStateMatchedIds(json);
  check("highlighting resolves matchedId to the element id actually on the canvas",
    m.nodeIds.has(hl.stableNodeId(a)), [...m.nodeIds].join(","));
  check("highlighting does not mark an unmatched same-named node",
    !m.nodeIds.has(hl.stableNodeId(b)), [...m.nodeIds].join(","));
  check("highlighting still resolves edges", m.edgeIds.has("live-e:7"), [...m.edgeIds].join(","));

  // no matchedId column (free-form query) -> everything returned is matched
  const free = { results: [{ columns: ["n"], data: [{ row: [null], graph: { nodes: [a, b], relationships: [] } }] }] };
  const mf = hl.extractStateMatchedIds(free);
  check("a query without matchedId marks everything it returned",
    mf.nodeIds.has(hl.stableNodeId(a)) && mf.nodeIds.has(hl.stableNodeId(b)), [...mf.nodeIds].join(","));

  // inspector
  const qa = hl.nodeDetailsQuery(ele({ id: hl.stableNodeId(a), key: a.key }));
  check("inspector builds a node query from the identity key, not an internal id",
    qa && qa.includes("MATCH (n:Function)") && qa.includes('n.name = "main"'), qa);
  check("inspector pins the file so same-named nodes don't collide",
    qa && qa.includes('n.file = "a.py"'), qa);

  const svc = node(33, "Service", "devgraph", "api");
  const qs = hl.nodeDetailsQuery(ele({ id: hl.stableNodeId(svc), key: svc.key }));
  check("inspector requires a null file for a non-file-scoped node",
    qs && qs.includes("n.file IS NULL"), qs);

  check("inspector rejects a key whose label is not a plain identifier",
    hl.nodeDetailsQuery(ele({ id: "live:x", key: 'Foo) DETACH DELETE (n' + SEP + "r" + SEP + "n" })) === null,
    "expected null");
  check("inspector treats a demo element (no key) as unresolvable",
    hl.nodeDetailsQuery(ele({ id: "repo:devgraph" })) === null, "expected null");
  check("inspector still resolves an edge by its internal id",
    (hl.edgeDetailsQuery(ele({ id: "live-e:7" })) || "").includes("id(r) = 7"),
    hl.edgeDetailsQuery(ele({ id: "live-e:7" })));
}

/* ---------------------------------------------------------------------
   Node labels. Text dominates the cost of a canvas repaint and the ambient
   rotation repaints on a tick, so a label nothing is asking to read is pure
   cost. The style mapper is the only thing deciding that, and it silently
   degrades in both directions: too broad and every repaint pays for text
   nobody wants, too narrow and structural nodes go unnamed. */
function runLabelChecks() {
  check("no node carries a label by default",
    /"label": "",/.test(html),
    "the base node style labels something; every repaint would pay for that text");
  check("a permanent label mapper has not crept back in",
    !/"label": ele =>/.test(html),
    "the base label is computed again, so some nodes are labelled without being asked for");
  check("hover names a node (.show-label is what turns a label on)",
    /selector: "node\.show-label", style: \{ "label": "data\(label\)" \}/.test(html),
    "the .show-label rule is gone, so nothing could ever be named");
  check("hovering a node adds show-label",
    /cy\.on\("mouseover", "node", e => \{ e\.target\.addClass\("show-label hover-hl"\)/.test(html),
    "node hover no longer labels");
  check("hovering an entity type highlights without labelling the whole type",
    /toggleClass\("hover-hl", on\)/.test(html) && !/toggleClass\("hover-hl show-label"/.test(html),
    "highlightCat labels every node of a type, which is the mass of text the empty base label avoids");
  check("mouseout releases the label unless a search is holding the node",
    /if \(!e\.target\.hasClass\("highlighted"\)\) e\.target\.removeClass\("show-label"\);/.test(html),
    "a hovered node's name would either stick after the pointer leaves, or be dropped mid-search");
  check("the search handler does not force-label every structural node",
    !/n\.addClass\("show-label"\); \}\);/.test(html),
    "a blanket addClass(show-label) is back and would relabel the whole graph");
}

/* ---------------------------------------------------------------------
   Exercise the real rotation loop with a deterministic animation-frame queue.
   Frames update visible nodes; periodic full passes catch off-screen nodes up
   from their fixed base positions. Visibility changes stop and re-arm the loop. */
function runRotationChecks() {
  const pivot = { x: 100, y: 100 };
  const nodeState = [];
  const mkNode = (id, x, y) => {
    const pos = { x, y };
    nodeState.push(pos);
    return { id: () => id, position: p => p === undefined ? { ...pos } : Object.assign(pos, p) };
  };
  const liveNodes = [mkNode("visible", 640, 100), mkNode("offscreen", 100, 640)];
  const bases = new Map(liveNodes.map(n => [n.id(), n.position()]));
  let styleWrites = 0, cullPasses = 0, now = 0;
  const pending = [];
  const listeners = {};
  const context = {
    document: { hidden: false, addEventListener: (event, fn) => { listeners[event] = fn; } },
    requestAnimationFrame: fn => pending.push(fn),
    performance: { now: () => now },
    cy: {
      nodes: () => liveNodes,
      batch: fn => fn(),
      container: () => ({ style: new Proxy({}, { set: () => (styleWrites++, true) }) }),
    },
    state: { rotationEnabled: true },
    rotationPivot: pivot, rotationBasePositions: bases, rotationAngle: 0,
    userInteracting: false, graphSettling: false,
    lastRotationTime: 0, lastRotationCullTime: 0, rotationLoopArmed: false,
    cachedOnscreenNodes: [liveNodes[0]],
    updateViewportCulling: () => cullPasses++,
  };
  const rotSrc = [
    grab(/^const ROTATION_CULL_INTERVAL_MS/m, ";"),
    grab(/^const ROTATION_DEG_PER_SEC/m, "\n}"),
    grab(/^function armRotationLoop\(\)/m, "\n}"),
    grab(/^document.addEventListener\("visibilitychange"/m, "});"),
  ].join("\n");
  const vm = require("vm");
  vm.createContext(context);
  vm.runInContext(rotSrc, context);
  const frame = timestamp => {
    now = timestamp;
    const callback = pending.shift();
    if (!callback) throw new Error("expected one scheduled animation frame");
    callback(now);
  };
  const snapshot = () => JSON.stringify(nodeState);
  const expected = (index, elapsedMs) => {
    const base = bases.get(liveNodes[index].id());
    const angle = 0.3 * elapsedMs / 1000 * Math.PI / 180;
    return { x: pivot.x + (base.x - pivot.x) * Math.cos(angle) - (base.y - pivot.y) * Math.sin(angle),
      y: pivot.y + (base.x - pivot.x) * Math.sin(angle) + (base.y - pivot.y) * Math.cos(angle) };
  };
  const matches = (index, elapsedMs) => {
    const want = expected(index, elapsedMs), got = nodeState[index];
    return Math.hypot(got.x - want.x, got.y - want.y) < 1e-8;
  };

  context.armRotationLoop();
  context.armRotationLoop();
  check("repeated arming schedules only one rotation loop", pending.length === 1, pending.length);
  frame(10);
  check("visible nodes rotate on the first animation frame", matches(0, 10) && nodeState[0].y > 100, snapshot());
  check("off-screen nodes wait for a full culling pass", nodeState[1].x === 100 && cullPasses === 0, snapshot());
  frame(26);
  check("successive visible frames use the fixed base without compounding rotation", matches(0, 26), snapshot());
  check("normal frames move the outermost visible node under one pixel",
    Math.hypot(nodeState[0].x - 640, nodeState[0].y - 100) < 1, snapshot());
  const interval = vm.runInContext("ROTATION_CULL_INTERVAL_MS", context);
  frame(interval);
  check("periodic full pass catches all nodes up to the same absolute angle",
    matches(0, interval) && matches(1, interval) && cullPasses === 1, snapshot());
  check("rigid rotation preserves every node's distance from the pivot",
    nodeState.every(p => Math.abs(Math.hypot(p.x - pivot.x, p.y - pivot.y) - 540) < 1e-8), snapshot());
  check("rotation never CSS-transforms the rendered labels", styleWrites === 0, styleWrites);

  const beforeHidden = snapshot();
  context.document.hidden = true;
  listeners.visibilitychange();
  frame(interval + 16);
  check("hidden tab stops rotation and leaves no animation callback pending",
    snapshot() === beforeHidden && !context.rotationLoopArmed && pending.length === 0, snapshot());
  now = 60000;
  context.document.hidden = false;
  listeners.visibilitychange();
  listeners.visibilitychange();
  check("visibility resume arms exactly one callback and resets elapsed time",
    pending.length === 1 && context.lastRotationTime === now, pending.length);
  frame(now + 16);
  check("resume excludes time spent hidden from rotation",
    matches(0, interval + 16) && matches(1, interval + 16), snapshot());

  for (const flag of ["userInteracting", "graphSettling"]) {
    const beforePause = snapshot();
    context[flag] = true;
    frame(now + 16);
    check(`${flag} pauses position updates while retaining one loop`,
      snapshot() === beforePause && pending.length === 1, snapshot());
    context[flag] = false;
  }
  const beforeDisabled = snapshot();
  context.state.rotationEnabled = false;
  frame(now + 16);
  check("disabled rotation stops scheduling and preserves positions",
    snapshot() === beforeDisabled && pending.length === 0 && !context.rotationLoopArmed, snapshot());
  context.armRotationLoop();
  check("disabled rotation cannot re-arm", pending.length === 0, pending.length);
}
