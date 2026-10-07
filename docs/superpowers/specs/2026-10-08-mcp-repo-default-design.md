# MCP built-ins default `repo_id` to the session's repository — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1. Builds on the tool plane
(`2026-10-04-tool-plane-design.md`), which already resolves the session's
repository for project and global tools.

## Problem

All 24 built-in MCP tools require `repo_id`. The server instructions and
DEVGRAPH-CLIENT.md tell the assistant to "always pass this repo's `repo_id`",
but the assistant has to remember or guess it. A wrong guess is not an error:
an unknown id returns empty results, and another registered id returns that
repository's answers. Both look like real answers.

The MCP process already knows its repository. `resolve_session_repo`
(`devgraph/mcp/tool_plane.py`) picks it once at startup: `DEVGRAPH_MCP_REPO`
(a repo id, or an absolute path inside a registered repository), else the
working directory, else none. Project tools inject it. Built-ins ignore it.

## Goal

- A built-in called without `repo_id` runs against the session's repository. A dict response says which repository was used. The two list-returning built-ins can't carry a key, so the server instructions name the repository instead (R5).
- A built-in called with `repo_id` behaves exactly as it does today, byte for byte.
- When there is no session repository, a call without `repo_id` fails with an error that lists the registered repositories. It never picks one.
- No new config knob and no new dependency.

## Decisions

| # | Decision |
| --- | --- |
| R1 | **Resolution order:** explicit argument, then the session repository, then an error. |
| R2 | **One resolution for both planes.** Built-ins reuse the session repository `build_server` already receives. Nothing is resolved again. |
| R3 | **`repo_id` becomes optional in every built-in's input schema.** Nothing else in any schema changes. |
| R4 | **One wrapper, at the existing registration chokepoint.** The Python tool definitions do not change. |
| R5 | **Transparency:** a defaulted dict response gains `repo_id` and a `notices` entry. The session repository is also named in the server instructions. |
| R6 | **No new trust boundary**, and no fallback to "any repo". |
| R7 | **Docs** say the argument is optional and when to pass it. |

### R1: resolution order

1. **Explicit argument wins.** Any value other than JSON `null`, including `""`, an unregistered id or another repository's id, is passed through untouched. Explicit values are not validated, because validation would change today's explicit calls (R3).
2. **Session repository.** It is used when `repo_id` is omitted or `null`, provided it is still active. On this path the wrapper makes one `registry.list_repos(active_only=True)` read at call time. If the session repository's id is not among the results, the call fails (R1.3) with the reason `session repository '<id>' is no longer registered or active`. Explicit calls never make this read.
3. **Error.** Otherwise the call fails as an MCP tool error (`ToolError`, as in `tool_plane._failure`) and the tool body never runs. The message:
   - says `repo_id` is required because this session has no repository;
   - gives the reason, which is one of:
     - `DEVGRAPH_MCP_REPO=<value>` matches no registered repository;
     - it names an inactive one;
     - the server's working directory is not inside a registered repository and `DEVGRAPH_MCP_REPO` is unset;
     - the session repository is no longer registered or active (R1.2);
   - lists the active registered repositories as `id (name)`, read from the registry at call time, or says there are none and points at `devgraph add`;
   - ends with: `pass repo_id explicitly, or restart the MCP server after registering a repository`. The session repository is resolved only at startup, so a repository registered later is not picked up without a restart.

The edge cases all follow from R2:

