"""Endpoint nodes and IMPLEMENTS edges for the routes a non-Python
extractor finds (each extractor's `_..._routes` function reads its
frameworks' forms).

They take the shape apis/extractor.py gives a Python route: an `Endpoint`
named `"<METHOD> <path>"` claimed by the file (`framework`, `source`), a
file-less handler stub `Function` claimed the same way, and IMPLEMENTS from
the Endpoint to the stub (`to_file` "") and to the handler. A handler
declared in the route's file is pinned to that file and its owner, as a
FastAPI handler is; one another file declares is pinned to the files its
import names (`calls.call_rows`, as a call to it would be), so it is a
by-name edge the file records and a batch adding the handler relinks. A
handler written inline (a lambda, an arrow function) has no node: its
Endpoint IMPLEMENTS nothing.

Only literal paths are read: a path built from a variable, a constant or a
concatenation is skipped, and so is a route whose prefix is.
"""

from __future__ import annotations

from devgraph.indexer.calls import call_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship


def join_route(*parts: str) -> str:
    """Route path segments joined with single slashes, with a leading one
    and no trailing one: `join_route("/api/", "users/")` is "/api/users",
    and no segment at all "/"."""
    return "/" + "/".join(segment for part in parts for segment in part.split("/") if segment)


def add_route(
    result: ExtractionResult,
    repo_id: str,
    file_path: str,
    framework: str,
    method: str,
    path: str,
    handler: str | None = None,
    owner: str | None = None,
    pins: set[str] | None = None,
) -> None:
    """Add one route's Endpoint, handler stub and IMPLEMENTS edges to
    `result`. `handler` None is an inline handler: the Endpoint alone.
    `pins` None is a handler of this file, of `owner` (None: any owner);
    else the files the handler can be in (`calls.call_rows` pins), none
    when nothing resolves it, which leaves the stub its only target."""
    endpoint = f"{method} {path}"
    result.nodes.append(GraphNode(
        label="Endpoint", repo_id=repo_id, name=endpoint, properties={"framework": framework, "source": file_path},
    ))
    if handler is None:
        return
    result.nodes.append(GraphNode(
        label="Function", repo_id=repo_id, name=handler, properties={"type": "handler", "source": file_path},
    ))
    stub = GraphRelationship(
        from_label="Endpoint", from_name=endpoint, rel_type="IMPLEMENTS", to_label="Function", to_name=handler,
        repo_id=repo_id, to_file="",
    )
    if pins is None:
        result.relationships += [stub, GraphRelationship(
            from_label="Endpoint", from_name=endpoint, rel_type="IMPLEMENTS", to_label="Function", to_name=handler,
            repo_id=repo_id, to_file=file_path, to_owner=owner,
        )]
        return
    result.relationships.append(stub)
    result.relationships += call_rows(
        "Endpoint", endpoint, handler, pins, False, None, file_path, repo_id, rel_type="IMPLEMENTS",
    )
