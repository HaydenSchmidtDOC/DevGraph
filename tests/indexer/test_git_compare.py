"""`compare.open_comparison`: refs, merge base and the changed-file list, on real temporary repositories."""

import gc
import os
import re
import sys
from pathlib import Path

import git as git_pkg
import pytest
from gitdb.exc import BadObject

from devgraph.indexer import symbols
from devgraph.indexer.dispatch import _CODE_ROUTES
from devgraph.indexer.git_history import compare
from devgraph.indexer.git_history.compare import CompareError, open_comparison, symbol_detail
from tests.indexer.git_compare_helpers import (
    commit_files,
    git,
    record_git_commands,
    record_object_reads,
    two_branch_repo,
)

REPO_ID = "demo"
SHALLOW = "; this is a shallow clone, so older commits may be missing: git fetch --unshallow"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks, exec bits and kill_after_timeout are POSIX")


def unknown(arg, ref):
    return (
        f"{arg} {ref!r} is not a branch, tag or commit in repository {REPO_ID!r}; "
        "refs must exist locally (DevGraph never fetches)"
    )


def changes_of(repo, base="main", head="feature", **kwargs):
    with open_comparison(repo, REPO_ID, base, head, **kwargs) as cmp:
        return [(c.path, c.status) for c in cmp.changes], cmp


def subcommand(command):
    return next(part for part in command[1:] if not part.startswith("-"))


def test_added_removed_modified_and_nested_paths(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"a.py": "a = 1\n", "pkg/b.py": "b = 1\n", "pkg/sub/c.txt": "c\n", "d.md": "# d\n"},
        {"pkg/b.py": "b = 2\n", "d.md": None, "pkg/sub/new.go": "package sub\n", "e/f/g.rs": "fn g() {}\n"},
    )
    changes, cmp = changes_of(repo)
    assert changes == [
        ("d.md", "removed"),
        ("e/f/g.rs", "added"),
        ("pkg/b.py", "modified"),
        ("pkg/sub/new.go", "added"),
    ]
    assert cmp.merge_base.hexsha == git(repo, "rev-parse", "main")
    assert cmp.base_commit.hexsha == git(repo, "rev-parse", "main")
    assert cmp.head_commit.hexsha == git(repo, "rev-parse", "feature")
    assert cmp.truncated_reasons == []
    modified = next(c for c in cmp.changes if c.status == "modified")
    assert modified.kind == "blob" and modified.base_blob is not None and modified.head_blob is not None
    assert next(c for c in cmp.changes if c.status == "added").base_blob is None
    assert next(c for c in cmp.changes if c.status == "removed").head_blob is None


def test_changes_on_base_after_branch_point_are_not_reported(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"a.py": "a = 1\n", "b.py": "b = 1\n"},
        {"b.py": "b = 2\n"},
        main_after={"a.py": "a = 2\n", "only_main.py": "x = 1\n"},
    )
    changes, _ = changes_of(repo)
    assert changes == [("b.py", "modified")]


def test_merged_head_is_empty(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    changes, cmp = changes_of(repo, base="feature", head="main")
    assert changes == []
    assert cmp.merge_base.hexsha == git(repo, "rev-parse", "feature")
    changes, _ = changes_of(repo, base="feature", head="feature")
    assert changes == []


def test_refs_of_every_shape_resolve(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    commit_files(repo, {"c.py": "c = 1\n"}, "second on main")
    git(repo, "tag", "v1.0", "main")
    git(repo, "branch", "fix#123", "feature")
    full = git(repo, "rev-parse", "feature")
    shapes = {
        "v1.0": git(repo, "rev-parse", "v1.0"),
        full: full,
        full[:7]: full,
        "main~1": git(repo, "rev-parse", "main~1"),
        "HEAD": git(repo, "rev-parse", "HEAD"),
        "refs/heads/feature": full,
        "fix#123": full,
    }
    for ref, sha in shapes.items():
        with open_comparison(repo, REPO_ID, "main", ref) as cmp:
            assert cmp.head_commit.hexsha == sha, ref
            assert cmp.head_ref == ref


def test_exact_rename_and_edited_rename(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"x/old.py": "def kept():\n    return 1\n", "m.py": "m = 1\n", "e1.txt": "", "e2.txt": ""},
        {
            "x/old.py": None,
            "y/new.py": "def kept():\n    return 1\n",
            "m.py": None,
            "n.py": "m = 2\n",
            "e1.txt": None,
            "e2.txt": None,
            "e3.txt": "",
            "e4.txt": "",
        },
    )
    changes, cmp = changes_of(repo)
    assert changes == [
        ("e1.txt", "removed"),
        ("e2.txt", "removed"),
        ("e3.txt", "added"),
        ("e4.txt", "added"),
        ("m.py", "removed"),
        ("n.py", "added"),
        ("y/new.py", "renamed"),
    ]
    renamed = cmp.changes[-1]
    assert renamed.old_path == "x/old.py"
    assert renamed.base_blob.hexsha == renamed.head_blob.hexsha
    assert all(c.old_path is None for c in cmp.changes[:-1])


def test_rename_pairing_is_in_sorted_path_order(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"a1.py": "same\n", "a2.py": "same\n", "a3.py": "same\n"},
        {"a1.py": None, "a2.py": None, "a3.py": None, "b1.py": "same\n", "b2.py": "same\n"},
    )
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        got = [(c.path, c.status, c.old_path) for c in cmp.changes]
    assert got == [("a3.py", "removed", None), ("b1.py", "renamed", "a1.py"), ("b2.py", "renamed", "a2.py")]


