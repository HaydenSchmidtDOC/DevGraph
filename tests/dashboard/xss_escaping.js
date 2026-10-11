/* Headless check that every dashboard renderer which writes graph, git,
   filesystem or registry text through innerHTML escapes it. The functions are
   lifted verbatim out of index.html and driven with hostile strings: a commit
   message, author, path, node name or repo id is attacker-controlled the
   moment someone indexes a repository they did not write, and the dashboard
   runs same-origin with an endpoint that executes arbitrary Cypher.

   A renderer passes when its markup contains neither a live tag from the
   payload nor an attribute breakout. */
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
  grab(/^let COMMITS = \[\];/m, "let treeConnectors = [];"),
  grab(/^function computeLanes\(/m, "\n}"),
  grab(/^function renderGitList\(/m, "\n}"),
  grab(/^let treeBaseW\b/m, ";"),
  grab(/^function renderTreeLegend\(/m, "\n}"),
  grab(/^function renderGitTree\(/m, "\n}"),
  "let gitCardHideTimer = null;",
  grab(/^function showGitCard\(/m, "\n}"),
  "let logRows = [];",
  grab(/^function renderLog\(/m, "\n"),
  grab(/^function insightRowsHtml\(/m, "\n}"),
  grab(/^function renderAC\(/m, "\n}"),
  grab(/^function escapeHtml\(/m, "}"),
  grab(/^function highlightCypher\(/m, "\n}"),
].join("\n");

// --- DOM stubs: every element records what was written into it ----------
const written = [];
const mkEl = () => {
  const el = {
    _html: "", style: {}, dataset: {}, children: [],
    textContent: "",
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {},
    addEventListener() {},
    appendChild(c) { el.children.push(c); },
    querySelector: () => null,
    get innerHTML() { return el._html; },
    set innerHTML(v) { el._html = v; written.push(v); },
  };
  return el;
};
const els = {};
const byId = id => (els[id] = els[id] || mkEl());
const globals = {
  document: { getElementById: byId, createElement: () => mkEl() },
  window: { innerWidth: 1200, innerHeight: 800 },
  gitHoverCard: byId("gitHoverCard"),
  logBody: byId("logTableBody"),
  ac: byId("autocomplete"),
  loadQueryFromHistory: () => {},
  console,
};
const api = new Function(...Object.keys(globals), src + `
return {
  escapeHtmlVal, highlightCypher, renderGitList, renderGitTree, showGitCard, renderLog, insightRowsHtml, renderAC,
  setGit: (commits, uncommitted) => {
    COMMITS = commits; UNCOMMITTED = uncommitted;
    COMMITS_BY_HASH = Object.fromEntries(commits.map(c => [c.hash, c]));
    computeLanes(commits);
  },
  setLog: rows => { logRows = rows; },
  setAc: items => { acItems = items; acIndex = 0; },
};`.replace("return {", "let acItems = [], acIndex = -1;\nreturn {"))(...Object.values(globals));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
/* Tag injection (`<img ... onerror=`) and attribute breakout (`" onmouseover="`
   inside a quoted attribute) are the two ways a string escapes into markup. */
const EVIL = `x" onmouseover="alert(1)" y='<img src=x onerror=alert(2)>`;
const safe = markup => !/<img/i.test(markup) && !/" onmouseover="/.test(markup) && !/<script/i.test(markup);
const lastWrites = n => written.slice(-n).join("\n");
const collect = () => { const out = written.join("\n"); written.length = 0; return out; };

// 1. escapeHtmlVal itself covers text AND quoted-attribute contexts.
{
  const out = api.escapeHtmlVal(EVIL);
  check("escapeHtmlVal escapes < > & and both quote characters",
    !/[<>"']/.test(out) && out.includes("&quot;") && out.includes("&#39;"), out);
}

// 2. Git list: title, body, author, hashes and uncommitted paths.
{
  collect();
  api.setGit([{
    hash: EVIL, short_hash: EVIL, title: EVIL, body: EVIL, author: EVIL,
    date: "2026-01-01T00:00:00Z", merge: false, parents: [],
  }], [{ path: EVIL, state: EVIL }]);
  api.renderGitList();
  const out = collect();
  check("renderGitList escapes commit title, body, author, hash and paths", safe(out), out);
}

// 3. Git tree: labels and data-hash attributes.
{
  api.renderGitTree();
  const out = collect();
  check("renderGitTree escapes commit hashes and titles in the SVG", safe(out), out);
}

// 4. Hover card: hash, title, body.
{
  api.showGitCard(EVIL, 10, 10);
  const out = collect();
  check("showGitCard escapes hash, title and body", safe(out), out);
}

// 5. Console history: every column.
{
  api.setLog([{ t: EVIL, tool: EVIL, repo: EVIL, cypher: EVIL }]);
  api.renderLog("");
  const out = collect();
  check("renderLog escapes time, tool, repo and query", safe(out), out);
}

// 6. Insight leaderboards: name, file (also used as a title attribute), tag, value.
{
  const out = api.insightRowsHtml([{ name: EVIL, file: "src/" + EVIL, title: EVIL, tag: EVIL, value: EVIL }]);
  check("insightRowsHtml escapes names, files and the title attribute", safe(out), out);
}

// 7. Autocomplete: schema labels and the kind badge.
{
  api.setAc([{ text: EVIL, kind: EVIL }]);
  api.renderAC();
  const out = collect();
  check("renderAC escapes candidate text and kind", safe(out), out);
}

// 8. The degree leaderboard lives inside a large async loader; pin its row
//    template so the count column cannot regress to a raw interpolation.
{
  const line = html.split("\n").find(l => l.includes("godEl.innerHTML = godJson.results[0].data.map"));
  check("degree leaderboard escapes every column",
    !!line && !/\$\{d\.row\[\d\]\}/.test(line), line);
}

// 9. The Cypher highlighter: quotes escaped, and the markup it builds shows
//    exactly the query text (no garbled attributes, no live tags).
{
  const strip = m => m.replace(/<span class="tok-[a-z]+">/g, "").replace(/<\/span>/g, "")
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  for (const q of [
    `MATCH (n {name: "a<img src=x onerror=alert(1)>"}) RETURN n LIMIT 25`,
    `MATCH (n) // it's "quoted" here\nWHERE n.name = 'x"y' RETURN n`,
    `RETURN "a" + 'b' AS s, 'x' AS t, 39 AS n`,
  ]) {
    const out = api.highlightCypher(q);
    check(`highlightCypher round-trips ${JSON.stringify(q)}`, strip(out) === q, out);
    check(`highlightCypher emits no live tag for ${JSON.stringify(q)}`, safe(out) && !/<(?!\/?span\b)/.test(out), out);
  }
  check("string literals are still highlighted", /<span class="tok-str">&quot;a&quot;<\/span>/.test(api.highlightCypher(`RETURN "a"`)),
    api.highlightCypher(`RETURN "a"`));
}

if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
console.log("\nall checks passed");
