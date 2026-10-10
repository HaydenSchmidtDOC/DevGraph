"""Measure the call graph DevGraph builds for a repository (M1-M6).

See docs/superpowers/specs/2026-10-10-python-call-resolution-design.md. Prints
one JSON object: multi-target CALLS (M1), cross-file resolved CALLS with no
import (M2), IMPORTS against an independent ground truth (M3), the PageRank top
ten (M4), a fixed sample of DevGraph call sites plus caller recall against an
`ast` scan (M5), and cost (M6).

Usage:
    <venv python> scripts/measure_call_graph.py <repo_id> [--root PATH] [--scan] [--cleanup]
    <venv python> scripts/measure_call_graph.py --fixture <lang>

`--fixture` measures one of the ground-truth fixtures under
tests/fixtures/callgraph instead (see `measure_fixture` and
docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md): it
copies the fixture to a temporary folder, scans it into a scratch repository,
compares the graph with the fixture's expected.json and deletes the scratch
repository again.

`--scan` deletes `<repo_id>` and full-scans `--root` (default: the current
directory) into it first, timing the scan and an incremental save of
`devgraph/config/__init__.py`; `--cleanup` deletes `<repo_id>` afterwards. The
M5 call-site sample is DevGraph's own code, so it is only meaningful there.
Neo4j is reached through DEVGRAPH_NEO4J_URI/USER/PASSWORD, defaulting to the
local development instance.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import posixpath
import shutil
import sys
import tempfile
import time
import uuid
from importlib.machinery import PathFinder
from pathlib import Path

from devgraph.analytics.insights import refresh_insights
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.indexer.walk import indexable_paths

# M1/M2 baselines measured on DevGraph before call resolution (see the spec).
_M1_BASELINE_MULTI = 23_159

# M5: (tier, caller, caller file, callee, callee file, expected confidence).
# None as the expected confidence means the edge must be absent.
_SITES = [
    ("same-file", "full_scan", "devgraph/indexer/dispatch.py", "index_paths", "devgraph/indexer/dispatch.py", "resolved"),
    ("same-file", "catch_up", "devgraph/indexer/dispatch.py", "prune_stale_files", "devgraph/indexer/dispatch.py",
     "resolved"),
    ("nested def", "index_paths", "devgraph/indexer/dispatch.py", "index_one", "devgraph/indexer/dispatch.py",
     "resolved"),
    ("nested def", "index_one", "devgraph/indexer/dispatch.py", "_index_single_path", "devgraph/indexer/dispatch.py",
     "resolved"),
    ("from-import", "_index_single_path", "devgraph/indexer/dispatch.py", "extract_python_file",
     "devgraph/indexer/python/extractor.py", "resolved"),
    ("from-import", "full_scan", "devgraph/indexer/dispatch.py", "check_repo_root", "devgraph/indexer/walk.py",
     "resolved"),
    ("alias", "doctor", "devgraph/cli/main.py", "project_schema_findings", "devgraph/config/schema_findings.py",
     "resolved"),
    ("alias", "_set_project_config", "devgraph/cli/main.py", "project_config_notes", "devgraph/config/edits.py",
     "resolved"),
    ("alias", "index_paths", "devgraph/indexer/dispatch.py", "index_file", "devgraph/indexer/docs/extractor.py",
     "resolved"),
    ("module attribute", "tray_start", "devgraph/cli/main.py", "start_tray_if_not_running",
     "devgraph/agent/lifecycle.py", "resolved"),
    ("module attribute", "tray_stop", "devgraph/cli/main.py", "read_tray_pid", "devgraph/agent/lifecycle.py",
     "resolved"),
    ("re-export", "tray_pid_path", "devgraph/agent/lifecycle.py", "get_settings", "devgraph/config/settings.py",
     "package"),
    ("re-export", "index_paths", "devgraph/indexer/dispatch.py", "get_settings", "devgraph/config/settings.py",
     "package"),
    ("self", "add_repo", "devgraph/registry/store.py", "_touch_change_marker", "devgraph/registry/store.py",
     "resolved"),
    ("self", "set_docs_path", "devgraph/registry/store.py", "get", "devgraph/registry/store.py", "resolved"),
    ("annotated receiver", "_relink_name_refs", "devgraph/indexer/dispatch.py", "find_name_refs",
     "devgraph/graph/engine.py", "resolved"),
    ("annotated receiver", "_relink_name_refs", "devgraph/indexer/dispatch.py", "upsert_relationships",
     "devgraph/graph/engine.py", "resolved"),
    ("annotated receiver", "full_scan", "devgraph/indexer/dispatch.py", "set_index_format",
     "devgraph/graph/engine.py", "resolved"),
    ("untyped receiver", "index_file", "devgraph/indexer/python/extractor.py", "upsert_nodes",
     "devgraph/graph/engine.py", "name"),
    ("untyped receiver", "index_file", "devgraph/indexer/jsts/extractor.py", "upsert_relationships",
     "devgraph/graph/engine.py", "name"),
    ("stoplisted untyped receiver", "fetch", "devgraph/indexer/pr_issues/extractor.py", "get",
     "devgraph/registry/store.py", None),
]
_RECALL_NAMES = [
    "upsert_nodes", "upsert_relationships", "get_settings", "index_paths", "run_cypher", "full_scan", "list_repos",
]
_PAGERANK_WANTED = {"GraphEngine", "run_cypher", "get_settings", "upsert_relationships", "devgraph/graph/engine.py"}
_STOPLIST_PROBE = {"get", "items", "keys", "values", "join", "append", "extend", "split", "strip", "format", "update",
                   "pop", "read", "write"}


def _rows(engine: GraphEngine, query: str, **params) -> list[dict]:
    return engine.run_cypher(query, params)


def measure_m1(engine: GraphEngine, repo_id: str) -> dict:
    totals = {
        str(row["conf"]): row["n"]
        for row in _rows(engine, "MATCH (a {repo_id: $r})-[x:CALLS]->() RETURN x.confidence AS conf, count(*) AS n",
                         r=repo_id)
    }
    multi = {
        str(row["conf"]): row["n"]
        for row in _rows(
            engine,
            "MATCH (a {repo_id: $r})-[x:CALLS]->(b) WITH a, b.name AS n, x.confidence AS conf, count(*) AS c "
            "WHERE c > 1 RETURN conf, sum(c) AS n",
            r=repo_id,
        )
    }
    resolved = totals.get("resolved", 0)
    self_loops = _rows(engine, "MATCH (a {repo_id: $r})-[x:CALLS]->(a) RETURN count(x) AS n", r=repo_id)[0]["n"]
    return {
        "calls_by_confidence": totals,
        "self_loops": self_loops,
        "multi_target_by_confidence": multi,
        "multi_target_total": sum(multi.values()),
        "resolved_multi_share": round(multi.get("resolved", 0) / resolved, 4) if resolved else None,
        "total_vs_baseline": round(sum(multi.values()) / _M1_BASELINE_MULTI, 4),
    }


def measure_m2(engine: GraphEngine, repo_id: str) -> dict:
    rows = _rows(
        engine,
        "MATCH (a {repo_id: $r})-[x:CALLS]->(b {repo_id: $r}) "
        "WITH a, b, x, coalesce(a.file, a.source_file) AS af, b.file AS bf "
        "WHERE af IS NOT NULL AND bf IS NOT NULL AND af <> bf "
        "  AND NOT EXISTS { MATCH (:Module {repo_id: $r, name: af})-[:IMPORTS]->(m:Module {repo_id: $r}) "
        "    WHERE m.name = bf OR (m.name ENDS WITH '__init__.py' "
        "      AND bf STARTS WITH substring(m.name, 0, size(m.name) - size('__init__.py'))) } "
        "RETURN x.confidence AS conf, count(*) AS n",
        r=repo_id,
    )
    return {"cross_file_without_import_by_confidence": {str(row["conf"]): row["n"] for row in rows}}


def _spec_file(parts: list[str], search: list[str], root: Path) -> tuple[str | None, list[str] | None]:
    """The repo-relative `.py` file a dotted module resolves to from `search`,
    and its package directories (None when it is not a package)."""
    spec = None
    paths = search
    for i in range(len(parts)):
        spec = PathFinder.find_spec(".".join(parts[: i + 1]), paths)
        if spec is None:
            return None, None
        if i < len(parts) - 1:
            if not spec.submodule_search_locations:
                return None, None
            paths = list(spec.submodule_search_locations)
    origin = spec.origin if spec is not None else None
    file = None
    if origin and origin.endswith(".py"):
        try:
            file = Path(origin).resolve().relative_to(root).as_posix()
        except ValueError:
            file = None
    package = list(spec.submodule_search_locations) if spec and spec.submodule_search_locations else None
    return file, package


def import_ground_truth(root: Path, files: list[str]) -> tuple[set[tuple[str, str]], int]:
    """(importer, imported file) for every import in `files`, resolved with
    importlib's PathFinder against the importer's ancestor directories, and
    how many imported modules resolved under more than one of them."""
    truth: set[tuple[str, str]] = set()
    multi = 0
    for rel in files:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        parts = rel.split("/")[:-1]
        roots = [str(root / "/".join(parts[:i])) if i else str(root) for i in range(len(parts) + 1)]
        for node in ast.walk(tree):
            found: set[str] = set()
            if isinstance(node, ast.Import):
                for alias in node.names:
                    hits = {f for r in roots if (f := _spec_file(alias.name.split("."), [r], root)[0])}
                    multi += len(hits) > 1
                    found |= hits
            elif isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names if a.name != "*"]
                if node.level:
                    base = parts[: len(parts) - (node.level - 1)] if node.level - 1 <= len(parts) else []
                    base_dir = str(root / "/".join(base)) if base else str(root)
                    if node.module:
                        mod = node.module.split(".")
                        file, _pkg = _spec_file(mod, [base_dir], root)
                        found |= {file} if file else set()
                        found |= {f for n in names if (f := _spec_file(mod + [n], [base_dir], root)[0])}
                    else:
                        init = Path(base_dir) / "__init__.py"
                        if init.is_file():
                            found.add(init.resolve().relative_to(root).as_posix())
                        found |= {f for n in names if (f := _spec_file([n], [base_dir], root)[0])}
                elif node.module:
                    mod = node.module.split(".")
                    hits = set()
                    for r in roots:
                        file, _pkg = _spec_file(mod, [r], root)
                        hits |= {file} if file else set()
                        hits |= {f for n in names if (f := _spec_file(mod + [n], [r], root)[0])}
                    found |= hits
            truth |= {(rel, f) for f in found if f != rel}
    return truth, multi


def measure_m3(engine: GraphEngine, repo_id: str, root: Path, py_files: list[str]) -> dict:
    graph = {
        (row["a"], row["b"])
        for row in _rows(
            engine, "MATCH (a:Module {repo_id: $r})-[:IMPORTS]->(b:Module {repo_id: $r}) RETURN a.name AS a, b.name AS b",
            r=repo_id,
        )
        if row["a"].endswith(".py")
    }
    truth, multi = import_ground_truth(root, py_files)
    both = graph & truth

    def importers(target: str) -> int:
        return len({a for a, b in graph if b == target})

    return {
        "graph_imports": len(graph),
        "truth_imports": len(truth),
        "recall": round(len(both) / len(truth), 4) if truth else None,
        "precision": round(len(both) / len(graph), 4) if graph else None,
        "importers_of_config_init": importers("devgraph/config/__init__.py"),
        "importers_of_lifecycle": importers("devgraph/agent/lifecycle.py"),
        "truth_importers_of_lifecycle": len({a for a, b in truth if b == "devgraph/agent/lifecycle.py"}),
        "modules_resolving_under_several_roots": multi,
        "missed_sample": sorted(truth - graph)[:10],
        "extra_sample": sorted(graph - truth)[:10],
    }


def measure_m4(engine: GraphEngine, repo_id: str) -> dict:
    refresh_insights(engine, repo_id)
    top = _rows(
        engine,
        "MATCH (n {repo_id: $r}) WHERE n.insight_pagerank IS NOT NULL "
        "RETURN labels(n)[0] AS label, n.name AS name, coalesce(n.file, n.source_file) AS file, "
        "n.insight_pagerank AS rank ORDER BY rank DESC, name LIMIT 10",
        r=repo_id,
    )
    names = [row["name"] for row in top]
    return {
        "top10": [[row["label"], row["name"], row["file"]] for row in top],
        "functions_under_tests": [n for n, row in zip(names, top) if (row["file"] or "").startswith("tests/")],
        "stoplisted": [n for n in names if n in _STOPLIST_PROBE],
        "wanted_present": sorted(_PAGERANK_WANTED & set(names)),
    }


def _call_sites(root: Path, files: list[str], names: set[str]) -> dict[str, set[tuple[str, str]]]:
    """(caller, file) of every call to one of `names`, by an `ast` scan: the
    innermost function, else the class body, else the module."""
    sites: dict[str, set[tuple[str, str]]] = {name: set() for name in names}

    def visit(node: ast.AST, rel: str, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in child.body:
                    visit_stmt(stmt, rel, child.name)
                continue
            if isinstance(child, ast.ClassDef):
                for stmt in child.body:
                    visit_stmt(stmt, rel, child.name)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                if name in names:
                    sites[name].add((scope, rel))
            visit(child, rel, scope)

    def visit_stmt(stmt: ast.AST, rel: str, scope: str) -> None:
        wrapper = ast.Module(body=[stmt], type_ignores=[])
        visit(wrapper, rel, scope)

    for rel in files:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        visit(tree, rel, rel)
    return sites


def measure_m5(engine: GraphEngine, repo_id: str, root: Path, py_files: list[str]) -> dict:
    sample = []
    for tier, caller, caller_file, callee, callee_file, expected in _SITES:
        rows = _rows(
            engine,
            "MATCH (a {repo_id: $r, name: $caller})-[x:CALLS]->(b:Function {repo_id: $r, name: $callee, file: $bf}) "
            "WHERE coalesce(a.file, a.source_file) = $af RETURN x.confidence AS conf",
            r=repo_id, caller=caller, callee=callee, af=caller_file, bf=callee_file,
        )
        present = bool(rows)
        conf = rows[0]["conf"] if rows else None
        ok = (not present) if expected is None else (present and conf == expected)
        sample.append({"tier": tier, "site": f"{caller_file}:{caller} -> {callee_file}:{callee}",
                       "present": present, "confidence": conf, "ok": ok})
    sites = _call_sites(root, py_files, set(_RECALL_NAMES))
    recall = {}
    for name in _RECALL_NAMES:
        graph = {
            (row["name"], row["file"])
            for row in _rows(
                engine,
                "MATCH (c {repo_id: $r})-[:CALLS]->(:Function {repo_id: $r, name: $n}) "
                "RETURN DISTINCT c.name AS name, coalesce(c.file, c.source_file) AS file",
                r=repo_id, n=name,
            )
        }
        truth = sites[name]
        recall[name] = {
            "sites": len(truth), "found": len(truth & graph), "extra": len(graph - truth),
            "recall": round(len(truth & graph) / len(truth), 4) if truth else None,
            "missed_sample": sorted(truth - graph)[:5],
        }
    return {"sample_ok": sum(s["ok"] for s in sample), "sample_size": len(sample), "sample": sample,
            "find_callers_recall": recall}


def measure_m6(engine: GraphEngine, repo_id: str, timings: dict) -> dict:
    sizes = [
        (row["name"], sum(len(e.encode("utf-8")) for e in row["refs"]))
        for row in _rows(
            engine, "MATCH (m:Module {repo_id: $r}) RETURN m.name AS name, coalesce(m.name_refs, []) AS refs",
            r=repo_id,
        )
    ]
    name, size = max(sizes, key=lambda pair: pair[1]) if sizes else (None, 0)
    return {**timings, "largest_name_refs_module": name, "largest_name_refs_bytes": size,
            "total_name_refs_bytes": sum(s for _n, s in sizes)}


# --- fixture mode ----------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "callgraph"
FIXTURE_LANGUAGES = ["ts", "go", "java", "kotlin", "csharp", "rust", "cpp"]

# M4: callee names that must never rank in a fixture's PageRank top five (the
# container, string and runtime methods every language's stoplist drops).
_FIXTURE_STOPLIST_PROBE = {
    "get", "set", "add", "put", "push", "pop", "find", "slice", "map", "filter", "join", "split", "append",
    "remove", "contains", "size", "len", "length", "toString", "equals", "hashCode", "String", "Error", "Close",
    "Lock", "Unlock", "clone", "unwrap", "iter", "new", "from", "parse", "stringify", "info", "log", "format",
}


def _fixture_scan(engine: GraphEngine, lang: str, repo_id: str) -> tuple[Path, dict]:
    """Copy fixture `lang` (less its expected.json) to a temporary folder and
    full-scan it into `repo_id`; the folder and the scan's timings."""
    root = Path(tempfile.mkdtemp(prefix=f"devgraph-measure-{lang}-")) / lang
    shutil.copytree(FIXTURES / lang, root, ignore=shutil.ignore_patterns("expected.json"))
    engine.delete_repository(repo_id)
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    started = time.perf_counter()
    full_scan(engine, repo_id, root)
    return root, {"full_scan_s": round(time.perf_counter() - started, 2)}


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _graph_calls(engine: GraphEngine, repo_id: str) -> list[tuple[tuple, str]]:
    """Every CALLS edge as ((caller, caller file, callee, callee file), confidence)."""
    return [
        ((row["a"], row["af"], row["b"], row["bf"]), str(row["conf"]))
        for row in _rows(
            engine,
            "MATCH (a {repo_id: $r})-[x:CALLS]->(b:Function {repo_id: $r}) "
            "RETURN a.name AS a, coalesce(a.file, a.source_file) AS af, b.name AS b, b.file AS bf, "
            "x.confidence AS conf",
            r=repo_id,
        )
    ]


