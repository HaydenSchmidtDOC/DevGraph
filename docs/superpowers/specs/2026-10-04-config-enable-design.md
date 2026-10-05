# Per-repository project config switch and drift reporting — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §6 (`config enable / disable
<repo>`) and §8 (`devgraph doctor` schema-hash drift). Stacked on the
integration branch `epic1/f-base` (#32 + #29).

## `devgraph config enable|disable <repo>`

A per-repository switch for the project config files (`devgraph.schema.yaml`
and `devgraph.tools.yaml`), for comparing a repository's behaviour with and
without them. `<repo>` is a registered repo id. The switch is stored in the
registry (`project_config_enabled`, default on), never in the repository.

While a repository's project config is **disabled**, DevGraph behaves exactly
as if neither file existed:

- The schema resolves to the built-in schema, and the schema fingerprint is
  `absent`. The existing rescan machinery handles the transition: disabling a
  repository whose graph was built with a schema makes it pending, and the
  next rescan (debounced, or `devgraph rescan <repo_id> --now`) applies the
  built-in schema, removing the user types as any schema change does.
  Enabling it again makes the file's schema pending in the same way.
- MCP sessions scoped to the repository serve no project tools, with a notice
  in `devgraph://project-tools` saying the project config is disabled and how
  to enable it. A running session picks the switch up on its next poll
  (≤ 2 s), like a file change.

The files stay on disk and are still checked by `devgraph config validate`,
which reports the repository as disabled alongside the result. `config show
--repo` says the project config is disabled and shows the built-in schema.
`devgraph list` and `doctor` show the switch.

The commands print what changes and when: "schema: applied at the next rescan
(`devgraph rescan <id> --now` to apply now)", "project tools: picked up by
running MCP sessions within 2 s". Enabling an enabled repository (or
disabling a disabled one) is a no-op that says so; an unknown repo id exits 1.

### Where the switch is read

The schema and tools loaders work from a repository path, with no registry in
reach, and are called from the indexer, the rescan scheduler, the CLI and the
MCP server. The switch is therefore looked up by path:
`project_config_enabled(repo_root)` reads the registry database read-only
(SQLite `mode=ro`), matching the resolved path against registered repository
paths. A repository that is not registered, a missing database, or a registry
without the column is enabled. It is consulted in exactly three places:

- `load_project_schema` returns `None` when disabled (a `respect_switch=False`
  argument lets `config validate`/`show` still read the file);
- `schema_file_hash` returns `absent` when disabled;
- the MCP tool plane's `tools_fingerprint` returns `disabled`, which serves no
  project tools with the notice above.

## `devgraph doctor`: schema drift

For each active registered repository, doctor compares the schema file's
fingerprint with the schema the graph was last built with
(`read_applied_schema`) and reports one of: `applied` (in sync), `pending`
("the schema changed since the graph was built; applied at the next rescan,
or `devgraph rescan <id> --now`"), or `never applied` (a schema exists but no
rescan has recorded one). Pending is a warning, not a failure. Doctor
already connects to Neo4j; when it can't, the drift check is skipped with a
note.

## Out of scope

Tool and schema CRUD commands and the global tools store (next slices); any
per-file switch (the epic's switch is per repository).

## Testing

Registry migration and setter; the path lookup (registered/unregistered,
missing DB, read-only); loaders and fingerprints honour the switch and
`respect_switch=False`; a disabled repository's full scan and rescan apply the
built-in schema and removing the switch re-applies the file (live Neo4j); the
tool plane serves nothing with the notice and reloads on enable; CLI
enable/disable output, idempotence, unknown id; validate/show/list/doctor
report the switch; doctor drift states (live Neo4j).
