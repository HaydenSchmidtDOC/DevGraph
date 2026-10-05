"""Repository-controlled config files are read only when they are regular, contained and small.

A committed symlink to /dev/zero must not exhaust memory, a FIFO must not block
the read, and an oversize regular file must be refused rather than loaded.
"""

import os
import signal
from contextlib import contextmanager

import pytest

from devgraph import paths
from devgraph.config import global_tools, project_schema, project_tools
from devgraph.config.project_schema import SCHEMA_FILENAME, ProjectSchemaError, load_project_schema, schema_file_hash
from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError, load_project_tools
from devgraph.mcp.tool_plane import tools_fingerprint

CAP = 64


@pytest.fixture(autouse=True)
def small_cap(monkeypatch):
    monkeypatch.setattr(paths, "MAX_CONFIG_BYTES", CAP)


class Blocked(BaseException):
    """Not an OSError, so the code under test can't swallow it."""


@contextmanager
def deadline(seconds=3):
    """Fail instead of hanging if the code under test blocks."""
    def expire(signum, frame):
        raise Blocked("the read blocked")
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def make(repo, name, kind):
    path = repo / name
    if kind == "zero":
        path.symlink_to("/dev/zero")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"#" * (CAP + 1))
    return path


KINDS = ["zero", "fifo", "oversize"]
REFUSED = "not a regular file|larger than|inside the repository"


@pytest.mark.parametrize("kind", KINDS)
def test_schema_file_hash_refuses_without_reading(tmp_path, kind):
    make(tmp_path, SCHEMA_FILENAME, kind)
    with deadline():
        value = schema_file_hash(tmp_path)
    assert value.startswith("unreadable:")


def test_schema_file_hash_refuses_a_file_outside_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.yaml").write_text("version: 1\n")
    (repo / SCHEMA_FILENAME).symlink_to(tmp_path / "outside.yaml")
    assert schema_file_hash(repo) == "unreadable:outside_repository"


def test_schema_file_hash_still_hashes_a_small_file(tmp_path):
    (tmp_path / SCHEMA_FILENAME).write_text("version: 1\n")
    assert schema_file_hash(tmp_path).startswith("sha256:")


@pytest.mark.parametrize("kind", KINDS)
def test_load_project_schema_refuses(tmp_path, kind):
    make(tmp_path, SCHEMA_FILENAME, kind)
    with deadline(), pytest.raises(ProjectSchemaError, match=REFUSED):
        load_project_schema(tmp_path)


@pytest.mark.parametrize("kind", KINDS)
def test_load_project_tools_refuses(tmp_path, kind):
    make(tmp_path, TOOLS_FILENAME, kind)
    with deadline(), pytest.raises(ProjectToolsError, match=REFUSED):
        load_project_tools(tmp_path)


@pytest.mark.parametrize("kind", KINDS)
def test_tools_fingerprint_refuses(tmp_path, kind):
    make(tmp_path, TOOLS_FILENAME, kind)
    with deadline():
        value = tools_fingerprint(tmp_path)
    assert isinstance(value, str) and value.startswith("unreadable:")


@pytest.mark.parametrize("kind", KINDS)
def test_global_store_refuses(tmp_path, kind):
    path = make(tmp_path, global_tools.GLOBAL_TOOLS_FILENAME, kind)
    with deadline():
        value = global_tools.global_tools_fingerprint(path)
        assert isinstance(value, str) and value.startswith("unreadable:")
        with pytest.raises(ProjectToolsError, match=REFUSED):
            global_tools.load_global_tools(path)
