/* Headless test of out-of-order Cypher replies on the graph path, driving the
   real runQueryAndUpdateGraph/previewQueryGlow out of index.html against
   stubs -- no browser, no cytoscape, no timers.

   The bug this exists to catch is invisible from the server side and looks
   like nothing at all in a screenshot: two overlapping requests (a repo
   switch on top of the initial load, two quick entity-type selects) both come
   back well-formed, but the EARLIER one can resolve LAST and then re-project
   its stale scope onto the canvas. In replace mode it also removes the newer
   request's elements and re-settles the layout to the old scope. Ordering
   here is controlled with explicit deferred promises rather than sleeps, so
   the "reply 1 arrives after reply 2" case is reproduced exactly, every run. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");

// Pull out just the functions under test plus the sequence counter they share.
const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
const src = [
  grab(/^function stableNodeId\(n\)/m, "\n}"),
  grab(/^function mapGraphResultToElements\(results\)/m, "\n}"),
  grab(/^function extractGraphIds\(results\)/m, "\n}"),
  grab(/^function extractStateMatchedIds\(json\)/m, "\n}"),
  grab(/^let glowRequestSeq = 0;/m, ";"),
  grab(/^async function runQueryAndUpdateGraph\(query, opts\)/m, "\n}"),
  grab(/^async function previewQueryGlow\(text\)/m, "\n}"),
].join("\n");

// --- fake canvas ------------------------------------------------------
/* Elements carry just what the code under test touches: id(), the excluded
   class toggling, and `length` (a real cytoscape element is a collection of
   one, which is what the focus filter tests). */
const canvas = new Map();
const mkEle = (id, isEdge) => {
  const classes = new Set();
  return {
    id: () => id, isEdge, length: 1,
    toggleClass: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
    hasClass: c => classes.has(c),
  };
};
const coll = arr => ({
  length: arr.length,
  forEach: fn => arr.forEach(fn),
  filter: fn => coll(arr.filter(fn)),
  map: fn => arr.map(fn),
});
const canvasIds = () => [...canvas.keys()].sort();

// --- stubs ------------------------------------------------------------
let cypherImpl = async () => ({ results: [], errors: [] });
const merges = [];
let settles = 0;
const glowCalls = [];
const focusCalls = [];
let visibleCountText = "";

/* Models the real mergeGraphElements closely enough for ordering: adds what
   is new, and in replace mode removes anything the incoming set does not
   contain and re-settles. Both halves matter -- a superseded reply that got
   this far would add its own stale nodes AND delete the newer request's. */
const mergeStub = (elements, mode, opts) => {
  merges.push({ ids: elements.map(el => el.data.id).sort(), mode, opts });
  const incoming = new Set(elements.map(el => el.data.id));
  if (opts && opts.remove) {
    for (const id of [...canvas.keys()]) if (!incoming.has(id)) canvas.delete(id);
  }
  for (const el of elements) if (!canvas.has(el.data.id)) canvas.set(el.data.id, mkEle(el.data.id, !!el.data.source));
  if (opts && opts.settle) settles++;
  return true;
};

const sandboxGlobals = {
  document: {
    getElementById: id => {
      if (id === "cypherInput") return { value: "" };
      if (id === "visibleCount") return { set textContent(v) { visibleCountText = v; }, get textContent() { return visibleCountText; } };
      return null;
    },
  },
  cy: {
    nodes: () => coll([...canvas.values()].filter(e => !e.isEdge)),
    edges: () => coll([...canvas.values()].filter(e => e.isEdge)),
    elements: () => coll([...canvas.values()]),
    collection: arr => coll(arr),
    getElementById: id => canvas.get(id) || { length: 0 },
  },
  syncCypherHighlight: () => {},
  runCypher: (statement, opts) => cypherImpl(statement, opts),
  mergeGraphElements: mergeStub,
  animateGlowTo: ids => glowCalls.push([...ids].sort()),
  focusRotation: (c, margin) => focusCalls.push({ length: c === null ? null : c.length, margin }),
  NODE_TYPES: [{ id: "Function", cat: "code" }, { id: "File", cat: "code" }],
  neo4jConnected: true,
  console,
};
const runner = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { runQueryAndUpdateGraph, previewQueryGlow, stableNodeId };");
const api = runner(...Object.values(sandboxGlobals));

