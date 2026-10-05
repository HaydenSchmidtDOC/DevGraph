# `devgraph config schema` commands — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §6 (`config schema list / add /
edit / delete / reset`). Stacked on #34 (`epic1/f2-global-tools`), whose
comment-preserving editor this reuses.

## Commands

All take `--repo <path>`; without it they use the deepest registered
repository containing the current directory, else the current directory with
a warning (the same rule as `config tools`). There is no `--global`: the epic
has no global schema store, and `config show --global` already shows the
built-in schema.

- `list [--json]` — the effective schema for the repository: node types and
  relationships, each marked `built-in` or `project`, with keys and providers.
  A disabled repository says so and lists the built-in schema.
- `add --from <file|->` — add one entry, given as a YAML (or JSON) mapping in
  the schema-file format: a mapping with `label` is a node type, one with
  `type` is a relationship; anything else is refused. Fails if the label
  already exists (use `edit`). Relationships may legitimately share a type
  with different endpoints, so adding one is refused only if an identical
  entry exists.
- `edit <name> [--from <file|->]` — replace one entry: `<name>` is a node type
  label or a relationship type. Without `--from` the entry opens in
  `$EDITOR`; an unchanged or invalid result writes nothing. A relationship
  type declared more than once can't be addressed by name and is refused with
  a message to edit the file by hand.
- `delete <name>` — remove one entry (same lookup rules); unknown names exit 1.
- `reset [--yes]` — delete `devgraph.schema.yaml`, returning the repository to
  the built-in schema. Asks for confirmation unless `--yes`.

A name that matches both a node type and a relationship type (possible only
with unconventional naming) is refused with a message to pass
`--node-type` or `--relationship`, which select the list explicitly.

## Writing

Every write validates the whole resulting file exactly as the indexer would
(loader validation plus resolution against the built-in schema, so label
conflicts, key/metadata mismatches and provider agreement are all checked)
and writes nothing if it is invalid. Writes are atomic and keep the file's
mode. The file is edited as text, splicing only the entry's lines, so
comments elsewhere survive — the editor from #34, generalised from `tools`
to any top-level list key and identity field. A new file starts with
`version: 1`. The CLI never stages or commits.

After a write the command says when it takes effect: "applied at the next
rescan (watched repositories; otherwise `devgraph rescan <repo_id> --now`)";
for a disabled repository, "not applied while the project config is
disabled"; for an unregistered directory, that DevGraph doesn't index it.
Applying a schema that removes a type deletes that type's nodes at the
rescan (#29); `delete` and `reset` say so when they remove node types or
relationships.

## Out of scope

Editing `extends` and other top-level keys (hand edit); a global schema
layer; dashboard editing.

## Testing

The generalised editor (tools behaviour unchanged; node_types and
relationships lists in one file; comments preserved; duplicate relationship
types refused by name). CLI: each subcommand, default scope, validation
refusals (built-in label, key not in metadata, unknown endpoint), `$EDITOR`,
reset confirmation, the effect notes, disabled and unregistered repositories.
