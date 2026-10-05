# Declarative providers — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1 (per-project graph schema). The
epic lists the providers `filesystem`, `git`, `ast`, `docs` and `custom`.
`filesystem` ships (`2026-10-04-filesystem-provider-design.md`). `custom`
(user scripts) is parked, because it would run repository code. This design
is the safe alternative: a **declarative provider** fills a node type or a
relationship declared in `devgraph.schema.yaml` from data DevGraph already
reads. Configuration selects and maps that data. Nothing is executed. The
whole feature is configured with YAML or with the dashboard Config page form,
never with code.

The threat model matters here. `devgraph.schema.yaml` ships inside the
repository, project config is on by default, and the schema is not
trust-gated. A cloned repository therefore controls every value in this
mapping language, and nothing in it may be able to hang, crash or escape the
indexer.

## 1. Decisions

| # | Decision |
| --- | --- |
| D1 | A declarative provider is a `source` on a node type (which files become nodes, and which values become properties) plus relationships with the same `provider` (which value names the target node). No other shape. |
| D2 | **The first slice is `docs`: Markdown front matter.** `git` and `ast` come later, in the same mapping language. |
| D3 | The mapping language has four parts: path globs, plain text conditions on front-matter fields (`is`, `starts_with`, `contains`, `like`), a property-to-field map, and relationships found by key lookup. There are no regexes, no expressions, no templating and no computed values. |
| D4 | A docs-sourced node type is keyed `[path]`, as a filesystem type is. Keys taken from a front-matter field (an `id` such as `ADR-012`) are the next slice. Until then, a docs→docs edge value is the target file's repo-relative path. |
| D5 | The provider owns its nodes by `extractor = "docs"`. Its lifecycle reuses the filesystem provider's engine calls, plus path-scoped prune, edge-delete and property-clear calls. |
| D6 | Schema changes reuse the applied-schema hash, the debounced rescan and the removed-type cleanup unchanged. |
| D7 | Lookups into Service, Class and Function **fan out**. These labels are keyed `(repo_id, name, file)`, so `service: api` links to every Service named `api` (one per compose file that declares it). This is documented and tested, not refused, because Service is the headline target. |

### Why `docs` first (D2)

- **It is file-scoped.** One Markdown file gives at most one node per type. The create, edit, delete and prune lifecycle is therefore the filesystem provider's, which already works and is already tested: `delete_extracted_nodes`, `prune_extracted_nodes`, the `<label>_repo_name` index and `[path]` keys.
- **The reading code already exists.** `devgraph/indexer/docs/extractor.py` parses front matter with `bounded_safe_load`. The provider adds only selection and mapping on top.
- **It is the most useful to a non-developer.** Runbooks, ADRs, policies and service pages are written by people who do not write code. Front matter is the metadata they already keep.
- **`git` costs more.** History is walked incrementally from `last_indexed_commit`. A commit has no file to own it, so pruning after a schema edit needs a full history re-walk, and there is no per-file lifecycle to reuse.
- **`ast` costs the most.** Decorator and annotation support differs across the eight language extractors. A uniform `decorated_with` selector would touch all of them.

## 2. Schema format

```yaml
version: 1
node_types:
  - label: Runbook
    key: [path]
    metadata:
      - {name: path}
      - {name: owner, required: true}
      - {name: severity, type: integer}
      - {name: on_call}
    source:
      provider: docs
      paths: ["runbooks/**/*.md"]          # 1–20 repo-relative globs
      where:                               # optional, at most 20; all must hold
        - {field: type, is: runbook}
        - {field: title, starts_with: "RB-"}
      fields: {on_call: on-call-team}      # optional; metadata name -> front-matter key
relationships:
  - type: RUNBOOK_FOR
    provider: docs
    from: Runbook
    to: Service
    field: service                         # front-matter key naming the target
```

### `source` (provider `docs`)

- `NodeSource` becomes `FilesystemSource | DocsSource`, discriminated on `provider`. The filesystem form is unchanged; the docs form is `{provider: docs, paths, where?, fields?}`. `NODE_SOURCE_PROVIDERS` gains `docs`.
- **`paths`** holds 1 to 20 globs. A glob is rejected when it:
  - is empty or longer than 200 characters;
  - has a `..` segment, a leading `/`, or a backslash.

  Matching uses `PurePosixPath.full_match` on the repo-relative path. It is case-sensitive, and `**` spans folders. The form hints "use `**/*.md` for every folder". Only `.md` and `.markdown` files are considered.
