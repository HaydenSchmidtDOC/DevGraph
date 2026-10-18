"""Which repo-relative paths are test code, by convention.

One definition shared by the datastore extractor (a test file's clients are
not read) and the MCP tools (test callers and dependents are listed after
production ones). `TEST_PATH_CYPHER` is the same rule as a Cypher `=~`
pattern: written with only what Python's `re` and Java's regex agree on
(non-capturing groups, character classes, `^`/`$` anchors), and matched
against the whole string, as `=~` matches.
"""

import re

#: A folder that holds tests (or their data): `test/`, `tests/`, `spec/`,
#: `e2e/`, `__tests__/`, `__mocks__/`, `testdata/`, `fixtures/`, a JVM
#: `src/test/`, a .NET test project (`Shop.Tests/`, `Shop.UnitTests/`,
#: `IntegrationTests/`).
TEST_DIR_PATTERN = (
    r"(?:^|/)(?:tests?|spec|e2e|__tests__|__mocks__|testdata|fixtures|src/test"
    r"|(?:[^/]+\.)?(?:Unit|Integration)?Tests)/"
)
#: A test file's name: Python `test_*.py`, `*_test.py`, `conftest.py`; Go
#: `*_test.go`; Rust `*_test.rs`; C++ `*_test.cc`/`.cpp`, `test_*.cc`/`.cpp`;
#: Ruby `*_spec.rb`; JS/TS `*.spec.ts`, `*.test.jsx` and the like; Java,
#: Kotlin and C# `*Test.java`, `*Tests.cs`; Java `TestFoo.java`, `FooIT.java`.
TEST_FILE_PATTERN = (
    r"(?:^|/)(?:test_[^/]*\.(?:py|cc|cpp)|[^/]*_test\.(?:py|go|rs|cc|cpp)|conftest\.py|[^/]*_spec\.rb"
    r"|[^/]*\.(?:spec|test)\.[cm]?[jt]sx?|[^/]*Tests?\.(?:cs|java|kt)|Test[A-Z][^/]*\.java|[^/]*[a-z]IT\.java)$"
)
TEST_DIR = re.compile(TEST_DIR_PATTERN)
TEST_FILE = re.compile(TEST_FILE_PATTERN)
TEST_PATH = re.compile(f"{TEST_DIR_PATTERN}|{TEST_FILE_PATTERN}")
#: `TEST_PATH` for Cypher's `=~`, which must match the whole path.
TEST_PATH_CYPHER = f".*(?:{TEST_DIR_PATTERN}|{TEST_FILE_PATTERN}).*"


def is_test_path(path: str | None) -> bool:
    """Whether a repo-relative path (`/`-separated) is test code; None or "" is not."""
    return bool(path) and TEST_PATH.search(path) is not None
