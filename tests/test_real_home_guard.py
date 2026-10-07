"""The suite's guard against touching the user's real DevGraph home."""

import os
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import _REAL_DEVGRAPH_HOME


def test_tests_run_against_a_temp_home_not_the_real_one():
    from devgraph.config.settings import devgraph_home, get_settings

    real = Path(_REAL_DEVGRAPH_HOME)
    assert os.path.normcase(os.path.realpath(Path.home() / ".devgraph")) != _REAL_DEVGRAPH_HOME
    assert real not in get_settings().registry_db_path.parents
    assert os.path.normcase(os.path.realpath(devgraph_home())) != _REAL_DEVGRAPH_HOME


def test_opening_a_path_under_the_real_home_is_refused(real_home_hits):
    target = Path(_REAL_DEVGRAPH_HOME) / "guard-probe-never-created"
    seen = len(real_home_hits)
    try:
        with pytest.raises(PermissionError):
            open(target, "rb")
        with pytest.raises(PermissionError):
            sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        assert len(real_home_hits) == seen + 2
    finally:
        del real_home_hits[seen:]
