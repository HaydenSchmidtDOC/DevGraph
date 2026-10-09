"""describe_node's query and result logic, against a stub engine that records every read."""

import re

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from neo4j import time as neo4j_time
from neo4j.exceptions import ClientError

from devgraph.config.project_tools import DEFAULT_TIMEOUT_S
from devgraph.graph import schema
from devgraph.mcp.tools import describe_node

LOOKUP_ROW = {
    "id": "4:x:1",
    "label": "Runbook",
    "name": "runbooks/api-outage.md",
    "file": "runbooks/api-outage.md",
    "properties": {"path": "runbooks/api-outage.md", "owner": "platform-team", "repo_id": "demo",
                   "name": "runbooks/api-outage.md"},
}


class StubEngine:
    """Returns the canned `(rows, more)` responses in call order; records every call."""

    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.calls = []
        self.error = error

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.calls.append({"query": query, "params": params, "timeout_s": timeout_s, "max_rows": max_rows})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else ([], False)

    def run_cypher(self, query, params=None):
        raise AssertionError("describe_node must only read through run_read_cypher")


def _found(engine, **kwargs):
    kwargs.setdefault("name", "runbooks/api-outage.md")
    return describe_node(engine, "demo", **kwargs)


def _client_error(code, message):
    return ClientError._hydrate_neo4j(code=code, message=message)


def _branches(query):
    return re.findall(r"MATCH \(n:`([^`]+)`\) WHERE n\.repo_id = \$repo_id AND n\.(name|path) = \$name", query)


def test_single_match_returns_node_and_groups():
    groups = [
        {"dir": "in", "rel": "MENTIONS", "total": 3,
         "refs": [{"label": "Document", "name": "a.md", "file": None}]},
        {"dir": "out", "rel": "RUNBOOK_FOR", "total": 2,
         "refs": [{"label": "Service", "name": "api", "file": "docker-compose.prod.yml"},
                  {"label": "Service", "name": "api", "file": "docker-compose.yml"}]},
    ]
    engine = StubEngine(([LOOKUP_ROW], False), (groups, False))
    result = _found(engine)
    assert result["status"] == "found"
    node = result["node"]
    assert (node["label"], node["name"], node["file"]) == ("Runbook", "runbooks/api-outage.md", "runbooks/api-outage.md")
    assert result["outgoing"] == {"RUNBOOK_FOR": {"count": 2, "results": groups[1]["refs"], "truncated": False}}
    assert result["incoming"] == {
        "MENTIONS": {"count": 3, "results": [{"label": "Document", "name": "a.md"}], "truncated": True}
    }
    assert result["groups_truncated"] is False


def test_properties_hide_only_bookkeeping():
    props = {
        "claims": ["x"], "extractor": "docs", "insight_pagerank": 0.4, "repo_id": "demo", "name": "n",
        "sources": ["a.yml"], "source": "s", "path": "p", "source_file": "sf", "file": "f",
        "long": "y" * 600,
        "when": neo4j_time.DateTime(2026, 1, 2, 3, 4, 5),
        "many": list(range(30)),
    }
    engine = StubEngine(([{**LOOKUP_ROW, "properties": props}], False), ([], False))
    node = _found(engine)["node"]
    shown = node["properties"]
    assert not {"claims", "extractor", "insight_pagerank", "repo_id", "name"} & set(shown)
    assert {"sources", "source", "path", "source_file", "file"} <= set(shown)
    assert len(shown["long"]) == 500
    assert shown["when"].startswith("2026-01-02T03:04:05")
    assert shown["many"] == list(range(20))
    assert node["properties_truncated"] is False

    wide = {f"p{i:02d}": i for i in range(60)}
    engine = StubEngine(([{**LOOKUP_ROW, "properties": wide}], False), ([], False))
    node = _found(engine)["node"]
    assert len(node["properties"]) == 50
    assert list(node["properties"]) == sorted(wide)[:50]
    assert node["properties_truncated"] is True


def test_refs_are_raw():
    odd = "n\t" + "z" * 600
    groups = [{"dir": "out", "rel": "CALLS", "total": 1, "refs": [{"label": "Function", "name": odd, "file": "a.py"}]}]
    engine = StubEngine(([LOOKUP_ROW], False), (groups, False))
    assert _found(engine)["outgoing"]["CALLS"]["results"] == [{"label": "Function", "name": odd, "file": "a.py"}]


