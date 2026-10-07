"""The dashboard's `/api/*` endpoints.

Every repo-scoped handler validates `repo_id` against the registry first and
404s if unknown -- the same allowlist discipline `mcp/server.py` applies,
since this is a second entry point into the same engine/registry the tray
already owns (see Implementation Plan #5's "Data comes from GraphEngine
directly" decision).

Read-only apart from these writes: the canvas layout (`PUT .../layout`),
repository registration (`POST /repos`), which is the same add-then-initial-
scan sequence `devgraph add <path>` runs, against the same services, the
Cypher console (`POST /cypher`), which runs whatever it is given, and the
Config page's entry edits (`POST|PUT|DELETE /config/...`), which write only
a registered repo's `devgraph.tools.yaml`/`devgraph.schema.yaml` or the
global tools store through `devgraph.config.edits`, and never touch git
(`POST /config/parse` only reads entry YAML back for the form), and
recomputing graph insights (`POST .../insights`), which replaces only derived
properties. All of them refuse cross-site browser requests
(`_reject_cross_site`).
"""

from __future__ import annotations

import asyncio
import colorsys
import hashlib
import inspect
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from devgraph.analytics.insights import read_insights, refresh_insights, top_nodes
from devgraph.config import edits
from devgraph.config.project_schema import (
    ABSENT_SCHEMA_HASH,
    LABEL_PATTERN,
    RELATIONSHIP_TYPE_PATTERN,
    SCHEMA_FILENAME,
    ProjectSchemaError,
    load_project_schema,
    schema_file_hash,
)
from devgraph.config.project_trust import trust_command
from devgraph.config.schema_findings import introduced_conflicts, schema_conflicts
from devgraph.config.settings import get_settings
from devgraph.config.yaml_bound import bounded_safe_load
from devgraph.dashboard import queries
from devgraph.dashboard.config_model import GLOBAL_SCOPE, build_config, build_global, build_project, form_entry, scrub
from devgraph.dashboard.db_metrics import MetricsHistory
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.git_info import get_git_log, get_git_status
from devgraph.dashboard.layout_store import load_layout, save_layout
from devgraph.dashboard.query_log import QueryLog
from devgraph.graph.engine import GraphEngine, identity_key, provision_repository_schema
from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools as devgraph_tools
from devgraph.registry.store import RepoRegistry

logger = logging.getLogger(__name__)

_GRAPH_LIMIT_DEFAULT = 500
# Hard ceiling so a large repo's full graph can't hang the browser tab, per
# Implementation Plan #5 Item 1.
_GRAPH_LIMIT_CEILING = 2000
# A saved layout has at most one entry per node the canvas could ever have
# fetched, i.e. _GRAPH_LIMIT_CEILING entries; budget generously per entry
# (a long identity_key plus an [x, y] pair) and round up, so a legitimate
# full-graph save never gets rejected while a malformed/hostile PUT still
# can't write an unbounded file to disk.
_LAYOUT_PAYLOAD_LIMIT_BYTES = _GRAPH_LIMIT_CEILING * 1024
_SSE_KEEPALIVE_S = 15
# History window bounds for `/database-stats/history`: at least one minute,
# at most what the in-memory ring buffer holds (one hour).
_HISTORY_SECONDS_MIN = 60
_HISTORY_SECONDS_MAX = 3600
# One config entry's YAML is a few hundred bytes; 64 KiB leaves room for long
# Cypher while keeping a hostile body from being buffered or parsed.
_CONFIG_PAYLOAD_LIMIT_BYTES = 64 * 1024
# The entry kinds `POST /config/parse` reads back (one per Config editor section).
_CONFIG_PARSE_KINDS = ("tool", "node_type", "relationship")
# `ConfigEditError.code` -> HTTP status. Every other code (invalid, malformed,
# flow_list, unsupported, anchor, unreadable) means the resulting document
# can't be validated or spliced: 422 `invalid`.
_CONFIG_ERROR_STATUS = {
    "not_found": 404,
    "exists": 409,
    "ambiguous": 409,
    "locked": 409,
    "not_regular": 409,
    "stale": 412,
    "io": 500,
}
# How much of the graph-insights summary the Community card shows.
_INSIGHT_COMMUNITY_LIMIT = 8
_INSIGHT_LIST_LIMIT = 6


