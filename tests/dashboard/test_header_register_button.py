"""Runs the header register-button checks (header_register_button.js) in the suite.

Same harness as test_register_repo_ui.py: the behaviour under test is browser
JavaScript, and its failure mode -- a header button that renders and looks
clickable but has no listener bound to it -- is completely invisible from the
server side.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("header_register_button.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_header_register_button():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
