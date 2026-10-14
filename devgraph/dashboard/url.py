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

    A wildcard bind is reached over loopback: `0.0.0.0` listens on every
    IPv4 address and the empty host on every address, so 127.0.0.1 reaches
    both, while `::` is bound IPv6-only (IPV6_V6ONLY, as asyncio sets it), so
    only `::1` reaches it. IPv6 literals are bracketed.
    """
    host = settings.dashboard_host.strip().strip("[]")
    if host == "::":
        host = "::1"
    elif host in WILDCARD_HOSTNAMES:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.dashboard_port}"


#: The body of the dashboard's `GET /api/health`: how a client tells DevGraph's
#: dashboard from another program listening on the same port.
DASHBOARD_IDENTITY = {"service": "devgraph-dashboard"}
#: Text at the start of the dashboard page (`GET /`) of every DevGraph version,
#: including those from before `/api/health`.
DASHBOARD_PAGE_MARKER = "<title>DevGraph"


def probe_dashboard(url: str, timeout_s: float = 3.0) -> str:
    """Who answers at `url`: "devgraph"; "outdated" (a DevGraph agent from
    before `/api/health`, which a restart updates); "other" (another program
    holds the port); or "none" (nothing listens there).

    The connect is tried apart from the requests: a refused or unanswered
    connect is "none" (Windows retries a refused connect for about two
    seconds), while a listener that then misbehaves is "other".
    """
    import http.client
    import json
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout_s)
    except ValueError:  # not an address anything could listen on
        return "none"
    try:
        try:
            connection.connect()
        except OSError:
            return "none"
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            body = response.read(4096)
            if response.status == 200:
                return "devgraph" if json.loads(body) == DASHBOARD_IDENTITY else "other"
            connection.request("GET", "/")
            page = connection.getresponse().read(4096)
            return "outdated" if DASHBOARD_PAGE_MARKER.encode() in page else "other"
        except (OSError, ValueError, http.client.HTTPException):
            return "other"
    finally:
        connection.close()
