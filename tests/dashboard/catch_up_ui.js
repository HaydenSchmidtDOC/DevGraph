/* Headless test of the Entities card's catch-up pill (spec W9), lifted
   verbatim out of index.html's "Catch-up pill" block: the pure reducer
   `catchUpPillState` and `applyCatchUpPill`, which writes its label into
   #entityLivePill. The pill's innerHTML setter throws, so a repository id or
   label can only ever reach it as text. */
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");
const start = html.search(/^\/\* ── Catch-up pill ── \*\//m);
const end = html.indexOf("/* ── end Catch-up pill ── */", start);
if (start < 0 || end < 0) throw new Error("could not find the Catch-up pill block");

const pill = {
  textContent: "Live", style: { display: "none" },
  get innerHTML() { throw new Error("the pill's innerHTML was read"); },
  set innerHTML(v) { throw new Error("the pill's innerHTML was written: " + v); },
};
const select = { value: "alpha" };
const document = {
  getElementById: id => ({ entityLivePill: pill, repoSelect: select }[id] ||
    (() => { throw new Error("unexpected element #" + id); })()),
};
const api = new Function("document",
  html.slice(start, end) + "\nreturn { catchUpPillState, applyCatchUpPill, setEntityPillTopology };")(document);
const { catchUpPillState, applyCatchUpPill, setEntityPillTopology } = api;

const ev = (repo_id, state) => ({ type: "catch_up", repo_id, state, changed: 0, deleted: 0 });

// the reducer
let s = catchUpPillState([], ev("alpha", "running"), "alpha");
assert.deepStrictEqual(s, { running: ["alpha"], label: "Catching up…" });
for (const closing of ["done", "failed"]) {
  assert.deepStrictEqual(catchUpPillState(s.running, ev("alpha", closing), "alpha"), { running: [], label: "Live" });
}
// another repository's catch-up leaves the selected repository's pill alone ...
const other = catchUpPillState([], ev("beta", "running"), "alpha");
assert.deepStrictEqual(other, { running: ["beta"], label: "Live" });
// ... unless every repository is shown
assert.strictEqual(catchUpPillState([], ev("beta", "running"), "__all__").label, "Catching up…");
assert.strictEqual(catchUpPillState(["alpha", "beta"], ev("beta", "done"), "__all__").label, "Catching up…");
assert.strictEqual(catchUpPillState(["beta"], ev("beta", "done"), "__all__").label, "Live");
// a repeated "running" counts once; other event types change nothing
assert.deepStrictEqual(catchUpPillState(["alpha"], ev("alpha", "running"), "alpha").running, ["alpha"]);
assert.deepStrictEqual(
  catchUpPillState(["alpha"], { type: "reindexed", repo_id: "alpha", changed: 1, deleted: 0 }, "alpha"),
  { running: ["alpha"], label: "Catching up…" });
// a null event only recomputes the label, as on a repository switch
assert.strictEqual(catchUpPillState(["beta"], null, "beta").label, "Catching up…");
// pure: the list passed in is never changed
const before = ["alpha"];
catchUpPillState(before, ev("alpha", "done"), "alpha");
catchUpPillState(before, ev("beta", "running"), "alpha");
assert.deepStrictEqual(before, ["alpha"]);

// applied to the pill, with repository-controlled text
const nasty = "<img src=x onerror=alert(1)>";
select.value = nasty;
applyCatchUpPill(ev(nasty, "running"));
assert.strictEqual(pill.textContent, "Catching up…");
assert.strictEqual(pill.style.display, "inline-block");
applyCatchUpPill(ev("beta", "running"));
assert.strictEqual(pill.textContent, "Catching up…");
applyCatchUpPill(ev(nasty, "done"));
assert.strictEqual(pill.textContent, "Live");
select.value = "beta";
applyCatchUpPill(null);
assert.strictEqual(pill.textContent, "Catching up…");
applyCatchUpPill(ev("beta", "failed"));
assert.strictEqual(pill.textContent, "Live");

// a pill hidden before the catch-up (Neo4j disconnected) is hidden again after it ...
setEntityPillTopology("none");
assert.strictEqual(pill.style.display, "none");
applyCatchUpPill(ev("beta", "running"));
assert.strictEqual(pill.style.display, "inline-block");
applyCatchUpPill(ev("beta", "done"));
assert.strictEqual(pill.style.display, "none");
assert.strictEqual(pill.textContent, "Live");
// ... and one already showing stays shown
setEntityPillTopology("inline-block");
applyCatchUpPill(ev("beta", "running"));
applyCatchUpPill(ev("beta", "failed"));
assert.strictEqual(pill.style.display, "inline-block");
// Neo4j reconnects mid-catch-up: the end shows the pill as the topology now
// has it, not as it was when the catch-up began
setEntityPillTopology("none");
applyCatchUpPill(ev("beta", "running"));
setEntityPillTopology("inline-block");
assert.strictEqual(pill.textContent, "Catching up…");
applyCatchUpPill(ev("beta", "done"));
assert.strictEqual(pill.style.display, "inline-block");
assert.strictEqual(pill.textContent, "Live");
// Neo4j disconnects mid-catch-up: the catch-up keeps the pill showing, then it hides
applyCatchUpPill(ev("beta", "running"));
setEntityPillTopology("none");
assert.strictEqual(pill.style.display, "inline-block");
applyCatchUpPill(ev("beta", "done"));
assert.strictEqual(pill.style.display, "none");

console.log("catch_up_ui: ok");
