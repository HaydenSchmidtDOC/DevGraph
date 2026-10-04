"""Runs the dashboard's Config page checks (config_page_ui.js) as part of the
normal suite.

tests/dashboard/test_config_routes.py covers `/api/config*` on the server;
this covers the browser side: the Global-then-projects rendering, locks,
badges, the request each edit sends (method, URL, If-Match, JSON body), the
global warning step, the destination dropdown, the dry-run confirm, the
412 reload flow, the whole-file reset dialog (typed name, armed Reset, the dry
run's fingerprint, Re-check after a 412) and the project-config switch. The JS file drives the real functions out of index.html
against stubbed responses, so it runs without a browser.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("config_page_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_config_page_ui():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
