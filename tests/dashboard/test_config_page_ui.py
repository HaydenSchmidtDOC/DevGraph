"""Runs the dashboard's Config page checks (config_page_ui.js) as part of the
normal suite.

tests/dashboard/test_config_routes.py covers `/api/config*` on the server;
this covers the browser side: the Global-then-projects rendering, locks,
badges, the request each edit sends (method, URL, If-Match, JSON body), the
global warning step, the destination dropdown, the dry-run confirm, the
412 reload flow, the whole-file reset dialog (typed name, armed Reset, the dry
run's fingerprint, Re-check after a 412), the project-config switch and
Copy to… (which rows offer it, destinations without the source, the read-only
dialog, add vs confirmed replace with the dry run's If-Match, the full reload
after every write) and the form view for tools, node types and relationships
(which entries open in the form and which
stay YAML with the reason, delete/copy never showing it; a form edit writing
the serialised YAML into the textarea, dropping a confirm and going through
the same dry run; untouched saves sending the server's text byte for byte;
the form locked while busy; Discard after a hand edit; labels, legends, named
row buttons and focus; selects and typed text never rewriting a value; the
relationship provider showing the custom name in place). The
JS file drives the real functions out of index.html
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
