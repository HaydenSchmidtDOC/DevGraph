"""Blame-based recency for Function/Class nodes.

Maps `git blame`'s per-line commit attribution onto a file's already-indexed
Function/Class line ranges, giving `last_modified_at`/`last_modified_by` at
function/class granularity without a `git log -L` call per function (see
Implementation Plan #7 for why `created_at` at this granularity is out of
scope for this pass — blame only tells you who last touched a line, not when
it was first written).
"""

from __future__ import annotations

from dataclasses import dataclass

from git import Repo


@dataclass
class FunctionRecency:
    """One contiguous blamed hunk: its line range and the commit that produced it."""

    start_line: int
    end_line: int
    last_modified_at: str
    last_modified_by: str | None


def compute_function_recency(repo: Repo, file_path: str) -> list[FunctionRecency]:
    """Blame `file_path` at HEAD and return one `FunctionRecency` per contiguous hunk.

    `Repo.blame("HEAD", file_path)` returns `[(Commit, [line, line, ...]), ...]`
    covering the whole file in order, each tuple's line list a contiguous
    run — a running line counter turns that directly into 1-indexed
    start/end ranges matching how Function/Class nodes store
    `start_line`/`end_line` (see `indexer/python/extractor.py`).
    """
    hunks = repo.blame("HEAD", file_path)
    recency: list[FunctionRecency] = []
    line_no = 1
    for commit, lines in hunks:
        start_line = line_no
        end_line = line_no + len(lines) - 1
        recency.append(
            FunctionRecency(
                start_line=start_line,
                end_line=end_line,
                last_modified_at=commit.authored_datetime.isoformat(),
                last_modified_by=commit.author.name if commit.author else None,
            )
        )
        line_no = end_line + 1
    return recency
