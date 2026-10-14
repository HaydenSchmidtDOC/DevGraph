"""What changed between two local refs, read from git objects in memory.

`open_comparison` resolves `branch_a` (the base) and `branch_b` (the head),
finds their merge base, and walks the merge base's tree against the head's,
like `git diff branch_a...branch_b`. Objects are read through GitPython's
`cat-file` processes; the one other git subcommand is `merge-base`, on resolved
SHAs. `symbol_detail` then reads the changed blobs and diffs their symbols.
Nothing is written: not the working tree, the index or the graph. See
docs/superpowers/specs/2026-10-08-compare-branches-design.md (C1, C2, C3, C4, C5, C6, C7).
"""

from __future__ import annotations

import logging
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import git
from git.exc import BadName, CommandError, GitCommandError, InvalidGitRepositoryError, NoSuchPathError
from gitdb.exc import BadObject

from devgraph.indexer.git_history import open_repo
from devgraph.indexer.symbols import TooManySymbols, decode_source, diff_symbols, extract_symbols, language_for
from devgraph.paths import MAX_CONFIG_BYTES

log = logging.getLogger(__name__)

_COMPARE_MAX_DIFF_ENTRIES = 10_000
_COMPARE_MAX_FILES = 200
_COMPARE_MAX_FILE_BYTES = MAX_CONFIG_BYTES
_COMPARE_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_COMPARE_MAX_SYMBOLS_PER_LIST = 50
_COMPARE_MAX_SYMBOLS = 1_000
_COMPARE_MAX_FILE_SYMBOLS = 5_000
_COMPARE_MAX_CALLERS = 25  # read by the MCP tool's graph query (C8)
_COMPARE_DEADLINE_S = 20.0

_MAX_REF_LENGTH = 256
_ECHO_LENGTH = 100
_NULL_SHA = "0" * 40
_EMPTY_BLOB_SHA = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
_SYMLINK_MODE = 0o120000
_SHALLOW_HINT = "; this is a shallow clone, so older commits may be missing: git fetch --unshallow"
_BINARY_SNIFF_BYTES = 8_000  # git's own heuristic: a NUL byte this early means binary
_REASON_ORDER = ("diff_entries", "files", "bytes", "symbols", "deadline")


class CompareError(ValueError):
    """A comparison the caller can fix or should know about; its text is safe to show."""


@dataclass
class FileChange:
    """One changed path. `base_blob`/`head_blob` are `None` on the side without the file,
    and always for a submodule (a gitlink is never opened)."""

    path: str
    status: str  # "added", "removed", "modified" or "renamed"
    old_path: str | None = None
    base_blob: git.Blob | None = None
    head_blob: git.Blob | None = None
    kind: str = "blob"  # "blob", "symlink" or "submodule"
    # Filled by `symbol_detail` for the files it details:
    language: str | None = None  # the code route, or None
    symbols: dict[str, list[dict]] | None = None  # {"added", "removed", "changed"}, or None
    symbols_skipped: str | None = None  # why `symbols` is None
    symbols_truncated: bool = False  # a list was capped


@dataclass
class RefComparison:
    repo: git.Repo
    base_ref: str
    head_ref: str
    base_commit: git.Commit
    head_commit: git.Commit
    merge_base: git.Commit
    changes: list[FileChange] = field(default_factory=list)
    truncated_reasons: list[str] = field(default_factory=list)
    deadline: float = float("inf")  # on `clock`
    clock: Callable[[], float] = time.monotonic  # the one given to `open_comparison`


def _echo(value: str) -> str:
    """A caller value for an error message: cut to 100 characters, quoted."""
    return repr(str(value)[:_ECHO_LENGTH])


def _ref_rule(ref: str) -> str | None:
    """The first C4 rule `ref` breaks, or `None`."""
    if not ref or len(ref) > _MAX_REF_LENGTH:
        return "it is empty or longer than 256 characters"
    if ref.startswith("-"):
        return "it starts with '-'"
    if ref.startswith("/"):
        return "it starts with '/'"
    if ".." in ref:
        return "it contains '..'"
    if ":" in ref:
        return "it contains ':'"
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in ref):
        return "it contains whitespace or a control character"
    if any(ch in "*?[\\" for ch in ref):
        return "it contains one of * ? [ \\"
    # GitPython's reflog, upstream and date forms do not always agree with git's,
    # so such a ref could silently compare the wrong commit.
    if "@{" in ref:
        return "reflog and upstream forms like @{...} aren't supported; pass a branch name, tag or commit SHA"
    # GitPython hands `^{/regex}` to an untimed `git rev-parse` that searches all history.
    if "{/" in ref:
        return "regex forms like ^{/...} aren't supported; pass a branch name, tag or commit SHA"
    return None


