# describe_node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** a new built-in MCP tool, `describe_node`. It takes a node's name, optionally narrowed by label and file, and returns:

- the node's properties, with bookkeeping left out;
- its outgoing and incoming relationships, grouped by type into capped envelopes of exact `{label, name, file}` refs. Each ref is the next call's arguments.

It works for built-in, filesystem and docs nodes, including id-keyed docs nodes, and never for `Repository` nodes. It is read-only, parameterised and bounded, and defaults `repo_id` to the session repository.

**Spec:** `docs/superpowers/specs/2026-10-08-describe-node-design.md`. Every task implements the decisions it names (D1–D7).

**Working directory:** this worktree, branch `epic1/describe-node` (from `epic1/mcp-repo-default`). Run `uv sync --extra dev` once, then `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests need the local Neo4j (`bolt://127.0.0.1:7687`) and skip without it. Never touch `~/.devgraph`.

## Global Constraints

- **Read-only and bounded (D6).**
  - Every query goes through `engine.run_read_cypher(..., timeout_s=DEFAULT_TIMEOUT_S, max_rows=...)`. Import `DEFAULT_TIMEOUT_S` from `devgraph.config.project_tools`; do not copy the number.
  - The caps are lookup `LIMIT 21` with `max_rows=21`, suggestions `LIMIT 5` with `max_rows=5`, and groups `max_rows=200`. A capped groups read sets `groups_truncated: true`.
  - `describe_node` never calls `engine.run_cypher`.
- **No `Repository` (D1).** `NOT n:Repository` appears in the lookup and the suggestions query, and `NOT m:Repository` in both neighbour branches and in the per-group top-N subquery.
- **Lookup shape (D1).**
  - A `CALL () { ... UNION ... }` of single-property branches, one per label:
    - name branches for every searched label;
    - path branches for declared labels only.
  - The searched labels are the given `label`, else `schema.NODE_LABELS` minus `Repository` plus `declared_labels`. Each one is `LABEL_PATTERN`-validated and interpolated in backticks.
  - There is never an `n.name = $name OR n.path = $name`.
  - `file` matches `n.file`, `n.path` or `n.source_file`, and nothing else.
- **Groups shape (D2).**
  - A count stage: a `CALL (n) { out-branch UNION in-branch }`, each branch guarded by `$direction IN [...]`, then `count(DISTINCT m)`, with no collect.
  - A per-group `CALL (n, dir, rel) { ... WITH DISTINCT m ORDER BY m.name, coalesce(m.file, m.path) LIMIT $cap RETURN collect(...) AS refs }`.
  - Neither `direction` nor any type or label filter is interpolated.
- **Parameters only (D6).** `name`, `file`, `repo_id`, `direction`, `relationship_types`, `neighbor_labels` and the cap are always Cypher parameters. Relationship types and neighbour labels are validated against `RELATIONSHIP_TYPE_PATTERN` and `LABEL_PATTERN` for a clear error, then still passed as values.
- **Shapes (D2).**
  - The top-level `status` is `"found"` or `"ambiguous"`. A found result has `groups_truncated`, and its node has `properties_truncated`.
  - A ref is `{label: labels(n)[0], name, file: coalesce(n.file, n.path)}`, with `file` omitted when null. It is built from raw values and is never sanitised.
  - Groups are `{count, results, truncated}`. Empty groups are omitted, and types are sorted.
  - Properties drop `repo_id`, `name`, `claims`, `extractor` and `insight_*`, and keep `sources`.
  - Display values are sanitised with `tool_plane._sanitize_deep`, lists are capped at 20 items, and at most 50 properties are returned in key order.
- **Errors are `ValueError`** raised from `tools.py`, the way `key_nodes` and `find_dependency_cycles` reject bad input. The SDK turns them into tool errors.
  - A message never echoes more than 100 characters of a caller's value.
  - A Neo4j error whose `code` contains `"TransactionTimedOut"` becomes the D6 timeout message. Any other Neo4j error becomes `describe_node failed: <code>`.
