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

## 1. Decisions

| # | Decision |
| --- | --- |
| D1 | A declarative provider is a `source` on a node type (which files become nodes, and which values become properties) plus relationships with the same `provider` (which value names the target node). No other shape. |
| D2 | **The first slice is `docs`: Markdown front matter.** `git` and `ast` come later, in the same mapping language. |
| D3 | The mapping language has four parts: path globs, `equals`/`matches` conditions on front-matter fields, a property-to-field map, and relationships found by key lookup. No expressions, no templating and no computed values. |
| D4 | A docs-sourced node type is keyed `[path]`, as a filesystem type is. Keys taken from a front-matter field (an `id` such as `ADR-012`) are deferred to a later slice. |
| D5 | The provider owns its nodes by `extractor = "docs"`. Its lifecycle reuses the filesystem provider's engine calls, plus two new path-scoped calls. |
| D6 | Schema changes reuse the applied-schema hash, the debounced rescan and the removed-type cleanup unchanged. |

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
      where:                               # optional; all must hold
        - {field: type, equals: runbook}
        - {field: title, matches: "^RB-"}
      fields: {on_call: on-call-team}      # optional; metadata name -> front-matter key
relationships:
  - type: RUNBOOK_FOR
    provider: docs
    from: Runbook
    to: Service
    field: service                         # front-matter key naming the target
