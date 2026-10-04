"""The dashboard address to hand a browser, from the configured bind host.

Kept free of FastAPI imports: the CLI and tray build this URL without
loading the web app.
"""

from __future__ import annotations

from devgraph.config.settings import Settings

# Bind-to-everything addresses: meaningful to listen on, never a name a
# browser should address the dashboard by (the Host guard in `app.py`
# refuses them).
WILDCARD_HOSTNAMES = frozenset({"", "0.0.0.0", "::"})


def dashboard_url(settings: Settings) -> str:
    """`http://<host>:<port>` for the running dashboard.

    A wildcard bind is reached over loopback: `0.0.0.0` and the empty host
    listen on 127.0.0.1, while uvicorn binds `::` IPv6-only (asyncio sets
    IPV6_V6ONLY), so only `::1` reaches it. IPv6 literals are bracketed.
    """
    host = settings.dashboard_host.strip().strip("[]")
    if host == "::":
        host = "::1"
    elif host in WILDCARD_HOSTNAMES:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.dashboard_port}"