- **Catalog lock (D4).** One `TOOL_CATALOG` entry with `envelope: False` and the per-group note; `builtin_tool_names()` picks it up. There is no separate lock list to edit.
- **No `cross_repo` parameter (D5).** The `repo_id: str` definition follows the existing pattern; the session default comes from `_with_repo_default` untouched.
- **No changes to other tools**, to telemetry fields, or to the server instructions. No new config knob, and no new dependency.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths. Test fixtures use fictional names (`api`, `payments`, `platform-team`).

## Review Focus

Each item names the test that proves it.

1. **Injection.** Hostile filter values are rejected before any query runs, and hostile `name` or `file` values are only ever parameters. Unit test: `test_hostile_filters_never_reach_cypher` (zero recorded queries). Live test: `test_injection_attempt_changes_nothing` (node and relationship counts are unchanged).
2. **Read-only and bounded.** The stub engine's `run_cypher` raises, so any unbounded query fails loudly. `test_queries_are_bounded` checks `timeout_s` and `max_rows` on every call. `test_many_edges_cap_and_exact_count` (live, 500 incoming edges) proves the top-N and the exact count.
3. **No `Repository` leak.** Unit test: `test_repository_is_excluded_everywhere`. Live test: `test_repository_name_is_not_ambiguous`.
4. **Refs round-trip exactly.** `test_refs_are_raw` (unit) and `test_every_ref_round_trips` (live).
5. **Provenance kept.** `sources` is visible; only `claims`, `extractor` and `insight_*` are hidden. Test: `test_properties_hide_only_bookkeeping`.
6. **Catalog lock.** A trusted project tools file declaring `describe_node` is ignored with the shadow notice. Test: `test_project_tool_cannot_take_describe_node`.
7. **Session default.** A call without `repo_id` describes the session repository's node and carries the notice. Test: `test_describe_node_uses_the_session_repo`.

### Task 1: `describe_node` in `tools.py` (D1, D2, D3, D6)

**Files:**
- `devgraph/mcp/tools.py`: `describe_node(engine, repo_id, name, label=None, file=None, direction="both", relationship_types=None, neighbor_labels=None, max_per_type=10, declared_labels=()) -> dict[str, Any]`, plus private helpers:
  - `_lookup_cypher(labels, declared)`, which builds the UNION;
  - `_node_ref(row)`, which uses raw values;
  - `_visible_properties(props)`, which hides, sanitises and caps;
  - `_validated_identifiers(values, pattern, what)`, which rejects more than 20 entries, and treats `None` or `[]` as no filter;
  - `_read(engine, cypher, params, max_rows)`. It wraps `run_read_cypher` and maps Neo4j errors per D6.
- `tests/mcp/test_describe_node.py` (new): unit tests against a `StubEngine` that records `run_read_cypher(query, params, timeout_s, max_rows)` calls and returns canned `(rows, more)` per call, and whose `run_cypher` raises `AssertionError`.
- `tests/mcp/test_describe_node_live.py` (new): live tests.
  - **Scaffolding.** Copy the token, repo and cleanup scaffolding of `tests/indexer/test_docs_provider_live.py`:
    - a unique `REPO`;
    - token-suffixed labels and relationship types via `_labels(text)`;
    - a module fixture that drops the generated constraints;
    - `delete_repository` before, and after through a fixture finaliser.
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

    All labels and types are token-suffixed.
  - **Setup:** `upsert_repository(REPO, "api", str(root))` (so the `Repository` node is named `api`), then `provision_repository_schema` and `full_scan`, once per module.
  - **Calls** pass `declared_labels=` the suffixed declared labels, as the server will.

