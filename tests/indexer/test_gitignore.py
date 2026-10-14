""".gitignore rules: the matcher, and the walk honouring them."""

import pytest

from devgraph.indexer import gitignore, walk
from devgraph.indexer.gitignore import is_gitignored, parse


def _ignored(text: str, rel: str, is_dir: bool = False) -> bool:
    """Case-sensitively, so the cases hold on Windows and macOS too (where
    git, like DevGraph, folds case: `[![:upper:]]` then matches no letter)."""
    return gitignore.matches((("", parse(text, ignorecase=False)),), rel, is_dir)


@pytest.mark.parametrize(
    ("pattern", "rel", "is_dir", "expected"),
    [
        ("*.log", "a.log", False, True),
        ("*.log", "deep/er/a.log", False, True),
        ("*.log", "a.log.txt", False, False),
        ("out/", "out", True, True),
        ("out/", "out", False, False),  # a directory-only pattern never matches a file
        ("out/", "src/out", True, True),
        ("/out", "out", True, True),
        ("/out", "src/out", True, False),  # a leading slash anchors to the .gitignore's folder
        ("doc/frotz", "doc/frotz", True, True),
        ("doc/frotz", "a/doc/frotz", True, False),  # a middle slash anchors too
        ("**/foo", "a/b/foo", False, True),
        ("**/foo/bar", "x/foo/bar", False, True),
        ("abc/**", "abc/x/y", False, True),
        ("abc/**", "abc", True, False),
        ("a/**/b", "a/b", False, True),
        ("a/**/b", "a/x/y/b", False, True),
        ("?.py", "a.py", False, True),
        ("?.py", "ab.py", False, False),
        ("[ab].py", "b.py", False, True),
        ("[!ab].py", "b.py", False, False),
        ("[!ab].py", "c.py", False, True),
        ("\\#keep", "#keep", False, True),
        ("# a comment", "# a comment", False, False),
        ("\\!bang", "!bang", False, True),
        ("trailing   ", "trailing", False, True),
        ("*.min.js", "static/js/app.min.js", False, True),
        (".next", ".next", True, True),
    ],
)
def test_patterns_follow_gitignore_5(pattern, rel, is_dir, expected):
    assert _ignored(pattern, rel, is_dir) is expected


def test_a_negation_re_includes_and_the_last_match_wins():
    rules = "*.log\n!keep.log\n"
    assert _ignored(rules, "a.log")
    assert not _ignored(rules, "keep.log")
    assert _ignored(rules + "keep.log\n", "keep.log")


