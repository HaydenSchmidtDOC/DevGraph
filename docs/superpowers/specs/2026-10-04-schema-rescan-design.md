# Schema rescan semantics — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §5 "Reindex and migration
semantics". Stacked on #27 (filesystem provider) and #28 (`devgraph config`).

## Problem

Changing `devgraph.schema.yaml` changes node identity and metadata shape, but
DevGraph keeps no record of which schema the graph was built with. #27 answers
a save by re-syncing filesystem nodes immediately, on every save. The epic asks
instead for a recorded schema hash, a full reindex after a quiet period, a way
to bypass the wait, and cleanup of node and relationship types removed from
the schema.

## Design

**Applied-schema state.** The Repository node records what the graph was last
built with: `schema_hash` (`sha256:<hex>` of the file's bytes, or `absent`),
`schema_labels` (declared node labels) and `schema_relationship_types`
(declared, non-built-in relationship types). This is DevGraph state outside the
repository, readable by every process (CLI, agent, dashboard) without a
registry migration.

**Pending.** A repository's schema is *pending* when the file's current hash
differs from the applied hash. A repository with no applied state and no file
is not pending (nothing to apply), so existing repositories are unaffected.

**Applying** (`apply_project_schema`): resolve the file (invalid → log, return
False, graph untouched); provision constraints/indexes; delete nodes of user
labels and relationships of user relationship types that were applied before
but are no longer declared (built-ins never); reconcile and re-sync the
filesystem provider; record the applied state. `full_scan` applies the schema
before indexing, so registration, `devgraph rescan` and the dashboard's
registration all apply immediately.

**While pending,** incremental indexing skips the filesystem provider (built-in
extraction is unaffected): writing filesystem nodes under a schema that has not
been applied is what produced partial graphs. Saving the schema itself no
longer triggers anything immediately.

**Debounce.** Each agent (tray, headless) runs a `SchemaRescanScheduler` that
checks active repositories every 30 s. When a repository is pending, the first
sighting of a hash starts a quiet period; any further edit (a new hash)
restarts it. After 5 minutes without a change, the scheduler runs a full rescan
(`full_scan`, which applies the schema) and marks the repository indexed. An
invalid schema is not retried until the file changes again. A failing
repository never stops the others or the thread.

**`devgraph rescan --now`.** `devgraph rescan` always applies a pending schema
immediately; `--now` is accepted to say so explicitly (the epic's spelling).

## Out of scope

Dropping Neo4j constraints/indexes for removed labels (the database is shared
across repositories, so another repository may still use the label); doctor
reporting of hash drift (left for the CLI slice); schema-driven built-in
extraction (later slices).

## Testing

Unit: hash function; scheduler debounce (restart on new hash, run after quiet
period, invalid not retried, failure isolation) with a fake clock. Live Neo4j:
`full_scan` records state; pending pauses provider writes; applying adds them;
removed labels/relationship types are deleted while built-ins and still-declared
types stay; invalid schema leaves everything; #27's schema-save tests rewritten
for the new semantics. CLI: `rescan --now` accepted.
