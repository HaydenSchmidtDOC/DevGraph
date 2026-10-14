/* Headless keyboard and screen-reader checks for the dashboard's shared widgets,
   lifted verbatim out of index.html: the themed <select> (themeSelect) and the
   "?" help tooltips (wireTooltip), run against a small stub DOM.

   What this guards: the themed select is a real combobox/listbox -- reachable by
   Tab, opened and driven by Arrow/Home/End/Enter/Space/Escape and type-ahead,
   announcing the active option through aria-activedescendant -- while the hidden
   native <select> stays out of the tab order; every help tooltip can be reached
   by keyboard, shows on focus, hides on blur and Escape, and is linked to its
   trigger through aria-describedby; nothing focusable is styled to opacity 0
   without a visible focus replacement; icon-only buttons carry an aria-label;
   focus outlines are never removed without a replacement. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");

const grab = (startRe, endRe) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const rest = html.slice(i);
  const j = rest.search(endRe);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return rest.slice(0, j);
};
const widgetSrc = grab(/^\/\* ── global fixed tooltip/m, /^\/\* The built-in types/m);

let failures = 0;
const check = (cond, msg) => {
  if (cond) console.log("ok   " + msg);
  else { failures++; console.log("FAIL " + msg); }
};

// --- a very small DOM ----------------------------------------------------
let focused = null;
const allEls = [];
const docListeners = {};
const mkEl = tag => {
  const classes = new Set();
  const listeners = {};
  const attrs = {};
  const el = {
    tagName: tag.toUpperCase(), _kids: [], parentNode: null, dataset: {}, style: {}, _text: "",
    get children() { return el._kids.slice(); },
    get textContent() { return el._text + el._kids.map(c => c.textContent).join(""); },
    set textContent(v) { el._text = String(v); el._kids.forEach(k => { k.parentNode = null; }); el._kids = []; },
    set innerHTML(v) { if (v !== "") throw new Error("stub innerHTML only supports clearing"); el.textContent = ""; },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach(c => classes.add(c)); },
    classList: {
      add: (...c) => c.forEach(x => classes.add(x)),
      remove: (...c) => c.forEach(x => classes.delete(x)),
      contains: c => classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
    },
    get id() { return attrs.id || ""; },
    set id(v) { attrs.id = String(v); },
    get tabIndex() {
      if ("tabindex" in attrs) return Number(attrs.tabindex);
      return ["BUTTON", "SELECT", "INPUT", "TEXTAREA"].includes(el.tagName) ? 0 : -1;
    },
    set tabIndex(v) { attrs.tabindex = String(v); },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: k => (k in attrs ? attrs[k] : null),
    hasAttribute: k => k in attrs,
    removeAttribute: k => { delete attrs[k]; },
    appendChild: c => {
      if (c.parentNode) c.parentNode._kids = c.parentNode._kids.filter(k => k !== c);
      el._kids.push(c); c.parentNode = el; return c;
    },
    insertBefore: (c, ref) => {
      if (c.parentNode) c.parentNode._kids = c.parentNode._kids.filter(k => k !== c);
      const i = el._kids.indexOf(ref);
      el._kids.splice(i < 0 ? el._kids.length : i, 0, c); c.parentNode = el; return c;
    },
    contains: o => { for (; o; o = o.parentNode) if (o === el) return true; return false; },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    dispatchEvent: e => {
      e.target = e.target || el;
      for (let n = el; n; n = n.parentNode) {
        (n._listeners[e.type] || []).forEach(fn => fn.call(n, e));
        if (!e.bubbles || e._stopped) return !e.defaultPrevented;
      }
      if (e.bubbles && !e._stopped) (docListeners[e.type] || []).forEach(fn => fn(e));
      return !e.defaultPrevented;
    },
    _listeners: listeners,
    getBoundingClientRect: () => ({ left: 300, top: 200, right: 313, bottom: 213, width: 13, height: 13 }),
    scrollIntoView: () => {},
    focus: () => {
      if (focused === el) return;
      const prev = focused;
      focused = el;
      if (prev) prev.dispatchEvent(mkEvent("blur", { bubbles: false }));
      el.dispatchEvent(mkEvent("focus", { bubbles: false }));
    },
    blur: () => { if (focused === el) { focused = null; el.dispatchEvent(mkEvent("blur", { bubbles: false })); } },
  };
  allEls.push(el);
  return el;
};
const mkEvent = (type, init) => {
  const e = Object.assign({ type, bubbles: true, defaultPrevented: false, _stopped: false }, init || {});
  e.preventDefault = () => { e.defaultPrevented = true; };
  e.stopPropagation = () => { e._stopped = true; };
  return e;
};
class Event { constructor(type, init) { return mkEvent(type, Object.assign({ bubbles: false }, init)); } }

const mkSelect = (id, opts, selected) => {
  const s = mkEl("select");
  s.id = id;
  s.options = opts.map(([value, text]) => ({ value, textContent: text }));
  let idx = Math.max(0, opts.findIndex(o => o[0] === selected));
  Object.defineProperty(s, "value", {
    get: () => (s.options[idx] ? s.options[idx].value : ""),
    set: v => { const i = s.options.findIndex(o => o.value === v); idx = i; },
  });
  Object.defineProperty(s, "selectedIndex", { get: () => idx, set: v => { idx = v; } });
  return s;
};

const header = mkEl("div");
const repoSelect = mkSelect("repoSelect",
  [["__all__", "All Repos"], ["alpha", "alpha"], ["beta", "beta"], ["bravo", "bravo"], ["charlie", "charlie"]], "alpha");
header.appendChild(repoSelect);
const others = ["layoutSelect", "chartInterval", "chartSpan"].map(id => {
  const s = mkSelect(id, [["a", "Apple"], ["b", "Banana"]], "a");
  header.appendChild(s);
  return s;
});
const changes = [];
repoSelect.addEventListener("change", () => changes.push(repoSelect.value));

const tipEl = mkEl("div"); tipEl.id = "tooltipBubble";
const qmark = mkEl("span"); qmark.className = "qmark"; qmark.dataset.tip = "Caps the LIMIT."; qmark.textContent = "?";
const qmark2 = mkEl("span"); qmark2.className = "qmark"; qmark2.dataset.tip = "Second tip."; qmark2.textContent = "?";
const byId = { tooltipBubble: tipEl, repoSelect };
others.forEach(s => { byId[s.id] = s; });

const document = {
  getElementById: id => byId[id] || allEls.find(e => e.id === id) || null,
  createElement: mkEl,
  querySelectorAll: sel => {
    const parts = sel.replace(/^\./, "").split(".");
    return allEls.filter(e => parts.every(p => e.classList.contains(p)));
  },
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  get activeElement() { return focused; },
};
/* A hand-driven clock: every key press below lands a second after the last
   unless it passes `quick`, so type-ahead's timeout is exercised deliberately. */
