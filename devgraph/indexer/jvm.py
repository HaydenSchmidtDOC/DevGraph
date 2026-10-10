"""Source roots and path pins shared by the Java and Kotlin extractors.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md
(Java and Kotlin). A JVM file's directory ends with as much of its package as
its source tree spells out: `core/src/main/java/com/acme/core/repo/Repo.java`
declaring `com.acme.core.repo` has its source root at `core/src/main/java`,
while Kotlin may leave the common root package out of the tree
(`src/main/kotlin/data/Note.kt` declaring `com.acme.notes.data`: root
`src/main/kotlin`, `com.acme.notes` omitted). A package then names a directory
under the same root (anchored, resolved) and, for every other source root of
the repository (another module, a test root), a path suffix (package
confidence). A suffix pin from a file outside a vendored directory leaves
vendored copies out (graph/schema.py `VENDORED_DIRS`, the `^` flag), and one
from main code leaves test source roots out too (`^t`).
"""

from __future__ import annotations

import posixpath

from devgraph.graph.schema import TEST_DIRS, VENDORED_DIRS

__all__ = ["JvmLayout", "type_segment"]


def type_segment(segments: list[str]) -> int | None:
    """The index of a dotted name's first capitalised segment, the class
    that picks the file (`a.b.Outer.Inner` -> Outer), or None."""
    return next((i for i, segment in enumerate(segments) if segment[:1].isupper()), None)


class JvmLayout:
    """Where one JVM file's packages live: its source root and the package
    prefix its tree leaves out."""

    def __init__(self, file_path: str, package: str | None) -> None:
        self.dir = posixpath.dirname(file_path)
        self.package = package.split(".") if package else []
        dirs = self.dir.split("/") if self.dir else []
        # The longest tail of the package the directory ends with.
        spelled = next(
            (k for k in range(len(self.package), 0, -1) if dirs[len(dirs) - k:] == self.package[len(self.package) - k:]),
            0,
        )
        self.root = "/".join(dirs[: len(dirs) - spelled])
        self.omitted = self.package[: len(self.package) - spelled]
        #: The flag a suffix pin carries (graph/schema.py `split_pin`): no
        #: vendored copy, and no test source root from main code; none from
        #: a vendored file itself.
        roots = set(self.root.split("/"))
        if roots & set(VENDORED_DIRS):
            self.flag = ""
        else:
            self.flag = "^" if roots & set(TEST_DIRS) else "^t"

    def _rel(self, package: list[str]) -> list[str] | None:
        if package[: len(self.omitted)] == self.omitted:
            return package[len(self.omitted):]
        return None

    def folder(self, package: list[str]) -> str | None:
        """The directory `package` is in under this file's source root, None
        when the root's omitted prefix rules it out."""
        rel = self._rel(package)
        if rel is None:
            return None
        return "/".join([self.root, *rel] if self.root else rel)

    def suffix(self, package: list[str], full: bool = False) -> str | None:
        """`package` as a path suffix (`/com/acme/core/util`): the part of it
        this file's tree spells out (all of it when `full`, or when it is not
        under the omitted prefix); None when that is empty (a suffix of no
        directory would match everything)."""
        rel = None if full else self._rel(package)
        parts = package if rel is None else rel
        return "/" + "/".join(parts) if parts else None
