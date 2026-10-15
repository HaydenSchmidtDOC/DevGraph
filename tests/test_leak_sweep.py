"""The session-end sweep (tests/conftest.py) only ever considers test
repository ids unique to their creation."""

from tests.conftest import _test_repo_ids


def test_only_test_prefixed_ids_with_a_random_part_are_collected():
    params = {
        "repo_id": "zz-scan-plans-1a2b3c4d",
        "repo_ids": ["_smoketest_docs_provider_d1f35b79", "zz-watcher-live-1790000000-a1b2c3", "my-real-project", 3],
        "fresh": "zz-accuracy-0f1e2d3c_fresh",
        "path": "/home/someone/zz-1a2b3c4d",
    }
    assert _test_repo_ids(params) == {
        "zz-scan-plans-1a2b3c4d", "_smoketest_docs_provider_d1f35b79",
        "zz-watcher-live-1790000000-a1b2c3", "zz-accuracy-0f1e2d3c_fresh",
    }


def test_fixed_ids_are_never_collected():
    """Another session may be using one right now."""
    fixed = ["test-repo", "test_repo_a", "dash_repo_b", "_smoketest_fs_provider", "_smoketest_extracted_nodes",
             "zz-compact-repo", "zz-not-a-real-repo-id-zz", "zz-deadbeef", "zz-abc1234"]
    assert _test_repo_ids({f"p{i}": value for i, value in enumerate(fixed)}) == set()


def test_no_parameters_collect_nothing():
    assert _test_repo_ids(None) == set()
    assert _test_repo_ids({"repo_id": "demo", "r": "my-app"}) == set()
