/* Headless test of the header's repo selector (populateRealRepos), lifted
   verbatim out of index.html: a repository the agent's watcher isn't
   watching (an inotify limit reached) is flagged, with the reason, so the
   graph isn't mistaken for a live one. */
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
  'const SELECTED_REPO_KEY = "devgraph.selectedRepo";',
  grab(/^function escapeHtmlVal\(/m, "\n"),
  grab(/^async function populateRealRepos\(/m, "\n}"),
].join("\n");

const select = { innerHTML: "", value: "", dispatchEvent: () => {} };
let repos = [];
const sandboxGlobals = {
  document: { getElementById: id => (id === "repoSelect" ? select : null) },
  fetch: async () => ({ ok: true, json: async () => ({ repos }) }),
  localStorage: { getItem: () => null },
  Event: function Event() {},
  console,
};
const api = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { populateRealRepos };")(...Object.values(sandboxGlobals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

(async () => {
  const issue = 'watching unavailable: inotify limit reached (fs.inotify.max_user_instances) <"x">';
  repos = [
    { repo_id: "live", active: true, path_missing: false, issue: null },
    { repo_id: "stuck", active: true, path_missing: false, issue },
    { repo_id: "gone", active: true, path_missing: true, issue: "path missing: repository folder not found: /x" },
  ];
  await api.populateRealRepos(false);
  const option = id => (select.innerHTML.match(new RegExp(`<option value="${id}"[^>]*>[^<]*</option>`)) || [""])[0];
  check("an unwatched repository is flagged", />stuck \(not watched\)</.test(option("stuck")), option("stuck"));
  check("its reason is the option's tooltip, escaped",
    option("stuck").includes('title="watching unavailable: inotify limit reached (fs.inotify.max_user_instances) &lt;&quot;x&quot;&gt;"'),
    option("stuck"));
  check("a watched repository is not flagged", />live</.test(option("live")) && !/title=/.test(option("live")), option("live"));
  check("a missing folder still reads as path missing", />gone \(path missing\)</.test(option("gone")), option("gone"));
  if (failures) { console.log(`\n${failures} check(s) failed`); process.exit(1); }
  console.log("\nall checks passed");
})();
