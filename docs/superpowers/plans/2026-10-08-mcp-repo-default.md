# MCP Repo Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Every built-in MCP tool may omit `repo_id`. It then runs against the session's repository and says so, or it fails with an error that lists the registered repositories. Calls that pass `repo_id` are unchanged byte for byte.

**Spec:** `docs/superpowers/specs/2026-10-08-mcp-repo-default-design.md`. Every task implements the decisions it names (R1–R7).

**Working directory:** this worktree, branch `epic1/mcp-repo-default` (from `epic1/watcher`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). None of these tests need Neo4j. Never touch `~/.devgraph`.

## Global Constraints

- **Explicit wins, untouched (R1).** With `repo_id` present and not `None`, the wrapper calls the tool with the caller's kwargs and returns the very object the tool returned. It does not validate, normalise, add a key or add a notice, even when the value equals the session repository.
- **One resolution (R2).** The default is `build_server`'s `session_repo`. Nothing calls `resolve_session_repo` again, nothing reads a file in the repository, and nothing falls back to the cwd after an unmatched pin.
- **Never "any repo" (R6).** With no session repository, a call without `repo_id` raises `ToolError` before the tool body runs. That holds even with exactly one registered repository.
- **Schema delta is exactly R3.**
  - For each of the 24 built-ins: `repo_id` leaves `required`, and its property is `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Repo Id"}`.
  - Everything else in every listed tool is equal to what the original function's signature produces: the other properties and their order, the rest of `required`, descriptions, annotations and names.
  - `run_cypher`, project tools and global tools are unchanged.
- **Tool definitions unchanged (R4).** The 24 `@server.tool` functions keep `repo_id: str` and their bodies. The change lives in one wrapper inside `_instrumented_tool`.
- **Envelopes kept (R5).** A defaulted dict response gains only `repo_id` and one `notices` entry (appended after the shadow notices, never replacing them). A defaulted list response is unchanged.
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
   - one registered repository.

   The error lists the repositories. Tests: Task 2 resolution branches.
5. **Notices compose.** A built-in that is both shadowed and defaulted carries both notices, shadow first. Test: Task 2 `test_shadow_and_default_notices_coexist`.

### Task 1: Optional `repo_id` and the session default (R1.1, R1.2, R2, R3, R4, R5)

**Files:**
- `devgraph/mcp/server.py`:
  - **`_with_repo_default(fn, session_repo, session_source, unscoped_error)`**:
    - it returns `fn` unchanged when `fn` has no `repo_id` parameter;
    - otherwise it is a `functools.wraps` wrapper whose `__signature__` is `fn`'s evaluated signature with every parameter keyword-only and `repo_id: str | None = None`;
    - it substitutes `session_repo.repo_id` and adds the R5 `repo_id` key and notice to a dict result;
    - when there is no session repository it calls `unscoped_error()` (Task 2 fills it in; for now it raises a plain `ToolError("repo_id is required")`).
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
  - **`BUILTINS_WITH_REPO_ID`.** Computed from `asyncio.run(server.list_tools())` and each tool's registered function (`server._tool_manager._tools[name].fn` in mcp 2.3.0): the names whose `inspect.signature(inspect.unwrap(fn))` has `repo_id`. Built with `enable_run_cypher=True`, so `run_cypher` is present and must be excluded.

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
  - **`test_null_repo_id_is_treated_as_omitted`.** `{"repo_id": None, ...}` behaves as the omitted case.
  - **`test_cwd_source_notice`.** With `session_source="cwd"`, the notice ends `(from the server's working directory)`.
  - **`test_run_cypher_is_unchanged`.**
    - With `enable_run_cypher=True`, `run_cypher`'s schema equals its original signature's.
    - Its `required` is still `["query"]`.
- [ ] Implement `_with_repo_default` and wire it into `_instrumented_tool` (spec R4).
- [ ] `uv run pytest -q`. Commit "Default built-in MCP tools' repo_id to the session repository".

### Task 2: Resolution branches, errors and composition (R1.3, R1 table, R2, R5, R6)

**Files:**
- `devgraph/mcp/server.py`:
  - **`build_server`**:
    - it builds the unscoped error (R1.3) from `session_source`, `session_pinned`, `_inactive_match(registry, pinned)` and, at call time, `registry.list_repos(active_only=True)`;
    - the `instructions` name the session repository and its source, or say `repo_id` is required (R5);
    - the module docstring and `build_server` docstring mention the default.
  - `_inactive_match` is imported from `tool_plane`. It moves nothing.
- `tests/mcp/test_repo_default.py`: more cases. A real `resolve_session_repo(registry, env, cwd)` feeds `build_server`, so each branch is the production resolution, not a hand-set `session_repo`.

- [ ] Write failing tests:
  - **Unmatched pin.** `{DEVGRAPH_MCP_REPO: "nope"}`, with the cwd inside registered repository `a`.
    - `search_component` without `repo_id` gives `is_error is True`.
    - The text contains `DEVGRAPH_MCP_REPO='nope'`, `matches no registered repository`, and `a (`, the listed id.
    - The recorder was never called: no cwd fallback.
  - **Inactive pin.** The registry holds an inactive `old` (stub `list_repos(active_only=...)` honours the flag). With `{DEVGRAPH_MCP_REPO: "old"}`, the error says `inactive` and does not list `old`.
  - **No cwd match.** With `env={}` and the cwd at `tmp_path`, outside every repository, the error names the working directory and `DEVGRAPH_MCP_REPO`.
  - **One registered repository, no session.** It still errors, and the text lists that repository.
  - **No registered repositories.** The error says none are registered and points at `devgraph add`/`devgraph list`.
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
  - **`test_shadow_and_default_notices_coexist`.** A trusted tools file declares `search_component` (like `test_tool_plane.TOOLS`). A defaulted `search_component` call gives `notices == [<shadow notice>, <default notice>]`.
  - **Telemetry.**
    - A defaulted call writes one record with exactly `_TELEMETRY_FIELDS`, and the string `demo` appears nowhere in the line.
    - An unscoped error writes one record with `ok: false`.
  - **Instructions.**
    - Scoped: `server.instructions` contains `'demo'` and the source.
    - Unscoped: it contains `repo_id` and `devgraph list`, and no repository id.
  - **Reload keeps the default.** A tools-file reload (`server.devgraph_tool_plane.reload_if_changed()` after rewriting the file) leaves a defaulted built-in call still on `demo`.
- [ ] Implement the error builder and the instructions (spec R1.3, R5).
- [ ] `uv run pytest -q`. Commit "Explain a missing MCP session repository and name it in the instructions".

### Task 3: Docs (R7)

**Files:**
- `DEVGRAPH-CLIENT.md`:
  - **About lines 209–211.** Replace "Always pass this repo's `repo_id`" with the R7 wording:
    - built-ins default to the session's repository;
    - check the response's `repo_id`/notice;
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
- `tests/mcp/test_server.py`: `test_client_guide_resource_registered_and_readable` also asserts the guide no longer contains "Always pass this repo's" and does mention the session default.

- [ ] Write the failing guide assertion.
- [ ] Update the three docs. Every sentence must match the behaviour Tasks 1–2 shipped. Grep for other places that claim `repo_id` is required, and fix them:
  - `rg -n "Always pass|takes a repo_id|needs .*repo_id" DEVGRAPH-CLIENT.md README.md PROJECT_STATUS.md devgraph/mcp`.
- [ ] `uv run pytest -q`. Commit "Document the MCP session repository default".
