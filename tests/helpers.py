"""Test helpers shared across the suite."""

import functools
import inspect
import os

#: Calls a `stub` refused, as "test: message"; tests/conftest.py fails the test
#: that made one even when the code under test swallowed the TypeError.
signature_mismatches: list[str] = []


def stub(monkeypatch, target, name, replacement):
    """`monkeypatch.setattr(target, name, replacement)` for one of DevGraph's own
    functions or methods, with every call first bound against the real one's
    signature: a stub of an outdated signature, or a call the real function would
    refuse, fails the test on every platform instead of passing silently.

    `target` may be a module, a class (the replacement then takes `self`) or an
    instance (it doesn't). Returns the installed wrapper.
    """
    real = getattr(target, name)
    signature = inspect.signature(real)

    @functools.wraps(real)  # introspection, a nested stub included, sees the real signature
    def checked(*args, **kwargs):
        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            message = f"{name}{signature} called as ({_call_text(args, kwargs)}): {exc}"
            signature_mismatches.append(f"{os.environ.get('PYTEST_CURRENT_TEST', '<collection>')}: {message}")
            raise TypeError(f"stub of {message}") from None
        return replacement(*args, **kwargs)

    monkeypatch.setattr(target, name, checked)
    return checked


def _call_text(args, kwargs) -> str:
    return ", ".join([*(type(a).__name__ for a in args), *(f"{k}=" for k in kwargs)])
