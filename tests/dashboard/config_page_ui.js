/* Headless test of the dashboard's Config page (Settings -> Config), lifted
   verbatim out of index.html: the real renderers, the real request builder,
   and the real editor flow (global warning step, destination dropdown,
   dry-run confirm, 412 reload). Everything they touch is fetch and a handful
   of DOM calls, so small stubs exercise the real code -- no browser.

   What this guards: the page lists Global first, then each project; built-ins
   carry a lock and no edit control; server strings (names, YAML, badge text,
   errors) only ever land as text; every write sends the fingerprint the user
   saw as If-Match and a JSON body; a changed file keeps the user's text and
   offers a reload; destructive schema changes and global edits need a second,
   informed click; a whole-file reset needs the scope's name typed exactly and
   confirms with the dry run's fingerprint; the project-config switch shows the
   server's state and asks only when disabling warns. */
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
const configSrc = grab(/^\/\* ── Config page/m, "/* ── end Config page ── */");

// --- a very small DOM ----------------------------------------------------
const allEls = [];
const mkEl = tag => {
  const classes = new Set();
  const listeners = {};
  const el = {
    tagName: tag.toUpperCase(), children: [], dataset: {}, style: { display: "" }, title: "", type: "",
    value: "", disabled: false, readOnly: false, parentNode: null, _text: "", _html: "",
    get textContent() { return el._text + el.children.map(c => c.textContent).join(""); },
    set textContent(v) { el._text = String(v); el.children = []; },
    get innerHTML() { return el._html; },
    set innerHTML(v) { el._html = String(v); el._text = ""; el.children = []; },
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, on) => { if (on === undefined ? !classes.has(c) : on) classes.add(c); else classes.delete(c); },
    },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(" ").filter(Boolean).forEach(c => classes.add(c)); },
    appendChild(c) { c.parentNode = el; el.children.push(c); return c; },
    replaceChildren(...cs) { el.children = []; el._text = ""; cs.forEach(c => el.appendChild(c)); },
    replaceWith(n) { const p = el.parentNode; p.children[p.children.indexOf(el)] = n; n.parentNode = p; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    async fire(type, detail = 1) {
      const ev = { target: el, detail, preventDefault() {} };
      for (const fn of listeners[type] || []) await fn(ev);
      if (el["on" + type]) await el["on" + type](ev);
    },
    focus() { focused = el; },
  };
  allEls.push(el);
  return el;
};
const ids = ["configScopes", "configStatus", "configModal", "configModalTitle", "configModalWarn", "configModalWarnText",
  "configDestField", "configDestLabel", "configDest", "configYaml", "configModalConfirm", "configModalError", "configModalReload",
  "configModalCancel", "configModalSave", "pane-config",
  "configResetModal", "configResetTitle", "configResetList", "configResetPhraseField", "configResetPhraseLabel", "configResetTyped",
  "configResetError", "configResetRecheck", "configResetCancel", "configResetConfirm"];
const tagFor = id => id === "configYaml" ? "textarea" : id === "configDest" ? "select" : id === "configResetTyped" ? "input" :
  /^configReset(Recheck|Cancel|Confirm)$/.test(id) ? "button" : "div";
const els = Object.fromEntries(ids.map(id => [id, mkEl(tagFor(id))]));
els["pane-config"].classList.add("active");
const document = { getElementById: id => els[id] || null, createElement: mkEl };

let tooltips = [];
let focused = null;
/* the page's clock (Date.now): a changed confirm button only arms after a pause */
let clock = 1e6;
/* when set, every fetch waits on it -- a request still in flight */
let gate = null;
/* when set, only a full GET /api/config waits on it (the refresh after a write) */
let reloadGate = null;
let fetchCalls = [];
let respond = () => ({ status: 500, body: { detail: "no handler" } });
/* what a full GET /api/config returns (the page refreshes after a tool write) */
let configPayload = null;
const globals = {
  document, console, Date: { now: () => clock },
  wireTooltip: el => tooltips.push(el),
  fetch: async (url, init) => {
    fetchCalls.push({ url, init: init || {} });
    /* the answer is what the server held when the request arrived, however late it lands */
    const { status, body } = url === "/api/config" && configPayload ? { status: 200, body: configPayload } : respond(url, init || {});
    if (gate) await gate;
    if (url === "/api/config" && reloadGate) await reloadGate;
    return { ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(body)) };
  },
};
const api = new Function(...Object.keys(globals),
  configSrc + "\nreturn { CONFIG_GLOBAL, renderConfigPage, renderConfigScope, configWriteRequest, describeConfigError," +
  " openConfigEditor, configEditTarget, configCopyDestinations, configCanCopy, loadConfigPage, applyConfigScope, configModalKey, CONFIG_SECTIONS," +
  " configResetRequest, configResetPhrase, configResetReady, describeConfigReset, configToggleRequest," +
  " get model() { return configModel; }, get edit() { return configEdit; }, get reset() { return configReset; } };")(...Object.values(globals));

// --- fixtures -----------------------------------------------------------
const HOSTILE = '<img src=x onerror=alert(1)>';
const globalBlock = () => ({
  node_types: [{ label: "Repository", locked: true }, { label: "Container", locked: true }],
  relationship_types: [{ type: "CALLS", locked: true }],
  tools: {
    file: "global-tools.json", state: "valid", error: null, fingerprint: "sha256:g1", badges: [],
    builtin: [{ name: "find_callers", tool_id: "find_callers", locked: true, description: "Who calls a function." }],
    entries: [
      { name: "hot_paths", tool_id: "gl_hot_paths", yaml: "name: hot_paths\ndescription: d\ncypher: x\n",
        badges: [{ level: "info", kind: "overridden", text: "Overridden in repo-a", detail: "These repositories' own tools win." }] },
      { name: HOSTILE, tool_id: "gl_" + HOSTILE, yaml: "name: '" + HOSTILE + "'\n", badges: [] },
    ],
  },
});
const project = (repo, extra) => ({
  repo_id: repo, display_path: "~/src/" + repo, project_config_enabled: true,
  effect_notes: { tools: "t", schema: "s" },
  schema: {
    file: "devgraph.schema.yaml", state: "pending", error: null, fingerprint: "sha256:" + repo + "-schema", extends: "default",
    badges: [{ level: "warn", kind: "schema-pending", text: "Schema change pending", detail: "rescan to apply it" }],
    node_types: [{ label: "Runbook", yaml: "label: Runbook\nkey: [slug]\n", editable: true, badges: [] }],
    relationships: [{ type: "DOCUMENTS", yaml: "type: DOCUMENTS\n", editable: false,
      badges: [{ level: "warn", kind: "ambiguous", text: "Declared more than once", detail: "Edit the file by hand." }] }],
  },
  tools: { file: "devgraph.tools.yaml", state: "valid", error: null, fingerprint: "sha256:" + repo + "-tools", badges: [],
    entries: extra || [] },
});
const MODEL = () => ({
  global: globalBlock(),
  projects: [
    project("repo-a", [{ name: "hot_paths", tool_id: "repo-a_hot_paths", yaml: "name: hot_paths\n", origin: "project (overrides global)",
      badges: [{ level: "info", kind: "overrides-global", text: "Overrides global tool", detail: "wins" }] }]),
    project("repo-b"),
  ],
});

// --- helpers ------------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const walk = (el, out = []) => { out.push(el); el.children.forEach(c => walk(c, out)); return out; };
const find = (root, pred) => walk(root).filter(pred);
const byClass = (root, cls) => find(root, e => e.classList.contains(cls));
const rowFor = (card, name) => byClass(card, "tool-row").find(r => byClass(r, "tool-name")[0]?.textContent === name);
const buttons = (root, label) => find(root, e => e.tagName === "BUTTON" && e.textContent === label);
const card = scope => els.configScopes.children.find(c => c.dataset.scope === scope);
const shown = el => el.style.display !== "none";
const lastCall = () => fetchCalls[fetchCalls.length - 1];
/* a deliberate click: well after the button last changed meaning */
const press = async (el, detail = 1) => { clock += 1000; await el.fire("click", detail); };
const yamlName = text => (/^name:\s*(\S+)/m.exec(text || "") || [])[1];
const writes = () => fetchCalls.filter(c => c.init.method && c.init.method !== "GET");
const body = call => JSON.parse(call.init.body);
const ifMatch = call => call.init.headers && call.init.headers["If-Match"];
const ok = scopeBlock => ({ status: 200, body: { ok: true, written: true, warnings: [], notes: ["Written to devgraph.tools.yaml; not committed."], scope: scopeBlock } });