| Case | Result | Why |
| --- | --- | --- |
| `DEVGRAPH_MCP_REPO` set but matching no active registered repository | error (R1.3). There is no cwd fallback. | A wrong pin must be loud, not quietly replaced by the cwd. This matches project tools (`test_an_unknown_env_value_never_falls_back_to_cwd`). |
| `DEVGRAPH_MCP_REPO` names an inactive repository | error, and the reason says "inactive" | Same as above. The tool plane already tells the two apart (`_inactive_match`). |
| cwd inside a registered repository nested in another registered one | the deepest (inner) repository | `_deepest_containing`. The cwd is closest to the inner one. |
| cwd inside an *unregistered* nested repository (for example a submodule) inside a registered one | the registered outer repository | Only registered repositories can be chosen. The outer one is what the user registered, and the notice names it. |
| absolute `DEVGRAPH_MCP_REPO` path inside nested registered repositories | the deepest | Same rule as cwd. |
| session repository unregistered or deactivated after startup | error: "no longer registered or active", listing the active repositories | One registry read per defaulted call. A silent empty answer is what this slice exists to remove. |
| exactly one repository registered and no session repository | error, listing that one | Never "any repo" (R6). The cost is one retry with the listed id. |
| project config disabled, or the tools file untrusted | the default still applies | The default is not project config. It reads no file in the repository. |
| `cross_repo=true` without `repo_id` | defaults or errors as above | Several built-ins still use `repo_id` with `cross_repo` (declared labels in `search_component`, the git root in `impact_analysis_for_diff`), so they still need a home repository. |

### R2: one resolution

`build_server` already takes `session_repo`, `session_source` and
`session_pinned`, and `main()` fills them from `resolve_session_repo`. The
default reuses those values. The session repository is fixed for the life of
the process. Nothing re-resolves it: tools-file reloads don't, registry changes
don't, and there is no cwd fallback. The one call-time check is R1.2's: a
defaulted call confirms that the session repository is still active, and errors
if it is not. It never picks a replacement. An explicit stale id still returns
empty results, as it does today.

### R3: schemas

**Which tools.** All 24 built-ins take `repo_id`. `run_cypher` takes none and
is unchanged. Project and global tools never expose `repo_id` (it is injected)
and are unchanged. No built-in is cross-repo-only: every one scopes by
`repo_id` unless `cross_repo=true`.

**Schema delta.** For each of the 24, the listed `inputSchema` differs from
today's in exactly two ways:

- `repo_id` is dropped from `required`;
- the `repo_id` property becomes `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Repo Id"}`.

Property order, the other properties, the other `required` entries,
descriptions, annotations and names are all unchanged. This was checked
against the SDK in use (mcp 2.3.0).

**Epic gate.** The epic #1 gate "all 18 existing MCP tool signatures unchanged"
exists to keep clients working. The delta only widens what is accepted: every
call valid today is still valid and gets the same response. The PR should name
this widening explicitly rather than claim zero change.

**Pins.** `catalog.py` (`TOOL_CATALOG`) carries no schemas and is unchanged.
`tests/mcp/test_server.py` and `test_server_telemetry.py` pin only names,
the count (24), descriptions and annotations. No existing test pins a
built-in's input schema.

### R4: where it plugs in

- **New wrapper.** A new `_with_repo_default(fn, ...)` in `server.py` is applied inside `_instrumented_tool`, innermost. The chain becomes `_instrument(_with_shadow_notices(_with_repo_default(fn)))`.
- **Functions without `repo_id`.** For a function with no `repo_id` parameter (`run_cypher`), the wrapper returns it unchanged.
- **Signature.** Otherwise the wrapper sets `__signature__`:
  - it starts from `inspect.signature(fn, eval_str=True)`;
  - every parameter becomes keyword-only, because MCP always calls by keyword and Python forbids an optional parameter before required positional ones;
  - `repo_id` gets the annotation `str | None` and the default `None`.

  The SDK reads this signature through `functools.wraps`' `__wrapped__` chain, so the schema delta is exactly R3.
- **Call.** With `repo_id` present and not `None`, the wrapper calls `fn(**kwargs)` and returns its result object untouched. Otherwise it checks that the session repository is still active (R1.2), then substitutes it (R5). If there is no session repository, or it is no longer active, it raises (R1.3).
- **Unchanged definitions.** The 24 definitions keep `repo_id: str` and their bodies. A built-in added later gets the default by being registered.
- **Instrumentation and telemetry.** Instrumentation still sees the original name. Telemetry still records no arguments and no defaulted id (`_TELEMETRY_FIELDS` is unchanged). A raised error is recorded as `ok: false`.

### R5: transparency

