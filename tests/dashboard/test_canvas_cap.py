"""Runs the dashboard's canvas cap checks (canvas_cap.js) as part of the
normal suite.

The canvas's default view used to fetch a whole repository (every node, every
edge, every property) on open and again on every save; it is now capped at
CANVAS_NODE_CAP nodes, says so, and coalesces live refreshes.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("canvas_cap.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_canvas_cap():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