- [ ] Write failing unit tests:
  - **`test_single_match_returns_node_and_groups`.** A canned lookup row and group rows give `status == "found"`, and:
    - the node ref;
    - `outgoing`/`incoming` built from `(dir, rel, total, refs)` rows;
    - `truncated` is true when `total > len(refs)`;
    - `groups_truncated` is false.
  - **`test_properties_hide_only_bookkeeping`.**
    - `claims`, `extractor`, `insight_pagerank`, `repo_id` and `name` are absent.
    - `sources`, `source`, `path`, `source_file` and `file` are present.
    - A 600-character string comes back capped at 500.
    - A `neo4j.time.DateTime` comes back as an ISO string.
    - A 30-item list comes back with 20 items.
    - 60 properties give 50 and `properties_truncated` true.
  - **`test_refs_are_raw`.** A neighbour named with 600 characters and a tab comes back unchanged in its ref.
  - **`test_lookup_without_label_is_a_per_label_union`.**
    - The lookup text has a name branch per built-in label except `Repository`, and per declared label.
    - It has path branches only for declared labels.
    - It contains no ` OR n.path`.
  - **`test_label_is_validated_then_interpolated`.**
    - `label="Runbook"` gives exactly two branches, `` (n:`Runbook`) `` by name and by path.
    - `label="Runbook) DETACH DELETE n //"` raises before any query runs.
    - So does a declared label that fails the pattern; it is dropped, never interpolated.
  - **`test_repository_is_excluded_everywhere`.** `NOT n:Repository` is in the lookup and the suggestions text, and `NOT m:Repository` is in the groups text.
  - **`test_file_filter_is_a_parameter`.**
    - `file="src/app.py"` is sent as `$file`.
    - The lookup text tests `n.file`, `n.path` and `n.source_file` against it, and not `n.source` or `n.sources`.
    - With no `file`, `$file` is `None`.
  - **`test_ambiguous_returns_capped_sorted_candidates`.**
    - 21 unsorted lookup rows give `status == "ambiguous"`, `count == 21`, 20 candidates sorted by label, name and file, and `truncated` true.
    - 3 rows give `count == 3` and `truncated` false.
    - No groups query runs.
  - **`test_not_found_raises_with_repo_and_suggestions`.**
    - An empty lookup and two suggestion rows raise a `ValueError` that names the repository (`'demo'`), the filters and both refs, and mentions `search_component`.
    - No groups query runs.
  - **`test_error_echo_is_capped`.**
    - A 5,000-character `name` that matches nothing gives a message containing at most 100 characters of it.
    - A 5,000-character bad relationship type does the same.
  - **`test_hostile_filters_never_reach_cypher`.** Each of these raises `ValueError`, and zero queries are recorded:
    - `relationship_types=["X]->() DETACH DELETE n //"]`;
    - `["calls"]` (lower case fails the pattern);
    - `neighbor_labels=["File`) RETURN 1 //"]`;
    - 21 entries;
    - `direction="sideways"`.
  - **`test_valid_filters_and_direction_are_parameters`.**
    - `relationship_types=["RUNBOOK_FOR"]`, `neighbor_labels=["Service"]` and `direction="in"` appear only in `params` (`$types`, `$labels`, `$direction`), never in the query text.
    - The groups text contains `$direction IN ['both','out']` and `$direction IN ['both','in']`.
    - `[]` sends `None`.
  - **`test_groups_query_is_top_n_with_separate_count`.** The groups text contains:
    - `count(DISTINCT m)`;
    - a `CALL (n, dir, rel)` subquery with `LIMIT $cap`;
    - no `collect(` before that subquery.
  - **`test_max_per_type_is_clamped`.** 0 sends `$cap` 1, and 500 sends 50.
  - **`test_groups_truncated_when_more_than_200`.** The groups stub returns 200 rows with `more=True`, which gives `groups_truncated` true.
  - **`test_node_deleted_between_queries`.** A single lookup row and an empty groups result give `status == "found"`, the node, `outgoing == {}` and `incoming == {}`.
  - **`test_queries_are_bounded`.** Every recorded call has `timeout_s == DEFAULT_TIMEOUT_S`. The lookup has `max_rows == 21`, the suggestions 5, and the groups 200.
  - **`test_timeout_is_a_clear_error`.** The stub raises a `Neo4jError` whose code is `Neo.ClientError.Transaction.TransactionTimedOut`, and the message reads `describe_node timed out after 10s; narrow it with ...`.
  - **`test_other_neo4j_error_is_its_code_only`.** A `Neo4jError` with code `Neo.ClientError.Statement.SyntaxError` and message `secret detail` gives `describe_node failed: Neo.ClientError.Statement.SyntaxError`, without `secret detail`.
