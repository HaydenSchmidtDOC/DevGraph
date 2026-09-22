"""Runs the Query telemetry card's MCP checks (mcp_telemetry_ui.js) as part of
the normal suite.

tests/dashboard/test_mcp_telemetry_routes.py covers the server side of
`/api/mcp-telemetry`; this covers the browser side, which is where the numbers
a user actually reads are computed. Its failure mode is quiet by construction:
the endpoint answers `{"entries": []}` both when no MCP client has run a tool
and when the local store is missing or unreadable, so a card that has stopped
reading, or one that quietly rounds a malformed record into a figure, looks
much like a card with nothing to report. The JS file drives the real
summarize/render/fetch functions, the real refreshTelemetry and the real
bootConnect out of index.html against stubbed responses, so those directions
are checked without a browser.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("mcp_telemetry_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_mcp_telemetry_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
