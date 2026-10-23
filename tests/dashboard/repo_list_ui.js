/* Headless test of the dashboard's repository selector, driving the real
   populateRealRepos out of index.html against a stub <select>.

   On a fresh install, or once the last repository was removed, the selector
   still showed a hard-coded "devgraph" repository that does not exist:
   populateRealRepos returned early on an empty registry, leaving the static
   markup's option selected. The list is now always rebuilt from the
   registry, an empty registry shows a call to action, and a removed
   selection switches to a real choice. */
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
  grab(/^function escapeHtmlVal\(/m, "}"),
  grab(/^let REGISTERED_REPO_IDS = /m, "\n"),
  grab(/^function showNeo4jDown\(/m, "\n}"),
  grab(/^async function populateRealRepos\(/m, "\n}"),
  grab(/^async function onRepoSelectChange\(/m, "\n}"),
].join("\n");

// --- a <select> that behaves like the DOM's for what is used here ----------
const decode = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const mkSelect = () => {
  const sel = {
    options: [], _value: "", events: [],
    set innerHTML(v) {
      sel.options = [...v.matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g)].map(m => ({ value: decode(m[1]), text: decode(m[2]) }));
      sel._value = sel.options.length ? sel.options[0].value : "";
    },
    get value() { return sel._value; },
    set value(v) { sel._value = sel.options.some(o => o.value === v) ? v : ""; },
    dispatchEvent: e => { sel.events.push(e.type); e.target = sel; if (onChange) onChange(e); },
    themeRefresh: () => {},
  };
  return sel;
};
const els = {};
let onChange = null;  // the page's change listener, wired once api exists
let registry = [];
let graphUnavailable = null;
const storage = new Map();
const globals = {
  document: { getElementById: id => els[id] || null },
  fetch: async url => {
    if (url !== "/api/repos") throw new Error("unexpected " + url);
    return { ok: true, json: async () => ({ repos: registry, graph_unavailable: graphUnavailable }) };
  },
  localStorage: {
    getItem: k => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: k => storage.delete(k),
  },
  SELECTED_REPO_KEY: "devgraph.selectedRepo",
  Event: class { constructor(type) { this.type = type; } },
  applyCatchUpPill: () => {}, loadSchemaTypes: async () => {}, refreshGraph: async () => true,
  buildQueryFromState: () => {}, loadGitHistory: () => {}, neo4jConnected: true,
  console,
};
const api = new Function(...Object.keys(globals), src +
  "\nreturn { populateRealRepos, onRepoSelectChange, registered: () => REGISTERED_REPO_IDS };")(...Object.values(globals));

onChange = e => api.onRepoSelectChange(e);

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const repo = id => ({ repo_id: id, active: true, path_missing: false });
const fresh = () => {
  els.repoSelect = mkSelect();
  els.repoSelect.innerHTML = grab(/<select id="repoSelect"/, "</select>");
  els.repoEmptyState = { hidden: true };
  els.neo4jDownState = { hidden: true, textContent: "" };
  graphUnavailable = null;
  storage.clear();
};
const values = () => els.repoSelect.options.map(o => o.value);

(async () => {
  // 0. The static markup offers no repository of its own.
  fresh();
  check("the static selector holds only All Repos", JSON.stringify(values()) === '["__all__"]', JSON.stringify(values()));

  // 1. Fresh install: nothing registered.
  fresh();
  registry = [];
  await api.populateRealRepos(false);
  check("an empty registry lists only All Repos", JSON.stringify(values()) === '["__all__"]', JSON.stringify(values()));
  check("an empty registry selects All Repos", els.repoSelect.value === "__all__", els.repoSelect.value);
  check("an empty registry shows the call to action", els.repoEmptyState.hidden === false, String(els.repoEmptyState.hidden));

  // 2. Repositories registered: built from the registry, no CTA, no hard-coded preference.
  fresh();
  registry = [repo("devgraph"), repo("alpha")];
  await api.populateRealRepos(false);
  check("the list is the registry's", JSON.stringify(values()) === '["__all__","alpha","devgraph"]', JSON.stringify(values()));
  check("with no saved choice the first repository is selected", els.repoSelect.value === "alpha", els.repoSelect.value);
  check("the call to action is hidden once something is registered", els.repoEmptyState.hidden === true, String(els.repoEmptyState.hidden));
  check("All Repos covers exactly the registered ids", JSON.stringify(api.registered()) === '["alpha","devgraph"]', JSON.stringify(api.registered()));

  // 3. A saved choice survives a reload.
  fresh();
  storage.set("devgraph.selectedRepo", "devgraph");
  await api.populateRealRepos(false);
  check("a saved repository is restored", els.repoSelect.value === "devgraph", els.repoSelect.value);
  storage.set("devgraph.selectedRepo", "__all__");
  await api.populateRealRepos(false);
  check("a saved All Repos is restored", els.repoSelect.value === "__all__", els.repoSelect.value);

  // 4. The selected repository is removed while the page is open.
  fresh();
  registry = [repo("alpha"), repo("beta")];
  storage.set("devgraph.selectedRepo", "beta");
  await api.populateRealRepos(false);
  registry = [repo("alpha")];
  els.repoSelect.events.length = 0;
  await api.populateRealRepos();
  check("a removed selection switches to a remaining repository", els.repoSelect.value === "alpha", els.repoSelect.value);
  check("the switch reloads the page's views", els.repoSelect.events.includes("change"), JSON.stringify(els.repoSelect.events));
  check("the removed choice is forgotten, not replaced by the fallback",
    !storage.has("devgraph.selectedRepo"), storage.get("devgraph.selectedRepo"));

  // 5. The last repository is removed.
  registry = [];
  els.repoSelect.events.length = 0;
  await api.populateRealRepos();
  check("removing the last repository leaves only All Repos", JSON.stringify(values()) === '["__all__"]', JSON.stringify(values()));
  check("removing the last repository selects All Repos", els.repoSelect.value === "__all__", els.repoSelect.value);
  check("removing the last repository shows the call to action", els.repoEmptyState.hidden === false, String(els.repoEmptyState.hidden));
  check("removing the last repository reloads the page's views", els.repoSelect.events.includes("change"), JSON.stringify(els.repoSelect.events));
  check("All Repos now covers nothing", api.registered().length === 0, JSON.stringify(api.registered()));

  // 5b. Only a real choice is remembered: a fresh install, or removing the
  //     last repository, must not pin All Repos for the first repository
  //     registered afterwards.
  check("the empty fallback is not saved", !storage.has("devgraph.selectedRepo"), storage.get("devgraph.selectedRepo"));
  fresh();
  registry = [];
  await api.populateRealRepos(false);
  check("a fresh install saves no choice", !storage.has("devgraph.selectedRepo"), storage.get("devgraph.selectedRepo"));
  registry = [repo("first")];
  await api.populateRealRepos();
  check("the first repository registered is selected", els.repoSelect.value === "first", els.repoSelect.value);
  check("selecting it automatically saves nothing", !storage.has("devgraph.selectedRepo"), storage.get("devgraph.selectedRepo"));

  // 5c. A choice the user makes is saved, All Repos included.
  els.repoSelect.value = "__all__";
  await api.onRepoSelectChange({ type: "change", target: els.repoSelect });
  check("a user's All Repos choice is saved", storage.get("devgraph.selectedRepo") === "__all__", storage.get("devgraph.selectedRepo"));
  await api.populateRealRepos();
  check("and survives the next refresh", els.repoSelect.value === "__all__", els.repoSelect.value);

  // 6. The call to action says how to register.
  const cta = grab(/<div id="repoEmptyState"/, "</div>");
  check("the call to action names the register command", /devgraph register &lt;path&gt;/.test(cta), cta);

  // Neo4j down: the registry still lists every repository, and one banner says why.
  fresh();
  registry = [repo("alpha"), repo("beta")];
  graphUnavailable = "Neo4j isn't reachable at bolt://127.0.0.1:7699; start Neo4j";
  await api.populateRealRepos(false);
  check("with Neo4j down the registered repositories still list",
    JSON.stringify(values()) === '["__all__","alpha","beta"]', JSON.stringify(values()));
  check("...and the banner says Neo4j isn't reachable",
    els.neo4jDownState.hidden === false && els.neo4jDownState.textContent.startsWith("Neo4j isn't reachable"),
    JSON.stringify(els.neo4jDownState));
  graphUnavailable = null;
  await api.populateRealRepos(false);
  check("...and goes once Neo4j answers", els.neo4jDownState.hidden === true, JSON.stringify(els.neo4jDownState));

  if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
  console.log("\nall checks passed");
})();
