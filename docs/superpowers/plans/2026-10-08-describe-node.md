# describe_node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** a new built-in MCP tool, `describe_node`. It takes a node's name, optionally narrowed by label and file, and returns:

- the node's properties, with bookkeeping left out;
- its outgoing and incoming relationships, grouped by type into capped envelopes of `{label, name, file}` refs. Each ref is the next call's arguments.

It works for built-in, filesystem and docs nodes, including id-keyed docs nodes. It is read-only, parameterised and bounded, and defaults `repo_id` to the session repository.

**Spec:** `docs/superpowers/specs/2026-10-08-describe-node-design.md`. Every task implements the decisions it names (D1–D7).

**Working directory:** this worktree, branch `epic1/describe-node` (from `epic1/mcp-repo-default`). Run `uv sync --extra dev` once, then `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests need the local Neo4j (`bolt://127.0.0.1:7687`) and skip without it. Never touch `~/.devgraph`.

## Global Constraints

- **Read-only and bounded (D6).**
  - Every query goes through `engine.run_read_cypher(..., timeout_s=DEFAULT_TIMEOUT_S, max_rows=...)`. Import `DEFAULT_TIMEOUT_S` from `devgraph.config.project_tools`; do not copy the number.
  - The caps are a lookup `LIMIT 21` with `max_rows=21`, groups `max_rows=200`, and suggestions `LIMIT 5`.
  - `describe_node` never calls `engine.run_cypher`.
- **Parameters only (D6).**
  - `name`, `file`, `repo_id`, `direction`, `relationship_types`, `neighbor_labels` and the cap are always Cypher parameters.
  - The single interpolation is `label`. It is interpolated only after `LABEL_PATTERN.fullmatch`, inside backticks.
  - Relationship types and neighbour labels are validated against `RELATIONSHIP_TYPE_PATTERN` and `LABEL_PATTERN` for a clear error, then still passed as values. They are never interpolated.
- **Identity (D1).**
  - The match is `n.repo_id = $repo_id AND (n.name = $name OR n.path = $name)`.
  - `file` matches `n.file`, `n.path`, `n.source_file` or `n.source`, or membership in `n.sources`.
  - Neighbours are restricted to `m.repo_id = $repo_id`.
- **Shapes (D2).**
  - The top-level `status` is `"found"` or `"ambiguous"`.
  - A ref is `{label: labels(n)[0], name, file: coalesce(n.file, n.path)}`, with `file` omitted when null.
  - Groups are `{count, results, truncated}`. Empty groups are omitted, and types are sorted.
  - Properties drop `repo_id`, `name`, `claims`, `sources`, `extractor` and `insight_*`, and are sanitised with `tool_plane._sanitize_deep`.
- **Errors are `ValueError`** raised from `tools.py`, the way `key_nodes` and `find_dependency_cycles` reject bad input. The SDK turns them into tool errors. A message never echoes more than 100 characters of a caller's value.
- **Catalog lock (D4).** One `TOOL_CATALOG` entry; `builtin_tool_names()` picks it up. There is no separate lock list to edit.
- **No `cross_repo` parameter (D5).** The `repo_id: str` definition follows the existing pattern; the session default comes from `_with_repo_default` untouched.
- **No changes to other tools**, to telemetry fields, or to the server instructions. No new config knob, and no new dependency.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths. Test fixtures use fictional names (`api`, `payments`, `platform-team`).

## Review Focus

Each item names the test that proves it.

1. **Injection.** Hostile filter values are rejected before any query runs, and a hostile `label` never reaches a query. Unit test: `test_hostile_filters_never_reach_cypher` (the stub engine records zero queries). Live test: `test_injection_attempt_changes_nothing` (node and relationship counts are identical before and after).
2. **Read-only path.** The stub engine's `run_cypher` raises. Every unit test runs against it, so any unbounded query fails loudly. Test: the `StubEngine` fixture itself, plus `test_queries_are_bounded` (each recorded call carries `timeout_s=10` and the stated `max_rows`).
3. **Refs round-trip.** For every neighbour ref in a live result, `describe_node(**ref)` returns `status == "found"` and that node. Test: `test_every_ref_round_trips`.
4. **Catalog lock.** A trusted project tools file declaring `describe_node` is ignored with the shadow notice. Test: `test_project_tool_cannot_take_describe_node`.
5. **Session default.** A call without `repo_id` describes the session repository's node and carries the notice. Test: `test_describe_node_uses_the_session_repo`.

