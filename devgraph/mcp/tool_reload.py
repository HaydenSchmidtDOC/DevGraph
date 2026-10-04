"""Telling MCP clients that the session's project tools changed.

`ProjectToolPlane.reload_if_changed` re-serves `devgraph.tools.yaml`; this
module polls it and notifies clients: `notifications/tools/list_changed` to
legacy-protocol clients (whose connection is captured by a middleware, since
it is only reachable from a request) and `ToolsListChanged` on the
subscription bus for `subscriptions/listen` clients. Both are sent; each is a
no-op for the other kind of client.

Relies on mcp 2.3.0 internals (`_lowlevel_server`, `_subscriptions`,
`ctx.session._connection`); tests exercise each against the real SDK.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import ToolsListChanged

from devgraph.mcp.tool_plane import RELOAD_INTERVAL_S, ProjectToolPlane

logger = logging.getLogger(__name__)


class ToolListNotifier:
    """Sends tool-list-changed notifications to every client of `server`."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self._connections: dict[int, Any] = {}
        server._lowlevel_server.middleware.append(self._capture)

    async def _capture(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        # One connection per client; the per-request session would duplicate notifications.
        connection = getattr(getattr(ctx, "session", None), "_connection", None)
        if connection is not None:
            self._connections[id(connection)] = connection
        return await call_next(ctx)

    async def notify(self) -> None:
        for connection in list(self._connections.values()):
            try:
                await connection.send_tool_list_changed()
            except Exception:
                logger.debug("could not send tools/list_changed", exc_info=True)
        await self._server._subscriptions.publish(ToolsListChanged())


async def poll_tool_reloads(
    plane: ProjectToolPlane,
    notifier: ToolListNotifier,
    *,
    interval_s: float = RELOAD_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    """Reload the tools file whenever it changes, until cancelled."""
    while True:
        await sleep(interval_s)
        try:
            changed = plane.reload_if_changed()
        except Exception:
            logger.warning("reloading project tools failed; keeping the current tools", exc_info=True)
            continue
        if changed:
            await notifier.notify()


def initialization_options(server: Any, *, tools_changed: bool) -> Any:
    return server._lowlevel_server.create_initialization_options(NotificationOptions(tools_changed=tools_changed))


async def run_stdio(server: Any) -> None:
    """`MCPServer.run_stdio_async`, plus project-tool reloads when the session has a repository."""
    plane: ProjectToolPlane = server.devgraph_tool_plane
    scoped = plane.repo is not None
    notifier = ToolListNotifier(server) if scoped else None
    async with stdio_server() as (read_stream, write_stream):
        async with anyio.create_task_group() as tasks:
            if notifier is not None:
                tasks.start_soon(poll_tool_reloads, plane, notifier)
            await server._lowlevel_server.run(
                read_stream, write_stream, initialization_options(server, tools_changed=scoped)
            )
            tasks.cancel_scope.cancel()
