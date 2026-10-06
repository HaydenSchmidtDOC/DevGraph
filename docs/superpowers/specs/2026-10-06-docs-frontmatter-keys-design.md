# Docs front-matter keys — design addendum

Addendum to `2026-10-06-declarative-providers-design.md` (slice 1), which
deferred this in D4 and §6. Upstream epic: HaydenSchmidtDOC/DevGraph#1.
Everything slice 1 says still holds unless a section below replaces it.

Today a docs-sourced node type is keyed `[path]`, so a docs→docs link must
name the target's file (`supersedes: decisions/adr-012.md`). People write
ids, not paths. This slice lets a docs type take its key from one
front-matter field, so the link can say `supersedes: ADR-012`.

## 1. Decisions

| # | Decision |
| --- | --- |
| K1 | A docs type's key is `[path]` (unchanged) or **one** declared `string` metadata field other than `path`, read from front matter like any other field (`fields` may rename it: `{adr_id: id}`). Composite keys stay out of scope. Filesystem types stay `[path]` only. |
| K2 | Key values are **text**, compared exactly and case-sensitively. A `str` counts as itself and an `int` (not a bool, within int64) in decimal, so `id: 12` and `supersedes: 12` meet. Anything else, including floats, dates, lists and maps, is not a key. |
| K3 | **Duplicates: the first file by path wins.** When several files of one type claim the same key, the file whose repo-relative path sorts first (code-point order, the order `read_selected` already uses) becomes the entry. Every other claimant is left out with a reason that names the winner. |
| K4 | **Missing or invalid key: the file is left out**, with a reason. The key field is always treated as required, whatever its `required` says. |
| K5 | `path` stays a property on every docs node and stays the **owner** of the entry: path-scoped deletes, prunes and edge deletes match `n.path`, not `n.name`. The key is `name`, so prune by key (`Label:name`, extractor-scoped) is unchanged. |
| K6 | **Ownership is re-derived from disk, never from the graph.** A batch that touches a field-keyed type re-reads every file that type selects to find each affected key's claimants. It then writes those claimants, so the result depends only on the files and never on the order in which they were indexed. |
| K7 | A key change (path↔field, or one field to another) is a constraint change. It goes through the applied-schema hash, the rescan and `realign_keys` unchanged. Docs nodes of a type whose recorded key differs are written after `realign_keys`, not before. |
| K8 | Edge lookup and relink are unchanged: a value names `to` nodes by `name`, which for a field-keyed type is the key. A new key is an added node, so relink fires. |

### Why "first by path wins" (K3)

The common way to get a duplicate is copying `adr-012.md` as a template and
forgetting to change `id`. **Refusing both** would delete the existing,
correct ADR-012 and every link into it as soon as the copy is saved. That
breaks the graph for a mistake in a different file. **First by path** leaves
the original in place (`decisions/adr-012.md` sorts before
`decisions/adr-013-draft.md` and before `decisions/copy-of-adr-012.md`).
Doctor names both files, so the mistake is still loud. The choice is
deterministic because K6 makes the winner a function of the files on disk
alone. The cost is that a copy whose path sorts *first* takes over the key.
Doctor reports that the same way, and the fix is the same edit.

## 2. Schema

```yaml
node_types:
  - label: Adr
    key: [adr_id]
    metadata:
      - {name: path}
      - {name: adr_id}
      - {name: title}
      - {name: status}
    source:
      provider: docs
      paths: ["decisions/**/*.md"]
      fields: {adr_id: id}
relationships:
  - type: REPLACES
    provider: docs
    from: Adr
    to: Adr
    field: supersedes
```

`_check_key_and_metadata`, for a docs source:
- **Key.** It is `[path]` or a single declared field other than `path`. Otherwise: "node type 'Adr' is sourced from Markdown front matter, so its key must be [path] or one string field read from front matter (such as [adr_id])".
- **Key type.** The key field is a `string`. Otherwise: "key field 'adr_id' of 'Adr' must be a string: keys are compared as text".
- **`path`.** `path` is still declared as a `string` metadata field. Otherwise: "node type 'Adr' is sourced from Markdown front matter, so it must declare a string 'path' field: every entry records its file there". This was implied by `[path]` before. It is now a separate check.

The filesystem rule and message are unchanged.

## 3. Values and uniqueness (pure provider)

`DocsType` gains `key: DocsField | None` (None means `[path]`). For a
field-keyed type, `_node` reads the key value before the other fields:

- **Text.** It is the K2 text: `str`, or an `int` within int64 written in decimal.
- **Left out.** The value is refused, with the reason in brackets, when it is:
  - absent or null ("missing 'id', which names the entry; the file is left out");
  - not `str`/`int`, or a bool ("'id' is not text or a whole number…");
  - empty;
  - longer than 4 KiB;
  - holding a lone surrogate, a control character (C0, DEL, C1) or a format character (Cf) ("'id' has a control or formatting character…"). Two ids that look the same must be the same;
  - starting with `./`, because edge values lose a leading `./`, so such a key could never be linked to.
- **Written.** The node has `name = <key text>`, `path = <repo-relative path>`, `extractor = "docs"`, and the key field set to the same text.

Uniqueness is resolved in `_evaluate`, which already walks files in sorted
order. Per (label, key), the first file that produces a node keeps it. A
later one yields no node, so it has no outgoing edges. It also yields the
problem "'id' 'ADR-012' is also used by decisions/adr-012.md, which comes
first and keeps it; this file is left out".

`keyed_claims(spec, selected)` is a new pure helper. For each field-keyed
type, it maps each key to its claimants in path order, for the dispatcher
(§4).

## 4. Lifecycle

