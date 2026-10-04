# Schema constraint cleanup — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §5 (schema rescan semantics). The
rescan slice applies schema changes to nodes and relationships but leaves the
constraints and indexes a user node type generated: a removed type keeps its
`<label>_repo_key` constraint (and `<label>_repo_name` index), and a changed
`key` keeps the old constraint because `CREATE CONSTRAINT <name> IF NOT EXISTS`
no-ops on an existing name.

## The crux: constraints are database-wide

Every registered repository shares one Neo4j database, and so may other
DevGraph installations and test runs pointed at the same server. Two
repositories declaring the same label share one constraint. The registry is
per installation, so it cannot say who else uses a constraint; the graph can.
Each `Repository` node already records the user labels its graph was last built
with (`schema_labels`). This slice also records each label's key
(`schema_keys`, a list of `"Label:k1,k2"` strings), and the graph's recorded
state is the authority for automatic cleanup.

## What DevGraph will touch

Only objects matching its generated naming, never a built-in:

- a uniqueness constraint named `<label.lower()>_repo_key` on one node label
  whose first property is `repo_id`;
- a range index (owning no constraint) named `<label.lower()>_repo_name` on
  one node label over `(repo_id, name)`;
- in both cases the label is not a built-in label (case-insensitively) and the
  name is not one of the built-in constraint names.

## Automatic cleanup on apply

`apply_project_schema` (the one seam every full scan goes through: CLI
`add`/`rescan`, the agent's `SchemaRescanScheduler`, the dashboard) runs
`reconcile_generated_constraints` after it records the applied state. It never
fails the apply: a Neo4j error is logged as a warning.

1. **Removed labels.** Candidates are only the labels this repository's
   previous applied state declared and its new one doesn't. A candidate's
   constraint and index are dropped when no `Repository` node in the graph
   records a label with the same lower-cased name (the generated name is
   lower-cased) and no node of that label remains. Restricting candidates to
   labels this apply removed (rather than every unused generated constraint in
   the database) avoids a race with another process that has provisioned a
   new label but not yet recorded it.
2. **Changed keys.** For each label this repository now declares, the
   existing same-named constraint is compared with the declaration: label and
   properties `(repo_id, *key)`. If they differ, it is replaced (drop, create)
   only when every `Repository` node that records a label with that lower-cased
   name records exactly this label and key. A repository recorded without keys
   (state written before this slice) counts as disagreeing until it is
   rescanned. A disagreement leaves the constraint untouched and logs a warning
   naming the repositories; `config validate`/`doctor` already report the
   conflict. If the new constraint cannot be created (existing nodes violate
   it), the old definition is recreated and a warning is logged. The filesystem
   lookup index is compared the same way (its label only; its properties are
   fixed).

## Removing a repository

`devgraph remove` and `devgraph prune` delete a repository's graph data,
including its `Repository` node. Both read its recorded labels first and run
step 1 for them afterwards, so the last repository declaring a label releases
its constraint.

## Stale objects: doctor and `prune-constraints`

A generated object is **stale** when no `Repository` node records its label,
no registered repository's effective schema (project config switch respected)
declares it, and no node of that label exists. Stale objects come from labels
removed before this slice shipped or from repositories deleted outside the CLI.

- `devgraph doctor` gains a "Schema constraints" section (skipped when Neo4j is
  down) listing stale objects as warnings, with the command that removes them.
- `devgraph config schema prune-constraints [--label L ...] [--dry-run]` drops
  stale objects, optionally only those for the given labels, and prints each.
  It is never run automatically: a full sweep can race another process that has
  provisioned but not yet recorded a label, so it is a deliberate user action.

## Not covered

- A label that stays declared but loses its filesystem `source` keeps its
  `<label>_repo_name` lookup index until the label is removed everywhere.
- A disabled repository keeps its recorded labels, and so its constraints,
  until its next rescan applies the built-in schema.
