# MCP Repo Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Every built-in MCP tool may omit `repo_id`. It then runs against the session's repository, or it fails with an error that lists the registered repositories. A dict response says which repository was used; the two list-returning built-ins rely on the server instructions instead. Calls that pass `repo_id` are unchanged byte for byte.

**Spec:** `docs/superpowers/specs/2026-10-08-mcp-repo-default-design.md`. Every task implements the decisions it names (R1–R7).

**Working directory:** this worktree, branch `epic1/mcp-repo-default` (from `epic1/watcher`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). None of these tests need Neo4j. Never touch `~/.devgraph`.

## Global Constraints

- **Explicit wins, untouched (R1).** With `repo_id` present and not `None`, the wrapper calls the tool with the caller's kwargs and returns the very object the tool returned. It does not validate, normalise, add a key or add a notice, even when the value equals the session repository.
- **One resolution (R2).** The default is `build_server`'s `session_repo`. Nothing calls `resolve_session_repo` again, nothing reads a file in the repository, and nothing falls back to the cwd after an unmatched pin.
- **Active check (R1.2).** A defaulted call makes exactly one `registry.list_repos(active_only=True)` read. If the session repository's id is not in it, the call raises the R1.3 error with the reason "session repository '<id>' is no longer registered or active". An explicit call never reads the registry.
- **Never "any repo" (R6).** With no session repository, a call without `repo_id` raises `ToolError` before the tool body runs. That holds even with exactly one registered repository.
- **Schema delta is exactly R3.**
  - For each of the 24 built-ins: `repo_id` leaves `required`, and its property is `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Repo Id"}`.
  - Everything else in every listed tool is equal to what the original function's signature produces: the other properties and their order, the rest of `required`, descriptions, annotations and names.
  - `run_cypher`, project tools and global tools are unchanged.
- **Tool definitions unchanged (R4).** The 24 `@server.tool` functions keep `repo_id: str` and their bodies. The change lives in one wrapper inside `_instrumented_tool`.
- **Envelopes kept (R5).** A defaulted dict response gains only `repo_id` and one `notices` entry (added before the shadow notices, never replacing them: the default wrapper is innermost). A defaulted list response is unchanged.
- **Telemetry unchanged.** No argument and no defaulted id is ever recorded. `_TELEMETRY_FIELDS` and the existing telemetry tests are untouched. A default error is recorded as `ok: false`.
- **Messages** use the spec's R1.3 and R5 wording.
- **No new config knob, no new dependency.**
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **Byte-for-byte explicit calls.** The characterization test is written and green *before* any production change (Task 1, first step), and it stays green, unedited, through the end. Test: `test_explicit_calls_are_unchanged`.
2. **Every built-in covered.**
   - The parametrized list is derived from the live server: every listed tool whose original signature has `repo_id`, checked to be exactly 24.
   - It is not a hand-kept list that a new built-in could miss.
   - The minimal-arguments table must cover every one of them; a missing entry fails the test.

   Test: Task 1 `BUILTINS_WITH_REPO_ID` guard.
3. **No widening beyond R3.** Compare each listed schema with the schema of `inspect.unwrap(tool.fn)`'s own signature. Test: `test_only_repo_id_became_optional`.
4. **No fallback.** Each of these errors, and never runs the tool:
   - an unmatched pin;
   - an inactive pin;
   - no cwd match;
   - one registered repository;
   - a session repository that is no longer active.

   The error lists the repositories and carries the restart hint. Tests: Task 1 `test_removed_session_repo_errors`, Task 2 resolution branches.
5. **Notices compose.** A built-in that is both shadowed and defaulted carries both notices, shadow first. Test: Task 2 `test_shadow_and_default_notices_coexist`.

### Task 1: Optional `repo_id` and the session default (R1.1, R1.2, R2, R3, R4, R5)