def test_lookup_without_label_is_a_per_label_union():
    engine = StubEngine()
    with pytest.raises(ToolError):
        describe_node(engine, "demo", "x", declared_labels=("Runbook", "Folder"))
    lookup = engine.calls[0]["query"]
    branches = _branches(lookup)
    builtins = [label for label in schema.NODE_LABELS if label != "Repository"]
    assert sorted(label for label, key in branches if key == "name") == sorted(builtins + ["Runbook", "Folder"])
    assert sorted(label for label, key in branches if key == "path") == ["Folder", "Runbook"]
    assert "`Repository`" not in lookup
    assert "UNION" in lookup
    assert " OR n.path" not in lookup


def test_label_is_validated_then_interpolated():
    engine = StubEngine()
    with pytest.raises(ToolError):
        describe_node(engine, "demo", "x", label="Runbook")
    assert _branches(engine.calls[0]["query"]) == [("Runbook", "name"), ("Runbook", "path")]

    engine = StubEngine()
    with pytest.raises(ToolError, match="label"):
        describe_node(engine, "demo", "x", label="Runbook) DETACH DELETE n //")
    assert engine.calls == []

    engine = StubEngine()
    with pytest.raises(ToolError):
        describe_node(engine, "demo", "x", declared_labels=("Runbook) DETACH DELETE n //",))
    assert engine.calls and all("DETACH" not in call["query"] for call in engine.calls)


def test_repository_is_excluded_everywhere():
    engine = StubEngine()
    with pytest.raises(ToolError):
        describe_node(engine, "demo", "x")
    lookup, suggestions = (call["query"] for call in engine.calls)
    assert "NOT n:Repository" in lookup
    assert "NOT n:Repository" in suggestions

    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine)
    assert "NOT m:Repository" in engine.calls[1]["query"]


def test_file_filter_is_a_parameter():
    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine, file="src/app.py")
    lookup = engine.calls[0]
    assert lookup["params"]["file"] == "src/app.py"
    assert "src/app.py" not in lookup["query"]
    for prop in ("n.file", "n.path", "n.source_file"):
        assert prop in lookup["query"]
    assert "n.source " not in lookup["query"] and "n.source]" not in lookup["query"]
    assert "n.sources" not in lookup["query"]

    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine)
    assert engine.calls[0]["params"]["file"] is None


def _candidate(label, name, file=None):
    return {"id": f"{label}:{name}:{file}", "label": label, "name": name, "file": file, "properties": {}}


def test_ambiguous_returns_capped_sorted_candidates():
    rows = [_candidate("Function", f"f{i:02d}", f"z{i % 3}.py") for i in range(20, -1, -1)]
    engine = StubEngine((rows, True))
    result = describe_node(engine, "demo", "f")
    assert result["status"] == "ambiguous"
    assert result["count"] == 21
    assert result["truncated"] is True
    expected = sorted(rows, key=lambda r: (r["label"], r["name"], r["file"] or ""))[:20]
    assert result["candidates"] == [{"label": r["label"], "name": r["name"], "file": r["file"]} for r in expected]
    assert len(engine.calls) == 1

    rows = [_candidate("Module", "src/app.py"), _candidate("File", "src/app.py", "src/app.py"),
            _candidate("Function", "main", "a.py")]
    engine = StubEngine((rows, False))
    result = describe_node(engine, "demo", "src/app.py")
    assert (result["count"], result["truncated"]) == (3, False)
    assert result["candidates"] == [
        {"label": "File", "name": "src/app.py", "file": "src/app.py"},
        {"label": "Function", "name": "main", "file": "a.py"},
        {"label": "Module", "name": "src/app.py"},
    ]
    assert len(engine.calls) == 1


def test_not_found_raises_with_repo_and_suggestions():
    suggestions = [{"label": "Service", "name": "api", "file": "docker-compose.yml"},
                   {"label": "Runbook", "name": "runbooks/api-outage.md", "file": "runbooks/api-outage.md"}]
    engine = StubEngine(([], False), (suggestions, False))
    with pytest.raises(ToolError) as caught:
        describe_node(engine, "demo", "ap", label="Service", file="compose.yml")
    message = str(caught.value)
    assert "'demo'" in message
    assert "'ap'" in message and "Service" in message and "'compose.yml'" in message
    assert "'api'" in message and "'docker-compose.yml'" in message and "'runbooks/api-outage.md'" in message
    assert "search_component" in message
    assert len(engine.calls) == 2
    assert engine.calls[1]["max_rows"] == 5