def test_tree_blob_swap(tmp_path):
    repo = two_branch_repo(tmp_path, {"thing": "a file\n"}, {"thing": None, "thing/inner.py": "x = 1\n"})
    changes, _ = changes_of(repo)
    assert changes == [("thing", "removed"), ("thing/inner.py", "added")]
    changes, _ = changes_of(repo, base="feature", head="main")
    assert changes == []  # main is the merge base: nothing on the head side
    git(repo, "checkout", "-q", "-b", "back", "feature")
    Path(repo, "thing", "inner.py").unlink()
    Path(repo, "thing").rmdir()
    commit_files(repo, {"thing": "a file again\n"}, "file back")
    changes, _ = changes_of(repo, base="feature", head="back")
    assert changes == [("thing", "added"), ("thing/inner.py", "removed")]


@posix_only
def test_mode_only_change_is_not_reported(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "checkout", "-q", "feature")
    Path(repo, "a.py").chmod(0o755)  # the working file too, so checking out main again is clean
    git(repo, "update-index", "--chmod=+x", "a.py")
    git(repo, "commit", "-q", "-m", "exec bit")
    git(repo, "checkout", "-q", "main")
    assert git(repo, "diff", "--name-only", "main", "feature").splitlines() == ["a.py", "b.py"]
    changes, _ = changes_of(repo)
    assert changes == [("b.py", "added")]


@posix_only
def test_symlink_and_submodule_are_listed_not_opened(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    gitlink = "1234567890abcdef1234567890abcdef12345678"
    git(repo, "checkout", "-q", "feature")
    os.symlink("a.py", Path(repo, "link"))
    git(repo, "add", "link")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},vendor/sub")
    git(repo, "commit", "-q", "-m", "link and submodule")
    git(repo, "checkout", "-q", "main")
    link_blob = git(repo, "rev-parse", "feature:link")
    streamed, infoed = record_object_reads(monkeypatch)
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        got = {c.path: (c.status, c.kind) for c in cmp.changes}
    assert got == {"b.py": ("added", "blob"), "link": ("added", "symlink"), "vendor/sub": ("added", "submodule")}
    assert gitlink not in streamed and gitlink not in infoed
    assert link_blob not in streamed


def test_only_cat_file_and_merge_base_run(tmp_path, monkeypatch):
    repo = two_branch_repo(
        tmp_path,
        {"a.py": "def a():\n    return 1\n", "d/x.py": "x\n"},
        {"a.py": "def a():\n    return 2\n", "d/y.py": "y\n"},
    )
    calls = record_git_commands(monkeypatch)
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        detailed = symbol_detail(cmp)
    assert [(c.path, c.status) for c in detailed] == [("a.py", "modified"), ("d/y.py", "added")]
    assert [e["name"] for e in detailed[0].symbols["changed"]] == ["a"]
    assert {subcommand(c) for c in calls} <= {"cat-file", "merge-base"}
    # A regex ref would make GitPython run an untimed `git rev-parse`; it is refused before any git runs.
    before = len(calls)
    for ref in ("HEAD^{/feat}", "feature^{/(}"):
        with pytest.raises(CompareError, match="regex forms"):
            changes_of(repo, head=ref)
    assert calls[before:] == []
    assert "rev-parse" not in {subcommand(c) for c in calls}
    merge_bases = [c for c in calls if subcommand(c) == "merge-base"]
    assert len(merge_bases) == 1
    assert merge_bases[0][-3] == "merge-base"
    assert all(re.fullmatch("[0-9a-f]{40}", sha) for sha in merge_bases[0][-2:])


