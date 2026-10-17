"""tests.helpers.stub: a stub is held to the real function's signature."""

from types import SimpleNamespace

import pytest

from tests import helpers
from tests.helpers import stub


def remove(engine, repo_id, root, paths, *, force=False):
    return len(paths)


class Engine:
    def files(self, repo_id):
        return {repo_id}


@pytest.fixture
def mismatches():
    seen = len(helpers.signature_mismatches)
    yield helpers.signature_mismatches
    del helpers.signature_mismatches[seen:]  # made on purpose; don't fail this test for them


def test_a_call_the_real_function_accepts_reaches_the_replacement(monkeypatch):
    module = SimpleNamespace(remove=remove)
    stub(monkeypatch, module, "remove", lambda *a, **k: "stubbed")
    assert module.remove("e", "r", "/", set(), force=True) == "stubbed"


def test_a_call_the_real_function_refuses_fails_even_when_caught(monkeypatch, mismatches):
    module = SimpleNamespace(remove=remove)
    stub(monkeypatch, module, "remove", lambda *a, **k: "stubbed")
    with pytest.raises(TypeError, match="stub of remove"):
        module.remove("e", "r", "/", set(), True)  # force is keyword-only
    try:
        module.remove("e", "r", "/")
    except TypeError:
        pass  # swallowed, as code under test may do: still recorded
    assert len(mismatches) == 2 and "missing a required argument: 'paths'" in mismatches[-1]


def test_a_method_is_checked_on_the_class_and_on_an_instance(monkeypatch, mismatches):
    stub(monkeypatch, Engine, "files", lambda self, repo_id: {"class"})
    assert Engine().files("r") == {"class"}
    engine = Engine()
    stub(monkeypatch, engine, "files", lambda repo_id: {"instance"})
    assert engine.files(repo_id="r") == {"instance"}
    with pytest.raises(TypeError):
        engine.files("r", "extra")
    assert len(mismatches) == 1


def test_the_stub_reports_the_real_signature(monkeypatch):
    import inspect

    module = SimpleNamespace(remove=remove)
    stub(monkeypatch, module, "remove", lambda *a, **k: None)
    stub(monkeypatch, module, "remove", lambda *a, **k: None)  # a stub of a stub is held to the real one
    assert inspect.signature(module.remove) == inspect.signature(remove)
