"""The DevGraph agent: tray app, headless agent and their shared lifecycle.

`TrayApp` is imported lazily: importing it imports pystray, which on Linux
with no display raises at import, and every CLI command and the MCP server
import this package (for `lifecycle`).
"""

__all__ = ["TrayApp"]


def __getattr__(name):
    if name == "TrayApp":
        from devgraph.agent.tray import TrayApp

        return TrayApp
    raise AttributeError(name)