INVALID = [
    ("-h", "it starts with '-'"),
    ("--output=x", "it starts with '-'"),
    ("a..b", "it contains '..'"),
    ("HEAD:secret.txt", "it contains ':'"),
    ("../../x", "it contains '..'"),
    ("", "it is empty or longer than 256 characters"),
    ("a" * 300, "it is empty or longer than 256 characters"),
    ("ma in", "it contains whitespace or a control character"),
    ("a\tb", "it contains whitespace or a control character"),
    ("a\x07b", "it contains whitespace or a control character"),
    ("x*", "it contains one of * ? [ \\"),
    ("x?", "it contains one of * ? [ \\"),
    ("x[1]", "it contains one of * ? [ \\"),
    ("a\\b", "it contains one of * ? [ \\"),
    ("/abs", "it starts with '/'"),
]
REFLOG = "reflog and upstream forms like @{...} aren't supported; pass a branch name, tag or commit SHA"
REGEX = "regex forms like ^{/...} aren't supported; pass a branch name, tag or commit SHA"
REGEX_FORMS = ["HEAD^{/x}", "HEAD^{/(}", "main^{/feat}", ":/feat"]
REFLOG_FORMS = ["HEAD@{1}", "@{-9}", "@{-1}", "@{upstream}", "feature@{u}", "main@{yesterday}", "HEAD@{}", "HEAD@{99}"]


@pytest.mark.parametrize(
    ("ref", "rule"),
    INVALID
    + [(form, REFLOG) for form in REFLOG_FORMS]
    + [(form, REGEX if form.startswith("H") or form.startswith("m") else "it contains ':'") for form in REGEX_FORMS],
    ids=lambda v: repr(v)[:20],
)
def test_invalid_refs_are_rejected_before_git(tmp_path, monkeypatch, ref, rule):
    monkeypatch.setattr(compare.git, "Repo", lambda *a, **k: pytest.fail("the repository was opened"))
    with pytest.raises(CompareError) as err:
        with open_comparison(tmp_path, REPO_ID, ref, "main"):
            pass
    assert type(err.value) is CompareError
    assert str(err.value) == f"branch_a {ref[:100]!r} is not a valid ref: {rule}"


def test_valid_unusual_names_pass_validation():
    for ref in ("fix#123", "feature/ünïcode", "v1.2+build", "a.b", "x@y", "x{1}"):
        compare.validate_ref("branch_a", ref)


def test_unknown_ref(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="nosuch")
    assert str(err.value) == unknown("branch_b", "nosuch")
    with pytest.raises(CompareError) as err:
        changes_of(repo, base="nosuch")
    assert str(err.value) == unknown("branch_a", "nosuch")


@pytest.mark.parametrize("ref", ["HEAD^{tree}", "HEAD~99", "deadbeef", "refs/heads/nosuch"])
def test_unresolvable_refs_are_unknown(tmp_path, ref):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    with pytest.raises(CompareError) as err:
        changes_of(repo, head=ref)
    assert type(err.value) is CompareError
    assert str(err.value) == unknown("branch_b", ref)


def test_all_zero_sha_is_unknown(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    assert Path(repo, ".git", "logs", "HEAD").read_text().startswith("0" * 40)
    calls = record_git_commands(monkeypatch)
    streamed, infoed = record_object_reads(monkeypatch)
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="logs/HEAD")
    assert str(err.value) == unknown("branch_b", "logs/HEAD")
    assert "merge-base" not in {subcommand(c) for c in calls}
    assert "0" * 40 not in streamed and "0" * 40 not in infoed