let clock = 1e6;
const sandbox = { document, window: { innerWidth: 1280, innerHeight: 800 }, Event, Date: { now: () => clock }, console };
vm.createContext(sandbox);
vm.runInContext(widgetSrc, sandbox);

const key = (el, k, extra) => {
  clock += extra && extra.quick ? 100 : 1000;
  const e = mkEvent("keydown", Object.assign({ key: k }, extra || {}));
  el.dispatchEvent(e);
  return e;
};

// --- themed select -----------------------------------------------------------
const wrap = repoSelect.parentNode;
const combo = wrap.children.find(c => c.tagName === "BUTTON");
const list = wrap.children.find(c => c.classList.contains("csel-list"));
check(!!combo && !!list, "themeSelect builds a trigger and a list next to the native select");
check(repoSelect.tabIndex === -1, "hidden native select is out of the tab order");
check(repoSelect.getAttribute("aria-hidden") === "true", "hidden native select is hidden from assistive tech");
check(combo.getAttribute("role") === "combobox", "trigger has role=combobox");
check(combo.getAttribute("aria-haspopup") === "listbox", "trigger declares a listbox popup");
check(combo.getAttribute("aria-expanded") === "false", "trigger starts collapsed");
check(!!combo.getAttribute("aria-label"), "trigger has an accessible name");
check(list.getAttribute("role") === "listbox", "list has role=listbox");
check(!!list.id && combo.getAttribute("aria-controls") === list.id, "trigger aria-controls points at the listbox");
const items = () => list.children;
check(items().length === 5 && items().every(i => i.getAttribute("role") === "option" && i.id),
  "every option is a role=option with an id");
