"""Runs the dashboard's All-Repos scope checks (all_repos_scope.js) as part of
the normal suite.

"All Repos" used to match every node in the graph, including repositories
left behind after they were unregistered; it now matches only registered
repo ids, the same bound MCP cross_repo calls get.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("all_repos_scope.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_all_repos_scope():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