def test_missing_subtree_is_object_missing(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n", "d/x.py": "x\n"}, {"d/y.py": "y\n"})
    subtree = git(repo, "rev-parse", "feature:d")
    loose = Path(repo, ".git", "objects", subtree[:2], subtree[2:])
    loose.chmod(0o644)  # git writes loose objects read-only, which Windows refuses to unlink
    loose.unlink()
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert type(err.value) is CompareError
    assert str(err.value) == (
        "git object missing while comparing 'main' and 'feature'; the clone may be partial or shallow"
    )


def _git_version():
    return tuple(int(n) for n in re.findall(r"\d+", git(".", "--version"))[:2])


@posix_only
@pytest.mark.skipif(_git_version() < (2, 44), reason="GIT_NO_LAZY_FETCH needs git 2.44 or later")
@pytest.mark.parametrize("filter_spec", ["blob:none", "tree:0"])
def test_partial_clone_never_lazy_fetches(tmp_path, filter_spec):
    src = two_branch_repo(tmp_path, {"a.py": "def a():\n    return 1\n"}, {"a.py": "def a():\n    return 2\n"})
    git(src, "config", "uploadpack.allowFilter", "true")
    dst = Path(tmp_path, "partial")
    git(tmp_path, "clone", "-q", "--no-checkout", f"--filter={filter_spec}", f"file://{src}", str(dst))
    marker = Path(tmp_path, "ssh-was-called")
    script = Path(tmp_path, "fake-ssh")
    script.write_text(f"#!/bin/sh\necho called >> '{marker}'\nexit 1\n")
    script.chmod(0o755)
    git(dst, "remote", "set-url", "origin", "ssh://git.example.invalid/repo.git")
    git(dst, "config", "core.sshCommand", str(script))
    with pytest.raises(CompareError) as err:
        with open_comparison(dst, REPO_ID, "origin/main", "origin/feature") as cmp:
            symbol_detail(cmp)
    assert str(err.value).startswith("git object missing while comparing 'origin/main' and 'origin/feature'")
    assert not marker.exists()


def _fail_merge_base(monkeypatch, stderr):
    real = git_pkg.cmd.Git.execute

    def execute(self, command, *args, **kwargs):
        if subcommand(command) == "merge-base":
            raise git_pkg.GitCommandError(["git", "merge-base"], -9, stderr=stderr)
        return real(self, command, *args, **kwargs)

    monkeypatch.setattr(git_pkg.cmd.Git, "execute", execute)


def test_merge_base_failures(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    _fail_merge_base(monkeypatch, 'Timeout: the command "git merge-base" did not complete in 1 secs.')
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert str(err.value) == "compare_branches timed out finding the merge base of 'main' and 'feature'"
    _fail_merge_base(monkeypatch, "fatal: something")
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert str(err.value) == "git failed while comparing 'main' and 'feature' in repository 'demo': GitCommandError"


def test_git_missing(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})

    def execute(self, command, *args, **kwargs):
        raise git_pkg.GitCommandNotFound("git", "not found")

    monkeypatch.setattr(git_pkg.cmd.Git, "execute", execute)
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert type(err.value) is CompareError
    assert str(err.value) == (
        "git failed while comparing 'main' and 'feature' in repository 'demo': GitCommandNotFound"
    )


def test_cat_file_failure(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"d/a.py": "a = 1\n"}, {"d/b.py": "b = 1\n"})
    head_tree = git(repo, "rev-parse", "feature^{tree}")
    real_stream = git_pkg.db.GitCmdObjectDB.stream

    def stream(self, binsha):
        if binsha.hex() == head_tree:
            raise OSError("cat-file died")
        return real_stream(self, binsha)

    gc.collect()  # earlier tests' unreachable Repos would otherwise close (via __del__) mid-test
    closed = []
    real_close = git_pkg.Repo.close

    def close(self):
        closed.append(self)
        return real_close(self)

    monkeypatch.setattr(git_pkg.db.GitCmdObjectDB, "stream", stream)
    monkeypatch.setattr(git_pkg.Repo, "close", close)
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert str(err.value) == "git failed while comparing 'main' and 'feature' in repository 'demo': OSError"
    assert len(closed) == 1


def test_repo_is_closed_after_the_block(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    gc.collect()  # earlier tests' unreachable Repos would otherwise close (via __del__) mid-test
    closed = []
    real_close = git_pkg.Repo.close
    monkeypatch.setattr(git_pkg.Repo, "close", lambda self: (closed.append(self), real_close(self))[1])
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        assert cmp.changes[0].head_blob.data_stream.read() == b"b = 1\n"
        assert closed == []
    assert closed == [cmp.repo]


def test_not_a_git_repository(tmp_path):
    message = (
        "repository 'demo' is not a git repository at its registered root; "
        "compare_branches needs the repository's own .git"
    )
    plain = Path(tmp_path, "plain")
    plain.mkdir()
    for path in (plain, Path(tmp_path, "missing")):
        with pytest.raises(CompareError) as err:
            changes_of(path)
        assert str(err.value) == message


def test_ref_naming_a_tree(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "tag", "treetag", git(repo, "rev-parse", "main^{tree}"))
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="treetag")
    assert str(err.value) == unknown("branch_b", "treetag")


def test_shallow_clone_without_base(tmp_path):
    src = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    dst = Path(tmp_path, "shallow")
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "feature", f"file://{src}", str(dst))
    with pytest.raises(CompareError) as err:
        changes_of(dst)
    assert str(err.value) == unknown("branch_a", "main") + SHALLOW


def test_shallow_clone_without_common_history(tmp_path):
    src = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"}, main_after={"c.py": "c = 1\n"})
    dst = Path(tmp_path, "shallow")
    git(tmp_path, "clone", "-q", "--depth", "1", "--no-single-branch", f"file://{src}", str(dst))
    with pytest.raises(CompareError) as err:
        changes_of(dst, base="origin/main", head="origin/feature")
    assert str(err.value) == "'origin/main' and 'origin/feature' share no history in repository 'demo'" + SHALLOW


def test_unrelated_histories(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "checkout", "-q", "--orphan", "island")
    commit_files(repo, {"z.py": "z = 1\n"}, "island")
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="island")
    assert str(err.value) == "'main' and 'island' share no history in repository 'demo'"


