"""find_callers, impact_analysis and find_related_files on a name two files define:
`file` narrows to one, and without it every row says which target it belongs to."""

import uuid

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

REPO = f"zz-same-named-{uuid.uuid4().hex[:8]}"

# models.py and other.py each define save(). use_a calls models.py's, caller calls
# other.py's, use_both calls both, and top calls use_a.
_SEED = """
CREATE (ms:Function {repo_id: $r, name: 'save', file: 'models.py'})
CREATE (os:Function {repo_id: $r, name: 'save', file: 'other.py'})
CREATE (a:Function {repo_id: $r, name: 'use_a', file: 'models.py'})
CREATE (c:Function {repo_id: $r, name: 'caller', file: 'other.py'})
CREATE (b:Function {repo_id: $r, name: 'use_both', file: 'both.py'})
CREATE (t:Function {repo_id: $r, name: 'top', file: 'top.py'})
CREATE (m:Module {repo_id: $r, name: 'models.py'})
CREATE (m)-[:CONTAINS]->(ms)
CREATE (a)-[:CALLS {confidence: 'resolved'}]->(ms)
CREATE (c)-[:CALLS {confidence: 'resolved'}]->(os)
CREATE (b)-[:CALLS {confidence: 'resolved'}]->(ms)
CREATE (b)-[:CALLS {confidence: 'resolved'}]->(os)
CREATE (t)-[:CALLS {confidence: 'resolved'}]->(a)
CREATE (la:Function {repo_id: $r, name: 'load', file: 'a.py'})
CREATE (lb:Function {repo_id: $r, name: 'load', file: 'b.py'})
CREATE (ls:Function {repo_id: $r, name: 'load'})
CREATE (:Function {repo_id: $r, name: 'loader', file: 'c.py'})-[:CALLS]->(ls)
CREATE (:Function {repo_id: $r, name: 'reader', file: 'c.py'})-[:CALLS]->(la)
CREATE (:Function {repo_id: $r, name: 'gadget_user', file: 'g.py'})-[:CALLS]->(:Gadget {repo_id: $r, name: 'gadget'})
"""


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    test_engine.run_cypher(_SEED, {"r": REPO})
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


TARGETS = {
    "count": 2,
    "results": [{"label": "Function", "file": "models.py"}, {"label": "Function", "file": "other.py"}],
    "truncated": False,
}


def test_find_callers_labels_each_caller_with_its_target(engine):
    result = tools.find_callers(engine, REPO, "save")
    assert result["ambiguous_targets"] == TARGETS
    assert sorted((r["name"], r["target_files"]) for r in result["results"]) == [
        ("caller", ["other.py"]), ("use_a", ["models.py"]), ("use_both", ["models.py", "other.py"]),
    ]


def test_find_callers_narrowed_by_file_is_the_old_shape(engine):
    result = tools.find_callers(engine, REPO, "save", file="other.py")
    assert sorted(r["name"] for r in result["results"]) == ["caller", "use_both"]
    assert "ambiguous_targets" not in result
    assert all("target_files" not in r for r in result["results"])


def test_an_unambiguous_name_keeps_the_old_shape(engine):
    result = tools.find_callers(engine, REPO, "use_a")
    assert [r["name"] for r in result["results"]] == ["top"]
    assert "ambiguous_targets" not in result and "target_files" not in result["results"][0]


def test_a_file_that_does_not_define_the_name_is_an_error(engine):
    with pytest.raises(ToolError, match="no component named 'save' in file 'top.py'; it is defined in: 'models.py', 'other.py'"):
        tools.find_callers(engine, REPO, "save", file="top.py")


def test_impact_analysis_traces_each_target_apart(engine):
    result = tools.impact_analysis(engine, REPO, "save")
    assert result["ambiguous_targets"] == TARGETS
    direct = sorted((d["name"], sorted(d["target_files"])) for d in result["direct_dependents"]["results"])
    assert direct == [("caller", ["other.py"]), ("use_a", ["models.py"]), ("use_both", ["models.py", "other.py"])]
    assert result["transitive_dependents"]["results"] == [
        {"name": "top", "type": "Function", "file": "top.py", "owner": "", "target_files": ["models.py"]}
    ]
    narrowed = tools.impact_analysis(engine, REPO, "save", file="other.py")
    assert sorted(d["name"] for d in narrowed["direct_dependents"]["results"]) == ["caller", "use_both"]
    assert narrowed["transitive_dependents"]["count"] == 0 and "ambiguous_targets" not in narrowed


def test_find_related_files_labels_rows_only_when_ambiguous(engine):
    result = tools.find_related_files(engine, REPO, "save")
    assert result["ambiguous_targets"] == TARGETS
    assert result["containing_modules"]["results"] == [{"name": "models.py", "target_files": ["models.py"]}]
    narrowed = tools.find_related_files(engine, REPO, "save", file="models.py")
    assert narrowed["containing_modules"]["results"] == ["models.py"] and "ambiguous_targets" not in narrowed


def test_a_file_parameter_is_normalised(engine):
    result = tools.find_callers(engine, REPO, "save", file=".\\other.py")
    assert sorted(r["name"] for r in result["results"]) == ["caller", "use_both"]


def test_a_file_less_target_is_listed_but_never_a_target_file(engine):
    """A file-less same-named node (a route's handler stub) shows as `file: None` in
    ambiguous_targets, and a row that reaches only it has empty `target_files`."""
    expected = [{"label": "Function", "file": None}, {"label": "Function", "file": "a.py"}, {"label": "Function", "file": "b.py"}]
    callers = tools.find_callers(engine, REPO, "load")
    assert sorted(callers["ambiguous_targets"]["results"], key=lambda t: t["file"] or "") == expected
    assert {r["name"]: r["target_files"] for r in callers["results"]} == {"loader": [], "reader": ["a.py"]}
    impact = tools.impact_analysis(engine, REPO, "load")
    assert sorted(impact["ambiguous_targets"]["results"], key=lambda t: t["file"] or "") == expected
    direct = {d["name"]: d["target_files"] for d in impact["direct_dependents"]["results"]}
    assert direct == {"loader": [], "reader": ["a.py"]}


def test_cross_repo_ambiguous_targets_name_their_repository(engine):
    result = tools.find_callers(engine, REPO, "save", cross_repo=True)
    assert {t["repo_id"] for t in result["ambiguous_targets"]["results"]} == {REPO}


def test_a_node_whose_label_is_not_declared_is_still_found(engine):
    """Gadget is no built-in label and nothing declares it (a schema typo, a label
    removed but not rescanned, a console-made node): found by the fallback lookup."""
    assert [r["name"] for r in tools.find_callers(engine, REPO, "gadget")["results"]] == ["gadget_user"]
    assert tools.impact_analysis(engine, REPO, "gadget")["direct_dependents"]["count"] == 1
    assert tools.find_requirements_for(engine, REPO, "gadget")["count"] == 0
