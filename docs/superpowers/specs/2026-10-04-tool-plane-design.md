# MCP tool plane (serving project tools) — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §7–§8. Stacked on the tools-file
slice (#30), which defines and validates `devgraph.tools.yaml`. This slice
serves those tools to MCP clients. Hot reload and `tools/list_changed` are the
next slice.

## Session scope

The epic pins a session to one repository by resolving the client's cwd or
MCP roots. In practice DevGraph's MCP server is launched with the DevGraph
checkout as its working directory (`devgraph mcp add` and the client-config
snippets set it), and MCP roots are deprecated in the SDK in use. So the scope
is resolved, once at startup, as:

1. `DEVGRAPH_MCP_REPO` — a registered repo id, or a path inside a registered
   repository — if set. A value that matches nothing serves no project tools
   (never a guess).
2. Otherwise the server process's working directory, if it lies inside a
   registered repository (the deepest match wins).
3. Otherwise no project scope: built-in tools only.

The resolved scope is fixed for the process lifetime: a session pinned to one
repository can never reach another's tools.

## Serving

For the scoped repository, each valid tool in `devgraph.tools.yaml` is
registered as an MCP tool with its declared name, description and a typed
input schema built from its parameters (required parameters required; optional
ones with their default, or null). Tools carry the read-only annotation and
the same telemetry as built-ins.

A call:

- binds the declared parameters, then sets `repo_id` to the session's
  repository (the author cannot override it: it is not a declared parameter);
- runs in a **read transaction** (read access mode) with the tool's
  `timeout_s` as the server-side transaction timeout;
- returns at most `max_rows` rows as `{count, results, truncated}` — `count`
  is the number of rows returned and `truncated` is true when more existed;
  string values are sanitized like every other tool's;
- reports a Neo4j failure (timeout, a write refused by read mode, a syntax
  error) as a tool error naming the tool.

## Resolution and notices

- A project tool named like a built-in is not registered; the built-in is used
  and a notice records it.
- An invalid tools file serves no project tools; a notice records it.
- Notices and the scope are published in a new `devgraph://project-tools`
  resource (`{scope: {repo_id, source}, tools_file, served, notices}`), and
  project-tool responses carry a `notices` list when there are any. `config
  validate` and `doctor` already report both conditions.
- `devgraph://tool-catalog` lists served project tools alongside built-ins.

## Out of scope

Hot reload and `tools/list_changed` (next slice); global configurable tools;
script and composition tools; a `devgraph mcp add --repo` helper (the docs give
the `claude mcp add … -e DEVGRAPH_MCP_REPO=<repo_id>` command).

## Testing

Live Neo4j: read-transaction rows, the row cap and truncation flag, a timeout,
a write refused by read mode. Server: scope resolution (env by id/path/unknown,
cwd deepest match, none); tool listing and input schema; a call injects
`repo_id` and returns the envelope; built-in-name and invalid-file notices; the
status resource; no change for a server without a scope (tool counts intact).
