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
   informed click. */
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
  "configDestField", "configDest", "configYaml", "configModalConfirm", "configModalError", "configModalReload",
  "configModalCancel", "configModalSave", "pane-config"];
const els = Object.fromEntries(ids.map(id => [id, mkEl(id === "configYaml" ? "textarea" : id === "configDest" ? "select" : "div")]));
els["pane-config"].classList.add("active");
const document = { getElementById: id => els[id] || null, createElement: mkEl };

let tooltips = [];
let focused = null;
/* the page's clock (Date.now): a changed confirm button only arms after a pause */
let clock = 1e6;
/* when set, every fetch waits on it -- a request still in flight */
let gate = null;
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
    return { ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(body)) };
  },
};
const api = new Function(...Object.keys(globals),
  configSrc + "\nreturn { CONFIG_GLOBAL, renderConfigPage, renderConfigScope, configWriteRequest, describeConfigError," +
  " openConfigEditor, configEditTarget, loadConfigPage, applyConfigScope, configModalKey, CONFIG_SECTIONS," +
  " get model() { return configModel; }, get edit() { return configEdit; } };")(...Object.values(globals));

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
  await press(els.configModalSave);
  check("Save sends a dry run first, then the write",
    fetchCalls.length >= 2 && body(fetchCalls[0]).dry_run === true && body(fetchCalls[1]).dry_run === false, JSON.stringify(fetchCalls));
  check("...both PUT to the entry with the schema file's fingerprint",
    fetchCalls.slice(0, 2).every(c => c.url === "/api/config/repo-a/schema/node_types/Runbook" && c.init.method === "PUT" &&
      ifMatch(c) === '"sha256:repo-a-schema"' && body(c).yaml === "label: Runbook\nkey: [id]\n"), JSON.stringify(fetchCalls));
  check("on success the modal closes", !els.configModal.classList.contains("open"), els.configModal.className);
  check("...the scope block is re-rendered from the response", !!rowFor(card("repo-a"), "fresh_tool"), card("repo-a").textContent);
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
  check("the second click deletes for real", fetchCalls.length === 2 &&
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

  // 8d. the destination is locked while a dry run is out, and a write never
  //     goes anywhere the dry run did not check
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [],
    scope: url.includes("repo-a") ? project("repo-a") : project("repo-b") } });
  await buttons(rowFor(card("__global__"), "hot_paths"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  els.configDest.value = "repo-b";
  await els.configDest.fire("change");
  let releaseDest;
  gate = new Promise(r => { releaseDest = r; });
  clock += 1000;
  let pendingDest = els.configModalSave.fire("click");
  await Promise.resolve();
  check("the destination dropdown is disabled while the dry run is out", els.configDest.disabled === true, String(els.configDest.disabled));
  /* a change that lands anyway (a stale event, a script) */
  els.configDest.value = "repo-a";
  await els.configDest.fire("change");
  gate = null; releaseDest(); await pendingDest;
  check("switching destination mid-dry-run writes nowhere for real",
    writes().length === 1 && body(writes()[0]).dry_run === true && writes()[0].url === "/api/config/repo-b/tools", JSON.stringify(writes()));
  check("...leaves the editor open on Save, the dropdown enabled again",
    els.configModal.classList.contains("open") && els.configModalSave.textContent === "Save" && els.configDest.disabled === false,
    JSON.stringify([els.configModalSave.textContent, els.configDest.disabled]));
  check("...and says to check the new destination", /destination changed/i.test(els.configModalError.textContent) && shown(els.configModalError),
    els.configModalError.textContent);
  els.configModalCancel.fire("click");
  els.configDest.value = "__global__";

  // 8e. a cross-repo replace whose real write failed asks for the replace confirm again
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  const freshA = project("repo-a", MODEL().projects[0].tools.entries);
  freshA.tools.fingerprint = "sha256:repo-a-tools-2";
  respond = (url, init) => {
    if (init.method === undefined || init.method === "GET") return { status: 200, body: freshA };
    const b = JSON.parse(init.body);
    if (init.method === "POST" && yamlName(b.yaml) === "hot_paths")
      return { status: 409, body: { detail: { code: "exists", name: "hot_paths", message: "a tool named 'hot_paths' already exists in this scope" } } };
    if (!b.dry_run && ifMatch({ init }) === '"sha256:repo-a-tools"')
      return { status: 412, body: { detail: { code: "stale", message: "devgraph.tools.yaml changed on disk", scope: freshA } } };
    return { status: 200, body: { ok: true, written: !b.dry_run, warnings: [], notes: [], scope: freshA } };
  };
  await buttons(rowFor(card("__global__"), "hot_paths"), "Edit")[0].fire("click");
  await press(els.configModalSave);
  els.configDest.value = "repo-a";
  await els.configDest.fire("change");
  await press(els.configModalSave);
  await press(els.configModalSave);
  check("a confirmed replace that comes back 412 offers Reload", shown(els.configModalReload) &&
    writes().filter(c => body(c).dry_run === false).length === 1, JSON.stringify(writes()));
  await els.configModalReload.fire("click");
  fetchCalls = [];
  await press(els.configModalSave);
  check("after Reload, Save stops at the replace confirm again", els.configModalSave.textContent === "Save anyway" &&
    els.configModalConfirm.textContent.includes("Replaces repo-a's own hot_paths.") && writes().every(c => body(c).dry_run === true),
    JSON.stringify([els.configModalSave.textContent, writes()]));
  await press(els.configModalSave);
  check("...and the confirmed write PUTs repo-a's hot_paths with the reloaded fingerprint",
    lastCall().url === "/api/config" && writes().filter(c => body(c).dry_run === false).length === 1 &&
    writes().some(c => body(c).dry_run === false && c.init.method === "PUT" && c.url === "/api/config/repo-a/tools/hot_paths" &&
      ifMatch(c) === '"sha256:repo-a-tools-2"'), JSON.stringify(writes()));
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
  check("a deliberate click later still confirms", fetchCalls.length === 2 && fetchCalls[1].init.method === "DELETE" &&
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

  // 13c. the text is locked while it is checked, and a confirm covers only the text it checked
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = (url, init) => JSON.parse(init.body).dry_run
    ? { status: 200, body: { ok: true, written: false, warnings: ["Changing the key keeps the old constraint."], notes: [], scope: project("repo-a") } }
    : ok(project("repo-a"));
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  const reviewed = els.configYaml.value;
  gate = new Promise(r => { release = r; });
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("the YAML is read-only while its dry run is out", els.configYaml.readOnly === true, String(els.configYaml.readOnly));
  /* text that lands anyway (a stale input event, a script) */
  els.configYaml.value = "label: Runbook\nkey: [sneaky]\n";
  await els.configYaml.fire("input");
  gate = null; release(); await pending;
  check("...editable again once the check is back", els.configYaml.readOnly === false && els.configModalSave.textContent === "Save anyway",
    JSON.stringify([els.configYaml.readOnly, els.configModalSave.textContent]));
  await press(els.configModalSave);
  check("a confirm never writes text its dry run did not check: the click dry-runs the new text",
    writes().length === 2 && writes().every(c => body(c).dry_run === true) && body(writes()[0]).yaml === reviewed &&
    body(writes()[1]).yaml === "label: Runbook\nkey: [sneaky]\n" && els.configModalSave.textContent === "Save anyway",
    JSON.stringify([els.configModalSave.textContent, writes()]));
  await press(els.configModalSave);
  check("...and the next confirm writes exactly the text it reviewed",
    writes().length === 3 && body(writes()[2]).dry_run === false && body(writes()[2]).yaml === "label: Runbook\nkey: [sneaky]\n",
    JSON.stringify(writes()));

  // no warnings: the editor closes after writing, so nothing may be typed during the check
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: project("repo-a") } });
  await buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0].fire("click");
  gate = new Promise(r => { release = r; });
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("a warning-free save locks the YAML during its dry run too", els.configYaml.readOnly === true, String(els.configYaml.readOnly));
  els.configYaml.value = "label: Runbook\nkey: [typed]\n";
  await els.configYaml.fire("input");
  gate = null; release(); await pending;
  check("...and text that changed anyway is neither written unchecked nor thrown away",
    writes().length === 1 && body(writes()[0]).dry_run === true && els.configModal.classList.contains("open") &&
    els.configYaml.value === "label: Runbook\nkey: [typed]\n" && els.configYaml.readOnly === false && shown(els.configModalError),
    JSON.stringify([writes(), els.configModal.className, els.configModalError.textContent]));
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

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
