"""Runs the Database & memory card's heap checks (database_stats_ui.js) as
part of the normal suite.

The logic under test is browser JavaScript, and its failure mode is quiet by
construction: the card is designed to stay dashed when the reading is
missing, so a card that can no longer read *anything* looks exactly like one
whose server has nothing to report -- and a boot that never asks looks like a
read still in flight. The JS file drives the real attemptMemoryMetrics and
bootConnect out of index.html against stubbed responses, so all of those
directions are checked without a browser.

Skipped when node isn't on PATH -- the Python suite is the one that has to
run everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("database_stats_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_database_stats_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
