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
const docRoot = {};
const inTree = (root, e) => { for (; e; e = e.parentNode) if (e === root) return true; return false; };
const mkEl = tag => {
  const classes = new Set();
  const listeners = {};
  const attrs = {};
  const el = {
    tagName: tag.toUpperCase(), children: [], dataset: {}, style: { display: "" }, title: "", type: "",
    value: "", readOnly: false, parentNode: null, _text: "", _html: "",
    /* as in a browser: disabling the focused control (or its fieldset) drops focus to <body> */
    get disabled() { return el._dis === true; },
    set disabled(v) {
      el._dis = !!v;
      if (v && focused && (focused === el || (el.tagName === "FIELDSET" && inTree(el, focused)))) focused = null;
    },
    get isConnected() { let e = el; while (e.parentNode) e = e.parentNode; return e === docRoot || Object.values(els).includes(e); },
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
    replaceChildren(...cs) { el.children.forEach(c => { c.parentNode = null; }); el.children = []; el._text = ""; cs.forEach(c => el.appendChild(c)); },
    replaceWith(n) { const p = el.parentNode; p.children[p.children.indexOf(el)] = n; n.parentNode = p; el.parentNode = null; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    async fire(type, detail = 1) {
      const ev = { target: el, detail, preventDefault() {} };
      for (const fn of listeners[type] || []) await fn(ev);
      if (el["on" + type]) await el["on" + type](ev);
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return k in attrs ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
    /* as in a browser: a disabled control, or one inside a disabled fieldset, can't take focus */
    focus() {
      for (let e = el; e; e = e.parentNode) {
        if (e.disabled && (e === el || e.tagName === "FIELDSET")) return;
        /* ...and nothing inside a modal that isn't open yet */
        if ((e === els.configModal || e === els.configResetModal) && !e.classList.contains("open")) return;
      }
      focused = el;
    },
  };
  /* like a browser's: a select reads back only a value one of its options has */
  if (tag === "select") {
    let v = null;
    Object.defineProperty(el, "value", {
      get() { const opts = el.children; return v === null ? (opts[0] ? opts[0].value : "") : opts.some(o => o.value === v) ? v : ""; },
      set(x) { v = String(x); },
    });
  }
  allEls.push(el);
  return el;
};
const ids = ["configScopes", "configStatus", "configModal", "configModalTitle", "configModalWarn", "configModalWarnText",
  "configDestField", "configDestLabel", "configDest", "configYaml", "configModalConfirm", "configModalError", "configModalReload",
  "configModalCancel", "configModalSave", "pane-config",
  "configEditorSwitch", "configModeForm", "configModeYaml", "configFormNotice", "configFormNoticeText", "configFormDiscard", "configForm",
  "configFormHelp", "configFormScroll",
  "configResetModal", "configResetTitle", "configResetList", "configResetPhraseField", "configResetPhraseLabel", "configResetTyped",
  "configResetError", "configResetRecheck", "configResetCancel", "configResetConfirm"];
const tagFor = id => id === "configYaml" ? "textarea" : id === "configDest" ? "select" : id === "configResetTyped" ? "input" :
  id === "configForm" ? "fieldset" : /^configReset(Recheck|Cancel|Confirm)$|^configModal(Reload|Cancel|Save)$|^configMode|^configFormDiscard$/.test(id) ? "button" : "div";
const els = Object.fromEntries(ids.map(id => [id, mkEl(tagFor(id))]));
els["pane-config"].classList.add("active");
/* the modal's own controls sit inside it, in the page's order (the page's other ids don't) */
const nest = (parent, kids) => kids.forEach(k => { els[k].parentNode = els[parent]; els[parent].children.push(els[k]); });
nest("configDestField", ["configDestLabel", "configDest"]);
nest("configFormNotice", ["configFormNoticeText", "configFormDiscard"]);
nest("configFormScroll", ["configForm"]);
nest("configModalWarn", ["configModalWarnText"]);
nest("configModal", ["configModalTitle", "configModalWarn", "configDestField", "configEditorSwitch", "configFormHelp", "configFormNotice",
  "configFormScroll", "configYaml", "configModalConfirm", "configModalError", "configModalReload", "configModalCancel", "configModalSave"]);
nest("configEditorSwitch", ["configModeForm", "configModeYaml"]);
nest("configResetPhraseField", ["configResetPhraseLabel", "configResetTyped"]);
nest("configResetModal", ["configResetTitle", "configResetList", "configResetPhraseField", "configResetError", "configResetRecheck", "configResetCancel", "configResetConfirm"]);
const document = { getElementById: id => els[id] || null, createElement: mkEl, get activeElement() { return focused; } };

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
  " configResetRequest, configResetPhrase, configResetReady, describeConfigReset, configToggleRequest, configRevokeTrustRequest," +
  " CONFIG_FORM_FIELDS, configFormFromEntry, configEntryFromForm, configYamlScalar, configEntryYaml, configFormHints, configFormSwitch, configFormContext," +
  " get model() { return configModel; }, get formEls() { return configFormEls; }, get edit() { return configEdit; }, get reset() { return configReset; } };")(...Object.values(globals));

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
const press = async (el, detail = 1) => { clock += 1000; el.focus(); await el.fire("click", detail); };
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
  {
    const helpOf = label => {
      const lab = find(els.configForm, e => e.tagName === "LABEL" && e.textContent === label)[0];
      const ctl = lab && find(els.configForm, e => e.id === lab.htmlFor)[0];
      const id = ctl && (ctl.getAttribute("aria-describedby") || "").split(" ")[0];
      return (find(els.configForm, e => e.id === id)[0] || {}).textContent;
    };
    check("Add relationship opens the template in the form, From and To carrying its guidance as help text",
      shown(els.configForm) && !shown(els.configYaml) && els.configYaml.value === api.CONFIG_SECTIONS.relationships.template &&
      helpOf("From") === "One or more node labels, separated by commas. Each must be a node type declared in this file (or a built-in one when the file uses `extends: default`)." &&
      helpOf("To") === "One node label: a node type declared in this file (or a built-in one when the file uses `extends: default`).", JSON.stringify([els.configForm.style.display, helpOf("From"), helpOf("To")]));
  }
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
  // a Re-check that fails leaves focus on Cancel, not on the hidden Re-check (the body)
  {
    const staleFirst = (getRes, dryRes) => {
      let dry = 0;
      return (url, init) => {
        if (!init.method) return getRes();
        if (JSON.parse(init.body).dry_run) return ++dry === 1
          ? { status: 200, body: { ok: true, written: false, fingerprint: "sha256:repo-b-schema", removed: { node_types: ["Runbook"], relationships: [] }, warnings: [], notes: [], scope: project("repo-b") } }
          : dryRes();
        return { status: 412, body: { detail: { code: "stale", message: "changed" } } };
      };
    };
    for (const [what, getRes, dryRes] of [
      ["the scope can't be fetched (network error)", () => { throw new Error("offline"); }, null],
      ["its dry run is a 412 again", () => ({ status: 200, body: freshB }), () => ({ status: 412, body: { detail: { code: "stale", message: "changed" } } })],
    ]) {
      api.renderConfigPage(MODEL());
      respond = staleFirst(getRes, dryRes);
      await buttons(card("repo-b"), "Reset devgraph.schema.yaml…")[0].fire("click");
      els.configResetTyped.value = "repo-b";
      await els.configResetTyped.fire("input");
      await press(els.configResetConfirm);
      await press(els.configResetRecheck);
      await new Promise(r => setTimeout(r, 0));
      check("a Re-check that fails because " + what + " moves focus to the dialog's Cancel",
        api.reset !== null && shown(els.configResetError) && focused === els.configResetCancel, focused && focused.id);
      api.configModalKey({ key: "Escape" });
    }
  }

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
    a.tools.trust = { state: "trusted", command: "devgraph config tools trust repo-a", revocable: true };
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
  {
    /* repo-b has its own mine (not overriding anything yet): it will override the copy there */
    const m = COPY_MODEL();
    m.projects[1].tools.entries = [{ name: "mine", tool_id: "repo-b_mine", yaml: "name: mine\n", origin: "project", badges: [] }];
    api.renderConfigPage(m);
  }
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: globalBlock() } });
  await copyBtn("repo-a", "mine")[0].fire("click");
  check("a tool's destinations include the global store", JSON.stringify(els.configDest.children.map(o => [o.value, o.textContent])) ===
    JSON.stringify([["repo-b", "repo-b"], [HOSTILE, HOSTILE], ["__global__", "Global store"]]),
    JSON.stringify(els.configDest.children.map(o => o.value)));
  els.configDest.value = "__global__";
  await els.configDest.fire("change");
  check("...the global store's warning says it adds the tool, the source overrides it, and where else it will be overridden",
    els.configModalWarnText.textContent === "Adds mine to the global store; repo-a's own mine will override it in repo-a. " +
      "Will also be overridden in: repo-b.", els.configModalWarnText.textContent);
  await press(els.configModalSave);
  check("...and the copy POSTs /api/config/__global__/tools with the global fingerprint",
    writes().length === 2 && writes().every(c => c.url === "/api/config/__global__/tools" && c.init.method === "POST" && ifMatch(c) === '"sha256:g1"' &&
      body(c).yaml === "name: mine\ndescription: d\ncypher: x\n"), JSON.stringify(writes()));
  check("...then reloads the page", lastCall().url === "/api/config", lastCall().url);
  for (const state of ["untrusted", "changed"]) {
    const m = COPY_MODEL();
    m.projects[0].tools.trust = { state, command: "devgraph config tools trust repo-a", revocable: state === "changed" };
    api.renderConfigPage(m);
    await copyBtn("repo-a", "mine")[0].fire("click");
    els.configDest.value = "__global__";
    await els.configDest.fire("change");
    const text = els.configModalWarnText.textContent;
    check("copying an untrusted (" + state + ") repo's tool to the global store warns it is served in every repo without trust",
      text.includes("repo-a's devgraph.tools.yaml is not trusted") &&
      text.includes("the global store serves this tool in every repository without any trust approval") &&
      text.includes("can read the whole graph") && text.includes("convention, not a sandbox"), text);
    els.configModalCancel.fire("click");
  }

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

  // 33b. a copy's destination is locked while its dry run is out; one that changes anyway is never written
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  respond = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: project("repo-b") } });
  gate = new Promise(r => { release = r; });
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("the Copy to list is disabled during the copy's dry run", els.configDest.disabled === true, String(els.configDest.disabled));
  els.configDest.value = HOSTILE;
  await els.configDest.fire("change");
  gate = null; release(); await pending;
  check("switching destination mid-dry-run copies nowhere",
    writes().length === 1 && body(writes()[0]).dry_run === true && writes()[0].url === "/api/config/repo-b/schema/node_types",
    JSON.stringify(writes()));
  check("...and keeps the dialog on Copy, asking to check the new destination",
    els.configModal.classList.contains("open") && els.configModalSave.textContent === "Copy" && els.configDest.disabled === false &&
    /destination changed.*copy again/i.test(els.configModalError.textContent), els.configModalError.textContent);
  els.configModalCancel.fire("click");

  // 33c. a confirmed replace-copy that fails 412: after Reload the next Copy stops at 'Copy anyway' again
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  const replB = project("repo-b"); replB.schema.fingerprint = "sha256:repo-b-schema-2";
  respond = (url, init) => {
    if (!init.method) return { status: 200, body: replB };
    const b = JSON.parse(init.body);
    if (init.method === "POST") return { status: 409, body: { detail: { code: "exists", name: "Runbook", message: "node type 'Runbook' already exists" } } };
    if (!b.dry_run && ifMatch({ init }) === '"sha256:repo-b-schema"') return { status: 412, body: { detail: { code: "stale", message: "changed on disk" } } };
    return { status: 200, body: { ok: true, written: !b.dry_run, warnings: [], notes: [], scope: replB } };
  };
  await copyBtn("repo-a", "Runbook")[0].fire("click");
  await press(els.configModalSave);
  await press(els.configModalSave);
  check("a confirmed replace-copy that 412s offers Reload", shown(els.configModalReload) &&
    writes().filter(c => body(c).dry_run === false).length === 1, JSON.stringify(writes()));
  await els.configModalReload.fire("click");
  fetchCalls = [];
  await press(els.configModalSave);
  check("after Reload, Copy stops at 'Copy anyway' with the Replaces line", els.configModalSave.textContent === "Copy anyway" &&
    els.configModalConfirm.textContent.includes("Replaces repo-b's own node type Runbook.") && writes().every(c => body(c).dry_run === true),
    JSON.stringify([els.configModalSave.textContent, writes()]));
  await press(els.configModalSave);
  check("...then the confirmed copy PUTs Runbook with the reloaded fingerprint",
    writes().filter(c => body(c).dry_run === false).length === 1 && writes().some(c => body(c).dry_run === false && c.init.method === "PUT" &&
      c.url === "/api/config/repo-b/schema/node_types/Runbook" && ifMatch(c) === '"sha256:repo-b-schema-2"'), JSON.stringify(writes()));

  // 33d. a tool copied to the global store where the name exists: confirm, then PUT the global entry
  api.renderConfigPage(COPY_MODEL());
  fetchCalls = [];
  respond = (url, init) => {
    const b = JSON.parse(init.body);
    if (init.method === "POST" && url === "/api/config/__global__/tools")
      return { status: 409, body: { detail: { code: "exists", name: "mine", message: "a tool named 'mine' already exists in this scope" } } };
    return { status: 200, body: { ok: true, written: !b.dry_run, warnings: [], notes: [], scope: globalBlock() } };
  };
  await copyBtn("repo-a", "mine")[0].fire("click");
  els.configDest.value = "__global__";
  await els.configDest.fire("change");
  await press(els.configModalSave);
  check("a tool already in the global store asks 'Copy anyway', naming the global store's own tool",
    els.configModalSave.textContent === "Copy anyway" &&
    els.configModalConfirm.textContent.includes("Replaces the global store's own tool mine.") &&
    els.configModalWarnText.textContent === "Replaces the global tool mine with repo-a's version: served in every repo without its own mine; " +
      "repo-a's own copy keeps overriding it there." &&
    writes().length === 2 && writes()[1].init.method === "PUT" && writes()[1].url === "/api/config/__global__/tools/mine" &&
    writes().every(c => body(c).dry_run === true), JSON.stringify([els.configModalConfirm.textContent, writes()]));
  { const newer = globalBlock(); newer.tools.fingerprint = "sha256:g-newer"; api.applyConfigScope("__global__", newer); }
  await press(els.configModalSave);
  check("...then PUTs /api/config/__global__/tools/mine with the dry run's fingerprint",
    writes().length === 3 && writes()[2].init.method === "PUT" && writes()[2].url === "/api/config/__global__/tools/mine" &&
    body(writes()[2]).dry_run === false && ifMatch(writes()[2]) === '"sha256:g1"', JSON.stringify(writes()));

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
  api.renderConfigPage(cypherModel(false));
  cypherRow = rowFor(card("__global__"), "run_cypher");
  check("run_cypher's state is the dashboard process's environment, not every MCP session's",
    /off for MCP sessions started with this environment/.test(cypherRow.textContent), cypherRow.textContent);

  // 36. the form shows only entries it can carry exactly
  const j = v => JSON.stringify(v);
  const CY = "MATCH (n {repo_id: $repo_id})\nRETURN n\n";
  const TOOL_OK = () => ({ name: "hot_paths", description: "d", cypher: CY, parameters: [
    { name: "s" }, { name: "i", type: "integer", required: false, default: 3 },
    { name: "f", type: "float", required: false, default: 0.5, description: "Rows." },
    { name: "b", type: "boolean", required: false, default: true, description: null }], max_rows: 200, timeout_s: 5 });
  const NODE_OK = () => ({ label: "Runbook", key: ["slug", "team"], description: null, color: "#1f77b4", metadata: [
    { name: "slug", type: "string", required: true }, { name: "owner", description: "Who" }, { name: "team", type: "integer" }] });
  const okEntries = [["tools", TOOL_OK()], ["tools", { name: "x" }], ["tools", { description: "only" }],
    ["node_types", NODE_OK()], ["node_types", { ...NODE_OK(), source: null }],
    ["node_types", { label: "Doc", key: ["path"], source: { provider: "filesystem", kind: "file" }, metadata: [{ name: "path" }] }],
    ["node_types", { label: "Dir", source: { kind: "folder", provider: "filesystem" }, key: ["path"], metadata: [{ name: "path" }] }],
    ["node_types", api.CONFIG_SECTIONS.node_types.entry], ["tools", api.CONFIG_SECTIONS.tools.entry],
    /* textareas carry line feeds */
    ["tools", { name: "x", description: "two\nlines", cypher: "a\nb\n" }], ["node_types", { label: "X", description: "two\nlines" }]];
  okEntries.forEach(([section, e], i) => {
    const rep = api.configFormFromEntry(section, e);
    check("the form can show representable " + section + " entry #" + i, rep.ok && rep.form, j(rep));
  });
  const FIELD = p => "This entry has a field the form doesn't edit: `" + p + "`. Edit it as YAML.";
  const ORDER = "This entry's key order differs from its metadata order; the form can't show that. Edit it as YAML.";
  const VALUE = "This entry contains a value the page can't carry exactly (for example a date or a very large number). Edit it as YAML.";
  const refusals = [
    ["an unknown tool key", "tools", { ...TOOL_OK(), version: 1 }, FIELD("version")],
    ["an unknown parameter key", "tools", { ...TOOL_OK(), parameters: [{ name: "x", kind: "y" }] }, FIELD("parameters.0.kind")],
    ["a parameter type outside the enum", "tools", { ...TOOL_OK(), parameters: [{ name: "x" }, { name: "y", type: "text" }] }, FIELD("parameters.1.type")],
    ["a non-boolean required", "tools", { ...TOOL_OK(), parameters: [{ name: "x", required: "yes" }] }, FIELD("parameters.0.required")],
    ["a list default", "tools", { ...TOOL_OK(), parameters: [{ name: "x", default: [1] }] }, FIELD("parameters.0.default")],
    ["a boolean max_rows", "tools", { ...TOOL_OK(), max_rows: true }, FIELD("max_rows")],
    ["a fractional timeout", "tools", { ...TOOL_OK(), timeout_s: 1.5 }, FIELD("timeout_s")],
    ["a null description", "tools", { ...TOOL_OK(), description: null }, FIELD("description")],
    ["parameters that are not a list", "tools", { ...TOOL_OK(), parameters: { name: "x" } }, FIELD("parameters")],
    ["an entry JSON couldn't carry", "tools", null, VALUE],
    ["an unknown node type key", "node_types", { ...NODE_OK(), extends: "x" }, FIELD("extends")],
    ["a key order differing from metadata order", "node_types", { ...NODE_OK(), key: ["team", "slug"] }, ORDER],
    ["a repeated key component", "node_types", { ...NODE_OK(), key: ["slug", "slug"] }, ORDER],
    ["a key naming no metadata row", "node_types", { ...NODE_OK(), key: ["slug", "nope"] },
      "This entry's key names `nope`, which is not one of its metadata fields; the form can't show that. Edit it as YAML."],
    ["a string key", "node_types", { ...NODE_OK(), key: "slug" }, FIELD("key")],
    ["a source with an extra key", "node_types", { ...NODE_OK(), source: { provider: "filesystem", kind: "file", glob: "*" } }, FIELD("source.glob")],
    ["a source with another provider", "node_types", { ...NODE_OK(), source: { provider: "git", kind: "file" } }, FIELD("source.provider")],
    ["a source with no kind", "node_types", { ...NODE_OK(), source: { provider: "filesystem" } }, FIELD("source.kind")],
    ["an unknown metadata key", "node_types", { ...NODE_OK(), metadata: [{ name: "slug", unique: true }] }, FIELD("metadata.0.unique")],
    ["a metadata type outside the enum", "node_types", { ...NODE_OK(), metadata: [{ name: "slug", type: "date" }] }, FIELD("metadata.0.type")],
    ["a metadata field declared twice", "node_types", { label: "X", key: ["a"], metadata: [{ name: "a" }, { name: "a" }] },
      "This entry declares metadata field `a` more than once; the form can't show that. Edit it as YAML."],
    /* a single-line input can't hold a line break; a textarea turns CR / CRLF into LF */
    ["a line feed in a tool name", "tools", { ...TOOL_OK(), name: "a\nb" }, FIELD("name")],
    ["a CR in a parameter name", "tools", { ...TOOL_OK(), parameters: [{ name: "a\rb" }] }, FIELD("parameters.0.name")],
    ["a line separator in a parameter default", "tools", { ...TOOL_OK(), parameters: [{ name: "x", default: "a\u2028b" }] }, FIELD("parameters.0.default")],
    ["a NEL in a parameter description", "tools", { ...TOOL_OK(), parameters: [{ name: "x", description: "a\x85b" }] }, FIELD("parameters.0.description")],
    ["a paragraph separator in a node label", "node_types", { ...NODE_OK(), label: "a\u2029b" }, FIELD("label")],
    ["a line feed in a colour", "node_types", { ...NODE_OK(), color: "#1f77b4\n" }, FIELD("color")],
    ["a line feed in a metadata name", "node_types", { label: "X", metadata: [{ name: "a\nb" }] }, FIELD("metadata.0.name")],
    ["a line feed in a metadata description", "node_types", { ...NODE_OK(), metadata: [{ name: "slug", description: "a\nb" }], key: ["slug"] }, FIELD("metadata.0.description")],
    ["a CRLF in a tool description", "tools", { ...TOOL_OK(), description: "a\r\nb" }, FIELD("description")],
    ["a CR in Cypher", "tools", { ...TOOL_OK(), cypher: "MATCH (n)\rRETURN n" }, FIELD("cypher")],
    ["a CR in a node description", "node_types", { ...NODE_OK(), description: "a\rb" }, FIELD("description")],
  ];
  refusals.forEach(([what, section, e, reason]) => {
    const rep = api.configFormFromEntry(section, e);
    check("the form refuses " + what + ", saying why", !rep.ok && rep.reason === reason, j(rep));
  });
  api.CONFIG_FORM_FIELDS.tool.push("version");
  const yamlOnly = api.configFormFromEntry("tools", { ...TOOL_OK(), version: 1 });
  const plainOk = api.configFormFromEntry("tools", TOOL_OK()).ok;
  api.CONFIG_FORM_FIELDS.tool.pop();
  check("a model field the form lists but has no check for opens as YAML, naming the field (no TypeError)",
    !yamlOnly.ok && yamlOnly.reason === FIELD("version") && plainOk, j(yamlOnly));

  // 37. form state -> mapping
  const back = (section, e) => api.configEntryFromForm(section, api.configFormFromEntry(section, e).form, Object.keys(e));
  okEntries.concat([["tools", { cypher: CY, name: "n", description: "", parameters: [], max_rows: 100 }],
    ["tools", { name: "n", parameters: [{ name: "p", type: "string", required: true, default: null, description: "" }] }],
    ["tools", { name: "n", parameters: [{ name: "p", type: "integer", default: "12" }, { name: "q", type: "boolean", default: "yes" },
      { name: "r", type: "float", default: 1e21 }, { name: "s", default: "" }] }],
    ["node_types", { label: "N", key: [], metadata: [], description: "", color: null, source: null }],
    ["node_types", { metadata: [{ description: null, required: false, name: "a", type: "string" }], key: ["a"], label: "N" }]])
    .forEach(([section, e], i) => {
      check("mapping -> form -> mapping is the identity, key order included (" + section + " #" + i + ")", j(back(section, e)) === j(e), j(back(section, e)));
    });
  const toolForm = e => api.configFormFromEntry("tools", e).form;
  let tf = toolForm(api.CONFIG_SECTIONS.tools.entry);
  tf.max_rows = "250"; tf.timeout_s = "abc"; tf.description = "";
  tf.parameters.push({ name: "i", type: "integer", required: false, default: "12", description: "" },
    { name: "f", type: "float", required: false, default: "2.5", description: "" },
    { name: "g", type: "float", required: false, default: "0x10", description: "" },
    { name: "h", type: "integer", required: false, default: "1.5", description: "" },
    { name: "b", type: "boolean", required: false, default: "false", description: "" },
    { name: "s", type: "string", required: false, default: "12", description: "" },
    { name: "r", type: "string", required: true, default: "", description: "Plain." });
  let m = api.configEntryFromForm("tools", tf, ["name", "description", "cypher"]);
  check("integer text becomes a number, other text stays the string typed",
    m.max_rows === 250 && m.timeout_s === "abc", j(m));
  check("typed defaults: integer and float text become numbers, a boolean select a boolean, the rest stays text",
    j(m.parameters.map(p => p.default)) === j([12, 2.5, "0x10", "1.5", false, "12", undefined]), j(m.parameters));
  check("an emptied description is omitted", !("description" in m), j(m));
  check("model defaults are not written into new rows (type string, required true)",
    j(m.parameters[6]) === j({ name: "r", description: "Plain." }), j(m.parameters[6]));
  check("a new row's keys come in model order", j(Object.keys(m.parameters[0])) === j(["name", "type", "required", "default"]),
    j(m.parameters[0]));
  check("new keys follow the opened ones in model order",
    j(Object.keys(m)) === j(["name", "cypher", "parameters", "max_rows", "timeout_s"]), j(Object.keys(m)));
  tf = toolForm({ cypher: CY, name: "n", description: "d", parameters: [{ required: true, type: "string", name: "p" }] });
  tf.max_rows = "5";
  m = api.configEntryFromForm("tools", tf, ["cypher", "name", "description", "parameters"]);
  check("a hand-ordered entry keeps its order; a new key goes last",
    j(Object.keys(m)) === j(["cypher", "name", "description", "parameters", "max_rows"]), j(m));
  check("defaults the opened entry spelled out are kept", j(m.parameters[0]) === j({ required: true, type: "string", name: "p" }), j(m));
  tf.parameters[0].required = false; tf.parameters[0].type = "integer"; tf.parameters[0].default = "3";
  m = api.configEntryFromForm("tools", tf, ["cypher", "name", "description", "parameters"]);
  check("a changed row keeps its own key order", j(m.parameters[0]) === j({ required: false, type: "integer", name: "p", default: 3 }), j(m));
  tf = toolForm({ name: "n", parameters: [{ name: "p", type: "string", required: false, default: "5" }] });
  tf.parameters[0].type = "integer";
  m = api.configEntryFromForm("tools", tf, ["name", "parameters"]);
  check("a default re-reads as its new type when the type changes", m.parameters[0].default === 5, j(m));
  tf = toolForm({ name: "n", description: "d", max_rows: 7 });
  tf.description = ""; tf.max_rows = "";
  m = api.configEntryFromForm("tools", tf, ["name", "description", "max_rows"]);
  check("clearing optional fields removes them", j(m) === j({ name: "n" }), j(m));
  tf = toolForm({ name: "n", parameters: [{ name: "p" }] });
  tf.parameters = [];
  m = api.configEntryFromForm("tools", tf, ["name", "parameters"]);
  check("an emptied list the entry had stays as an empty list", j(m) === j({ name: "n", parameters: [] }), j(m));
  const nodeForm = e => api.configFormFromEntry("node_types", e).form;
  let nf = nodeForm(api.CONFIG_SECTIONS.node_types.entry);
  check("key components are ticks on metadata rows", nf.metadata[0].key === true, j(nf));
  nf.metadata.push({ name: "path", type: "string", required: false, key: true, description: "" },
    { name: "owner", type: "string", required: true, key: false, description: "" });
  nf.description = "Runbooks."; nf.color = "#00ff00"; nf.source = "folder";
  m = api.configEntryFromForm("node_types", nf, ["label", "key", "metadata"]);
  check("the key is the ticked rows' names in row order", j(m.key) === j(["slug", "path"]), j(m));
  check("a new metadata row omits model defaults", j(m.metadata.slice(1)) === j([{ name: "path" }, { name: "owner", required: true }]), j(m));
  check("a filesystem source is {provider, kind}", j(m.source) === j({ provider: "filesystem", kind: "folder" }), j(m));
  check("node type keys: opened order, then model order",
    j(Object.keys(m)) === j(["label", "key", "metadata", "description", "color", "source"]), j(Object.keys(m)));
  nf = nodeForm({ label: "N", key: ["a"], metadata: [{ name: "a" }], source: { provider: "filesystem", kind: "file" }, color: "#000000" });
  nf.source = ""; nf.color = "";
  m = api.configEntryFromForm("node_types", nf, ["label", "key", "metadata", "source", "color"]);
  check("choosing no source and clearing the colour removes them", j(m) === j({ label: "N", key: ["a"], metadata: [{ name: "a" }] }), j(m));

  // 38. the serialiser
  const sc = v => api.configYamlScalar(v);
  check("identifiers are plain", sc("hot_paths") === "hot_paths" && sc("Runbook") === "Runbook", sc("hot_paths"));
  [["yes", '"yes"'], ["No", '"No"'], ["null", '"null"'], ["Y", '"Y"'], ["off", '"off"'], ["#1f77b4", '"#1f77b4"'], ["1e3", '"1e3"'],
    ["123", '"123"'], ["a: b", '"a: b"'], [" lead", '" lead"'], ["", '""'], ["2026-10-05", '"2026-10-05"'],
    ["two words", '"two words"'], ["say \"hi\"", '"say \\"hi\\""'],
    ["a\u2028b", '"a\\u2028b"'], ["a\u2029b\x85c", '"a\\u2029b\\u0085c"'], ["del\x7f", '"del\\u007f"'],
    ["a\r\nb\r\n", '"a\\r\\nb\\r\\n"'], ["lone\ud800\nx\n", '"lone\\ud800\\nx\\n"'], ["lo\nne\udc00", '"lo\\nne\\udc00"'], ["x\n\n", '"x\\n\\n"'], ["  lead\nx", '"  lead\\nx"'], ["\n", '"\\n"'],
    [true, "true"], [false, "false"], [12, "12"], [-3, "-3"], [0.5, "0.5"], [1e-7, "1.0e-7"], [1e21, "1.0e+21"], [null, "null"]]
    .forEach(([v, want]) => check("scalar " + j(v) + " -> " + want, sc(v) === want, sc(v)));
  check("text ending in one line break is a | block", api.configYamlScalar("A\nB\n", "  ") === "|\n  A\n  B", j(api.configYamlScalar("A\nB\n", "  ")));
  check("a surrogate pair stays in a block", api.configYamlScalar("a \ud83d\ude42\nb\n", "  ") === "|\n  a \ud83d\ude42\n  b",
    j(api.configYamlScalar("a \ud83d\ude42\nb\n", "  ")));
  check("text ending in none is a |- block", api.configYamlScalar("A\n\nB", "    ") === "|-\n    A\n\n    B", j(api.configYamlScalar("A\n\nB", "    ")));
  check("the node type template serialises to its own text",
    api.configEntryYaml("node_types", api.CONFIG_SECTIONS.node_types.entry, ["label", "key", "metadata"]) === api.CONFIG_SECTIONS.node_types.template,
    j(api.configEntryYaml("node_types", api.CONFIG_SECTIONS.node_types.entry, ["label", "key", "metadata"])));
  const toolYaml = api.configEntryYaml("tools", TOOL_OK(), Object.keys(TOOL_OK()));
  check("a tool serialises as block YAML with a | Cypher block and a parameter sequence", toolYaml ===
    "name: hot_paths\ndescription: d\ncypher: |\n  MATCH (n {repo_id: $repo_id})\n  RETURN n\nparameters:\n  - name: s\n" +
    "  - name: i\n    type: integer\n    required: false\n    default: 3\n" +
    "  - name: f\n    type: float\n    required: false\n    default: 0.5\n    description: \"Rows.\"\n" +
    "  - name: b\n    type: boolean\n    required: false\n    default: true\n    description: null\nmax_rows: 200\ntimeout_s: 5\n", toolYaml);
  check("keys follow the given order, then model order",
    api.configEntryYaml("tools", { cypher: "c", name: "nm", max_rows: 1, description: "d" }, ["cypher"]) ===
    "cypher: c\nname: nm\ndescription: d\nmax_rows: 1\n", api.configEntryYaml("tools", { cypher: "c", name: "nm", max_rows: 1, description: "d" }, ["cypher"]));
  check("key is a flow list, quoting what needs it; source a nested mapping; empty lists are []",
    api.configEntryYaml("node_types", { label: "Nd", key: ["path", "a b"], source: { provider: "filesystem", kind: "file" }, metadata: [] }, []) ===
    'label: Nd\nkey: [path, "a b"]\nsource:\n  provider: filesystem\n  kind: file\nmetadata: []\n',
    api.configEntryYaml("node_types", { label: "Nd", key: ["path", "a b"], source: { provider: "filesystem", kind: "file" }, metadata: [] }, []));
  const nested = api.configEntryYaml("node_types", { label: "Nd", metadata: [{ name: "a", description: "two\nlines\n" }] }, []);
  check("a block inside a sequence item is indented under its key", nested ===
    "label: Nd\nmetadata:\n  - name: a\n    description: |\n      two\n      lines\n", nested);

  // 39. advisory hints
  const hints = (section, e, edit) => {
    const f = api.configFormFromEntry(section, e).form;
    if (edit) edit(f);
    return api.configFormHints(section, f);
  };
  const VALID_TOOL = { name: "hot_paths", description: "d", cypher: "MATCH (n {repo_id: $repo_id}) WHERE n.x = $p RETURN n",
    parameters: [{ name: "p", type: "integer", required: false, default: 3 }], max_rows: 10, timeout_s: 60 };
  check("no hints for a valid tool", j(hints("tools", VALID_TOOL)) === "[]", j(hints("tools", VALID_TOOL)));
  const VALID_NODE = { label: "Doc", key: ["path"], color: "#A0b0C0", source: { provider: "filesystem", kind: "file" },
    metadata: [{ name: "path", type: "string" }, { name: "title" }] };
  check("no hints for a valid node type", j(hints("node_types", VALID_NODE)) === "[]", j(hints("node_types", VALID_NODE)));
  const hintCases = [
    ["tools", "an empty tool name", f => { f.name = ""; }, "name", /required/],
    ["tools", "a tool name off the pattern", f => { f.name = "Hot"; }, "name", /lowercase/],
    ["tools", "a blank description", f => { f.description = "  "; }, "description", /agent reads/],
    ["tools", "a description over the limit", f => { f.description = "x".repeat(1025); }, "description", /1025 characters.*1024/],
    ["tools", "a blank cypher", f => { f.cypher = " \n"; }, "cypher", /required/],
    ["tools", "cypher without $repo_id", f => { f.cypher = "MATCH (n) WHERE n.x = $p RETURN n"; }, "cypher", /doesn't appear to filter on `\$repo_id`/],
    ["tools", "duplicate parameter names", f => { f.parameters.push({ ...f.parameters[0], open: {} }); }, "parameters.1.name", /Another parameter is also named p/],
    ["tools", "an empty parameter name", f => { f.parameters[0].name = ""; }, "parameters.0.name", /required/],
    ["tools", "a default on a required parameter", f => { f.parameters[0].required = true; }, "parameters.0.default", /optional/],
    ["tools", "a default not reading as its type", f => { f.parameters[0].default = "3.5"; }, "parameters.0.default", /doesn't read as an integer/],
    ["tools", "a float default not reading as a float", f => { f.parameters[0].type = "float"; f.parameters[0].default = "x"; }, "parameters.0.default", /doesn't read as a float/],
    ["tools", "max_rows out of range", f => { f.max_rows = "1001"; }, "max_rows", /1 to 1000/],
    ["tools", "timeout_s not an integer", f => { f.timeout_s = "2.5"; }, "timeout_s", /1 to 60/],
    ["node_types", "an empty label", f => { f.label = ""; }, "label", /required/],
    ["node_types", "a label off the pattern", f => { f.label = "1Doc"; }, "label", /letter/],
    ["node_types", "no key ticked", f => { f.source = ""; f.metadata[0].key = false; }, "key", /Tick Key/],
    ["node_types", "a metadata name off the pattern", f => { f.metadata[1].name = "Title"; }, "metadata.1.name", /lowercase/],
    ["node_types", "duplicate metadata names", f => { f.metadata[1].name = "path"; }, "metadata.1.name", /Another field is also named path/],
    ["node_types", "a colour that isn't #rrggbb", f => { f.color = "red"; }, "color", /#rrggbb/],
    ["node_types", "a filesystem source without a string path key", f => { f.metadata[0].type = "integer"; }, "source", /path/],
    ["node_types", "a filesystem source keyed on more than path", f => { f.metadata[1].key = true; }, "source", /path/],
  ];
  hintCases.forEach(([section, what, edit, field, re]) => {
    const got = hints(section, section === "tools" ? VALID_TOOL : VALID_NODE, edit);
    check("a hint for " + what, got.some(h => h.field === field && re.test(h.text)), j(got));
  });

  // 39b. relationships: which entries the form shows, form <-> mapping, YAML, hints, context
  const FROM_LIST = "This entry's `from` can't be shown as comma-separated labels exactly (a label with a comma, surrounding spaces or a non-text value). Edit it as YAML.";
  const CUSTOM_PROVIDER = "This entry has a `custom` block but its provider isn't custom; the form can't show that. Edit it as YAML.";
  const REL_OK = [
    { type: "CALLS", from: "Function", to: "Function" },
    { type: "DOCUMENTS", from: ["Runbook", "Service"], to: "Service", provider: "custom", custom: { name: "runbook_links" }, color: "#1f77b4" },
    { type: "DOCUMENTS", from: "Runbook", to: "Service", provider: "custom", custom: { name: "runbook_links", params: {} } },
    { type: "DOCUMENTS", from: "Runbook", to: "Service", provider: "custom", custom: { params: {}, name: "x" } },
    { type: "IS_CHILD_OF", from: ["File", "Folder"], to: "Folder", provider: "filesystem" },
    { type: "OWNS", from: ["Team"], to: "Service", provider: "builtin", color: null, custom: null },
    { to: "B", type: "T", from: "A", provider: "custom", custom: null },
    { type: "T", from: "", to: "" },
    api.CONFIG_SECTIONS.relationships.entry,
  ];
  REL_OK.forEach((e, i) => {
    const rep = api.configFormFromEntry("relationships", e);
    check("the form can show representable relationship #" + i, rep.ok && rep.form, j(rep));
  });
  check("the Add relationship template carries the entry its text parses to",
    j(api.CONFIG_SECTIONS.relationships.entry) === j({ type: "DOCUMENTS", provider: "custom", custom: { name: "runbook_links" }, from: "Runbook", to: "Service" }),
    j(api.CONFIG_SECTIONS.relationships.entry));
  const relForm = e => api.configFormFromEntry("relationships", e).form;
  let rf = relForm(REL_OK[1]);
  check("relationship form state: from joined with commas, provider and custom name as text",
    rf.type === "DOCUMENTS" && rf.from === "Runbook, Service" && rf.to === "Service" && rf.provider === "custom" &&
    rf.custom === "runbook_links" && rf.color === "#1f77b4" && rf.open === REL_OK[1], j(rf));
  rf = relForm(REL_OK[0]);
  check("a missing provider shows as builtin; missing fields as empty text",
    rf.provider === "builtin" && rf.custom === "" && rf.color === "" && rf.from === "Function", j(rf));
  const REL = { type: "T", from: "A", to: "B" };
  [["an unknown relationship key", { ...REL, extra: 1 }, FIELD("extra")],
    ["an unknown custom key", { ...REL, provider: "custom", custom: { name: "x", module: "m" } }, FIELD("custom.module")],
    ["non-empty custom params", { ...REL, provider: "custom", custom: { name: "x", params: { a: 1 } } }, FIELD("custom.params")],
    ["custom params that are not a mapping", { ...REL, provider: "custom", custom: { name: "x", params: [] } }, FIELD("custom.params")],
    ["a custom block that is not a mapping", { ...REL, provider: "custom", custom: "x" }, FIELD("custom")],
    ["a non-text custom name", { ...REL, provider: "custom", custom: { name: 3 } }, FIELD("custom.name")],
    ["a provider outside the enum", { ...REL, provider: "git" }, FIELD("provider")],
    ["a custom block under builtin", { ...REL, provider: "builtin", custom: { name: "x" } }, CUSTOM_PROVIDER],
    ["a custom block with no provider", { ...REL, custom: { name: "x" } }, CUSTOM_PROVIDER],
    ["a custom block under filesystem", { ...REL, provider: "filesystem", custom: {} }, CUSTOM_PROVIDER],
    ["a from item with a comma", { ...REL, from: ["A", "B,C"] }, FROM_LIST],
    ["a from string with a comma", { ...REL, from: "A,B" }, FROM_LIST],
    ["a from item with a trailing space", { ...REL, from: ["A", "B "] }, FROM_LIST],
    ["a from string with a leading space", { ...REL, from: " A" }, FROM_LIST],
    ["a from item that is a number", { ...REL, from: ["A", 3] }, FROM_LIST],
    ["a from that is a number", { ...REL, from: 3 }, FROM_LIST],
    ["a from that is null", { ...REL, from: null }, FROM_LIST],
    ["an empty from item", { ...REL, from: ["A", ""] }, FROM_LIST],
    ["an empty from list", { ...REL, from: [] }, FROM_LIST],
    ["a from item with a byte-order mark", { ...REL, from: ["﻿A"] }, FROM_LIST],
    ["a multi-line from", { ...REL, from: "A\nB" }, FROM_LIST],
    ["a multi-line type", { ...REL, type: "T\nU" }, FIELD("type")],
    ["a line separator in to", { ...REL, to: "B C" }, FIELD("to")],
    ["a list to", { ...REL, to: ["B"] }, FIELD("to")],
    ["a CR in a custom name", { ...REL, provider: "custom", custom: { name: "a\rb" } }, FIELD("custom.name")],
    ["a line feed in a colour", { ...REL, color: "#000000\n" }, FIELD("color")],
    ["an entry JSON couldn't carry", null, VALUE],
  ].forEach(([what, e, reason]) => {
    const rep = api.configFormFromEntry("relationships", e);
    check("the form refuses " + what + ", saying why", !rep.ok && rep.reason === reason, j(rep));
  });
  REL_OK.forEach((e, i) => {
    const got = back("relationships", e);
    check("relationship mapping -> form -> mapping is the identity, key order included (#" + i + ")", j(got) === j(e), j(got));
  });
  const relBack = (e, edit) => { const f = relForm(e); edit(f); return api.configEntryFromForm("relationships", f, Object.keys(e)); };
  const fromOf = (e, text) => relBack(e, f => { f.from = text; }).from;
  check("an untouched one-item from list stays a list", j(back("relationships", REL_OK[5]).from) === j(["Team"]), j(back("relationships", REL_OK[5])));
  check("editing a from string to two labels writes a list", j(fromOf(REL, "A, B")) === j(["A", "B"]), j(fromOf(REL, "A, B")));
  check("editing a from list down to one label keeps a list", j(fromOf(REL_OK[1], "Runbook")) === j(["Runbook"]), j(fromOf(REL_OK[1], "Runbook")));
  check("editing a from string to another label keeps a string", fromOf(REL, " C ") === "C", j(fromOf(REL, " C ")));
  check("a trailing comma or blank pieces add nothing", fromOf(REL, "A,") === "A" && j(fromOf(REL, "A, ,B,")) === j(["A", "B"]),
    j([fromOf(REL, "A,"), fromOf(REL, "A, ,B,")]));
  let rm = relBack(REL, f => { f.from = " , "; });
  check("an emptied from is omitted", !("from" in rm), j(rm));
  const fresh = (edit) => { const f = relForm(api.CONFIG_SECTIONS.relationships.entry); f.open = undefined; edit(f); return api.configEntryFromForm("relationships", f, []); };
  rm = fresh(f => { f.from = "Runbook"; f.provider = "builtin"; f.type = "CALLS"; });
  check("a new entry writes one from label as a string, in model order, without the default provider",
    j(rm) === j({ type: "CALLS", from: "Runbook", to: "Service" }), j(rm));
  rm = fresh(f => { f.from = "Runbook, Service"; });
  check("a new entry writes several from labels as a list, and a custom block for the custom provider",
    j(rm) === j({ type: "DOCUMENTS", from: ["Runbook", "Service"], to: "Service", provider: "custom", custom: { name: "runbook_links" } }), j(rm));
  rm = relBack({ type: "CALLS", provider: "builtin", from: "A", to: "B" }, f => { f.type = "USES"; });
  check("a provider the entry spelled out at its default is kept", j(rm) === j({ type: "USES", provider: "builtin", from: "A", to: "B" }), j(rm));
  rm = relBack(REL, f => { f.provider = "filesystem"; });
  check("a provider changed from the default is written in model order", j(rm) === j({ type: "T", from: "A", to: "B", provider: "filesystem" }), j(rm));
  rm = relBack(REL, f => { f.provider = "custom"; });
  check("switching to custom with no name writes no custom block", j(rm) === j({ type: "T", from: "A", to: "B", provider: "custom" }), j(rm));
  rm = relBack(REL, f => { f.provider = "custom"; f.custom = "my_links"; });
  check("a custom name is written as a nested block", j(rm) === j({ type: "T", from: "A", to: "B", provider: "custom", custom: { name: "my_links" } }), j(rm));
  rf = relForm(REL_OK[2]);
  rf.provider = "builtin";
  rm = api.configEntryFromForm("relationships", rf, Object.keys(REL_OK[2]));
  check("switching away from custom keeps the name in the form state but doesn't write it",
    rf.custom === "runbook_links" && j(rm) === j({ type: "DOCUMENTS", from: "Runbook", to: "Service", provider: "builtin" }), j([rf.custom, rm]));
  rf.provider = "custom";
  rm = api.configEntryFromForm("relationships", rf, Object.keys(REL_OK[2]));
  check("...and switching back restores it, params included", j(rm) === j(REL_OK[2]), j(rm));
  rf.custom = "";
  rm = api.configEntryFromForm("relationships", rf, Object.keys(REL_OK[2]));
  check("an emptied name in an opened custom block drops only the name", j(rm.custom) === j({ params: {} }), j(rm));
  rm = relBack(REL_OK[6], f => { f.type = "U"; });
  check("an opened custom: null is kept while the name stays empty", "custom" in rm && rm.custom === null, j(rm));
  rm = relBack(REL_OK[6], f => { f.custom = "named"; });
  check("...and becomes a block once a name is typed", j(rm.custom) === j({ name: "named" }), j(rm));
  rm = relBack(REL_OK[6], f => { f.custom = "named"; f.provider = "builtin"; });
  check("...but stays custom: null when the provider is then switched away (only a name under custom replaces it)",
    "custom" in rm && rm.custom === null && rm.provider === "builtin", j(rm));
  rm = relBack(REL_OK[1], f => { f.color = ""; f.to = "Doc"; });
  check("a cleared colour is omitted; an edited to is written", !("color" in rm) && rm.to === "Doc", j(rm));
  check("relationship YAML: from as a flow list, custom as a nested block, keys in model order",
    api.configEntryYaml("relationships", { color: "#000000", custom: { name: "x", params: {} }, provider: "custom", to: "B", from: ["A", "B"], type: "T" }, []) ===
    'type: T\nfrom: [A, B]\nto: B\nprovider: custom\ncustom:\n  name: x\n  params: {}\ncolor: "#000000"\n',
    api.configEntryYaml("relationships", { color: "#000000", custom: { name: "x", params: {} }, provider: "custom", to: "B", from: ["A", "B"], type: "T" }, []));
  check("from list items are quoted when YAML would read them otherwise",
    api.configEntryYaml("relationships", { type: "T", from: ["on", "null", "Y", "A"], to: "B" }, []) === 'type: T\nfrom: ["on", "null", "Y", A]\nto: B\n',
    api.configEntryYaml("relationships", { type: "T", from: ["on", "null", "Y", "A"], to: "B" }, []));
  check("relationship keys: opened order first, then model order",
    api.configEntryYaml("relationships", { to: "B", type: "T", from: "A", color: null }, ["to"]) === "to: B\ntype: T\nfrom: A\ncolor: null\n",
    api.configEntryYaml("relationships", { to: "B", type: "T", from: "A", color: null }, ["to"]));

  // 39c. relationship hints and the page-model context behind them
  const CTX_MODEL = () => {
    const m = MODEL();
    m.global.node_types = [{ label: "Function", locked: true }, { label: "Service", locked: true }];
    m.global.relationship_types = [{ type: "CALLS", locked: true }, { type: "USES", locked: true }];
    m.projects[0].schema.node_types = [
      { label: "Runbook", yaml: "", entry: { label: "Runbook" }, editable: true, badges: [] },
      { label: "File", yaml: "", entry: { label: "File", source: { provider: "filesystem", kind: "file" } }, editable: true, badges: [] },
      { label: "Folder", yaml: "", entry: { label: "Folder", source: { provider: "filesystem", kind: "folder" } }, editable: true, badges: [] },
      { label: "Dated", yaml: "", entry: null, editable: true, badges: [] }];
    return m;
  };
  let ctx = api.configFormContext(CTX_MODEL(), "repo-a");
  check("the context lists built-in labels then this file's, the built-in relationship types, and the filesystem labels",
    j(ctx) === j({ labels: ["Function", "Service", "Runbook", "File", "Folder", "Dated"], relationship_types: ["CALLS", "USES"],
      filesystem: { file: "File", folder: "Folder" } }), j(ctx));
  const noneModel = CTX_MODEL();
  noneModel.projects[0].schema.extends = "none";
  const ctxNone = api.configFormContext(noneModel, "repo-a");
  check("under extends: none the built-in labels are not known", j(ctxNone.labels) === j(["Runbook", "File", "Folder", "Dated"]), j(ctxNone));
  const ctxB = api.configFormContext(CTX_MODEL(), "repo-b");
  check("a file with no filesystem node types has none in the context",
    j(ctxB.filesystem) === j({ file: null, folder: null }) && j(ctxB.labels) === j(["Function", "Service", "Runbook"]), j(ctxB));
  const relHints = (e, edit, c = ctx) => { const f = relForm(e); if (edit) edit(f); return c === "none" ? api.configFormHints("relationships", f) : api.configFormHints("relationships", f, c); };
  [{ type: "CALLS", from: "Function", to: "Function" },
    { type: "DOCUMENTS", from: ["Runbook", "Service"], to: "Service", provider: "custom", custom: { name: "runbook_links" }, color: "#1f77b4" },
    { type: "IS_CHILD_OF", from: ["File", "Folder"], to: "Folder", provider: "filesystem" }].forEach((e, i) => {
    check("no hints for valid relationship #" + i, j(relHints(e)) === "[]", j(relHints(e)));
  });
  const R_OK = { type: "DOCUMENTS", from: "Runbook", to: "Service", provider: "custom", custom: { name: "runbook_links" } };
  const FS_OK = { type: "IS_CHILD_OF", from: ["File", "Folder"], to: "Folder", provider: "filesystem" };
  [["an empty type", R_OK, f => { f.type = ""; }, "type", /^A relationship type is uppercase letters, digits and underscores, starting with a letter \(at most 64 characters\)\.$/],
    ["a lowercase type", R_OK, f => { f.type = "documents"; }, "type", /uppercase letters/],
    ["a builtin provider with a new type", R_OK, f => { f.provider = "builtin"; }, "provider",
      /^The builtin provider reuses one of DevGraph's relationship types; pick Custom or Filesystem to declare a new one\.$/],
    ["a custom provider with a built-in type", R_OK, f => { f.type = "USES"; }, "provider", /^`USES` is built in: use the builtin provider to reuse it\.$/],
    ["a filesystem provider with a built-in type", FS_OK, f => { f.type = "CALLS"; }, "provider", /^`CALLS` is built in/],
    ["an empty custom name", R_OK, f => { f.custom = ""; }, "custom", /required/],
    ["a custom name off the pattern", R_OK, f => { f.custom = "Links"; }, "custom", /lowercase/],
    ["no from labels", R_OK, f => { f.from = " , "; }, "from", /at least one/],
    ["a from label off the pattern", R_OK, f => { f.from = "Runbook, 9x"; }, "from", /`9x`/],
    ["a from label listed twice", R_OK, f => { f.from = "Runbook, Service, Runbook"; }, "from", /`Runbook` is listed more than once/],
    ["an empty to", R_OK, f => { f.to = ""; }, "to", /required/],
    ["a to off the pattern", R_OK, f => { f.to = "a-b"; }, "to", /`a-b`/],
    ["an unknown from label", R_OK, f => { f.from = "Runbok"; }, "from",
      /^`Runbok` isn't a node type this repository has \(built-in, or declared in this file as last loaded\)\.$/],
    ["an unknown to label", R_OK, f => { f.to = "Servic"; }, "to", /`Servic` isn't a node type/],
    ["a filesystem to that isn't the folder type", FS_OK, f => { f.to = "File"; }, "to",
      /^A filesystem relationship points to the filesystem folder node type \(`Folder`\)\.$/],
    ["a filesystem from that isn't a filesystem type", FS_OK, f => { f.from = "File, Runbook"; }, "from", /^`Runbook` is not a filesystem node type\.$/],
    ["a colour that isn't #rrggbb", R_OK, f => { f.color = "red"; }, "color", /#rrggbb/],
  ].forEach(([what, e, edit, field, re]) => {
    const got = relHints(e, edit);
    check("a relationship hint for " + what, got.some(h => h.field === field && re.test(h.text)), j(got));
  });
  let got = relHints(FS_OK, null, ctxB);
  check("a filesystem relationship in a file with no folder type says so",
    got.some(h => h.field === "to" && h.text === "A filesystem relationship points to the filesystem folder node type, and this file declares none."), j(got));
  got = relHints(FS_OK, f => { f.from = "File, Folder, Runbook"; }, ctxB).filter(h => h.field === "from" && /filesystem/.test(h.text));
  check("...and its from labels get one hint saying the file declares no filesystem node types, not one per label",
    j(got.map(h => h.text)) === j(["A filesystem relationship starts from the filesystem node types (file or folder), and this file declares none."]), j(got));
  got = relHints(FS_OK, f => { f.from = "File, 9x"; });
  check("a from label that isn't a valid label gets no filesystem hint on top",
    got.some(h => /`9x` isn't a valid label/.test(h.text)) && !got.some(h => /`9x` is not a filesystem/.test(h.text)), j(got));
  got = relHints(R_OK, f => { f.from = "Function"; }, ctxNone);
  check("built-in labels are unknown under extends: none", got.some(h => h.field === "from" && /`Function` isn't a node type/.test(h.text)), j(got));
  got = relHints(R_OK, f => { f.from = "Runbok"; f.to = "Servic"; f.type = "USES"; }, "none");
  check("no label, provider-type or filesystem hints without a context", j(got) === "[]", j(got));
  got = relHints(R_OK, f => { f.provider = "builtin"; f.custom = ""; });
  check("the custom name is only hinted for the custom provider", !got.some(h => h.field === "custom"), j(got));
  check("tools and node types ignore the context",
    j(api.configFormHints("tools", api.configFormFromEntry("tools", VALID_TOOL).form, ctx)) === "[]" &&
    j(api.configFormHints("node_types", api.configFormFromEntry("node_types", VALID_NODE).form, ctx)) === "[]", "");

  // 40. switching back to the form
  const openText = "name: hot_paths\n";
  const swEdit = { section: "tools", openEntry: { name: "hot_paths" }, openText, formText: openText, formState: null };
  let s = api.configFormSwitch(swEdit, openText);
  check("the opening text can go back to the form, rebuilt from the opening entry", s.available && s.restore === "open" && !s.reason, j(s));
  swEdit.formState = toolForm({ name: "renamed" }); swEdit.formText = "name: renamed\n";
  s = api.configFormSwitch(swEdit, "name: renamed\n");
  check("the form's last text restores the form state", s.available && s.restore === "state", j(s));
  s = api.configFormSwitch(swEdit, "name: renamed\n# mine\n");
  check("hand-edited text can't go back to the form, saying why", !s.available && s.restore === null &&
    s.reason === "The YAML was edited by hand, and the form can only show text it produced. Keep editing as YAML, or discard the hand edits to return to the form.", j(s));
  s = api.configFormSwitch({ ...swEdit, openEntry: { name: "x", extra: 1 }, formState: null }, openText);
  check("an entry the form can't show never switches to it", !s.available && s.reason === FIELD("extra"), j(s));

  // 41. the form view in the editor (DOM): opening, editing, saving through the same dry run
  const TOOL_ENTRY = { name: "hot_paths", description: "Hot paths.", cypher: "MATCH (f:Function {repo_id: $repo_id})\nRETURN f.name AS name\n",
    parameters: [{ name: "limit", type: "integer", required: false, default: 10 }, { name: "flag", type: "boolean", required: false, default: "yes" }] };
  /* what the server dumped: deliberately not what the form's emitter would write */
  const TOOL_YAML = "name: hot_paths\ndescription: 'Hot paths.'\ncypher: |\n  MATCH (f:Function {repo_id: $repo_id})\n  RETURN f.name AS name\nparameters:\n- {name: limit, type: integer, required: false, default: 10}\n- {name: flag, type: boolean, required: false, default: 'yes'}\n";
  const REL_ENTRY = { type: "OWNS", from: ["Team"], to: "Service", provider: "custom", custom: { name: "owners", params: {} }, color: "#1f77b4" };
  /* what the server dumped: again not the emitter's text */
  const REL_YAML = "type: OWNS\nfrom:\n- Team\nto: Service\nprovider: custom\ncustom:\n  name: owners\n  params: {}\ncolor: '#1f77b4'\n";
  const RUNBOOK = { label: "Runbook", key: ["slug"], metadata: [{ name: "slug", type: "string", required: true }, { name: "owner", type: "string" }] };
  const FM = () => {
    const m = MODEL();
    m.global.tools.entries[0].entry = { name: "hot_paths", description: "d", cypher: "x" };
    const b = m.projects[1];
    b.tools.entries = [
      { name: "hot_paths", tool_id: "repo-b_hot_paths", yaml: TOOL_YAML, entry: TOOL_ENTRY, origin: "project", badges: [] },
      { name: "dated", tool_id: "repo-b_dated", yaml: "name: dated\nsince: 2024-01-01\n", entry: null, origin: "project", badges: [] },
      { name: "extra", tool_id: "repo-b_extra", yaml: "name: extra\nversion: 2\n", entry: { name: "extra", version: 2 }, origin: "project", badges: [] },
      { name: "hostile", tool_id: "repo-b_hostile", yaml: "name: hostile\n", entry: { name: HOSTILE, description: HOSTILE, cypher: HOSTILE }, origin: "project", badges: [] },
      { name: "multi", tool_id: "repo-b_multi", yaml: "name: multi\n", entry: { name: "multi", parameters: [{ name: "x", description: "line one\nline two" }] }, origin: "project", badges: [] },
    ];
    b.schema.node_types = [{ label: "Runbook", yaml: "label: Runbook\nkey: [slug]\n", entry: RUNBOOK, editable: true, badges: [] }];
    b.schema.relationships = [
      { type: "OWNS", yaml: REL_YAML, entry: REL_ENTRY, editable: true, badges: [] },
      { type: "PARAMS", yaml: "type: PARAMS\n", entry: { type: "PARAMS", from: "A", to: "B", provider: "custom", custom: { name: "x", params: { a: 1 } } },
        editable: true, badges: [] },
      { type: "HOSTILE_REL", yaml: "type: HOSTILE_REL\n", entry: { type: HOSTILE, from: HOSTILE, to: HOSTILE, provider: "custom", custom: { name: HOSTILE }, color: HOSTILE },
        editable: true, badges: [] }];
    return m;
  };
  const formCtl = (label, n = 0, root = els.configForm) => {
    const lab = find(root, e => e.tagName === "LABEL" && e.textContent === label)[n];
    return lab && find(els.configForm, e => e.id === lab.htmlFor)[0];
  };
  const typeIn = async (el, v) => { el.value = v; await el.fire("input"); };
  const group = title => find(els.configForm, e => e.tagName === "FIELDSET" && e.children[0] && e.children[0].textContent === title)[0];
  const rowsOf = title => group(title).children.filter(e => e.tagName === "FIELDSET");
  const legendOf = row => row.children[0].textContent;
  const removeOf = row => find(row, e => e.tagName === "BUTTON" && e.textContent === "Remove")[0];
  const editRow = async (scope, name, label = "Edit") => { await buttons(rowFor(card(scope), name), label)[0].fire("click"); };
  const warnDry = (url, init) => JSON.parse(init.body).dry_run
    ? { status: 200, body: { ok: true, written: false, warnings: ["Renaming keeps the old tool id."], notes: [], scope: FM().projects[1] } }
    : ok(FM().projects[1]);
  const quiet = (url, init) => ({ status: 200, body: { ok: true, written: !JSON.parse(init.body).dry_run, warnings: [], notes: [], scope: FM().projects[1] } });
  configPayload = FM();
  api.renderConfigPage(FM());
  fetchCalls = [];
  respond = quiet;
  await editRow("repo-b", "hot_paths");
  check("a tool with an entry opens in the form", shown(els.configForm) && !shown(els.configYaml) && shown(els.configEditorSwitch) &&
    els.configModeForm.getAttribute("aria-pressed") === "true" && els.configModeYaml.getAttribute("aria-pressed") === "false" &&
    els.configModeForm.getAttribute("aria-disabled") === "false" && !shown(els.configFormNotice),
    JSON.stringify([els.configForm.style.display, els.configYaml.style.display]));
  check("...the switch is a group labelled Editor; the form is a fieldset with no <form> (Enter never submits)",
    /id="configEditorSwitch" role="group" aria-labelledby="configEditorLabel"[^>]*><span id="configEditorLabel">Editor<\/span>/.test(html) &&
    /<fieldset class="cfg-form" id="configForm"/.test(html) && !/<form[\s>]/.test(html.slice(html.indexOf('id="configModal"'), html.indexOf('id="configResetModal"'))), "");
  check("...showing the entry", formCtl("Name").value === "hot_paths" && formCtl("Cypher").value === TOOL_ENTRY.cypher &&
    formCtl("Description").value === "Hot paths.", formCtl("Name").value);
  check("...the textarea keeps the server's text untouched", els.configYaml.value === TOOL_YAML, els.configYaml.value);
  check("...and focus is on the form's first control", focused === formCtl("Name"), focused && focused.tagName);
  check("...with one help line under the switch", shown(els.configFormHelp) &&
    html.includes('id="configFormHelp" style="display:none">The form writes the YAML; Save checks it first, as before.</div>'), els.configFormHelp.style.display);
  await press(els.configModalSave);
  check("open then save without changes sends the model's yaml byte for byte, dry run then write",
    writes().length === 2 && body(writes()[0]).dry_run === true && body(writes()[1]).dry_run === false &&
    writes().every(c => body(c).yaml === TOOL_YAML && c.url === "/api/config/repo-b/tools/hot_paths" && c.init.method === "PUT" &&
      ifMatch(c) === '"sha256:repo-b-tools"'), JSON.stringify(writes()));

  // 41b. a form edit is a textarea edit: serialised text, crossName cleared, the same request
  api.renderConfigPage(FM());
  fetchCalls = [];
  await editRow("repo-b", "hot_paths");
  api.edit.crossName = "stale";
  await typeIn(formCtl("Description"), "Hotter paths.");
  const edited = api.configEntryYaml("tools", { ...TOOL_ENTRY, description: "Hotter paths." }, Object.keys(TOOL_ENTRY));
  check("a form edit writes the serialised entry into the textarea", els.configYaml.value === edited, els.configYaml.value);
  check("...and clears a known-taken name like a textarea edit", api.edit.crossName === null, String(api.edit.crossName));
  check("...keeping a value the form didn't touch exactly (the boolean parameter's 'yes')", /default: "yes"/.test(els.configYaml.value), els.configYaml.value);
  await press(els.configModalSave);
  check("Save from the form sends the textarea's text: same URL, method, If-Match, dry run then write",
    writes().length === 2 && body(writes()[0]).dry_run === true && body(writes()[1]).dry_run === false &&
    writes().every(c => body(c).yaml === edited && c.url === "/api/config/repo-b/tools/hot_paths" && c.init.method === "PUT" &&
      ifMatch(c) === '"sha256:repo-b-tools"'), JSON.stringify(writes()));

  // 41c. a form edit after a warning confirm forces a new dry run
  api.renderConfigPage(FM());
  fetchCalls = [];
  respond = warnDry;
  await editRow("repo-b", "hot_paths");
  await press(els.configModalSave);
  check("a dry run with warnings asks for 'Save anyway' in form mode too", els.configModalSave.textContent === "Save anyway" &&
    shown(els.configModalConfirm), els.configModalSave.textContent);
  check("...moving focus off the button to the form's first control (not left on Save once the form unlocks)",
    focused === formCtl("Name") && focused !== els.configModalSave, focused && focused.tagName);
  const cy = formCtl("Cypher");
  cy.focus();
  await typeIn(cy, TOOL_ENTRY.cypher + "LIMIT 5\n");
  check("a form edit drops the confirm exactly as typing does", els.configModalSave.textContent === "Save" && !shown(els.configModalConfirm) &&
    api.edit.confirmed === false, els.configModalSave.textContent);
  check("...without pulling focus away from the field being typed in", focused === cy, focused && focused.tagName);
  await press(els.configModalSave);
  check("...so the next click dry-runs the new text instead of writing it",
    writes().length === 2 && writes().every(c => body(c).dry_run === true) && body(writes()[1]).yaml === els.configYaml.value &&
    /LIMIT 5/.test(body(writes()[1]).yaml) && els.configModalSave.textContent === "Save anyway", JSON.stringify(writes()));
  await typeIn(formCtl("Description"), formCtl("Description").value);
  check("an input event that leaves the text as it was keeps the confirm", els.configModalSave.textContent === "Save anyway" &&
    api.edit.confirmed === true, els.configModalSave.textContent);
  /* a confirm bound to text the form then changed (an edit landing after the click) re-dry-runs too */
  api.edit.confirmed = true;
  await typeIn(cy, TOOL_ENTRY.cypher + "LIMIT 6\n");
  api.edit.confirmed = true;
  await press(els.configModalSave);
  check("a confirm never covers text the form changed after its dry run", writes().length === 3 && body(writes()[2]).dry_run === true &&
    /LIMIT 6/.test(body(writes()[2]).yaml), JSON.stringify(writes()));
  await press(els.configModalSave);
  check("...and the confirmed write sends exactly the reviewed text", writes().length === 4 && body(writes()[3]).dry_run === false &&
    body(writes()[3]).yaml === body(writes()[2]).yaml, JSON.stringify(writes()));

  // 41d. busy: the form and the switch are locked like the textarea
  api.renderConfigPage(FM());
  fetchCalls = [];
  await editRow("repo-b", "hot_paths");
  gate = new Promise(r => { release = r; });
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("while a dry run is out the form fieldset and both switch buttons are disabled",
    els.configForm.disabled === true && els.configModeForm.disabled === true && els.configModeYaml.disabled === true,
    JSON.stringify([els.configForm.disabled, els.configModeForm.disabled, els.configModeYaml.disabled]));
  const busyText = els.configYaml.value;
  await typeIn(formCtl("Description"), "typed while busy");
  await press(find(els.configForm, e => e.tagName === "BUTTON" && e.textContent === "Add parameter")[0]);
  check("...a form event that lands anyway changes neither the text nor the rows", els.configYaml.value === busyText &&
    rowsOf("Parameters").length === 2 && api.edit.formState.description === "Hot paths.", els.configYaml.value);
  await press(els.configModeYaml);
  check("...and switching to YAML meanwhile does nothing", shown(els.configForm) && !shown(els.configYaml), els.configForm.style.display);
  gate = null; release(); await pending;
  check("...unlocked once the check is back", els.configForm.disabled === false && els.configModeForm.disabled === false &&
    els.configModeYaml.disabled === false && els.configModalSave.textContent === "Save anyway", els.configModalSave.textContent);
  els.configModalCancel.fire("click");

  // 41e. YAML only, with the reason: entries the form can't carry
  const formOff = reason => !shown(els.configForm) && shown(els.configYaml) && shown(els.configEditorSwitch) && shown(els.configFormNotice) &&
    els.configFormNoticeText.textContent === reason && els.configModeForm.getAttribute("aria-disabled") === "true" &&
    els.configModeForm.getAttribute("aria-describedby") === "configFormNoticeText" && els.configModeForm.disabled === false &&
    !shown(els.configFormDiscard);
  await editRow("repo-b", "dated");
  check("an entry JSON can't carry (entry: null) opens in YAML with the reason; Form stays focusable but unavailable",
    formOff("This entry contains a value the page can't carry exactly (for example a date or a very large number). Edit it as YAML."),
    els.configFormNoticeText.textContent);
  await press(els.configModeForm);
  check("...and pressing Form does nothing", !shown(els.configForm) && shown(els.configYaml), els.configForm.style.display);
  await editRow("repo-b", "extra");
  check("an entry with a field the form doesn't edit names it", formOff(FIELD("version")), els.configFormNoticeText.textContent);
  await editRow("repo-b", "multi");
  check("a line break in a single-line field opens in YAML, naming the field", formOff(FIELD("parameters.0.description")),
    els.configFormNoticeText.textContent);
  await editRow("repo-b", "PARAMS");
  check("a relationship with non-empty custom.params opens in YAML, naming the field", formOff(FIELD("custom.params")),
    els.configFormNoticeText.textContent);
  els.configModalCancel.fire("click");

  // 41f. delete and copy never show the form: the read-only YAML, no switch, no notice
  await editRow("repo-b", "hot_paths", "Delete");
  check("Delete shows the read-only YAML, never the form", !shown(els.configForm) && !shown(els.configEditorSwitch) && !shown(els.configFormNotice) &&
    shown(els.configYaml) && els.configYaml.readOnly === true && els.configYaml.value === TOOL_YAML, els.configForm.style.display);
  els.configModalCancel.fire("click");
  await editRow("repo-b", "hot_paths", "Copy to…");
  check("Copy shows the read-only YAML, never the form", !shown(els.configForm) && !shown(els.configEditorSwitch) && !shown(els.configFormNotice) &&
    shown(els.configYaml) && els.configYaml.readOnly === true, els.configForm.style.display);
  els.configModalCancel.fire("click");
  await editRow("repo-b", "Runbook", "Delete");
  check("...a node type's Delete too", !shown(els.configForm) && !shown(els.configEditorSwitch) && shown(els.configYaml), els.configForm.style.display);
  els.configModalCancel.fire("click");
  await editRow("repo-b", "OWNS", "Delete");
  check("...a relationship's Delete too", !shown(els.configForm) && !shown(els.configEditorSwitch) && shown(els.configYaml) &&
    els.configYaml.readOnly === true && els.configYaml.value === REL_YAML, els.configForm.style.display);
  els.configModalCancel.fire("click");
  await editRow("repo-b", "OWNS", "Copy to…");
  check("...and a relationship's Copy", !shown(els.configForm) && !shown(els.configEditorSwitch) && shown(els.configYaml) &&
    els.configYaml.readOnly === true, els.configForm.style.display);
  els.configModalCancel.fire("click");

  // 41g. the global warning step shows no editor; Continue shows the form
  await editRow("__global__", "hot_paths");
  check("the global warning step shows neither the form, the YAML nor the switch",
    !shown(els.configForm) && !shown(els.configYaml) && !shown(els.configEditorSwitch) && !shown(els.configFormNotice), els.configForm.style.display);
  await press(els.configModalSave);
  check("...Continue shows the form", shown(els.configForm) && shown(els.configEditorSwitch) && shown(els.configDestField), els.configForm.style.display);
  els.configModalCancel.fire("click");

  // 41h. Form -> YAML -> a hand edit: Form explains itself; Discard returns and drops the confirm
  fetchCalls = [];
  respond = warnDry;
  await editRow("repo-b", "hot_paths");
  await typeIn(formCtl("Description"), "Edited.");
  const formText = els.configYaml.value;
  await press(els.configModeYaml);
  check("Form -> YAML shows the form's own text", shown(els.configYaml) && !shown(els.configForm) && els.configYaml.value === formText &&
    els.configModeYaml.getAttribute("aria-pressed") === "true" && els.configModeForm.getAttribute("aria-disabled") === "false" &&
    !shown(els.configFormNotice) && !shown(els.configFormHelp), els.configYaml.value);
  els.configYaml.value = formText + "# mine\n";
  await els.configYaml.fire("input");
  check("after a hand edit Form is unavailable, with the reason and a Discard button",
    els.configModeForm.getAttribute("aria-disabled") === "true" && els.configModeForm.getAttribute("aria-describedby") === "configFormNoticeText" &&
    els.configFormNoticeText.textContent === "The YAML was edited by hand, and the form can only show text it produced. Keep editing as YAML, or discard the hand edits to return to the form." &&
    shown(els.configFormNotice) && shown(els.configFormDiscard), els.configFormNoticeText.textContent);
  await press(els.configModeForm);
  check("...pressing Form keeps the hand edit in YAML", shown(els.configYaml) && els.configYaml.value === formText + "# mine\n", els.configYaml.value);
  await press(els.configModalSave);
  check("...the hand edit is what gets dry-run", writes().length === 1 && body(writes()[0]).yaml === formText + "# mine\n" &&
    els.configModalSave.textContent === "Save anyway", JSON.stringify(writes()));
  await press(els.configFormDiscard);
  check("Discard YAML edits puts the form's text back and returns to the form", els.configYaml.value === formText && shown(els.configForm) &&
    !shown(els.configYaml) && formCtl("Description").value === "Edited.", els.configYaml.value);
  check("...dropping the confirm", api.edit.confirmed === false && els.configModalSave.textContent === "Save" && !shown(els.configModalConfirm),
    els.configModalSave.textContent);
  check("...and focus lands in the form", focused === formCtl("Name"), focused && focused.tagName);
  await press(els.configModalSave);
  check("...so the next Save dry-runs the form's text", writes().length === 2 && body(writes()[1]).dry_run === true &&
    body(writes()[1]).yaml === formText, JSON.stringify(writes()));
  els.configModalCancel.fire("click");

  // 41i. the last picked editor wins for the page session, when it is available
  await editRow("repo-b", "hot_paths");
  await press(els.configModeYaml);
  els.configModalCancel.fire("click");
  await editRow("repo-b", "hot_paths");
  check("after picking YAML the next entry opens in YAML, with Form available",
    shown(els.configYaml) && !shown(els.configForm) && els.configModeForm.getAttribute("aria-disabled") === "false" && els.configYaml.value === TOOL_YAML,
    els.configYaml.style.display);
  await press(els.configModeForm);
  check("...Form from the untouched opening text shows the opened entry", shown(els.configForm) && formCtl("Description").value === "Hot paths." &&
    els.configYaml.value === TOOL_YAML, els.configYaml.value);
  els.configModalCancel.fire("click");
  await buttons(card("repo-b"), "Add tool")[0].fire("click");
  check("Add opens the template in the form", shown(els.configForm) && els.configYaml.value === api.CONFIG_SECTIONS.tools.template &&
    formCtl("Name").value === "my_tool", els.configYaml.value);
  els.configModalCancel.fire("click");

  // 41j. accessibility: labels, legends, named row buttons, focus after Add and Remove
  await editRow("repo-b", "hot_paths");
  const labelled = root => find(root, e => ["INPUT", "SELECT", "TEXTAREA"].includes(e.tagName)).filter(c =>
    !(c.id && find(root, l => l.tagName === "LABEL" && l.htmlFor === c.id).length === 1) && !c.getAttribute("aria-label"));
  check("every tool form control has a label", labelled(els.configForm).length === 0, JSON.stringify(labelled(els.configForm).map(c => c.id)));
  check("every button in the form has a name", find(els.configForm, e => e.tagName === "BUTTON").every(b => b.textContent || b.getAttribute("aria-label")), "");
  check("parameter rows are legended with position and name", JSON.stringify(rowsOf("Parameters").map(legendOf)) ===
    JSON.stringify(["Parameter 1: limit", "Parameter 2: flag"]), JSON.stringify(rowsOf("Parameters").map(legendOf)));
  check("...and Remove names its row", removeOf(rowsOf("Parameters")[0]).getAttribute("aria-label") === "Remove parameter limit",
    removeOf(rowsOf("Parameters")[0]).getAttribute("aria-label"));
  await typeIn(formCtl("Name", 0, rowsOf("Parameters")[0]), "top_n");
  check("...both follow a renamed row", legendOf(rowsOf("Parameters")[0]) === "Parameter 1: top_n" &&
    removeOf(rowsOf("Parameters")[0]).getAttribute("aria-label") === "Remove parameter top_n", legendOf(rowsOf("Parameters")[0]));
  await typeIn(formCtl("Name"), "Bad Name");
  const nameHint = find(els.configForm, e => e.id === formCtl("Name").getAttribute("aria-describedby").split(" ").pop())[0];
  check("...a hint shows under its field, linked by aria-describedby", nameHint && shown(nameHint) && /lowercase/.test(nameHint.textContent),
    nameHint && nameHint.textContent);
  await press(find(els.configForm, e => e.tagName === "BUTTON" && e.textContent === "Add parameter")[0]);
  check("Add parameter adds a row and focuses its Name", rowsOf("Parameters").length === 3 &&
    focused === formCtl("Name", 0, rowsOf("Parameters")[2]) && legendOf(rowsOf("Parameters")[2]) === "Parameter 3",
    JSON.stringify(rowsOf("Parameters").map(legendOf)));
  await press(removeOf(rowsOf("Parameters")[0]));
  check("Remove focuses the next row's Name", rowsOf("Parameters").length === 2 && focused === formCtl("Name", 0, rowsOf("Parameters")[0]) &&
    legendOf(rowsOf("Parameters")[0]) === "Parameter 1: flag", JSON.stringify(rowsOf("Parameters").map(legendOf)));
  await press(removeOf(rowsOf("Parameters")[1]));
  check("...or the Add button after the last row", focused && focused.textContent === "Add parameter", focused && focused.textContent);
  check("removed rows leave the YAML", !/top_n/.test(els.configYaml.value) && /name: flag/.test(els.configYaml.value), els.configYaml.value);
  els.configModalCancel.fire("click");

  // 41k. never rebuild state from a control that could rewrite it
  await editRow("repo-b", "hot_paths");
  const flagDefault = formCtl("Default", 1);
  check("a boolean default the select has no word for gets its own option, and shows it",
    flagDefault.tagName === "SELECT" && flagDefault.value === "yes" && flagDefault.children.some(o => o.value === "yes"), flagDefault.value);
  flagDefault.value = "true";
  await flagDefault.fire("change");
  check("...choosing true writes a boolean", /default: true/.test(els.configYaml.value), els.configYaml.value);
  await press(find(els.configForm, e => e.tagName === "BUTTON" && e.textContent === "Add parameter")[0]);
  check("...and the opened value keeps its option after a rebuild", formCtl("Default", 1).value === "true" &&
    formCtl("Default", 1).children.some(o => o.value === "yes"), JSON.stringify(formCtl("Default", 1).children.map(o => o.value)));
  const limitDefault = formCtl("Default", 0);
  check("a typed default is a text input (a number input would read '' for '12abc')", limitDefault.tagName === "INPUT" && limitDefault.type === "text",
    limitDefault.type);
  await typeIn(limitDefault, "12abc");
  check("...text that isn't an integer is kept as typed", limitDefault.value === "12abc" && /default: "12abc"/.test(els.configYaml.value), els.configYaml.value);
  await typeIn(limitDefault, "12");
  check("...an integer becomes a number", /default: 12\n/.test(els.configYaml.value), els.configYaml.value);
  const maxRows = formCtl("Max rows");
  check("Max rows is a text input too", maxRows.tagName === "INPUT" && maxRows.type === "text", maxRows.type);
  await typeIn(maxRows, "12abc");
  check("...keeping what was typed", /max_rows: "12abc"/.test(els.configYaml.value), els.configYaml.value);
  await typeIn(maxRows, "50");
  check("...or the number", /max_rows: 50\n/.test(els.configYaml.value), els.configYaml.value);
  const typeSel = formCtl("Type", 0);
  typeSel.focus();
  typeSel.value = "float";
  await typeSel.fire("change");
  check("changing a parameter's type keeps the same select, focused (arrow keys don't churn)", formCtl("Type", 0) === typeSel &&
    focused === typeSel && /type: float/.test(els.configYaml.value), focused && focused.tagName);
  typeSel.value = "boolean";
  await typeSel.fire("change");
  check("...and swaps the default control for the type", formCtl("Default", 0).tagName === "SELECT" && focused === typeSel,
    formCtl("Default", 0).tagName);
  typeSel.value = "integer";
  await typeSel.fire("change");
  const req = formCtl("Required", 0);
  req.focus();
  req.checked = true;
  await req.fire("change");
  check("ticking Required disables the default and keeps focus on the box", formCtl("Default", 0).disabled === true &&
    focused === formCtl("Required", 0), String(formCtl("Default", 0).disabled));
  els.configModalCancel.fire("click");

  // 41l. node types: ticks make the key, the source select, the colour text
  await editRow("repo-b", "Runbook");
  check("a node type opens in the form", shown(els.configForm) && formCtl("Label").value === "Runbook" &&
    JSON.stringify(rowsOf("Metadata").map(legendOf)) === JSON.stringify(["Field 1: slug", "Field 2: owner"]), els.configForm.style.display);
  check("every node type form control has a label", labelled(els.configForm).length === 0, JSON.stringify(labelled(els.configForm).map(c => c.id)));
  const key2 = formCtl("Key", 1);
  key2.checked = true;
  await key2.fire("change");
  check("ticking Key on a row appends it to the key in row order", /^key: \[slug, owner\]$/m.test(els.configYaml.value), els.configYaml.value);
  const src = formCtl("Source");
  src.value = "folder";
  await src.fire("change");
  check("a filesystem source writes provider and kind", /source:\n  provider: filesystem\n  kind: folder/.test(els.configYaml.value), els.configYaml.value);
  const srcHint = find(els.configForm, e => e.id === src.getAttribute("aria-describedby").split(" ").pop())[0];
  check("...with the path-key hint", shown(srcHint) && /path/.test(srcHint.textContent), srcHint.textContent);
  await typeIn(formCtl("Colour"), "#1F77B4");
  const swatch = find(els.configForm, e => e.type === "color")[0];
  check("a colour is text; the swatch follows it", /color: "#1F77B4"/.test(els.configYaml.value) && swatch.value === "#1f77b4", els.configYaml.value);
  await press(find(els.configForm, e => e.getAttribute && e.getAttribute("aria-label") === "Clear colour")[0]);
  check("...and Clear removes it", !/color:/.test(els.configYaml.value) && formCtl("Colour").value === "", els.configYaml.value);
  els.configModalCancel.fire("click");

  // 41m. hostile values land only in .value / textContent
  await editRow("repo-b", "hostile");
  check("hostile name, description and Cypher are control values", formCtl("Name").value === HOSTILE && formCtl("Description").value === HOSTILE &&
    formCtl("Cypher").value === HOSTILE, formCtl("Name").value);
  await typeIn(formCtl("Name"), HOSTILE + "2");
  check("...and nothing reaches innerHTML", allEls.every(e => !e._html.includes("<img") && !e._html.includes("onerror")),
    JSON.stringify(allEls.filter(e => e._html.includes("<img")).map(e => e._html)));
  els.configModalCancel.fire("click");

  // 41n. relationships in the form: provider and custom name, from as labels, the shared colour control
  api.renderConfigPage(FM());
  fetchCalls = [];
  respond = quiet;
  await editRow("repo-b", "OWNS");
  check("a relationship with an entry opens in the form, legended Relationship", shown(els.configForm) && !shown(els.configYaml) &&
    shown(els.configEditorSwitch) && els.configModeForm.getAttribute("aria-pressed") === "true" && els.configForm.children[0].tagName === "LEGEND" &&
    els.configForm.children[0].textContent === "Relationship", els.configForm.style.display);
  check("...showing the entry: from as comma-separated labels, the provider, the custom name, the colour",
    formCtl("Type").value === "OWNS" && formCtl("From").value === "Team" && formCtl("To").value === "Service" &&
    formCtl("Provider").value === "custom" && formCtl("Custom provider name").value === "owners" && formCtl("Colour").value === "#1f77b4",
    JSON.stringify([formCtl("Type").value, formCtl("From").value, formCtl("Provider").value]));
  check("...the fields in model order, the custom name right after Provider",
    JSON.stringify(find(els.configForm, e => e.tagName === "LABEL").map(l => l.textContent)) ===
      JSON.stringify(["Type", "From", "To", "Provider", "Custom provider name", "Colour"]),
    JSON.stringify(find(els.configForm, e => e.tagName === "LABEL").map(l => l.textContent)));
  check("...Type's help says an edit renames it", /Changing it renames the relationship\./.test(
    find(els.configForm, e => e.id === formCtl("Type").getAttribute("aria-describedby").split(" ")[0])[0].textContent), "");
  check("...the provider options name what each does", JSON.stringify(formCtl("Provider").children.map(o => [o.value, o.textContent])) ===
    JSON.stringify([["builtin", "Built-in type"], ["custom", "Custom provider"], ["filesystem", "Filesystem (file → parent folder)"]]),
    JSON.stringify(formCtl("Provider").children.map(o => [o.value, o.textContent])));
  check("...the textarea keeps the server's text, focus on the first control", els.configYaml.value === REL_YAML && focused === formCtl("Type"),
    focused && focused.tagName);
  check("every relationship form control has a label", labelled(els.configForm).length === 0, JSON.stringify(labelled(els.configForm).map(c => c.id)));
  check("...and every text field and select has its hint box linked by aria-describedby",
    ["Type", "From", "To", "Provider", "Custom provider name", "Colour"].every(l => {
      const ids = (formCtl(l).getAttribute("aria-describedby") || "").split(" ");
      return ids.some(id => find(els.configForm, e => e.id === id && e.classList.contains("cfg-hint")).length === 1);
    }), "");
  await press(els.configModalSave);
  check("open then save sends the relationship's yaml byte for byte, dry run then write, to the schema route",
    writes().length === 2 && body(writes()[0]).dry_run === true && body(writes()[1]).dry_run === false &&
    writes().every(c => body(c).yaml === REL_YAML && c.url === "/api/config/repo-b/schema/relationships/OWNS" && c.init.method === "PUT" &&
      ifMatch(c) === '"sha256:repo-b-schema"'), JSON.stringify(writes()));
  // provider switching: the custom name shows and hides in place, the select keeps focus, the YAML follows
  api.renderConfigPage(FM());
  fetchCalls = [];
  await editRow("repo-b", "OWNS");
  const prov = formCtl("Provider");
  prov.focus();
  prov.value = "builtin";
  await prov.fire("change");
  const nameGone = api.formEls.all.filter(c => c.tagName === "INPUT" && c.type === "text").length === 4;
  check("switching the provider away from custom hides the custom name, keeping the same select focused",
    nameGone &&
    formCtl("Provider") === prov && focused === prov && !formCtl("Custom provider name") &&
    !find(els.configForm, e => e.tagName === "LABEL" && e.textContent === "Custom provider name").length, focused && focused.tagName);
  const emptyHolders = els.configForm.children.filter(e => e.tagName === "DIV" && !e.children.length && e.style.display !== "none");
  check("...and the empty holder is out of the layout (no doubled flex gap)", !emptyHolders.length, String(emptyHolders.length));
  check("...and the YAML drops the custom block", /^provider: builtin$/m.test(els.configYaml.value) && !/custom:|owners/.test(els.configYaml.value),
    els.configYaml.value);
  prov.value = "custom";
  await prov.fire("change");
  check("...the holder is shown again for the custom name", !els.configForm.children.some(e => e.tagName === "DIV" && e.style.display === "none" && !e.children.length), "");
  check("switching back shows the custom name again with its value, focus still on the select",
    formCtl("Custom provider name") && formCtl("Custom provider name").value === "owners" && focused === prov &&
    /custom:\n  name: owners\n  params: \{\}/.test(els.configYaml.value), els.configYaml.value);
  await typeIn(formCtl("Custom provider name"), "Owners");
  const cnHint = find(els.configForm, e => e.id === formCtl("Custom provider name").getAttribute("aria-describedby").split(" ").pop())[0];
  check("...a bad custom name shows its hint", shown(cnHint) && /lowercase/.test(cnHint.textContent), cnHint.textContent);
  check("the custom name control is among the form's controls only while shown",
    api.formEls.all.includes(formCtl("Custom provider name")) && api.formEls.all.filter(c => c.tagName === "INPUT" && c.type === "text").length === 5,
    String(api.formEls.all.length));
  els.configModalCancel.fire("click");
  // a form edit is a textarea edit: crossName cleared, a confirm dropped, Save sends the textarea's text
  api.renderConfigPage(FM());
  fetchCalls = [];
  respond = (url, init) => JSON.parse(init.body).dry_run
    ? { status: 200, body: { ok: true, written: false, warnings: ["Renaming a relationship drops the old edges."], notes: [], scope: FM().projects[1] } }
    : ok(FM().projects[1]);
  await editRow("repo-b", "OWNS");
  await press(els.configModalSave);
  check("a warned relationship dry run asks for 'Save anyway', focus moving into the form (not onto Save)",
    els.configModalSave.textContent === "Save anyway" && focused === formCtl("Type"), focused && focused.tagName);
  api.edit.crossName = "stale";
  await typeIn(formCtl("From"), "Team, Service");
  check("a form edit writes from as a list into the textarea", /^from: \[Team, Service\]$/m.test(els.configYaml.value), els.configYaml.value);
  check("...clears crossName and drops the confirm", api.edit.crossName === null && api.edit.confirmed === false &&
    els.configModalSave.textContent === "Save", els.configModalSave.textContent);
  await press(els.configModalSave);
  check("...so Save dry-runs the textarea's text again", writes().length === 2 && writes().every(c => body(c).dry_run === true) &&
    body(writes()[1]).yaml === els.configYaml.value, JSON.stringify(writes()));
  els.configModalCancel.fire("click");
  // busy: the relationship fieldset and the switch are locked
  respond = quiet;
  await editRow("repo-b", "OWNS");
  gate = new Promise(r => { release = r; });
  clock += 1000;
  pending = els.configModalSave.fire("click");
  await Promise.resolve();
  check("while a relationship dry run is out the fieldset and both switch buttons are disabled",
    els.configForm.disabled === true && els.configModeForm.disabled === true && els.configModeYaml.disabled === true, "");
  const relBusy = els.configYaml.value;
  const provBusy = formCtl("Provider");
  provBusy.value = "builtin";
  await provBusy.fire("change");
  check("...a provider change that lands anyway changes nothing", els.configYaml.value === relBusy && api.edit.formState.provider === "custom" &&
    !!formCtl("Custom provider name"), els.configYaml.value);
  gate = null; release(); await pending;
  els.configModalCancel.fire("click");
  // the shared colour control
  await editRow("repo-b", "OWNS");
  await typeIn(formCtl("Colour"), "#FF0000");
  const relSwatch = find(els.configForm, e => e.type === "color")[0];
  check("the relationship colour is text with a swatch that follows it", /color: "#FF0000"/.test(els.configYaml.value) && relSwatch.value === "#ff0000",
    els.configYaml.value);
  await press(find(els.configForm, e => e.getAttribute && e.getAttribute("aria-label") === "Clear colour")[0]);
  check("...and Clear removes it", !/color:/.test(els.configYaml.value) && formCtl("Colour").value === "", els.configYaml.value);
  els.configModalCancel.fire("click");
  // hostile values
  await editRow("repo-b", "HOSTILE_REL");
  check("hostile relationship values are control values", ["Type", "From", "To", "Custom provider name", "Colour"].every(l => formCtl(l).value === HOSTILE),
    formCtl("Type").value);
  await typeIn(formCtl("From"), HOSTILE + ", " + HOSTILE);
  check("...their hints are text, and nothing reaches innerHTML", allEls.every(e => !e._html.includes("<img") && !e._html.includes("onerror")),
    JSON.stringify(allEls.filter(e => e._html.includes("<img")).map(e => e._html)));
  els.configModalCancel.fire("click");

  // 42. the editor is a dialog: focus lands inside once it is open, Tab wraps, focus goes back to the opener
  api.renderConfigPage(FM());
  fetchCalls = [];
  const dlg = html.slice(html.indexOf('id="configModal"') - 40, html.indexOf('id="configModalWarn"'));
  check("the Config modal is a labelled modal dialog", /role="dialog"/.test(dlg) && /aria-modal="true"/.test(dlg) &&
    /aria-labelledby="configModalTitle"/.test(dlg), dlg);
  check("the hand-edit notice is a polite live region", /id="configFormNotice"[^>]*aria-live="polite"/.test(html), "");
  const opener = buttons(rowFor(card("repo-b"), "hot_paths"), "Edit")[0];
  await press(opener);
  check("opening the editor focuses its first control once the modal is open (form mode)", focused === formCtl("Name"), focused && focused.tagName);
  const tab = (shift = false) => { const ev = { key: "Tab", shiftKey: shift, defaultPrevented: false, preventDefault() { ev.defaultPrevented = true; } }; api.configModalKey(ev); return ev; };
  focused = els.configModalSave;
  check("Tab from Save wraps to the first control (the Form switch)", tab().defaultPrevented && focused === els.configModeForm, focused && focused.id);
  check("Shift+Tab from the first control wraps to Save", tab(true).defaultPrevented && focused === els.configModalSave, focused && focused.id);
  focused = formCtl("Name");
  check("Tab in the middle is left to the browser", !tab().defaultPrevented && !tab(true).defaultPrevented && focused === formCtl("Name"), "");
  focused = opener;
  check("Tab from outside the modal pulls focus in", tab().defaultPrevented && focused === els.configModeForm, focused && focused.id);
  els.configModalSave.disabled = true;
  focused = els.configModalCancel;
  check("a disabled last control is skipped: Tab from Cancel wraps", tab().defaultPrevented && focused === els.configModeForm, focused && focused.id);
  els.configModalSave.disabled = false;
  await els.configModalCancel.fire("click");
  check("Cancel returns focus to the Edit button that opened it", focused === opener && api.edit === null, focused && focused.tagName);
  await press(opener);
  api.configModalKey({ key: "Escape" });
  check("Escape returns focus to the opener too", focused === opener && !els.configModal.classList.contains("open"), focused && focused.tagName);
  await press(opener);
  await els.configModal.onclick({ target: els.configModal });
  check("an overlay click returns focus to the opener too", focused === opener, focused && focused.tagName);
  api.renderConfigPage(MODEL());
  await press(buttons(rowFor(card("repo-a"), "Runbook"), "Edit")[0]);
  check("a YAML-only entry focuses the textarea", focused === els.configYaml, focused && focused.id);
  focused = els.configModalSave;
  const firstCtl = shown(els.configEditorSwitch) ? els.configModeForm : els.configYaml;
  check("in YAML mode Tab from Save wraps to the first visible control; Shift+Tab back", tab().defaultPrevented && focused === firstCtl && tab(true).defaultPrevented && focused === els.configModalSave, focused && focused.id);
  els.configModalCancel.fire("click");
  respond = () => ok(globalBlock());
  await press(buttons(rowFor(card("__global__"), "hot_paths"), "Delete")[0]);
  check("the global warning step focuses its Continue button (no editor yet)", focused === els.configModalSave && els.configModalSave.textContent === "Continue", focused && focused.id);
  els.configModalCancel.fire("click");
  // the arming focus move after a warned dry run is unchanged: off Save, onto the editor
  api.renderConfigPage(FM());
  respond = warnDry;
  await editRow("repo-b", "hot_paths");
  await press(els.configModalSave);
  check("after a warned dry run focus is still off Save, on the form's first control", els.configModalSave.textContent === "Save anyway" &&
    focused === formCtl("Name"), focused && focused.tagName);
  els.configModalCancel.fire("click");

  // 43. the description count is the server's: stripped (Python's whitespace), then code points
  const descCount = () => find(els.configForm, e => /characters$/.test(e.textContent) && e.children.length === 0)[0].textContent;
  const hintFor = d => { const f = api.configFormFromEntry("tools", TOOL_ENTRY).form; f.description = d; return api.configFormHints("tools", f).filter(h => h.field === "description"); };
  const A = n => "a".repeat(n);
  check("1024 characters padded with spaces is within the limit", hintFor("  " + A(1024) + "\n").length === 0, "");
  check("1025 characters is over, and says so", /1025 characters; the limit is 1024/.test((hintFor(A(1025))[0] || {}).text || ""), JSON.stringify(hintFor(A(1025))));
  check("1024 astral characters count as 1024 (code points, not UTF-16 units)", hintFor("\u{1F600}".repeat(1024)).length === 0 && hintFor("\u{1F600}".repeat(1025)).length === 1, "");
  check("Python's str.strip removes NEL and the separator controls, so they pad for free", hintFor(A(1024) + "\x85\x1c").length === 0 && hintFor("\x1f" + A(1024)).length === 0, "");
  check("...and keeps a BOM, which it does not strip", hintFor(A(1024) + "\uFEFF").length === 1, "");
  await editRow("repo-b", "hot_paths");
  await typeIn(formCtl("Description"), A(1024) + "\x85");
  check("the live count agrees: 1024 / 1024", descCount() === "1024 / 1024 characters", descCount());
  await typeIn(formCtl("Description"), A(1024) + "\uFEFF");
  check("...and a BOM counts: 1025 / 1024", descCount() === "1025 / 1024 characters", descCount());
  els.configModalCancel.fire("click");

  // 44. the Reset dialog gets the editor's dialog handling
  const rdlg = html.slice(html.indexOf('id="configResetModal"') - 40, html.indexOf('id="configResetTitle"'));
  check("the Reset modal is a labelled modal dialog", /role="dialog"/.test(rdlg) && /aria-modal="true"/.test(rdlg) &&
    /aria-labelledby="configResetTitle"/.test(rdlg), rdlg);
  const inside = (el, root) => { for (let e = el; e; e = e.parentNode) if (e === root) return true; return false; };
  const tickOnce = () => new Promise(r => setImmediate(r));
  const rdry = { status: 200, body: { ok: true, written: false, file: "devgraph.tools.yaml", fingerprint: "sha256:dry-fp",
    removed: { tools: ["hot_paths"] }, warnings: [], notes: [], scope: project("repo-a"), global: globalBlock() } };
  api.renderConfigPage(MODEL());
  fetchCalls = [];
  respond = () => rdry;
  const ropener = buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0];
  let openGate;
  gate = new Promise(r => { openGate = r; });
  const opening = press(ropener);
  await tickOnce();
  check("opening Reset moves focus inside the dialog before the dry run answers", focused === els.configResetCancel, focused && focused.id);
  openGate(); gate = null;
  await opening;
  check("...and onto the phrase input once the list is shown", focused === els.configResetTyped, focused && focused.id);
  const rtab = (shift = false) => { const ev = { key: "Tab", shiftKey: shift, defaultPrevented: false, preventDefault() { ev.defaultPrevented = true; } }; api.configModalKey(ev); return ev; };
  focused = els.configResetCancel;
  check("Tab from Cancel wraps to the phrase input (the disabled Reset is skipped)", rtab().defaultPrevented && focused === els.configResetTyped, focused && focused.id);
  check("Shift+Tab from the phrase input wraps to Cancel", rtab(true).defaultPrevented && focused === els.configResetCancel, focused && focused.id);
  focused = els.configResetTyped;
  check("Tab from the phrase input is left to the browser", !rtab().defaultPrevented && focused === els.configResetTyped, "");
  focused = ropener;
  check("Tab from outside the dialog pulls focus in", rtab().defaultPrevented && focused === els.configResetTyped, focused && focused.id);
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  clock += 1000;
  api.configModalKey({ key: "Escape" });
  check("Escape closes the dialog, returns focus to the opener and sends no reset", !els.configResetModal.classList.contains("open") &&
    api.reset === null && focused === ropener && fetchCalls.length === 1 && body(fetchCalls[0]).dry_run === true, JSON.stringify(fetchCalls));
  await press(ropener);
  await els.configResetCancel.fire("click");
  check("Cancel returns focus to the opener", focused === ropener && api.reset === null, focused && focused.id);
  await press(ropener);
  await els.configResetModal.onclick({ target: els.configResetModal });
  check("an overlay click returns focus to the opener", focused === ropener && api.reset === null, focused && focused.id);

  // 45. after a save or reset re-renders the card, focus lands on the same entry's Edit button, else its section's Add button
  const FMNamed = (from, to) => { const m = FM(); m.projects[1].tools.entries.find(e => e.name === from).name = to; return m; };
  const FMWithout = name => { const m = FM(); m.projects[1].tools.entries = m.projects[1].tools.entries.filter(e => e.name !== name); return m; };
  const editBtn = (scope, name) => buttons(rowFor(card(scope), name), "Edit")[0];
  const addBtn = (scope, section) => buttons(card(scope), "Add " + api.CONFIG_SECTIONS[section].noun)[0];
  const answer = model => (url, init) => ({ status: 200, body: { ok: true, written: !(init.body && JSON.parse(init.body).dry_run), warnings: [], notes: [], scope: model.projects[1] } });
  const desc = () => focused && focused.tagName + " " + focused.textContent;
  api.renderConfigPage(FM());
  configPayload = FM();
  respond = answer(FM());
  let old = editBtn("repo-b", "hot_paths");
  await press(old);
  await press(els.configModalSave);
  check("a saved edit re-renders the card and focuses the same entry's new Edit button", api.edit === null && editBtn("repo-b", "hot_paths") !== old &&
    focused === editBtn("repo-b", "hot_paths"), desc());
  api.renderConfigPage(FM());
  configPayload = FMNamed("hot_paths", "renamed");
  respond = answer(configPayload);
  await press(editBtn("repo-b", "hot_paths"));
  await typeIn(formCtl("Name"), "renamed");
  await press(els.configModalSave);
  check("a renamed entry has no old-name Edit button, so focus goes to its section's Add button",
    !rowFor(card("repo-b"), "hot_paths") && focused === addBtn("repo-b", "tools"), desc());
  api.renderConfigPage(FM());
  configPayload = FMWithout("hot_paths");
  respond = answer(configPayload);
  old = buttons(rowFor(card("repo-b"), "hot_paths"), "Delete")[0];
  await press(old);
  await press(els.configModalSave);
  check("a deleted entry: focus goes to its section's Add button, not the removed opener", !rowFor(card("repo-b"), "hot_paths") &&
    focused === addBtn("repo-b", "tools") && focused !== old, desc());
  check("the card heading can take focus by script (the last resort)", find(card("repo-b"), e => e.tagName === "H3")[0].getAttribute("tabindex") === "-1", "no tabindex on the card heading");
  // a reset: the file is gone, so its Reset button is too
  api.renderConfigPage(MODEL());
  const afterA2 = project("repo-a"); afterA2.tools.state = "absent"; afterA2.tools.fingerprint = "absent";
  respond = (url, init) => JSON.parse(init.body).dry_run ? rdry
    : { status: 200, body: { ok: true, written: true, file: "devgraph.tools.yaml", fingerprint: "absent", removed: { tools: ["hot_paths"] }, warnings: [], notes: [], scope: afterA2, global: globalBlock() } };
  configPayload = MODEL();
  await press(buttons(card("repo-a"), "Reset devgraph.tools.yaml…")[0]);
  els.configResetTyped.value = "repo-a";
  await els.configResetTyped.fire("input");
  await press(els.configResetConfirm);
  check("after a reset focus goes to the card's Add button for that file, not the body", api.reset === null && focused === addBtn("repo-a", "tools") &&
    buttons(card("repo-a"), "Reset devgraph.tools.yaml…").length === 0, desc());
  // the global store: the whole page reloads after it
  api.renderConfigPage(MODEL());
  respond = (url, init) => JSON.parse(init.body).dry_run ? { status: 200, body: { ...rdry.body, scope: globalBlock() } }
    : { status: 200, body: { ok: true, written: true, file: "global-tools.json", fingerprint: "sha256:g2", removed: { tools: ["hot_paths"] }, warnings: [], notes: [], scope: globalBlock() } };
  old = buttons(card("__global__"), "Reset global-tools.json…")[0];
  await press(old);
  els.configResetTyped.value = "global";
  await els.configResetTyped.fire("input");
  await press(els.configResetConfirm);
  check("after resetting the global store focus is on a control in the re-rendered global card, not the removed opener", api.reset === null &&
    focused !== old && inside(focused, card("__global__")), desc());

  // project tools trust: a badge, and Revoke only -- the page can never trust
  const trusting = (repo, state, revocable) => {
    const p = project(repo, [{ name: "hot_paths", tool_id: repo + "_hot_paths", yaml: "name: hot_paths\n", origin: state === "trusted" ? "project" : null,
      badges: state === "trusted" ? [] : [{ level: "warn", kind: "not-trusted", text: "Not served: not trusted", detail: "project tools not trusted (run devgraph config tools trust " + repo + ")" }] }]);
    p.tools.trust = { state, command: "devgraph config tools trust " + repo, revocable };
    p.tools.badges = [state === "trusted"
      ? { level: "info", kind: "trusted", text: "Trusted", detail: "Served." }
      : { level: "warn", kind: "not-trusted", text: state === "changed" ? "Changed since trusted" : "Not trusted", detail: "project tools not trusted (run devgraph config tools trust " + repo + ")." }];
    return p;
  };
  const sectionHead = (scope, title) => byClass(card(scope), "cfg-section-head").find(h => h.children[0].textContent === title);
  api.renderConfigPage({ global: globalBlock(), projects: [trusting("repo-a", "trusted", true), trusting("repo-b", "untrusted", false)] });
  check("a trusted repository's Tools heading shows the Trusted badge",
    byClass(sectionHead("repo-a", "Tools"), "cfg-badge").some(b => b.textContent === "Trusted" && b.classList.contains("info")), sectionHead("repo-a", "Tools").textContent);
  check("an untrusted one shows Not trusted, naming the command on hover",
    byClass(sectionHead("repo-b", "Tools"), "cfg-badge").some(b => b.textContent === "Not trusted" && b.dataset.tip.includes("devgraph config tools trust repo-b")),
    sectionHead("repo-b", "Tools").textContent);
  check("...and its tool row says it is not served", rowFor(card("repo-b"), "hot_paths").textContent.includes("Not served: not trusted"),
    rowFor(card("repo-b"), "hot_paths").textContent);
  check("Revoke trust is offered only where an approval is recorded",
    buttons(card("repo-a"), "Revoke trust").length === 1 && buttons(card("repo-b"), "Revoke trust").length === 0, card("repo-b").textContent);
  check("no control anywhere grants trust",
    find(els.configScopes, e => e.tagName === "BUTTON" && /^trust|approve/i.test(e.textContent)).length === 0,
    JSON.stringify(find(els.configScopes, e => e.tagName === "BUTTON").map(b => b.textContent)));
  r = api.configRevokeTrustRequest("repo a");
  check("configRevokeTrustRequest -> DELETE /api/config/<repo>/trust/tools, no body or If-Match",
    r.url === "/api/config/repo%20a/trust/tools" && r.init.method === "DELETE" && r.init.body === undefined && !r.init.headers, JSON.stringify(r));
  fetchCalls = [];
  const revokedNote = "Revoked trust in repo-a's project tools; running MCP sessions stop serving them within 2 seconds.";
  respond = () => ({ status: 200, body: { ok: true, written: true, notes: [revokedNote, "Trusting them again is done in a terminal: `devgraph config tools trust repo-a`."],
    scope: trusting("repo-a", "untrusted", false), global: globalBlock() } });
  await press(buttons(card("repo-a"), "Revoke trust")[0]);
  check("Revoke sends exactly one DELETE to the trust route", writes().length === 1 && writes()[0].url === "/api/config/repo-a/trust/tools" &&
    writes()[0].init.method === "DELETE", JSON.stringify(fetchCalls));
  check("...redraws the card from the response (Not trusted, no Revoke)",
    byClass(sectionHead("repo-a", "Tools"), "cfg-badge").some(b => b.textContent === "Not trusted") && buttons(card("repo-a"), "Revoke trust").length === 0,
    card("repo-a").textContent);
  check("...shows the notes, naming the CLI command to trust again",
    els.configStatus.textContent.includes(revokedNote) && card("repo-a").textContent.includes("devgraph config tools trust repo-a"), els.configStatus.textContent);
  check("...and keeps focus on the card", inside(focused, card("repo-a")), desc());
  respond = () => ({ status: 403, body: { detail: { code: "forbidden", message: "cross-site request refused" } } });
  api.renderConfigPage({ global: globalBlock(), projects: [trusting("repo-a", "trusted", true)] });
  await press(buttons(card("repo-a"), "Revoke trust")[0]);
  check("a refused revoke says so and leaves the badge", els.configStatus.textContent.includes("Revoke failed: cross-site request refused") &&
    buttons(card("repo-a"), "Revoke trust").length === 1, els.configStatus.textContent);

  // 46. focus stays in the dialogs after failed saves and resets
  check("the editor's error line is an alert", /id="configModalError"[^>]*role="alert"|role="alert"[^>]*id="configModalError"/.test(html));
  api.renderConfigPage(FM());
  configPayload = FM();
  respond = (url, init) => ({ status: 422, body: { detail: { code: "invalid", message: "bad tool" } } });
  await press(editBtn("repo-b", "hot_paths"));
  await press(els.configModalSave);
  check("a failed dry run leaves focus in the editor, not the body", api.edit !== null && shown(els.configModalError) &&
    focused === formCtl("Name"), desc());
  api.configModalKey({ key: "Escape" });
  respond = (url, init) => JSON.parse(init.body).dry_run ? { status: 200, body: { ok: true, warnings: [], notes: [], scope: FM().projects[1] } }
    : { status: 500, body: { detail: { code: "boom", message: "disk full" } } };
  await press(editBtn("repo-b", "hot_paths"));
  await press(els.configModalSave);
  check("a failed write leaves focus in the editor, not the body", api.edit !== null && shown(els.configModalError) &&
    focused === formCtl("Name"), desc());
  api.configModalKey({ key: "Escape" });
  // Reload re-renders the card; Cancel then falls back to the redrawn Edit button
  const fresh46 = FM().projects[1];
  respond = (url, init) => !init.method ? { status: 200, body: fresh46 }
    : { status: 412, body: { detail: { code: "stale", message: "changed on disk", scope: fresh46 } } };
  old = editBtn("repo-b", "hot_paths");
  await press(old);
  await press(els.configModalSave);
  await press(els.configModalReload);
  check("(setup) Reload redrew the card, so the opener is gone", old.isConnected === false && api.edit !== null);
  els.configModalCancel.focus();
  await els.configModalCancel.fire("click");
  check("Cancel after a Reload focuses the redrawn Edit button, not the detached opener", api.edit === null &&
    focused === editBtn("repo-b", "hot_paths"), desc());
  // reset: a 412 lands on Re-check
  api.renderConfigPage(MODEL());
  const freshB46 = project("repo-b"); freshB46.schema.fingerprint = "sha256:repo-b-schema-3";
  respond = (url, init) => {
    if (!init.method) return { status: 200, body: freshB46 };
    return JSON.parse(init.body).dry_run
      ? { status: 200, body: { ok: true, written: false, fingerprint: ifMatch({ init }).slice(1, -1), removed: { node_types: ["Runbook"], relationships: [] }, warnings: [], notes: [], scope: project("repo-b") } }
      : { status: 412, body: { detail: { code: "stale", message: "changed" } } };
  };
  old = buttons(card("repo-b"), "Reset devgraph.schema.yaml…")[0];
  await press(old);
  els.configResetTyped.value = "repo-b";
  await els.configResetTyped.fire("input");
  await press(els.configResetConfirm);
  check("a 412 on Reset moves focus to Re-check", focused === els.configResetRecheck, desc());
  await press(els.configResetRecheck);
  await new Promise(r => setTimeout(r, 0));
  check("(setup) Re-check redrew the card, so the opener is gone", old.isConnected === false && api.reset !== null);
  await press(els.configResetCancel);
  check("Cancel after a Re-check focuses a control in the redrawn card, not the detached opener", api.reset === null &&
    focused !== old && inside(focused, card("repo-b")), desc());

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