**Files:**
- `devgraph/mcp/server.py`:
  - **`_with_repo_default(fn, session_repo, session_source, registry, unscoped_error)`**:
    - it returns `fn` unchanged when `fn` has no `repo_id` parameter;
    - otherwise it is a `functools.wraps` wrapper whose `__signature__` is `fn`'s evaluated signature with every parameter keyword-only and `repo_id: str | None = None`;
    - on the default path it reads `registry.list_repos(active_only=True)` once. If the session repository is active, it substitutes `session_repo.repo_id` and adds the R5 `repo_id` key and notice to a dict result;
    - when there is no session repository, or it is no longer active, it raises `unscoped_error(reason, active_repos)`. That is the full R1.3 message (reason, the active repositories as `id (name)` or "none registered" with `devgraph add`, and the restart hint), built here in Task 1 so that no commit ships a placeholder message.
  - **`build_server`**: it builds the R1.3 reason from `session_source`, `session_pinned` and `_inactive_match(registry, pinned)` (imported from `tool_plane`, which moves nothing).
  - **`_instrumented_tool`**: it applies `_with_repo_default` innermost.
- `tests/mcp/test_repo_default.py` (new). It uses the stub `Repo`/`Registry`/`Engine` pattern of `tests/mcp/test_tool_plane.py`, and `Settings(registry_db_path=tmp_path/...)` via `monkeypatch.setattr(mcp_server, "get_settings", ...)`.
  - **Stub every built-in body.** Monkeypatch each `devgraph_tools.<name>` that a built-in calls with a recorder. It records `(name, args, kwargs)` and returns a fixed payload:
    - a dict `{"count": 1, "results": [{"name": "X"}], "truncated": False}` for dict tools;
    - `[{"name": "X"}]` for `find_requirements_for` and `blame_component`.

    Also stub `devgraph_tools.declared_node_labels`. The tests then exercise the server layer only, and no `gh` CLI or git runs.
  - **`MIN_ARGS`.** A dict from each built-in to its minimal required non-`repo_id` arguments, for example:
    - `search_component`: `{"query": "X"}`;
    - `list_recent_changes`: `{"within_commits": 5}`;
    - `compare_branches`: `{"branch_a": "a", "branch_b": "b"}`;
    - `impact_analysis_for_diff`: `{"base_ref": "a", "head_ref": "b"}`;
    - `find_mentions`: `{"name": "X"}`.
  - **`registered_fn(server, name)`.** A test helper and the only place that touches SDK internals. It returns `server._tool_manager._tools[name].fn`. If `_tool_manager` or `_tools` is missing, it fails with `pytest.fail("MCPServer internals changed (written against mcp 2.3.0): ...")` rather than an AttributeError deep in a test. A comment names the SDK version.
  - **`BUILTINS_WITH_REPO_ID`.** Computed from `asyncio.run(server.list_tools())` and `registered_fn` for each tool: the names whose `inspect.signature(inspect.unwrap(fn))` has `repo_id`. Built with `enable_run_cypher=True`, so `run_cypher` is present and must be excluded.

- [ ] **First, a characterization test, green on the unchanged code. Commit it alone** as "Pin explicit built-in MCP calls":
  - **`test_explicit_calls_are_unchanged`** is parametrized over every name in `BUILTINS_WITH_REPO_ID`.
    - Build a server with `session_repo=Repo("demo", ...)`, `session_source="env"`, and another with `session_repo=None`.
    - Call each tool with `{"repo_id": "other", **MIN_ARGS[name]}`.
    - Assert, for each server:
      - `is_error is False`;
      - the recorder saw the same positional and keyword arguments, with `"other"` as `repo_id`;
      - `structured_content` equals the fixed payload exactly: no `repo_id` key and no `notices`. A list tool's is `{"result": payload}`.
    - Assert `result.model_dump_json()` is identical across the two servers.
    - Repeat with `repo_id="demo"`, the session's own id. The result still has no added key.
    - Repeat with `repo_id=""`. It is passed through as `""`.
  - **`test_builtins_with_repo_id_are_exactly_the_24`** checks that:
    - `len(BUILTINS_WITH_REPO_ID) == 24`;
    - `"run_cypher"` is not in it;
    - `set(MIN_ARGS) == BUILTINS_WITH_REPO_ID`.

  Run `uv run pytest -q tests/mcp/test_repo_default.py`. It must pass before you touch `server.py`.
