"""A repository is registered once, however its path is written."""

from types import SimpleNamespace

import pytest

from devgraph import paths
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def registry(tmp_path):
    reg = RepoRegistry(tmp_path / "registry.sqlite3")
    yield reg
    reg.close()


def _git_dir(tmp_path, name):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True, exist_ok=True)  # one folder on Windows
    return root


def test_the_same_path_is_not_registered_twice(registry, tmp_path):
    root = _git_dir(tmp_path, "shop")
    record = registry.add_repo(root)
    with pytest.raises(ValueError, match=f"already registered as '{record.repo_id}'"):
        registry.add_repo(root)


def test_a_path_in_another_case_is_not_registered_twice_on_macos(
    registry, tmp_path, monkeypatch
):
    """Two folders stand in for one: on macOS's case-insensitive file system
    'Shop' and 'shop' are the same directory, which the fake samefile says."""
    record = registry.add_repo(_git_dir(tmp_path, "Shop"))
    other_case = _git_dir(tmp_path, "shop")
    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        paths.os.path, "samefile", lambda a, b: str(a).casefold() == str(b).casefold()
    )
    with pytest.raises(ValueError, match=f"already registered as '{record.repo_id}'"):
        registry.add_repo(other_case)
