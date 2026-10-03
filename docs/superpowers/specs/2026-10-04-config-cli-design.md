# `devgraph config` CLI — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1 (per-project graph schema epic),
§6 "CLI". This slice turns `devgraph config` into a command group with the
read-only schema commands and `eject`. It is cut from upstream `master` and is
independent of the filesystem-provider PR (#27).

## Commands

| Command | Behaviour |
| :--- | :--- |
| `devgraph config [--show-defaults] [--json]` | Unchanged: prints DevGraph's settings. |
| `devgraph config settings [KEY] [--show-defaults] [--json]` | The same settings viewer, with an optional single key. |
| `devgraph config <setting-name>` | Exit 2 with: use `devgraph config settings <name>` (the old positional form). |
| `devgraph config show [--repo PATH] [--global] [--json]` | Effective schema for the repository at PATH (default: current directory): schema file status and `extends`, every node type (label, origin, key) and relationship (type, from, to, provider, origin). Origin is `built-in` or `devgraph.schema.yaml`. `--global` shows the built-in schema only (there is no global config file yet). Invalid schema → loader message, exit 1. |
| `devgraph config validate [--repo PATH \| --all]` | Fail-closed check of one repository (default: current directory) or every registered repository: each reports `absent` (built-in schema), `valid`, or `invalid` with the loader's message; `--all` also reports cross-repository label conflicts (the same findings `devgraph doctor` prints). Exit 1 if anything is invalid or conflicting — usable in CI. |
| `devgraph config eject [--repo PATH]` | Writes a commented, valid starter `devgraph.schema.yaml` (`version: 1`, `extends: default`): the built-in labels and relationship types listed as comments (generated from `devgraph/graph/schema.py`), and a commented example node type and relationship that validate when uncommented. Refuses — exit 1, naming the existing path — if the file already exists; never overwrites (exclusive create). |

`--repo` takes a directory path; it is not resolved to a git root or registry
entry.

## Why eject writes a starter, not "the defaults"

The epic describes `eject` as materialising the full default set. Today the
loader rejects a project file that redeclares a built-in label, and
extraction is not schema-driven, so a file containing the defaults could
neither validate nor change anything. The starter is the honest version for
now: valid, self-documenting, and the place to start editing. Revisit when
extraction becomes schema-driven. The filesystem-provider example (#27) is
added to the starter once that PR merges; until then only examples valid on
`master` are included.

## Settings masking

While moving the settings viewer, its masking check is fixed: it tested the
requested key rather than each field's name, so `devgraph config` printed
`neo4j_password` in clear. Fields whose name contains `password`, `secret`
or `token` are masked in the table and in `--json` output.

## Out of scope (epic items for later slices)

`config enable/disable` (needs a registry flag that indexing honours),
`config tools …` (no tool plane yet), `config schema add/edit/delete/reset`
(needs safe YAML round-tripping), a global config file.

## Testing

Typer `CliRunner` tests over `tmp_path` repositories: bare `config` and
`config settings` output and masking; the old positional form's message;
`show` for absent, valid (`extends: default` and `none`) and invalid schemas,
`--global`, `--json`; `validate` single and `--all` (temporary registry) with
exit codes and conflicts; `eject` round trip (written file loads; the
uncommented example loads; a second eject refuses and leaves the file
untouched).
