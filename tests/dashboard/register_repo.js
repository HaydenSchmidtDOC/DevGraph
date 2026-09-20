/* Headless test of the Repos pane's register-repo submit path, lifted
   verbatim out of index.html. submitRegisterRepo only touches
   document.getElementById, fetch and populateRealRepos, so a handful of stubs
   exercises the real function -- no browser, deterministic, re-runnable.

   The failures this exists to catch are invisible server-side: a double-click
   that registers (or tries to register) twice, a rejected path that also wipes
   what the user typed, a button left disabled after a network error, and
   server-supplied text rendered as HTML. */
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
  "const registerToast = document.getElementById('registerToast');",
  "const btnRegisterRepo = document.getElementById('btnRegisterRepo');",
  "let registerInFlight = false;",
  grab(/^function showRegisterToast\(/m, "\n}"),
  grab(/^async function submitRegisterRepo\(\)/m, "\n}"),
].join("\n");

// --- stubs ------------------------------------------------------------
const els = {
  regRepoPath: { value: "" },
  regRepoId: { value: "" },
  registerToast: { textContent: "", style: {}, classList: { add: () => {} } },
  btnRegisterRepo: { disabled: false },
};
let fetchCalls = [];
let repoRefreshes = 0;
let respond = async () => ({ ok: true, status: 201, json: async () => ({}) });
const sandboxGlobals = {
  document: { getElementById: id => els[id] || null },
  fetch: async (url, opts) => { fetchCalls.push({ url, opts }); return respond(); },
  populateRealRepos: async () => { repoRefreshes++; },
  console,
};
const api = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { submitRegisterRepo };")(...Object.values(sandboxGlobals));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const reset = (pathValue, idValue) => {
  els.regRepoPath.value = pathValue === undefined ? "/home/dev/projects/rag4" : pathValue;
  els.regRepoId.value = idValue === undefined ? "" : idValue;
  els.registerToast.textContent = "";
  els.registerToast.style = {};
  els.btnRegisterRepo.disabled = false;
  fetchCalls = [];
  repoRefreshes = 0;
};
const ok = body => async () => ({ ok: true, status: 201, json: async () => body });

