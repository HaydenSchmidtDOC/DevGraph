/* Headless test of the header "+ Register repo" button (#btnRegister), whose
   wiring is lifted verbatim out of index.html and run against stub elements.

   The failure this exists to pin is invisible to every server-side test and to
   a glance at the markup: #btnRegister existed, looked enabled, and carried a
   tooltip promising "Add a repo below in Settings -> Repos", but no listener was
   ever bound to it, so clicking it did nothing. A DOM element with no listener
   must therefore fail here, as must a handler that opens the overlay on the
   wrong pane. Fully synchronous -- no timers, no load-order assumptions. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");

const lines = html.split("\n");
/* Returns the single source line matching `re`, or null -- null is reported as
   a readable check failure rather than thrown, so a missing handler produces a
   diagnosis instead of a stack trace. */
const line = re => {
  const found = lines.filter(l => re.test(l));
  if (found.length > 1) throw new Error("expected one line matching " + re + ", found " + found.length);
  return found.length ? found[0].trim() : null;
};
const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};

const registerLine = line(/document\.getElementById\(["']btnRegister["']\)\.addEventListener/);
const mcpLine = line(/querySelectorAll\(['"]#linkOpenMcpSettings['"]\)/);
const navHandler = grab(/^document\.querySelectorAll\("\.settings-nav button"\)/m, "}));");

// --- stubs ------------------------------------------------------------
const PANES = ["repos", "mcp", "watcher", "network", "neo4j", "vector", "global"];

const makeEl = (id, classes) => {
  const set = new Set(classes || []);
  const listeners = [];
  return {
    id,
    listeners,
    dataset: {},
    classes: set,
    classList: {
      add: c => set.add(c),
      remove: c => set.delete(c),
      contains: c => set.has(c),
    },
    addEventListener: (type, fn) => listeners.push({ type, fn }),
    /* A stub click runs the listeners the real source registered, so clicking
       [data-pane="repos"] genuinely executes index.html's settings-nav handler
       instead of a test-local imitation of it. */
    click(evt) {
      const e = evt || { preventDefault() {} };
      listeners.filter(l => l.type === "click").forEach(l => l.fn.call(this, e));
    },
  };
};

/* The overlay starts closed on the MCP pane, so "the Repos pane is active"
   can only become true if the handler actually switched panes. */
const settingsOverlay = makeEl("settingsOverlay", ["settings-overlay"]);
const btnRegister = makeEl("btnRegister", ["btn", "btn-sm"]);
const linkOpenMcpSettings = makeEl("linkOpenMcpSettings");
const navButtons = PANES.map(p => {
  const b = makeEl("nav-" + p, p === "mcp" ? ["active"] : []);
  b.dataset.pane = p;
  return b;
});
const panes = PANES.map(p => makeEl("pane-" + p, p === "mcp" ? ["settings-pane", "active"] : ["settings-pane"]));
const els = { settingsOverlay, btnRegister, linkOpenMcpSettings };
panes.forEach(p => { els[p.id] = p; });

const sandboxGlobals = {
  document: {
    getElementById: id => els[id] || null,
    querySelectorAll: sel => {
      if (sel === ".settings-nav button") return navButtons;
      if (sel === ".settings-pane") return panes;
      if (sel === "#linkOpenMcpSettings") return [linkOpenMcpSettings];
      const byPane = /^\[data-pane="([^"]+)"\]$/.exec(sel);
      if (byPane) return navButtons.filter(b => b.dataset.pane === byPane[1]);
      throw new Error("unstubbed selector: " + sel);
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  },
  console,
};

const src = [navHandler, registerLine || "", mcpLine || ""].join("\n");
new Function(...Object.keys(sandboxGlobals), src)(...Object.values(sandboxGlobals));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const activePane = () => panes.filter(p => p.classList.contains("active")).map(p => p.id).join(",");
const activeNav = () => navButtons.filter(b => b.classList.contains("active")).map(b => b.dataset.pane).join(",");
/* Always starts closed on some OTHER pane than the one under test, so
   "the right pane is active" can never pass by accident. */
const reset = initialPane => {
  settingsOverlay.classList.remove("open");
  navButtons.forEach(b => (b.dataset.pane === initialPane ? b.classList.add("active") : b.classList.remove("active")));
  panes.forEach(p => (p.id === "pane-" + initialPane ? p.classList.add("active") : p.classList.remove("active")));
};

// 1. the markup this wiring depends on is still there and unchanged
check("the header still has #btnRegister with its Settings -> Repos tooltip",
  /id="btnRegister"[^>]*title="Add a repo below in Settings → Repos"/.test(html),
  "the button or the tooltip it promises changed");
check("#settingsOverlay is still the overlay element", /id="settingsOverlay"/.test(html), "overlay id changed");
check("the Repos nav button is still selected by [data-pane=\"repos\"]",
  /<button[^>]*data-pane="repos"/.test(html), "the settings-nav Repos button changed");

// 2. the handler exists and is bound to the button -- inert markup fails here
check("index.html binds a click handler to #btnRegister", registerLine !== null,
  "no addEventListener on #btnRegister: the header button is inert markup");
check("exactly one click listener is bound to #btnRegister",
  btnRegister.listeners.filter(l => l.type === "click").length === 1,
  "click listeners bound: " + btnRegister.listeners.filter(l => l.type === "click").length);

// 3. clicking it opens the overlay ON THE REPOS PANE
reset("mcp");
btnRegister.click();
check("clicking #btnRegister opens the settings overlay",
  settingsOverlay.classList.contains("open"), "settingsOverlay classes: " + [...settingsOverlay.classes]);
check("clicking #btnRegister lands on the Repos pane", activePane() === "pane-repos",
  "active pane(s): " + (activePane() || "none"));
check("the Repos nav button is the one marked active", activeNav() === "repos",
  "active nav button(s): " + (activeNav() || "none"));

// 4. clicking twice is idempotent -- no toggle-closed on a second click
btnRegister.click();
check("a second click leaves the overlay open on Repos",
  settingsOverlay.classList.contains("open") && activePane() === "pane-repos",
  "open=" + settingsOverlay.classList.contains("open") + " pane=" + (activePane() || "none"));

// 5. the fix delegates pane switching instead of reimplementing it
check("the #btnRegister handler does not reimplement pane switching",
  registerLine !== null && !/settings-pane|pane-|classList\.remove/.test(registerLine),
  "handler: " + registerLine);
check("the #btnRegister handler leaves the register-repo submit path alone",
  registerLine !== null && !/regRepoPath|btnRegisterRepo|submitRegisterRepo/.test(registerLine),
  "handler: " + registerLine);

// 6. the pattern it copies still works -- the shared overlay/pane path is intact
reset("repos");
let prevented = 0;
linkOpenMcpSettings.click({ preventDefault: () => { prevented++; } });
check("#linkOpenMcpSettings still opens the overlay on the MCP pane",
  settingsOverlay.classList.contains("open") && activePane() === "pane-mcp",
  "open=" + settingsOverlay.classList.contains("open") + " pane=" + (activePane() || "none"));
check("#linkOpenMcpSettings still suppresses its anchor navigation", prevented === 1, "preventDefault calls: " + prevented);

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
