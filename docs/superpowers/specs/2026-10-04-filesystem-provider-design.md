# Filesystem provider — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1 (per-project graph schema epic).
PRs #21 and #23 delivered the schema file's loader, validation, constraint
provisioning and doctor reporting, but nothing indexes a declared node type
yet. This slice makes the epic's headline example work end to end: a schema
file declares `File` and `Folder` node types and an `IS_CHILD_OF`
relationship sourced from the filesystem, and they are indexed, kept current,
and queryable through MCP — with no change for repositories without a file.

## Schema format

```yaml
version: 1
node_types:
  - label: File
    key: [path]
    metadata: [{name: path}]
    source: {provider: filesystem, kind: file}
  - label: Folder
    key: [path]
    metadata: [{name: path}]
    source: {provider: filesystem, kind: folder}
relationships:
  - type: IS_CHILD_OF
    provider: filesystem
    from: [File, Folder]
    to: Folder
```

- `NodeTypeDecl.source` (optional): `{provider: filesystem, kind: file|folder}`.
  A filesystem node type must have `key: [path]` and a `path` metadata field
  of type `string`. At most one node type per kind.
- `PROVIDER_KINDS` gains `filesystem`. A filesystem relationship means
  "child → its parent folder": it must not carry a `custom` block, its type
  must not be built in, its `to` must be the folder-kind node type, and every
  `from` label must be a filesystem node type. At most one filesystem
  relationship.
- `from` accepts a single label or a list (existing files stay valid);
  `RelationshipDecl.from_labels` gives the tuple.
- `extractor` joins `RESERVED_NODE_PROPERTIES`: it is the epic's fixed
  "source extractor" metadata and the property this provider owns its nodes by.

## Provider

`devgraph/indexer/providers/filesystem.py`:

- Every regular, non-ignored file (the same set a full scan indexes) becomes a
  file-kind node; every ancestor directory of such a file becomes a
  folder-kind node, with `.` for the repository root.
- Node properties: `name = path = ` repo-relative POSIX path, `extractor =
  "filesystem"`. Because `name` equals the declared key, the engine's existing
  `(repo_id, name)` MERGE behaves exactly as a merge on `(repo_id, path)`, and
  the `(repo_id, path)` uniqueness constraint #23 provisions always holds.
  General declared-key MERGE is left for a later slice.
- Edges: each file (if its label is in `from`) and each non-root folder (if
  its label is in `from`) to its parent folder.
- These nodes carry no `file`/`source_file`/`source` properties, so built-in
  per-file replacement, deletion and pruning never touch them; the provider
  owns their lifecycle:
  - changed/created files: upsert the file, its ancestor folders and edges;
  - deleted paths (files or whole directories): delete provider nodes at that
    path or below it, then delete each ancestor folder that no longer
    contains any indexable file on disk;
  - full scan: prune every `extractor = "filesystem"` node in the repo that
    is not in the desired set — which also removes nodes of a type dropped
    from the schema, or all of them when the schema no longer declares any.

## Wiring

`index_paths`, `remove_paths` and `full_scan` (`devgraph/indexer/dispatch.py`)
resolve the repository's schema once per call and run the provider after
built-in extraction. Every caller (watcher, CLI add/rescan, dashboard
registration) goes through these. An invalid schema logs a warning and skips
the provider entirely — including the full-scan prune, so a bad edit never
deletes the last good filesystem nodes. Built-in indexing is unaffected.

## MCP

`search_component` matches only five built-in labels. For a repository whose
schema declares node types, it also matches those labels (resolved through the
registry). With no schema the generated Cypher is unchanged.

## Backward compatibility

A repository without `devgraph.schema.yaml` produces identical nodes and
relationships with and without this change; all existing tests and MCP tool
signatures are unchanged.

## Known limits (documented)

- A new or edited schema takes effect on the next `devgraph rescan`
  (hash-triggered rescans are the next slice). File edits update filesystem
  nodes live once the schema is in place.
- The dashboard's `?label=` graph filter still only accepts built-in labels
  (UI slice).

## Testing

Loader tests for every rule; engine tests for the delete/prune Cypher (live
Neo4j); end-to-end live tests on a temp repository (full scan, add, edit,
delete a file, delete a directory, ignored directories, schema removed,
invalid schema, backward-compatibility snapshot); MCP search tests; manual run
on a real repository.