### Task 1: `describe_node` in `tools.py` (D1, D2, D3, D6)

**Files:**
- `devgraph/mcp/tools.py`: `describe_node(engine, repo_id, name, label=None, file=None, direction="both", relationship_types=None, neighbor_labels=None, max_per_type=10) -> dict[str, Any]`, plus private helpers:
  - `_node_ref(row)`;
  - `_visible_properties(props)`;
  - `_validated_identifiers(values, pattern, what)`, which rejects more than 20 entries, and treats `None` or `[]` as no filter;
  - `_read(engine, cypher, params, max_rows)`. It wraps `run_read_cypher` and maps a Neo4j `TransactionTimedOut` to the D6 message and any other `Neo4jError` to its code.
- `tests/mcp/test_describe_node.py` (new): unit tests against a `StubEngine` that records `run_read_cypher(query, params, timeout_s, max_rows)` calls and returns canned rows per call, and whose `run_cypher` raises `AssertionError`.
- `tests/mcp/test_describe_node_live.py` (new): live tests.
  - **Scaffolding.** Copy the token, repo and cleanup scaffolding of `tests/indexer/test_docs_provider_live.py`:
    - a unique `REPO`;
    - token-suffixed labels and relationship types via `_labels(text)`;
    - a module fixture that drops the generated constraints;
    - `delete_repository` before and after.
  - **The `tmp_path` repository:**
    - `docker-compose.yml` declaring `api` and `docker-compose.prod.yml` also declaring `api`;
    - `src/app.py` and `src/worker.py`, each defining `def main()`;
    - `runbooks/api-outage.md` (`type: runbook`, `service: api`, `owner: platform-team`);
    - `decisions/adr-012.md` (`id: ADR-012`);
    - `decisions/adr-013.md` (`id: ADR-013`, `supersedes: ADR-012`);
    - twelve files under `many/`.
  - **The schema:**
    - `File` and `Folder` (filesystem) with `IS_CHILD_OF`;
    - `Runbook` (docs, `[path]`) with `RUNBOOK_FOR` → `Service`;
    - `Adr` (docs, `key: [adr_id]`, `fields: {adr_id: id}`) with `REPLACES` → `Adr`.
  - **Setup:** `provision_repository_schema` and `full_scan`, once per module.

- [ ] Write failing unit tests:
  - **`test_single_match_returns_node_and_groups`.** Canned lookup row and group rows give `status == "found"`, and:
    - the node ref;
    - `outgoing`/`incoming` built from `(dir, rel, total, refs)` rows;
    - `truncated` is true when `total > len(refs)`.
  - **`test_properties_hide_bookkeeping`.**
    - `claims`, `sources`, `extractor`, `insight_pagerank`, `repo_id` and `name` are absent.
    - `path`, `source_file`, `file` and `source` are present.
    - A 600-character string comes back capped at 500.
    - A `neo4j.time.DateTime` comes back as an ISO string.
  - **`test_ambiguous_returns_capped_candidates`.**
    - 21 lookup rows give `status == "ambiguous"`, 20 candidates and `truncated` true.
    - No groups query runs.
  - **`test_not_found_raises_with_suggestions`.**
    - An empty lookup and two suggestion rows raise `ValueError` naming `repo_id`, the filters and both refs, and mentioning `search_component`.
    - No groups query runs.
  - **`test_label_is_validated_then_interpolated`.**
    - `label="Runbook"` appears in the lookup as `` (n:`Runbook` ``.
    - `label="Runbook) DETACH DELETE n //"` raises before any query runs.
  - **`test_hostile_filters_never_reach_cypher`.** Each of these raises `ValueError`, and zero queries are recorded:
    - `relationship_types=["X]->() DETACH DELETE n //"]`;
    - `["calls"]` (lower case fails the pattern);
    - `neighbor_labels=["File`) RETURN 1 //"]`;
    - 21 entries;
    - `direction="sideways"`.
  - **`test_valid_filters_are_parameters`.**
    - `relationship_types=["RUNBOOK_FOR"]` and `neighbor_labels=["Service"]` appear only in `params` (`$types`, `$labels`), never in the query text.
    - `[]` sends `None`.
  - **`test_max_per_type_is_clamped`.** 0 sends 1, and 500 sends 50.
  - **`test_queries_are_bounded`.** Every recorded call has `timeout_s == DEFAULT_TIMEOUT_S`, the lookup has `max_rows == 21`, and the groups query `max_rows == 200`.
  - **`test_timeout_is_a_clear_error`.** The stub raises a `Neo4jError` with code `Neo.ClientError.Transaction.TransactionTimedOut`, and the message reads `describe_node timed out after 10s; narrow it with ...`.