- [ ] Write failing tests (parametrized over `BUILTINS_WITH_REPO_ID`):
  - **`test_only_repo_id_became_optional`.**
    - `original = func_metadata(inspect.unwrap(fn)).arg_model.model_json_schema()`, with `func_metadata` from `mcp.server.mcpserver.utilities.func_metadata`.
    - The listed `inputSchema` equals `original`, except that:
      - `required` is `original["required"]` minus `"repo_id"` (the key is absent when that leaves it empty, matching the SDK);
      - `properties["repo_id"]` is the R3 value.
    - The property order is unchanged (`list(properties) == list(original["properties"])`).
  - **`test_omitted_repo_id_uses_the_session_repo`.**
    - With `session_repo=Repo("demo")` and `session_source="env"`, call with `MIN_ARGS[name]` only.
    - The recorder saw `"demo"` exactly where an explicit call puts `repo_id`.
    - **Dict tools.** `structured_content == {**payload, "repo_id": "demo", "notices": ["repo_id not given; used this session's repository 'demo' (from DEVGRAPH_MCP_REPO)"]}`.
    - **List tools.** `structured_content == {"result": payload}`.
  - **`test_defaulted_list_payload_is_unchanged`.** This test is for `find_requirements_for` and `blame_component`. With and without `repo_id`, `model_dump_json()` of the results is identical, and neither result has a `repo_id` or `notices` key anywhere.
  - **`test_session_repo_still_active_defaults_normally`.** The registry lists `demo` as active. The call defaults, and the stub registry records exactly one `list_repos(active_only=True)` call.
  - **`test_removed_session_repo_errors`.** The session repository is `demo`, but the registry's active list no longer contains it (unregistered, or present only as inactive).
    - A call without `repo_id` gives `is_error is True`.
    - The text contains `session repository 'demo' is no longer registered or active`, the remaining active ids and the restart hint.
    - The recorder was never called.
    - The same call with `repo_id="demo"` succeeds unchanged, with no registry read.
  - **`test_null_repo_id_is_treated_as_omitted`.** `{"repo_id": None, ...}` behaves as the omitted case.
  - **`test_cwd_source_notice`.** With `session_source="cwd"`, the notice ends `(from the server's working directory)`.
  - **`test_run_cypher_is_unchanged`.**
    - With `enable_run_cypher=True`, `run_cypher`'s schema equals its original signature's.
    - Its `required` is still `["query"]`.
- [ ] Implement `_with_repo_default`, the R1.3 message builder, and the wiring into `_instrumented_tool` (spec R1.2, R1.3, R4).
- [ ] `uv run pytest -q`. Commit "Default built-in MCP tools' repo_id to the session repository".

### Task 2: Resolution branches, instructions and composition (R1.3, R1 table, R2, R5, R6)

**Files:**
- `devgraph/mcp/server.py`:
  - **`build_server`**:
    - the `instructions` name the session repository and its source, or say `repo_id` is required (R5). The sentence "Every built-in tool takes a repo_id (the id shown by `devgraph list`) and defaults to that repo only" (about lines 320–322) is rewritten so it does not contradict this, and the `cross_repo` rule is kept;
    - the module docstring and `build_server` docstring mention the default.
- `tests/mcp/test_repo_default.py`: more cases. A real `resolve_session_repo(registry, env, cwd)` feeds `build_server`, so each branch is the production resolution, not a hand-set `session_repo`.

