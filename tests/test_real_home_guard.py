"""The suite's guard against touching the user's real DevGraph home."""

import os
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import _ALLOW_REAL_HOME, _REAL_DEVGRAPH_HOME, _under_real_home

REAL = Path(_REAL_DEVGRAPH_HOME)
# Inside a directory that does not exist, so even a write the guard missed could not create anything.
PROBE = REAL / "guard-probe-missing-dir" / "never-created"

guarded = pytest.mark.skipif(_ALLOW_REAL_HOME, reason="DEVGRAPH_ALLOW_REAL_HOME is set")


def test_a_path_under_the_real_home_is_flagged():
    assert _under_real_home(REAL)
    assert _under_real_home(REAL / "registry.sqlite3")
    assert _under_real_home(str(REAL / "layouts" / "repo-a.json"))
    assert _under_real_home(f"{(REAL / 'registry.sqlite3').as_uri()}?mode=ro")


def test_a_sibling_directory_is_not_flagged():
    assert not _under_real_home(REAL.parent / ".devgraph2" / "registry.sqlite3")


def test_a_devgraph_name_elsewhere_is_not_flagged(tmp_path):
    assert not _under_real_home(tmp_path / ".devgraph" / "registry.sqlite3")
    assert not _under_real_home(tmp_path / "notes.devgraph" / "x")
    assert not _under_real_home(3)  # a file descriptor


@guarded
def test_tests_run_against_a_temp_home_not_the_real_one():
    from devgraph.config.settings import devgraph_home, get_settings

    assert os.path.normcase(os.path.realpath(Path.home() / ".devgraph")) != _REAL_DEVGRAPH_HOME
    assert REAL not in get_settings().registry_db_path.parents
    assert os.path.normcase(os.path.realpath(devgraph_home())) != _REAL_DEVGRAPH_HOME


@guarded
@pytest.mark.parametrize(
    "touch",
    [
        lambda: open(PROBE, "rb"),
        lambda: sqlite3.connect(f"{PROBE.as_uri()}?mode=ro", uri=True),
        lambda: os.open(PROBE, os.O_WRONLY | os.O_CREAT),
        lambda: PROBE.write_text("x", encoding="utf-8"),
    ],
    ids=["open-read", "sqlite-ro", "os-open-write", "write-text"],
)
def test_touching_the_real_home_is_refused(real_home_hits, touch):
    seen = len(real_home_hits)
    try:
        with pytest.raises(PermissionError):
            touch()
        assert len(real_home_hits) == seen + 1
    finally:
        del real_home_hits[seen:]
