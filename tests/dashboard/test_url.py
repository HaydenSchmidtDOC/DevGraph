"""`dashboard_url` gives the address the tray menu and CLI hand to a browser.

It must be one the dashboard's Host guard accepts and the server actually
listens on: a wildcard bind is reachable over loopback, never at the
wildcard address itself (which the guard refuses).
"""

import pytest

from devgraph.config.settings import Settings
from devgraph.dashboard.url import dashboard_url


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8765"),
        ("localhost", "http://localhost:8765"),
        ("192.168.1.20", "http://192.168.1.20:8765"),
        # IPv4 wildcard and the empty host both listen on 127.0.0.1.
        ("0.0.0.0", "http://127.0.0.1:8765"),
        ("", "http://127.0.0.1:8765"),
        # uvicorn binds `::` IPv6-only (asyncio sets IPV6_V6ONLY), so only
        # the IPv6 loopback reaches it.
        ("::", "http://[::1]:8765"),
        ("[::]", "http://[::1]:8765"),
        # IPv6 literals need brackets in a URL.
        ("::1", "http://[::1]:8765"),
        ("fd00::20", "http://[fd00::20]:8765"),
        ("[fd00::20]", "http://[fd00::20]:8765"),
    ],
)
def test_dashboard_url(host, expected):
    assert dashboard_url(Settings(dashboard_host=host, dashboard_port=8765)) == expected
