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
| K1 | A docs type's key is `[path]` (unchanged) or **one** declared `string` metadata field other than `path`. The field is read from front matter like any other field, and `fields` may rename it (`{adr_id: id}`). Composite keys stay out of scope. Filesystem types stay `[path]` only. |
| K2 | **Key values are text**, compared exactly and case-sensitively. A `str` counts as itself, and an `int` (not a bool, within int64) counts in decimal, so `id: 12` and `supersedes: 12` meet. Anything else (floats, dates, lists, maps) is not a key, and neither is text with leading or trailing whitespace (refused, not trimmed). |
| K3 | **Duplicates: the original wins, by a stem-aware path order.** When several files of one type claim the same key, the file that comes first in **claimant order** becomes the entry. That order compares the path's parts with the last part's suffix removed, then the full path as a tie-break. Every other claimant is left out, and doctor names the owner. |
| K4 | **Missing or invalid key: the file is left out**, with a reason in doctor. The key field is always treated as required, whatever its `required` says. |
| K5 | `path` stays a property on every docs node and **owns** the entry. Path-scoped deletes, prunes and edge deletes match `n.path`, not `n.name`. The key is `name`, so prune by key (`Label:name`, extractor-scoped) is unchanged. |
| K6 | **An owner map, computed from disk, gates every write.** `keyed_owners` maps `(label, key)` to the owning path. It is computed over **every** file of **every** field-keyed type, never from a partial file set and never from the graph. A (type, file) pair whose key it does not own yields no node and no edges, in every pass: batch, relink, delete takeover, apply and doctor. |
| K7 | **A key change is a constraint change.** Changing path↔field, or one field to another, goes through the applied-schema hash, the rescan and `realign_keys`. A docs label whose existing generated constraint differs from its declared key is written after `realign_keys`, in its own transaction. |
| K8 | Edge lookup and relink are unchanged. A value names `to` nodes by `name`, which for a field-keyed type is the key. A new key is an added node, so relink fires. |

### Why the original wins (K3)

The common way to get a duplicate is copying `adr-012.md` and forgetting to
change `id`. **Refusing both** would delete the correct ADR-012 and every
link into it as soon as the copy is saved, which breaks the graph for a
mistake in another file.

Plain code-point order on the full path does not pick the original either:
`adr-012 copy.md`, `adr-012 - Copy.md`, `adr-012 (1).md` and `adr-012-v2.md`
all sort *before* `adr-012.md`, because space and `-` come before `.`.
Comparing the stems makes `adr-012` a prefix of each copy's stem, so the
original comes first. That covers the copy names that Finder, Explorer, and
people with `-v2` habits produce. A copy named so that it sorts first
anyway, such as `Copy of adr-012.md`, takes over the id. Doctor says so in
the same way, and the fix is the same edit.

The order is a pure function of the paths, and K6 computes the owner from
every claimant on disk. So the same files always give the same graph,
whatever order the events arrived in.

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
- **Key shape.** The key is `[path]` or a single declared field other than `path`. Otherwise: "node type 'Adr' is sourced from Markdown front matter, so its key must be [path] or one string field read from front matter (such as [adr_id])".
- **Key type.** The key field is a `string`. Otherwise: "key field 'adr_id' of 'Adr' must be a string: keys are compared as text (use string; numbers like 12 still work)".
- **Path field.** `path` is still declared as a `string` metadata field. Otherwise: "node type 'Adr' is sourced from Markdown front matter, so it must declare a string 'path' field: every entry records its file there". `[path]` used to imply this; it is now checked separately.

The filesystem rule and message are unchanged.

## 3. Values and ownership (pure provider)

`DocsType` gains `key: DocsField | None`, where None means `[path]`.

**Key text.** `_key_text(raw)` returns the K2 text. A claim is refused, with a
reason, when the value:
- is absent or null ("missing 'id', which names the entry; add an `id:` line");
- is not `str`/`int`, or is a bool ("'id' is not text or a whole number");
- is empty, or has leading or trailing whitespace;
- is longer than 4 KiB;
- holds a lone surrogate, a control character (C0, DEL, C1) or a format character (Cf). Invisible characters must not make two ids differ. Unicode normalisation (NFC/NFD) is not applied, so composed and decomposed spellings remain distinct keys;
- starts with `./`. Edge values lose a leading `./`, so such a key could never be linked to.

**Claims and owners.** A *claim* is a (type, file) pair that the type selects
and whose `where` conditions hold, with no required-field skip and a valid
key. Two pure functions sit on top of claims:
- `keyed_claims(spec, selected)` returns `{(label, key): [paths in claimant order]}` for the field-keyed types.
- `keyed_owners(claims)` returns `{(label, key): first path}`.

