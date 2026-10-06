# Per-repository opt-in for project tools — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1 (tool plane). Stacked on
`epic1/g2b3-form-editor`.

## Problem

A repository can ship `devgraph.tools.yaml`, and until now an MCP session scoped
to it served those tools by default. The `$repo_id` rule is textual: the
validator only checks that the query *references* `$repo_id`, so
`MATCH (n) WHERE $repo_id IS NOT NULL RETURN n` passes and reads every
repository's graph. Cloning and registering a repository therefore handed its
author a read of the whole graph. Global tools (the user's own store) are
trusted and unaffected.

## Decision

Project tools are **off until the user trusts them for that repository**, from
the CLI, and the approval is pinned to the SHA-256 of the tools file's exact
bytes. Any change to the file (by hand, `git pull`, the CLI or the dashboard)
makes it untrusted again until it is re-approved. The `$repo_id` rule stays,
documented as a convention rather than a sandbox: an enabled tool can read the
whole graph.

## Trust record

- A nullable `project_tools_sha256` column on the registry's `repos` table
  (added by the registry's existing column migrations; `NULL` = not trusted).
  It lives in the registry database at its existing path, never in the
  repository, so a repository cannot approve itself.
- `devgraph/config/project_trust.py` answers, for a repository path and the
  file bytes the caller read, one of `trusted`, `untrusted` (no approval, or
  the repository is not registered), `changed` (an approval for other bytes)
  or `error`. Like `project_switch` it is looked up by path and reads the
  registry read-only. It **fails closed**: a missing database is `untrusted`;
  any SQLite error (locked, no column yet, corrupt) is `error`; only
  `trusted` serves.

## Commands

- `devgraph config tools trust [REPO] [--sha256 HEX]` — `REPO` is a repo id or
  a path (default: the registered repository containing the current
  directory). It reads the file once, refuses an absent or invalid file, and
  prints every tool's name and Cypher, then the SHA-256 of those bytes, then a
  warning that an enabled tool can read the whole graph. On a TTY it asks for
  confirmation. With `--sha256` it does not ask, and succeeds only when the
  value matches the bytes it just read (so a script approves exactly the
  content it reviewed). Without a TTY and without `--sha256` it refuses.
- `devgraph config tools untrust [REPO]` clears the approval.
- `config tools list` shows the trust state (`trust` in `--json`).
- CLI edits (`config tools add|edit|delete|reset`) never re-trust: they say the
  tools stop being served until `devgraph config tools trust <repo_id>` runs.

## Serving

`tools_fingerprint` (what the tool plane polls every 2 s) asks the trust
lookup after reading the bytes. Anything but `trusted` becomes an
`UntrustedTools(state, data)` fingerprint, so a trust change reloads the
session just as a file change does. For it the project layer serves **no**
project tools, records `project_trust` on the status, and adds the notice and a
per-name fallback reason `project tools not trusted (run devgraph config tools
trust <repo_id>)` (prefixed with "devgraph.tools.yaml changed since it was
trusted" or "the trust state could not be read" where that applies). Global
tools of the same names then serve as normal, carrying that reason in their
`used global tool` notice.

Keep-last-good applies only to trusted content: an untrusted fingerprint
resets the last good tools, so an edited file is never served from memory, and
only a trusted file whose bytes then fail to parse keeps the previous trusted
tools. Since `trust` refuses an invalid file, that case is a parser change,
not a user edit.

## Dashboard

- Each repository's Tools section shows a trust badge — Trusted, Not trusted,
  Changed since trusted, Trust state unreadable — and the per-tool badges show
  the untrusted fallback reason. The tools block carries `trust` (state and
  the command to run).
- `DELETE /api/config/{repo_id}/trust/tools` revokes (cross-site refused like
  every Config write). There is no route that grants trust; approval is
  CLI-only. The page shows **Revoke trust** while an approval is recorded.
- Every project-tools write (add, replace, delete, reset, copy) changes the
  hash, so its notes — dry run included — say "Saving stops <repo>'s project
  tools being served until you run `devgraph config tools trust <repo>`". No
  hash is printed.

## Doctor

`devgraph doctor` reports a valid tools file that is not trusted, changed
since it was trusted, or whose trust state can't be read as a non-failing
warning naming the command.

## Testing

Registry migration and setter; the lookup (trusted, untrusted, changed,
unregistered, missing database, SQLite error → not served); the tool plane
(untrusted not served, trusted served, edit → not served on reload and not kept
as last good, global fallback with the reason); CLI trust (TTY confirm, wrong
`--sha256` refused, no TTY without `--sha256` refused, invalid file refused),
untrust and list; doctor; routes (badge state, revoke, no route trusts, dry-run
note text); the node harness for the badge and Revoke button.
