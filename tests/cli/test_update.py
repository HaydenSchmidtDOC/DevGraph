"""Tests for `devgraph update`: the git target, the reinstall command, and the tray restart."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("origin/master", ["git", "pull", "--ff-only", "origin", "master"]),
        (
            "upstream/release/next",
            ["git", "pull", "--ff-only", "upstream", "release/next"],
        ),
    ],
)
def test_git_pull_command_separates_remote_from_branch(value, expected):
    assert cli_main._git_pull_command(value) == expected


@pytest.mark.parametrize("value", ["master", "/master", "origin/"])
def test_git_pull_command_rejects_incomplete_value(value):
    with pytest.raises(ValueError, match="REMOTE/BRANCH"):
        cli_main._git_pull_command(value)


# --- reinstall and tray restart ---------------------------------------------------
#
# `devgraph update` ran `python -m pip install -e .[dev]`, which fails in a
# uv-created environment (it has no pip), and then exited without restarting
# the tray it had just stopped.


def _venv(tmp_path, *, uv):
    venv = tmp_path / "venv"
    venv.mkdir()
    lines = ["home = /usr/bin", "implementation = CPython"] + (["uv = 0.11.0"] if uv else []) + ["version_info = 3.13.1"]
    (venv / "pyvenv.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return venv


def test_a_uv_environment_reinstalls_with_uv_sync(tmp_path, monkeypatch):
    venv = _venv(tmp_path, uv=True)
    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    assert cli_main._reinstall_command(venv, Path("/venv/bin/python")) == ["/usr/bin/uv", "sync", "--extra", "dev"]


def test_a_pip_environment_reinstalls_with_pip(tmp_path, monkeypatch):
    venv = _venv(tmp_path, uv=False)
    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    assert cli_main._reinstall_command(venv, Path("/venv/bin/python")) == [
        str(Path("/venv/bin/python")), "-m", "pip", "install", "-e", ".[dev]",
    ]


def test_a_uv_environment_without_uv_on_path_falls_back_to_pip(tmp_path, monkeypatch):
    venv = _venv(tmp_path, uv=True)
    monkeypatch.setattr(cli_main.shutil, "which", lambda name: None)
    assert cli_main._reinstall_command(venv, Path("/venv/bin/python"))[1:3] == ["-m", "pip"]


class _Recorder:
    """Stands in for subprocess.run: records each command, fails the ones named."""

    def __init__(self, fail=(), porcelain=""):
        self.commands, self.fail, self.porcelain = [], set(fail), porcelain

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        joined = " ".join(map(str, cmd))
        if "status --porcelain" in joined:
            return SimpleNamespace(returncode=0, stdout=self.porcelain, stderr="")
        failed = any(word in joined for word in self.fail)
        return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="boom" if failed else "")

    def ran(self, *words):
        return [c for c in self.commands if all(w in " ".join(map(str, c)) for w in words)]


@pytest.fixture
def update_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli_main, "resolve_venv_python", lambda: Path("/venv/bin/python"))
    monkeypatch.setattr(cli_main, "get_settings", lambda: SimpleNamespace(registry_db_path=tmp_path / "r.sqlite3"))
    monkeypatch.setattr(cli_main, "_tray_liveness_text", lambda settings: "running")
    monkeypatch.setattr(cli_main, "_reinstall_command", lambda venv, python: ["/usr/bin/uv", "sync", "--extra", "dev"])


@pytest.mark.parametrize("failing", ["uv sync", "doctor"])
def test_update_restarts_the_tray_it_stopped_even_when_a_step_fails(update_env, monkeypatch, failing):
    run = _Recorder(fail=[failing])
    monkeypatch.setattr(cli_main.subprocess, "run", run)
    result = CliRunner().invoke(cli_main.app, ["update"])
    assert result.exit_code == 1
    assert run.ran("tray", "stop") and run.ran("tray", "start")
    stop_at = run.commands.index(run.ran("tray", "stop")[0])
    start_at = run.commands.index(run.ran("tray", "start")[0])
    assert stop_at < start_at
    assert "Restarting tray app" in result.stdout


def test_update_runs_the_reinstall_command(update_env, monkeypatch):
    run = _Recorder()
    monkeypatch.setattr(cli_main.subprocess, "run", run)
    result = CliRunner().invoke(cli_main.app, ["update"])
    assert result.exit_code == 0, result.stdout
    assert run.ran("uv", "sync") and not run.ran("pip", "install")
    assert run.ran("tray", "start")


def test_an_untracked_uv_lock_does_not_count_as_local_changes(update_env, monkeypatch):
    """uv sync writes uv.lock, which this repository does not track."""
    run = _Recorder(porcelain="?? uv.lock\n")
    monkeypatch.setattr(cli_main.subprocess, "run", run)
    result = CliRunner().invoke(cli_main.app, ["update"])
    assert result.exit_code == 0, result.stdout


def test_other_local_changes_still_block_the_update(update_env, monkeypatch):
    run = _Recorder(porcelain="?? uv.lock\n M devgraph/cli/main.py\n")
    monkeypatch.setattr(cli_main.subprocess, "run", run)
    result = CliRunner().invoke(cli_main.app, ["update"])
    assert result.exit_code == 1 and "Local changes present" in result.stdout
