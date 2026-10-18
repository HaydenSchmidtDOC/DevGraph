"""devgraph/test_paths.py: which repo-relative paths count as test code, and the
Cypher form of the rule matching the same paths under Python's regex."""

import re

import pytest

from devgraph.test_paths import TEST_DIR, TEST_PATH_CYPHER, is_test_path

CASES = [
    # Test folders.
    ("tests/test_a.py", True), ("pkg/tests/helpers.py", True), ("test/a.js", True), ("web/__tests__/a.js", True),
    ("web/__mocks__/api.js", True), ("app/src/test/java/a/A.java", True), ("Shop.Tests/Cart.cs", True),
    ("Shop.UnitTests/Cart.cs", True), ("UnitTests/Cart.cs", True), ("IntegrationTests/Cart.cs", True),
    ("spec/models/user_spec.rb", True), ("spec/helper.rb", True), ("e2e/login.ts", True), ("crate/tests/it.rs", True),
    ("pkg/testdata/input.go", True), ("tests/fixtures/a.py", True), ("fixtures/users.py", True),
    # Test file names.
    ("a/test_cli.py", True), ("a/cli_test.py", True), ("conftest.py", True), ("pkg/x_test.go", True),
    ("src/parse_test.rs", True), ("src/parse_test.cc", True), ("src/parse_test.cpp", True), ("src/test_parse.cpp", True),
    ("lib/user_spec.rb", True), ("web/a.spec.ts", True), ("web/a.test.tsx", True), ("web/a.test.mjs", True),
    ("src/CartTest.java", True), ("src/CartTests.cs", True), ("src/CartTest.kt", True), ("src/TestCart.java", True),
    ("src/CartServiceIT.java", True),
    # Production code.
    ("devgraph/indexer/dispatch.py", False), ("scripts/measure.py", False), ("contest.py", False),
    ("latest/a.py", False), ("src/testing.py", False), ("attestation/x.go", False), ("src/Testimonial.java", False),
    ("src/AUDIT.java", False), ("src/Kit.java", False), ("src/specs.py", False), ("api/openapi.spec.yaml", False),
    ("src/contest_test_helpers/a.py", False), ("lib/protest.rb", False), ("src/latest_spec_list.py", False),
    ("Tests.md", False), ("src/fixture.py", False), ("src/e2e_config.py", False), ("mytests/a.py", False),
    (None, False), ("", False),
]


@pytest.mark.parametrize(("path", "expected"), CASES)
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


@pytest.mark.parametrize(("path", "expected"), [(p, e) for p, e in CASES if p])
def test_the_cypher_form_matches_the_whole_path_alike(path, expected):
    assert (re.fullmatch(TEST_PATH_CYPHER, path) is not None) is expected


def test_test_dir_is_folders_only():
    assert TEST_DIR.search("tests/a.py") and not TEST_DIR.search("a/test_cli.py")
