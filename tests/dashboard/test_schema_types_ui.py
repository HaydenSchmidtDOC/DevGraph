"""Runs the dashboard's schema-driven type-list checks (schema_types_ui.js) as
part of the normal suite.

tests/dashboard/test_routes.py covers `/api/repos/{repo_id}/schema` on the
server; this covers the browser side, where that payload becomes the Entities
rows, the Relationships chips, their colours and the isolate queries. The JS
file drives the real functions out of index.html against stubbed responses, so
it runs without a browser.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("schema_types_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_schema_types_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
