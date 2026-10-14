"""Temporary git repositories and recorders for the compare tests (not a test module itself)."""

import subprocess
from pathlib import Path

import git as git_pkg


def git(repo, *args):
    """Run git in `repo` with a fixed fictional identity, no signing and no line-ending
    conversion (a Windows runner's core.autocrlf would rewrite committed CRLF); stdout, stripped."""
    return subprocess.run(
        ["git", "-c", "user.email=dev@example.com", "-c", "user.name=Dev Example", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_files(repo, files: dict[str, str | bytes | None], message):
    """Write (or, for `None`, delete) each file, commit everything, and return the new SHA."""
    for rel, content in files.items():
        path = Path(repo, rel)
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bytes as given: text mode would write CRLF on Windows.
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def two_branch_repo(tmp_path, base, branch, main_after=None, branch_name="feature"):
    """A repository whose `main` holds `base`, with `branch_name` adding `branch` on top
    and, when given, `main` moving on with `main_after`. `main` is checked out at the end."""
    repo = Path(tmp_path, "repo")
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_files(repo, base, "base")
    git(repo, "checkout", "-q", "-b", branch_name)
    commit_files(repo, branch, "branch")
    git(repo, "checkout", "-q", "main")
    if main_after is not None:
        commit_files(repo, main_after, "main after")
    return repo


class GitCalls(list):
    """Each recorded `Git.execute` command list; `kwargs` holds the matching keyword arguments."""

    def __init__(self):
        super().__init__()
        self.kwargs = []


def record_git_commands(monkeypatch):
    """Record every `Git.execute` call's command list (and its kwargs in `.kwargs`)."""
    calls = GitCalls()
    real = git_pkg.cmd.Git.execute

    def execute(self, command, *args, **kwargs):
        calls.append(list(command))
        calls.kwargs.append(kwargs)
        return real(self, command, *args, **kwargs)

    monkeypatch.setattr(git_pkg.cmd.Git, "execute", execute)
    return calls


def record_object_reads(monkeypatch):
    """Record the hex SHAs passed to `GitCmdObjectDB.stream` and `.info`: (streamed, infoed)."""
    streamed, infoed = [], []
    real_stream = git_pkg.db.GitCmdObjectDB.stream
    real_info = git_pkg.db.GitCmdObjectDB.info

    def stream(self, binsha):
        streamed.append(binsha.hex())
        return real_stream(self, binsha)

    def info(self, binsha):
        infoed.append(binsha.hex())
        return real_info(self, binsha)

    monkeypatch.setattr(git_pkg.db.GitCmdObjectDB, "stream", stream)
    monkeypatch.setattr(git_pkg.db.GitCmdObjectDB, "info", info)
    return streamed, infoed
