"""The call-graph fixtures (tests/fixtures/callgraph) meet the M1-M5 targets of
docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md, measured
by `scripts/measure_call_graph.py --fixture` against a live Neo4j.

A language whose resolution has not landed yet is a strict xfail, so the day
it lands the marker has to go.
"""

import importlib.util
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_call_graph.py"
_spec = importlib.util.spec_from_file_location("measure_call_graph", _SCRIPT)
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)

#: Languages whose resolution has not landed yet.
PENDING = {"go", "java", "kotlin", "csharp", "rust", "cpp"}


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.mark.parametrize(
    "lang",
    [
        pytest.param(lang, marks=pytest.mark.xfail(strict=True, reason="resolution not landed"))
        if lang in PENDING else lang
        for lang in measure.FIXTURE_LANGUAGES
    ],
)
def test_fixture_meets_the_targets(engine, lang):
    result = measure.measure_fixture(engine, lang)
    assert result["failures"] == [], result
