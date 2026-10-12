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


TARGETS = [{"label": "Function", "file": "models.py"}, {"label": "Function", "file": "other.py"}]


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
    assert result["transitive_dependents"]["results"] == [{"name": "top", "type": "Function", "target_files": ["models.py"]}]
    narrowed = tools.impact_analysis(engine, REPO, "save", file="other.py")
    assert sorted(d["name"] for d in narrowed["direct_dependents"]["results"]) == ["caller", "use_both"]
    assert narrowed["transitive_dependents"]["count"] == 0 and "ambiguous_targets" not in narrowed


def test_find_related_files_labels_rows_only_when_ambiguous(engine):
    result = tools.find_related_files(engine, REPO, "save")
    assert result["ambiguous_targets"] == TARGETS
    assert result["containing_modules"]["results"] == [{"name": "models.py", "target_files": ["models.py"]}]
    narrowed = tools.find_related_files(engine, REPO, "save", file="models.py")
    assert narrowed["containing_modules"]["results"] == ["models.py"] and "ambiguous_targets" not in narrowed