- **Defaulted dict response.** It returns `{**result, "repo_id": "<id>", "notices": [...existing, notice]}`. No built-in's dict result has a top-level `repo_id` today: `summarise_repository` uses `repo_name`. `notices` is the key `_with_shadow_notices` already uses, and that wrapper appends to it, so both kinds of notice coexist. The `count`/`results`/`truncated` envelopes are untouched.
- **Notice wording:** `repo_id not given; used this session's repository '<id>' (from DEVGRAPH_MCP_REPO)`, or `(from the server's working directory)`.
- **Defaulted list response** (`find_requirements_for`, `blame_component`). It is returned unchanged, because a list can't carry a key without changing its envelope. This is the same limit the shadow notices accept. These two tools rely on the instructions below.
- **Instructions.** The current sentence in `build_server`'s `instructions` (server.py about lines 320–322), "Every built-in tool takes a repo_id (the id shown by `devgraph list`) and defaults to that repo only", is rewritten so it does not contradict the default. The `cross_repo` rule that follows it stays. The `instructions` (sent once at session start) name the session repository and its source when there is one, for example "This session's repository is `<id>`; built-in tools use it when repo_id is omitted." With no session repository they say `repo_id` is required and point at `devgraph list`. The `devgraph://project-tools` resource already reports `scope.repo_id` and `scope.source`.
- **Explicit calls** get no `repo_id` key and no notice, even when the value equals the session repository.

### R6: safety

- **Where the session repository comes from.** It is computed only from:
  - the MCP process's environment;
  - its working directory;
  - the registry in the DevGraph state directory.

  The client configuration that launches the process sets the first two. Anything able to set them can already run the server with any arguments. Nothing inside a repository is read to choose it, so a cloned repository cannot steer it.
- **No new capability.** An explicit `repo_id` (or `cross_repo=true`) can already name any registered repository. The default only fills in a value the caller could have passed. Repository isolation for project tools (the injected `$repo_id` they can't override) is untouched.
- **A project `.mcp.json` pin.** A repository's own `.mcp.json` could pin `DEVGRAPH_MCP_REPO` to another registered repository. That changes only the default, which is no more than the explicit argument allows. Claude Code also asks the user before it starts a project-scoped server.
- **No fallback.** There is never a fallback to "any repo": no first, sole or most recently registered repository. With no session repository the call errors (R1.3).
- **Error message contents.** It lists only active repository ids and names, which `devgraph list` already shows and which every MCP caller can already use.
- **Known limitation: the VS Code entry's working directory.** `devgraph client-config` writes VS Code's user-level `mcp.json` with `cwd` set to the DevGraph checkout. If that checkout is itself registered, an unpinned VS Code session defaults to DevGraph's own repository.
  - Project tools already behave the same way.
  - The notice names the repository on every defaulted call.
  - Pinning with `DEVGRAPH_MCP_REPO` (R7) avoids it.
  - Changing `client-config` is out of scope.

### R7: docs

- **DEVGRAPH-CLIENT.md** (around lines 209–211). Replace "Always pass this repo's `repo_id`" with:
  - built-ins default to the session's repository;
  - a defaulted dict response carries `repo_id` and a notice, so check it;
  - `find_requirements_for` and `blame_component` return lists, which carry no notice; for those, the session repository is the one named in the server instructions and in `devgraph://project-tools`;
  - pass `repo_id` explicitly only for a different repository, or when the session has none (the error lists them);
  - `cross_repo` guidance is unchanged.

  Step 1's "Record that `repo_id`" becomes "you'll need it to pin the session or to pass explicitly". The project tools paragraph's pin example also applies to the built-ins.
- **README.** In Operating model, next to "Repository isolation", add a bullet on the default and the no-fallback rule. In "Project tools", the serving bullet says the same session repository now also defaults built-ins.
- **PROJECT_STATUS.md.** A line under the tool plane entry. The `devgraph/mcp/` map line says built-ins default `repo_id`.
- **Server.** The module docstring and `build_server` docstring.

## Out of scope

- Validating explicit `repo_id` values.
- Changing `client-config`'s VS Code `cwd`.
- Per-call session switching.
- Adding a key to list-returning built-ins.
