# Project tool reload and `tools/list_changed` — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §7. Stacked on the tool plane
slice (#31), which serves a scoped repository's `devgraph.tools.yaml` tools but
only reads the file at startup.

## Behaviour

While an MCP session has a scoped repository, the server notices edits to that
repository's `devgraph.tools.yaml` and serves the new tool set without a
restart, then tells the client its tool list changed.

- **Detection.** The server polls the file every 2 seconds, inside its own
  event loop (no threads), and compares a fingerprint: the file's bytes, or
  "absent", or "unreadable". Polling, not the tray watcher, because the MCP
  server is a separate process that may run without the tray, and the file is
  tiny.
- **Reload.** When the fingerprint changes, every project tool the session
  served is removed and the file is loaded again under the startup rules:
  valid tools are served, built-in names are refused, an absent file serves
  nothing, a tool that fails to register is skipped — each with the same
  notices. File-level notices are replaced, not accumulated; scope notices are
  untouched (the scope never changes).
- **An invalid save keeps the last good tools** (epic §9: fail closed by
  keeping the last good config, never dropping the tool surface). The tools
  stay served, a notice says the file is invalid and the last good tools are
  kept, and a warning goes to stderr; the next valid save reloads. A file that
  is invalid when the session starts has no last good state, so it serves
  nothing until fixed. Deleting the file removes the tools (deliberate).
- **Notification.** After a reload that changes the served tool set or any
  served tool's definition, the server sends `notifications/tools/list_changed`
  to legacy-protocol clients and publishes `ToolsListChanged` to modern
  (`subscriptions/listen`) clients. The server advertises
  `tools.listChanged: true` in its initialize result. On 2026-07-28
  connections the SDK advertises `listChanged` whenever `subscriptions/listen`
  is served, so an unscoped server advertises it there too; this is harmless.
- **Mid-save states.** A save can be seen half-done (an empty file caught
  mid-write, or delete-then-recreate), which can produce two notifications
  about 2 seconds apart. Accepted: the second poll corrects the tool set.
- **Failed and racy reloads.** The file is read once per load, as its
  fingerprint, and the reload (startup included) parses exactly those bytes,
  so a save landing mid-load cannot make the served tools differ from the
  fingerprint; a later save changes the fingerprint and is picked up on the
  next poll. A reload that raised is retried on the next poll. A missing
  repository root has its own fingerprint (`root-missing`), so its notice is
  cleared when the root returns. A reload that ends with file-level notices
  is logged as a warning on stderr.
- **Status.** `devgraph://project-tools` and `devgraph://tool-catalog` reflect
  the reloaded state.
- **No scope, no polling.** A server without a session repository behaves
  exactly as today.

## SDK integration (mcp 2.3.0)

Verified by a spike against the installed SDK, including a real stdio
subprocess client:

- `MCPServer.run_stdio_async` passes no `NotificationOptions`, so the server
  runs its own stdio loop: `stdio_server()` plus
  `server._lowlevel_server.run(read, write,
  create_initialization_options(NotificationOptions(tools_changed=True)))`,
  with the reload poller in the same task group.
- Legacy notifications need the connection, which is only reachable from a
  request context: a middleware on `server.middleware`
  records `ctx.session._connection` on `initialize` requests only (legacy
  clients: one long-lived connection each; 2026-07-28 clients never send
  `initialize` and get a new connection per request, which must not be
  captured), and the poller calls `send_tool_list_changed()` on each.
- Modern notifications: `server._subscriptions.publish(ToolsListChanged())`;
  a no-op without listeners and on legacy connections, so both are always sent.
- `add_tool` / `remove_tool` are synchronous dict mutations, safe from a task
  on the server's loop.

These are private attributes; the SDK version is pinned, and tests exercise
each against the real SDK (including `run_stdio` over memory streams) so an
upgrade that moves them fails loudly.

## Out of scope

Reloading the scope itself; global tools; reload of `devgraph.schema.yaml`
(already handled by the tray's schema rescan).

## Testing

Reload: add, remove and change a tool; unchanged file is a no-op; invalid file
serves nothing with a notice, then a fix restores the tools; deleted file
serves nothing; built-in names still refused; the status resource and catalog
follow. Notification: an in-process legacy client receives
`tools/list_changed` and re-lists the new set; a modern listener receives
`ToolsListChanged` and modern clients are never captured for legacy
notifications; `run_stdio` advertises `listChanged` for a scoped server only
and ends when the client closes; the initialize result advertises `listChanged: true`; no
notification when nothing served changed.
