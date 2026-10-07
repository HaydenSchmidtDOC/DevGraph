# `describe_node`: one node, its properties and its relationships — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1 ("indexed → queryable through MCP
tools"). Builds on the session repository default
(`2026-10-08-mcp-repo-default-design.md`).

## Problem

An assistant can find a user-declared node (`Runbook`, `File`, `Folder`, `Adr`,
any schema type) only with `search_component`, which returns its name and
labels. Nothing built in shows the node's properties or follows its
relationships (`RUNBOOK_FOR`, `IS_CHILD_OF`, `REPLACES`, ...). Today that takes a
hand-written Cypher project tool and a `devgraph tools trust` step in a
terminal, which a non-developer can't do. The built-in tools that do follow
edges (`find_callers`, `impact_analysis`, `get_service_dependencies`) are each
fixed to particular built-in labels and relationship types.

## Goal

- One built-in tool takes a node's name and returns the node's properties and
  its outgoing and incoming relationships, grouped by type. Each neighbour
  carries enough to call the tool again on it, so an assistant can walk the
  graph one hop at a time.
- It works for every node in a repository: built-in labels, filesystem and docs
  provider nodes, and docs nodes keyed by a front-matter id.
- Read-only, parameterised, bounded in time and rows. No new config knob and
  no new dependency.

## Decisions

| # | Decision |
| --- | --- |
| D1 | **Identify by `name`, narrowed by optional `label` and `file`.** `Repository` nodes are never matched. Several matches return capped candidates; none is an error with suggestions. |
| D2 | **Output:** the node's properties minus bookkeeping, then `outgoing`/`incoming` maps of relationship type to a capped `{count, results, truncated}` envelope of exact neighbour refs. |
| D3 | **Filters:** `direction`, `relationship_types`, `neighbor_labels`; cap `max_per_type`. |
| D4 | **One tool, `describe_node`,** added to the locked built-in catalog. No separate `neighbors`. |
| D5 | **`repo_id` follows the session default; no `cross_repo`.** |
| D6 | **Safety:** every user value is a query parameter; only pattern-validated labels are interpolated; every query runs read-only under a timeout and row cap. |
| D7 | **Docs** teach the walk: `search_component` to find, `describe_node` to look and step, one hop per call. |

### D1: identifying a node

Arguments: `name` (required), `label` (optional), `file` (optional).

How keys look today, which this has to cover:

| Node | `name` | Other identity |
| --- | --- | --- |
| `Class`, `Function`, and `Service` declared in a compose file | symbol or service name | `file` (unique key is `repo_id, name, file`) |
| `Module` | repo-relative path | `source_file` = the same path |
| Other built-ins (`Endpoint`, `Database`, `Container`, `Commit`, ...) | name (sha for `Commit`) | none: `(repo_id, name)` is unique |
| Filesystem node (`File`, `Folder`) | repo-relative path (`.` for the root) | `path` = the same path |
| Docs node keyed `[path]` | repo-relative path | `path` = the same path |
| Docs node keyed on a front-matter field | the id (`ADR-013`) | `path` = its file |

**`Repository` is excluded everywhere.** The lookup, the suggestions and the neighbour match all carry `NOT n:Repository` (`NOT m:Repository`). A `Repository` node has `name` (the folder name) and `path` (an absolute path), plus `schema_*` and `insights_*` bookkeeping. Without the exclusion, a repository folder named `api` would make `describe_node(name="api")` ambiguous, and its absolute path and bookkeeping would leak.

**The match.** A node matches when `n.name = $name` or `n.path = $name`. Passing a docs file's path therefore finds its id-keyed node too. Matching is exact and case-sensitive, like the keys themselves; `search_component` remains the fuzzy finder.

**Index use.** In Neo4j an index belongs to one label, and an `OR` across two properties can't seek. So the lookup is a `CALL () { ... UNION ... }` of single-property branches, one per label:

- **Labels searched.**
  - With `label` given: that label alone, after a `LABEL_PATTERN` fullmatch, interpolated in backticks.
  - Without it: every built-in label except `Repository` (`schema.NODE_LABELS`), plus the repository's `declared_node_labels(registry, repo_id)`, each one validated the same way. `search_component` already builds its label list like this.