- [ ] Write failing live tests (`test_describe_node_live.py`):
  - **`test_builtin_module_and_function`.**
    - `describe_node(name="main", label="Function", file="src/app.py")` is found, with an incoming `CONTAINS` from the `src/app.py` Module.
    - `describe_node(name="src/app.py", label="Module")` lists both outgoing `CONTAINS`.
  - **`test_filesystem_nodes`.**
    - The `Folder` `src` has incoming `IS_CHILD_OF` from the `File` nodes `src/app.py` and `src/worker.py`, and an outgoing `IS_CHILD_OF` to `.`.
    - `node.properties.path == "src"`.
  - **`test_docs_path_keyed_node`.**
    - `describe_node(name="runbooks/api-outage.md", label=<Runbook>)` has `owner` among its properties.
    - Its outgoing `RUNBOOK_FOR` holds two `Service api` refs with different `file`s.
  - **`test_docs_id_keyed_node`.**
    - `describe_node(name="ADR-013")` has an outgoing `REPLACES` to `{label: <Adr>, name: "ADR-012", file: "decisions/adr-012.md"}`.
    - `describe_node(name="decisions/adr-013.md", label=<Adr>)` finds the same node through `path`.
  - **`test_ambiguity`.**
    - `name="src/app.py"` with no label is ambiguous between the `Module` and the `File`.
    - `name="main"` is ambiguous across the two files.
    - `name="api", label="Service"` is ambiguous across the two compose files.
    - Each candidate, re-called as `describe_node(**candidate)`, is found.
  - **`test_not_found`.** `name="ap"` raises, and the message suggests `api`.
  - **`test_filters`.**
    - `direction="in"` gives `outgoing == {}`.
    - `relationship_types=[<IS_CHILD_OF>]` on `src/app.py`'s `File` leaves only that group.
    - `neighbor_labels=["Module"]` on `main` keeps only the Module ref.
    - A valid type with no edges gives empty maps, not an error.
  - **`test_caps`.** The `Folder` `many` with `max_per_type=5` gives incoming `IS_CHILD_OF` `count == 12`, 5 results and `truncated` true. The results are sorted by name.
  - **`test_every_ref_round_trips`.** Every ref from the results of the `Folder` `src`, the runbook and `ADR-013` re-describes to `found`.
  - **`test_injection_attempt_changes_nothing`.**
    - Run the hostile inputs from the unit test, plus `name="x' OR 1=1 //"` and `file="') DETACH DELETE n //"`, against the live graph.
    - The two strings are not-found errors.
    - `MATCH (n {repo_id: $r}) RETURN count(n)` and the relationship count are unchanged.
  - **`test_other_repo_is_invisible`.** Seed a second unique repository containing `Service api`. Neither `count` nor any ref includes it.
- [ ] Implement `describe_node` per D1–D3 and D6 so that both files pass.
- [ ] `uv run pytest -q`. Commit "Add describe_node graph lookup".

### Task 2: Register, catalog and session default (D4, D5)

**Files:**
- `devgraph/mcp/catalog.py`: the D4 entry, placed after `get_source`.
- `devgraph/mcp/server.py`: the `@server.tool(annotations=_READ_ONLY) def describe_node(repo_id: str, name: str, label: str | None = None, file: str | None = None, direction: str = "both", relationship_types: list[str] | None = None, neighbor_labels: list[str] | None = None, max_per_type: int = 10) -> dict[str, Any]` tool, which delegates to `devgraph_tools.describe_node`. Its docstring says:
  - what it returns;
  - that a file, folder or docs node is named by its path or id;
  - that an ambiguous result lists candidates to call again with `label` and `file`;
  - that each ref is the next call's arguments.
