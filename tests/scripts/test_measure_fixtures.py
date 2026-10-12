"""The call-graph fixtures (tests/fixtures/callgraph) meet the M1-M5 targets of
docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md, measured
by `scripts/measure_call_graph.py --fixture` against a live Neo4j.

A language whose resolution has not landed yet, or has landed short of the
targets for a reason the spec records, is a strict xfail, so the day it meets
them the marker has to go.
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
PENDING: set[str] = set()
#: Languages whose resolution has landed short of the targets, and why.
SHORT = {
    "csharp": "an enclosing namespace's User does not shadow a using's: both match until shadow tiers "
    "(see the spec's C# section)",
}
#: Named sites no resolver that reads only its own file can reach (see the
#: spec's slice B results): `repo.size()` on an `AuditedRepo` field is the
#: `size` AuditedRepo inherits from Repo, which only AuditedRepo.java says.
DISPUTED = {
    "java": {
        "app/src/main/java/com/acme/app/OrderService.java:pages -> core/src/main/java/com/acme/core/repo/Repo.java:size",
    },
}
#: Resolved cross-directory edges with no IMPORTS between their files that are
#: right: Rust names another crate in a path with no `use` (the truth counts
#: no import there), and a crate the Cargo configuration names uniquely is
#: resolved.
CROSS_WITHOUT_IMPORT = {
    "rust": {"crates/app/src/main.rs:main -> crates/core/src/lib.rs:version"},
}


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
        if lang in PENDING
        else pytest.param(lang, marks=pytest.mark.xfail(strict=True, reason=SHORT[lang]))
        if lang in SHORT else lang
        for lang in measure.FIXTURE_LANGUAGES
    ],
)
def test_fixture_meets_the_targets(engine, lang):
    result = measure.measure_fixture(engine, lang)
    failures = result["failures"]
    disputed = DISPUTED.get(lang, set())
    if "M5 every named site" in failures and {site["site"] for site in result["M5"]["failed"]} <= disputed:
        failures = [f for f in failures if f != "M5 every named site"]
    cross = "M2 no resolved cross-directory edge without an import"
    if cross in failures and set(result["M2"]["cross_sample"]) <= CROSS_WITHOUT_IMPORT.get(lang, set()) and (
        result["M2"]["resolved_cross_dir_without_import"] == len(result["M2"]["cross_sample"])
    ):
        failures = [f for f in failures if f != cross]
    assert failures == [], result


def test_the_synthetic_csharp_repository_is_seeded(tmp_path):
    """`--cs-synthetic`'s repository and truth depend on its seed alone, and
    every truth call names files it wrote."""
    first = measure.generate_cs_synthetic(tmp_path / "a", 4, 3, seed=7)
    again = measure.generate_cs_synthetic(tmp_path / "b", 4, 3, seed=7)
    assert first == again
    files = {p.relative_to(tmp_path / "a").as_posix(): p.read_text() for p in (tmp_path / "a").rglob("*") if p.is_file()}
    assert files == {p.relative_to(tmp_path / "b").as_posix(): p.read_text()
                     for p in (tmp_path / "b").rglob("*") if p.is_file()}
    calls, imports = first
    assert {f for _a, af, _b, bf in calls for f in (af, bf)} | {f for pair in imports for f in pair} <= set(files)
    # Every service reaches another project's Gateway.
    assert any(af.split("/")[1] != bf.split("/")[1] for _a, af, b, bf in calls if b == "Send")