Claimant order is `(PurePosixPath(rel).parent.parts + (stem,), rel)`, which is the K3 rule. Each label has its own map, so `Adr` and `Rfc` may share a key value. A file selected by both types is judged separately for each.

**Gating.** `_evaluate`, `build_nodes` and `build_edges` take `owners`. A field-keyed (type, file) pair yields its node and edges only when `owners[(label, key)] == rel`. Path-keyed types ignore `owners`.

The watcher path records no problem for a pair that loses ownership. Losers are a doctor concern: `source_report` computes claims itself and names them.

**Node.** An owner's node has these properties:
- `name = <key text>`;
- `path = <repo-relative path>`;
- `extractor = "docs"`;
- the key field, set to the same text.

**Edges.** Every edge dict gains `from_path`, which is not written, alongside `field`. `unmatched_report` names the file through it, because `from_name` is now the id.

## 4. Lifecycle

### Engine

The path-scoped engine calls now match `n.path`: `delete_extracted_nodes`, `prune_extracted_at`, `delete_extracted_edges` and the provider branch of `list_file_nodes`. Filesystem nodes and path-keyed docs nodes carry `path = name`, so they behave identically. No migration is needed, because slice 1 already writes `path` on every provider node.

`extracted_nodes_at(repo_id, extractor, paths)` is new. It returns `(label, name)` at or below the given paths, with the delete's predicate.

### One walk and one read per batch (`KeyedView`)

When a batch touches any field-keyed type, the dispatcher builds one `KeyedView`:
1. One `walk.indexable_paths`.
2. One `read_selected` over every file any field-keyed type selects. The batch's own files are reused from the batch read, never read twice.
3. `claims` and `owners`.

The relink and the `remove_paths` takeover use the same view. A relink that also needs path-keyed files reuses this walk, and reads only the files the view has not already read.

A batch *touches* type T when:
- one of its paths is selected by T's globs; or
- `previous_nodes` holds a T node at one of its paths; or
- for `remove_paths`, `extracted_nodes_at(gone)` returns a T node.

### Watcher edits and creates (`index_paths`)

For each touched field-keyed type T:
1. **Affected keys.** K is the set of keys that the batch's files claim for T, plus the keys of T nodes previously at the batch's paths.
2. **Expansion.** Add the on-disk **owner** of each key in K (`owners[(T, k)]`) to the batch's `Selected`, using front matter the view already read. Losers are never added. Every engine `$paths` list therefore holds at most |batch| + |K| paths.
3. **Snapshot.** Extend `previous_nodes` with `existing_node_names(repo_id, T, K)`. An id that already existed is then not "added" when it moves.
4. **Sync.** `_sync_docs(…, owners)` runs over the expanded set, in this order:
   1. upsert nodes;
   2. delete outgoing docs edges at the expanded paths;
   3. `prune_extracted_at`;
   4. upsert edges.

   Upserting first means an entry whose owner changed is MERGEd onto its new `path` before its previous owner's outgoing edges are deleted. For path-keyed types the reorder is a no-op.

**Relink.** `_relink_docs` passes the view's `owners` to `build_edges`. A loser outside the batch can never act as the first claimant of a key whose owner is inside the batch.

### Deletes (`remove_paths`)

Before `delete_extracted_nodes(gone)` runs as today:
1. For each field-keyed `(label, key)` returned by `extracted_nodes_at(gone)`, upsert its current owner from the view, if one exists.
2. Rebuild that owner's outgoing edges.

An id that survives in another file therefore moves instead of being deleted. A failure in these steps logs a warning and falls through to the delete, and the next rescan restores the entry.

### Outcomes

All of these follow from the rule above:
- **Rename keeping the id, in either event order.** The node is MERGEd onto the new path, so its incoming edges are kept.
- **Re-id.** The old key goes to its next claimant, or is pruned. The new key is an added node, so links that were waiting for it relink.
- **Delete.** The next claimant takes over, or the node is deleted with its edges.
- **A copy that sorts first.** It takes over the existing node in place.
- **An owner that is briefly unreadable** (mid-write, or permissions). It makes no claim during that batch, so the runner-up takes the id until the owner's next save or the next rescan.

### Full scan and apply

`build_nodes` over the whole repository uses owners computed from the same `Selected`. Prune keeps `Label:name`.

### Key switch (K7)

**Which labels are deferred.** A docs label is deferred when `generated_objects(engine)` holds its `<label>_repo_key` constraint with properties other than `(repo_id, *key)`. This compares against the database rather than this repository's `previous["keys"]`. It therefore covers a first apply under a constraint that another repository created.