def _graph_imports(engine: GraphEngine, repo_id: str) -> set[tuple[str, str]]:
    return {
        (row["a"], row["b"])
        for row in _rows(
            engine, "MATCH (a:Module {repo_id: $r})-[:IMPORTS]->(b:Module {repo_id: $r}) RETURN a.name AS a, b.name AS b",
            r=repo_id,
        )
    }


def fixture_metrics(engine: GraphEngine, repo_id: str, expected: dict, timings: dict) -> dict:
    """M1-M6 of the graph `repo_id` holds against a fixture's `expected`.

    A CALLS edge is linked when its confidence is `resolved` or `package` (a
    package edge to the right file counts as a correct link; confidences are
    reported apart). An `ambiguous` truth row is correct when linked and not
    required for recall."""
    truth = {(c["caller"], c["caller_file"], c["callee"], c["callee_file"]) for c in expected["calls"]}
    required = {
        (c["caller"], c["caller_file"], c["callee"], c["callee_file"]) for c in expected["calls"] if not c.get("ambiguous")
    }
    edges = _graph_calls(engine, repo_id)
    by_conf: dict[str, set[tuple]] = {}
    for key, conf in edges:
        by_conf.setdefault(conf, set()).add(key)
    every = {key for key, _conf in edges}
    resolved = by_conf.get("resolved", set())
    linked = resolved | by_conf.get("package", set())
    imports = _graph_imports(engine, repo_id)

    # M1: edges from one caller to one callee name with more than one target.
    groups: dict[tuple, list[str]] = {}
    for (a, af, b, _bf), conf in edges:
        groups.setdefault((a, af, b, conf), []).append(conf)
    multi: dict[str, int] = {}
    for (_a, _af, _b, conf), hits in groups.items():
        if len(hits) > 1:
            multi[conf] = multi.get(conf, 0) + len(hits)
    m1 = {
        "calls_by_confidence": {conf: len(keys) for conf, keys in sorted(by_conf.items())},
        "multi_target_by_confidence": dict(sorted(multi.items())),
        "resolved_multi_share": _ratio(multi.get("resolved", 0), len(resolved)),
    }

    # M2: precision, and resolved edges between directories with no IMPORTS
    # (a Go or Java package is one directory, so same-directory edges are exempt).
    cross = sorted(
        f"{af}:{a} -> {bf}:{b}" for a, af, b, bf in resolved
        if af and bf and posixpath.dirname(af) != posixpath.dirname(bf) and (af, bf) not in imports
    )
    m2 = {
        "precision_resolved": _ratio(len(resolved & truth), len(resolved)),
        "precision_linked": _ratio(len(linked & truth), len(linked)),
        "precision_all": _ratio(len(every & truth), len(every)),
        "linked_false_positives": sorted(f"{af}:{a} -> {bf}:{b}" for a, af, b, bf in linked - truth)[:15],
        "resolved_cross_dir_without_import": len(cross),
        "cross_sample": cross[:10],
    }

    # M3: IMPORTS file to file.
    import_truth = {(i["from_file"], i["to_file"]) for i in expected["imports"]}
    import_required = {(i["from_file"], i["to_file"]) for i in expected["imports"] if not i.get("ambiguous")}
    m3 = {
        "graph_imports": len(imports),
        "truth_imports": len(import_required),
        "precision": _ratio(len(imports & import_truth), len(imports)),
        "recall": _ratio(len(imports & import_required), len(import_required)),
        "missed": sorted(f"{a} -> {b}" for a, b in import_required - imports)[:15],
        "extra": sorted(f"{a} -> {b}" for a, b in imports - import_truth)[:15],
    }

    # M4: recall, and the PageRank top five.
    refresh_insights(engine, repo_id)
    top = _rows(
        engine,
        "MATCH (n {repo_id: $r}) WHERE n.insight_pagerank IS NOT NULL "
        "RETURN n.name AS name, coalesce(n.file, n.source_file) AS file, n.insight_pagerank AS rank "
        "ORDER BY rank DESC, name LIMIT 5",
        r=repo_id,
    )
    m4 = {
        "recall_any": _ratio(len(every & required), len(required)),
        "recall_linked": _ratio(len(linked & required), len(required)),
        "recall_resolved": _ratio(len(resolved & required), len(required)),
        "missed": sorted(f"{af}:{a} -> {bf}:{b}" for a, af, b, bf in required - every)[:15],
        "top5": [[row["name"], row["file"]] for row in top],
        # A stoplisted name may rank when the truth really calls it there.
        "top5_stoplisted": [
            row["name"] for row in top
            if row["name"] in _FIXTURE_STOPLIST_PROBE
            and not any((b, bf) == (row["name"], row["file"]) for _a, _af, b, bf in truth)
        ],
    }

    # M5: the named sites: every call with a `note`, and every `no_edge`.
    conf_of = {key: conf for key, conf in edges}
    sample = []
    for c in expected["calls"]:
        if not c.get("note") or c.get("ambiguous"):
            continue
        key = (c["caller"], c["caller_file"], c["callee"], c["callee_file"])
        wrong = sorted(f for f in c.get("not_files", []) if (c["caller"], c["caller_file"], c["callee"], f) in every)
        ok = key in every and not wrong
        sample.append({"site": f"{c['caller_file']}:{c['caller']} -> {c['callee_file']}:{c['callee']}",
                       "note": c["note"], "confidence": conf_of.get(key), "wrong_files": wrong, "ok": ok})
    for n in expected.get("no_edge", []):
        hits = sorted(
            f"{bf}:{conf}" for (a, af, b, bf), conf in edges
            if (a, af, b) == (n["caller"], n["caller_file"], n["callee"])
        )
        sample.append({"site": f"{n['caller_file']}:{n['caller']} -/-> {n['callee']}", "note": n.get("note"),
                       "edges": hits, "ok": not hits})
    m5 = {
        "sites_ok": sum(s["ok"] for s in sample),
        "sites": len(sample),
        "failed": [s for s in sample if not s["ok"]],
    }
    if expected.get("symbols"):
        nodes = {
            (row["name"], row["file"])
            for row in _rows(
                engine, "MATCH (n {repo_id: $r}) WHERE n:Function OR n:Class RETURN n.name AS name, n.file AS file",
                r=repo_id,
            )
        }
        missing = sorted(f"{s['file']}:{s['name']}" for s in expected["symbols"] if (s["name"], s["file"]) not in nodes)
        m5["symbols_found"] = len(expected["symbols"]) - len(missing)
        m5["symbols"] = len(expected["symbols"])
        m5["symbols_missing"] = missing

    return {"M1": m1, "M2": m2, "M3": m3, "M4": m4, "M5": m5, "M6": measure_m6(engine, repo_id, timings)}


