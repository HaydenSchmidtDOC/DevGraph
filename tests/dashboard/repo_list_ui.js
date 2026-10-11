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
  grab(/^async function populateRealRepos\(/m, "\n}"),
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
    dispatchEvent: e => sel.events.push(e.type),
    themeRefresh: () => {},
  };
  return sel;
};
const els = {};
let registry = [];
const storage = new Map();
const globals = {
  document: { getElementById: id => els[id] || null },
  fetch: async url => {
    if (url !== "/api/repos") throw new Error("unexpected " + url);
    return { ok: true, json: async () => ({ repos: registry }) };
  },
  localStorage: { getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)) },
  SELECTED_REPO_KEY: "devgraph.selectedRepo",
  Event: class { constructor(type) { this.type = type; } },
  console,
};
const api = new Function(...Object.keys(globals), src +
  "\nreturn { populateRealRepos, registered: () => REGISTERED_REPO_IDS };")(...Object.values(globals));

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
  check("the switch is remembered", storage.get("devgraph.selectedRepo") === "alpha", storage.get("devgraph.selectedRepo"));

  // 5. The last repository is removed.
  registry = [];
  els.repoSelect.events.length = 0;
  await api.populateRealRepos();
  check("removing the last repository leaves only All Repos", JSON.stringify(values()) === '["__all__"]', JSON.stringify(values()));
  check("removing the last repository selects All Repos", els.repoSelect.value === "__all__", els.repoSelect.value);
  check("removing the last repository shows the call to action", els.repoEmptyState.hidden === false, String(els.repoEmptyState.hidden));
  check("removing the last repository reloads the page's views", els.repoSelect.events.includes("change"), JSON.stringify(els.repoSelect.events));
  check("All Repos now covers nothing", api.registered().length === 0, JSON.stringify(api.registered()));

  // 6. The call to action says how to register.
  const cta = grab(/<div id="repoEmptyState"/, "</div>");
  check("the call to action names the register command", /devgraph register &lt;path&gt;/.test(cta), cta);

  if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
  console.log("\nall checks passed");
})();
