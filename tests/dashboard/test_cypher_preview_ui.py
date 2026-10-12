"""Runs the Cypher console's typing-is-read-only checks (cypher_preview_ui.js)
as part of the normal suite.

The live glow preview fires on every pause in typing, so a write typed into
the box used to execute before Run was pressed. The JS file types a
`DETACH DELETE` keystroke by keystroke through the real input listener and
fails if any request it sends is not a `preview: true` (read-only) request.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("cypher_preview_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cypher_preview_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