// --- helpers ----------------------------------------------------------
const SEP = "\u001f";
const node = (internalId, name, file) => ({
  id: internalId, labels: ["Function"],
  key: ["Function", "devgraph", name, file].join(SEP),
  properties: { repo_id: "devgraph", name, file },
});
const nodeId = n => "live:" + n.key;
/* A currentStateQuery()-shaped reply: one row per node, `matchedId` naming
   the node that actually satisfied the filter (the rest are edge context). */
const stateJson = (nodes, matched) => ({
  results: [{
    columns: ["n", "r", "m", "matchedId"],
    data: nodes.map(n => ({
      row: [null, null, null, matched.includes(n.id) ? n.id : null],
      graph: { nodes: [n], relationships: [] },
    })),
  }],
  errors: [],
});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const reset = () => {
  canvas.clear(); merges.length = 0; settles = 0;
  glowCalls.length = 0; focusCalls.length = 0; visibleCountText = "";
};

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

// Fixtures: two disjoint scopes, as two different repo selections would be.
const oldA = node(11, "stale_one", "old.py");
const oldB = node(12, "stale_two", "old.py");
const newA = node(21, "fresh_one", "new.py");
const newB = node(22, "fresh_two", "new.py");

(async () => {
  /* 1. The core defect: request 1's reply lands after request 2's. Only the
        newest request may reach the canvas. Fails against the pre-fix
        ordering, where the stale merge ran before the supersession check. */
  reset();
  const d1 = deferred(), d2 = deferred();
  cypherImpl = q => (q === "OLD" ? d1.promise : d2.promise);
  const p1 = api.runQueryAndUpdateGraph("OLD", { replace: true });
  const p2 = api.runQueryAndUpdateGraph("NEW", { replace: true });
  d2.resolve(stateJson([newA, newB], [newA.id, newB.id]));
  const r2 = await p2;
  const canvasAfterNew = canvasIds();
  d1.resolve(stateJson([oldA, oldB], [oldA.id, oldB.id]));
  const r1 = await p1;
  check("out-of-order replies leave only the newest request's elements on the canvas",
    JSON.stringify(canvasIds()) === JSON.stringify([nodeId(newA), nodeId(newB)].sort()),
    JSON.stringify(canvasIds()));
  check("a superseded reply contributes no node to the canvas",
    !canvasIds().includes(nodeId(oldA)) && !canvasIds().includes(nodeId(oldB)),
    JSON.stringify(canvasIds()));
  check("only the newest request merges at all",
    merges.length === 1 && JSON.stringify(merges[0].ids) === JSON.stringify([nodeId(newA), nodeId(newB)].sort()),
    JSON.stringify(merges.map(m => m.ids)));
  check("the superseded call still reports the superseded contract its callers expect",
    r1.ok === true && r1.superseded === true && !!r1.json, JSON.stringify(r1 && { ok: r1.ok, superseded: r1.superseded }));
  check("the winning call reports a plain ok result",
    r2.ok === true && r2.superseded === undefined, JSON.stringify({ ok: r2.ok, superseded: r2.superseded }));
  check("the canvas the newest request built is untouched by the late reply",
    JSON.stringify(canvasAfterNew) === JSON.stringify(canvasIds()),
    JSON.stringify({ before: canvasAfterNew, after: canvasIds() }));

  /* 2. Replace mode is the destructive case: a stale reply must not delete
        the newer request's elements, and must not re-settle to the old scope. */
  reset();
  const d3 = deferred(), d4 = deferred();
  cypherImpl = q => (q === "OLD" ? d3.promise : d4.promise);
  const p3 = api.runQueryAndUpdateGraph("OLD", { replace: true, mode: "full" });
  const p4 = api.runQueryAndUpdateGraph("NEW", { replace: true, mode: "full" });
  d4.resolve(stateJson([newA, newB], [newA.id, newB.id]));
  await p4;
  const settlesAfterNew = settles;
  d3.resolve(stateJson([oldA, oldB], [oldA.id, oldB.id]));
  await p3;
  check("a superseded replace removes nothing belonging to the newer request",
    [nodeId(newA), nodeId(newB)].every(id => canvas.has(id)), JSON.stringify(canvasIds()));
  check("a superseded replace triggers no re-settle to the stale scope",
    settles === settlesAfterNew && settlesAfterNew === 1, JSON.stringify({ settles, settlesAfterNew }));

  /* 3. Additive (highlight) mode: no merge at all, so not one stale element
        is added on top of what is already shown. */
  reset();
  const d5 = deferred(), d6 = deferred();
  cypherImpl = q => (q === "OLD" ? d5.promise : d6.promise);
  const p5 = api.runQueryAndUpdateGraph("OLD", { replace: false });
  const p6 = api.runQueryAndUpdateGraph("NEW", { replace: false });
  d6.resolve(stateJson([newA], [newA.id]));
  await p6;
  merges.length = 0;
  const glowsAfterNew = glowCalls.length;
  d5.resolve(stateJson([oldA, oldB], [oldA.id]));
  await p5;
  check("a superseded additive reply performs no merge at all", merges.length === 0,
    JSON.stringify(merges.map(m => m.ids)));
  check("a superseded additive reply adds no element of its own",
    JSON.stringify(canvasIds()) === JSON.stringify([nodeId(newA)]), JSON.stringify(canvasIds()));
  check("a superseded reply does not re-glow the stale match",
    glowCalls.length === glowsAfterNew, JSON.stringify(glowCalls));

  /* 4. The ordinary path must be untouched by the fix, end to end. */
  reset();
  cypherImpl = async () => stateJson([newA, newB], [newA.id]);
  const res = await api.runQueryAndUpdateGraph("SOLO", { replace: true, mode: "light", focusMargin: 90 });
  check("a non-superseded call merges exactly what the reply mapped to",
    merges.length === 1 && JSON.stringify(merges[0].ids) === JSON.stringify([nodeId(newA), nodeId(newB)].sort()),
    JSON.stringify(merges.map(m => m.ids)));
  check("a non-superseded call passes mode and replace flags through unchanged",
    merges[0] && merges[0].mode === "light" && merges[0].opts.remove === true && merges[0].opts.settle === true,
    JSON.stringify(merges[0]));
  check("unmatched elements are marked excluded, matched ones are not",
    canvas.get(nodeId(newB)).hasClass("excluded") && !canvas.get(nodeId(newA)).hasClass("excluded"),
    JSON.stringify(canvasIds()));
  check("the visible count reflects only non-excluded elements",
    visibleCountText === "1 nodes · 0 edges", visibleCountText);
  check("glow is applied to the matched set",
    glowCalls.length === 1 && JSON.stringify(glowCalls[0]) === JSON.stringify([nodeId(newA)]),
    JSON.stringify(glowCalls));
  check("focus is invoked on the matched collection with the caller's margin",
    focusCalls.length === 1 && focusCalls[0].length === 1 && focusCalls[0].margin === 90,
    JSON.stringify(focusCalls));
  check("a non-superseded call returns ok without the superseded flag",
    res.ok === true && res.superseded === undefined, JSON.stringify({ ok: res.ok, superseded: res.superseded }));

  /* 5. Non-results still short-circuit before any canvas work, as before. */
  reset();
  cypherImpl = async () => ({ results: [], errors: [{ code: "Neo.ClientError", message: "bad syntax" }] });
  const errRes = await api.runQueryAndUpdateGraph("BAD", { replace: true });
  check("an error reply returns not-ok and never merges",
    errRes.ok === false && merges.length === 0 && canvas.size === 0, JSON.stringify(merges));
  cypherImpl = async () => stateJson([], []);
  const emptyRes = await api.runQueryAndUpdateGraph("EMPTY", { replace: true });
  check("an empty reply returns not-ok and never merges",
    emptyRes.ok === false && merges.length === 0 && canvas.size === 0, JSON.stringify(merges));

  /* 6. previewQueryGlow already checked supersession in the right order --
        it must keep doing so. */
  reset();
  const d7 = deferred(), d8 = deferred();
  cypherImpl = q => (q === "OLD" ? d7.promise : d8.promise);
  canvas.set(nodeId(newA), mkEle(nodeId(newA), false));
  const g1 = api.previewQueryGlow("OLD");
  const g2 = api.previewQueryGlow("NEW");
  d8.resolve(stateJson([newA], [newA.id]));
  await g2;
  d7.resolve(stateJson([oldA, oldB], [oldA.id]));
  await g1;
  check("a superseded glow preview never re-applies its stale highlight",
    glowCalls.length === 1 && JSON.stringify(glowCalls[0]) === JSON.stringify([nodeId(newA)]),
    JSON.stringify(glowCalls));
  check("a superseded glow preview does not refocus the camera on the stale match",
    focusCalls.length === 1, JSON.stringify(focusCalls));

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