def _tree(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _walked(root):
    return {p.relative_to(root).as_posix() for p in walk.indexable_paths(root)}


def test_the_walk_honours_root_and_nested_gitignores_with_negations(tmp_path):
    _tree(tmp_path, {
        ".gitignore": "coverage/\n.tox/\nenv/\nout/\n*.log\n!important.log\n",
        "src/app.py": "",
        "src/debug.log": "",
        "important.log": "",
        "coverage/index.py": "",
        ".tox/py313/lib.py": "",
        "env/lib/site.py": "",
        "out/bundle.js": "",
        "web/.gitignore": ".next\n/local.py\n!debug.log\n",
        "web/.next/server.js": "",
        "web/local.py": "",
        "web/sub/local.py": "",
        "web/debug.log": "",
    })
    assert _walked(tmp_path) == {
        ".gitignore", "src/app.py", "important.log",
        "web/.gitignore", "web/sub/local.py", "web/debug.log",
    }


def test_a_file_inside_an_ignored_folder_cannot_be_re_included(tmp_path):
    _tree(tmp_path, {".gitignore": "build2/\n!build2/keep.py\n", "build2/keep.py": "", "a.py": ""})
    assert _walked(tmp_path) == {".gitignore", "a.py"}
    assert is_gitignored(tmp_path, "build2/keep.py")


def test_walking_a_subfolder_applies_the_rules_above_it(tmp_path):
    _tree(tmp_path, {".gitignore": "*.gen.py\nskip/\n", "pkg/a.py": "", "pkg/b.gen.py": "", "skip/c.py": ""})
    assert walk.indexable_paths_under(tmp_path, tmp_path / "pkg") == {tmp_path / "pkg" / "a.py"}
    assert walk.indexable_paths_under(tmp_path, tmp_path / "skip") == set()


def test_point_checks_agree_with_the_walk(tmp_path):
    _tree(tmp_path, {".gitignore": "*.log\n", "web/.gitignore": "!keep.log\n"})
    assert is_gitignored(tmp_path, "a.log")
    assert is_gitignored(tmp_path, "deep/a.log")
    assert not is_gitignored(tmp_path, "web/keep.log")
    assert not is_gitignored(tmp_path, "a.py")


def test_an_edited_gitignore_is_read_again(tmp_path):
    _tree(tmp_path, {".gitignore": "*.log\n"})
    assert is_gitignored(tmp_path, "a.log")
    (tmp_path / ".gitignore").write_text("*.tmp\n# longer, so the size changes too\n")
    assert not is_gitignored(tmp_path, "a.log")
    assert is_gitignored(tmp_path, "a.tmp")
    (tmp_path / ".gitignore").unlink()
    assert not is_gitignored(tmp_path, "a.tmp")


def test_an_unreadable_gitignore_ignores_nothing(tmp_path):
    (tmp_path / ".gitignore").mkdir()  # not a file
    (tmp_path / "a.py").write_text("")
    assert "a.py" in _walked(tmp_path)


# --- review fixes: never an invalid regex, and closer to git's wildmatch ------


@pytest.mark.parametrize("pattern", ["[z-a].py", "[%-#]", "[z-a]", "a[\\"])
def test_a_reversed_range_or_odd_class_never_raises(pattern):
    parse(pattern)  # must not raise re.error


def test_a_reversed_range_matches_nothing_but_the_rest_of_the_class_still_does():
    assert not _ignored("[z-a].py", "m.py")
    assert not _ignored("[%-#]", "$")
    assert _ignored("[z-ab].py", "b.py")
    assert not _ignored("[z-ab].py", "m.py")


def test_a_bad_gitignore_does_not_break_the_walk(tmp_path):
    _tree(tmp_path, {".gitignore": "[z-a]\n*.log\n", "a.py": "", "b.log": ""})
    assert _walked(tmp_path) == {".gitignore", "a.py"}


def test_an_uncompilable_rule_is_dropped(monkeypatch):
    real = gitignore._translate

    def broken(pattern):
        return "(" if pattern == "bad" else real(pattern)

    monkeypatch.setattr(gitignore, "_translate", broken)
    rules = parse("bad\n*.log\n")
    assert len(rules) == 1


@pytest.mark.parametrize(
    ("pattern", "rel", "expected"),
    [
        ("[[:digit:]].py", "7.py", True),
        ("[[:digit:]].py", "a.py", False),
        ("[[:alpha:]_]x", "_x", True),
        ("[![:upper:]]", "a", True),
        ("[![:upper:]]", "A", False),
        ("[[:nosuch:]]", "a", False),  # an unknown class name matches nothing
        ("a[bc", "a[bc", False),  # an unclosed [ matches nothing
        ("a[bc", "ab", False),
        ("trail\\", "trail\\", False),  # a trailing backslash matches nothing
        ("trail\\", "trail", False),
    ],
)
def test_wildmatch_corners_follow_git(pattern, rel, expected):
    assert _ignored(pattern, rel) is expected


def test_trailing_spaces_are_trimmed_by_walking_escapes_as_git_does():
    assert _ignored("a\\ ", "a ")  # an escaped space is kept
    assert _ignored("bs\\\\ ", "bs\\")  # an escaped backslash, then a space that is trimmed
    assert not _ignored("bs\\\\ ", "bs\\ ")


def test_lines_split_on_newline_only_and_lose_a_trailing_carriage_return():
    rules = parse("one\r\ntwo\x0bthree\n", ignorecase=False)
    assert _ignored("one\r\ntwo\x0bthree\n", "one")
    assert _ignored("one\r\ntwo\x0bthree\n", "two\x0bthree")
    assert not _ignored("one\r\ntwo\x0bthree\n", "two")
    assert len(rules) == 2


def test_case_is_ignored_only_where_the_platform_is_case_insensitive():
    assert gitignore.matches((("", parse("*.LOG", ignorecase=True)),), "a.log", False)
    assert not gitignore.matches((("", parse("*.LOG", ignorecase=False)),), "a.log", False)
    import sys

    assert gitignore.IGNORECASE is (sys.platform in ("win32", "darwin"))