def test_diff_entry_cap_and_deadline(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {f"n{i}.py": f"n = {i}\n" for i in range(5)})
    monkeypatch.setattr(compare, "_COMPARE_MAX_DIFF_ENTRIES", 3)
    changes, cmp = changes_of(repo)
    assert len(changes) == 3
    assert cmp.truncated_reasons == ["diff_entries"]
    monkeypatch.undo()

    def clock_from(*values):
        ticks = iter(values)
        last = [0.0]

        def clock():
            last[0] = next(ticks, last[0])
            return last[0]

        return clock

    changes, cmp = changes_of(repo, clock=clock_from(0.0, compare._COMPARE_DEADLINE_S + 1))
    assert changes == []
    assert cmp.truncated_reasons == ["deadline"]

    if sys.platform != "win32":
        calls = record_git_commands(monkeypatch)
        changes, cmp = changes_of(repo, clock=clock_from(0.0, compare._COMPARE_DEADLINE_S - 0.2))
        assert len(changes) == 5 and cmp.truncated_reasons == []
        index = next(i for i, c in enumerate(calls) if subcommand(c) == "merge-base")
        assert calls.kwargs[index]["kill_after_timeout"] == 1


# --- Task 2: symbols at both refs (C3, C5 per-file caps, C6) ---


def detail(repo, base="main", head="feature", clock=None, after_open=None):
    """`symbol_detail` over `open_comparison`: ({path: FileChange}, [paths in listing order], comparison)."""
    kwargs = {} if clock is None else {"clock": clock}
    with open_comparison(repo, REPO_ID, base, head, **kwargs) as cmp:
        if after_open is not None:
            after_open()
        detailed = symbol_detail(cmp)  # the clock and deadline come from the comparison
    return {c.path: c for c in detailed}, [c.path for c in detailed], cmp


def triples(symbols):
    return {name: {(e["kind"], e["container"], e["name"]) for e in entries} for name, entries in symbols.items()}


def line_of(text, needle, nth=0):
    return [i for i, line in enumerate(text.split("\n"), 1) if needle in line][nth]


F, C = "Function", "Class"

PY_BASE = """def keep():
    return 0

def gone():
    return 1

class A:
    def __init__(self):
        self.x = 1

class B:
    def __init__(self):
        self.x = 1
"""
PY_HEAD = """class A:
    def __init__(self):
        self.x = 1

class B:
    def __init__(self):
        self.x = 2

def fresh():
    return 3

def keep():
    return 0
"""
TS_BASE = """export function keep(): number {
  return 0;
}

function gone(): number {
  return 1;
}

class Box {
  size(): number {
    return 1;
  }
}
"""
TS_HEAD = """function fresh(): number {
  return 3;
}

class Box {
  size(): number {
    return 2;
  }
}

export function keep(): number {
  return 0;
}
"""
CS_BASE = """namespace Demo {
  public class Svc {
    public int Keep() {
      return 0;
    }
    public int Run(int a) {
      return a;
    }
    public int Run(string b) {
      return 1;
    }
    public void Gone() {
    }
  }
}
"""
CS_HEAD = """namespace Demo {
  public class Svc {
    public int Run(int a) {
      return a;
    }
    public int Run(string b) {
      return 2;
    }
    public int Keep() {
      return 0;
    }
    public void Fresh() {
    }
  }
}
"""
CPP_BASE = """class Shape {
public:
  int area() {
    return 1;
  }
};

int keep() {
  return 0;
}

int gone() {
  return 1;
}
"""
CPP_HEAD = """int fresh() {
  return 3;
}

class Shape {
public:
  int area() {
    return 2;
  }
};

int keep() {
  return 0;
}
"""
JAVA_BASE = """public class Main {
  static int keep() {
    return 0;
  }
  static int gone() {
    return 1;
  }
  static int edit() {
    return 1;
  }
}
"""
JAVA_HEAD = """public class Main {
  static int edit() {
    return 2;
  }
  static int keep() {
    return 0;
  }
  static int fresh() {
    return 3;
  }
}
"""
RS_BASE = """struct Point { x: i32 }

impl Point {
    fn norm(&self) -> i32 {
        self.x
    }
}

fn keep() -> i32 {
    0
}

fn gone() -> i32 {
    1
}
"""
RS_HEAD = """fn keep() -> i32 {
    0
}

struct Point { x: i32 }

impl Point {
    fn norm(&self) -> i32 {
        self.x * 2
    }
}

fn fresh() -> i32 {
    3
}
"""
KT_BASE = """class App {
    fun run(): Int {
        return 1
    }
}

fun keep(): Int {
    return 0
}

fun gone(): Int {
    return 1
}
"""
KT_HEAD = """fun keep(): Int {
    return 0
}

class App {
    fun run(): Int {
        return 2
    }
}

fun fresh(): Int {
    return 3
}
"""
GO_BASE = """package main

type A struct{}

func (a A) String() string {
\treturn "a"
}

type B struct{}

func (b B) String() string {
\treturn "b"
}

func keep() int {
\treturn 0
}

func gone() int {
\treturn 1
}
"""
GO_HEAD = """package main

func keep() int {
\treturn 0
}

type A struct{}

func (a A) String() string {
\treturn "a"
}

type B struct{}

func (b B) String() string {
\treturn "bb"
}

func fresh() int {
\treturn 3
}
"""