def _reject_cross_site(request: Request) -> None:
    """Refuse a state-changing request that another site's page initiated.

    The dashboard binds to loopback with no auth, so any page in the same
    browser can reach it; without this, a visited site could POST a path of
    its choosing into the registry (the browser attaches no credentials, but
    none are required here). Both signals are browser-supplied and only
    present on browser traffic: `Sec-Fetch-Site` (absent on older browsers)
    and `Origin` (sent on every cross-origin request, and on same-origin
    POSTs). A client that sends neither -- curl, the CLI, a test -- is not a
    browser being driven by a third-party page and is left alone; this is a
    browser-confused-deputy guard, not an authentication check.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    origin = request.headers.get("origin")
    # Lowercased both sides: hostnames are case-insensitive, and the Host
    # guard in app.py already accepted this Host case-insensitively.
    if origin is not None and origin.lower() != f"{request.url.scheme}://{request.url.netloc}".lower():
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def _config_error(
    status: int, code: str, message: str, scope: dict[str, Any] | None = None, name: str | None = None
) -> HTTPException:
    """A Config write refusal: `detail` is an object so the page can branch on `code`
    (and, on a tool `exists`, offer to replace the taken `name`)."""
    detail: dict[str, Any] = {"code": code, "message": message}
    if scope is not None:
        detail["scope"] = scope
    if name is not None:
        detail["name"] = name
    return HTTPException(status_code=status, detail=detail)


def _reject_cross_site_config(request: Request) -> None:
    try:
        _reject_cross_site(request)
    except HTTPException as exc:
        raise _config_error(403, "forbidden", exc.detail) from exc


def _if_match(request: Request) -> str:
    """The fingerprint the client last saw (`If-Match: "<fingerprint>"`); 428 when missing."""
    value = request.headers.get("if-match")
    if value is None or not value.strip():
        raise _config_error(428, "precondition_required", "If-Match with the file's fingerprint is required")
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def _dry_run_flag(value: str | None) -> bool:
    """`?dry_run=` on DELETE: 1/true/0/false (any case); anything else is refused rather than guessed."""
    if value is None:
        return False
    flag = value.strip().lower()
    if flag in ("1", "true"):
        return True
    if flag in ("0", "false"):
        return False
    raise _config_error(400, "bad_request", "dry_run must be 1, true, 0 or false")


async def _json_payload(request: Request) -> dict:
    """A Config write's JSON object body.

    Strict `application/json` (what a cross-site page can't send without a
    preflight), at most `_CONFIG_PAYLOAD_LIMIT_BYTES` (checked on
    Content-Length, then while reading), and the parsed body must be a JSON
    object.
    """
    media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise _config_error(415, "media_type", "content-type must be application/json")
    content_length = request.headers.get("content-length")
    too_large = _config_error(413, "too_large", "request body is too large")
    if content_length is not None and content_length.isdigit() and int(content_length) > _CONFIG_PAYLOAD_LIMIT_BYTES:
        raise too_large
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _CONFIG_PAYLOAD_LIMIT_BYTES:
            raise too_large
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise _config_error(400, "bad_request", "payload must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise _config_error(400, "bad_request", "payload must be a JSON object")
    return payload


def _body_dry_run(payload: dict) -> bool:
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise _config_error(400, "bad_request", "dry_run must be true or false")
    return dry_run


async def _config_body(request: Request) -> tuple[dict, bool]:
    """The entry mapping and `dry_run` flag of a Config entry write body (entry parsed with `bounded_safe_load` only)."""
    payload = await _json_payload(request)
    text = payload.get("yaml")
    if not isinstance(text, str):
        raise _config_error(400, "bad_request", "yaml must be a string")
    dry_run = _body_dry_run(payload)
    try:
        entry = bounded_safe_load(text)
    except yaml.YAMLError as exc:
        raise _config_error(400, "bad_request", f"malformed YAML: {exc}") from exc
    if not isinstance(entry, dict):
        raise _config_error(400, "bad_request", "yaml must be one mapping (a single entry)")
    return entry, dry_run


_ALL_REPOS_SCOPE = "__all__"

# Worst state wins when the all-repos view merges several repositories.
_SCHEMA_STATE_RANK = ("absent", "disabled", "applied", "never", "pending", "invalid")


def _hash_color(name: str) -> str:
    """A stable `#rrggbb` for a user-declared type with no colour of its own."""
    hue = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:2], "big") % 360
    r, g, b = colorsys.hls_to_rgb(hue / 360, 0.55, 0.65)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def build_router(
    engine: GraphEngine,
    registry: RepoRegistry,
    events: EventBroadcaster,
    query_log: QueryLog | None = None,
    metrics: MetricsHistory | None = None,
    run_exclusive: Callable[[str, Callable[[], Any]], Any] | None = None,
) -> APIRouter:
    """`run_exclusive(repo_id, fn)` is the agent's watcher's: registration's
    scan and stamp run under the repository's batch lock (W4)."""
    router = APIRouter(prefix="/api")
    if run_exclusive is None:
        def run_exclusive(repo_id: str, fn: Callable[[], Any]) -> Any:
            return fn()
    query_log = query_log if query_log is not None else QueryLog()
    # Not started here: `build_app` runs the sampler under the app lifespan.
    # Unstarted, it takes a fresh reading per request.
    metrics = metrics if metrics is not None else MetricsHistory(engine, None)

    def _require_repo(repo_id: str) -> None:
        """Ensure repo_id is registered. Raises 404 if not."""
        if registry.get(repo_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")

    # Load repo issues from file (written by tray app)
    def _get_repo_issues() -> dict[str, str]:
        """Load repo issues from the repo_issues.json file if it exists."""
        settings = get_settings()
        issues_path = settings.registry_db_path.parent / "repo_issues.json"
        if issues_path.exists():
            try:
                return json.loads(issues_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    @router.get("/repos")
    def list_repos() -> dict[str, Any]:
        repo_issues = _get_repo_issues()
        return {
            "repos": [
                {
                    "repo_id": repo.repo_id,
                    "path": str(repo.path),
                    "active": repo.active,
                    "watch_enabled": repo.watch_enabled,
                    "last_indexed": repo.last_indexed,
                    "node_count": queries.count_nodes(engine, repo.repo_id),
                    "issue": repo_issues.get(repo.repo_id),  # Include issue if any
                }
                for repo in registry.list_repos()
            ],
            "issues": repo_issues,  # Also return all issues as a summary
        }

    def _register_repo(path: str, repo_id: str | None) -> dict[str, Any]:
        """Register + initially scan, exactly as `devgraph add <path>` does.

        Blocking (SQLite write, Neo4j round trips, a full file walk, and a
        wait on the repository's batch lock while a live batch or catch-up
        holds it), so it runs in a threadpool -- the event loop also serves the SSE stream the
        dashboard is watching while this runs.
        """
        try:
            record = registry.add_repo(path, repo_id)
        except ValueError as exc:
            # add_repo's own message names the actual problem (missing path,
            # not a git repo, already registered) and contains nothing the
            # caller didn't just supply.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("repo registration failed for %s", path)
            raise HTTPException(status_code=500, detail="registration failed") from exc

        indexed = False
        files_indexed: int | None = None
        warning: str | None = None
        try:
            provision_repository_schema(engine, record.path)
            engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))

            def scan_and_stamp() -> int:
                started = datetime.now(timezone.utc)
                count = full_scan(
                    engine,
                    record.repo_id,
                    record.path,
                    docs_path=record.docs_path,
                    mentions_enabled=record.mentions_enabled,
                )
                registry.mark_indexed(record.repo_id, at=started)
                return count

            # Waits on the repository's batch lock (W4): a watcher that picks
            # the new repository up mid-scan waits too, then catches up from
            # this stamp.
            files_indexed = run_exclusive(record.repo_id, scan_and_stamp)
            indexed = True
        except Exception as exc:
            # Registration already committed to SQLite; an indexing failure
            # (e.g. Neo4j down) must not undo it -- same call as the CLI's,
            # and `devgraph rescan <repo_id>` is the same retry.
            logger.warning("registered %s but the initial scan failed: %s", record.repo_id, exc)
            warning = (
                f"Registered, but the initial scan failed: {exc}. "
                f"Run 'devgraph rescan {record.repo_id}' once Neo4j is reachable."
            )

        # Report what was persisted, not what was asked for: add_repo
        # slugifies the id and can suffix it on collision, and mark_indexed
        # just wrote last_indexed.
        persisted = registry.get(record.repo_id)
        return {
            "repo_id": persisted.repo_id,
            "path": str(persisted.path),
            "active": persisted.active,
            "watch_enabled": persisted.watch_enabled,
            "last_indexed": persisted.last_indexed,
            "registered": True,
            "indexed": indexed,
            "files_indexed": files_indexed,
            "warning": warning,
        }

    @router.post("/repos", status_code=201)
    async def register_repo(request: Request) -> dict[str, Any]:
        _reject_cross_site(request)
        # Strict media type: a browser form post (or a text/plain body) is
        # what a cross-site page can send without a preflight, so anything
        # but JSON is refused before the body is even read.
        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="content-type must be application/json")
        try:
            payload = json.loads(await request.body())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="payload must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")

        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise HTTPException(status_code=400, detail="path is required")
        raw_repo_id = payload.get("repo_id")
        if raw_repo_id is not None and (not isinstance(raw_repo_id, str) or not raw_repo_id.strip()):
            raise HTTPException(status_code=400, detail="repo_id must be a non-empty string")

        return await run_in_threadpool(
            _register_repo,
            raw_path.strip(),
            raw_repo_id.strip() if isinstance(raw_repo_id, str) else None,
        )

    @router.get("/repos/{repo_id}/summary")
    def repo_summary(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        return queries.summary_counts(engine, repo_id)

    @router.get("/repos/{repo_id}/graph")
    def repo_graph(repo_id: str, label: str | None = None, limit: int = _GRAPH_LIMIT_DEFAULT) -> dict[str, Any]:
        _require_repo(repo_id)
        if label is not None and label not in _allowed_labels(repo_id):
            raise HTTPException(status_code=400, detail=f"unknown label: {label}")
        capped_limit = max(1, min(limit, _GRAPH_LIMIT_CEILING))
        nodes, edges = queries.graph_slice(engine, repo_id, label, capped_limit)
        return {
            "nodes": [
                {
                    "data": {
                        "id": n["id"],
                        "label": n["label"],
                        "name": n["name"],
                        "key": identity_key(n["label"], repo_id, n["name"], n["file"]),
                    }
                }
                for n in nodes
            ],
            "edges": [
                {
                    "data": {
                        "id": f"{e['source']}->{e['rel_type']}->{e['target']}",
                        "source": e["source"],
                        "target": e["target"],
                        "type": e["rel_type"],
                    }
                }
                for e in edges
            ],
        }

    def _applied_labels(record: Any) -> tuple[list[str], list[str]]:
        """The applied project labels and relationship types of one repo.

        Reads only the applied schema (never the schema file), honours the
        repo's project-config switch, and re-checks every name against the
        identifier patterns as defence in depth before it can reach Cypher.
        """
        if not record.project_config_enabled:
            return [], []
        applied = engine.read_applied_schema(record.repo_id)
        if applied is None:
            return [], []
        return (
            [x for x in applied["labels"] if LABEL_PATTERN.fullmatch(x)],
            [x for x in applied["relationship_types"] if RELATIONSHIP_TYPE_PATTERN.fullmatch(x)],
        )

    def _repo_schema(record: Any) -> dict[str, Any]:
        """One repository's applied project types, colours and schema state."""
        colors: dict[str, str] = {}
        notices: list[str] = []
        if not record.project_config_enabled:
            return {"labels": [], "rels": [], "colors": colors, "state": "disabled", "notices": notices, "error": None}

        applied = engine.read_applied_schema(record.repo_id)
        current = schema_file_hash(record.path)
        project_labels, project_rels = _applied_labels(record)
        if current == ABSENT_SCHEMA_HASH and (applied is None or applied["hash"] == ABSENT_SCHEMA_HASH):
            state = "absent"
        elif applied is None:
            state = "never"
        elif current == applied["hash"]:
            state = "applied"
        else:
            state = "pending"

        error = None
        try:
            declaration = load_project_schema(record.path, respect_switch=False)
        except ProjectSchemaError as exc:
            state = "invalid"
            error = str(exc)
            notices.append(f"{record.repo_id}: schema file is invalid: {exc}")
            declaration = None
        if declaration is not None:
            for node_type in declaration.node_types:
                if node_type.color:
                    colors[node_type.label] = node_type.color
            for relationship in declaration.relationships:
                if relationship.color:
                    colors[relationship.type] = relationship.color
        if state == "pending":
            notices.append(f"{record.repo_id}: schema file changed since it was applied; rescan to apply it")
        return {"labels": project_labels, "rels": project_rels, "colors": colors, "state": state, "notices": notices,
                "error": error}

    def _config_schema_info(record: Any) -> dict[str, Any]:
        """`_repo_schema` for the Config page, which must render (and answer writes) with Neo4j down.

        Only the driver's own errors are tolerated: the applied state is then
        `unknown`, while what the file alone decides (invalid) still shows.
        """
        from neo4j.exceptions import DriverError, Neo4jError

        try:
            return _repo_schema(record)
        except (DriverError, Neo4jError) as exc:
            logger.debug("schema state of %s unavailable: %s", record.repo_id, exc)
        try:
            load_project_schema(record.path, respect_switch=False)
        except ProjectSchemaError as exc:
            return {"state": "invalid", "error": str(exc)}
        return {"state": "unknown", "error": None}

    def _scope_records(repo_id: str) -> list[Any]:
        """The registered repos a scope covers: every repo for `__all__`, else one."""
        if repo_id == _ALL_REPOS_SCOPE:
            return registry.list_repos()
        _require_repo(repo_id)
        return [registry.get(repo_id)]

    def _allowed_labels(repo_id: str) -> list[str]:
        """Built-in labels plus the applied project labels of every repo in scope.

        The one source of truth the schema, graph and search routes share; only
        labels in this list may be interpolated into Cypher.
        """
        labels = list(NODE_LABELS)
        for record in _scope_records(repo_id):
            labels += [x for x in _applied_labels(record)[0] if x not in labels]
        return labels

    @router.get("/repos/{repo_id}/schema")
    def repo_schema(repo_id: str) -> dict[str, Any]:
        records = _scope_records(repo_id)

        project_labels: list[str] = []
        project_rels: list[str] = []
        colors: dict[str, str] = {}
        notices: list[str] = []
        state = "absent"
        counts = queries.node_counts_by_label(engine, [r.repo_id for r in records])
        for record in records:
            info = _repo_schema(record)
            project_labels += [x for x in info["labels"] if x not in project_labels]
            project_rels += [x for x in info["rels"] if x not in project_rels]
            for name, color in info["colors"].items():
                colors.setdefault(name, color)  # first registered repo wins
            notices += info["notices"]
            if _SCHEMA_STATE_RANK.index(info["state"]) > _SCHEMA_STATE_RANK.index(state):
                state = info["state"]

        builtin_labels = set(NODE_LABELS)
        builtin_rels = set(RELATIONSHIP_TYPES)
        node_types = [
            {"label": label, "origin": "builtin", "color": None, "count": counts.get(label, 0)}
            for label in NODE_LABELS
        ] + [
            {"label": label, "origin": "project", "color": colors.get(label) or _hash_color(label),
             "count": counts.get(label, 0)}
            for label in project_labels
            if label not in builtin_labels
        ]
        relationship_types = [
            {"type": rel, "origin": "builtin", "color": None} for rel in RELATIONSHIP_TYPES
        ] + [
            {"type": rel, "origin": "project", "color": colors.get(rel) or _hash_color(rel)}
            for rel in project_rels
            if rel not in builtin_rels
        ]
        return {
            "node_types": node_types,
            "relationship_types": relationship_types,
            "schema_state": state,
            "notices": notices,
        }

    def _insights_payload(repo_id: str) -> dict[str, Any]:
        summary = read_insights(engine, repo_id)
        if summary is None:
            return {"computed": False}
        return {
            "computed": True,
            "computed_at": summary.get("computed_at"),
            "node_count": summary.get("node_count"),
            "community_count": summary.get("community_count"),
            "modularity": summary.get("modularity"),
            "communities": summary["communities"][:_INSIGHT_COMMUNITY_LIMIT],
            "key_nodes": top_nodes(engine, repo_id, "pagerank", _INSIGHT_LIST_LIMIT),
            "bridges": top_nodes(engine, repo_id, "betweenness", _INSIGHT_LIST_LIMIT),
        }

    @router.get("/repos/{repo_id}/insights")
    def repo_insights(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        try:
            return _insights_payload(repo_id)
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            logger.debug("graph insights unavailable for %s: %s", repo_id, exc)
            raise HTTPException(status_code=503, detail="graph unavailable") from exc

    @router.post("/repos/{repo_id}/insights")
    async def recompute_repo_insights(request: Request, repo_id: str) -> dict[str, Any]:
        """Recompute now. The only other write the dashboard makes besides the
        layout and registration, and it only replaces derived properties."""
        _reject_cross_site(request)
        _require_repo(repo_id)
        try:
            summary = await run_in_threadpool(refresh_insights, engine, repo_id, blocking=False)
            if summary is None:
                raise HTTPException(status_code=409, detail="insights are already being computed for this repository")
            events.publish({"type": "insights_refreshed", "repo_id": repo_id})
            return await run_in_threadpool(_insights_payload, repo_id)
        except HTTPException:
            raise
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            logger.warning("graph insights recompute failed for %s", repo_id, exc_info=True)
            raise HTTPException(status_code=503, detail="graph unavailable") from exc

    @router.get("/repos/{repo_id}/search")
    def repo_search(repo_id: str, q: str, max_results: int = 15) -> dict[str, Any]:
        if repo_id == _ALL_REPOS_SCOPE:  # search is per-repo; `_scope_records` below does the one lookup
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        project_labels = [x for x in _allowed_labels(repo_id) if x not in NODE_LABELS]
        return {"results": queries.search_components(engine, repo_id, q, max_results, project_labels)}

    # The canvas's repo selector has an "All Repos" option that is not a
    # registered repo, and its layout is worth persisting like any other
    # view's. `_require_repo` is what keeps a repo_id safe to use as a
    # filename (it can only ever be an id the registry itself issued), so
    # this reserved id is matched by exact equality rather than being folded
    # into a pattern that would reopen that.
    _ALL_REPOS_LAYOUT_ID = _ALL_REPOS_SCOPE

    def _require_layout_scope(repo_id: str) -> None:
        if repo_id != _ALL_REPOS_LAYOUT_ID:
            _require_repo(repo_id)

    @router.get("/repos/{repo_id}/layout")
    def get_repo_layout(repo_id: str) -> dict[str, Any]:
        _require_layout_scope(repo_id)
        return load_layout(repo_id)

    @router.put("/repos/{repo_id}/layout")
    async def put_repo_layout(repo_id: str, request: Request) -> dict[str, Any]:
        _reject_cross_site(request)
        _require_layout_scope(repo_id)
        # Declaring a `payload: dict[str, Any]` parameter (the previous
        # shape) makes Starlette buffer and json-decode the entire body
        # before this function ever runs, so the size check below couldn't
        # actually stop that work -- only the eventual write to disk. Taking
        # the raw `Request` instead means the body is only read here, after
        # Content-Length has already rejected an oversized request; a caller
        # that omits or lies about the header (chunked transfer, no header at
        # all) still hits the len(body) check right after the read, before
        # any JSON parsing happens.
        content_length = request.headers.get("content-length")
        if content_length is not None and content_length.isdigit() and int(content_length) > _LAYOUT_PAYLOAD_LIMIT_BYTES:
            raise HTTPException(status_code=413, detail="layout payload too large")
        body = await request.body()
        if len(body) > _LAYOUT_PAYLOAD_LIMIT_BYTES:
            raise HTTPException(status_code=413, detail="layout payload too large")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="payload must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        save_layout(repo_id, payload)
        return {"ok": True}

    @router.post("/cypher")
    def run_cypher(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Server-side Cypher execution for the dashboard's Cypher console.

        Replaces the prototype's direct browser->Neo4j HTTP connection (see
        Implementation Plan #5 rebuild) -- the browser no longer holds or
        sends Neo4j credentials. Trust boundary is the same one the rest of
        the dashboard already relies on: loopback-only by default (see the
        Network/access settings pane), not a second gate layered on top of
        `Settings.enable_run_cypher` (that flag is documented as
        agent/MCP-only and is orthogonal to a human typing a query into the
        dashboard they're already running locally).

        `record: true` opts the call into the query log/rate telemetry --
        only the console's own explicit Run/isolate/history actions set it,
        so background polling (repo list, topology counts, live glow preview
        on every keystroke) doesn't flood the log.

        The query is arbitrary Cypher, writes included, so this gets the same
        cross-site check as the other state-changing routes.
        """
        _reject_cross_site(request)
        query = (payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        params = payload.get("params") or {}
        repo_id = payload.get("repo_id")
        should_record = bool(payload.get("record"))

        start = time.monotonic()
        try:
            result = engine.run_cypher_graph(query, params)
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            if should_record:
                query_log.record(
                    repo_id=repo_id, query=query, duration_ms=(time.monotonic() - start) * 1000, ok=False
                )
            return {"results": [], "errors": [{"code": exc.__class__.__name__, "message": str(exc)}]}

        if should_record:
            query_log.record(
                repo_id=repo_id, query=query, duration_ms=(time.monotonic() - start) * 1000, ok=True
            )
        return {"results": [result], "errors": []}

    @router.get("/database-stats")
    def database_stats() -> dict[str, Any]:
        """Latest Database & memory snapshot (see dashboard/db_metrics.py).

        Read-only and fixed-query. Always 200, never driver error text: an
        unreachable database, a missing procedure, or a denied role are all
        the same thing to the card -- no reading -- and each group says so
        through its own `available` flag.
        """
        return metrics.latest()

    @router.get("/database-stats/history")
    def database_stats_history(seconds: int = _HISTORY_SECONDS_MAX) -> dict[str, Any]:
        window = max(_HISTORY_SECONDS_MIN, min(seconds, _HISTORY_SECONDS_MAX))
        return {"interval_s": metrics.interval_s, "samples": metrics.since(window)}

    @router.get("/query-log")
    def get_query_log(limit: int = 100) -> dict[str, Any]:
        return {"entries": query_log.recent(max(1, min(limit, 500)))}

    @router.get("/query-rate")
    def get_query_rate(span: int = 3600, interval: int = 60) -> dict[str, Any]:
        return {"buckets": query_log.rate(max(1, span), max(1, interval))}

    def _config_scope(scope: str) -> dict[str, Any]:
        """One Config page block: the global store (`__global__`) or one active registered repo."""
        records = registry.list_repos(active_only=True)
        if scope == GLOBAL_SCOPE:
            return build_global(records)
        record = next((r for r in records if r.repo_id == scope), None)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {scope}")
        return build_project(record, _config_schema_info, conflicts=schema_conflicts(registry.list_repos()))

    @router.get("/config")
    def get_config() -> dict[str, Any]:
        return build_config(
            registry.list_repos(active_only=True), _config_schema_info,
            conflicts=schema_conflicts(registry.list_repos()),
        )

    @router.get("/config/{scope}")
    def get_config_scope(scope: str) -> dict[str, Any]:
        return _config_scope(scope)

    @router.post("/config/parse")
    async def parse_config_entry(request: Request) -> dict[str, Any]:
        """Read hand-edited entry YAML back for the form: `{entry}` (`form_entry`, so null when JSON
        can't carry it exactly) or `{entry: null, error}`. Read-only and unvalidated: no file, no
        fingerprint, no model check -- the dry run and the write stay authoritative."""
        _reject_cross_site_config(request)
        payload = await _json_payload(request)
        if payload.get("kind") not in _CONFIG_PARSE_KINDS:
            raise _config_error(400, "bad_request", "kind must be tool, node_type or relationship")
        text = payload.get("yaml")
        if not isinstance(text, str):
            raise _config_error(400, "bad_request", "yaml must be a string")
        try:
            entry = bounded_safe_load(text)
        except yaml.YAMLError as exc:
            return {"entry": None, "error": f"malformed YAML: {exc}"}
        if not isinstance(entry, dict):
            return {"entry": None, "error": "yaml must be one mapping (a single entry)"}
        return {"entry": form_entry(entry)}

    # --- Config page writes ------------------------------------------------------------------
    # Every write: cross-site refusal, then the scope (only `__global__` or an
    # active registered repo -- never a path), then the body, then If-Match,
    # then `devgraph.config.edits` (fingerprint CAS under a per-path lock,
    # whole-document validation, symlink refusal, atomic write). No git.

    def _write_record(scope: str, schema: bool = False) -> Any:
        """The target repo record (None for the global store); 404 for anything else."""
        if scope == GLOBAL_SCOPE and not schema:
            return None
        record = None if scope == GLOBAL_SCOPE else registry.get(scope)
        if record is None or not record.active:
            raise _config_error(404, "not_found", f"unknown scope: {scope}")
        root = Path(record.path)
        if root.resolve() != root:  # the registered directory was replaced by (or moved under) a symlink
            raise _config_error(409, "not_regular", f"the registered path of {scope} is now a symlink; re-register it")
        if not root.is_dir():
            raise _config_error(404, "not_found", f"repository directory for {scope} is missing")
        return record

    def _require_section(section: str) -> None:
        if section not in edits.SCHEMA_SECTIONS:
            raise _config_error(404, "not_found", f"unknown schema section: {section}")

    def _apply_edit(
        scope: str, record: Any, kind: str, op: Callable[[Path | None], edits.EditResult], created: bool
    ) -> JSONResponse:
        root = None if record is None else Path(record.path).resolve()
        if kind == "tools":
            path = edits.tools_path(root)
            effect = edits.tools_effect_note(root, record)
        else:
            path = root / SCHEMA_FILENAME
            effect = edits.schema_effect_note(root, record)
        try:
            result = op(root)
        except edits.ConfigEditError as exc:
            status = _CONFIG_ERROR_STATUS.get(exc.code, 422)
            if status == 500:
                logger.warning("config write to %s failed: %s", path, exc.message)
                raise _config_error(500, "io", f"could not write {path.name}") from exc
            code = exc.code if status != 422 else "invalid"
            message = scrub(exc.message, path, root)
            if code == "not_regular":  # a symlink message names the link target, which can be outside the repo
                message = f"{path.name} is a symlink or not a regular file; fix it by hand"
            raise _config_error(status, code, message, _config_scope(scope), exc.name) from exc
        notes = [*result.notes, effect]
        warnings = list(result.warnings)
        if kind == "schema":  # reads other repositories' files only; deletes and resets introduce none
            warnings += introduced_conflicts(registry.list_repos(), scope, result.before, result.after)
        block = _config_scope(scope)
        part = block["tools"] if kind == "tools" else block["schema"]
        if result.written:
            if result.removed is None:
                notes.append(f"Written to {path.name}; not committed.")
            elif root is None:
                notes.append("Emptied the global tools store.")
            else:
                notes.append(f"Deleted {path.name}; not staged or committed.")
            # The fingerprint edits.py took under the lock, of exactly what was written: if the file
            # changed again since, the client's next write is a 412 rather than a blind overwrite.
            part["fingerprint"] = result.fingerprint
        # A reset's dry run reports the fingerprint of the bytes it listed, taken in the same read under
        # the lock, not the scope re-read below: the confirm sends it back, so a file that changed after
        # it was listed is a 412 rather than a reset of content nobody saw.
        fingerprint = result.fingerprint if result.removed is not None else part["fingerprint"]
        content: dict[str, Any] = {
            "ok": True,
            "written": result.written,
            "file": path.name,
            "fingerprint": fingerprint,
            "warnings": [scrub(w, path, root) for w in warnings],
            "notes": [scrub(n, path, root) for n in notes],
            "scope": block,
        }
        if result.removed is not None:
            content["removed"] = result.removed
        if kind == "tools":  # a tools write can change other scopes' badges ("Overridden in")
            content["global"] = block if scope == GLOBAL_SCOPE else _config_scope(GLOBAL_SCOPE)
        return JSONResponse(status_code=201 if created and result.written else 200, content=content)

    @router.post("/config/{scope}/tools")
    async def add_config_tool(scope: str, request: Request) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope)
        entry, dry_run = await _config_body(request)
        expected = _if_match(request)
        return await run_in_threadpool(
            _apply_edit, scope, record, "tools",
            lambda root: edits.add_tool(root, entry, expected_fingerprint=expected, dry_run=dry_run), True,
        )

    @router.put("/config/{scope}/tools/{name}")
    async def replace_config_tool(scope: str, name: str, request: Request) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope)
        entry, dry_run = await _config_body(request)
        expected = _if_match(request)
        return await run_in_threadpool(
            _apply_edit, scope, record, "tools",
            lambda root: edits.replace_tool(root, name, entry, expected_fingerprint=expected, dry_run=dry_run), False,
        )

    @router.delete("/config/{scope}/tools/{name}")
    async def delete_config_tool(scope: str, name: str, request: Request, dry_run: str | None = None) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope)
        expected = _if_match(request)
        dry = _dry_run_flag(dry_run)
        return await run_in_threadpool(
            _apply_edit, scope, record, "tools",
            lambda root: edits.delete_tool(root, name, expected_fingerprint=expected, dry_run=dry), False,
        )

    @router.post("/config/{scope}/reset/tools")
    async def reset_config_tools(scope: str, request: Request) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope)
        dry = _body_dry_run(await _json_payload(request))
        expected = _if_match(request)
        return await run_in_threadpool(
            _apply_edit, scope, record, "tools",
            lambda root: edits.reset_tools(root, record=record, expected_fingerprint=expected, dry_run=dry), False,
        )

    @router.post("/config/{scope}/reset/schema")
    async def reset_config_schema(scope: str, request: Request) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope, schema=True)
        dry = _body_dry_run(await _json_payload(request))
        expected = _if_match(request)
        return await run_in_threadpool(
            _apply_edit, scope, record, "schema",
            lambda root: edits.reset_schema(root, record=record, expected_fingerprint=expected, dry_run=dry), False,
        )

    @router.delete("/config/{scope}/trust/tools")
    async def revoke_config_tools_trust(scope: str, request: Request) -> JSONResponse:
        """Revoke trust in a repository's project tools. There is deliberately no route that grants it:
        approving a tools file (`devgraph config tools trust`) is CLI-only."""
        _reject_cross_site_config(request)
        record = _write_record(scope, schema=True)  # a repository; never the global store

        def apply() -> JSONResponse:
            revoked = record.project_tools_sha256 is not None
            if revoked:
                try:
                    registry.set_project_tools_sha256(scope, None)
                except ValueError as exc:  # removed since the scope check
                    raise _config_error(404, "not_found", f"unknown scope: {scope}") from exc
                except sqlite3.Error as exc:
                    logger.warning("revoking project tools trust for %s failed: %s", scope, exc)
                    raise _config_error(500, "io", "could not update the registry") from exc
                notes = [f"Revoked trust in {scope}'s project tools; running MCP sessions stop serving them within 2 seconds."]
            else:
                notes = [f"Project tools for {scope} are not trusted; nothing to revoke."]
            notes.append(f"Trusting them again is done in a terminal: `{trust_command(scope)}`.")
            return JSONResponse(content={
                "ok": True,
                "written": revoked,
                "notes": notes,
                "scope": _config_scope(scope),
                "global": _config_scope(GLOBAL_SCOPE),
            })

        return await run_in_threadpool(apply)

    @router.put("/config/{scope}/project-config")
    async def set_config_project_config(scope: str, request: Request) -> JSONResponse:
        """Switch a repository's project config on or off. Names the end state, so it is idempotent: no If-Match."""
        _reject_cross_site_config(request)
        record = _write_record(scope, schema=True)
        payload = await _json_payload(request)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise _config_error(400, "bad_request", "enabled must be true or false")
        dry = _body_dry_run(payload)
        word = "enabled" if enabled else "disabled"

        def apply() -> JSONResponse:
            changed = record.project_config_enabled != enabled
            warnings, notes = edits.project_config_change(record, enabled) if changed else ([], [])
            if not changed:
                notes = [f"Project config for {scope} is already {word}."]
            elif not dry:
                try:
                    registry.set_project_config_enabled(scope, enabled)
                except ValueError as exc:  # removed since the scope check
                    raise _config_error(404, "not_found", f"unknown scope: {scope}") from exc
                except sqlite3.Error as exc:
                    logger.warning("project config switch for %s failed: %s", scope, exc)
                    raise _config_error(500, "io", "could not update the registry") from exc
            return JSONResponse(content={
                "ok": True,
                "written": changed and not dry,
                "changed": changed,
                "enabled": enabled,
                "warnings": warnings,
                "notes": notes,
                "scope": _config_scope(scope),
                "global": _config_scope(GLOBAL_SCOPE),
            })

        return await run_in_threadpool(apply)

    @router.post("/config/{scope}/schema/{section}")
    async def add_config_schema_entry(scope: str, section: str, request: Request) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope, schema=True)
        _require_section(section)
        entry, dry_run = await _config_body(request)
        expected = _if_match(request)

        def op(root: Path) -> edits.EditResult:
            if edits.entry_section(entry) != section:
                raise edits.ConfigEditError(f"the new entry must be a {edits.SCHEMA_SECTIONS[section][1]}", "invalid")
            return edits.add_schema_entry(root, entry, record=record, expected_fingerprint=expected, dry_run=dry_run)

        return await run_in_threadpool(_apply_edit, scope, record, "schema", op, True)

    @router.put("/config/{scope}/schema/{section}/{name}")
    async def replace_config_schema_entry(scope: str, section: str, name: str, request: Request) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope, schema=True)
        _require_section(section)
        entry, dry_run = await _config_body(request)
        expected = _if_match(request)
        return await run_in_threadpool(
            _apply_edit, scope, record, "schema",
            lambda root: edits.replace_schema_entry(
                root, name, entry, node_type=section == "node_types", relationship=section == "relationships",
                record=record, expected_fingerprint=expected, dry_run=dry_run,
            ),
            False,
        )

    @router.delete("/config/{scope}/schema/{section}/{name}")
    async def delete_config_schema_entry(
        scope: str, section: str, name: str, request: Request, dry_run: str | None = None
    ) -> JSONResponse:
        _reject_cross_site_config(request)
        record = _write_record(scope, schema=True)
        _require_section(section)
        expected = _if_match(request)
        dry = _dry_run_flag(dry_run)
        return await run_in_threadpool(
            _apply_edit, scope, record, "schema",
            lambda root: edits.delete_schema_entry(
                root, name, node_type=section == "node_types", relationship=section == "relationships",
                record=record, expected_fingerprint=expected, dry_run=dry,
            ),
            False,
        )

    @router.get("/mcp-tools")
    def get_mcp_tools() -> list[dict[str, Any]]:
        # Imported lazily: devgraph.mcp.server pulls in devgraph.agent (for
        # lifecycle), and devgraph.agent.tray imports dashboard.app at module
        # scope -- importing mcp.server at routes.py's own module scope would
        # create app -> routes -> mcp.server -> agent -> tray -> app.
        from devgraph.mcp.server import _TOOL_CATALOG

        settings = get_settings()
        catalog = _TOOL_CATALOG if settings.enable_run_cypher else [
            t for t in _TOOL_CATALOG if t["name"] != "run_cypher"
        ]
        return [
            {**tool, "description": (inspect.getdoc(getattr(devgraph_tools, tool["name"], None)) or "").split("\n")[0]}
            for tool in catalog
        ]

    @router.get("/mcp-telemetry")
    def get_mcp_telemetry(limit: int = 100) -> dict[str, Any]:
        """Metadata-only record of MCP tool calls, newest first.

        Distinct from `/query-log`, which is this dashboard's own Cypher
        console and nothing else: this reads back what connected MCP clients
        ran in their own separate server processes, from the local store
        those processes append to (devgraph/mcp/server.py). Same `{entries:
        [...]}` shape and `limit` capping as `/query-log`.
        """
        # Imported lazily, for the same import cycle as /mcp-tools above.
        from devgraph.mcp.server import read_tool_telemetry

        return {"entries": read_tool_telemetry(max(1, min(limit, 500)))}

    @router.get("/settings")
    def get_dashboard_settings() -> dict[str, Any]:
        s = get_settings()
        return {
            "neo4j_uri": s.neo4j_uri,
            "neo4j_user": s.neo4j_user,
            "dashboard_host": s.dashboard_host,
            "dashboard_port": s.dashboard_port,
            "enable_run_cypher": s.enable_run_cypher,
            "allow_cross_repo": s.allow_cross_repo,
            "telemetry_enabled": s.telemetry_enabled,
            "cloud_sync": s.cloud_sync,
            "mentions_ambiguous_mode": s.mentions_ambiguous_mode,
            "git_recency_track_author": s.git_recency_track_author,
            "registry_db_path": str(s.registry_db_path),
            "watch_debounce_ms": s.watch_debounce_ms,
            "health_check_interval_s": s.health_check_interval_s,
        }

    @router.get("/repos/{repo_id}/git-log")
    def repo_git_log(repo_id: str, limit: int = 60) -> list[dict[str, Any]]:
        _require_repo(repo_id)
        rec = registry.get(repo_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        repo_path = rec.path
        try:
            return get_git_log(repo_path, max(1, min(limit, 300)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"git log failed: {exc}") from exc

    @router.get("/repos/{repo_id}/git-status")
    def repo_git_status(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        rec = registry.get(repo_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        repo_path = rec.path
        try:
            return get_git_status(repo_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"git status failed: {exc}") from exc

    @router.get("/events")
    async def stream_events(request: Request) -> StreamingResponse:
        queue = events.subscribe()

        async def event_source():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        # Idle-timeout keep-alive so intermediary buffering
                        # doesn't silently drop a quiet connection.
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                events.unsubscribe(queue)

        return StreamingResponse(event_source(), media_type="text/event-stream")

    return router