- **`where`** lists at most 20 conditions, all of which must hold. Each is `{field, <operator>: text}` with exactly one operator. The text is at most 200 characters.

  | Operator | Holds when the value… | Form wording |
  | --- | --- | --- |
  | `is` | equals the text | "is" |
  | `starts_with` | begins with the text | "starts with" |
  | `contains` | contains the text | "contains" |
  | `like` | matches the text, where `*` is any run of characters (`fnmatch.fnmatchcase`) | "looks like (use * as a wildcard)" |

  - **Values are compared as text on both sides, case-sensitively.** A string is itself, an integer is written in decimal, and a boolean is written `true`/`false`. So `is: 1` and `is: "1"` both match `version: 1`. The schema accepts a string, integer or boolean after an operator and stores its canonical text.
  - A list value holds when any item holds (`tags: [runbook, oncall]`). Floats, dates, maps and values longer than 4 KiB never hold.
  - Without a `where`, every file matching `paths` becomes a node, with or without front matter.
- **`fields`** renames, with at most 50 entries. A metadata field is filled from the front-matter key of the same name unless `fields` maps it to another key. Every key in `fields` must be a declared metadata field other than `path`. Front-matter key names are 1 to 64 characters with no control characters. Nested paths (`a.b`) are not supported.
- **Key.** The key must be exactly `[path]`, with `path` a string field. The `_check_key_and_metadata` message names the provider: "…is sourced from Markdown front matter, so its key must be exactly [path]".
- **Limits.** The current "at most one type per kind" rule (`_check_filesystem`) applies to filesystem sources only. Any number of node types may be sourced from docs, and one file may become a node of several docs types and also be a filesystem `File`.

### Relationships (provider `docs`)

- `PROVIDER_KINDS` gains `docs`, and `RelationshipDecl` gains `field`. A docs relationship needs `field`, has no `custom` block, and its type is not built in. `field` is allowed only on docs relationships. These are per-relationship checks in `_check_provider`.
- A new `ProjectSchema` model validator, `_check_docs`, requires every `from` label to be a docs-sourced node type. `to` may be any label in the effective schema; the existing endpoint check enforces this.
- **Key lookup.** The field's value is a string, an integer, or a list of at most 100 of these, each at most 4 KiB. Values of exactly type `str` or `int` count; a boolean never counts as an integer. A leading `./` is removed. Each value names one target: one edge to every `to` node whose `name` equals it. For every node DevGraph writes, `name` is the key:
  - a Service's name (fan-out, D7);
  - a Module's repo-relative path;
  - a docs or filesystem type's repo-relative `path`.

  An edge whose target does not exist is skipped, as every cross-extractor edge already is.

### Property values

`build_nodes` emits every declared metadata field. A field whose value is absent or can't be coerced is written as `None`, so `SET n += …` removes a stale property. Coercion uses exact type checks (`type(v) is int`):

| Declared type | Accepts | Unset when |
| --- | --- | --- |
| `string` | `str`, `int` (decimal), `bool` (`true`/`false`), `float` | longer than 4 KiB |
| `integer` | `int` that is not a bool | outside int64 |
| `float` | `float`, or `int` within int64 | — |
| `boolean` | `bool` | — |

- YAML 1.1 parsing means `yes`, `no`, `on` and `off` are booleans. In a `string` field they are written `true`/`false`, and the README says so.
- Lists, maps and dates never become properties.
- A document missing a `required` field is skipped. Doctor names it (§5).
- Every node carries `name = path = <repo-relative path>`, `extractor = "docs"` and the declared fields. Nothing else is written. Reserved properties cannot be declared, as today.

## 3. Safety properties

1. **No repository code runs, and no user-supplied pattern engine runs.** The provider reads bytes and parses YAML with `bounded_safe_load`. It applies four plain text operators. `like` uses `fnmatch.fnmatchcase`, whose translation has avoided catastrophic backtracking on `*` since Python 3.9. Globs use `PurePosixPath.full_match`. There are no regexes, because a cloned repository controls the schema (see the threat model at the top) and Python's `re` has no timeout. One crafted pattern could stall the watcher or doctor for hours.
2. **Reads are bounded.** A file is read with `read_bounded` (default cap `MAX_CONFIG_BYTES`, 1 MiB), so a FIFO, a device or an oversized file is never read. Front matter goes through `bounded_safe_load` with `YAML_MAX_NODES`. Limits:
   - globs, conditions and `fields` entries are counted and length-capped (§2);
   - edge lists hold at most 100 items;
   - compared or written strings are at most 4 KiB;
   - integers must fit int64.
