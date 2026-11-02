// Pulls a registered repo's real graph from the running dashboard and settles a
// layout for it offline, writing data/<name>.json for bench.html.
//   node export.js <name> <repo_id|__all__> [replicate]
// `replicate` tiles the graph k x k times (disjoint copies) to stand in for a
// repository bigger than any registered here.
const fs = require("fs"), path = require("path");
const d3 = require("d3-force");
const [name, repo, rep] = [process.argv[2], process.argv[3], +(process.argv[4] || 1)];
const BASE = process.env.DEVGRAPH_URL || "http://127.0.0.1:8765";
const CAT = { Repository: "repo", Container: "repo", Service: "service", Module: "module", Class: "class", Function: "function",
  Endpoint: "endpoint", Database: "database", VectorStore: "vectorstore", Queue: "queue", Cache: "cache" };

async function cypher(query) {
  const r = await fetch(BASE + "/api/cypher", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ query }) });
  const j = await r.json();
  if (j.errors?.length) throw new Error(JSON.stringify(j.errors));
  return j.results[0];
}
(async () => {
  const where = repo === "__all__" ? "true" : `n.repo_id = "${repo}"`;
  const nodes = new Map(), edges = [];
  const nres = await cypher(`MATCH (n) WHERE ${where} RETURN n`);
  for (const row of nres.data) for (const n of row.graph.nodes) nodes.set(n.id, { id: n.id, cat: CAT[n.labels[0]] || "phase2", c: n.properties.insight_community ?? -1 });
  const eres = await cypher(`MATCH (n)-[r]->(m) WHERE ${where} RETURN id(n) AS a, id(m) AS b`);
  for (const { row: [a, b] } of eres.data) if (nodes.has(a) && nodes.has(b)) edges.push([a, b]);
  console.log(name, "nodes", nodes.size, "edges", edges.length);

  const ns = [...nodes.values()];
  const idx = new Map(ns.map((n, i) => [n.id, i]));
  const links = edges.map(([s, t]) => ({ source: idx.get(s), target: idx.get(t) }));
  const sim = d3.forceSimulation(ns).alpha(1).alphaDecay(0.03).stop()
    .force("link", d3.forceLink(links).distance(30).strength(0.5))
    .force("charge", d3.forceManyBody().strength(-25).theta(0.9).distanceMax(400))
    .force("center", d3.forceCenter(0, 0)).force("x", d3.forceX().strength(0.03)).force("y", d3.forceY().strength(0.03));
  const t0 = Date.now();
  for (let i = 0; i < 200; i++) sim.tick();
  console.log("layout ms", Date.now() - t0);

  let out = { name, repo, nodes: ns.map(n => [Math.round(n.x * 10) / 10, Math.round(n.y * 10) / 10, n.cat, n.c]), edges: links.map(l => [l.source.index, l.target.index]) };
  if (rep > 1) {
    const xs = out.nodes.map(n => n[0]), ys = out.nodes.map(n => n[1]);
    const w = Math.max(...xs) - Math.min(...xs) + 200, h = Math.max(...ys) - Math.min(...ys) + 200;
    const N = out.nodes.length, nn = [], ee = [];
    for (let a = 0; a < rep; a++) for (let b = 0; b < rep; b++) {
      const off = nn.length;
      out.nodes.forEach(n => nn.push([n[0] + a * w, n[1] + b * h, n[2], n[3]]));
      out.edges.forEach(e => ee.push([e[0] + off, e[1] + off]));
    }
    out = { ...out, nodes: nn, edges: ee };
  }
  fs.mkdirSync(path.join(__dirname, "data"), { recursive: true });
  fs.writeFileSync(path.join(__dirname, "data", name + ".json"), JSON.stringify(out));
  console.log("wrote", name, out.nodes.length, "nodes", out.edges.length, "edges");
})();