def test_error_echo_is_capped():
    engine = StubEngine()
    with pytest.raises(ToolError) as caught:
        describe_node(engine, "demo", "q" * 5000)
    assert "q" * 100 in str(caught.value) and "q" * 101 not in str(caught.value)

    engine = StubEngine()
    with pytest.raises(ToolError) as caught:
        describe_node(engine, "demo", "x", relationship_types=["Q" * 5000])
    assert "Q" * 100 in str(caught.value) and "Q" * 101 not in str(caught.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"relationship_types": ["X]->() DETACH DELETE n //"]},
        {"relationship_types": ["calls"]},
        {"neighbor_labels": ["File`) RETURN 1 //"]},
        {"relationship_types": [f"T{i}" for i in range(21)]},
        {"neighbor_labels": [f"L{i}" for i in range(21)]},
        {"direction": "sideways"},
    ],
)
def test_hostile_filters_never_reach_cypher(kwargs):
    engine = StubEngine(([LOOKUP_ROW], False))
    with pytest.raises(ToolError):
        describe_node(engine, "demo", "x", **kwargs)
    assert engine.calls == []


def test_valid_filters_and_direction_are_parameters():
    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine, relationship_types=["RUNBOOK_FOR"], neighbor_labels=["Service"], direction="in")
    groups = engine.calls[1]
    assert groups["params"]["types"] == ["RUNBOOK_FOR"]
    assert groups["params"]["labels"] == ["Service"]
    assert groups["params"]["direction"] == "in"
    assert "RUNBOOK_FOR" not in groups["query"] and "Service" not in groups["query"]
    assert "$direction IN ['both','out']" in groups["query"]
    assert "$direction IN ['both','in']" in groups["query"]

    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine, relationship_types=[], neighbor_labels=[])
    assert engine.calls[1]["params"]["types"] is None
    assert engine.calls[1]["params"]["labels"] is None


def test_groups_query_is_top_n_with_separate_count():
    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine)
    query = engine.calls[1]["query"]
    assert "count(DISTINCT m)" in query
    subquery = query.index("CALL (n, dir, rel)")
    assert "LIMIT $cap" in query[subquery:]
    assert "collect(" not in query[:subquery]


@pytest.mark.parametrize("given, sent", [(0, 1), (500, 50), (10, 10)])
def test_max_per_type_is_clamped(given, sent):
    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine, max_per_type=given)
    assert engine.calls[1]["params"]["cap"] == sent


def test_groups_truncated_when_more_than_200():
    groups = [{"dir": "out", "rel": f"T{i:03d}", "total": 1, "refs": [{"label": "File", "name": "a", "file": "a"}]}
              for i in range(200)]
    engine = StubEngine(([LOOKUP_ROW], False), (groups, True))
    result = _found(engine)
    assert result["groups_truncated"] is True
    assert len(result["outgoing"]) == 200


def test_node_deleted_between_queries():
    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    result = _found(engine)
    assert result["status"] == "found"
    assert result["node"]["name"] == "runbooks/api-outage.md"
    assert (result["outgoing"], result["incoming"]) == ({}, {})


def test_queries_are_bounded():
    engine = StubEngine(([LOOKUP_ROW], False), ([], False))
    _found(engine)
    engine_nf = StubEngine()
    with pytest.raises(ToolError):
        describe_node(engine_nf, "demo", "x")
    calls = engine.calls + engine_nf.calls
    assert all(call["timeout_s"] == DEFAULT_TIMEOUT_S for call in calls)
    assert [call["max_rows"] for call in engine.calls] == [21, 200]
    assert [call["max_rows"] for call in engine_nf.calls] == [21, 5]


def test_timeout_is_a_clear_error():
    engine = StubEngine(error=_client_error("Neo.ClientError.Transaction.TransactionTimedOut", "secret detail"))
    with pytest.raises(ToolError) as caught:
        describe_node(engine, "demo", "x")
    assert str(caught.value).startswith("describe_node timed out after 10s; narrow it with ")
    assert "secret detail" not in str(caught.value)


