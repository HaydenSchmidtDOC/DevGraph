"""Repo ids the dashboard reserves as scope tokens are never issued to a repository."""

import pytest

from devgraph.registry.store import RESERVED_REPO_IDS, RepoRegistry


@pytest.fixture
def registry(tmp_path):
    reg = RepoRegistry(tmp_path / "registry.sqlite3")
    yield reg
    reg.close()


def _git_dir(tmp_path, name):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


@pytest.mark.parametrize("reserved", ["__global__", "__all__"])
def test_a_reserved_directory_name_gets_a_suffix(registry, tmp_path, reserved):
    record = registry.add_repo(_git_dir(tmp_path, reserved))
    assert record.repo_id == f"{reserved}-2"
    assert registry.get(reserved) is None


@pytest.mark.parametrize("reserved", ["__global__", "__all__", "__GLOBAL__"])
def test_a_reserved_explicit_id_gets_a_suffix(registry, tmp_path, reserved):
    record = registry.add_repo(_git_dir(tmp_path, "repo"), repo_id=reserved)
    assert record.repo_id not in RESERVED_REPO_IDS and record.repo_id.endswith("-2")


def test_reserved_ids_are_the_dashboard_scopes():
    from devgraph.dashboard import config_model, routes

    assert RESERVED_REPO_IDS == {config_model.GLOBAL_SCOPE, routes._ALL_REPOS_SCOPE}