def fixture_failures(metrics: dict) -> list[str]:
    """The design's M1-M5 targets a fixture's metrics miss (empty when met)."""
    m1, m2, m3, m4, m5 = (metrics[k] for k in ("M1", "M2", "M3", "M4", "M5"))
    checks = [
        ("M1 resolved multi-target share < 2 %", m1["resolved_multi_share"] is not None
         and m1["resolved_multi_share"] < 0.02),
        ("M2 linked precision >= 98 %", (m2["precision_linked"] or 0) >= 0.98),
        ("M2 no resolved cross-directory edge without an import", m2["resolved_cross_dir_without_import"] == 0),
        ("M3 IMPORTS precision >= 98 %", (m3["precision"] or 0) >= 0.98),
        ("M3 IMPORTS recall >= 95 %", (m3["recall"] or 0) >= 0.95),
        ("M4 CALLS recall >= 95 %", (m4["recall_any"] or 0) >= 0.95),
        ("M4 no stoplisted name in the PageRank top five", not m4["top5_stoplisted"]),
        ("M5 every named site", m5["sites_ok"] == m5["sites"]),
        ("M5 every expected symbol", m5.get("symbols_found") == m5.get("symbols")),
    ]
    return [name for name, ok in checks if not ok]


def measure_fixture(engine: GraphEngine, lang: str) -> dict:
    """Scan fixture `lang` into a scratch repository and measure it; the
    repository and the temporary copy are deleted afterwards."""
    expected = json.loads((FIXTURES / lang / "expected.json").read_text(encoding="utf-8"))
    repo_id = f"zz-measure-{lang}-{uuid.uuid4().hex[:8]}"
    root = None
    try:
        root, timings = _fixture_scan(engine, lang, repo_id)
        metrics = fixture_metrics(engine, repo_id, expected, timings)
    finally:
        engine.delete_repository(repo_id)
        if root is not None:
            shutil.rmtree(root.parent, ignore_errors=True)
    return {"fixture": lang, **metrics, "failures": fixture_failures(metrics)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo_id", nargs="?")
    parser.add_argument("--fixture", choices=FIXTURE_LANGUAGES, help="measure a ground-truth fixture instead")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--scan", action="store_true", help="delete the repo id and full-scan --root into it first")
    parser.add_argument("--cleanup", action="store_true", help="delete the repo id afterwards")
    args = parser.parse_args()
    if not args.fixture and not args.repo_id:
        parser.error("give a repo_id or --fixture")
    root = args.root.resolve()
    engine = GraphEngine(
        uri=os.environ.get("DEVGRAPH_NEO4J_URI", "bolt://127.0.0.1:7687"),
        user=os.environ.get("DEVGRAPH_NEO4J_USER", "neo4j"),
        password=os.environ.get("DEVGRAPH_NEO4J_PASSWORD", "devgraph-local-dev"),
    )
    if args.fixture:
        try:
            json.dump(measure_fixture(engine, args.fixture), sys.stdout, indent=1)
            print()
        finally:
            engine.close()
        return 0
    timings: dict = {}
    try:
        if args.scan:
            engine.delete_repository(args.repo_id)
            provision_repository_schema(engine, root)
            engine.upsert_repository(args.repo_id, args.repo_id, str(root))
            started = time.perf_counter()
            full_scan(engine, args.repo_id, root)
            timings["full_scan_s"] = round(time.perf_counter() - started, 2)
            config_init = root / "devgraph" / "config" / "__init__.py"
            if config_init.is_file():
                started = time.perf_counter()
                timings["config_init_save_files"] = index_paths(engine, args.repo_id, root, {config_init})
                timings["config_init_save_s"] = round(time.perf_counter() - started, 2)
        py_files = sorted(
            p.relative_to(root).as_posix() for p in indexable_paths(root) if p.suffix == ".py"
        )
        result = {
            "repo_id": args.repo_id,
            "M1": measure_m1(engine, args.repo_id),
            "M2": measure_m2(engine, args.repo_id),
            "M3": measure_m3(engine, args.repo_id, root, py_files),
            "M4": measure_m4(engine, args.repo_id),
            "M5": measure_m5(engine, args.repo_id, root, py_files),
            "M6": measure_m6(engine, args.repo_id, timings),
        }
        json.dump(result, sys.stdout, indent=2)
        print()
    finally:
        if args.cleanup:
            engine.delete_repository(args.repo_id)
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
