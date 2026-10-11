"""Runs the dashboard's repository-selector checks (repo_list_ui.js) as part of
the normal suite.

A fresh install, or removing the last repository, left a hard-coded
"devgraph" repository selected. The JS file drives populateRealRepos with an
empty, a changing and a shrinking registry.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("repo_list_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_repo_list_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