check(items()[1].getAttribute("aria-selected") === "true" && items()[0].getAttribute("aria-selected") === "false",
  "the selected option carries aria-selected=true, the rest false");
const ids = new Set(others.flatMap(s => s.parentNode.children.find(c => c.classList.contains("csel-list")).children.map(i => i.id)));
check(items().every(i => !ids.has(i.id)), "option ids are unique across themed selects");

combo.focus();
const activeId = () => combo.getAttribute("aria-activedescendant");
const activeText = () => { const a = items().find(i => i.id === activeId()); return a ? a.textContent : null; };

let e = key(combo, "ArrowDown");
check(list.classList.contains("show") && combo.getAttribute("aria-expanded") === "true", "ArrowDown opens the list");
check(e.defaultPrevented, "ArrowDown does not scroll the page");
check(activeText() === "alpha", "opening makes the selected option active");
check(items().some(i => i.id === activeId() && i.classList.contains("active")), "the active option is visibly highlighted");
key(combo, "ArrowDown");
check(activeText() === "beta", "ArrowDown moves to the next option");
key(combo, "ArrowUp"); key(combo, "ArrowUp"); key(combo, "ArrowUp");
check(activeText() === "All Repos", "ArrowUp moves back and stops at the first option");
key(combo, "End");
check(activeText() === "charlie", "End jumps to the last option");
key(combo, "ArrowDown");
check(activeText() === "charlie", "ArrowDown stops at the last option");
key(combo, "Home");
check(activeText() === "All Repos", "Home jumps to the first option");
check(focused === combo, "focus stays on the combobox while navigating");

key(combo, "c");
check(activeText() === "charlie", "type-ahead jumps to the first option starting with the typed letter");
key(combo, "b");
check(activeText() === "beta", "a fresh type-ahead letter finds its match");
key(combo, "r", { quick: true });
check(activeText() === "bravo", "type-ahead extends the search string within the timeout");
key(combo, "b", { quick: true });
check(activeText() === "bravo", "a non-matching extension leaves the active option alone");

e = key(combo, "Enter");
check(e.defaultPrevented, "Enter is consumed so it does not also click the trigger");
check(repoSelect.value === "bravo" && changes[changes.length - 1] === "bravo", "Enter commits the active option and fires change");
check(!list.classList.contains("show") && combo.getAttribute("aria-expanded") === "false", "Enter closes the list");
check(!combo.hasAttribute("aria-activedescendant"), "a closed list leaves no active descendant");
check(combo.textContent === "bravo", "the trigger shows the chosen option");
check(items().some(i => i.textContent === "bravo" && i.getAttribute("aria-selected") === "true"), "the chosen option is now aria-selected");

e = key(combo, " ");
check(e.defaultPrevented && list.classList.contains("show"), "Space opens the list");
key(combo, "ArrowUp");
const before = changes.length;
e = key(combo, "Escape");
check(!list.classList.contains("show") && repoSelect.value === "bravo" && changes.length === before,
  "Escape closes the list without changing the value");
check(e.defaultPrevented, "Escape is consumed by the open list");
e = key(combo, "Escape");
check(!e.defaultPrevented, "Escape on a closed list is left for the page");

key(combo, "Enter");
check(list.classList.contains("show"), "Enter opens a closed list");
key(combo, "ArrowDown");
key(combo, " ");
check(repoSelect.value === "charlie" && !list.classList.contains("show"), "Space commits the active option");

key(combo, "ArrowDown");
combo.blur();
check(!list.classList.contains("show"), "losing focus closes the list");

// mouse still works
combo.dispatchEvent(mkEvent("click"));
check(list.classList.contains("show"), "clicking the trigger opens the list");
const md = mkEvent("mousedown");
items()[0].dispatchEvent(md);
check(repoSelect.value === "__all__" && !list.classList.contains("show"), "mousedown on an option still selects it");