- **Branches.**
  - A name branch for every label: `` MATCH (n:`L`) WHERE n.repo_id = $repo_id AND n.name = $name ``. This seeks on the `(repo_id, name)` constraint where the label has one (all built-ins but `Class`, `Function` and `Service`, and every sourced declared type through its `_repo_name` index). Other labels are scanned, one label at a time.
  - A path branch for declared labels only, since only provider nodes carry `path`: `` MATCH (n:`L`) WHERE n.repo_id = $repo_id AND n.path = $name ``. It seeks on the `(repo_id, path)` key constraint of `[path]`-keyed types. Id-keyed docs types are scanned within their label.
- A node of a label that is neither built-in nor declared is not found. Applying a schema already deletes the nodes of a dropped label, so none should exist.

**`file`.** When given, `file` keeps only the nodes where `$file` equals `n.file`, `n.path` or `n.source_file`. That tells apart a `Function main` in two files, or a `Service api` in two compose files (a compose `Service` carries `file`).

**Results:**

- **Exactly one match:** describe it (D2).
- **Several:** not an error. Return `{"status": "ambiguous", "count", "candidates", "truncated"}`.
  - `candidates` holds up to 20 refs (D2), sorted by label, name and file.
  - The lookup reads 21 rows, so when `truncated` is true, `count` is 21, a lower bound.
  - The tool description says: call again with `label` and `file` from one candidate.
  - Typical cases are a `Module` and a `File` on the same path, a `Runbook` and a `File` on the same path, and a same-named `Function` in several files.
