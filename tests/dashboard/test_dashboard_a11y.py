"""Runs the dashboard keyboard and screen-reader checks (dashboard_a11y.js) in the suite.

Same harness as test_header_register_button.py: keyboard and screen-reader
behaviour lives entirely in browser JavaScript and markup, invisible to the
server-side tests.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("dashboard_a11y.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_a11y():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
