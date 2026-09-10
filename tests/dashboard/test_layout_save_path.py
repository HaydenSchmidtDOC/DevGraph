"""Runs the dashboard's layout save-path checks (layout_save_path.js) as part
of the normal suite.

The logic under test is browser JavaScript, and the failure it exists to catch
is invisible from the server side: a `fetch(..., {keepalive: true})` body over
64KB is rejected by the browser before it reaches the network, so a save that
silently never happens looks identical to one that was never triggered. The JS
file drives the real functions out of index.html against stubs, so it catches
that class of bug without a browser.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("layout_save_path.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_layout_save_path():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