(async () => {
  // 1. a successful POST carries JSON with just the path when no id is typed
  reset();
  respond = ok({ repo_id: "rag4", path: "/home/dev/projects/rag4", registered: true, indexed: true, files_indexed: 42, warning: null });
  await api.submitRegisterRepo();
  const call = fetchCalls[0];
  check("POSTs to /api/repos", call && call.url === "/api/repos" && call.opts.method === "POST",
    JSON.stringify(fetchCalls));
  check("declares a JSON content type so the server's media-type gate passes",
    call && /application\/json/.test(call.opts.headers["Content-Type"]), JSON.stringify(call && call.opts.headers));
  check("sends the trimmed path and omits repo_id when it was left blank",
    call && JSON.stringify(JSON.parse(call.opts.body)) === JSON.stringify({ path: "/home/dev/projects/rag4" }),
    call && call.opts.body);
  check("reports the server's authoritative repo_id and file count, not the typed input",
    /rag4/.test(els.registerToast.textContent) && /42/.test(els.registerToast.textContent),
    els.registerToast.textContent);
  check("refreshes the live repo list after a successful registration", repoRefreshes === 1, repoRefreshes);
  check("clears the form on success", els.regRepoPath.value === "" && els.regRepoId.value === "",
    els.regRepoPath.value + "|" + els.regRepoId.value);
  check("re-enables the button on success", els.btnRegisterRepo.disabled === false, els.btnRegisterRepo.disabled);

  // 2. the authoritative id wins even when the server had to change it
  reset(" /home/dev/projects/rag4 ", " rag4 ");
  respond = ok({ repo_id: "rag4-2", path: "/home/dev/projects/rag4", registered: true, indexed: true, files_indexed: 1, warning: null });
  await api.submitRegisterRepo();
  check("trims both fields before sending",
    JSON.stringify(JSON.parse(fetchCalls[0].opts.body)) === JSON.stringify({ path: "/home/dev/projects/rag4", repo_id: "rag4" }),
    fetchCalls[0].opts.body);
  check("displays the id the registry actually assigned",
    /rag4-2/.test(els.registerToast.textContent), els.registerToast.textContent);

  // 3. partial success: registered but not indexed
  reset();
  respond = ok({ repo_id: "rag4", path: "/home/dev/projects/rag4", registered: true, indexed: false, files_indexed: null,
    warning: "Registered, but the initial scan failed: Neo4j unreachable. Run 'devgraph rescan rag4' once Neo4j is reachable." });
  await api.submitRegisterRepo();
  check("surfaces the partial-success warning verbatim",
    /initial scan failed/.test(els.registerToast.textContent) && /devgraph rescan rag4/.test(els.registerToast.textContent),
    els.registerToast.textContent);
  check("does not claim indexing succeeded", !/indexed 0/i.test(els.registerToast.textContent),
    els.registerToast.textContent);
  check("still refreshes the repo list -- the repo IS registered", repoRefreshes === 1, repoRefreshes);

  // 4. client-side validation short-circuits without a request
  reset("   ", "rag4");
  await api.submitRegisterRepo();
  check("a blank path never reaches the network", fetchCalls.length === 0, JSON.stringify(fetchCalls));
  check("a blank path still says why nothing happened", els.registerToast.textContent !== "",
    els.registerToast.textContent);
  check("a blank path leaves the button usable", els.btnRegisterRepo.disabled === false, els.btnRegisterRepo.disabled);

  // 5. a server rejection shows the reason and keeps the typed input
  reset("/not/a/repo");
  respond = async () => ({ ok: false, status: 400, json: async () => ({ detail: "not a git repository: /not/a/repo" }) });
  await api.submitRegisterRepo();
  check("shows the server's reason for rejecting", /not a git repository/.test(els.registerToast.textContent),
    els.registerToast.textContent);
  check("keeps the typed path so it can be corrected", els.regRepoPath.value === "/not/a/repo", els.regRepoPath.value);
  check("does not refresh the repo list after a rejection", repoRefreshes === 0, repoRefreshes);
  check("re-enables the button after a rejection", els.btnRegisterRepo.disabled === false, els.btnRegisterRepo.disabled);

  // 6. a non-JSON error body (proxy/HTML error page) must not throw
  reset("/home/dev/projects/rag4");
  respond = async () => ({ ok: false, status: 500, json: async () => { throw new Error("not json"); } });
  await api.submitRegisterRepo();
  check("falls back to the status code when the error body is not JSON",
    /500/.test(els.registerToast.textContent), els.registerToast.textContent);
  check("re-enables the button after an unparseable error", els.btnRegisterRepo.disabled === false,
    els.btnRegisterRepo.disabled);

  // 7. a network failure is reported, not swallowed
  reset();
  respond = async () => { throw new Error("Failed to fetch"); };
  await api.submitRegisterRepo();
  check("reports a network failure", /Failed to fetch/.test(els.registerToast.textContent),
    els.registerToast.textContent);
  check("re-enables the button after a network failure", els.btnRegisterRepo.disabled === false,
    els.btnRegisterRepo.disabled);
  check("keeps the typed path after a network failure", els.regRepoPath.value !== "", els.regRepoPath.value);

  // 8. a double click cannot register twice
  reset();
  let release;
  const gate = new Promise(r => { release = r; });
  respond = async () => { await gate; return { ok: true, status: 201, json: async () => ({ repo_id: "rag4", indexed: true, files_indexed: 3 }) }; };
  const first = api.submitRegisterRepo();
  const second = api.submitRegisterRepo();
  check("the button is disabled while a registration is in flight", els.btnRegisterRepo.disabled === true,
    els.btnRegisterRepo.disabled);
  check("a second click while in flight issues no second POST", fetchCalls.length === 1,
    JSON.stringify(fetchCalls.map(c => c.url)));
  release();
  await Promise.all([first, second]);
  check("still exactly one POST after both settle", fetchCalls.length === 1, fetchCalls.length);
  check("a later submit is allowed again", els.btnRegisterRepo.disabled === false, els.btnRegisterRepo.disabled);

  // 9. server text is never rendered as markup
  // the assignment, not the bare word -- the source comment naming innerHTML is not a defect
  check("the register path never assigns innerHTML", !/\.innerHTML\s*\+?=/.test(src),
    "server-supplied text reaching innerHTML would be parsed as HTML");
  check("the toast is written through textContent", /textContent/.test(src), src);

  // 10. the removed prototype affordances must not come back
  check("the unimplemented incremental/full selector is gone from the markup",
    !/rebuildModeToggle/.test(html), "the segmented control it drove was never wired to anything");
  check("the register form no longer says nothing persists",
    !/not yet persisted/.test(html), "the form does persist now");
  check("the Repos pane no longer calls repo add CLI-only",
    !/Repo add\/remove stays CLI-only/.test(html), "registration is available in the dashboard now");

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
