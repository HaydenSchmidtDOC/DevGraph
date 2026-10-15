/* Headless test that typing in the Cypher console can never execute a write,
   nor stack previews on Neo4j. Drives the real input listener,
   schedulePreview, previewQueryGlow and runCypher out of index.html against a
   recording fetch: every request the live preview sends must carry
   `preview: true`, which the server runs in a read transaction, and at most
   one is in flight at a time. */
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
  grab(/^async function runCypher\(statement, opts\)/m, "\n}"),
  grab(/^let previewDebounce = null;/m, ";"),
  grab(/^let glowRequestSeq = 0;/m, ";"),
  grab(/^let previewInFlight = null;/m, ";"),
  grab(/^let previewQueued = null;/m, ";"),
  grab(/^let previewQueuedSeq = 0;/m, ";"),
  grab(/^function schedulePreview\(text\)/m, "\n}"),
  grab(/^async function previewQueryGlow\(text\)/m, "\n}"),
  grab(/^cypherInput\.addEventListener\("input"/m, "\n});"),
].join("\n");

const bodies = [];
let inputHandler = null;
const cypherInput = {
  value: "",
  addEventListener: (ev, fn) => { if (ev === "input") inputHandler = fn; },
};
const pending = [];
// When set, each fetch waits for its own release() before replying.
let held = null;
let inFlight = 0, maxInFlight = 0;
const globals = {
  document: { getElementById: id => (id === "repoSelect" ? { value: "zz-repo" } : null) },
  fetch: async (url, opts) => {
    bodies.push(JSON.parse(opts.body));
    inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
    if (held) await new Promise(release => held.push(release));
    inFlight--;
    return { json: async () => ({ results: [], errors: [{ code: "Forbidden", message: "read only" }] }) };
  },
  setTimeout: fn => { pending.push(fn); return pending.length; },
  clearTimeout: () => {},
  cypherInput,
  syncCypherHighlight: () => {},
  syncSlidersFromCypherText: () => {},
  extractGraphIds: () => ({ nodeIds: new Set(), edgeIds: new Set() }),
  animateGlowTo: () => {},
  focusRotation: () => {},
  cy: {},
  neo4jConnected: true,
  console,
};
const api = new Function(...Object.keys(globals),
  src + "\nreturn { runCypher, previewQueryGlow, otherRequest: () => ++glowRequestSeq };")(...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

(async () => {
  // 1. Typing a destructive query, keystroke by keystroke, with the debounce
  //    firing after each one (the worst case: a slow typist).
  const typed = "MATCH (n) DETACH DELETE n";
  for (let k = 1; k <= typed.length; k++) {
    cypherInput.value = typed.slice(0, k);
    inputHandler();
    while (pending.length) await pending.shift()();
  }
  check("typing sent preview requests", bodies.length > 0, "no request was made");
  check("every request sent while typing is a read-only preview",
    bodies.every(b => b.preview === true), JSON.stringify(bodies.filter(b => b.preview !== true)));
  check("no preview is recorded in the query log",
    bodies.every(b => !b.record), JSON.stringify(bodies));

  // 1b. Pauses in typing while a preview is still running on the server: no
  //     second query is sent beside it, the texts in between are never sent,
  //     and the newest one runs once the first returns.
  bodies.length = 0;
  held = [];
  maxInFlight = 0;
  const settled = [];
  for (const text of ["MATCH (a) RETURN a", "MATCH (b) RETURN b", "MATCH (c) RETURN c"]) {
    cypherInput.value = text;
    inputHandler();
    settled.push(pending.shift()());
  }
  await new Promise(r => setTimeout(r, 0));
  check("one preview in flight while typing continues", bodies.length === 1 && maxInFlight === 1,
    JSON.stringify(bodies));
  while (held.length) { held.shift()(); await new Promise(r => setTimeout(r, 0)); }
  await Promise.all(settled);
  check("only the first and the newest text are previewed",
    JSON.stringify(bodies.map(b => b.query)) === JSON.stringify(["MATCH (a) RETURN a", "MATCH (c) RETURN c"]),
    JSON.stringify(bodies.map(b => b.query)));
  check("never two previews at once", maxInFlight === 1, `max in flight ${maxInFlight}`);
  // 1c. A Run or canvas refresh issued after a preview was queued is newer:
  //     the queued preview is dropped, so it can't supersede that reply.
  bodies.length = 0;
  maxInFlight = 0;
  settled.length = 0;
  for (const text of ["MATCH (d) RETURN d", "MATCH (e) RETURN e"]) {
    cypherInput.value = text;
    inputHandler();
    settled.push(pending.shift()());
  }
  await new Promise(r => setTimeout(r, 0));
  api.otherRequest();  // what runQueryAndUpdateGraph does as it starts
  while (held.length) { held.shift()(); await new Promise(r => setTimeout(r, 0)); }
  await Promise.all(settled);
  check("a preview queued before a Run is not sent after it",
    JSON.stringify(bodies.map(b => b.query)) === JSON.stringify(["MATCH (d) RETURN d"]),
    JSON.stringify(bodies.map(b => b.query)));
  held = null;

  // 2. Run (and everything else that calls runCypher without opts) is not a preview.
  bodies.length = 0;
  await api.runCypher("MATCH (n) RETURN n", { record: true });
  check("runCypher without preview does not ask for one",
    bodies.length === 1 && !bodies[0].preview && bodies[0].record === true, JSON.stringify(bodies));

  // 3. Only text that opens with a plain read clause is previewed: a read
  //    transaction still runs LOAD CSV (fetches a URL), SHOW, TERMINATE and
  //    procedure CALLs.
  const previewed = async text => {
    bodies.length = 0;
    await api.previewQueryGlow(text);
    return bodies.length > 0;
  };
  for (const text of [
    "MATCH (n) RETURN n", "  optional match (n) return n", "WITH 1 AS x RETURN x", "RETURN 1",
    "UNWIND [1,2] AS x RETURN x", "// a note\nMATCH (n) RETURN n",
  ]) check(`previews ${JSON.stringify(text)}`, await previewed(text), "no request");
  for (const text of [
    "LOAD CSV FROM 'http://example.test/x.csv' AS row RETURN row", "CALL db.labels()",
    "SHOW TRANSACTIONS", "TERMINATE TRANSACTIONS 'x'", "MATCH (n) CALL apoc.load.json('http://example.test') YIELD value RETURN value",
    "WITH 1 AS x LOAD CSV FROM 'file:///etc/passwd' AS row RETURN row", "match (n) call { return 1 as y } return n",
    "CREATE (n) RETURN n", "EXPLAIN MATCH (n) RETURN n",
  ]) check(`does not preview ${JSON.stringify(text)}`, !(await previewed(text)), JSON.stringify(bodies));

  // 4. The row-cap notice: shown for a capped result, cleared by an uncapped
  //    one (a canvas refresh), and an error shown since is left alone.
  const errorEl = { textContent: "" };
  const notice = new Function("cypherErrorEl",
    grab(/^let shownTruncatedNotice = null;/m, ";") + "\n" + grab(/^function showTruncatedNotice\(result\)/m, "\n}") +
    "\nreturn showTruncatedNotice;")(errorEl);
  notice({ truncated: true, data: new Array(3) });
  check("a capped result shows the notice", errorEl.textContent.startsWith("Showing the first 3 rows"), errorEl.textContent);
  notice({ truncated: false, data: [] });
  check("an uncapped result clears it", errorEl.textContent === "", errorEl.textContent);
  notice({ truncated: true, data: [] });
  errorEl.textContent = "ClientError: bad query";
  notice({ truncated: false, data: [] });
  check("an error shown since is not cleared", errorEl.textContent === "ClientError: bad query", errorEl.textContent);

  if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
  console.log("\nall checks passed");
})();