# (route, path, base text, head text, expected triples, (name, nth line match in head) of the one changed overload)
FAMILIES = [
    (
        "py",
        "app.py",
        PY_BASE,
        PY_HEAD,
        {"added": {(F, None, "fresh")}, "removed": {(F, None, "gone")}, "changed": {(C, None, "B"), (F, "B", "__init__")}},
        ("__init__", "def __init__", 1),
    ),
    (
        "js",
        "web/util.ts",
        TS_BASE,
        TS_HEAD,
        {"added": {(F, None, "fresh")}, "removed": {(F, None, "gone")}, "changed": {(C, None, "Box"), (F, "Box", "size")}},
        None,
    ),
    (
        "cs",
        "Svc.cs",
        CS_BASE,
        CS_HEAD,
        {"added": {(F, "Svc", "Fresh")}, "removed": {(F, "Svc", "Gone")}, "changed": {(C, None, "Svc"), (F, "Svc", "Run")}},
        ("Run", "public int Run", 1),
    ),
    (
        "cpp",
        "geo.cpp",
        CPP_BASE,
        CPP_HEAD,
        {"added": {(F, None, "fresh")}, "removed": {(F, None, "gone")}, "changed": {(C, None, "Shape"), (F, "Shape", "area")}},
        None,
    ),
    (
        "java",
        "src/Main.java",
        JAVA_BASE,
        JAVA_HEAD,
        {"added": {(F, "Main", "fresh")}, "removed": {(F, "Main", "gone")}, "changed": {(C, None, "Main"), (F, "Main", "edit")}},
        None,
    ),
    (
        "rs",
        "lib.rs",
        RS_BASE,
        RS_HEAD,
        {"added": {(F, None, "fresh")}, "removed": {(F, None, "gone")}, "changed": {(F, None, "norm")}},
        None,
    ),
    (
        "kt",
        "App.kt",
        KT_BASE,
        KT_HEAD,
        {"added": {(F, None, "fresh")}, "removed": {(F, None, "gone")}, "changed": {(C, None, "App"), (F, "App", "run")}},
        None,
    ),
    (
        "go",
        "main.go",
        GO_BASE,
        GO_HEAD,
        {"added": {(F, None, "fresh")}, "removed": {(F, None, "gone")}, "changed": {(F, None, "String")}},
        ("String", ") String()", 1),
    ),
]


def test_language_families_cover_every_code_route():
    assert {case[0] for case in FAMILIES} == set(_CODE_ROUTES.values())


@pytest.mark.parametrize(("route", "path", "base", "head", "expected", "overload"), FAMILIES, ids=[c[0] for c in FAMILIES])
def test_symbols_per_language_family(tmp_path, route, path, base, head, expected, overload):
    repo = two_branch_repo(tmp_path, {path: base}, {path: head})
    files, _, cmp = detail(repo)
    change = files[path]
    assert change.language == route
    assert change.symbols_skipped is None
    assert triples(change.symbols) == expected
    (gone,) = change.symbols["removed"]
    assert gone["start_line"] == line_of(base, gone["name"] + "(")
    if overload is not None:
        name, needle, nth = overload
        (entry,) = [e for e in change.symbols["changed"] if e["name"] == name]
        assert entry["start_line"] == line_of(head, needle, nth)  # the second same-named symbol, not the first
        assert entry["old_start_line"] == line_of(base, needle, nth)
    assert cmp.truncated_reasons == []


def test_crlf_only_change_is_not_changed(tmp_path):
    text = "def f():\n    return 1\n\nclass K:\n    def m(self):\n        pass\n"
    repo = two_branch_repo(tmp_path, {"app.py": text.encode()}, {"app.py": text.replace("\n", "\r\n").encode()})
    files, _, _ = detail(repo)
    assert files["app.py"].status == "modified"
    assert files["app.py"].symbols == {"added": [], "removed": [], "changed": []}


def test_changed_entry_has_old_lines(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"app.py": "def f():\n    return 1\n"},
        {"app.py": "x = 1\n" * 5 + "def f():\n    return 2\n"},
    )
    files, _, _ = detail(repo)
    assert files["app.py"].symbols["changed"] == [
        {"kind": F, "name": "f", "container": None, "start_line": 6, "end_line": 7, "old_start_line": 1, "old_end_line": 2}
    ]


def test_class_with_changed_method_is_changed(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"app.py": "class K:\n    def m(self):\n        return 1\n"},
        {"app.py": "class K:\n    def m(self):\n        return 2\n"},
    )
    files, _, _ = detail(repo)
    assert triples(files["app.py"].symbols) == {"added": set(), "removed": set(), "changed": {(C, None, "K"), (F, "K", "m")}}