- **Path-scoped engine calls match `n.path`.** These are `delete_extracted_nodes`, `prune_extracted_at`, `delete_extracted_edges` and the provider branch of `list_file_nodes`. Filesystem nodes and path-keyed docs nodes carry `path = name`, so they behave identically. No migration is needed, because slice 1 already writes `path` on every provider node. `extracted_nodes_at(repo_id, extractor, paths)` is new. It returns `(label, name)` at or below the paths, with the same predicate as the delete.
- **Watcher edits and creates (`index_paths`).** Let T be a field-keyed type. The batch touches T when one of its files is selected by T's globs, or `previous_nodes` holds a T node at one of its paths. For each touched T:
  1. **Affected keys.** K is the set of keys the batch's files now claim for T, plus the keys of T nodes previously at the batch's paths.
  2. **Expansion.** Read every file T selects on disk (`read_selected` on T alone). Add to the batch's `Selected` every file that claims a key in K. The front matter already read is reused, so no file is read twice.
  3. **Snapshot.** Before writing, extend `previous_nodes` with `existing_node_names(repo_id, T, K)`. An id that already existed is then not "added", even when it moves.
  4. **Sync.** `_sync_docs` runs over the expanded set, in this order:
     1. upsert nodes;
     2. delete outgoing docs edges at the expanded paths;
     3. `prune_extracted_at`;
     4. upsert edges.

     The upsert moves first, so an entry whose owner changed is MERGEd onto its new `path` before its old outgoing edges are deleted. For path-keyed types the reordering is a no-op.
- **Deletes (`remove_paths`).** K is the set of field-keyed `(label, key)` pairs from `extracted_nodes_at(gone)`. When K is non-empty:
  1. read T's files on disk;
  2. upsert the winners among K's claimants, and rebuild their outgoing edges as above;
  3. then run `delete_extracted_nodes(gone)` as today.

  An id that survives elsewhere is moved, not deleted. A failure in steps 1–2 logs a warning and falls through to the delete, and the next rescan restores the entry.
- **Outcomes, all with the same rule:**
  - **Rename keeping the id, in either event order.** The node is MERGEd onto the new path, so incoming edges are kept.
  - **Change of id.** The old key is released: a later claimant takes it over, or it is pruned. The new key is an added node, so links waiting for it relink.
  - **Delete.** A successor takes over, or the node is deleted with its edges.
  - **A copy that sorts first.** It takes over the existing node in place.
- **Full scan and apply.** `build_nodes` over the whole repository already sees every claimant. Prune keeps `Label:name`.
- **Key switch (K7).** The new names differ, so `_prune_docs` removes the type's old nodes and their edges. The final `sync_edges` of `full_scan` rebuilds links. When a docs label's recorded key (`previous["keys"]`, parsed as `recorded_declarations` does) differs from the declared one, its nodes are upserted after `record_applied_schema`, `init_schema`, `release_labels` and `realign_keys`, not with the other docs nodes:
  - **Why.** Switching away from a field key could otherwise write duplicate old-key values under the old constraint.
  - **Safe to drop.** Prune has already removed the label's nodes in this repository, so `realign_keys`' duplicate check passes and it can replace the constraint.
  - **Failure.** The deferred upsert is wrapped in `try/except`. A failure (another repository still records the old key) logs a warning that names doctor's Schema constraints section, and apply still returns True. This is the existing cross-repository-disagreement behaviour.
- **Cost.** A batch that touches a field-keyed type re-reads every file that type selects, which is O(type's files). It writes only K's claimants. This sits next to slice 1's relink cost in the README's known limits.

## 5. Safety

Nothing new runs or is interpolated.
- **Values.** Key values travel only as parameters, and labels and property names still come from the validated schema.
- **Existing bounds.** These all apply unchanged: `read_bounded`, `bounded_safe_load`, the 4 KiB and int64 caps, the condition caps, the glob matcher, and fail-closed on an invalid or pending schema.
- **New bounds.** The new paths read only `walk.indexable_paths`, and only for touched field-keyed types. The Cf and control-character refusal for key values (§3) matches the one front-matter key names already have.

## 6. How it shows up

- **Doctor.** No new section. The duplicate and missing-key reasons arrive as per-file problem lines through `source_report`, under its five-file cap. For example:
  - "Adr: decisions/copy-of-adr-012.md: 'id' 'ADR-012' is also used by decisions/adr-012.md, which comes first and keeps it; this file is left out"
  - "Adr: decisions/draft.md: missing 'id', which names the entry; the file is left out"

  When the target type is field-keyed, `unmatched_report` adds a hint: "Adr: supersedes 'decisions/adr-012.md' in decisions/adr-013.md matches no Adr (Adr entries are named by 'id', not by file path)".
- **Warnings.** For a docs type whose key changed, `edits.schema_change_warnings` adds: "the next rescan renames every Adr entry by its new key; links that name an Adr the old way stop matching (devgraph doctor lists them)".
- **Config form.** The existing Key checkbox on each metadata row is the control. In docs mode:
  - **Source hint.** "A node type sourced from Markdown front matter is keyed on exactly one string field: path (the file's location) or a front-matter field such as id."
  - **Key help.** "Tick path, or one front-matter field whose value names the entry (like ADR-012), so links can use it."
  - **Path hint.** A missing `path` row shows "Add a field named path (type string): each entry records its file there."

  Round-trip and refusal rules are unchanged. A key order that differs from the metadata order already opens in YAML.
- **README.** The docs-source section adds the Adr example with `supersedes: ADR-012`. It also covers duplicates (first path wins, doctor names both), missing ids, what renaming, re-iding and deleting do, and the key-switch warning. The "What it doesn't do yet" line about path-only links is removed, and the new cost is added.

## 7. Out of scope

- Composite keys, and keys from nested front matter (`a.b`) or from the body.
- Case-insensitive or trimmed key matching.
- Keys for filesystem types.