(async () => {
  configPayload = MODEL();
  // 1. order and structure
  api.renderConfigPage(MODEL());
  check("renders Global first, then each project in payload order",
    JSON.stringify(els.configScopes.children.map(c => c.dataset.scope)) === JSON.stringify(["__global__", "repo-a", "repo-b"]),
    JSON.stringify(els.configScopes.children.map(c => c.dataset.scope)));
  const g = card("__global__");
  const locks = byClass(g, "cfg-lock");
  check("built-in node and relationship types carry a lock", ["Repository", "Container", "CALLS"].every(label =>
    find(g, e => e.classList.contains("cfg-chip") && e.textContent === label && byClass(e, "cfg-lock").length === 1).length === 1),
    JSON.stringify(byClass(g, "cfg-chip").map(c => c.textContent)));
  check("the lock says it is a locked built-in", locks.length > 0 && locks.every(l => l.title === "Built-in — locked"),
    JSON.stringify(locks.map(l => l.title)));
  const builtinRow = rowFor(g, "find_callers");
  check("a built-in tool is locked and has no edit or delete control",
    builtinRow && byClass(builtinRow, "cfg-lock").length === 1 && find(builtinRow, e => e.tagName === "BUTTON").length === 0,
    builtinRow && builtinRow.textContent);
  const hotRow = rowFor(g, "hot_paths");
  check("a global tool carries the GLOBAL flag", hotRow && byClass(hotRow, "cfg-global").map(e => e.textContent).join() === "GLOBAL",
    hotRow && hotRow.textContent);
  check("...its display-only tool id", hotRow && hotRow.textContent.includes("gl_hot_paths"), hotRow && hotRow.textContent);
  check("...and Edit / Delete controls", buttons(hotRow, "Edit").length === 1 && buttons(hotRow, "Delete").length === 1,
    hotRow.textContent);
  const badge = byClass(hotRow, "cfg-badge")[0];
  check("a badge shows the payload's text", badge && badge.textContent === "Overridden in repo-a", badge && badge.textContent);
  check("...with its level as a modifier class", badge && badge.classList.contains("info"), badge && badge.className);
  check("...and the detail as a hover tooltip", badge && badge.dataset.tip === "These repositories' own tools win." && tooltips.includes(badge),
    badge && JSON.stringify(badge.dataset));
  const a = card("repo-a");
  check("a project card names the repository and its display path",
    a.textContent.includes("repo-a") && a.textContent.includes("~/src/repo-a"), a.textContent);
  check("a project card shows the schema state badge",
    byClass(a, "cfg-badge").some(b => b.textContent === "Schema change pending" && b.classList.contains("warn")), a.textContent);
  check("project node types are editable", buttons(rowFor(a, "Runbook"), "Edit").length === 1, rowFor(a, "Runbook").textContent);
  check("a relationship declared more than once has no Edit control",
    buttons(rowFor(a, "DOCUMENTS"), "Edit").length === 0 && rowFor(a, "DOCUMENTS").textContent.includes("Declared more than once"),
    rowFor(a, "DOCUMENTS").textContent);
  check("a project tool overriding a global one offers 'Remove override'",
    buttons(rowFor(a, "hot_paths"), "Remove override").length === 1, rowFor(a, "hot_paths").textContent);

  // 2. hostile names are text, never markup
  const hostileRow = rowFor(g, HOSTILE);
  check("a hostile tool name is rendered as text", !!hostileRow && hostileRow.textContent.includes(HOSTILE), "no row with the hostile name as text");
  check("...and nothing server-supplied reaches innerHTML",
    allEls.every(e => !e._html.includes("<img") && !e._html.includes("onerror")),
    JSON.stringify(allEls.filter(e => e._html.includes("<img")).map(e => e._html)));

  // 3. the request builder
  let r = api.configWriteRequest("add", "__global__", "tools", null, "name: x\n", "sha256:g1", false);
  check("add tool -> POST /api/config/__global__/tools", r.url === "/api/config/__global__/tools" && r.init.method === "POST", JSON.stringify(r));
  check("...with If-Match quoting the fingerprint", r.init.headers["If-Match"] === '"sha256:g1"', JSON.stringify(r.init.headers));
  check("...a JSON content type", r.init.headers["Content-Type"] === "application/json", JSON.stringify(r.init.headers));
  check("...and a {yaml, dry_run} body", r.init.body === JSON.stringify({ yaml: "name: x\n", dry_run: false }), r.init.body);
  r = api.configWriteRequest("replace", "repo-a", "node_types", "Runbook", "label: Runbook\n", "absent", true);
  check("replace node type -> PUT /api/config/repo-a/schema/node_types/Runbook with dry_run",
    r.url === "/api/config/repo-a/schema/node_types/Runbook" && r.init.method === "PUT" && JSON.parse(r.init.body).dry_run === true,
    JSON.stringify(r));
  r = api.configWriteRequest("delete", "repo-a", "relationships", "A B", null, "sha256:x", true);
  check("delete -> DELETE with ?dry_run=1, no body, encoded name",
    r.url === "/api/config/repo-a/schema/relationships/A%20B?dry_run=1" && r.init.method === "DELETE" && r.init.body === undefined &&
    r.init.headers["If-Match"] === '"sha256:x"', JSON.stringify(r));
  r = api.configWriteRequest("delete", "repo-a", "tools", "t", null, "sha256:x", false);
  check("a real delete has no dry_run query", r.url === "/api/config/repo-a/tools/t", r.url);

  // 4. a plain edit: dry run, then the write; the modal closes and the block re-renders
  const newA = project("repo-a", [{ name: "hot_paths", tool_id: "repo-a_hot_paths", yaml: "name: hot_paths\n", origin: "project (overrides global)", badges: [] },
    { name: "fresh_tool", tool_id: "repo-a_fresh_tool", yaml: "name: fresh_tool\n", origin: "project", badges: [] }]);
  fetchCalls = [];
  respond = (url, init) => JSON.parse(init.body || "{}").dry_run ? { status: 200, body: { ok: true, written: false, warnings: [], notes: [], scope: newA } } : ok(newA);
  await buttons(rowFor(a, "Runbook"), "Edit")[0].fire("click");
  check("Edit opens the modal with the entry's YAML", els.configModal.classList.contains("open") && els.configYaml.value === "label: Runbook\nkey: [slug]\n",
    els.configYaml.value);
  check("a project entry skips the global warning step", !shown(els.configModalWarn) && shown(els.configYaml), els.configModalWarn.style.display);
  els.configYaml.value = "label: Runbook\nkey: [id]\n";
  await els.configYaml.fire("input");
  let releaseReload;
  reloadGate = new Promise(r => { releaseReload = r; });
  clock += 1000;
  let saving = els.configModalSave.fire("click");
  for (let i = 0; i < 20 && lastCall().url !== "/api/config"; i++) await new Promise(r => setTimeout(r, 0));
  check("...the scope block is re-rendered from the response", !!rowFor(card("repo-a"), "fresh_tool"), card("repo-a").textContent);
  check("...and then the whole page refreshes, after a schema write too (conflict badges cross repos)",
    lastCall().url === "/api/config", lastCall().url);
  reloadGate = null; releaseReload(); await saving;
  check("Save sends a dry run first, then the write",
    fetchCalls.length >= 2 && body(fetchCalls[0]).dry_run === true && body(fetchCalls[1]).dry_run === false, JSON.stringify(fetchCalls));
  check("...both PUT to the entry with the schema file's fingerprint",
    fetchCalls.slice(0, 2).every(c => c.url === "/api/config/repo-a/schema/node_types/Runbook" && c.init.method === "PUT" &&
      ifMatch(c) === '"sha256:repo-a-schema"' && body(c).yaml === "label: Runbook\nkey: [id]\n"), JSON.stringify(fetchCalls));
  check("on success the modal closes", !els.configModal.classList.contains("open"), els.configModal.className);
  check("...and the notes are shown as text", els.configStatus.textContent.includes("not committed"), els.configStatus.textContent);
  api.renderConfigPage(MODEL());

  // 5. 412: the user's text survives and Reload refreshes the fingerprint
  fetchCalls = [];
  const fresher = project("repo-b");
  fresher.tools.fingerprint = "sha256:repo-b-tools-2";
  respond = (url, init) => {
    if (init.method === undefined || init.method === "GET") return { status: 200, body: fresher };
    return ifMatch({ init }) === '"sha256:repo-b-tools"'
      ? { status: 412, body: { detail: { code: "stale", message: "devgraph.tools.yaml changed on disk", scope: fresher } } }
      : { status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: fresher } };
  };
  await buttons(card("repo-b"), "Add tool")[0].fire("click");
  check("Add pre-fills a tool skeleton filtering on $repo_id", /name:/.test(els.configYaml.value) && /\$repo_id/.test(els.configYaml.value),
    els.configYaml.value);
  els.configYaml.value = "name: mine\n# my unsaved work\n";
  await els.configYaml.fire("input");
  await press(els.configModalSave);
  check("a stale fingerprint keeps the modal open", els.configModal.classList.contains("open"), els.configModal.className);
  check("...keeps the user's text", els.configYaml.value === "name: mine\n# my unsaved work\n", els.configYaml.value);
  check("...says the file changed on disk", /changed on disk/i.test(els.configModalError.textContent) && shown(els.configModalError),
    els.configModalError.textContent);
  check("...and offers Reload", shown(els.configModalReload), els.configModalReload.style.display);
  check("...without writing", fetchCalls.every(c => body(c).dry_run === true), JSON.stringify(fetchCalls));
  await els.configModalReload.fire("click");
  check("Reload fetches the scope", lastCall().url === "/api/config/repo-b", lastCall().url);
  check("...keeps the textarea", els.configYaml.value === "name: mine\n# my unsaved work\n", els.configYaml.value);
  check("...and hides itself", !shown(els.configModalReload), els.configModalReload.style.display);
  fetchCalls = [];
  await press(els.configModalSave);
  check("the next save carries the reloaded fingerprint",
    writes().length === 2 && writes().every(c => ifMatch(c) === '"sha256:repo-b-tools-2"' && c.init.method === "POST" &&
      c.url === "/api/config/repo-b/tools"), JSON.stringify(fetchCalls));
  api.renderConfigPage(MODEL());

  // 6. dry-run warnings need a second, explicit confirm
  fetchCalls = [];
  const WARN = "Removing node type <b>Runbook</b> deletes its 4 nodes on the next rescan.";
  respond = (url, init) => url.includes("dry_run=1")
    ? { status: 200, body: { ok: true, written: false, warnings: [WARN], notes: [], scope: project("repo-a") } }
    : ok(project("repo-a"));
  await buttons(rowFor(card("repo-a"), "Runbook"), "Delete")[0].fire("click");
  await press(els.configModalSave);
  check("a delete with warnings stops after the dry run", fetchCalls.length === 1 && fetchCalls[0].url.endsWith("?dry_run=1"),
    JSON.stringify(fetchCalls));
  check("...shows the warnings as text", els.configModalConfirm.textContent.includes(WARN) && shown(els.configModalConfirm) &&
    !els.configModalConfirm._html.includes("<b>"), els.configModalConfirm.textContent);
  check("...and asks for a second click", els.configModalSave.textContent === "Delete anyway", els.configModalSave.textContent);
  check("...with the modal still open", els.configModal.classList.contains("open"), els.configModal.className);
  await press(els.configModalSave);
  check("the second click deletes for real", writes().length === 2 &&
    fetchCalls[1].url === "/api/config/repo-a/schema/node_types/Runbook" && fetchCalls[1].init.method === "DELETE" &&
    ifMatch(fetchCalls[1]) === '"sha256:repo-a-schema"', JSON.stringify(fetchCalls));
  api.renderConfigPage(MODEL());

  // ...and editing the text after seeing warnings asks again
  fetchCalls = [];
  respond = (url, init) => JSON.parse(init.body).dry_run
    ? { status: 200, body: { ok: true, written: false, warnings: ["Changing the key keeps the old constraint."], notes: [], scope: project("repo-a") } }
    : ok(project("repo-a"));
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  check("an edit with warnings asks for 'Save anyway'", els.configModalSave.textContent === "Save anyway" && fetchCalls.length === 1,
    els.configModalSave.textContent);
  els.configYaml.value = "label: Runbook\nkey: [other]\n";
  await els.configYaml.fire("input");
  check("changing the text withdraws the confirm", els.configModalSave.textContent === "Save" && !shown(els.configModalConfirm),
    els.configModalSave.textContent);
  await press(els.configModalSave);
  check("...so the next click dry-runs again", fetchCalls.length === 2 && body(fetchCalls[1]).dry_run === true, JSON.stringify(fetchCalls));
  els.configModalCancel.fire("click");
  check("Cancel closes the modal", !els.configModal.classList.contains("open"), els.configModal.className);

  // 7. a global edit starts with a warning step
  fetchCalls = [];
  await buttons(rowFor(card("__global__"), "hot_paths"), "Edit")[0].fire("click");
  check("editing a global tool opens on the warning step", shown(els.configModalWarn) && !shown(els.configYaml), els.configModalWarn.style.display);
  check("...saying it is served in every repository's MCP sessions, how many, and where it is overridden",
    els.configModalWarnText.textContent ===
      "Global tools are served in every registered repository's MCP sessions (2 repos). Overridden in: repo-a.",
    els.configModalWarnText.textContent);
  check("...with a Continue button and nothing sent", els.configModalSave.textContent === "Continue" && fetchCalls.length === 0,
    els.configModalSave.textContent);
  await press(els.configModalSave);
  check("Continue shows the editor", shown(els.configYaml) && els.configModalSave.textContent === "Save" && fetchCalls.length === 0,
    els.configModalSave.textContent);
  check("the destination dropdown lists the global store, then each repo",
    JSON.stringify(els.configDest.children.map(o => [o.value, o.textContent])) ===
      JSON.stringify([["__global__", "Global store"], ["repo-a", "repo-a"], ["repo-b", "repo-b"]]) && shown(els.configDestField),
    JSON.stringify(els.configDest.children.map(o => [o.value, o.textContent])));
  check("...defaulting to the global store", els.configDest.value === "__global__", els.configDest.value);

  // 8. destination switches POST <-> PUT by existence
  els.configDest.value = "repo-b";
  await els.configDest.fire("change");
  check("choosing a repo rewords the warning",
    els.configModalWarnText.textContent === "Writes a project override to repo-b/devgraph.tools.yaml; the global tool is unchanged.",
    els.configModalWarnText.textContent);
  let t = api.configEditTarget();
  check("a repo without the tool gets a POST to its tools", t.scope === "repo-b" && t.op === "add", JSON.stringify(t));
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: project("repo-b") } });
  await press(els.configModalSave);
  check("...sent with that repo's tools fingerprint", writes().length === 2 && writes().every(c =>
    c.url === "/api/config/repo-b/tools" && c.init.method === "POST" && ifMatch(c) === '"sha256:repo-b-tools"'),
    JSON.stringify(fetchCalls));
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  /* the destination's existing names are the server's to say: a POST that
     comes back 409 exists names the taken entry, and only then is it a PUT */
  respond = (url, init) => {
    const b = JSON.parse(init.body);
    if (init.method === "POST" && url === "/api/config/repo-a/tools" && yamlName(b.yaml) === "hot_paths")
      return { status: 409, body: { detail: { code: "exists", name: "hot_paths", message: "a tool named 'hot_paths' already exists in this scope" } } };
    return { status: 200, body: { ok: true, written: !b.dry_run, warnings: [], notes: [], scope: project("repo-a") } };
  };
  await buttons(rowFor(card("__global__"), "hot_paths"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  els.configDest.value = "repo-a";
  await els.configDest.fire("change");
  check("the title follows the destination", els.configModalTitle.textContent === "Save global tool hot_paths to repo-a",
    els.configModalTitle.textContent);
  t = api.configEditTarget();
  check("before asking, a repo destination is an add", t.scope === "repo-a" && t.op === "add", JSON.stringify(t));
  await press(els.configModalSave);
  check("a taken name is found by the dry-run POST, then dry-run as a PUT to that entry",
    writes().length === 2 && writes()[0].init.method === "POST" && writes()[1].init.method === "PUT" &&
    writes()[1].url === "/api/config/repo-a/tools/hot_paths" && writes().every(c => body(c).dry_run === true &&
    ifMatch(c) === '"sha256:repo-a-tools"'), JSON.stringify(writes()));
  check("replacing a repo's own tool needs a confirm", els.configModalSave.textContent === "Save anyway" &&
    els.configModalConfirm.textContent.includes("Replaces repo-a's own hot_paths."), els.configModalConfirm.textContent);
  check("...and the warning says so", els.configModalWarnText.textContent.includes("Replaces repo-a's own hot_paths."),
    els.configModalWarnText.textContent);
  await press(els.configModalSave);
  check("...then PUTs /api/config/repo-a/tools/hot_paths with repo-a's fingerprint", writes().length === 3 &&
    writes()[2].url === "/api/config/repo-a/tools/hot_paths" && writes()[2].init.method === "PUT" &&
    ifMatch(writes()[2]) === '"sha256:repo-a-tools"' && body(writes()[2]).dry_run === false, JSON.stringify(fetchCalls));
  check("...and refreshes the whole page, since tool resolution crosses scopes", lastCall().url === "/api/config", lastCall().url);

  // 8b. renamed in the YAML: across scopes the new name is what gets written
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  await buttons(rowFor(card("__global__"), "hot_paths"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  els.configYaml.value = "name: hot_paths_v2\ndescription: d\ncypher: x\n";
  await els.configYaml.fire("input");
  els.configDest.value = "repo-a";
  await els.configDest.fire("change");
  await press(els.configModalSave);
  check("a renamed global tool saved to a repo is a POST of the new name, never a PUT over the old one",
    writes().length === 2 && writes().every(c => c.init.method === "POST" && c.url === "/api/config/repo-a/tools"),
    JSON.stringify(writes()));
  check("...and repo-a's own hot_paths is left alone", !writes().some(c => c.url.endsWith("/tools/hot_paths")), JSON.stringify(writes()));

  // 8c. renamed within the same scope: a PUT of the old name (a rename)
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: globalBlock() } });
  await buttons(rowFor(card("__global__"), "hot_paths"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  els.configYaml.value = "name: hot_paths_v2\ndescription: d\ncypher: x\n";
  await els.configYaml.fire("input");
  await press(els.configModalSave);
  check("a rename in the same scope PUTs the old name", writes().length === 2 && writes().every(c =>
    c.init.method === "PUT" && c.url === "/api/config/__global__/tools/hot_paths" && ifMatch(c) === '"sha256:g1"'),
    JSON.stringify(writes()));
  els.configDest.value = "__global__";
  api.renderConfigPage(MODEL());

  // 9. deleting a global tool warns too, and has no destination
  await buttons(rowFor(card("__global__"), "hot_paths"), "Delete")[0].fire("click");
  check("deleting a global tool warns first", shown(els.configModalWarn) && els.configModalWarnText.textContent.startsWith("Global tools are served"),
    els.configModalWarnText.textContent);
  await press(els.configModalSave);
  check("...and offers no destination", !shown(els.configDestField), els.configDestField.style.display);
  els.configModalCancel.fire("click");

  // 13. double submits and held Enter never skip a step
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = () => ok(globalBlock());
  await buttons(rowFor(card("__global__"), "hot_paths"), "Delete")[0].fire("click");
  await press(els.configModalSave);           // Continue past the global warning
  await els.configModalSave.fire("click", 2);  // the double-click's second click lands on "Delete"
  check("a double-click on Continue does not also press Delete", fetchCalls.length === 0, JSON.stringify(fetchCalls));
  check("entering the edit step moves focus to the textarea, off the button", focused === els.configYaml, focused && focused.tagName);
  for (let i = 0; i < 5; i++) { clock += 30; await els.configModalSave.fire("click", 0); }  // held Enter: keyboard clicks, detail 0
  check("held Enter right after the button changes meaning sends nothing", fetchCalls.length === 0, JSON.stringify(fetchCalls));
  els.configModalCancel.fire("click");

  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = (url, init) => url.includes("dry_run=1")
    ? { status: 200, body: { ok: true, written: false, warnings: ["Removing node type Runbook deletes its nodes."], notes: [], scope: project("repo-a") } }
    : ok(project("repo-a"));
  await buttons(rowFor(card("repo-a"), "Runbook"), "Delete")[0].fire("click");
  await press(els.configModalSave);           // first click: the dry run
  check("the dry run asks for 'Delete anyway'", els.configModalSave.textContent === "Delete anyway", els.configModalSave.textContent);
  await els.configModalSave.fire("click", 2);  // second click of a double-click
  for (let i = 0; i < 5; i++) { clock += 30; await els.configModalSave.fire("click", 0); }
  check("a double-click or held Enter cannot reach the real delete before the warnings can be read",
    fetchCalls.length === 1 && fetchCalls[0].url.endsWith("?dry_run=1"), JSON.stringify(fetchCalls));
  check("...focus is off the confirm button", focused === els.configYaml, focused && focused.tagName);
  await press(els.configModalSave);
  check("a deliberate click later still confirms", writes().length === 2 && fetchCalls[1].init.method === "DELETE" &&
    !fetchCalls[1].url.includes("dry_run"), JSON.stringify(fetchCalls));

  // 13b. Save is disabled and says so while busy
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  let release;
  gate = new Promise(r => { release = r; });
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: project("repo-a") } });
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  clock += 1000;
  let pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("Save is disabled with busy text during the request",
    els.configModalSave.disabled === true && els.configModalSave.textContent === "Checking…", els.configModalSave.textContent);
  clock += 1000;
  await els.configModalSave.fire("click");
  check("...and a click meanwhile sends nothing more", fetchCalls.length === 1, JSON.stringify(fetchCalls));
  gate = null; release(); await pending;
  check("...then the save completes", writes().length === 2 && !els.configModal.classList.contains("open"), JSON.stringify(writes()));
  check("Save is enabled again for the next editor", (await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click"), els.configModalSave.disabled === false),
    String(els.configModalSave.disabled));
  els.configModalCancel.fire("click");

  // 14. Cancel while a save is in flight
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  gate = new Promise(r => { release = r; });
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  els.configModalCancel.fire("click");
  gate = null; release();
  let threw = null;
  try { await pending; } catch (e) { threw = e; }
  check("Cancel during the dry run: no error", threw === null, String(threw));
  check("...and no real write", writes().length === 1 && body(writes()[0]).dry_run === true, JSON.stringify(writes()));
  check("...and the modal stays closed", !els.configModal.classList.contains("open"), els.configModal.className);

  fetchCalls = [];
  gate = new Promise(r => { release = r; });
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  els.configModalCancel.fire("click");
  await buttons(card("repo-b"), "Add tool")[0].fire("click");
  els.configYaml.value = "name: second\n";
  gate = null; release(); await pending;
  check("an editor opened meanwhile is untouched by the old save",
    els.configModal.classList.contains("open") && els.configModalTitle.textContent === "Add a tool (repo-b)" &&
    els.configYaml.value === "name: second\n" && els.configModalSave.textContent === "Save" && writes().length === 1,
    JSON.stringify([els.configModalTitle.textContent, els.configModalSave.textContent, writes().length]));
  els.configModalCancel.fire("click");

  // 10. error wording
  check("422 shows the validator's message", api.describeConfigError(422, { code: "invalid", message: "tools.0.cypher: must filter on $repo_id" }) ===
    "tools.0.cypher: must filter on $repo_id", api.describeConfigError(422, { code: "invalid", message: "x" }));
  check("a plain-string detail is shown as is", api.describeConfigError(404, "unknown repo: x") === "unknown repo: x",
    api.describeConfigError(404, "unknown repo: x"));
  check("no detail falls back to the status", api.describeConfigError(500, undefined).includes("500"), api.describeConfigError(500, undefined));

  // 11. loading
  fetchCalls = [];
  await api.loadConfigPage();
  check("loading fetches /api/config and renders it", fetchCalls[0].url === "/api/config" && els.configScopes.children.length === 3,
    JSON.stringify(fetchCalls));

  // 12. wiring in index.html
  check("the Config nav button follows Repos", /data-pane="repos">Repos<\/button>\s*<button data-pane="config">Config<\/button>/.test(html),
    "no Config nav button after Repos");
  check("there is a Config pane", /<div class="settings-pane" id="pane-config"/.test(html), "no #pane-config");
  check("the pane loads on activation, quietly once it has a model",
    /dataset\.pane === "config"\) loadConfigPage\(!!configModel\)/.test(html), "nav handler does not (re)load the Config page");

  // 15. live events refresh a loaded Config page, whatever repo the graph shows
  const liveSrc = grab(/^function connectLiveEvents\(/m, "\n}");
  const live = { es: null, model: null, loads: [] };
  const liveEls = { ctlLiveUpdate: { checked: false }, repoSelect: { value: "repo-a" } };
  const connect = new Function("document", "EventSource", "loadConfigPage", "neo4jConnected", "loadSchemaTypes", "refreshGraph",
    "loadGitHistory", "populateRealRepos", "live",
    "const self = { get configModel() { return live.model; } };\n" +
    liveSrc.replace(/\bconfigModel\b/g, "self.configModel") + "\nreturn connectLiveEvents;")(
    { getElementById: id => liveEls[id] }, class { constructor() { live.es = this; } }, quiet => live.loads.push(quiet),
    false, async () => {}, () => {}, () => {}, () => {}, live);
  connect();
  const send = async ev => live.es.onmessage({ data: JSON.stringify(ev) });
  await send({ type: "registry_changed" });
  check("before the pane is opened, events load nothing", live.loads.length === 0, JSON.stringify(live.loads));
  live.model = {};
  await send({ type: "registry_changed" });
  await send({ type: "reindexed", repo_id: "other-repo", changed: 1, deleted: 0 });
  check("registry_changed and a real reindex (any repo, live updates paused) refresh it quietly",
    JSON.stringify(live.loads) === "[true,true]", JSON.stringify(live.loads));
  await send({ type: "reindexed", repo_id: "repo-a", changed: 0, deleted: 0 });
  await send({ type: "git_history_synced", mode: "full" });
  check("a no-op reindex or other events do not", live.loads.length === 2, JSON.stringify(live.loads));

  // 16. refreshes: a stale response never overwrites a newer one; quiet refreshes wait for the pane
  const withFp = fp => { const m = MODEL(); m.global.tools.fingerprint = fp; return m; };
  api.renderConfigPage(withFp("sha256:g1"));
  let releaseOld, releaseNew;
  configPayload = withFp("sha256:old");
  gate = new Promise(r => { releaseOld = r; });
  const older = api.loadConfigPage(true);
  configPayload = withFp("sha256:new");
  gate = new Promise(r => { releaseNew = r; });
  const newer = api.loadConfigPage(true);
  gate = null;
  releaseNew(); await newer;
  releaseOld(); await older;
  check("a refresh answered out of order never overwrites a newer fingerprint",
    api.model.global.tools.fingerprint === "sha256:new", api.model.global.tools.fingerprint);
  configPayload = withFp("sha256:before-write");
  gate = new Promise(r => { releaseOld = r; });
  const inFlight = api.loadConfigPage(true);
  gate = null;
  const written = globalBlock();
  written.tools.fingerprint = "sha256:written";
  api.applyConfigScope("__global__", written);
  releaseOld(); await inFlight;
  check("...nor does a refresh that started before a write's block arrived",
    api.model.global.tools.fingerprint === "sha256:written", api.model.global.tools.fingerprint);
  configPayload = MODEL();
  els["pane-config"].classList.remove("active");
  fetchCalls = [];
  await api.loadConfigPage(true);
  check("a quiet refresh while the Config pane is hidden fetches nothing", fetchCalls.length === 0, JSON.stringify(fetchCalls));
  await api.loadConfigPage();
  check("...an explicit Reload still does", fetchCalls.length === 1 && fetchCalls[0].url === "/api/config", JSON.stringify(fetchCalls));
  els["pane-config"].classList.add("active");
  check("activating the pane reloads it (what a skipped refresh missed)",
    /classList\.add\("active"\);\s*if \(btn\.dataset\.pane === "config"\) loadConfigPage\(/.test(html), "activation does not reload");

  // 17. the modal: Escape and an overlay click close it like Cancel; the status line is a list
  api.renderConfigPage(MODEL());
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  api.configModalKey({ key: "Enter" });
  check("other keys leave the modal open", els.configModal.classList.contains("open"), els.configModal.className);
  api.configModalKey({ key: "Escape" });
  check("Escape closes the modal", !els.configModal.classList.contains("open") && api.edit === null, els.configModal.className);
  let threwKey = null;
  try { api.configModalKey({ key: "Escape" }); } catch (e) { threwKey = e; }
  check("...and is harmless with no modal open", threwKey === null, String(threwKey));
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  await els.configModal.onclick({ target: els.configYaml });
  check("a click inside the box keeps the modal open", els.configModal.classList.contains("open"), els.configModal.className);
  await els.configModal.onclick({ target: els.configModal });
  check("a click on the overlay closes it", !els.configModal.classList.contains("open") && api.edit === null, els.configModal.className);
  fetchCalls = [];
  gate = new Promise(r => { release = r; });
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: project("repo-a") } });
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  api.configModalKey({ key: "Escape" });
  gate = null; release(); await pending;
  check("Escape during the dry run cancels like Cancel: no real write",
    writes().length === 1 && body(writes()[0]).dry_run === true && !els.configModal.classList.contains("open"), JSON.stringify(writes()));
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run,
    warnings: JSON.parse(init.body).dry_run ? [] : ["Key change keeps the old constraint."],
    notes: ["Written to devgraph.schema.yaml; not committed.", "Applied after the next rescan."], scope: project("repo-a") } });
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  const items = find(els.configStatus, e => e.tagName === "LI").map(e => e.textContent);
  check("after a save the status line lists each warning and note",
    JSON.stringify(items) === JSON.stringify(["Key change keeps the old constraint.", "Written to devgraph.schema.yaml; not committed.", "Applied after the next rescan."]),
    JSON.stringify(items));
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  check("...and opening the editor again clears it", els.configStatus.textContent === "" && els.configStatus.children.length === 0,
    els.configStatus.textContent);
  els.configModalCancel.fire("click");
  await buttons(card("repo-a"), "Add relationship")[0].fire("click");
  check("the Add relationship template says its endpoints must exist",
    /^#.*must name node types that already exist/m.test(els.configYaml.value) && /^from: /m.test(els.configYaml.value), els.configYaml.value);
  els.configModalCancel.fire("click");

  // 18. whole-file reset: buttons per file state
  api.renderConfigPage(MODEL());
  check("each existing file gets a Reset button: global store, project schema (once, on Nodes) and project tools",
    buttons(card("__global__"), "Reset global-tools.json…").length === 1 &&
    buttons(card("repo-a"), "Reset devgraph.schema.yaml…").length === 1 &&
    buttons(card("repo-a"), "Reset devgraph.tools.yaml…").length === 1,
    JSON.stringify(find(els.configScopes, e => e.tagName === "BUTTON" && e.textContent.startsWith("Reset")).map(b => b.textContent)));
  {
    const m = MODEL();
    m.projects[0].tools.state = "absent"; m.projects[0].tools.fingerprint = "absent";
    m.projects[1].tools.state = "invalid"; m.projects[1].tools.error = "tools: not a list";
    api.renderConfigPage(m);
    check("an absent file has no Reset button", buttons(card("repo-a"), "Reset devgraph.tools.yaml…").length === 0, card("repo-a").textContent);
    check("an invalid file keeps it (that is when it matters)", buttons(card("repo-b"), "Reset devgraph.tools.yaml…").length === 1,
      card("repo-b").textContent);
  }

  // 19. reset: the pure helpers
  r = api.configResetRequest("repo-a", "tools", "sha256:x", true);
  check("configResetRequest -> POST /api/config/<scope>/reset/<kind> with If-Match and a JSON {dry_run} body",
    r.url === "/api/config/repo-a/reset/tools" && r.init.method === "POST" && r.init.headers["If-Match"] === '"sha256:x"' &&
    r.init.headers["Content-Type"] === "application/json" && r.init.body === JSON.stringify({ dry_run: true }), JSON.stringify(r));
  r = api.configResetRequest("__global__", "tools", "sha256:g", false);
  check("...the global store's URL, a real reset", r.url === "/api/config/__global__/reset/tools" && JSON.parse(r.init.body).dry_run === false,
    JSON.stringify(r));
  check("the phrase is the repo id, or 'global' for the global store",
    api.configResetPhrase("repo-a") === "repo-a" && api.configResetPhrase("__global__") === "global",
    api.configResetPhrase("__global__"));
  check("configResetReady is an exact match only (case, whitespace, empty)",
    api.configResetReady("repo-a", "repo-a") && !api.configResetReady("Repo-a", "repo-a") && !api.configResetReady(" repo-a", "repo-a") &&
    !api.configResetReady("repo-a ", "repo-a") && !api.configResetReady("", "") && !api.configResetReady("__global__", "global"),
    "loose match accepted");
  let d = api.describeConfigReset({ removed: { tools: null }, warnings: [], notes: [] });
  check("an unreadable file says its whole contents go", d.removed.length === 1 && /not valid YAML/.test(d.removed[0]), JSON.stringify(d));
  d = api.describeConfigReset({ removed: { node_types: ["Runbook"], relationships: ["DOCUMENTS"] }, warnings: ["w"], notes: ["n"] });
  check("a schema listing names node types and relationships",
    JSON.stringify(d) === JSON.stringify({ removed: ["Node type Runbook", "Relationship DOCUMENTS"], warnings: ["w"], notes: ["n"] }), JSON.stringify(d));

  // 20. reset flow: dry run, list, typed name, armed Reset with the dry run's fingerprint
  api.renderConfigPage(MODEL());
  const RWARN = "<b>bold</b> warning";
  const afterA = project("repo-a"); afterA.tools.state = "absent"; afterA.tools.fingerprint = "absent";
  const afterG = globalBlock(); afterG.tools.entries.push({ name: "after_reset_marker", tool_id: "gl_after_reset_marker", yaml: "", badges: [] });
  fetchCalls = [];
  respond = (url, init) => JSON.parse(init.body).dry_run
    ? { status: 200, body: { ok: true, written: false, file: "devgraph.tools.yaml", fingerprint: "sha256:dry-fp",
        removed: { tools: [HOSTILE, "hot_paths"] }, warnings: [RWARN], notes: ["After the reset, global tool hot_paths is served in repo-a."],
        scope: project("repo-a"), global: globalBlock() } }
    : { status: 200, body: { ok: true, written: true, file: "devgraph.tools.yaml", fingerprint: "absent", removed: { tools: [HOSTILE, "hot_paths"] },
        warnings: [], notes: ["Deleted devgraph.tools.yaml; not staged or committed."], scope: afterA, global: afterG } };
  await buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0].fire("click");
  check("Reset opens the dialog and sends only a dry run, with the page's fingerprint",
    els.configResetModal.classList.contains("open") && fetchCalls.length === 1 && fetchCalls[0].url === "/api/config/repo-a/reset/tools" &&
    body(fetchCalls[0]).dry_run === true && ifMatch(fetchCalls[0]) === '"sha256:repo-a-tools"', JSON.stringify(fetchCalls));
  check("...titled with the file and scope", els.configResetTitle.textContent === "Reset devgraph.tools.yaml (repo-a)", els.configResetTitle.textContent);
  check("...lists what is removed, warnings and notes as text",
    els.configResetList.textContent.includes("Tool " + HOSTILE) && els.configResetList.textContent.includes(RWARN) &&
    els.configResetList.textContent.includes("global tool hot_paths is served in repo-a") &&
    allEls.every(e => !e._html.includes("<img") && !e._html.includes("<b>")), els.configResetList.textContent);
  check("...the dry run's list comes before the phrase input",
    html.indexOf('id="configResetList"') < html.indexOf('id="configResetPhraseField"') && shown(els.configResetPhraseField),
    "phrase input before the list");
  const recoverText = els.configResetList.textContent;
  check("...which asks for the repo id", els.configResetPhraseLabel.textContent === "Type repo-a to reset", els.configResetPhraseLabel.textContent);
  check("Reset is disabled before the name is typed", els.configResetConfirm.disabled === true, String(els.configResetConfirm.disabled));
  els.configResetTyped.value = "Repo-a";
  await els.configResetTyped.fire("input");
  await press(els.configResetConfirm);
  check("a wrong name keeps it disabled and sends nothing", els.configResetConfirm.disabled === true && fetchCalls.length === 1,
    JSON.stringify(fetchCalls));
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  check("the exact name enables Reset", els.configResetConfirm.disabled === false && els.configResetConfirm.textContent === "Reset",
    els.configResetConfirm.textContent);
  for (let i = 0; i < 5; i++) { clock += 30; await els.configResetConfirm.fire("click", 0); }
  await els.configResetConfirm.fire("click", 2);
  check("held Enter or a double-click right after it arms sends nothing", fetchCalls.length === 1, JSON.stringify(fetchCalls));
  await press(els.configResetConfirm);
  check("a deliberate click resets with the dry run's fingerprint as If-Match",
    fetchCalls.length === 2 && fetchCalls[1].url === "/api/config/repo-a/reset/tools" && body(fetchCalls[1]).dry_run === false &&
    ifMatch(fetchCalls[1]) === '"sha256:dry-fp"', JSON.stringify(fetchCalls));
  check("...closes the dialog", !els.configResetModal.classList.contains("open") && api.reset === null, els.configResetModal.className);
  check("...re-renders the repo's card from the response (no file, so no Reset)",
    buttons(card("repo-a"), "Reset devgraph.tools.yaml…").length === 0, card("repo-a").textContent);
  check("...and the global card from its block", !!rowFor(card("__global__"), "after_reset_marker"), card("__global__").textContent);
  check("...the status says what was deleted, not 'written', and the card carries the same outcome",
    els.configStatus.textContent.includes("Deleted devgraph.tools.yaml; not staged or committed.") && !els.configStatus.textContent.includes("Written") &&
    card("repo-a").textContent.includes("Deleted devgraph.tools.yaml; not staged or committed."), els.configStatus.textContent);
  check("...the recoverability sentence was shown in the dialog, before the typed phrase (it is in the list above the phrase field)",
    recoverText.includes("Git can restore a tracked file; an untracked file or the global store cannot be restored."), recoverText);
  check("the dialog's Reset button is styled as destructive", /class="[^"]*btn-danger[^"]*" id="configResetConfirm"/.test(html), "no btn-danger");
  configPayload = MODEL();
  await api.loadConfigPage();
  configPayload = null;
  check("a config reload clears the card's reset outcome", !card("repo-a").textContent.includes("Deleted devgraph.tools.yaml"), card("repo-a").textContent);

  // 21. reset: 412 clears the name and offers Re-check; nothing is deleted
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  const freshB = project("repo-b"); freshB.schema.fingerprint = "sha256:repo-b-schema-2";
  respond = (url, init) => {
    if (!init.method) return { status: 200, body: freshB };
    const fp = ifMatch({ init });
    if (JSON.parse(init.body).dry_run)
      return { status: 200, body: { ok: true, written: false, fingerprint: fp.slice(1, -1), removed: { node_types: ["Runbook"], relationships: [] },
        warnings: ["Removing node type Runbook deletes its nodes on the next rescan."], notes: [], scope: project("repo-b") } };
    return { status: 412, body: { detail: { code: "stale", message: "devgraph.schema.yaml changed on disk", scope: freshB } } };
  };
  await buttons(card("repo-b"), "Reset devgraph.schema.yaml…")[0].fire("click");
  check("a schema reset dry-runs /reset/schema", fetchCalls[0].url === "/api/config/repo-b/reset/schema" &&
    els.configResetList.textContent.includes("Node type Runbook"), JSON.stringify(fetchCalls));
  els.configResetTyped.value = "repo-b";
  await els.configResetTyped.fire("input");
  await press(els.configResetConfirm);
  check("a 412 keeps the dialog open and says the file changed",
    els.configResetModal.classList.contains("open") && /changed since this list was made/.test(els.configResetError.textContent) &&
    shown(els.configResetError), els.configResetError.textContent);
  check("...clears the typed name and the stale list", els.configResetTyped.value === "" && els.configResetList.children.length === 0 &&
    !shown(els.configResetPhraseField) && els.configResetConfirm.disabled === true, els.configResetList.textContent);
  check("...and offers Re-check", shown(els.configResetRecheck), els.configResetRecheck.style.display);
  fetchCalls = [];
  await els.configResetRecheck.fire("click");
  await new Promise(r => setTimeout(r, 0));
  check("Re-check fetches the scope, then dry-runs again with its current fingerprint",
    fetchCalls.length === 2 && fetchCalls[0].url === "/api/config/repo-b" && body(fetchCalls[1]).dry_run === true &&
    ifMatch(fetchCalls[1]) === '"sha256:repo-b-schema-2"', JSON.stringify(fetchCalls));
  check("...shows the list again and hides itself", els.configResetList.textContent.includes("Node type Runbook") &&
    !shown(els.configResetRecheck) && !shown(els.configResetError), els.configResetList.textContent);
  api.configModalKey({ key: "Escape" });
  check("Escape closes the reset dialog", !els.configResetModal.classList.contains("open") && api.reset === null, els.configResetModal.className);

  // 22. the global store asks for 'global', not the scope token
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, fingerprint: "sha256:g1",
    removed: { tools: ["hot_paths"] }, warnings: [], notes: ["Removes 1 global tool(s) from every repository's MCP sessions."], scope: globalBlock(), global: globalBlock() } });
  await buttons(card("__global__"), "Reset global-tools.json…")[0].fire("click");
  check("a global reset asks for 'global'", els.configResetPhraseLabel.textContent === "Type global to reset" &&
    els.configResetTitle.textContent === "Reset the global tools store", els.configResetPhraseLabel.textContent);
  els.configResetTyped.value = "__global__";
  await els.configResetTyped.fire("input");
  check("...the scope token is not accepted", els.configResetConfirm.disabled === true, String(els.configResetConfirm.disabled));
  check("...and the dialog says plainly the global store cannot be restored",
    els.configResetList.textContent.includes("cannot be restored") && !els.configResetList.textContent.includes("Git can restore"),
    els.configResetList.textContent);
  els.configResetTyped.value = "global";
  await els.configResetTyped.fire("input");
  await press(els.configResetConfirm);
  check("...'global' resets the store, then the page refreshes (every repo's override badges move)",
    writes().length === 2 && writes()[1].url === "/api/config/__global__/reset/tools" && body(writes()[1]).dry_run === false &&
    lastCall().url === "/api/config", JSON.stringify(fetchCalls));

  // 23. reset: cancel while a request is in flight
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, fingerprint: "sha256:repo-a-tools",
    removed: { tools: [] }, warnings: [], notes: [], scope: afterA, global: globalBlock() } });
  gate = new Promise(r => { release = r; });
  pending = buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0].fire("click");
  await Promise.resolve();
  els.configResetCancel.fire("click");
  gate = null; release();
  threw = null;
  try { await pending; } catch (e) { threw = e; }
  check("Cancel during the dry run: no error, no reset, dialog stays closed",
    threw === null && fetchCalls.length === 1 && body(fetchCalls[0]).dry_run === true && !els.configResetModal.classList.contains("open"),
    String(threw) + JSON.stringify(fetchCalls));
  fetchCalls = [];
  await buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0].fire("click");
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  clock += 1000;
  gate = new Promise(r => { release = r; });
  pending = els.configResetConfirm.fire("click");
  await Promise.resolve();
  check("Reset is disabled with busy text while it runs", els.configResetConfirm.disabled === true &&
    els.configResetConfirm.textContent === "Resetting…", els.configResetConfirm.textContent);
  els.configResetCancel.fire("click");
  gate = null; release(); await pending;
  check("Cancel during the reset itself: the dialog stays closed, the page still shows what is on disk",
    !els.configResetModal.classList.contains("open") && fetchCalls.length === 2 &&
    buttons(card("repo-a"), "Reset devgraph.tools.yaml…").length === 0, JSON.stringify(fetchCalls));

  // 23b. a cancelled reset's late answers never touch the dialog opened after it
  api.renderConfigPage(MODEL());
  const staleOn = new Set();
  respond = (url, init) => {
    if (!init.method) return { status: 200, body: project("repo-a") };
    if (staleOn.has(url)) return { status: 412, body: { detail: { code: "stale", message: "changed", scope: project("repo-a") } } };
    return { status: 200, body: { ok: true, written: false, fingerprint: "sha256:fp", removed: { tools: ["kept_listing"] }, warnings: [], notes: [], scope: project("repo-a") } };
  };
  staleOn.add("/api/config/repo-a/reset/tools");
  gate = new Promise(r => { release = r; });
  pending = buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0].fire("click");
  await Promise.resolve();
  els.configResetCancel.fire("click");
  gate = null;
  await buttons(card("repo-a"), "Reset devgraph.schema.yaml…")[0].fire("click");
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  release(); await pending;
  check("reset A's 412, arriving after B opened, leaves B's dialog alone",
    els.configResetTitle.textContent === "Reset devgraph.schema.yaml (repo-a)" && els.configResetTyped.value === "repo-a" &&
    els.configResetList.textContent.includes("kept_listing") && !shown(els.configResetError) && !shown(els.configResetRecheck),
    JSON.stringify([els.configResetTyped.value, els.configResetError.textContent, els.configResetList.textContent]));
  check("the review moves focus to the name input", focused === els.configResetTyped, focused && focused.tagName);
  els.configResetCancel.fire("click");
  /* the same after Re-check's fetch of the scope */
  await buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0].fire("click");
  check("(A is now stale)", shown(els.configResetRecheck), els.configResetError.textContent);
  staleOn.clear();
  gate = new Promise(r => { release = r; });
  fetchCalls = [];
  pending = els.configResetRecheck.fire("click");
  await Promise.resolve();
  els.configResetCancel.fire("click");
  gate = null;
  await buttons(card("repo-a"), "Reset devgraph.schema.yaml…")[0].fire("click");
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  const before = fetchCalls.length;
  release(); await pending;
  await new Promise(r => setTimeout(r, 0));
  check("a cancelled Re-check stops after its fetch: no dry run of A, B untouched",
    fetchCalls.length === before && els.configResetTitle.textContent === "Reset devgraph.schema.yaml (repo-a)" &&
    els.configResetTyped.value === "repo-a", JSON.stringify(fetchCalls.slice(before)));
  els.configResetCancel.fire("click");

  // 23c. a reset that fails after its dialog was cancelled still says so; opening the dialog keeps earlier notes
  configPayload = MODEL();
  api.renderConfigPage(MODEL());
  els.configStatus.textContent = "Written to devgraph.tools.yaml; not committed.";
  respond = (url, init) => JSON.parse(init.body).dry_run
    ? { status: 200, body: { ok: true, written: false, fingerprint: "sha256:fp", removed: { tools: [] }, warnings: [], notes: [], scope: project("repo-a") } }
    : { status: 500, body: { detail: { code: "io", message: "could not write devgraph.tools.yaml" } } };
  await buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0].fire("click");
  check("opening the reset dialog keeps the previous write's notes", els.configStatus.textContent === "Written to devgraph.tools.yaml; not committed.",
    els.configStatus.textContent);
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  clock += 1000;
  gate = new Promise(r => { release = r; });
  pending = els.configResetConfirm.fire("click");
  await Promise.resolve();
  els.configResetCancel.fire("click");
  gate = null; release(); await pending;
  check("a reset that fails after Cancel reports the error on the status line",
    els.configStatus.textContent.includes("could not write devgraph.tools.yaml"), els.configStatus.textContent);

  // 24. project-config switch
  r = api.configToggleRequest("repo a", false, true);
  check("configToggleRequest -> PUT /api/config/<repo>/project-config, JSON {enabled, dry_run}, no If-Match",
    r.url === "/api/config/repo%20a/project-config" && r.init.method === "PUT" && r.init.headers["Content-Type"] === "application/json" &&
    r.init.body === JSON.stringify({ enabled: false, dry_run: true }) && !("If-Match" in r.init.headers), JSON.stringify(r));
  const switchOf = scope => find(card(scope), e => e.tagName === "INPUT" && e.type === "checkbox")[0];
  const disabledA = () => {
    const p = project("repo-a"); p.project_config_enabled = false; p.tools.state = "disabled";
    p.tools.badges = [{ level: "muted", kind: "not-served", text: "Project config disabled", detail: "" }];
    return p;
  };
  const NOTES = ["schema: applied at the next rescan (`devgraph rescan repo-a --now` to apply now)", "project tools: picked up by running MCP sessions within 2 s"];
  let toggleWarnings = [];
  let toggleFail = null;
  respond = (url, init) => {
    const b = JSON.parse(init.body);
    if (!b.dry_run && toggleFail) return toggleFail;
    const after = b.dry_run || b.enabled ? project("repo-a") : disabledA();
    return { status: 200, body: { ok: true, written: !b.dry_run, changed: true, enabled: b.enabled,
      warnings: b.dry_run ? toggleWarnings : [], notes: NOTES, scope: after, global: afterG } };
  };
  api.renderConfigPage(MODEL());
  check("each project card has a Project config switch showing its state",
    switchOf("repo-a") && switchOf("repo-a").checked === true && card("repo-a").textContent.includes("Project config") &&
    find(card("__global__"), e => e.tagName === "INPUT").length === 0, card("repo-a").textContent);
  fetchCalls = [];
  let sw = switchOf("repo-a"); sw.checked = false; await sw.fire("change");
  check("disabling without warnings: a dry run, then the write at once",
    fetchCalls.length === 2 && body(fetchCalls[0]).dry_run === true && body(fetchCalls[1]).dry_run === false &&
    body(fetchCalls[1]).enabled === false && fetchCalls.every(c => c.url === "/api/config/repo-a/project-config"), JSON.stringify(fetchCalls));
  check("...the card re-renders off, with the disabled badge and the effect notes",
    switchOf("repo-a").checked === false && card("repo-a").textContent.includes("Project config disabled") &&
    NOTES.every(n => card("repo-a").textContent.includes(n)), card("repo-a").textContent);
  check("...and the global card from its block", !!rowFor(card("__global__"), "after_reset_marker"), card("__global__").textContent);
  check("...the notes also show in the top status", NOTES.every(n => els.configStatus.textContent.includes(n)), els.configStatus.textContent);
  configPayload = MODEL();
  await api.loadConfigPage();
  check("...and the card's toggle notes are gone after the next config reload", !card("repo-a").textContent.includes(NOTES[0]), card("repo-a").textContent);
  configPayload = null;

  api.renderConfigPage(MODEL());
  fetchCalls = [];
  sw = switchOf("repo-a"); sw.checked = true; await sw.fire("change");
  check("enabling applies at once, no dry run", fetchCalls.length === 1 && body(fetchCalls[0]).enabled === true &&
    body(fetchCalls[0]).dry_run === false, JSON.stringify(fetchCalls));

  api.renderConfigPage(MODEL());
  fetchCalls = [];
  toggleWarnings = [HOSTILE + " no longer served", "Removing node type Runbook deletes its nodes on the next rescan."];
  sw = switchOf("repo-a"); sw.checked = false; await sw.fire("change");
  check("disabling with warnings stops after the dry run", fetchCalls.length === 1 && body(fetchCalls[0]).dry_run === true, JSON.stringify(fetchCalls));
  check("...the switch still shows the server's state (on), held while the confirm is open",
    switchOf("repo-a").checked === true && switchOf("repo-a").disabled === true, String(switchOf("repo-a").checked));
  check("...and lists the warnings as text with 'Disable anyway'",
    card("repo-a").textContent.includes(HOSTILE + " no longer served") && buttons(card("repo-a"), "Disable anyway").length === 1 &&
    allEls.every(e => !e._html.includes("<img")), card("repo-a").textContent);
  await buttons(card("repo-a"), "Disable anyway")[0].fire("click", 2);
  for (let i = 0; i < 5; i++) { clock += 30; await buttons(card("repo-a"), "Disable anyway")[0].fire("click", 0); }
  check("a double-click or held Enter cannot confirm before it arms", fetchCalls.length === 1, JSON.stringify(fetchCalls));
  await press(buttons(card("repo-a"), "Disable anyway")[0]);
  check("'Disable anyway' writes", fetchCalls.length === 2 && body(fetchCalls[1]).dry_run === false && body(fetchCalls[1]).enabled === false &&
    switchOf("repo-a").checked === false, JSON.stringify(fetchCalls));

  api.renderConfigPage(MODEL());
  fetchCalls = [];
  sw = switchOf("repo-a"); sw.checked = false; await sw.fire("change");
  await press(buttons(card("repo-a"), "Cancel")[0]);
  check("Cancel on the confirm: nothing written, the switch stays on and usable",
    fetchCalls.length === 1 && switchOf("repo-a").checked === true && switchOf("repo-a").disabled === false &&
    buttons(card("repo-a"), "Disable anyway").length === 0, JSON.stringify(fetchCalls));

  toggleWarnings = [];
  toggleFail = { status: 500, body: { detail: { code: "io", message: "could not update the registry" } } };
  fetchCalls = [];
  sw = switchOf("repo-a"); sw.checked = false; await sw.fire("change");
  check("after an error the switch snaps back to the server's state and shows the error",
    fetchCalls.length === 2 && switchOf("repo-a").checked === true && switchOf("repo-a").disabled === false &&
    card("repo-a").textContent.includes("could not update the registry"), card("repo-a").textContent);
  toggleFail = null;

  // 25. Copy to…: which rows offer it, and where to
  configPayload = MODEL();
  const CONFLICT = { level: "error", kind: "schema-conflict", text: "Key conflict with repo-b",
    detail: "incompatible declarations of label 'runbook' in one shared database: repo-a declares Runbook keyed on (slug); repo-b declares Runbook keyed on (id)." };
  const COPY_MODEL = () => {
    const a = project("repo-a", [
      { name: "hot_paths", tool_id: "repo-a_hot_paths", yaml: "name: hot_paths\n", origin: "project (overrides global)",
        badges: [{ level: "info", kind: "overrides-global", text: "Overrides global tool", detail: "wins" }] },
      { name: "mine", tool_id: "repo-a_mine", yaml: "name: mine\ndescription: d\ncypher: x\n", origin: "project", badges: [] },
      { name: "find_callers", tool_id: "repo-a_find_callers", yaml: "name: find_callers\n", origin: "project",
        badges: [{ level: "warn", kind: "locked-shadow", text: "Ignored: shadows a locked tool", detail: "" }] },
    ]);
    a.schema.node_types = [{ label: "Runbook", yaml: "label: Runbook\nkey: [slug]\n", editable: true, badges: [CONFLICT] },
      { label: HOSTILE, yaml: "label: '" + HOSTILE + "'\n", editable: true, badges: [] }];
    a.schema.relationships = [{ type: "DOCUMENTS", yaml: "type: DOCUMENTS\nfrom: Runbook\nto: Service\n", editable: true, badges: [] },
      { type: "OWNS", yaml: "type: OWNS\n", editable: false, badges: [{ level: "warn", kind: "ambiguous", text: "Declared more than once", detail: "" }] }];
    return { global: globalBlock(), projects: [a, project("repo-b"), project(HOSTILE)] };
  };
  api.renderConfigPage(COPY_MODEL());
  let ca = card("repo-a");
  const copyBtn = (scope, name) => buttons(rowFor(card(scope), name), "Copy to…");
  check("Copy to… is on project node types, relationships and tools",
    copyBtn("repo-a", "Runbook").length === 1 && copyBtn("repo-a", "DOCUMENTS").length === 1 && copyBtn("repo-a", "mine").length === 1 &&
    copyBtn("repo-a", "hot_paths").length === 1, ca.textContent);
  check("...not on an ambiguous relationship", copyBtn("repo-a", "OWNS").length === 0, rowFor(ca, "OWNS").textContent);
  check("...not on a tool that shadows a locked one", copyBtn("repo-a", "find_callers").length === 0, rowFor(ca, "find_callers").textContent);
  check("...and not on global tools (their editor's Save to does that)",
    find(card("__global__"), e => e.tagName === "BUTTON" && e.textContent === "Copy to…").length === 0, card("__global__").textContent);
  {
    const solo = { global: globalBlock(), projects: [COPY_MODEL().projects[0]] };
    check("with one repository a schema entry has nowhere to go, a tool still has the global store",
      !api.configCanCopy(solo, "repo-a", "node_types", solo.projects[0].schema.node_types[0]) &&
      !api.configCanCopy(solo, "repo-a", "relationships", solo.projects[0].schema.relationships[0]) &&
      api.configCanCopy(solo, "repo-a", "tools", solo.projects[0].tools.entries[1]), "wrong visibility with one repo");
    api.renderConfigPage(solo);
    check("...and the rendered rows agree", copyBtn("repo-a", "Runbook").length === 0 && copyBtn("repo-a", "mine").length === 1,
      card("repo-a").textContent);
    api.renderConfigPage(COPY_MODEL());
  }
  const destsOf = (scope, section) => JSON.stringify(api.configCopyDestinations(api.model, scope, section).map(d => d.value));
  check("schema destinations are the other repositories, never the source or the global store",
    destsOf("repo-a", "node_types") === JSON.stringify(["repo-b", HOSTILE]) && destsOf("repo-b", "relationships") === JSON.stringify(["repo-a", HOSTILE]),
    destsOf("repo-a", "node_types"));
  check("tool destinations add the global store", destsOf("repo-a", "tools") === JSON.stringify(["repo-b", HOSTILE, "__global__"]),
    destsOf("repo-a", "tools"));
  const conflictBadge = byClass(rowFor(card("repo-a"), "Runbook"), "cfg-badge")[0];
  check("a schema conflict badge shows its text as an error, with the detail on hover",
    conflictBadge && conflictBadge.textContent === "Key conflict with repo-b" && conflictBadge.classList.contains("error") &&
    conflictBadge.dataset.tip === CONFLICT.detail && tooltips.includes(conflictBadge), conflictBadge && conflictBadge.className);

  // 26. the copy dialog: read-only, titled, a destination other than the source
  fetchCalls = [];
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  check("Copy opens the editor titled 'Copy node type Runbook from repo-a'",
    els.configModal.classList.contains("open") && els.configModalTitle.textContent === "Copy node type Runbook from repo-a",
    els.configModalTitle.textContent);
  check("...with the source entry's YAML, read-only", els.configYaml.value === "label: Runbook\nkey: [slug]\n" && els.configYaml.readOnly === true &&
    shown(els.configYaml), String(els.configYaml.readOnly));
  check("...a 'Copy to' list without the source, defaulting to the first other repo",
    shown(els.configDestField) && els.configDestLabel.textContent === "Copy to" &&
    JSON.stringify(els.configDest.children.map(o => o.value)) === JSON.stringify(["repo-b", HOSTILE]) && els.configDest.value === "repo-b" &&
    api.edit.dest === "repo-b", JSON.stringify(els.configDest.children.map(o => o.value)));
  check("...saying where it writes and that the source is unchanged",
    els.configModalWarnText.textContent === "Writes to repo-b/devgraph.schema.yaml; repo-a is unchanged." && shown(els.configModalWarn),
    els.configModalWarnText.textContent);
  check("...a Copy button, nothing sent yet", els.configModalSave.textContent === "Copy" && fetchCalls.length === 0, els.configModalSave.textContent);
  t = api.configEditTarget();
  check("the target is an add in the destination", t.scope === "repo-b" && t.op === "add" && t.name === null, JSON.stringify(t));
  for (let i = 0; i < 5; i++) { clock += 30; await els.configModalSave.fire("click", 0); }
  check("held Enter right as the dialog opens sends nothing", fetchCalls.length === 0, JSON.stringify(fetchCalls));

  // 27. absent in the destination: dry-run POST, then the POST, same If-Match; the page reloads
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [],
    notes: ["Written to devgraph.schema.yaml; not committed.", "Applied after the next rescan."], scope: project("repo-b") } });
  await press(els.configModalSave);
  check("an absent entry is dry-run then written as a POST to the destination with its fingerprint and the source YAML",
    writes().length === 2 && writes().every(c => c.url === "/api/config/repo-b/schema/node_types" && c.init.method === "POST" &&
      ifMatch(c) === '"sha256:repo-b-schema"' && body(c).yaml === "label: Runbook\nkey: [slug]\n") &&
    body(writes()[0]).dry_run === true && body(writes()[1]).dry_run === false, JSON.stringify(writes()));
  check("...never touching the source", !fetchCalls.some(c => c.url.includes("repo-a")), JSON.stringify(fetchCalls));
  check("...closes the dialog, shows the notes and reloads the whole page",
    !els.configModal.classList.contains("open") && els.configStatus.textContent.includes("not committed") && lastCall().url === "/api/config",
    JSON.stringify([els.configStatus.textContent, lastCall().url]));

  // 28. exists in the destination: confirm 'Replaces …', then PUT with the dry run's If-Match
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  respond = (url, init) => {
    const b = JSON.parse(init.body);
    if (init.method === "POST") return { status: 409, body: { detail: { code: "exists", name: "Runbook", message: "node type 'Runbook' already exists" } } };
    return { status: 200, body: { ok: true, written: !b.dry_run, warnings: [], notes: [], scope: project("repo-b") } };
  };
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  await press(els.configModalSave);
  check("a taken name: the dry-run POST finds it, then the PUT is dry-run",
    writes().length === 2 && writes()[0].init.method === "POST" && writes()[1].init.method === "PUT" &&
    writes()[1].url === "/api/config/repo-b/schema/node_types/Runbook" && writes().every(c => body(c).dry_run === true &&
    ifMatch(c) === '"sha256:repo-b-schema"'), JSON.stringify(writes()));
  check("...and asks to confirm 'Replaces repo-b's own node type Runbook.'",
    els.configModalSave.textContent === "Copy anyway" && els.configModalConfirm.textContent.includes("Replaces repo-b's own node type Runbook.") &&
    els.configModalWarnText.textContent.includes("Replaces repo-b's own node type Runbook."), els.configModalConfirm.textContent);
  t = api.configEditTarget();
  check("...the target is now a replace of that name", t.scope === "repo-b" && t.op === "replace" && t.name === "Runbook", JSON.stringify(t));
  await els.configModalSave.fire("click", 2);
  for (let i = 0; i < 5; i++) { clock += 30; await els.configModalSave.fire("click", 0); }
  check("a double-click or held Enter cannot reach the replace", writes().length === 2, JSON.stringify(writes()));
  /* a refresh lands meanwhile with a newer destination fingerprint: the write
     still carries the one the dry run was reviewed against (so it 412s, never overwrites unseen) */
  { const newer = project("repo-b"); newer.schema.fingerprint = "sha256:repo-b-schema-newer"; api.applyConfigScope("repo-b", newer); }
  await press(els.configModalSave);
  check("the confirmed write PUTs with the dry run's If-Match",
    writes().length === 3 && writes()[2].init.method === "PUT" && writes()[2].url === "/api/config/repo-b/schema/node_types/Runbook" &&
    body(writes()[2]).dry_run === false && ifMatch(writes()[2]) === '"sha256:repo-b-schema"', JSON.stringify(writes()));
  check("...and reloads the page", lastCall().url === "/api/config", lastCall().url);

  // 29. dry-run warnings (a conflict the copy would create) need a confirm; a new destination withdraws it
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  const CWARN = "Creates a schema conflict: incompatible declarations of label 'runbook' <b>x</b>";
  respond = (url, init) => {
    const b = JSON.parse(init.body);
    return { status: 200, body: { ok: true, written: !b.dry_run, warnings: b.dry_run ? [CWARN] : [], notes: [], scope: project("repo-b") } };
  };
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  await press(els.configModalSave);
  check("a dry run that warns stops for a confirm, listing the warning as text",
    writes().length === 1 && els.configModalSave.textContent === "Copy anyway" && els.configModalConfirm.textContent.includes(CWARN) &&
    !els.configModalConfirm._html.includes("<b>") && els.configModalConfirm.textContent.includes("Check before copying:"),
    els.configModalConfirm.textContent);
  els.configDest.value = HOSTILE;
  await els.configDest.fire("change");
  check("changing the destination withdraws the confirm", els.configModalSave.textContent === "Copy" && !shown(els.configModalConfirm) &&
    api.edit.crossName === null, els.configModalSave.textContent);
  await press(els.configModalSave);
  check("...so the next click dry-runs the new destination (its id encoded, its fingerprint)",
    writes().length === 2 && body(writes()[1]).dry_run === true &&
    writes()[1].url === "/api/config/" + encodeURIComponent(HOSTILE) + "/schema/node_types" && ifMatch(writes()[1]) === '"sha256:' + HOSTILE + '-schema"',
    JSON.stringify(writes()));
  await press(els.configModalSave);
  check("...and the confirm writes there", writes().length === 3 && body(writes()[2]).dry_run === false &&
    writes()[2].url === "/api/config/" + encodeURIComponent(HOSTILE) + "/schema/node_types", JSON.stringify(writes()));

  // 30. an identical relationship: 409 exists without a name -> a message, no write
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  const SAME = "repo-b already declares this identical relationship; nothing to copy.";
  respond = () => ({ status: 409, body: { detail: { code: "exists", message: SAME } } });
  await copyBtn("repo-a", "DOCUMENTS")[0].fire("click");
  check("copying a relationship is titled for it", els.configModalTitle.textContent === "Copy relationship DOCUMENTS from repo-a",
    els.configModalTitle.textContent);
  await press(els.configModalSave);
  check("an identical relationship in the destination shows the server's message and writes nothing",
    writes().length === 1 && body(writes()[0]).dry_run === true && writes()[0].init.method === "POST" &&
    writes()[0].url === "/api/config/repo-b/schema/relationships" && els.configModalError.textContent === SAME && shown(els.configModalError) &&
    !shown(els.configModalReload) && els.configModal.classList.contains("open") && els.configModalSave.textContent === "Copy",
    JSON.stringify([writes(), els.configModalError.textContent]));
  els.configModalCancel.fire("click");

  // 31. 412 from the destination: Reload fetches the destination, keeps the dialog, the next try carries its fingerprint
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  const freshDest = project("repo-b"); freshDest.schema.fingerprint = "sha256:repo-b-schema-2";
  respond = (url, init) => {
    if (!init.method) return { status: 200, body: freshDest };
    return ifMatch({ init }) === '"sha256:repo-b-schema"'
      ? { status: 412, body: { detail: { code: "stale", message: "changed on disk" } } }
      : { status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: freshDest } };
  };
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  await press(els.configModalSave);
  check("a stale destination keeps the dialog and offers Reload, writing nothing",
    els.configModal.classList.contains("open") && shown(els.configModalReload) && writes().length === 1 && body(writes()[0]).dry_run === true,
    JSON.stringify(writes()));
  await els.configModalReload.fire("click");
  check("Reload fetches the destination, not the source", lastCall().url === "/api/config/repo-b" &&
    els.configYaml.value === "label: Runbook\nkey: [slug]\n" && els.configModal.classList.contains("open"), lastCall().url);
  await press(els.configModalSave);
  check("...and the copy then goes through with the reloaded fingerprint",
    writes().length === 3 && writes().slice(1).every(c => ifMatch(c) === '"sha256:repo-b-schema-2"' && c.url === "/api/config/repo-b/schema/node_types"),
    JSON.stringify(writes()));

  // 32. a project tool to the global store
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: globalBlock() } });
  await copyBtn("repo-a", "mine")[0].fire("click");
  check("a tool's destinations include the global store", JSON.stringify(els.configDest.children.map(o => [o.value, o.textContent])) ===
    JSON.stringify([["repo-b", "repo-b"], [HOSTILE, HOSTILE], ["__global__", "Global store"]]),
    JSON.stringify(els.configDest.children.map(o => o.value)));
  els.configDest.value = "__global__";
  await els.configDest.fire("change");
  check("...the global store's warning says it is served everywhere and the source keeps overriding it",
    els.configModalWarnText.textContent === "Global tools are served in every registered repository's MCP sessions (3 repos). " +
      "Copying adds it there; repo-a's own mine keeps overriding it in repo-a.", els.configModalWarnText.textContent);
  await press(els.configModalSave);
  check("...and the copy POSTs /api/config/__global__/tools with the global fingerprint",
    writes().length === 2 && writes().every(c => c.url === "/api/config/__global__/tools" && c.init.method === "POST" && ifMatch(c) === '"sha256:g1"' &&
      body(c).yaml === "name: mine\ndescription: d\ncypher: x\n"), JSON.stringify(writes()));
  check("...then reloads the page", lastCall().url === "/api/config", lastCall().url);

  // 33. Cancel while a copy is in flight: no write
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: project("repo-b") } });
  gate = new Promise(r => { release = r; });
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("Copy is disabled with busy text during the dry run", els.configModalSave.disabled === true &&
    els.configModalSave.textContent === "Checking…", els.configModalSave.textContent);
  els.configModalCancel.fire("click");
  gate = null; release(); await pending;
  check("Cancel during a copy's dry run: no write", writes().length === 1 && body(writes()[0]).dry_run === true &&
    !els.configModal.classList.contains("open"), JSON.stringify(writes()));

  // 34. hostile names stay text in the copy dialog
  api.renderConfigPage(COPY_MODEL());
  await copyBtn("repo-a", HOSTILE)[0].fire("click");
  check("a hostile node type and repository id are text in the copy dialog",
    els.configModalTitle.textContent === "Copy node type " + HOSTILE + " from repo-a" &&
    els.configDest.children.some(o => o.textContent === HOSTILE) &&
    allEls.every(e => !e._html.includes("<img") && !e._html.includes("onerror")), els.configModalTitle.textContent);
  els.configModalCancel.fire("click");

  // 35. run_cypher's real state is read-only on the Global card
  const cypherModel = enabled => {
    const m = MODEL();
    m.global.tools.run_cypher_enabled = enabled;
    if (enabled) m.global.tools.builtin.push({ name: "run_cypher", tool_id: "run_cypher", locked: true, description: "Raw Cypher." });
    return m;
  };
  api.renderConfigPage(cypherModel(false));
  let cypherRow = rowFor(card("__global__"), "run_cypher");
  check("run_cypher off: a locked line names DEVGRAPH_ENABLE_RUN_CYPHER and has no control",
    cypherRow && /DEVGRAPH_ENABLE_RUN_CYPHER=true/.test(cypherRow.textContent) && byClass(cypherRow, "cfg-lock").length === 1 &&
    find(cypherRow, e => e.tagName === "BUTTON" || e.tagName === "INPUT").length === 0 && byClass(cypherRow, "cfg-badge").length === 0,
    cypherRow && cypherRow.textContent);
  api.renderConfigPage(cypherModel(true));
  const onRows = byClass(card("__global__"), "tool-row").filter(r => byClass(r, "tool-name")[0].textContent === "run_cypher");
  check("run_cypher on: one built-in row with a warn badge and no off line",
    onRows.length === 1 && byClass(onRows[0], "cfg-badge").some(b => b.textContent === "Raw Cypher enabled" && b.classList.contains("warn")) &&
    !/DEVGRAPH_ENABLE_RUN_CYPHER/.test(onRows[0].textContent), JSON.stringify(onRows.map(r => r.textContent)));

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
