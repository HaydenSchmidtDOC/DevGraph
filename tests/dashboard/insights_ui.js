/* Headless test of the graph-insights UI, lifted verbatim out of index.html:
   the Community card renderers, the leaderboard switch to PageRank, the
   community palette and the canvas mapping that carries a node's community.
   Only fetch and document.getElementById are reached, so stubs suffice. */
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
  grab(/^const COMMUNITY_PALETTE\b/m, "];"),
  grab(/^function communityColor\(/m, "\n}"),
  grab(/^function escapeHtmlVal\(/m, "}"),
  grab(/^function insightRowsHtml\(/m, "\n}"),
  grab(/^function formatScore\(/m, "\n}"),
  grab(/^let insightsRequest\b/m, ";"),
  grab(/^function clearInsights\(/m, "\n}"),
  grab(/^function renderInsights\(/m, "\n}"),
  grab(/^async function loadInsights\(/m, "\n}"),
  grab(/^async function recomputeInsights\(/m, "\n}"),
  grab(/^function mapGraphResultToElements\(/m, "\n}"),
  grab(/^function mergeGraphElements\(/m, "\n}"),
].join("\n");

const mkEl = initialClass => {
  const classes = new Set(initialClass ? initialClass.split(" ") : []);
  return {
    textContent: "", innerHTML: "", title: "", disabled: false, value: "",
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(" ").filter(Boolean).forEach(c => classes.add(c)); },
  };
};
let els = {};
let calls = [];
let routes = {};
const fetchStub = async (url, opts) => {
  calls.push({ url, method: opts?.method || "GET" });
  const handler = routes[(opts?.method || "GET") + " " + url];
  if (!handler) return { ok: false, status: 404, json: async () => ({}) };
  return handler();
};
const globals = {
  document: { getElementById: id => els[id] || null },
  fetch: fetchStub,
  NODE_TYPES: [{ id: "Function", cat: "code" }],
  stableNodeId: n => "s:" + n.id,
  state: { isolatedEntity: null },
  cy: new Proxy({}, { get: (_, k) => cyStub[k] }),
  console,
};
let cyStub = null;
const api = new Function(...Object.keys(globals), src +
  "\nreturn { communityColor, COMMUNITY_PALETTE, loadInsights, recomputeInsights, mapGraphResultToElements, renderInsights, mergeGraphElements };")(
  ...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const reset = repo => {
  els = {
    repoSelect: mkEl(""), communityPill: mkEl("sample-pill"), communityNote: mkEl("placeholder-note"),
    communityVal: mkEl("metric-val dim"), modularityVal: mkEl("metric-val dim"), insightsAtVal: mkEl("metric-val dim"),
    communityList: mkEl(""), bridgeList: mkEl(""), btnRecomputeInsights: mkEl("btn"),
    godNodes: mkEl(""), godNodesBasis: mkEl("lb-type"),
  };
  els.repoSelect.value = repo;
  els.godNodes.innerHTML = "DEGREE-LIST";
  els.godNodesBasis.textContent = "degree";
  calls = []; routes = {};
};
const json = (body, status = 200) => async () => ({ ok: status < 400, status, json: async () => body });
const COMPUTED = {
  computed: true, computed_at: "2026-10-01T12:00:00+00:00", node_count: 40, community_count: 3, modularity: 0.3571,
  communities: [{ community: 0, label: "<b>auth</b>", size: 12 }, { community: 1, label: "billing", size: 9 }],
  key_nodes: [{ name: "Hub", labels: ["Class"], file: "a.py", score: 0.123456, community: 0 }],
  bridges: [{ name: "Bridge", labels: ["Function"], file: "b.py", score: 0.6, community: 1 }],
};

(async () => {
  // 1. palette
  check("community 0 takes the first palette colour", api.communityColor(0) === api.COMMUNITY_PALETTE[0], api.communityColor(0));
  check("the palette cycles", api.communityColor(api.COMMUNITY_PALETTE.length + 1) === api.COMMUNITY_PALETTE[1], "");
  for (const bad of [null, undefined, -1, 1.5, "2"]) {
    check(`no community (${JSON.stringify(bad)}) is grey`, api.communityColor(bad) === "#5a5f68", api.communityColor(bad));
  }

  // 2. canvas mapping carries the community
  const els2 = api.mapGraphResultToElements([{ graph: { nodes: [
    { id: 1, labels: ["Function"], properties: { name: "f", insight_community: 3 } },
    { id: 2, labels: ["Function"], properties: { name: "g", insight_community: "3" } },
    { id: 3, labels: ["Function"], properties: { name: "h" } },
  ], relationships: [] } }]);
  const community = name => els2.find(e => e.data.label === name).data.community;
  check("an integer community is carried onto the node", community("f") === 3, JSON.stringify(els2));
  check("a non-integer community is dropped", community("g") === null, String(community("g")));
  check("a node without one has null", community("h") === null, String(community("h")));

  // 3. all-repos view asks nothing
  reset("__all__");
  await api.loadInsights();
  check("all-repos view makes no request", calls.length === 0, JSON.stringify(calls));
  check("all-repos view disables Recompute", els.btnRecomputeInsights.disabled === true, "");
  check("all-repos view says to pick a repository", /select a single repository/i.test(els.communityNote.textContent), els.communityNote.textContent);

  // 4. not computed
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ computed: false });
  await api.loadInsights();
  check("not computed: pill says so", els.communityPill.textContent === "Not computed", els.communityPill.textContent);
  check("not computed: values dashed", ["communityVal", "modularityVal", "insightsAtVal"].every(id => els[id].textContent === "—" && els[id].classList.contains("dim")), "");
  check("not computed: leaderboard left on degree", els.godNodes.innerHTML === "DEGREE-LIST" && els.godNodesBasis.textContent === "degree", els.godNodesBasis.textContent);

  // 5. computed
  reset("my repo");
  routes["GET /api/repos/my%20repo/insights"] = json(COMPUTED);
  await api.loadInsights();
  check("the repo id is URL-encoded", calls[0].url === "/api/repos/my%20repo/insights", calls[0].url);
  check("community count shown", els.communityVal.textContent === "3" && !els.communityVal.classList.contains("dim"), els.communityVal.textContent);
  check("modularity to two places", els.modularityVal.textContent === "0.36", els.modularityVal.textContent);
  check("last computed is filled", els.insightsAtVal.textContent !== "—" && els.insightsAtVal.textContent !== "", els.insightsAtVal.textContent);
  check("community labels are escaped", els.communityList.innerHTML.includes("&lt;b&gt;auth&lt;/b&gt;") && !els.communityList.innerHTML.includes("<b>auth"), els.communityList.innerHTML);
  check("key-node rows show the file basename and full path title", els.godNodes.innerHTML.includes("a.py") && els.godNodes.innerHTML.includes('title="a.py"'), els.godNodes.innerHTML);
  check("bridge rows show the file basename", els.bridgeList.innerHTML.includes("b.py"), els.bridgeList.innerHTML);
  check("bridges are listed with their score", els.bridgeList.innerHTML.includes("Bridge") && els.bridgeList.innerHTML.includes("0.600"), els.bridgeList.innerHTML);
  check("leaderboard switches to PageRank", els.godNodes.innerHTML.includes("Hub") && els.godNodesBasis.textContent === "PageRank", els.godNodesBasis.textContent);
  check("pill is live", els.communityPill.textContent === "Live" && els.communityPill.className === "wired-pill", els.communityPill.className);

  // 5b. file names are escaped; an isolated entity type keeps the degree list
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ ...COMPUTED, key_nodes: [{ name: "K", labels: ["Class"], file: "x/<img>.py", score: 0.5 }] });
  await api.loadInsights();
  check("file names are escaped", els.godNodes.innerHTML.includes("&lt;img&gt;.py") && !els.godNodes.innerHTML.includes("<img>"), els.godNodes.innerHTML);
  reset("demo");
  globals.state.isolatedEntity = "code";
  routes["GET /api/repos/demo/insights"] = json(COMPUTED);
  await api.loadInsights();
  globals.state.isolatedEntity = null;
  check("an isolated entity type keeps the degree list", els.godNodes.innerHTML === "DEGREE-LIST" && els.godNodesBasis.textContent === "degree", els.godNodesBasis.textContent);

  // 6. computed with no structure
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ ...COMPUTED, community_count: 0, communities: [], key_nodes: [], bridges: [] });
  await api.loadInsights();
  check("empty result says there is no structure", /no dependency structure/i.test(els.communityNote.textContent), els.communityNote.textContent);
  check("empty result keeps the degree leaderboard", els.godNodesBasis.textContent === "degree", els.godNodesBasis.textContent);

  // 7. failure
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ detail: "graph unavailable" }, 503);
  await api.loadInsights();
  check("a failed read is labelled unavailable", els.communityPill.textContent === "Unavailable" && /unavailable/i.test(els.communityNote.textContent), els.communityNote.textContent);

  // 8. a slower response for the previous repo never wins
  reset("first");
  let releaseFirst;
  routes["GET /api/repos/first/insights"] = () => new Promise(r => { releaseFirst = () => r({ ok: true, status: 200, json: async () => ({ ...COMPUTED, community_count: 99 }) }); });
  routes["GET /api/repos/second/insights"] = json({ ...COMPUTED, community_count: 2 });
  const slow = api.loadInsights();
  els.repoSelect.value = "second";
  await api.loadInsights();
  releaseFirst();
  await slow;
  check("the newer repo's result stands", els.communityVal.textContent === "2", els.communityVal.textContent);

  // 9. recompute
  reset("demo");
  routes["POST /api/repos/demo/insights"] = json({ detail: "busy" }, 409);
  await api.recomputeInsights();
  check("409 says a run is already going", /already/i.test(els.communityNote.textContent), els.communityNote.textContent);
  check("the button is re-enabled after 409", els.btnRecomputeInsights.disabled === false, "");
  reset("demo");
  routes["POST /api/repos/demo/insights"] = json(COMPUTED);
  await api.recomputeInsights();
  check("recompute uses POST", calls.some(c => c.method === "POST" && c.url === "/api/repos/demo/insights"), JSON.stringify(calls));
  check("recompute renders the fresh result", els.communityVal.textContent === "3", els.communityVal.textContent);
  reset("demo");
  routes["POST /api/repos/demo/insights"] = json({ detail: "graph unavailable" }, 503);
  await api.recomputeInsights();
  check("a failed recompute says so", /failed/i.test(els.communityNote.textContent) && els.btnRecomputeInsights.disabled === false, els.communityNote.textContent);

  // 9b. community colours on nodes already on the canvas stay current
  const mkNode = (id, community) => ({
    id: () => id, nonempty: () => true,
    data(k, v) { if (v !== undefined) this._d[k] = v; return this._d[k]; }, _d: { community },
  });
  const existing = mkNode("n1", null);
  const list = [existing];
  list.map = Array.prototype.map; list.filter = Array.prototype.filter;
  cyStub = { elements: () => list, collection: () => [], getElementById: id => (id === "n1" ? existing : { nonempty: () => false }) };
  api.mergeGraphElements([{ data: { id: "n1", community: 4 } }], "light", { remove: false, settle: false });
  check("mergeGraphElements updates community on an existing node", existing.data("community") === 4, String(existing.data("community")));

  // 10. the GDS placeholder is gone
  check("no GDS probe remains", !/gds\.list/.test(html) && !/attemptCommunityDetection/.test(html), "index.html still probes for GDS");

  // 11. the wiring sits in the right functions
  const liveBody = grab(/^function connectLiveEvents\(/m, "\n}");
  const topoBody = grab(/^async function loadTopologyCounts\(/m, "\n}");
  check("connectLiveEvents handles insights_refreshed", liveBody.includes("insights_refreshed"), "missing from connectLiveEvents");
  check("loadTopologyCounts does not reference an event", !topoBody.includes("insights_refreshed") && !/\bevent\./.test(topoBody), "event handling leaked into loadTopologyCounts");
  check("loadTopologyCounts resets the basis label to degree", topoBody.includes('godNodesBasis").textContent = "degree"'), "missing from loadTopologyCounts");
  check("insights_refreshed refreshes the graph when colouring by community", /insights_refreshed[\s\S]{0,200}state\.colorByCommunity[\s\S]{0,80}refreshGraph\(\)/.test(liveBody), "missing refreshGraph under colorByCommunity");
  check("loadTopologyCounts resets the basis in the empty branch too", /No nodes for this scope yet[\s\S]{0,120}godNodesBasis"\)\.textContent = "degree"/.test(topoBody), "empty branch does not reset the basis");
  check("connectLiveEvents does not touch the basis label", !liveBody.includes("godNodesBasis"), "misplaced in connectLiveEvents");

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