def test_whole_file_added_and_removed(tmp_path):
    src = "def f():\n    pass\n\nclass K:\n    def m(self):\n        pass\n"
    all_three = {(F, None, "f"), (C, None, "K"), (F, "K", "m")}
    repo = two_branch_repo(tmp_path, {"old.py": src}, {"old.py": None, "new.py": src + "\nX = 1\n"})
    files, _, _ = detail(repo)
    assert files["new.py"].status == "added"
    assert triples(files["new.py"].symbols) == {"added": all_three, "removed": set(), "changed": set()}
    assert files["old.py"].status == "removed"
    assert triples(files["old.py"].symbols) == {"added": set(), "removed": all_three, "changed": set()}


@posix_only
def test_unsupported_and_special_files(tmp_path, monkeypatch):
    moved = "def kept():\n    return 1\n"
    repo = two_branch_repo(
        tmp_path,
        {"x/old.py": moved, "a.py": "a = 1\n"},
        {"README.md": "# hi\n", "notes.c": "int main() { return 0; }\n", "x/old.py": None, "y/new.py": moved},
    )
    git(repo, "checkout", "-q", "feature")
    os.symlink("a.py", Path(repo, "link.py"))
    git(repo, "add", "link.py")
    git(repo, "update-index", "--add", "--cacheinfo", "160000,1234567890abcdef1234567890abcdef12345678,vendor/sub")
    git(repo, "commit", "-q", "-m", "link and submodule")
    git(repo, "checkout", "-q", "main")
    moved_blob = git(repo, "rev-parse", "feature:y/new.py")
    streamed, _ = record_object_reads(monkeypatch)
    files, _, _ = detail(repo)
    skipped = {path: (c.symbols, c.symbols_skipped) for path, c in files.items()}
    assert skipped == {
        "README.md": (None, "unsupported_language"),
        "notes.c": (None, "unsupported_language"),
        "link.py": (None, "symlink"),
        "vendor/sub": (None, "submodule"),
        "y/new.py": ({"added": [], "removed": [], "changed": []}, None),
    }
    assert files["README.md"].language is None and files["y/new.py"].language == "py"
    assert moved_blob not in streamed


def test_too_large_and_binary(tmp_path, monkeypatch):
    big = b"x = 1\n" + b"#" * (compare._COMPARE_MAX_FILE_BYTES - 6 + 1)
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"big.py": big, "bin.py": b"x = 1\n\x00\n"})
    big_blob = git(repo, "rev-parse", "feature:big.py")
    streamed, infoed = record_object_reads(monkeypatch)
    files, _, cmp = detail(repo)
    assert (files["big.py"].symbols, files["big.py"].symbols_skipped) == (None, "too_large")
    assert big_blob in infoed and big_blob not in streamed
    assert (files["bin.py"].symbols, files["bin.py"].symbols_skipped) == (None, "binary")
    assert cmp.truncated_reasons == []


def test_parse_error_is_per_file(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"m.go": "package m\n", "p.py": "def f():\n    pass\n"})

    def boom(source, path):
        raise RuntimeError("parser exploded")

    monkeypatch.setitem(symbols.EXTRACTORS, "go", boom)
    files, _, _ = detail(repo)
    assert (files["m.go"].symbols, files["m.go"].symbols_skipped) == (None, "parse_error")
    assert triples(files["p.py"].symbols)["added"] == {(F, None, "f")}


def five_functions(prefix):
    return "".join(f"def {prefix}{i}():\n    pass\n" for i in range(5))


def test_symbol_caps(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"p.py": five_functions("p"), "q.py": five_functions("q")})
    monkeypatch.setattr(compare, "_COMPARE_MAX_SYMBOLS_PER_LIST", 2)
    files, _, cmp = detail(repo)
    assert [e["name"] for e in files["p.py"].symbols["added"]] == ["p0", "p1"]
    assert files["p.py"].symbols_truncated is True
    assert cmp.truncated_reasons == ["symbols"]
    monkeypatch.undo()

    monkeypatch.setattr(compare, "_COMPARE_MAX_SYMBOLS", 3)
    files, _, cmp = detail(repo)
    assert len(files["p.py"].symbols["added"]) == 3 and files["p.py"].symbols_truncated is True
    assert (files["q.py"].symbols, files["q.py"].symbols_skipped) == (None, "limit")
    assert cmp.truncated_reasons == ["symbols"]


def test_byte_budget_and_deadline(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"p.py": five_functions("p"), "q.py": five_functions("q")})
    monkeypatch.setattr(compare, "_COMPARE_MAX_TOTAL_BYTES", len(five_functions("p")) + 1)
    files, _, cmp = detail(repo)
    assert files["p.py"].symbols is not None
    assert (files["q.py"].symbols, files["q.py"].symbols_skipped) == (None, "limit")
    assert cmp.truncated_reasons == ["bytes"]
    monkeypatch.undo()

    now = [0.0]

    def expire():
        now[0] = compare._COMPARE_DEADLINE_S + 1

    files, _, cmp = detail(repo, clock=lambda: now[0], after_open=expire)
    assert {path: c.symbols_skipped for path, c in files.items()} == {"p.py": "limit", "q.py": "limit"}
    assert cmp.truncated_reasons == ["deadline"]


