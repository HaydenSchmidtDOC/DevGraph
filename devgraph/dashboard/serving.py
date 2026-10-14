"""Run the agent's dashboard server, turning a port it cannot bind into a
message instead of uvicorn's silent `SystemExit`.

uvicorn logs a bind failure (hidden at the agent's `critical` log level) and
calls `sys.exit`, which `except Exception` does not catch. Binding the socket
here first gives the real `OSError`; the `SystemExit` catch covers any other
startup failure (a lifespan error).
"""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
from dataclasses import dataclass

import uvicorn

from devgraph.config.settings import devgraph_home

# Windows reports an address in use as WSAEADDRINUSE.
_ADDRESS_IN_USE = {errno.EADDRINUSE, 10048}


@dataclass(frozen=True)
class DashboardFailure:
    """Why the dashboard is off: `summary` fits a tray tooltip, `detail` is the log line."""

    summary: str
    detail: str


def _bind(host: str, port: int, backlog: int = 2048) -> list[socket.socket]:
    """Listening sockets for every address `host` resolves to, by asyncio's
    `create_server` rules (which uvicorn used): `localhost` gets ::1 and
    127.0.0.1, the empty host every address, and an IPv6 socket is IPv6-only.

    Each socket listens before the next is made, so a port another server
    already listens on fails here even where SO_REUSEADDR let the bind through.
    """
    infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    sockets: list[socket.socket] = []
    try:
        for family, kind, proto, _name, address in dict.fromkeys(infos):
            # Port 0 (tests): every address on the one port the first was given.
            address = (address[0], port, *address[2:])
            sock = socket.socket(family, kind, proto)
            sockets.append(sock)
            if sys.platform == "win32":
                # SO_REUSEADDR on Windows lets two servers share a port; this refuses any sharing.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(address)
            sock.listen(backlog)
            port = sock.getsockname()[1]
    except OSError:
        for sock in sockets:
            sock.close()
        raise
    for sock in sockets:
        sock.set_inheritable(True)
    return sockets


def _fix_hint() -> str:
    return (
        f"set DEVGRAPH_DASHBOARD_PORT to a free port in {devgraph_home() / '.env'} "
        "(or the environment) and restart the agent"
    )


def serve_dashboard(server: uvicorn.Server, loop: asyncio.AbstractEventLoop) -> DashboardFailure | None:
    """Serve until the server exits; the failure when it could not start, else None."""
    host, port = server.config.host, server.config.port
    try:
        sockets = _bind(host, port, server.config.backlog)
    except OSError as exc:
        if exc.errno in _ADDRESS_IN_USE:
            return DashboardFailure(
                f"dashboard port {port} in use",
                f"dashboard port {port} on {host} is already in use (another program, or another "
                f"DevGraph agent, holds it), so the dashboard is off; {_fix_hint()}",
            )
        return DashboardFailure(
            f"dashboard cannot bind port {port}",
            f"dashboard cannot listen on {host}:{port} ({exc.strerror or exc}), so it is off; {_fix_hint()}",
        )
    try:
        loop.run_until_complete(server.serve(sockets=sockets))
    except SystemExit:
        return DashboardFailure(
            "dashboard failed to start", f"dashboard on {host}:{port} failed to start, so it is off"
        )
    finally:
        for sock in sockets:
            sock.close()
    return None
