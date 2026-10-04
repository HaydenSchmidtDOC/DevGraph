# Global tools, `config tools` commands and tool resolution — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §6 (`config tools list / add /
edit / delete / reset`), §7 (global tools) and §8 (resolution, collisions and
notices). Stacked on #33 (`epic1/f1-config-enable`).

## Global tools

Global tools use the same definition format as project tools and are
available in every scoped MCP session. Per the epic they are defined through
the CLI (and later the dashboard), and stored in the **user's DevGraph
directory**, never the install location: `global-tools.json` next to the
registry database (`registry_db_path.parent`). The file holds
`{"version": 1, "tools": [...]}`, is validated by the same loader as
`devgraph.tools.yaml` (JSON is YAML), and is written atomically (temporary
file plus rename). Hand edits are possible but not the intended path.

A Cypher tool needs a repository to inject as `$repo_id`, so **global tools
are served only in sessions scoped to a repository**. An unscoped session
serves none, and `devgraph://project-tools` says why. Global tools are not
project config, so `config disable` does not hide them.

## Resolution

The MCP session builds one tool set from three layers:

- **Built-in tools** are locked. A global or project tool with a built-in name
  is never served; the built-in is used and a notice records it. `config tools
  add --global` refuses built-in names outright.
- **Global tools** are served unless a project tool of the same name replaces
  them.
- **Project tools** win over a global tool of the same name ("local overrides
  global", epic §8). The project tool's responses carry the notice `resolved:
  project override of global tool '<name>'`.
- **A rejected project tool falls back.** When a project tool named like a
  global tool can't be served (it fails to register, or the project file is
  invalid at startup), the global tool is served instead and its responses
  carry `used global tool '<name>': <reason>`. A name is never silently
  disabled.

Wire names stay bare (`find_files`, not `gl_find_files`). Scoped tool IDs for
telemetry and the UI are out of scope here (they matter with the dashboard).

Both files are polled every 2 seconds, as the project file already is, and
any change to the resolved set sends `tools/list_changed`. An invalid global
file keeps the last good global tools (fail closed, as for the project file);
invalid at startup serves no global tools. `devgraph://project-tools` gains
`global_tools_file`, the served tools with their origin (`global`, `project`,
`project (overrides global)`), and the notices from both layers.

## `devgraph config tools`

All subcommands take `--global` or `--repo <path>` (default: the current
directory's repository), print what they changed, and validate the whole
resulting file before writing; nothing invalid is ever written.

- `list [--json]` — the effective tool set for the scope: built-in (locked),
  global, project, and which project tools override a global. `--global`
  lists only the global store.
- `add --from <file|->` — add one tool, given as a YAML (or JSON) mapping in
  the tools-file format. Fails if the name exists in that scope (use `edit`)
  or is a built-in name.
- `edit <name> [--from <file|->]` — replace one tool. Without `--from`, opens
  the tool's YAML in `$EDITOR`; an unchanged or invalid result writes nothing.
- `delete <name>` — remove one tool; unknown names exit 1.
- `reset [--yes]` — remove every tool in the scope, returning to the next
  layer: for a repository, delete `devgraph.tools.yaml`; for `--global`, empty
  the store. Asks for confirmation unless `--yes`.

### Editing `devgraph.tools.yaml` without losing comments

The project file is hand-edited and committed, and its comments matter. The
CLI therefore edits it as text, never by re-dumping the whole document: it
locates each tool's mapping by its line range in the YAML node tree (PyYAML
`compose`) and splices only that range: `add` inserts after the last tool,
`edit` replaces the tool's lines, `delete` removes them. Comments and
formatting elsewhere are untouched; comments inside an edited tool are lost.
A file whose `tools` value isn't a block sequence (for example a flow list)
can't be spliced, and the CLI says so instead of rewriting it. The CLI never
stages or commits.

## Reporting

`config show` lists global tools and overrides. `config validate` and
`doctor` check the global store too (invalid file, built-in names) and report
project tools that override global ones as non-failing notices.

## Out of scope

Scoped tool IDs in telemetry; dashboard editing; script and composition
tools; global node types/schema (`config schema` is the next slice).

## Testing

The global store (atomic write, missing file, invalid file) and the text
splicer (add/edit/delete preserve surrounding comments; flow lists refused;
empty and missing files). Resolution: global served when scoped and not when
unscoped; project overrides global with the envelope notice; a project tool
that fails to register falls back to the global with a notice; built-in names
refused in both layers; reload of either file notifies; invalid global keeps
the last good. CLI: each subcommand in both scopes, refusals, `$EDITOR`
editing, `reset` confirmation; show/validate/doctor reporting.