def validate_ref(arg_name: str, ref: str) -> None:
    """Raise `CompareError` if `ref` is option-like, path-like or otherwise not a plain ref."""
    rule = _ref_rule(ref)
    if rule is not None:
        raise CompareError(f"{arg_name} {_echo(ref)} is not a valid ref: {rule}")


def _shallow_hint(repo: git.Repo) -> str:
    return _SHALLOW_HINT if Path(repo.common_dir, "shallow").exists() else ""


def _resolve(repo: git.Repo, repo_id: str, arg_name: str, ref: str) -> git.Commit:
    """The commit `ref` names, or the unknown-ref `CompareError`."""
    unknown = CompareError(
        f"{arg_name} {_echo(ref)} is not a branch, tag or commit in repository {_echo(repo_id)}; "
        f"refs must exist locally (DevGraph never fetches){_shallow_hint(repo)}"
    )
    try:
        commit = repo.commit(ref)
    except (CommandError, OSError):
        raise  # git itself failed: the generic git-failure message, not a bad ref
    except Exception as exc:
        # GitPython's rev_parse raises many unrelated types for a ref that does not resolve.
        raise unknown from exc
    if commit.hexsha == _NULL_SHA:
        raise unknown
    try:
        commit.tree
    except (BadName, BadObject, ValueError) as exc:
        raise unknown from exc
    return commit


def _stderr_text(exc: GitCommandError) -> str:
    """The process's own stderr, without GitPython's `stderr: '...'` wrapping."""
    text = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
    text = text.strip()
    if text.startswith("stderr: '"):
        text = text[len("stderr: '") :]
    return text