- [ ] Write failing tests:
  - **Unmatched pin.** `{DEVGRAPH_MCP_REPO: "nope"}`, with the cwd inside registered repository `a`.
    - `search_component` without `repo_id` gives `is_error is True`.
    - The text contains `DEVGRAPH_MCP_REPO='nope'`, `matches no registered repository`, `a (` (the listed id) and `restart the MCP server after registering a repository`.
    - The recorder was never called: no cwd fallback.
  - **Inactive pin.** The registry holds an inactive `old` (stub `list_repos(active_only=...)` honours the flag). With `{DEVGRAPH_MCP_REPO: "old"}`, the error says `inactive` and does not list `old`.
  - **No cwd match.** With `env={}` and the cwd at `tmp_path`, outside every repository, the error names the working directory and `DEVGRAPH_MCP_REPO`.
  - **One registered repository, no session.** It still errors, and the text lists that repository.
  - **No registered repositories.** The error says none are registered, points at `devgraph add`, and contains `restart the MCP server after registering a repository`.
  - **Every error case above** (unmatched, inactive, no cwd match, one repository, none) contains the restart hint.
  - **Nested registered repositories.**
    - With the cwd at `outer/inner/src`, the call defaults to `inner`.
    - With the cwd at `outer`, it defaults to `outer`.
    - An absolute-path pin inside `inner` defaults to `inner`.
  - **Unregistered nested repository.** `outer/vendor/sub` has its own `.git` but is not registered. With the cwd there, the call defaults to `outer`.
  - **Explicit wins over every branch.** In each error scenario above, the same call with `repo_id="a"` succeeds unchanged (same assertions as `test_explicit_calls_are_unchanged`).
  - **Project config does not gate the default.**
    - With `devgraph config disable` in effect (`project_config_enabled` → False) the default still applies.
    - With an untrusted `devgraph.tools.yaml` (the default conftest state) it also applies.
  - **`cross_repo=true` without `repo_id`.** It defaults when scoped, and errors when unscoped.
  - **`test_shadow_and_default_notices_coexist`.** A trusted tools file declares `search_component` (like `test_tool_plane.TOOLS`). A defaulted `search_component` call gives `notices == [<default notice>, <shadow notice>]`.
  - **Same repository across planes.**
    - In one session scoped by cwd to `demo`, with a trusted tools file declaring `list_folder` (as in `test_tool_plane.TOOLS`):
      - the `repo_id` key of a defaulted `search_component` response;
      - the `repo_id` that `list_folder` injected (the stub engine's recorded `run_read_cypher` parameters).
    - Both are `demo`.
  - **Telemetry.**
    - A defaulted call writes one record with exactly `_TELEMETRY_FIELDS`, and the string `demo` appears nowhere in the line.
    - An unscoped error writes one record with `ok: false`.
  - **Instructions.**
    - Scoped: `server.instructions` contains `'demo'` and the source.
    - Unscoped: it contains `repo_id` and `devgraph list`, and no repository id.
    - In both: the old sentence "takes a repo_id ... and defaults to that repo only" is gone, and the `cross_repo` rule is still there.
  - **Reload keeps the default.** A tools-file reload (`server.devgraph_tool_plane.reload_if_changed()` after rewriting the file) leaves a defaulted built-in call still on `demo`.
- [ ] Implement the instructions and the docstrings (spec R5). Fix anything the branch tests expose in the Task 1 error builder.
- [ ] `uv run pytest -q`. Commit "Name the MCP session repository in the server instructions".

### Task 3: Docs (R7)

**Files:**
- `DEVGRAPH-CLIENT.md`:
  - **About lines 209–211.** Replace "Always pass this repo's `repo_id`" with the R7 wording:
    - built-ins default to the session's repository;
    - check a dict response's `repo_id`/notice;
    - `find_requirements_for` and `blame_component` return lists with no notice; for those, the session repository is the one named in the instructions and in `devgraph://project-tools`;
    - the unscoped error asks you to restart the server after registering a repository;
    - pass `repo_id` for another repository or when the session has none;
    - the `cross_repo` rule is unchanged.
  - **Step 1** (about line 106). Instead of "every DevGraph MCP tool call needs it", say you need it to pin the session or to pass it explicitly.
  - **Project tools paragraph.** The `DEVGRAPH_MCP_REPO` pin example now says it scopes the built-ins' default as well.
- `README.md`:
  - **Operating model.** A "Session repository" bullet beside "Repository isolation" covers the default, the no-fallback error and the VS Code `cwd` limitation (spec R6).
  - **Project tools > Serving.** One clause: the same session repository is the built-ins' default.
- `PROJECT_STATUS.md`:
  - a line under the tool plane entry;
  - the `devgraph/mcp/` map line says built-ins default `repo_id` to the session repository.
- `tests/mcp/test_server.py`: `test_client_guide_resource_registered_and_readable` also asserts three things about the guide: it no longer contains "Always pass this repo's", it mentions the session default, and it names `find_requirements_for` and `blame_component` as carrying no notice.

- [ ] Write the failing guide assertion.
- [ ] Update the three docs. Every sentence must match the behaviour Tasks 1–2 shipped. Grep for other places that claim `repo_id` is required, and fix them:
  - `rg -n "Always pass|takes a repo_id|needs .*repo_id" DEVGRAPH-CLIENT.md README.md PROJECT_STATUS.md devgraph/mcp`.
- [ ] `uv run pytest -q`. Commit "Document the MCP session repository default".
