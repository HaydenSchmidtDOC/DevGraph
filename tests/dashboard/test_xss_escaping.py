"""Runs the dashboard's markup-escaping checks (xss_escaping.js) as part of the
normal suite.

Commit messages, authors, file paths, node names and repo ids all come from
repositories the user indexes, not from the user, and the dashboard renders
them through innerHTML on the same origin as `POST /api/cypher`. The JS file
drives each renderer out of index.html with hostile strings and fails on any
live tag or attribute breakout in the markup it produces.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("xss_escaping.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_xss_escaping():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