- **None:** a tool error (`ToolError`, whose text the MCP SDK passes to the client; it hides a `ValueError`'s text behind `Error executing tool describe_node`).
  - It names the repository it searched and the filters used.
  - It lists up to five "did you mean" refs, from a second read-only query: a case-insensitive `CONTAINS` on `name` or `path`, under the same label set, repository and `Repository` exclusion.
  - It ends with the hint that `search_component` searches by partial name, and that a file, folder or docs node is named by its repo-relative path or its id.
  - Every caller value echoed in the message is cut to 100 characters.

**Not chosen:**

- An opaque node id (Neo4j `elementId`): it changes on every re-index, and an assistant can't make one up.
- A path-only argument: built-in symbols have no path.

### D2: output

Found:

```json
{
  "status": "found",
  "node": {"label": "Runbook", "name": "runbooks/api-outage.md",
           "file": "runbooks/api-outage.md",
           "properties": {"path": "runbooks/api-outage.md", "owner": "platform-team", "on_call": "api-oncall"},
           "properties_truncated": false},
  "outgoing": {"RUNBOOK_FOR": {"count": 2, "results": [
      {"label": "Service", "name": "api", "file": "deploy/docker-compose.yml"},
      {"label": "Service", "name": "api", "file": "docker-compose.yml"}], "truncated": false}},
  "incoming": {},
  "groups_truncated": false
}
```

- **Refs** (`{label, name, file}`) are both the neighbour's description and the next call's arguments.
  - `label` is `labels(n)[0]`, the convention the engine's provider queries already use.
  - `file` is `coalesce(n.file, n.path)` and is left out when null. A `Module` needs no file, because its name is its path.
  - Refs are built from the raw values, never passed through the sanitiser, so `describe_node(name=ref.name, label=ref.label, file=ref.file)` always hits exactly that node.
  - Names and paths are written by the indexer, from file paths, symbols and validated front-matter ids, and are already short and free of control characters.
- **Properties:** every property, except:
  - `repo_id` and `name`, which are already shown;
  - the extractor bookkeeping `claims` and `extractor`;
  - `insight_*`, which `key_nodes` and `find_communities` serve.

  `sources` is kept: on a shared node (a `Container` or `Database` several compose files declare) it is the provenance, the files that declare it. A compose `Service` is file-scoped and carries `source` (its one compose file), not `sources`. So are `path`, `file`, `source_file`, `source`, `created_at` and `last_modified_*`. No embedding property exists today, so there is no rule for one.

  Display values go through `tool_plane._sanitize_deep`: control characters are stripped, strings are capped at 500 characters, and temporal values become ISO strings. On top of that, list values are capped at 20 items, and at most 50 properties are returned in key order, with `properties_truncated` saying whether any were dropped.
- **Groups.** `outgoing` and `incoming` map each relationship type to an envelope with the same `{count, results, truncated}` shape as the other tools.
  - `count` is the exact number of distinct neighbours.
  - `results` holds the first `max_per_type` refs, sorted by name and then file.
  - `truncated` is `count > len(results)`.
  - Only types with at least one neighbour appear, and the maps are sorted by type.
  - Parallel edges of one type to the same neighbour count once.
  - A self-loop shows in both directions.
  - `groups_truncated` is true when the node had more than 200 (direction, type) groups and the rest were not read. In practice it never fires, because a repository has a few dozen relationship types.
- **Edge properties are not returned.** None of the provider relationships carries any. Built-in edges carry extraction details that the purpose-built tools already present.
- Neighbours are filtered to the same `repo_id`, per Design Brief Principle 3, and never include `Repository`.

**The groups query** keeps every group's work bounded, and it has three parts:

1. **Match the node.** `MATCH (n) WHERE elementId(n) = $id`, using the id from the lookup.
2. **Count the neighbours.** A `CALL (n) { ... UNION ... }` with two guarded branches:
   - out: `MATCH (n)-[r]->(m) WHERE $direction IN ['both','out'] AND <neighbour filters> RETURN 'out' AS dir, type(r) AS rel, m`;
   - in: the same with `(n)<-[r]-(m)` and `['both','in']`.

   `direction` is a parameter, never interpolated. The `UNION` drops duplicate `(dir, rel, m)` rows, which collapses parallel edges, while a self-loop appears once in each direction. Then `WITH dir, rel, count(DISTINCT m) AS total ORDER BY dir, rel`, so the count is taken without collecting anything.
3. **Take the first refs.** A `CALL (n, dir, rel) { ... }` per group re-matches only that direction and type: an inner `UNION` of `MATCH (n)-[r:$(rel)]->(m) WHERE dir = 'out'` and `MATCH (n)<-[r:$(rel)]-(m) WHERE dir = 'in'`, so it expands only that type's relationships (`rel` comes from the graph; no text is interpolated). It then applies the repository, `Repository` and label filters, and does `WITH DISTINCT m ORDER BY m.name, coalesce(m.file, m.path) LIMIT $cap`, then `RETURN collect({label: labels(m)[0], name: m.name, file: coalesce(m.file, m.path)}) AS refs`. That gives a top-N, so a node with 50,000 incoming `CALLS` never builds a 50,000-element list.

The neighbour filters are:

- `m.repo_id = $repo_id AND NOT m:Repository`;
- `($types IS NULL OR type(r) IN $types)`;
- `($labels IS NULL OR any(l IN labels(m) WHERE l IN $labels))`.

The scoped `CALL (...) { }` form matches what `summarise_repository` already uses.

### D3: filters and caps

- **`direction`:** `"both"` (the default), `"out"` or `"in"`. Any other value is an error. With `"out"`, `incoming` is `{}`, and the reverse for `"in"`.
- **`relationship_types`:** a list of up to 20 types. Each must fullmatch `RELATIONSHIP_TYPE_PATTERN`, otherwise the call fails and names the offending value, cut to 100 characters. They are matched as a value (`type(r) IN $types`). A valid type with no edges yields no group. `None` or `[]` means no filter; a bare string is an error, never split into characters.
- **`neighbor_labels`:** the same rules with `LABEL_PATTERN`, matched as a value.
- **`max_per_type`:** default 10, clamped to 1..50, the way `find_dependency_cycles` clamps `max_length`.
- The filters apply to relationships only. The node lookup uses `label` and `file`.

### D4: name and catalog

- **One tool, `describe_node`.** A separate `neighbors` tool would be the same query without the properties, and `direction` plus the filters already narrow the output. Two tools would only double what the catalog has to explain.
- **The catalog entry:**

  ```python
  {"name": "describe_node",
   "identifier_kind": "node name (any label; a file, folder or docs node by repo-relative path or front-matter id), optional label and file to disambiguate",
   "envelope": False,
   "phase": 3,
   "note": "the response is not an envelope; each relationship group inside outgoing/incoming is {count, results, truncated}"}
  ```

  `builtin_tool_names()` derives from the catalog, so this alone locks the name.
- **Collisions:** no tool in this repository, its test fixtures, its docs, or the local global store is named `describe_node`. The existing shadow rule applies unchanged:
  - a project or global tool named `describe_node` is ignored;
  - `devgraph://project-tools`, `config validate`, `config tools list` and `doctor` report `ignored: project tool 'describe_node' shadows a locked tool; using the fixed implementation`;
  - the built-in's dict result carries the same notice.
- **The built-in count goes from 24 to 25.** Four tests assert it:
  - `test_server.py::test_all_24_tools_registered`, renamed `test_all_25_tools_registered`;
  - `test_server_telemetry.py`;
  - `test_tools_cycles.py`;
  - `test_repo_default.py::test_builtins_with_repo_id_are_exactly_the_24`, renamed `..._the_25`, plus a `MIN_ARGS` entry.

  The dated PROJECT_STATUS history lines that say 24 stay as they are.

### D5: repository scope

- **`repo_id: str` in the definition.** `_with_repo_default` makes it optional, so an omitted value uses the session repository, and the found or ambiguous dict gains `repo_id` and a notice. A not-found error names the repository it searched.
- **No `cross_repo`.** Nodes and edges are written within one repository, and nothing links across repositories, so `cross_repo` would only widen the ambiguity. Pass `repo_id` to look at another repository. The `cross_repo` rule in the server instructions is untouched.
- **The server-side tool passes `declared_node_labels(registry, repo_id)`** into `tools.describe_node`, as `search_component` does.

### D6: safety

- **Queries.** `describe_node(engine, repo_id, name, label, file, direction, relationship_types, neighbor_labels, max_per_type, declared_labels=())` in `tools.py` runs, each through `engine.run_read_cypher`:
  - a lookup with `LIMIT 21`;
  - on no match, a suggestions query with `LIMIT 5`;
  - on a single match, the groups query.

  `run_read_cypher` uses a READ_ACCESS session, so the server refuses writes. Each query gets `timeout_s=DEFAULT_TIMEOUT_S` (10s, the project-tools default). The row caps are 21, 5 and 200. A capped groups read sets `groups_truncated`.

  The other built-ins use the unbounded `engine.run_cypher`. That is left alone here.
- **Values.** Every value is a parameter: `name`, `file`, `repo_id`, the filter lists, `direction` and the cap. The only interpolation is labels: built-in constants, or a `LABEL_PATTERN` fullmatch, inside backticks, as `search_component` does. Relationship types and neighbour labels are never interpolated.
- **Errors.** A Neo4j error whose code contains `TransactionTimedOut` (a substring match, as in `tool_plane._failure`) becomes `describe_node timed out after 10s; narrow it with label, file, relationship_types or neighbor_labels`. Any other Neo4j error becomes `describe_node failed: <code>`, never the raw message. Every caller-facing error (not found, bad direction, label or filter, timeout) is a `ToolError`, so its text reaches the client.
- **Races.** If the node is deleted between the lookup and the groups query, the groups query returns no rows. The result is the node as looked up, with empty maps: the same snapshot a later call corrects.
- **Telemetry** is unchanged: metadata only, never arguments.

### D7: docs

- **DEVGRAPH-CLIENT.md:**
  - a table row ("What is X connected to? / follow X's links");
  - a short "Walking the graph" section:
    - the walk: `search_component` → `describe_node(name)` → pick a ref → `describe_node(**ref)`;
    - it is one hop per call, at most 50 neighbours per relationship type, and `count` gives the full total;
    - the filters (`direction`, `relationship_types`, `neighbor_labels`) are how to narrow a busy node, and paging is not supported;
    - what `ambiguous` means;
  - the identifier-nuance paragraph: it takes a name, or a path or id for file, folder and docs nodes;
  - the response-shape paragraph: the per-group envelopes.
- **README:**
  - Project schema: `search_component` finds filesystem and docs nodes, and `describe_node` shows their fields and links;
  - the runbook example: `describe_node` on the runbook shows the `RUNBOOK_FOR` link.
- **PROJECT_STATUS:** a shipped entry saying 25 built-in tools, and `describe_node` in the `devgraph/mcp/` tool list.

## Non-goals

- Multi-hop traversal, path finding, or paging past the 50-per-type cap. One hop per call keeps every response bounded. `impact_analysis` and `trace_request_flow` cover the deep built-in walks.
- Edge properties, fuzzy lookup (`search_component`'s job) and source text (`get_source`'s).
- Changing the other built-ins' unbounded `run_cypher` reads.
