"""Runs the register-repo UI checks (register_repo.js) as part of the normal suite.

Same harness as test_layout_save_path.py: the logic under test is browser
JavaScript driving `POST /api/repos`, and its failure modes (a double-click
registering twice, a button left disabled after a network error, server text
rendered as markup) are all invisible from the server side.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("register_repo.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_register_repo_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