// themeRefresh keeps the ARIA wiring when the options change
repoSelect.options.push({ value: "delta", textContent: "delta" });
repoSelect.value = "delta";
repoSelect.themeRefresh();
check(combo.textContent === "delta" && items().length === 6 && items()[5].getAttribute("aria-selected") === "true",
  "themeRefresh rebuilds the options with their ARIA state");

// --- help tooltips -----------------------------------------------------------
const tipShown = () => tipEl.classList.contains("show");
check(qmark.tabIndex === 0, "help tooltip trigger is in the tab order");
check(!!qmark.getAttribute("role"), "help tooltip trigger has a role");
check(!!qmark.getAttribute("aria-label") && qmark.getAttribute("aria-label") !== "?", "help trigger has a meaningful name");
check(qmark.getAttribute("aria-describedby") === "tooltipBubble", "help trigger is described by the tooltip");
check(tipEl.getAttribute("role") === "tooltip", "the bubble has role=tooltip");
qmark.focus();
check(tipShown() && tipEl.textContent === "Caps the LIMIT.", "focusing the trigger shows its tooltip");
e = key(qmark, "Escape");
check(!tipShown(), "Escape hides the tooltip");
check(e.defaultPrevented, "Escape that hides a tooltip is consumed");
qmark2.focus();
check(tipShown() && tipEl.textContent === "Second tip.", "moving focus shows the next trigger's tooltip");
qmark2.blur();
check(!tipShown(), "blur hides the tooltip");
qmark.dispatchEvent(mkEvent("mouseenter", { bubbles: false }));
check(tipShown() && tipEl.textContent === "Caps the LIMIT.", "hover still shows the tooltip");
key(combo, "Escape");
check(!tipShown(), "Escape anywhere dismisses a hover tooltip");
qmark.dispatchEvent(mkEvent("mouseenter", { bubbles: false }));
qmark.dispatchEvent(mkEvent("mouseleave", { bubbles: false }));
check(!tipShown(), "mouseleave hides the tooltip");

// --- static checks over the stylesheet and markup -----------------------------------
const css = html.slice(html.indexOf("<style>"), html.indexOf("</style>"));
const rules = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].map(m => ({ sel: m[1].trim(), body: m[2] }));
/* Every rule that renders something fully transparent must be either a
   non-focusable element or a focusable one with a visible focus replacement. */
const NOT_FOCUSABLE = ["#tooltipBubble", "#gitHoverCard", ".copy-toast"];
for (const r of rules.filter(r => /(^|;)\s*opacity:\s*0\s*(;|$)/.test(r.body))) {
  for (const sel of r.sel.split(",").map(s => s.trim())) {
    if (NOT_FOCUSABLE.includes(sel)) { check(true, sel + " at opacity 0 is not focusable"); continue; }
    if (sel === ".csel-native") { check(repoSelect.tabIndex === -1, ".csel-native at opacity 0 is removed from the tab order"); continue; }
    if (sel === ".switch input") {
      check(rules.some(x => /\.switch input:focus-visible/.test(x.sel) && /box-shadow|outline/.test(x.body)),
        ".switch input at opacity 0 gives its visible track a focus ring");
      continue;
    }
    check(false, "unreviewed opacity:0 rule '" + sel + "' -- is it focusable?");
  }
}
for (const r of rules.filter(r => /outline:\s*(none|0)/.test(r.body))) {
  check(/box-shadow|border/.test(r.body), "'" + r.sel + "' removes the outline only with a replacement");
}
const buttons = [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)];
for (const [, attrs, inner] of buttons) {
  const text = inner.replace(/<svg[\s\S]*?<\/svg>/g, "").replace(/<[^>]+>/g, "").trim();
  if (text) continue;
  const id = (attrs.match(/id="([^"]+)"/) || [, "?"])[1];
  check(/aria-label="[^"]+"/.test(attrs), "icon-only button #" + id + " has an aria-label");
}

if (failures) { console.log(failures + " check(s) failed"); process.exit(1); }
console.log("all checks passed");
