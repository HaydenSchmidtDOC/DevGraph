"""Tests for the self-update git target."""

import pytest

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