```

### `source` (provider `docs`)

- `NodeSource` becomes a union, discriminated on `provider`: the existing filesystem form `{provider: filesystem, kind}` or the docs form `{provider: docs, paths, where?, fields?}`. `NODE_SOURCE_PROVIDERS` gains `docs`.
- **`paths`** holds 1 to 20 globs. Each glob is relative, POSIX, at most 200 characters, with no `..` segment and no leading `/`. It is matched against the repo-relative path with `PurePosixPath.full_match`, so `**` works. Only `.md` and `.markdown` files are considered.
- **`where`** lists conditions, all of which must hold. Each is `{field, equals}` or `{field, matches}`, never both:
  - `equals` takes a string, integer or boolean. It holds when the field's value equals it, or when the value is a list and one item equals it (`tags: [runbook, oncall]`).
  - `matches` takes a Python regex of at most 200 characters, compiled during validation, so a bad pattern is a validation error. It is applied with `re.search` to string values (or list items) of at most 1,000 characters.
  - Without a `where`, every file matching `paths` becomes a node, with or without front matter.
- **`fields`** renames. A metadata field is filled from the front-matter key of the same name unless `fields` maps it to another key. Every key in `fields` must be a declared metadata field other than `path`. Front-matter key names are 1 to 64 characters and contain no control characters.
- **Key.** The key must be exactly `[path]`, with `path` a string field. This is the rule filesystem types already follow, so the existing validator applies unchanged.
- **Limits.** The current "at most one type per kind" rule applies to filesystem types only. Any number of node types may be sourced from docs, and one file may become a node of several of them.

### Relationships (provider `docs`)

- `PROVIDER_KINDS` gains `docs`. A docs relationship needs `field`, has no `custom` block, and its type is not built in.
- Every `from` label must be a docs-sourced node type. `to` may be any label in the effective schema, built-in or declared; the existing endpoint check enforces this.
- `field` is allowed only on docs relationships.
- **Key lookup.** The field's value is a string, an integer, or a list of at most 100 of these. Each value names one target: one edge to the `to` node whose `name` equals it. For every node DevGraph writes, `name` is the key: a Service's name, a Module's repo-relative path, a declared type's `path`. An edge whose target does not exist is skipped, as every cross-extractor edge already is.

### Property values

- A declared metadata type is honoured or the property is left unset. A `string` field takes a string, integer, float or boolean, written as text. An `integer`, `float` or `boolean` field takes a value of exactly that YAML type. Lists, maps and dates never become properties.
- A document missing a `required` field is skipped. Doctor reports it (§5).
- Every node carries `name = path = <repo-relative path>` and `extractor = "docs"`. Nothing else is written. Reserved properties cannot be declared, as today.

## 3. Safety properties

1. **No repository code runs, ever.** The provider reads bytes, parses YAML with `bounded_safe_load`, and compares strings. Regexes run in Python's `re` module, never in a shell.
2. **Reads are bounded.** A file is read with `read_bounded` (default cap `MAX_CONFIG_BYTES`, 1 MiB), so a FIFO, a device or an oversized file is never read. Front matter goes through `bounded_safe_load` with `YAML_MAX_NODES`. List fields are capped at 100 items, and regex input at 1,000 characters.
3. **Files are scoped exactly like the existing extractors.** The candidate set is `_indexable_paths`: regular files outside `IGNORED_DIR_NAMES`, with symlinks resolving outside the repository skipped (`is_within`). DevGraph has no separate `.gitignore` or tracked-file logic, and this provider adds none, so it sees exactly the files every other extractor sees.
4. **Only declared names are written.** Labels, relationship types and property names come only from the validated schema. Each one fullmatches its identifier pattern before it reaches Cypher. Front-matter keys and values only ever travel as query parameters.
5. **Writes MERGE on the declared key.** Nodes MERGE on `(repo_id, name)`, where `name` is the declared key `path`. The `(repo_id, path)` uniqueness constraint and the `(repo_id, name)` index are provisioned as for filesystem types.
6. **The provider never touches other nodes.** It deletes only nodes tagged `extractor = "docs"` and only edges of docs relationship types leaving them. Built-in nodes and filesystem nodes are never touched.
7. **The provider fails closed.** An invalid schema skips the provider entirely, prune included, so a bad edit never deletes good nodes (the `_filesystem_spec` rule). A malformed file is skipped and the rest of the batch continues.

ReDoS from a user's own pattern against their own repository is accepted. Python's `re` has no timeout, and the length caps keep the input small.

## 4. Lifecycle and rescan

- **Schema edits (D6).** A changed `devgraph.schema.yaml` changes `schema_file_hash`. The repository goes pending and the debounced schema rescan runs `full_scan`. Inside it, `apply_project_schema` runs as follows:
  1. Delete the labels and relationship types the schema no longer declares (existing step).
  2. Delete every docs edge leaving a docs node.
  3. `prune_extracted_nodes(extractor="docs", keep=…)` with the nodes the new mapping produces.
  4. Upsert those nodes.
- **Edges come after the built-ins.** Edges are written after `index_paths`, so built-in targets (Service, Module) exist by then. `full_scan` gains one final `docs.sync_edges` over every matched file.
- **Edits and creates, from the watcher's `index_paths`, when the schema is not pending.** For the changed Markdown paths, the provider:
  1. deletes docs edges leaving nodes at those paths (`delete_extracted_edges`);
  2. upserts the produced nodes;
  3. prunes docs nodes at those paths that are no longer produced (`prune_extracted_at`, for example when a `where` no longer holds);
  4. upserts edges.

  Nodes MERGE in place, so edges coming in from other docs nodes survive.
- **Relink.** Built-in per-file replacement DETACH DELETEs and recreates a code file's nodes, which removes docs edges pointing at them. When an `index_paths` batch wrote nodes of a label that some docs relationship targets, the provider re-derives edges for all matched docs files and upserts the ones whose target is in the batch. This is the same idea as `_docs_note_referrers`: it re-reads glob-limited files and never reads the graph.
- **Deletes, from `remove_paths`.** `delete_extracted_nodes(repo_id, "docs", paths)` removes the deleted paths' nodes and their edges, including everything below a deleted directory.
- **Known limit.** An edge whose target first appears in a later batch (a Service added after the runbook) is made on the next save of the source file or at the next rescan.

## 5. How it shows up

- **Validation.** `devgraph config validate`, the Config page dry run and `devgraph config schema add/edit` all use the loader, so they report the new rules automatically. Messages are in plain words. Examples:
  - "node type 'Runbook' reads Markdown front matter, so its key must be exactly [path]";
  - "where[1] of 'Runbook': 'matches' is not a valid pattern: …";
  - "fields of 'Runbook' maps 'owner', which is not a declared metadata field";
  - "relationship 'RUNBOOK_FOR' uses the docs provider, so it needs a 'field'".
- **`devgraph doctor`.** The Project schemas section gains one line per docs-sourced type, from a pure `docs.source_report(repo_root, effective)`. It touches no graph. Examples:
  - OK: "Runbook: 12 files match, 10 nodes".
  - Warning: "Runbook: 2 skipped (1 malformed front matter, 1 missing required owner)".
  - Warning: "Runbook: no file matches runbooks/**/*.md".
- **The Config page form.** The node-type Source select gains "Docs front matter". When it is picked, the form shows:
  - Paths, as rows with "Add path";
  - Conditions, as rows of Field, Test (equals or matches) and Value;
  - a "Front-matter key" input on each metadata row, blank meaning the same name.

  The relationship Provider select gains "Docs front matter", which shows a Field input. Entries the form can't show exactly open in YAML with a reason, as today. One example is an `equals` value that is not a string, because its type would be lost in a text input. The drift guard covers the new models and constants.
- **Graph view and MCP.** Declared labels are already rendered with their `color`, and `search_component` already matches a repository's declared labels. No change.
- **Starter template.** The commented example in `starter_schema_text` becomes the Runbook example above, replacing the `custom` relationship.

## 6. Out of scope

- `git` and `ast` providers, and keys from front-matter fields (D4).
- Body or heading extraction (title, sections).
- Nested front-matter paths (`a.b`).
- Composite keys, and edges whose source is not a docs node.
- The `custom` provider stays parked and validated as inert data.

## 7. Testing

- Loader tests for every rule in §2.
- Pure provider tests: globs, conditions, coercion, required fields, relationship values, bounds and hostile files (FIFO, oversized, outside symlink, YAML bomb).
- Live Neo4j tests for the two new engine calls.
- End-to-end live tests on a temporary repository: full scan; edit, un-match, delete and directory delete; schema edit, removal and an invalid schema; relink after a code file is re-indexed; a repository without docs sources produces an identical graph.
- Doctor output tests.
- Config form round-trip and drift tests.