def test_file_cap(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {f"n{i}.py": f"n = {i}\n" for i in range(4)})
    monkeypatch.setattr(compare, "_COMPARE_MAX_FILES", 2)
    _, order, cmp = detail(repo)
    assert order == ["n0.py", "n1.py"]
    assert len(cmp.changes) == 4
    assert cmp.truncated_reasons == ["files"]


def test_code_files_get_the_slots_first(tmp_path, monkeypatch):
    repo = two_branch_repo(
        tmp_path,
        {"z.txt": "z\n"},
        {"a.md": "# a\n", "b.json": "{}\n", "c.py": "c = 1\n", "d.go": "package d\n"},
    )
    monkeypatch.setattr(compare, "_COMPARE_MAX_FILES", 2)
    _, order, cmp = detail(repo)
    assert order == ["c.py", "d.go"]
    assert len(cmp.changes) == 4
    assert cmp.truncated_reasons == ["files"]
    monkeypatch.setattr(compare, "_COMPARE_MAX_FILES", 3)
    files, order, _ = detail(repo)
    assert order == ["c.py", "d.go", "a.md"]
    assert files["a.md"].symbols_skipped == "unsupported_language"


def test_truncated_reasons_keep_the_spec_order(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {f"n{i}.py": five_functions(f"n{i}_") for i in range(5)})
    monkeypatch.setattr(compare, "_COMPARE_MAX_DIFF_ENTRIES", 4)
    monkeypatch.setattr(compare, "_COMPARE_MAX_FILES", 3)
    monkeypatch.setattr(compare, "_COMPARE_MAX_SYMBOLS_PER_LIST", 2)
    monkeypatch.setattr(compare, "_COMPARE_MAX_TOTAL_BYTES", 2 * len(five_functions("n0_")) + 1)
    _, order, cmp = detail(repo)
    assert len(order) == 3
    assert cmp.truncated_reasons == ["diff_entries", "files", "bytes", "symbols"]


@pytest.mark.parametrize("error", [BadObject(b"\x00" * 20), ValueError("missing")], ids=["BadObject", "ValueError"])
@pytest.mark.parametrize("method", ["stream", "info"])
def test_missing_blob_is_a_compare_error(tmp_path, monkeypatch, method, error):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"a.py": "a = 2\n"})
    blob = git(repo, "rev-parse", "feature:a.py")
    real = getattr(git_pkg.db.GitCmdObjectDB, method)

    def fail(self, binsha):
        if binsha.hex() == blob:
            raise error
        return real(self, binsha)

    monkeypatch.setattr(git_pkg.db.GitCmdObjectDB, method, fail)
    with pytest.raises(CompareError) as err:
        detail(repo)
    assert str(err.value) == "git object missing while comparing 'main' and 'feature'; the clone may be partial or shallow"


@posix_only
def test_symlink_side_of_a_modified_file_has_no_symbols(tmp_path):
    repo = two_branch_repo(tmp_path, {"t.py": "def target():\n    pass\n"}, {})
    git(repo, "checkout", "-q", "feature")
    os.symlink("class Target: pass", Path(repo, "s.py"))  # link text that would parse as a class
    git(repo, "add", "s.py")
    git(repo, "commit", "-q", "-m", "link")
    git(repo, "checkout", "-q", "-b", "real")
    Path(repo, "s.py").unlink()
    commit_files(repo, {"s.py": "def own():\n    pass\n"}, "file replaces link")
    git(repo, "checkout", "-q", "main")
    files, _, _ = detail(repo, base="feature", head="real")
    assert files["s.py"].status == "modified" and files["s.py"].kind == "blob"
    assert triples(files["s.py"].symbols) == {"added": {(F, None, "own")}, "removed": set(), "changed": set()}


def test_per_file_symbol_cap(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"p.py": five_functions("p"), "q.py": "def q():\n    pass\n"})
    monkeypatch.setattr(compare, "_COMPARE_MAX_FILE_SYMBOLS", 4)
    files, _, cmp = detail(repo)
    assert (files["p.py"].symbols, files["p.py"].symbols_skipped) == (None, "limit")
    assert triples(files["q.py"].symbols)["added"] == {(F, None, "q")}  # not sticky: the next file is detailed
    assert cmp.truncated_reasons == ["symbols"]


def test_a_stray_value_error_is_not_a_missing_object(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"a.py": "a = 2\n"})

    def boom(old, new):
        raise ValueError("a bug, not git")

    monkeypatch.setattr(compare, "diff_symbols", boom)
    with pytest.raises(ValueError, match="a bug, not git") as err:
        detail(repo)
    assert not isinstance(err.value, CompareError)
