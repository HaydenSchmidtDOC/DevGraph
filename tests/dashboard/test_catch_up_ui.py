"""Runs the catch-up pill checks (catch_up_ui.js) as part of the suite.

Skipped when node isn't on PATH -- the Python suite has to run everywhere."""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("catch_up_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_catch_up_ui():
    result = subprocess.run([shutil.which("node"), str(_SCRIPT)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