**Order in `_apply_project_schema`.**
1. Docs nodes of non-deferred labels are upserted as today.
2. After `record_applied_schema`, `init_schema`, `release_labels` and `realign_keys`, each deferred label is upserted **in its own transaction**, inside `try/except`.

`_prune_docs` has already removed the label's old nodes in this repository (their names differ), so `realign_keys`' duplicate check is not blocked by them.

**When a deferred upsert fails.** This happens when another repository still records the old key and this repository's entries violate it. Apply still returns True, which is the existing cross-repository disagreement behaviour. The warning:
- names the other repositories from `recorded_declarations`;
- says "Adr entries were not written".

**Doctor.** `constraint_drift` gains a `conflict` status. It applies when the generated constraint differs from a repository's recorded key, there are no duplicates (otherwise the status is `blocked`), and another repository records the constraint's key, so the next apply will not replace it. Doctor's Schema constraints section prints:

> "other-repo: Adr is keyed on (adr_id) but its constraint uses (path), which this-repo declares; entries that break it are not written. Align the key or rename one label"

Links into the new names are rebuilt by full_scan's final edge pass.

### Cost

A batch that touches a field-keyed type walks the repository once (`indexable_paths`) and reads every file any field-keyed type selects. That is O(repository files) for the walk and O(keyed files) for the reads. It writes only the batch's paths plus K's owners.

An in-process cache keyed by `(path, mtime_ns, size)` would make repeated reads cheap. It is optional and left out of this slice. The README names the cost next to slice 1's relink cost. The follow-up `2026-10-07-docs-read-cache-design.md` adds that cache, keyed on the file's full stat identity: a save re-parses only the files changed since they were last read, and the walk remains.

## 5. Safety

Nothing new runs or is interpolated:
- Key values travel only as parameters.
- Labels and property names still come from the validated schema.

All the slice 1 safeguards apply unchanged:
- `read_bounded` and `bounded_safe_load`;
- the 4 KiB and int64 caps;
- the condition caps;
- the glob matcher;
- fail-closed on an invalid or pending schema.

The new code paths:
- read only `walk.indexable_paths`, and only when a batch touches a field-keyed type;
- refuse control, Cf and whitespace-edged key values (§3), matching the refusals front-matter key names already have.

## 6. How it shows up

### Doctor

These lines come from `source_report`, under its five-file cap, as per-file problem lines:
- "Adr: decisions/adr-012 copy.md: 'id' 'ADR-012' is also used by decisions/adr-012.md, whose path sorts first and keeps it; change the id in one of them"
- "Adr: decisions/draft.md: missing 'id', which names the entry; add an `id:` line"

The summary line counts duplicates: "Adr: 14 files match, 12 Adr entries, 1 duplicate id".

When the target type is field-keyed, the unmatched-value line names the file through `from_path` and adds a hint: "Adr: supersedes 'decisions/adr-012.md' in decisions/adr-013.md matches no Adr (Adr entries are named by 'id', not by file path)".

The `conflict` line from §4 appears under Schema constraints.

### Warnings

`edits.schema_change_warnings`, for a docs type whose key changed, adds: "the next rescan renames every Adr entry by its new key; links that name an Adr the old way stop matching (devgraph doctor lists them)".

### Config form

The existing Key checkbox on each metadata row is the control. In docs mode:
- **Required.** A ticked key row shows Required as ticked and disabled ("key fields are always required"). The YAML is unchanged.
- **Source hint.** "A node type sourced from Markdown front matter is keyed on exactly one string field: path (the file's location) or a front-matter field such as id."
- **Key help.** "Tick path, or one front-matter field whose value names the entry (like ADR-012), so links can use it."
- **Integer key.** Ticking an `integer` field hints "Use string: numbers like 12 still work."
- **No `path` row.** Hints "Add a field named path (type string): each entry records its file there."

The round-trip and refusal rules are unchanged.

### README

The docs-source section adds:
- the Adr example and its front matter, with `supersedes: ADR-012`;
- quoting ids with leading zeros (`id: "012"`), because YAML 1.1 reads a bare `012` as the number 10;
- duplicates: the original wins over copies, and doctor names both files;
- missing ids, and ids with leading or trailing spaces;
- rename, re-id and delete;
- the key-switch warning;
- that ids become `MENTIONS` targets when mentions are on;
- the new per-save walk and read cost.

Point 4 changes from "a docs node's path" to "a docs node's name: its key (an id, or its path for `[path]` types)". The "What it doesn't do yet" line about path-only links is removed.

## 7. Out of scope

- Composite keys, and keys from nested front matter (`a.b`) or the body.
- Case-insensitive, trimmed or Unicode-normalised key matching.
- Keys for filesystem types.
- The `(path, mtime_ns, size)` read cache. Since added: see `2026-10-07-docs-read-cache-design.md`.
