/* Headless test of the dashboard's schema-driven type lists, lifted verbatim
   out of index.html: the real type tables, the real row/chip builder, the
   real /schema loader, the real repo-change handler, and the real Cypher the
   isolate controls generate. Everything they touch is fetch and a handful of
   DOM calls, so small stubs exercise the real code -- no browser.

   What this guards: a repository's declared types (File, Folder, IS_CHILD_OF)
   show up with their own colour and can be isolated on their own; switching
   repos swaps that set without leaving the previous repo's rows behind; and a
   repo with no schema file looks exactly as it did when the lists were
   hardcoded. */
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
const tablesSrc = grab(/^const BUILTIN_NODE_TYPES = \[/m, "\n};");
const fnSrc = [
  grab(/^function catColor\(/m, "\n"),
  grab(/^function nodeType\(/m, "\n}"),
  grab(/^function renderTypeLists\(/m, "\n}"),
  grab(/^function applySchemaTypes\(/m, "\n}"),
  grab(/^async function loadSchemaTypes\(/m, "\n}"),
  grab(/^function refreshIsolateUI\(/m, "\n}"),
  grab(/^function escapeCypherStr\(/m, "\n"),
  grab(/^function escapeHtmlVal\(/m, "\n"),
  grab(/^function stableNodeId\(/m, "\n}"),
  grab(/^function mapGraphResultToElements\(/m, "\n}"),
  grab(/^function currentStateQuery\(/m, "\n}"),
  grab(/^function acCandidates\(/m, "\n}"),
  grab(/^async function onRepoSelectChange\(/m, "\n}"),
].join("\n");
const bootSrc = grab(/^async function bootConnect\(\)/m, "\n}");

/* Today's hardcoded lists, frozen here as the snapshot a repo with no schema
   file must still reproduce: same labels in the same order, same categories,
   same colours, same 18 relationship types. */
const SNAPSHOT_NODES = [
  ["Repository", "repo"], ["Service", "service"], ["Module", "module"], ["Class", "class"],
  ["Function", "function"], ["Endpoint", "endpoint"], ["Database", "database"],
  ["VectorStore", "vectorstore"], ["Queue", "queue"], ["Requirement", "phase2"],
  ["DesignDecision", "phase2"], ["ArchitectureNote", "phase2"], ["Document", "phase2"],
  ["Commit", "phase3"], ["PullRequest", "phase3"], ["Issue", "phase3"],
];
const SNAPSHOT_COLORS = {
  repo: "#b7b3ff", service: "#7c8dff", module: "#22c3e6", class: "#22c55e", function: "#a3e635",
  endpoint: "#fbbf24", database: "#f0813c", vectorstore: "#dc2626", queue: "#e0559e",
  phase2: "#9299a6", phase3: "#6b7280",
};
const SNAPSHOT_RELS = ["CONTAINS", "CALLS", "IMPORTS", "USES", "RUNS", "WRITES_TO", "READS_FROM",
  "IMPLEMENTS", "DEPENDS_ON", "EXTENDS", "SATISFIES", "DOCUMENTED_BY", "DECIDED_BY", "SUPERSEDES",
  "MENTIONS", "MODIFIES", "RESOLVES", "REFERENCES"];

// What the backend actually serves for built-ins: graph/schema.py's order,
// which includes Container (a label the old hardcoded list never showed).
const BACKEND_LABELS = ["Repository", "Container", "Service", "Module", "Class", "Function", "Endpoint",
  "Database", "VectorStore", "Queue", "Requirement", "DesignDecision", "ArchitectureNote", "Document",
  "Commit", "PullRequest", "Issue"];
const builtins = () => BACKEND_LABELS.map((label, i) => ({ label, origin: "builtin", color: null, count: i }));
const builtinRels = () => SNAPSHOT_RELS.map(type => ({ type, origin: "builtin", color: null }));
const payload = (extraNodes, extraRels, state) => ({
  node_types: [...builtins(), ...extraNodes.map(([label, color, count]) => ({ label, origin: "project", color, count }))],
  relationship_types: [...builtinRels(), ...extraRels.map(([type, color]) => ({ type, origin: "project", color }))],
  schema_state: state,
  notices: state === "pending" ? ["alpha: schema file changed since it was applied; rescan to apply it"] : [],
});
const SCHEMAS = {
  alpha: payload([["File", "#ff8800", 12], ["Folder", "#0088ff", 3]], [["IS_CHILD_OF", "#123456"]], "applied"),
  beta: payload([["Ticket", "#abcdef", 5]], [], "applied"),
  plain: payload([], [], "absent"),
  waiting: payload([], [], "pending"),
};

// --- a very small DOM ----------------------------------------------------
const mkEl = tag => {
  const classes = new Set();
  const listeners = {};
  const el = {
    tagName: tag, children: [], dataset: {}, textContent: "", title: "",
    style: { display: "", _props: {}, setProperty(k, v) { this._props[k] = v; } },
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, on) => { if (on === undefined ? !classes.has(c) : on) classes.add(c); else classes.delete(c); },
    },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(" ").filter(Boolean).forEach(c => classes.add(c)); },
    appendChild(c) { el.children.push(c); c.parentNode = el; return c; },
    replaceChildren(...cs) { el.children = []; cs.forEach(c => el.appendChild(c)); },
    set innerHTML(v) { el._html = v; if (v === "") el.children = []; },
    get innerHTML() { return el._html || ""; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type) { (listeners[type] || []).forEach(fn => fn({ target: el })); },
    querySelector: () => el._dot || (el._dot = mkEl("span")),
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      return el.children.filter(c => c.classList.contains(cls));
    },
  };
  return el;
};
const els = {
  entityCounts: mkEl("div"), relTypes: mkEl("div"), schemaPendingHint: mkEl("div"),
  repoSelect: mkEl("select"),
  ctlNodeCeiling: Object.assign(mkEl("input"), { max: 100 }),
  ctlRelCeiling: Object.assign(mkEl("input"), { max: 100 }),
};
els.schemaPendingHint.style.display = "none";
const document = { getElementById: id => els[id] || null, createElement: mkEl };
let fetchCalls = [];
let refreshes = 0, isolations = 0, styleUpdates = 0;
const state = { isolatedEntity: null, isolatedRel: null, nodeCeiling: 100, relCeiling: 100 };
const globals = {
  document, state, console, brightCache: {},
  entityCounts: els.entityCounts, relTypes: els.relTypes,
  fetch: async url => {
    fetchCalls.push(url);
    const m = /^\/api\/repos\/([^/]+)\/schema$/.exec(url);
    const body = m && SCHEMAS[decodeURIComponent(m[1])];
    return body ? { ok: true, status: 200, json: async () => body }
                : { ok: false, status: 404, json: async () => ({ detail: "no such repo" }) };
  },
  localStorage: { setItem: () => {} },
  SELECTED_REPO_KEY: "k",
  neo4jConnected: true,
  refreshGraph: async () => { refreshes++; return true; },
  buildQueryFromState: () => {},
  loadGitHistory: () => {},
  highlightType: () => {}, highlightRel: () => {},
  isolateChanged: () => { isolations++; },
  cy: { style: () => ({ update: () => { styleUpdates++; } }) },
};
const api = new Function(...Object.keys(globals),
  tablesSrc + "\n" + fnSrc +
  "\nreturn { NODE_TYPES, REL_TYPES, CAT_COLORS, BUILTIN_NODE_TYPES, renderTypeLists, applySchemaTypes," +
  " loadSchemaTypes, currentStateQuery, mapGraphResultToElements, acCandidates, onRepoSelectChange, refreshIsolateUI };")(
  ...Object.values(globals));

// --- helpers ------------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const rows = () => els.entityCounts.children;
const rowLabels = () => rows().map(r => r.dataset.label);
const chips = () => els.relTypes.children;
const chipTypes = () => chips().map(c => c.dataset.rel);
const rowFor = label => rows().find(r => r.dataset.label === label);
const switchTo = async repo => { els.repoSelect.value = repo; await api.onRepoSelectChange({ target: els.repoSelect }); };
const hintShown = () => els.schemaPendingHint.style.display !== "none";

(async () => {
  // 0. before any schema arrives the page renders today's lists (demo mode)
  api.renderTypeLists();
  check("before any schema loads the rows are today's 16 labels",
    JSON.stringify(rowLabels()) === JSON.stringify(SNAPSHOT_NODES.map(n => n[0])), JSON.stringify(rowLabels()));
  check("...and today's 18 relationship chips",
    JSON.stringify(chipTypes()) === JSON.stringify(SNAPSHOT_RELS), JSON.stringify(chipTypes()));

  // 1. boot: the schema for the selected repo is fetched and rendered
  await api.loadSchemaTypes("alpha");
  check("fetches the selected repo's schema route",
    fetchCalls.includes("/api/repos/alpha/schema"), JSON.stringify(fetchCalls));
  const expectLabels = [...SNAPSHOT_NODES.map(n => n[0]), "File", "Folder"];
  check("built-in rows match today's constants, user rows follow",
    JSON.stringify(rowLabels()) === JSON.stringify(expectLabels), JSON.stringify(rowLabels()));
  check("built-in rows keep today's categories",
    SNAPSHOT_NODES.every(([label, cat]) => rowFor(label)?.dataset.cat === cat),
    JSON.stringify(rows().map(r => [r.dataset.label, r.dataset.cat])));
  check("built-in rows keep today's colours",
    SNAPSHOT_NODES.every(([label, cat]) => rowFor(label)?.style._props["--dotcolor"] === SNAPSHOT_COLORS[cat] &&
      rowFor(label)._dot?.style.background === SNAPSHOT_COLORS[cat]),
    JSON.stringify(rows().map(r => [r.dataset.label, r.style._props["--dotcolor"]])));
  check("the built-in colour table is unchanged",
    Object.entries(SNAPSHOT_COLORS).every(([cat, hex]) => api.CAT_COLORS[cat] === hex), JSON.stringify(api.CAT_COLORS));
  check("relationship chips are today's 18 plus the repo's own",
    JSON.stringify(chipTypes()) === JSON.stringify([...SNAPSHOT_RELS, "IS_CHILD_OF"]), JSON.stringify(chipTypes()));

  // 2. user labels: their own category and the payload's colour
  check("a user label gets its own category, keyed by label",
    rowFor("File")?.dataset.cat === "user:File" && rowFor("Folder")?.dataset.cat === "user:Folder",
    JSON.stringify([rowFor("File")?.dataset.cat, rowFor("Folder")?.dataset.cat]));
  check("a user label's dot uses the payload colour",
    rowFor("File").style._props["--dotcolor"] === "#ff8800" && rowFor("File")._dot.style.background === "#ff8800" &&
    rowFor("Folder").style._props["--dotcolor"] === "#0088ff",
    JSON.stringify([rowFor("File").style._props, rowFor("Folder").style._props]));
  check("the graph's colour lookup knows the user category",
    api.CAT_COLORS["user:File"] === "#ff8800", JSON.stringify(api.CAT_COLORS));
  check("a user row shows the payload count", /12/.test(rowFor("File").innerHTML), rowFor("File").innerHTML);
  check("a user relationship chip carries the payload colour",
    chips().find(c => c.dataset.rel === "IS_CHILD_OF").style.borderColor === "#123456",
    JSON.stringify(chips().find(c => c.dataset.rel === "IS_CHILD_OF").style));
  check("the canvas is restyled so already-drawn nodes pick up the colours", styleUpdates > 0, String(styleUpdates));
  check("autocomplete offers the repo's labels and relationship types",
    api.acCandidates().some(c => c.text === "File" && c.kind === "label") &&
    api.acCandidates().some(c => c.text === "IS_CHILD_OF" && c.kind === "rel"),
    JSON.stringify(api.acCandidates().slice(0, 25)));
  check("a graph node with a user label maps onto that label's category",
    (() => {
      const [el] = api.mapGraphResultToElements([{ graph: { nodes: [{ id: 1, key: "k", labels: ["File"], properties: { name: "a.py" } }], relationships: [] } }]);
      return el.data.cat === "user:File" && el.data.ntype === "File";
    })(), "mapped File node lacks cat user:File / ntype File");
  check("the pending hint stays hidden for an applied schema", !hintShown(), els.schemaPendingHint.style.display);

  // 3. isolation is keyed by label
  rowFor("File").fire("click");
  check("clicking a user row isolates that label", state.isolatedEntity === "File", String(state.isolatedEntity));
  check("isolating File builds a labels(n)[0] = \"File\" query",
    api.currentStateQuery().includes('labels(n)[0] = "File"'), api.currentStateQuery());
  api.refreshIsolateUI();
  check("only the File row is marked isolated",
    rows().filter(r => r.classList.contains("isolated")).map(r => r.dataset.label).join() === "File",
    JSON.stringify(rows().filter(r => r.classList.contains("isolated")).map(r => r.dataset.label)));
  state.isolatedEntity = null;
  rowFor("Document").fire("click");
  check("a built-in sharing its category with others isolates itself, not its first sibling",
    state.isolatedEntity === "Document" && api.currentStateQuery().includes('labels(n)[0] = "Document"'),
    String(state.isolatedEntity) + " | " + api.currentStateQuery());
  state.isolatedEntity = 'Fi"le';
  check("the isolated label is escaped into the Cypher",
    api.currentStateQuery().includes('labels(n)[0] = "Fi\\"le"'), api.currentStateQuery());

  // 4. switching repos rebuilds rows and chips, nothing stale survives
  state.isolatedEntity = "File";
  state.isolatedRel = "IS_CHILD_OF";
  refreshes = 0;
  await switchTo("beta");
  check("the repo change handler fetches the new repo's schema",
    fetchCalls[fetchCalls.length - 1] === "/api/repos/beta/schema", JSON.stringify(fetchCalls));
  check("the repo change handler still refreshes the graph", refreshes === 1, String(refreshes));
  check("switching repos drops the previous repo's user rows",
    JSON.stringify(rowLabels()) === JSON.stringify([...SNAPSHOT_NODES.map(n => n[0]), "Ticket"]), JSON.stringify(rowLabels()));
  check("...and its relationship chips",
    JSON.stringify(chipTypes()) === JSON.stringify(SNAPSHOT_RELS), JSON.stringify(chipTypes()));
  check("...and its colours", !("user:File" in api.CAT_COLORS) && api.CAT_COLORS["user:Ticket"] === "#abcdef",
    JSON.stringify(api.CAT_COLORS));
  check("...and its autocomplete entries", !api.acCandidates().some(c => c.text === "File"), "File still offered");
  check("an isolation on a type the new repo lacks is cleared",
    state.isolatedEntity === null && state.isolatedRel === null,
    JSON.stringify([state.isolatedEntity, state.isolatedRel]));
  check("no duplicate rows after repeated loads",
    new Set(rowLabels()).size === rowLabels().length, JSON.stringify(rowLabels()));

  // 5. a repo with no schema file looks exactly like the hardcoded lists did
  await switchTo("plain");
  check("a no-file payload renders exactly today's 16 labels",
    JSON.stringify(rowLabels()) === JSON.stringify(SNAPSHOT_NODES.map(n => n[0])), JSON.stringify(rowLabels()));
  check("...with today's categories and colours",
    SNAPSHOT_NODES.every(([label, cat]) => rowFor(label).dataset.cat === cat &&
      rowFor(label).style._props["--dotcolor"] === SNAPSHOT_COLORS[cat]),
    JSON.stringify(rows().map(r => [r.dataset.label, r.dataset.cat, r.style._props["--dotcolor"]])));
  check("...and today's 18 relationship types",
    JSON.stringify(chipTypes()) === JSON.stringify(SNAPSHOT_RELS), JSON.stringify(chipTypes()));
  check("no user category is left in the colour table",
    !Object.keys(api.CAT_COLORS).some(k => k.startsWith("user:")), JSON.stringify(api.CAT_COLORS));

  // 6. a pending schema says so
  await switchTo("waiting");
  check("a pending schema shows the pending hint", hintShown(), els.schemaPendingHint.style.display);
  check("the hint says a rescan is needed", /id="schemaPendingHint"[^>]*>[^<]*rescan/i.test(html),
    "the served #schemaPendingHint does not mention a rescan");
  await switchTo("plain");
  check("the hint goes away again for a repo without a pending change", !hintShown(), els.schemaPendingHint.style.display);

  // 7. a failed schema read leaves the current lists alone
  const before = JSON.stringify(rowLabels());
  await api.loadSchemaTypes("nope");
  check("a failed schema read keeps the current rows", JSON.stringify(rowLabels()) === before, JSON.stringify(rowLabels()));

  // 8. wiring: boot loads the schema before the first graph fetch
  check("boot loads the selected repo's schema before the first graph fetch",
    /await loadSchemaTypes\(.*\);[\s\S]*await refreshGraph\(/.test(bootSrc), bootSrc);
  check("the repo dropdown is wired to the named handler",
    /getElementById\("repoSelect"\)\.addEventListener\("change", onRepoSelectChange\)/.test(html),
    "repoSelect change listener is not onRepoSelectChange");
  check("the pending hint sits in the entity card, hidden by default",
    /id="schemaPendingHint"[^>]*display:\s*none/.test(html), "no hidden #schemaPendingHint in index.html");

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
