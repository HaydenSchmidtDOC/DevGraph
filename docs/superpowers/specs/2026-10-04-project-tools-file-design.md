# Project tools file — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §7 (MCP tool plane) and §9
(validation). First slice of the tool plane: the `devgraph.tools.yaml` format,
its fail-closed loader, and CLI/doctor reporting. Nothing is served to MCP
clients yet — like #21 for the schema file, this slice is inert. Stacked on
#28 (`devgraph config`).

## Format

```yaml
version: 1
tools:
  - name: list_folder
    description: List the files directly inside a folder.
    cypher: |
      MATCH (f:File {repo_id: $repo_id})-[:IS_CHILD_OF]->(:Folder {repo_id: $repo_id, path: $folder})
      RETURN f.path AS path ORDER BY path
    parameters:
      - name: folder
        type: string
        required: true
        description: Repo-relative folder path ("." for the root).
    max_rows: 100
    timeout_s: 10
```

- `version: 1`; unknown keys rejected everywhere.
- `name`: `[a-z][a-z0-9_]{0,63}`, unique in the file.
- `description`: required, non-blank, at most 1024 characters (it is what an
  agent reads to decide whether to call the tool).
- `cypher`: required; must reference `$repo_id`, which the server will inject
  and the author cannot override; must pass a static read-only check.
- `parameters`: `name` (Unicode identifier, not `repo_id`, unique),
  `type` (`string | integer | float | boolean`, default `string`),
  `required` (default `true`), optional `default` (must match the type; only
  allowed when `required: false`), optional `description`.
- Every `$name` or `$`name`` the query uses (other than `$repo_id`) is a declared 
  parameter, and every declared parameter is used.
- `max_rows`: 1–1000 (default 100). `timeout_s`: 1–60 (default 10).
- Only Cypher tools. The epic's script and composition tools need the sandbox
  and the tool plane respectively and arrive with them.

## Read-only check

String literals, backtick-quoted identifiers and comments are blanked out, then
the query is scanned (case-insensitively, with lookaround boundaries that treat
digits as separators) for `CREATE`, `INSERT`, `MERGE`, `SET`, `DELETE`, 
`DETACH`, `REMOVE`, `DROP`, `FOREACH`, `LOAD CSV`, `CALL` and `USE`. Any hit 
rejects the file. APOC references (case-insensitive `apoc.` outside strings) are 
also rejected. This is defence in depth: when the tool plane serves these tools 
it will also run them in a read transaction. `CALL` is rejected outright 
(procedures and subqueries alike) to keep the rule simple and auditable.

The `$repo_id` requirement proves the query *references* the injected parameter, 
not that it *scopes* every match — the tool plane's runtime (read transaction, 
injected repo_id) remains the real gate.

## Built-in names

A project tool named like a built-in MCP tool is valid, but `config validate`
and `doctor` warn that the built-in will be used — the epic's rule that a
locked name can't be shadowed and the fixed implementation wins, reported
rather than silently ignored.

## CLI and doctor

- `devgraph config validate` checks both files; a tools file reports `absent`,
  `valid` (with tool names) or `invalid`, plus non-failing `warning` findings
  for built-in names. Exit 1 if either file is invalid.
- `devgraph config show` adds a `tools` section (JSON `tools` key: status,
  file, and each tool's name, description, parameters, limits). An invalid
  tools file is reported and exits 1, like an invalid schema.
- `devgraph doctor` gains a "Project tools" section mirroring "Project
  schemas".

## Testing

Loader unit tests for every rule (including keywords inside strings/comments
not tripping the check, and each write keyword tripping it); CLI tests for
validate/show/doctor reporting and exit codes.