- `tests/mcp/test_server.py`, `tests/mcp/test_server_telemetry.py`, `tests/mcp/test_tools_cycles.py`: 24 becomes 25.
- `tests/mcp/test_repo_default.py`: `MIN_ARGS["describe_node"] = {"name": "X"}`; `test_builtins_with_repo_id_are_exactly_the_24` is renamed `..._the_25` and asserts 25.
- `tests/mcp/test_describe_node.py`: server-level tests that reuse the stub pattern of `test_repo_default.py`/`test_tool_plane.py`.

- [ ] Write failing tests:
  - **The count assertions** are 25, and `test_tool_catalog_resource_lists_every_registered_tool` passes only with the catalog entry. That test checks that the catalog and the registrations agree.
  - **`test_describe_node_is_locked`.** `"describe_node" in builtin_tool_names()`.
  - **`test_project_tool_cannot_take_describe_node`.**
    - A trusted `devgraph.tools.yaml` declares `describe_node` (copy the shape `test_tool_plane.TOOLS` uses).
    - `devgraph://project-tools` lists the `ignored: project tool 'describe_node' shadows a locked tool; using the fixed implementation` notice.
    - The listed tool is the built-in (its schema has `max_per_type`).
    - A call's dict carries the shadow notice.
  - **`test_global_tool_cannot_take_describe_node`.** The same, for a global store entry, with the `global` wording.
  - **`test_describe_node_uses_the_session_repo`.**
    - With `session_repo=Repo("demo")`, a call with only `name` reaches `devgraph_tools.describe_node` (monkeypatched recorder) with `repo_id="demo"`.
    - The result gains `repo_id: "demo"` and the session notice.
    - An explicit `repo_id="other"` passes through untouched, with no notice.
  - **`test_describe_node_has_no_cross_repo`.** The listed `inputSchema` has no `cross_repo` property, and `required == ["name"]`.
  - **`test_describe_node_is_read_only_annotated`.** `annotations.readOnlyHint is True`.
  - **`test_describe_node_error_surfaces_as_tool_error`.** A recorder raising `ValueError("no node named 'x' ...")` gives `is_error is True` with that text, and a telemetry record with `ok: false`.
- [ ] Add the catalog entry and the registration.
- [ ] `uv run pytest -q`. Commit "Serve describe_node as a built-in MCP tool".

### Task 3: Docs (D7)

**Files:**
- `DEVGRAPH-CLIENT.md`:
  - **§4 table.** Add the row "What is X connected to? / follow X's links" → `describe_node` (a name, or a path or id for file, folder and docs nodes).
  - **A new "Walking the graph" subsection after the table:**
    1. `search_component` finds a starting node;
    2. `describe_node(name=...)` shows its fields and links;
    3. pick a ref and call `describe_node(**ref)`.

    It also says:
    - `status: "ambiguous"` means calling again with a candidate's `label` and `file`;
    - `direction`, `relationship_types` and `neighbor_labels` keep a busy node small, and `max_per_type` (up to 50) widens a group;
    - check `truncated`;
    - it is one hop per call, with no `cross_repo`.
  - **Identifier-nuance paragraph.** Add `describe_node`.
  - **Response-shape paragraph.** `describe_node` applies the envelope per relationship group.
  - **Tool list (§ "What this gets you").** Add `describe_node`.
- `README.md`:
  - **Project schema paragraph** ("`search_component` finds them"): add "and `describe_node` shows their fields and `IS_CHILD_OF` links".
  - **Runbook example.** After "a `RUNBOOK_FOR` edge to the `api` Service", one sentence: an assistant can ask `describe_node` for `runbooks/api-outage.md` to see both.
- `PROJECT_STATUS.md`:
  - a shipped entry under the repo-default one;
  - the `devgraph/mcp/` map line (line 15's tool list) gains `describe_node`.
- `tests/mcp/test_server.py`: `test_client_guide_resource_registered_and_readable` also asserts that the guide mentions `describe_node` and `ambiguous`.

- [ ] Write the failing guide assertion.
- [ ] Update the three docs. Every sentence must match what Tasks 1–2 shipped. Run `rg -n "search_component finds|only through search_component|24 tools|24 built-in" README.md DEVGRAPH-CLIENT.md PROJECT_STATUS.md devgraph/mcp`:
  - fix current-state claims;
  - leave the dated shipped entries alone.
- [ ] `uv run pytest -q`. Commit "Document describe_node".
