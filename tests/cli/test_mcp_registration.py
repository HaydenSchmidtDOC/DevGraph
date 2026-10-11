"""Claude Code registration is user-scoped everywhere, against a stub `claude`.

`devgraph mcp add` and `client-config --run` used to run a bare `claude mcp
add`, which registers in *local* scope: for the DevGraph checkout folder only,
so Claude Code sessions in every other repository had no DevGraph. `mcp
doctor` then ran `claude mcp get` from wherever it was invoked and reported
"not registered" for the registration `mcp add` had just made. Now every
path uses `-s user`, `mcp doctor` and `mcp add` read the same registration,
and a leftover local-scope registration is detected and explained.

The stub is a real executable on PATH that logs each call (arguments and
working directory) and answers `mcp get` from a file the test writes.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the stub claude is a POSIX shell script")

_USER_GET = """devgraph:
  Scope: User config (available in all your projects)
  Status: ✓ Connected
  Type: stdio
  Command: /venv/bin/python
  Args: -P -m devgraph.mcp.server

To remove this server, run: claude mcp remove "devgraph" -s user
"""
_LOCAL_GET = _USER_GET.replace("User config (available in all your projects)", "Local config (private to you in this project)").replace("-s user", "-s local")


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A stub `claude` on PATH. `claude.registered(text)` makes `mcp get` answer
    with `text` (None: not registered); `claude.calls()` lists [cwd, *args]."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude-calls.jsonl"
    get_reply = tmp_path / "claude-get.txt"
    script = bin_dir / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps([os.getcwd(), *sys.argv[1:]]) + '\\n')\n"
        "if sys.argv[1:3] == ['mcp', 'get']:\n"
        f"    if not os.path.exists({str(get_reply)!r}):\n"
        "        print('No MCP server found with name: devgraph', file=sys.stderr); sys.exit(1)\n"
        f"    print(open({str(get_reply)!r}).read()); sys.exit(0)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    monkeypatch.setattr(cli_main, "resolve_repo_root", lambda: repo_root)
    monkeypatch.setattr(cli_main, "resolve_venv_python", lambda: Path("/venv/bin/python"))

    class Stub:
        root = repo_root

        @staticmethod
        def registered(text):
            if text is None:
                get_reply.unlink(missing_ok=True)
            else:
                get_reply.write_text(text, encoding="utf-8")

        @staticmethod
        def calls():
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return Stub


def _invoke(*args):
    return CliRunner().invoke(cli_main.app, list(args))


def _adds(claude):
    return [c[1:] for c in claude.calls() if c[1:3] == ["mcp", "add"]]


def test_mcp_add_registers_in_user_scope(claude):
    result = _invoke("mcp", "add")
    assert result.exit_code == 0, result.stdout
    (add,) = _adds(claude)
    assert add[:5] == ["mcp", "add", "-s", "user", "devgraph"]
    assert add[add.index("--") + 1:] == ["/venv/bin/python", "-P", "-m", "devgraph.mcp.server"]


def test_client_config_run_registers_in_user_scope(claude):
    result = _invoke("client-config", "--target", "claude", "--run")
    assert result.exit_code == 0, result.stdout
    (add,) = _adds(claude)
    assert add[2:4] == ["-s", "user"]


def test_the_printed_one_liner_is_user_scoped(claude):
    result = _invoke("client-config", "--claude-mcp-add-only")
    collapsed = " ".join(line.strip() for line in result.stdout.strip().splitlines())
    assert collapsed.startswith("claude mcp add -s user devgraph -- ")


def test_mcp_add_skips_an_existing_user_registration(claude):
    claude.registered(_USER_GET)
    result = _invoke("mcp", "add")
    assert result.exit_code == 0, result.stdout
    assert "already registered" in result.stdout
    assert _adds(claude) == []


def test_mcp_add_explains_how_to_move_a_local_registration(claude):
    claude.registered(_LOCAL_GET)
    result = _invoke("mcp", "add")
    assert result.exit_code == 1
    out = " ".join(result.stdout.split())
    assert "local scope" in out
    assert "claude mcp remove devgraph -s local" in out and "devgraph mcp add" in out
    assert str(claude.root) in out
    assert _adds(claude) == []


def test_mcp_doctor_agrees_with_mcp_add_from_any_directory(claude, tmp_path, monkeypatch):
    """doctor runs `mcp get` from the checkout, like add, not from wherever it was started."""
    _invoke("mcp", "add")
    claude.registered(_USER_GET)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = _invoke("mcp", "doctor")
    assert "registered (user scope)" in result.stdout, result.stdout
    gets = [c for c in claude.calls() if c[1:3] == ["mcp", "get"]]
    assert {c[0] for c in gets} == {str(claude.root)}


def test_mcp_doctor_flags_a_local_registration(claude):
    claude.registered(_LOCAL_GET)
    result = _invoke("mcp", "doctor")
    assert result.exit_code == 1
    out = " ".join(result.stdout.split())
    assert "local scope" in out and "claude mcp remove devgraph -s local" in out


def test_mcp_doctor_reports_not_registered(claude):
    result = _invoke("mcp", "doctor")
    assert result.exit_code == 1
    assert "not registered" in result.stdout and "devgraph mcp add" in result.stdout


def test_mcp_remove_removes_the_user_registration(claude):
    claude.registered(_USER_GET)
    result = _invoke("mcp", "remove")
    assert result.exit_code == 0, result.stdout
    removes = [c[1:] for c in claude.calls() if c[1:3] == ["mcp", "remove"]]
    assert removes == [["mcp", "remove", "-s", "user", "devgraph"]]


def test_a_hanging_mcp_get_is_bounded_and_reads_as_unknown(claude, monkeypatch):
    """`claude mcp get` health-checks the server; a hang must not hang devgraph."""
    import subprocess

    seen = {}

    def hang(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)

    monkeypatch.setattr(cli_main.subprocess, "run", hang)
    scope, output = cli_main._claude_mcp_registration("/usr/bin/claude", claude.root)
    assert scope == "unknown" and seen["timeout"] == 60