3. **Files are scoped exactly like the existing extractors.** The candidate set is `indexable_paths`: regular files outside `IGNORED_DIR_NAMES`, with symlinks resolving outside the repository skipped (`is_within`). DevGraph has no separate `.gitignore` or tracked-file logic, and this provider adds none. These helpers move from `dispatch.py` to a new `devgraph/indexer/walk.py`, which `dispatch` re-exports, so the provider and doctor don't import the dispatcher (§5).
4. **Only declared names are written.** Labels, relationship types and property names come only from the validated schema. Each one fullmatches its identifier pattern before it reaches Cypher. Property names read back from the graph for clearing (§4) are re-validated the same way. Front-matter keys and values only ever travel as query parameters.
5. **Writes MERGE on the declared key.** Nodes MERGE on `(repo_id, name)`, where `name` is the declared key `path`. The `(repo_id, path)` uniqueness constraint and the `(repo_id, name)` index are provisioned as for filesystem types.
6. **The provider never touches other nodes.** It deletes only nodes tagged `extractor = "docs"`, and only outgoing non-built-in edges from them. Built-in nodes and filesystem nodes are never touched, including filesystem nodes at the same path.
7. **The provider fails closed.** Each of the following skips every docs write and delete, including prune, the final `sync_edges` and relink:
   - an invalid schema;
   - a pending schema (outside `apply_project_schema`);
   - `apply_project_schema` returning False.

   A bad file or value is skipped and counted. The docs node pass, edge pass and relink are each wrapped in `try/except` with a warning, like the docs-notes and mentions passes, so one failure never aborts a batch.

## 4. Lifecycle and rescan

- **One resolve per batch.** `index_paths` resolves the schema once per batch for both providers (`_provider_specs(repo_root) -> (ok, filesystem_spec, docs_spec)`).
- **Schema edits (D6).** A changed `devgraph.schema.yaml` changes `schema_file_hash`. The repository goes pending and the debounced schema rescan runs `full_scan`. Inside it, `apply_project_schema` runs:
  1. Delete the labels and relationship types the schema no longer declares (existing step).
  2. Delete every outgoing non-built-in edge from docs nodes, of any type, current or former (`delete_extracted_edges(…, paths=None)`).
  3. Clear properties the schema no longer declares on each docs label: every property except declared fields, reserved names and `insight_*`, re-validated against `PROPERTY_NAME_PATTERN`.
  4. Run `prune_extracted_nodes(extractor="docs", keep=…)` with the nodes the new mapping produces.
  5. Upsert those nodes.

  After `index_paths`, `full_scan` runs one final `docs.sync_edges` over every matched file, so built-in targets exist first. It runs only when apply returned True.
- **Edits and creates, from the watcher's `index_paths`, when the schema is valid and not pending.** For the changed Markdown paths, the provider:
  1. deletes outgoing docs edges at those paths (`delete_extracted_edges`);
  2. upserts the produced nodes, all declared fields included;
  3. prunes docs nodes at those paths that are no longer produced (`prune_extracted_at`);
  4. upserts their edges.

  Nodes MERGE in place, so incoming edges survive.
- **Relink, on added nodes only.** Built-in re-indexing MERGEs in place (`_replace_file_nodes_tx`, `_upsert_container_result`), so a re-indexed target keeps its incoming docs edges. Only a node that is new to the graph needs linking.
  - `_batch_nodes` is extended with the batch's compose and Containerfile Service nodes and its docs-provider nodes. `previous_nodes` is snapshotted the same way.
  - When `added_nodes` holds a label that some docs relationship targets, the provider re-derives edges for all matched docs files and upserts those whose target was added.
  - The relink re-reads the glob-limited files, as `_docs_note_referrers` does, and never reads the graph.
  - This covers a Service added after the runbook that names it, a docs→docs target created later, and a target deleted and then recreated.
- **Deletes, from `remove_paths`.** `delete_extracted_nodes(repo_id, "docs", paths)` removes the deleted paths' docs nodes and their edges, including everything below a deleted directory.
- **Known limit.** A target that appears without passing through `index_paths` gets its edge at the next rescan. Today that means git-history Commits, which no docs relationship is likely to target.

## 5. How it shows up

- **Validation.** `devgraph config validate`, the Config page dry run and `devgraph config schema add/edit` all use the loader, so they report the new rules automatically. Messages are in plain words. Examples:
  - "node type 'Runbook' is sourced from Markdown front matter, so its key must be exactly [path]";
  - "where[1] of 'Runbook' must use exactly one of is, starts_with, contains, like";
  - "fields of 'Runbook' maps 'owner', which is not a declared metadata field";
  - "relationship 'RUNBOOK_FOR' uses the docs provider, so it needs a 'field'".
