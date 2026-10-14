"""The repository's `.gitignore` files, as the walk and the watcher apply them.

Every `.gitignore` in the tree counts, each for the folder it sits in and
below, with the rules of gitignore(5): `#` comments, `!` negations (the last
matching rule wins, and a deeper file's rules come after its parents'), a
trailing `/` for folders only, a leading or middle `/` anchoring the pattern
to the file's folder, and `*`, `?`, `[...]` and `**` wildcards. A file inside
an ignored folder stays ignored whatever a later rule says, as in git.

Applied to any registered folder, git checkout or not. `.git/info/exclude`
and the user's global excludes file are not read: they are per-clone, so two
clones of one repository would index differently. Only the .gitignore files
count, never git's index: a file added with `git add -f` that a pattern
matches stays ignored. Patterns are matched as git's wildmatch matches them
(a malformed one matches nothing), ignoring case on Windows and macOS.

Parsed files are cached by path and re-read when their stat changes, so an
edited `.gitignore` takes effect on the next check.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

GITIGNORE = ".gitignore"


class Rule(NamedTuple):
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


#: (repo-relative POSIX prefix of the folder holding a .gitignore -- "" for
#: the root, else ending in "/" -- and its rules), outermost first.
Chain = Sequence[tuple[str, Sequence[Rule]]]

_cache: dict[str, tuple[tuple[int, int, int], list[Rule]]] = {}


#: Whether patterns match case-insensitively: git's default `core.ignorecase`
#: on the platforms whose filesystems usually ignore case.
IGNORECASE = sys.platform in ("win32", "darwin")

#: The POSIX classes wildmatch knows, as regex character classes.
_POSIX_CLASSES = {
    "alnum": "[A-Za-z0-9]",
    "alpha": "[A-Za-z]",
    "blank": "[ \\t]",
    "cntrl": "[\\x00-\\x1f\\x7f]",
    "digit": "[0-9]",
    "graph": "[!-~]",
    "lower": "[a-z]",
    "print": "[ -~]",
    "punct": "[!-/:-@\\[-`{-~]",
    "space": "[ \\t\\n\\r\\f\\v]",
    "upper": "[A-Z]",
    "xdigit": "[0-9A-Fa-f]",
}


class _NoMatch(Exception):
    """The pattern can match nothing (git's wildmatch aborts on it)."""


def parse(text: str, ignorecase: bool = IGNORECASE) -> list[Rule]:
    """The rules of one .gitignore file's text. Lines are split on `\\n`
    only, each losing one trailing `\\r`, as git reads them. A rule that can
    match nothing (an unclosed `[`, a trailing backslash, an unknown POSIX
    class) is dropped, as is any that would not compile."""
    rules = []
    for line in text.removeprefix("\ufeff").split("\n"):
        try:
            rule = _rule(line.removesuffix("\r"), ignorecase)
        except (_NoMatch, re.error):
            continue
        if rule is not None:
            rules.append(rule)
    return rules


def _trim_trailing_spaces(line: str) -> str:
    """git's `trim_trailing_spaces`: drop the run of spaces at the end unless
    a backslash escapes its first one, walking the escapes from the start."""
    last_space = None
    i = 0
    while i < len(line):
        c = line[i]
        if c == " ":
            if last_space is None:
                last_space = i
        elif c == "\\":
            i += 1
            if i >= len(line):
                return line
            last_space = None
        else:
            last_space = None
        i += 1
    return line if last_space is None else line[:last_space]


def _rule(line: str, ignorecase: bool) -> Rule | None:
    line = _trim_trailing_spaces(line)
    if not line or line.startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    if not line:
        return None
    anchored = "/" in line
    line = line.removeprefix("/")
    body = _translate(line)
    flags = re.DOTALL | (re.IGNORECASE if ignorecase else 0)
    return Rule(re.compile(body if anchored else f"(?:.*/)?{body}", flags), negated, dir_only)


def _translate(pattern: str) -> str:
    """The regex for a pattern matched against a path relative to its
    .gitignore's folder. Raises `_NoMatch` for a pattern that matches nothing."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*" and pattern.startswith("**", i):
            at_start = i == 0 or pattern[i - 1] == "/"
            at_end = i + 2 == n
            if at_start and at_end:
                out.append(".*")
                i += 2
                continue
            if at_start and pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            out.append("[^/]*")  # not a whole segment: an ordinary star
            i += 2
            while i < n and pattern[i] == "*":
                i += 1
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            regex, i = _char_class(pattern, i)
            out.append(regex)
            continue
        elif c == "\\":
            i += 1
            if i >= n:
                raise _NoMatch  # a trailing backslash
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _char_class(pattern: str, start: int) -> tuple[str, int]:
    """(regex, index after the class) for the `[...]` opening at `start`,
    parsed as git's wildmatch parses it: a leading `!` or `^` negates, a
    first `]` is literal, `\\` escapes, `a-z` is a range (a reversed one
    matches nothing), `[:name:]` a POSIX class. Never matches `/`. Raises
    `_NoMatch` for an unclosed class or an unknown POSIX class."""
    n = len(pattern)
    i = start + 1
    negated = i < n and pattern[i] in "!^"
    if negated:
        i += 1
    members: list[str] = []
    prev: str | None = None
    first = True
    while True:
        if i >= n:
            raise _NoMatch  # unclosed
        c = pattern[i]
        if c == "]" and not first:
            i += 1
            break
        first = False
        if c == "\\":
            i += 1
            if i >= n:
                raise _NoMatch
            c = pattern[i]
            members.append(re.escape(c))
            prev = c
        elif c == "-" and prev is not None and i + 1 < n and pattern[i + 1] != "]":
            i += 1
            hi = pattern[i]
            if hi == "\\":
                i += 1
                if i >= n:
                    raise _NoMatch
                hi = pattern[i]
            members.pop()  # prev itself is matched by the range
            if prev <= hi:
                members.append(f"[{re.escape(prev)}-{re.escape(hi)}]")
            prev = None
        elif c == "[" and pattern.startswith("[:", i):
            end = pattern.find("]", i + 2)
            if end == -1:
                raise _NoMatch
            if pattern[end - 1] != ":" or end - 1 < i + 2:
                members.append(re.escape(c))  # not a POSIX class: a literal [
                prev = c
            else:
                name = pattern[i + 2:end - 1]
                if name not in _POSIX_CLASSES:
                    raise _NoMatch
                members.append(_POSIX_CLASSES[name])
                prev = None
                i = end
        else:
            members.append(re.escape(c))
            prev = c
        i += 1
    alternatives = "|".join(members)
    if negated:
        return (f"(?!{alternatives})[^/]" if members else "[^/]"), i
    if not members:
        raise _NoMatch
    return f"(?!/)(?:{alternatives})", i


def matches(chain: Chain, rel: str, is_dir: bool) -> bool:
    """Whether the rules in `chain` ignore `rel` (repo-relative POSIX). Only
    `rel` itself is judged, not the folders above it."""
    ignored = False
    for prefix, rules in chain:
        if not rel.startswith(prefix):
            continue
        sub = rel[len(prefix):]
        for rule in rules:
            if (not rule.dir_only or is_dir) and rule.regex.fullmatch(sub):
                ignored = not rule.negated
    return ignored


def rules_in(folder: Path) -> list[Rule]:
    """The rules of `folder`'s .gitignore: none when it has none or it can't be read."""
    path = os.path.join(folder, GITIGNORE)
    try:
        st = os.stat(path)
    except OSError:
        _cache.pop(path, None)
        return []
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, "rb") as f:
            rules = parse(f.read().decode("utf-8", errors="replace"))
    except OSError:  # a folder, or unreadable
        rules = []
    _cache[path] = (stamp, rules)
    return rules


def chain_to(repo_root: Path, rel_dir: str) -> list[tuple[str, list[Rule]]] | None:
    """The rules that apply inside the folder `rel_dir` ("" for the root),
    or None when it, or a folder above it, is ignored."""
    chain: list[tuple[str, list[Rule]]] = []
    prefix = ""
    parts = [part for part in rel_dir.split("/") if part]
    for depth in range(len(parts) + 1):
        rules = rules_in(repo_root / prefix if prefix else repo_root)
        if rules:
            chain.append((prefix, rules))
        if depth == len(parts):
            break
        folder = prefix + parts[depth]
        if matches(chain, folder, True):
            return None
        prefix = folder + "/"
    return chain


def is_gitignored(repo_root: Path, rel: str, is_dir: bool = False) -> bool:
    """Whether a .gitignore ignores the repo-relative POSIX path `rel`, or a folder above it."""
    parent, _, _name = rel.rpartition("/")
    chain = chain_to(Path(repo_root), parent)
    return chain is None or matches(chain, rel, is_dir)