- [ ] Write failing live tests (`test_describe_node_live.py`):
  - **`test_builtin_module_and_function`.**
    - `describe_node(name="main", label="Function", file="src/app.py")` is found, with an incoming `CONTAINS` from the `src/app.py` Module.
    - `describe_node(name="src/app.py", label="Module")` lists the outgoing `CONTAINS`.
  - **`test_filesystem_nodes`.**
    - The `Folder` `src` has incoming `IS_CHILD_OF` from the `File` nodes `src/app.py` and `src/worker.py`, and an outgoing `IS_CHILD_OF` to `.`.
    - `node.properties.path == "src"`.
  - **`test_docs_path_keyed_node`.**
    - The runbook (by path, with its label) has `owner` among its properties.
    - Its outgoing `RUNBOOK_FOR` holds two `Service api` refs with different `file`s.
  - **`test_docs_id_keyed_node`.**
    - `describe_node(name="ADR-013")` has an outgoing `REPLACES` to `{label: <Adr>, name: "ADR-012", file: "decisions/adr-012.md"}`.
    - `describe_node(name="decisions/adr-013.md", label=<Adr>)` finds the same node through the path branch.
  - **`test_shared_node_shows_sources`.** The `Service api` from `docker-compose.yml` shows `sources` in its properties, and no `claims`.
  - **`test_repository_name_is_not_ambiguous`.**
    - `describe_node(name="api", label="Service")` stays ambiguous only across the two compose files.
    - `describe_node(name="api")` lists no `Repository` candidate.
    - No candidate or ref anywhere carries the repository's absolute path.
  - **`test_ambiguity`.**
    - `name="src/app.py"` with no label is ambiguous between the `Module` and the `File`.
    - `name="main"` is ambiguous across the two files.
    - Each candidate, re-called as `describe_node(**candidate)`, is found.
  - **`test_not_found`.** `name="ap"` raises, and the message names `REPO` and suggests `api`.
  - **`test_filters`.**
    - `direction="in"` gives `outgoing == {}`.
    - `relationship_types=[<IS_CHILD_OF>]` on `src/app.py`'s `File` leaves only that group.
    - `neighbor_labels=["Module"]` on `main` keeps only the Module ref.
    - A valid type with no edges gives empty maps, not an error.
  - **`test_caps`.** The `Folder` `many` with `max_per_type=5` gives incoming `IS_CHILD_OF` `count == 12`, 5 results sorted by name, and `truncated` true.
  - **`test_many_edges_cap_and_exact_count`.**
    - Seed, with `upsert_nodes` and `upsert_relationships` in `REPO`, 500 `Function` nodes (`f000`…`f499`, `file` `gen.py`), each with a `CALLS` edge to one `Function hub`, plus a second, parallel `CALLS` edge from `f000`, added with a raw `CREATE` through `engine.run_cypher` in test setup, because `upsert_relationships` MERGEs.
    - `describe_node(name="hub", label="Function", file="gen.py", max_per_type=50)` gives incoming `CALLS` `count == 500`, 50 results `f000`…`f049`, and `truncated` true.
    - It runs under the 10s timeout.
  - **`test_every_ref_round_trips`.** Every ref from the results of the `Folder` `src`, the runbook and `ADR-013` re-describes to `found`, and to the same label, name and file.
  - **`test_injection_attempt_changes_nothing`.**
    - Run the hostile inputs from the unit test, plus `name="x' OR 1=1 //"` and `file="') DETACH DELETE n //"`, against the live graph.
    - The two strings are not-found errors.
    - `MATCH (n {repo_id: $r}) RETURN count(n)` and the relationship count are unchanged.
  - **`test_other_repo_is_invisible`.**
    - A function-scoped fixture seeds a second repository, `f"{REPO}_other"`. It holds a `Service api` and a token-suffixed `Runbook` node at `runbooks/api-outage.md`.
    - The fixture calls `delete_repository` on it before seeding, and again in a finaliser (`request.addfinalizer`), so a failing assertion still cleans up.
    - Neither the ambiguity `count` nor any ref includes the other repository's nodes.
- [ ] Implement `describe_node` per D1–D3 and D6 so that both files pass.
- [ ] `uv run pytest -q`. Commit "Add describe_node graph lookup".

### Task 2: Register, catalog and session default (D4, D5)

