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
| D1 | **Identify by `name`, narrowed by optional `label` and `file`.** Several matches return capped candidates; none is an error with suggestions. |
| D2 | **Output:** the node's properties minus bookkeeping, then `outgoing`/`incoming` maps of relationship type to a capped `{count, results, truncated}` envelope of neighbour refs. |
| D3 | **Filters:** `direction`, `relationship_types`, `neighbor_labels`; cap `max_per_type`. |
| D4 | **One tool, `describe_node`,** added to the locked built-in catalog. No separate `neighbors`. |
| D5 | **`repo_id` follows the session default; no `cross_repo`.** |
| D6 | **Safety:** every user value is a query parameter; the one interpolated identifier is pattern-validated; both queries run read-only under a timeout and row cap. |
| D7 | **Docs** teach the walk: `search_component` to find, `describe_node` to look and step. |

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

Every node has `name`, and every provider node has `path`, so the match is:

- `n.repo_id = $repo_id AND (n.name = $name OR n.path = $name)`. Passing a docs
  file's path finds its id-keyed node too. Exact and case-sensitive, like the
  keys themselves. `search_component` remains the fuzzy finder.
- `label`, when given, narrows to that label. It is validated against
  `LABEL_PATTERN` and then interpolated as `` MATCH (n:`Label` ...) `` so the
  lookup uses the label's index. Without `label` the match is unlabelled,
  bounded by the timeout (D6).
- `file`, when given, keeps nodes where `$file` equals `n.file`, `n.path`,
  `n.source_file` or `n.source`, or is in `n.sources`. It tells apart a
  `Function main` in two files, or a `Service api` in two compose files.

**Results:**

- **Exactly one match:** describe it (D2).
- **Several:** not an error. Return `{"status": "ambiguous", "count", "candidates", "truncated"}`, where `candidates` holds up to 20 neighbour refs (D2) sorted by label, name and file. The lookup reads 21 rows, so `count` is a lower bound when `truncated` is true. The tool description says: call again with `label` and `file` from one candidate. Typical cases are a `Module` and a `File` on the same path, a `Runbook` and a `File` on the same path, and a same-named `Function` in several files.
- **None:** a tool error (`ValueError`, as the existing tools raise), naming the repository and the filters used, with up to five "did you mean" refs. These come from a second read-only query: a case-insensitive `CONTAINS` on `name` or `path`, under the same label filter and repository. The message ends with the hint that `search_component` searches by partial name, and that a file, folder or docs node is named by its repo-relative path or its id.

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
           "properties": {"path": "runbooks/api-outage.md", "owner": "platform-team", "on_call": "api-oncall"}},
  "outgoing": {"RUNBOOK_FOR": {"count": 2, "results": [
      {"label": "Service", "name": "api", "file": "docker-compose.yml"},
      {"label": "Service", "name": "api", "file": "docker-compose.prod.yml"}], "truncated": false}},
  "incoming": {"IS_CHILD_OF": {"count": 0, "results": [], "truncated": false}}
}
```

(An empty group is never returned. It is shown here only to give the shape.)

- **The ref** `{label, name, file}` is both the neighbour's description and the next call's arguments:
  - `label` is `labels(n)[0]`, the convention the engine's provider queries already use;
  - `file` is `coalesce(n.file, n.path)` and is left out when null. A `Module` needs no file, because its name is its path.
  - Calling `describe_node(name=ref.name, label=ref.label, file=ref.file)` hits exactly that node, because `file` matches `n.file` or `n.path` (D1).
- **Properties:** every property, except:
  - `repo_id` and `name`, which are already shown;
  - the extractor bookkeeping `claims`, `sources` and `extractor`;
  - `insight_*`, which `key_nodes` and `find_communities` serve.

  `path`, `file`, `source_file`, `source`, `created_at` and `last_modified_*` are kept. No embedding property exists today, so there is no rule for one. Values go through `tool_plane._sanitize_deep`: control characters are stripped, strings are capped at 500 characters, and temporal values become ISO strings.
- **Groups.** `outgoing` and `incoming` map each relationship type to an envelope. The envelope has the same `{count, results, truncated}` shape as the other tools.
  - `count` is the exact number of distinct neighbours.
  - `results` holds the first `max_per_type` refs, sorted by name and then file.
  - `truncated` is `count > len(results)`.
  - Only types with at least one neighbour appear. The maps are sorted by type.
  - Parallel edges of one type to the same neighbour count once.
  - A self-loop shows in both directions.
- **Edge properties are not returned.** None of the provider relationships carries any. Built-in edges carry extraction details that the purpose-built tools already present.
- Neighbours are filtered to the same `repo_id`, per Design Brief Principle 3.

### D3: filters and caps

- **`direction`:** `"both"` (the default), `"out"` or `"in"`. Any other value is an error. With `"out"`, `incoming` is `{}`, and the reverse for `"in"`.
- **`relationship_types`:** a list of up to 20 types. Each must fullmatch `RELATIONSHIP_TYPE_PATTERN`, otherwise the call fails and names the offending value, sanitised and truncated. They are matched as a value (`type(r) IN $types`). A valid type with no edges yields no group. `None` or `[]` means no filter.
- **`neighbor_labels`:** the same rules with `LABEL_PATTERN`, matched as `any(l IN labels(m) WHERE l IN $labels)`.
- **`max_per_type`:** default 10, clamped to 1..50, the way `find_dependency_cycles` clamps `max_length`.
- The filters apply to relationships only. The node lookup uses `label` and `file`.

### D4: name and catalog

- **One tool, `describe_node`.** A separate `neighbors` tool would be the same query without the properties, and `direction` plus the filters already narrow the output. Two tools would only double what the catalog has to explain.
- It is added to `TOOL_CATALOG` as `{"name": "describe_node", "identifier_kind": "node name (any label; a file, folder or docs node by repo-relative path or front-matter id), optional label and file to disambiguate", "envelope": True, "phase": 3}`. `builtin_tool_names()` derives from the catalog, so this alone locks the name.
- **Collisions:** no tool in this repository, its test fixtures, its docs, or the local global store is named `describe_node`. The existing shadow rule applies unchanged: a project or global tool named `describe_node` is ignored, `devgraph://project-tools`, `config validate`, `config tools list` and `doctor` report `ignored: project tool 'describe_node' shadows a locked tool; using the fixed implementation`, and the built-in's dict result carries the same notice.
- The built-in count goes from 24 to 25. Four tests assert it: `test_server.py`, `test_server_telemetry.py`, `test_tools_cycles.py` and `test_repo_default.py`, the last with a `MIN_ARGS` entry.