class _Walk:
    """The C2 tree walk: merge base's tree against the head's, by object id."""

    def __init__(self, deadline: float, clock: Callable[[], float]):
        self.deadline = deadline
        self.clock = clock
        self.changes: list[FileChange] = []
        self.reasons: list[str] = []

    def _stop(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def stopped(self) -> bool:
        return bool(self.reasons)

    def _entries(self, tree: git.Tree) -> dict[str, object] | None:
        """`tree`'s entries by name, or `None` if the deadline has passed (checked before the read)."""
        if self.clock() >= self.deadline:
            self._stop("deadline")
            return None
        # `obj.path` rather than `obj.name`: a Submodule's name needs .gitmodules.
        return {obj.path.rpartition("/")[2]: obj for obj in tree}

    def _add(self, path: str, status: str, old, new) -> None:
        if len(self.changes) >= _COMPARE_MAX_DIFF_ENTRIES:
            self._stop("diff_entries")
            return
        kind = _kind(new if new is not None else old)
        if kind == "submodule":
            old = new = None
        self.changes.append(FileChange(path, status, None, old, new, kind))

    def one_side(self, path: str, obj, status: str) -> None:
        """Everything under `obj`, present on one side only, as `status`."""
        if self.stopped:
            return
        if obj.type != "tree":
            self._add(path, status, *((obj, None) if status == "removed" else (None, obj)))
            return
        entries = self._entries(obj)
        if entries is None:
            return
        for name in sorted(entries):
            self.one_side(f"{path}/{name}", entries[name], status)

    def trees(self, prefix: str, base: git.Tree, head: git.Tree) -> None:
        base_entries = self._entries(base)
        head_entries = None if base_entries is None else self._entries(head)
        if head_entries is None:
            return
        for name in sorted(base_entries.keys() | head_entries.keys()):
            if self.stopped:
                return
            path = f"{prefix}{name}"
            old, new = base_entries.get(name), head_entries.get(name)
            if new is None:
                self.one_side(path, old, "removed")
            elif old is None:
                self.one_side(path, new, "added")
            elif old.binsha == new.binsha:
                continue  # unchanged (a mode-only change keeps the object id): never read
            elif old.type == new.type == "tree":
                self.trees(f"{path}/", old, new)
            elif old.type == new.type:
                self._add(path, "modified", old, new)
            else:
                # A tree on one side and a blob (or gitlink) on the other.
                self.one_side(path, old, "removed")
                self.one_side(path, new, "added")


def _kind(obj) -> str:
    if obj.type == "submodule":
        return "submodule"
    return "symlink" if obj.mode == _SYMLINK_MODE else "blob"


def _pair_renames(changes: list[FileChange]) -> list[FileChange]:
    """Exact renames: a removed and an added path with the same blob id (never the empty
    blob, never a submodule) become one `renamed` entry, paired in sorted path order."""
    removed: dict[tuple[str, str], list[FileChange]] = {}
    for change in sorted(changes, key=lambda c: c.path):
        if change.status == "removed" and change.base_blob is not None and change.base_blob.hexsha != _EMPTY_BLOB_SHA:
            removed.setdefault((change.kind, change.base_blob.hexsha), []).append(change)
    paired: set[int] = set()
    result = []
    for change in sorted(changes, key=lambda c: c.path):
        if change.status == "added" and change.head_blob is not None:
            candidates = removed.get((change.kind, change.head_blob.hexsha))
            if candidates:
                old = candidates.pop(0)
                paired.add(id(old))
                change = FileChange(change.path, "renamed", old.path, old.base_blob, change.head_blob, change.kind)
        result.append(change)
    return sorted((c for c in result if id(c) not in paired), key=lambda c: c.path)


@contextmanager
def open_comparison(
    repo_path,
    repo_id: str,
    base_ref: str,
    head_ref: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    arg_names: tuple[str, str] = ("branch_a", "branch_b"),
    tool: str = "compare_branches",
) -> Iterator[RefComparison]:
    """Yield the `RefComparison` of `head_ref` against its merge base with `base_ref`.

    The repository stays open (its blobs readable) until the block exits. Every
    failure is a `CompareError` with the C7 message, naming the refs by
    `arg_names` and the calling tool by `tool`."""
    deadline = clock() + _COMPARE_DEADLINE_S
    base_arg, head_arg = arg_names
    validate_ref(base_arg, base_ref)
    validate_ref(head_arg, head_ref)
    pair = f"{_echo(base_ref)} and {_echo(head_ref)}"
    repo = None
    try:
        try:
            repo = open_repo(repo_path)
        except (NoSuchPathError, InvalidGitRepositoryError) as exc:
            raise CompareError(
                f"repository {_echo(repo_id)} is not a git repository at its registered root; "
                f"{tool} needs the repository's own .git"
            ) from exc
        base = _resolve(repo, repo_id, base_arg, base_ref)
        head = _resolve(repo, repo_id, head_arg, head_ref)

        timeout_kw = {} if sys.platform == "win32" else {"kill_after_timeout": max(deadline - clock(), 1)}
        try:
            bases = repo.merge_base(base.hexsha, head.hexsha, **timeout_kw)
        except GitCommandError as exc:
            if _stderr_text(exc).startswith("Timeout:"):
                raise CompareError(f"{tool} timed out finding the merge base of {pair}") from exc
            raise
        if not bases:
            raise CompareError(f"{pair} share no history in repository {_echo(repo_id)}{_shallow_hint(repo)}")
        merge_base = bases[0]

        walk = _Walk(deadline, clock)
        try:
            walk.trees("", merge_base.tree, head.tree)
        except (BadName, BadObject, ValueError) as exc:
            raise CompareError(
                f"git object missing while comparing {pair}; the clone may be partial or shallow"
            ) from exc
        yield RefComparison(
            repo=repo,
            base_ref=base_ref,
            head_ref=head_ref,
            base_commit=base,
            head_commit=head,
            merge_base=merge_base,
            changes=_pair_renames(walk.changes),
            truncated_reasons=walk.reasons,
            deadline=deadline,
            clock=clock,
        )
    except CompareError:
        raise
    except (CommandError, OSError) as exc:
        log.warning("%s: git failed for %s in %s: %s", tool, pair, repo_id, exc)
        raise CompareError(
            f"git failed while comparing {pair} in repository {_echo(repo_id)}: {type(exc).__name__}"
        ) from exc
    finally:
        if repo is not None:
            repo.close()


class _Detail:
    """C5's per-call budgets for `symbol_detail`. Once a byte, symbol or deadline limit
    is hit, every later file that would need parsing gets `symbols_skipped: "limit"`."""

    def __init__(self, comparison: RefComparison):
        self.deadline = comparison.deadline
        self.clock = comparison.clock
        self.missing = (
            f"git object missing while comparing {_echo(comparison.base_ref)} and {_echo(comparison.head_ref)}; "
            "the clone may be partial or shallow"
        )
        self.bytes = 0
        self.symbols = 0
        self.reasons: set[str] = set()
        self.stopped = False

    def _limit(self, reason: str) -> str:
        self.reasons.add(reason)
        self.stopped = True
        return "limit"

    def _size(self, blob: git.Blob) -> int:
        try:
            return blob.size  # cat-file --batch-check: the data is not read
        except (BadName, BadObject, ValueError) as exc:
            raise CompareError(self.missing) from exc

    def _data(self, blob: git.Blob) -> bytes:
        try:
            return blob.data_stream.read()
        except (BadName, BadObject, ValueError) as exc:
            raise CompareError(self.missing) from exc

    def skip_reason(self, change: FileChange) -> str | None:
        """Why `change` gets no symbols, before any blob data is read; `None` to parse it."""
        if change.kind != "blob":
            return change.kind  # "symlink" or "submodule"
        if change.language is None:
            return "unsupported_language"
        if change.status == "renamed":
            return None
        sizes = [self._size(blob) for blob in (change.base_blob, change.head_blob) if blob is not None]
        if any(size > _COMPARE_MAX_FILE_BYTES for size in sizes):
            return "too_large"
        if self.stopped:
            return "limit"
        if self.clock() >= self.deadline:
            return self._limit("deadline")
        if self.bytes + sum(sizes) > _COMPARE_MAX_TOTAL_BYTES:
            return self._limit("bytes")
        if self.symbols >= _COMPARE_MAX_SYMBOLS:
            return self._limit("symbols")
        self.bytes += sum(sizes)
        return None

    def parse(self, change: FileChange) -> None:
        """Fill `change.symbols` (or `symbols_skipped`) from both sides' blobs."""
        sides = []
        for blob in (change.base_blob, change.head_blob):
            if blob is None or blob.mode == _SYMLINK_MODE:
                # A symlink side of a link-to-file change: its blob is link text, not source.
                sides.append([])
                continue
            data = self._data(blob)
            if b"\0" in data[:_BINARY_SNIFF_BYTES]:
                change.symbols_skipped = "binary"
                return
            try:
                sides.append(extract_symbols(change.path, decode_source(data, change.path), _COMPARE_MAX_FILE_SYMBOLS))
            except TooManySymbols:
                # Not sticky: the next file may be small. Bounds the per-symbol work on one file.
                change.symbols_skipped = "limit"
                self.reasons.add("symbols")
                return
            except Exception:
                log.debug("compare_branches: extractor failed on %s", change.path, exc_info=True)
                change.symbols_skipped = "parse_error"
                return
        lists = dict(zip(("added", "removed", "changed"), diff_symbols(*sides)))
        for name, entries in lists.items():
            keep = min(_COMPARE_MAX_SYMBOLS_PER_LIST, _COMPARE_MAX_SYMBOLS - self.symbols)
            if len(entries) > keep:
                lists[name] = entries[:keep]
                change.symbols_truncated = True
                self.reasons.add("symbols")
            self.symbols += len(lists[name])
        change.symbols = lists


def symbol_detail(comparison: RefComparison) -> list[FileChange]:
    """Detail the changes `files.results` lists, and return them in listing order.

    Every change gets `language`. Code-language files come first (path order), then
    the rest (path order), up to `_COMPARE_MAX_FILES` in all; only these get
    `symbols` or `symbols_skipped`, and the changes past the cap keep both `None`.
    The deadline and clock are the comparison's. The C5 reasons hit here join
    `comparison.truncated_reasons`, in the spec's order. Must run inside the
    `open_comparison` block, while the blobs are readable."""
    for change in comparison.changes:
        change.language = language_for(change.path) if change.kind == "blob" else None
    code = [c for c in comparison.changes if c.language is not None]
    rest = [c for c in comparison.changes if c.language is None]
    listed = (code + rest)[:_COMPARE_MAX_FILES]
    budget = _Detail(comparison)
    if len(comparison.changes) > _COMPARE_MAX_FILES:
        budget.reasons.add("files")
    for change in listed:
        change.symbols_skipped = budget.skip_reason(change)
        if change.symbols_skipped is not None:
            continue
        if change.status == "renamed":
            change.symbols = {"added": [], "removed": [], "changed": []}  # identical content, never read
        else:
            budget.parse(change)
    reasons = set(comparison.truncated_reasons) | budget.reasons
    comparison.truncated_reasons = [r for r in _REASON_ORDER if r in reasons]
    return listed