def test_other_neo4j_error_is_its_code_only():
    engine = StubEngine(error=_client_error("Neo.ClientError.Statement.SyntaxError", "secret detail"))
    with pytest.raises(ToolError) as caught:
        describe_node(engine, "demo", "x")
    assert str(caught.value) == "describe_node failed: Neo.ClientError.Statement.SyntaxError"


# ── the MCP server: catalog, shadowing, session default ────────────────────

import asyncio  # noqa: E402
import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

from devgraph.config import global_tools  # noqa: E402
from devgraph.config.project_tools import TOOLS_FILENAME  # noqa: E402
from devgraph.config.settings import Settings  # noqa: E402
from devgraph.mcp import server as mcp_server  # noqa: E402
from devgraph.mcp import tools as devgraph_tools  # noqa: E402
from devgraph.mcp.catalog import TOOL_CATALOG, builtin_tool_names  # noqa: E402

SHADOW = "ignored: {layer} tool 'describe_node' shadows a locked tool; using the fixed implementation"
SHADOWING_TOOL = {
    "name": "describe_node",
    "description": "Shadows a built-in.",
    "cypher": "MATCH (n {repo_id: $repo_id}) RETURN n.name AS name",
}


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


class _ServerEngine:
    def run_cypher(self, query, params=None):
        return []

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        return [], False


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Build a server for repo `demo`; optional project tools yaml and global tool dicts."""
    from devgraph.config import project_trust

    monkeypatch.setattr(project_trust, "project_tools_trust", lambda repo_root, data: "trusted")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    store = tmp_path / "store" / global_tools.GLOBAL_TOOLS_FILENAME
    monkeypatch.setattr(global_tools, "_default_path", lambda: store)

    def build(project=None, global_list=None):
        repo = tmp_path / "demo"
        repo.mkdir(exist_ok=True)
        if project:
            (repo / TOOLS_FILENAME).write_text(project)
        if global_list:
            store.parent.mkdir(exist_ok=True)
            store.write_text(json.dumps({"version": 1, "tools": global_list}))
        record = _Repo("demo", repo)
        return mcp_server.build_server(_ServerEngine(), _Registry([record]), session_repo=record, session_source="env")

    return build


@pytest.fixture
def recorder(monkeypatch):
    log = []

    def fake(*args, **kwargs):
        log.append((args, kwargs))
        return {"count": 1, "results": [{"name": "X"}], "truncated": False}

    monkeypatch.setattr(devgraph_tools, "describe_node", fake)
    monkeypatch.setattr(devgraph_tools, "declared_node_labels", lambda registry, repo_id: ("Runbook",))
    return log


def _call(server, arguments):
    return asyncio.run(server.call_tool("describe_node", arguments))


def _listed(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}["describe_node"]


def test_describe_node_catalog_entry():
    assert "describe_node" in builtin_tool_names()
    entry = next(e for e in TOOL_CATALOG if e["name"] == "describe_node")
    assert entry["envelope"] is False
    assert "{count, results, truncated}" in entry["note"]


@pytest.mark.parametrize("layer", ["project", "global"])
def test_a_shadowing_tool_cannot_take_describe_node(served, recorder, layer):
    if layer == "project":
        server = served(project=(
            "version: 1\ntools:\n  - name: describe_node\n    description: Shadows a built-in.\n"
            "    cypher: |\n      MATCH (n {repo_id: $repo_id}) RETURN n.name AS name\n"
        ))
    else:
        server = served(global_list=[SHADOWING_TOOL])
    status = json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)
    assert "describe_node" not in status["served"]
    assert SHADOW.format(layer=layer) in status["notices"]
    assert "max_per_type" in _listed(server).input_schema["properties"]
    assert SHADOW.format(layer=layer) in _call(server, {"repo_id": "demo", "name": "X"}).structured_content["notices"]


def test_describe_node_uses_the_session_repo(served, recorder):
    server = served()
    result = _call(server, {"name": "X"}).structured_content
    (args, kwargs), = recorder
    assert args[1] == "demo" and args[2] == "X"
    assert kwargs["declared_labels"] == ("Runbook",)
    assert result["repo_id"] == "demo"
    assert any("used this session's repository 'demo'" in n for n in result["notices"])

    recorder.clear()
    explicit = _call(server, {"repo_id": "other", "name": "X"}).structured_content
    assert recorder[0][0][1] == "other"
    assert "repo_id" not in explicit and "notices" not in explicit


def test_describe_node_has_no_cross_repo(served):
    schema = _listed(served()).input_schema
    assert "cross_repo" not in schema["properties"]
    assert schema["required"] == ["name"]


def test_describe_node_is_read_only_annotated(served):
    assert _listed(served()).annotations.read_only_hint is True


def test_describe_node_error_surfaces_as_tool_error(served, monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise ValueError("no node named 'x' in repository 'demo'")

    monkeypatch.setattr(devgraph_tools, "describe_node", boom)
    monkeypatch.setattr(devgraph_tools, "declared_node_labels", lambda registry, repo_id: ())
    server = served()
    # In process the SDK raises; over the wire it becomes an is_error result. It hides a
    # crash's own text from the client (as for every built-in), so the text is on __cause__.
    with pytest.raises(ToolError) as excinfo:
        _call(server, {"repo_id": "demo", "name": "x"})
    assert "no node named 'x'" in str(excinfo.value.__cause__)
    entries = mcp_server.read_tool_telemetry(100)
    assert entries[0]["tool"] == "describe_node" and entries[0]["ok"] is False


@pytest.mark.parametrize("kwargs", [{"relationship_types": "CALLS"}, {"neighbor_labels": "File"}])
def test_a_bare_string_filter_is_rejected(kwargs):
    engine = StubEngine(([LOOKUP_ROW], False))
    with pytest.raises(ToolError, match="must be a list"):
        describe_node(engine, "demo", "x", **kwargs)
    assert engine.calls == []


class _CannedEngine(_ServerEngine):
    """The not-found path: an empty lookup, then one suggestion."""

    def __init__(self, error=None):
        self.responses = [([], False), ([{"label": "Service", "name": "api", "file": "docker-compose.yml"}], False)]
        self.error = error

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


@pytest.fixture
def served_with(tmp_path, monkeypatch):
    """A server for repo `demo` over the given engine, with the real describe_node."""
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    monkeypatch.setattr(global_tools, "_default_path", lambda: tmp_path / "store" / global_tools.GLOBAL_TOOLS_FILENAME)

    def build(engine):
        repo = tmp_path / "demo"
        repo.mkdir(exist_ok=True)
        record = _Repo("demo", repo)
        return mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    return build


def _visible(server, arguments):
    """The text a client sees for a failed call."""
    with pytest.raises(ToolError) as excinfo:
        _call(server, arguments)
    return str(excinfo.value)


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"name": "ap"}, ["no node named 'ap'", "in repository 'demo'", "name='api'", "search_component"]),
        ({"name": "x", "direction": "sideways"}, ["direction must be 'both', 'out' or 'in', not 'sideways'"]),
        ({"name": "x", "label": "Runbook) DETACH DELETE n //"}, ["invalid label 'Runbook) DETACH DELETE n //'"]),
        ({"name": "x", "relationship_types": ["calls"]}, ["invalid relationship type 'calls'"]),
        ({"name": "x", "neighbor_labels": [f"L{i}" for i in range(21)]}, ["at most 20 neighbor labels, got 21"]),
        ({"name": "q" * 5000}, ["no node named '" + "q" * 100 + "'"]),
    ],
)
def test_errors_reach_the_client_through_the_server(served_with, arguments, expected):
    text = _visible(served_with(_CannedEngine()), arguments)
    for fragment in expected:
        assert fragment in text
    assert "q" * 101 not in text


def test_a_timeout_reaches_the_client_through_the_server(served_with):
    engine = _CannedEngine(error=_client_error("Neo.ClientError.Transaction.TransactionTimedOut", "secret detail"))
    text = _visible(served_with(engine), {"name": "x"})
    assert "describe_node timed out after 10s; narrow it with" in text
    assert "secret detail" not in text


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_an_empty_name_is_rejected(name):
    engine = StubEngine()
    with pytest.raises(ToolError, match="name is empty"):
        describe_node(engine, "demo", name)
    assert engine.calls == []