- **Callers that read `source.kind`** become provider-aware:
  - `edits.pruned_types`/`schema_change_warnings` warn on a source removed, a provider changed (filesystem↔docs), a filesystem kind changed, or docs `paths`/`where` changed. Example: "the next rescan rebuilds Runbook entries from Markdown front matter".
  - `edits.schema_entry_notes` names both providers in its note.
  - `devgraph config schema list` shows `docs (runbooks/**/*.md)` or `filesystem (file)` in the Source column, and the docs fields in `--json`.
- **`devgraph doctor`.** The Project schemas section gains lines per docs-sourced type, from `docs.source_report(repo_root, effective, files)`. Doctor passes in `walk.indexable_paths(repo_root)`. The report touches no graph and imports no dispatcher. Examples:
  - OK: "Runbook: 12 files match, 10 Runbook entries".
  - Warning: "Runbook: no file matches runbooks/**/*.md (matching is case-sensitive; use **/*.md for every folder)".
  - Warning, naming up to 5 files with the reason and then "and N more":
    - "runbooks/db.md: missing required 'owner'";
    - "runbooks/x.md: front matter is not valid YAML";
    - "runbooks/y.md: 'severity' is not a whole number, left blank".
  - Edge values that match no target are a graph question, so `source_report` defers them explicitly. When Neo4j is reachable, doctor adds "Runbook → Service: 'paymnts' (runbooks/pay.md) matches no Service" for up to 5 values. Otherwise it prints "skipped: Neo4j is not reachable", as the drift section does.
- **The Config page form.** The node-type Source select gains "Markdown front matter". When it is picked, the form shows:
  - Paths, as rows with "Add path" and placeholder `runbooks/**/*.md`;
  - Conditions, as rows of Field, a Test select in plain words (§2 table) and Value;
  - a "Front-matter key" input on each metadata row, with help text "the name before the colon at the top of the file; leave blank if it's the same as the field name".

  Renaming a metadata row carries its `fields` entry. The relationship Provider select gains "Markdown front matter", which shows a Field input with the same help text.
  - A condition value from YAML that is an integer or boolean is shown as its canonical text and written back unchanged unless edited. It is not refused.
  - These entries open in YAML with a reason: unknown source keys, and a `fields` map whose key order differs from the metadata order (the form could not keep it).
  - `where` and `fields` are optional, so the `configFormFromEntry` all-fields-present check becomes provider-specific.
  - The drift guard covers `FilesystemSource`, `DocsSource`, `Condition` and the new constants.
- **Graph view and MCP.** Declared labels are already rendered with their `color`, and `search_component` already matches a repository's declared labels. No change.
- **Starter template.** The header no longer says extraction comes later: it says filesystem and Markdown front-matter sources are indexed. The commented example becomes the Runbook example above, replacing the `custom` relationship.
- **README.** Covers:
  - the four mapping parts;
  - that matching is case-sensitive;
  - the boolean wording;
  - the Service fan-out;
  - that docs→docs edge values are file paths for now, with front-matter keys the next slice.
- **CONTEXT.md.** Gains **Declarative provider** and **Docs source**. Today the glossary exists only on the coordinator branch; Task 6 adds the terms wherever it lives once merged, or creates it at the repo root in that format.

## 6. Out of scope

- `git` and `ast` providers, and keys from front-matter fields (D4).
- Regexes, which are refused for the safety reason in §3.1.
- Body or heading extraction (title, sections).
- Nested front-matter paths (`a.b`).
- Composite keys, and edges whose source is not a docs node.
- The `custom` provider stays parked and validated as inert data.

## 7. Testing

- Loader tests for every rule in §2, including the `_check_docs` model validator.
- Provider-aware tests for `pruned_types`, `schema_entry_notes` and `config schema list`, including filesystem↔docs transitions.
- Pure provider tests:
  - each operator and the text canonicalisation;
  - every coercion row, including bool-is-not-int, int64 overflow and 4 KiB strings;
  - `None` for absent fields;
  - `./` normalisation;
  - hostile files: FIFO, oversized, outside symlink and YAML bomb.
- Live Neo4j tests for the three new engine calls.
- End-to-end live tests on a temporary repository:
  - full scan;
  - edit, field removal, un-match, delete and directory delete;
  - schema edit, removal (stale property cleared) and an invalid schema;
  - relink when a compose file is added in a later batch, and on delete-then-recreate;
  - Service fan-out;
  - a bad value in one file does not stop the batch;
  - a filesystem `File` and a docs `Runbook` on the same path (each survives the other's un-match, delete, type removal and invalid schema);
  - a repository without docs sources produces an identical graph.
- Doctor output tests.
- Config form round-trip and drift tests.
