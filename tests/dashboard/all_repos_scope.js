/* Headless test that the dashboard's "All Repos" scope is bounded to the
   registered repositories: the graph keeps every repo_id ever indexed (an
   unregistered repo, a stray test run), and "All Repos" must not surface them
   -- the same bound an MCP cross_repo call gets. Drives the real
   currentStateQuery out of index.html. */
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
  grab(/^function currentStateQuery\(/m, "\n}"),
].join("\n");

const els = {
  repoSelect: { value: "__all__" },
  ctlNodeCeiling: { max: 100 }, ctlRelCeiling: { max: 100 },
};
const globals = {
  document: { getElementById: id => els[id] || null },
  state: { isolatedEntity: null, isolatedRel: null, nodeCeiling: 100, relCeiling: 100 },
};
const api = new Function(...Object.keys(globals), src +
  "\nreturn { currentStateQuery, setIds: ids => { REGISTERED_REPO_IDS = ids; } };")(...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

api.setIds(["alpha", 'be"ta']);
let q = api.currentStateQuery();
check("All Repos matches only the registered repo ids",
  q.includes('WHERE n.repo_id IN ["alpha", "be\\"ta"]'), q);
check("All Repos is not an unbounded match", !/WHERE true\b/.test(q), q);

api.setIds([]);
q = api.currentStateQuery();
check("with nothing registered, All Repos matches nothing", q.includes("WHERE n.repo_id IN []"), q);

els.repoSelect.value = "alpha";
q = api.currentStateQuery();
check("a single repo is still scoped to itself", q.includes('WHERE n.repo_id = "alpha"'), q);

if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
console.log("\nall checks passed");
