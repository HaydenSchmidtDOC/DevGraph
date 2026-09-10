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
  const m = /"label": ele => \((.*?)\) \? ele\.data\("label"\) : "",/.exec(html);
  check("the label mapper is still where this test thinks it is", !!m, "regex found nothing");
  if (!m) return;
  const STRUCTURAL = ["repo", "service", "database", "vectorstore", "queue", "endpoint"];
  const label = new Function("STRUCTURAL_CATS", "ele",
    'return (' + m[1] + ') ? ele.data("label") : "";');
  const cats = new Set(STRUCTURAL);
  const el = (cat, excluded) => ({ data: k => (k === "cat" ? cat : "NAME"), hasClass: c => c === "excluded" && excluded });

  check("labels a matched structural node", label(cats, el("service", false)) === "NAME",
    label(cats, el("service", false)));
  check("drops the label on a structural node the query excluded",
    label(cats, el("service", true)) === "", label(cats, el("service", true)));
  check("still never labels a non-structural node by default",
    label(cats, el("function", false)) === "", label(cats, el("function", false)));
  check("drops the label on an excluded non-structural node too",
    label(cats, el("function", true)) === "", label(cats, el("function", true)));
  check("hover can still name anything (.show-label overrides the mapper)",
    /selector: "node\.show-label", style: \{ "label": "data\(label\)" \}/.test(html),
    "the .show-label rule is gone, so hovering an unlabelled node would name nothing");
  check("the search handler no longer force-labels every structural node",
    !/n\.addClass\("show-label"\); \}\);/.test(html),
    "a blanket addClass(show-label) is back and would defeat the mapper");
}

/* ---------------------------------------------------------------------
   Ambient rotation. Driven by hand here (requestAnimationFrame stubbed to a
   no-op, timestamps supplied) so the tick is just a function call.
   Two things worth pinning down: that it rotates node MODEL positions --
   rotating the rendered layer with a CSS transform is much cheaper but
   turns the labels drawn into that layer, which is the regression this
   replaced -- and that it does so at a bounded rate, which is what made the
   per-frame version expensive in the first place. */
function runRotationChecks() {
  const nodeState = [];
  const mkNode = (x, y) => {
    const s = { x, y };
    nodeState.push(s);
    return { position: p => (p === undefined ? { x: s.x, y: s.y } : Object.assign(s, p)) };
  };
  const rotSrc = [
    grab(/^const ROTATION_DEG_PER_SEC/m, "\n}"),   // constants + rotationTick
  ].join("\n");
  let styleWrites = 0;
  const container = { style: new Proxy({}, { set: () => (styleWrites++, true) }) };
  let pivot = { x: 100, y: 100 };
  const stub = {
    document: { hidden: false },
    requestAnimationFrame: () => {},
    performance,
    Math,
    get cy() { return cyStub; },
  };
  const cyStub = {
    nodes: () => { const a = liveNodes; a.forEach = Array.prototype.forEach.bind(liveNodes); return a; },
    batch: fn => fn(),
    container: () => container,
  };
  /* 540 units from the pivot stands in for the worst on-screen case:
     fitToCircle always frames the graph so the pivot's radius fits the
     viewport, so a node can never be further from the pivot on screen than
     half the viewport's smaller side -- ~540px on a 1080p display. Testing
     at that radius is testing the largest step any node can actually take. */
  let liveNodes = [mkNode(100 + 540, 100), mkNode(100, 100 + 540)];
  const ctx = {
    document: stub.document, requestAnimationFrame: stub.requestAnimationFrame,
    cy: cyStub, rotationPivot: pivot, userInteracting: false,
    lastRotationTime: 0, rotationLoopArmed: true,
  };
  const fn = new Function("document", "requestAnimationFrame", "cy",
    "rotationPivot", "userInteracting", "lastRotationTime", "rotationLoopArmed",
    "let __t = lastRotationTime;\n" +
    rotSrc.replace(/lastRotationTime = now;/, "__t = now; lastRotationTime = now;")
          .replace(/const elapsed = now - lastRotationTime;/, "const elapsed = now - __t;") +
    "\nreturn { rotationTick, ROTATION_TICK_MS, ROTATION_DEG_PER_SEC };");
  const rot = fn(ctx.document, ctx.requestAnimationFrame, ctx.cy, ctx.rotationPivot,
    ctx.userInteracting, ctx.lastRotationTime, ctx.rotationLoopArmed);

  const distTo = s => Math.hypot(s.x - pivot.x, s.y - pivot.y);
  const before = nodeState.map(distTo);
  const startX = nodeState[0].x, startY = nodeState[0].y;

  rot.rotationTick(10); // well under one tick interval
  check("does not rotate before a full tick interval has passed",
    nodeState[0].x === startX && nodeState[0].y === startY,
    JSON.stringify(nodeState[0]));

  rot.rotationTick(rot.ROTATION_TICK_MS + 10);
  const moved = nodeState[0].x !== startX || nodeState[0].y !== startY;
  check("rotates node model positions once a tick interval has passed",
    moved, JSON.stringify(nodeState[0]));
  check("rotates positions rather than CSS-transforming the rendered layer, which would lean the labels",
    styleWrites === 0, styleWrites + " style writes");

  const after = nodeState.map(distTo);
  check("rigid rotation preserves every node's distance from the pivot",
    after.every((d, i) => Math.abs(d - before[i]) < 1e-6),
    JSON.stringify({ before, after }));

  const stepPx = Math.hypot(nodeState[0].x - startX, nodeState[0].y - startY);
  check(`one tick moves the outermost on-screen node under a pixel (${stepPx.toFixed(3)}px at r=${before[0].toFixed(0)})`,
    stepPx < 1, stepPx + "px");
}
