"""The session-end sweep (tests/conftest.py) only ever considers test repository ids."""

from tests.conftest import _test_repo_ids


def test_only_test_prefixed_ids_are_collected():
    params = {
        "repo_id": "zz-scan-1234",
        "repo_ids": ["_smoketest_fs_provider", "my-real-project", 3],
        "name": "test_repo_a",
        "path": "/home/someone/zz-not-an-id",
        "other": "devgraph",
    }
    assert _test_repo_ids(params) == {"zz-scan-1234", "_smoketest_fs_provider", "test_repo_a"}


def test_no_parameters_collect_nothing():
    assert _test_repo_ids(None) == set()
    assert _test_repo_ids({"repo_id": "demo", "r": "my-app"}) == set()