**Files:**
- `devgraph/mcp/catalog.py`: the D4 entry, placed after `get_source`, with `envelope: False` and the per-group note.
- `devgraph/mcp/server.py`: the `@server.tool(annotations=_READ_ONLY) def describe_node(repo_id: str, name: str, label: str | None = None, file: str | None = None, direction: str = "both", relationship_types: list[str] | None = None, neighbor_labels: list[str] | None = None, max_per_type: int = 10) -> dict[str, Any]` tool. It delegates to `devgraph_tools.describe_node(..., declared_labels=devgraph_tools.declared_node_labels(registry, repo_id))`. Its docstring says:
  - what it returns;
  - that a file, folder or docs node is named by its path or id;
  - that an ambiguous result lists candidates to call again with `label` and `file`;
  - that each ref is the next call's arguments;
  - that it is one hop per call, at most 50 per type, and filters narrow it.
- `tests/mcp/test_server.py`: rename `test_all_24_tools_registered` to `test_all_25_tools_registered`, and assert 25.
- `tests/mcp/test_server_telemetry.py` and `tests/mcp/test_tools_cycles.py`: 24 becomes 25.
- `tests/mcp/test_repo_default.py`: rename `test_builtins_with_repo_id_are_exactly_the_24` to `test_builtins_with_repo_id_are_exactly_the_25`, assert 25, and add `MIN_ARGS["describe_node"] = {"name": "X"}`.
- `tests/mcp/test_describe_node.py`: server-level tests that reuse the stub pattern of `test_repo_default.py`/`test_tool_plane.py`.

- [ ] Write failing tests:
  - **The count assertions and renames** above. `test_tool_catalog_resource_lists_every_registered_tool` passes only with the catalog entry; that test checks that the catalog and the registrations agree.
  - **`test_describe_node_catalog_entry`.**
    - `"describe_node" in builtin_tool_names()`.
    - Its entry has `envelope is False` and a `note` mentioning `{count, results, truncated}`.
  - **`test_project_tool_cannot_take_describe_node`.**
    - A trusted `devgraph.tools.yaml` declares `describe_node` (copy the shape `test_tool_plane.TOOLS` uses).
    - `devgraph://project-tools` lists the `ignored: project tool 'describe_node' shadows a locked tool; using the fixed implementation` notice.
    - The listed tool is the built-in (its schema has `max_per_type`).
    - A call's dict carries the shadow notice.
  - **`test_global_tool_cannot_take_describe_node`.** The same, for a global store entry, with the `global` wording.
  - **`test_describe_node_uses_the_session_repo`.**
    - With `session_repo=Repo("demo")`, a call with only `name` reaches `devgraph_tools.describe_node` (monkeypatched recorder) with `repo_id="demo"` and the stubbed declared labels.
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
    - it is one hop per call, with at most 50 neighbours per relationship type (`max_per_type`, default 10) and no paging;
    - `count` is the full total, so check `truncated`;
    - to narrow a busy node, use `direction`, `relationship_types` and `neighbor_labels`;
    - `status: "ambiguous"` means calling again with a candidate's `label` and `file`;
    - there is no `cross_repo`.
  - **Identifier-nuance paragraph.** Add `describe_node`.
  - **Response-shape paragraph.** `describe_node` is not itself an envelope; each relationship group is.
  - **Tool list (§ "What this gets you").** Add `describe_node`.
- `README.md`:
  - **Project schema paragraph** ("`search_component` finds them"): add "and `describe_node` shows their fields and `IS_CHILD_OF` links".
  - **Runbook example.** After "a `RUNBOOK_FOR` edge to the `api` Service", one sentence: an assistant can ask `describe_node` for `runbooks/api-outage.md` to see both.
- `PROJECT_STATUS.md`:
  - a new shipped entry under the repo-default one, saying "25 built-in tools";
  - line 15's tool list gains `describe_node`;
  - the dated entries that say 24 stay as they are.
- `tests/mcp/test_server.py`: `test_client_guide_resource_registered_and_readable` also asserts that the guide mentions `describe_node`, `ambiguous` and `one hop`.

- [ ] Write the failing guide assertion.
- [ ] Update the three docs. Every sentence must match what Tasks 1–2 shipped. Run `rg -n "search_component finds|only through search_component|24 tools|24 built-in" README.md DEVGRAPH-CLIENT.md PROJECT_STATUS.md devgraph/mcp`:
  - fix current-state claims;
  - leave the dated shipped entries alone.
- [ ] `uv run pytest -q`. Commit "Document describe_node".
