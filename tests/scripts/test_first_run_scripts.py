"""The Windows first-run scripts, read as text (no PowerShell needed).

`_bootstrap-core.ps1` took a TCP connect to 7687 as "Neo4j ready", but a
fresh container's port forwarder accepts TCP ~20 s before Bolt answers.
`install.ps1` runs under `irm ... | iex`, in the user's own session, where
`exit` closes the window.
"""

import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _code(name):
    """The script without its comments and help block."""
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_bootstrap_waits_for_bolt_not_for_tcp():
    code = _code("_bootstrap-core.ps1")
    assert "TcpClient" not in code
    assert re.search(r"devgraph\.cli\.main doctor --wait \d+", code)


def test_install_never_exits_the_callers_session():
    code = _code("install.ps1")
    assert not re.search(r"\bexit\b", code)
    assert "& {" in code
