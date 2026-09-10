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

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