### D5: repository scope

- **`repo_id: str` in the definition.** `_with_repo_default` makes it optional, so an omitted value uses the session repository, and the found or ambiguous dict gains `repo_id` and a notice. A not-found error names the repository it searched.
- **No `cross_repo`.** Nodes and edges are written within one repository, and nothing links across repositories, so `cross_repo` would only widen the ambiguity. Pass `repo_id` to look at another repository. The `cross_repo` rule in the server instructions is untouched.

### D6: safety

- **Queries.** `describe_node(engine, repo_id, name, label, file, direction, relationship_types, neighbor_labels, max_per_type)` in `tools.py` runs at most two queries, each through `engine.run_read_cypher`:
  - a lookup, `LIMIT 21`;
  - the groups query, run only for a single match and keyed on the matched node's `elementId`.

  `run_read_cypher` uses a READ_ACCESS session, so the server refuses writes. Each query gets `timeout_s=DEFAULT_TIMEOUT_S` (10s, the project-tools default) and a row cap: 21 for the lookup, 200 groups.

  Note that the other built-ins use the unbounded `engine.run_cypher`. That is left alone here.
- **Values.** Every value is a parameter: `name`, `file`, `repo_id`, the filter lists, `direction` and the cap. The only interpolation is `label`, after a `LABEL_PATTERN` fullmatch, inside backticks, as `search_component` does for declared labels. Relationship types and neighbour labels are never interpolated.
- **Errors.** A Neo4j timeout becomes `describe_node timed out after 10s; narrow it with label, file, relationship_types or neighbor_labels`. Any other Neo4j error becomes its status code only, as in `tool_plane._failure`.
- **Races.** If the node is deleted between the two queries, the result has the node and no groups. That is the same snapshot a later call would correct.
- **Telemetry** is unchanged: metadata only, never arguments.

### D7: docs

- **DEVGRAPH-CLIENT.md:**
  - a table row ("What is X connected to? / follow X's links");
  - a short "Walking the graph" section: `search_component` → `describe_node(name)` → pick a ref → `describe_node(**ref)`, with what `ambiguous` means and how filters keep a busy node small;
  - the identifier-nuance paragraph: it takes a name, or a path for file, folder and docs nodes;
  - the response-shape paragraph: per-group envelopes.
- **README:**
  - Project schema: `search_component` finds filesystem and docs nodes, and `describe_node` shows their fields and links;
  - the runbook example: `describe_node` on the runbook shows the `RUNBOOK_FOR` link.
- **PROJECT_STATUS:** a shipped entry, and the `devgraph/mcp/` map line.

## Non-goals

- Multi-hop traversal or path finding. One hop per call keeps every response bounded. `impact_analysis` and `trace_request_flow` cover the deep built-in walks.
- Edge properties, fuzzy lookup (`search_component`'s job) and source text (`get_source`'s).
- Changing the other built-ins' unbounded `run_cypher` reads.
