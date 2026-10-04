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

A constraint a user created by hand in exactly this shape is
indistinguishable from DevGraph's and is treated as DevGraph's.

## Automatic cleanup on apply

The logic lives in `devgraph/indexer/schema_constraints.py`.
`apply_project_schema` (the one seam every full scan goes through: CLI
`add`/`rescan`, the agent's `SchemaRescanScheduler`, the dashboard) records the
applied state (labels plus `schema_keys`), then runs, in order:

1. **Re-provisioning.** `engine.init_schema(effective)` again (every statement
   is `IF NOT EXISTS`). Another repository's apply can release a label between
   this repository's provisioning and its record. Nothing else would re-create
   that constraint, because the schema is no longer pending, so the
   repository's own apply closes the race.
2. **`release_labels(engine, removed_labels)`.** The only candidates are the
   labels this repository's previous applied state declared and its new one
   doesn't. A candidate's constraint and index are dropped when no
   `Repository` node in the graph records a label with the same case-folded
   name (the generated name is lower-cased) and no node of that label remains.
   Restricting candidates to labels this apply removed, rather than every
   unused generated constraint in the database, keeps the sweep away from
   labels another process has provisioned but not yet recorded.
3. **`realign_keys(engine, node_types)`.** For each label this repository now
   declares, the existing same-named constraint is compared with the
   declaration: label and properties `(repo_id, *key)`. When they differ, the
   constraint is replaced only if every `Repository` node recording a label
   with that case-folded name records exactly this label and key. A repository
   recorded without keys (state written before this slice) counts as
   disagreeing until it is rescanned. A disagreement leaves the constraint
   untouched and logs a warning naming the repositories; `config
   validate`/`doctor` already report the conflict. Before anything is dropped,
   a grouped count looks for two nodes sharing a `(repo_id, *key)` value. If
   any exist, the replacement is skipped with a warning, so bad data never
   opens an unconstrained window and is not retried destructively on every
   apply. On clean data the old constraint is dropped and the new one created.
   If the create still fails (a node written since the check), the old
   definition is recreated. After creating, the definition is read back before
   the replacement is logged as done. The filesystem lookup index is compared
   the same way, on its label only; its properties are fixed. Labels that
   differ only by case across repositories (`Widget` in one, `widget` in the
   other) share one generated name. Once every repository records the same
   spelling, the next apply converges the constraint on it.

Steps 1–3 never fail the apply: a Neo4j error is logged as a warning.

## Removing a repository

`devgraph remove` and `devgraph prune` delete a repository's graph data,
including its `Repository` node. Both read its recorded labels first and run
`release_labels` for them afterwards, so the last repository declaring a label
releases its constraint.

## Doctor and `prune-constraints`

A generated object is **stale** when no `Repository` node records its label,
no registered repository's effective schema (project config switch respected)
declares it, and no node of that label exists (`stale_generated_objects`).
Stale objects come from labels removed before this slice shipped or from
repositories deleted outside the CLI.

`devgraph doctor` gains a "Schema constraints" section, skipped when Neo4j is
down, with these warnings:

- each stale object, with the command that removes it;
- from `constraint_drift`, each recorded label of a repository whose generated
  constraint is **missing** (fix: `devgraph rescan <repo_id> --now`);
- from `constraint_drift`, each recorded label whose **key change is blocked by
  duplicate nodes** (fix: remove the duplicates, then rescan).

`devgraph config schema prune-constraints [--label L ...] [--dry-run]` drops
stale objects, optionally only those for the given labels, and prints each.
It is never run automatically: a full sweep can race another process that has
provisioned a label but not yet recorded it, so it is a deliberate user
action.

## Not covered

- A label that stays declared but loses its filesystem `source` keeps its
  `<label>_repo_name` lookup index until the label is removed everywhere.
- A disabled repository keeps its recorded labels, and so its constraints,
  until its next rescan applies the built-in schema.
