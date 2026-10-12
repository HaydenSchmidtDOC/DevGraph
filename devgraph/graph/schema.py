"""Node labels and relationship types from Design Brief #1.

Every node label listed here must carry a `repo_id` property except
`Repository` itself, which is the scoping root. Do not add new labels or
relationship types without updating the brief — this module is the single
source of truth extractors and MCP tools should import from rather than
hardcoding label strings.
"""

import re
from collections.abc import Iterable
from typing import NamedTuple

NODE_LABELS: tuple[str, ...] = (
    "Repository",
    "Container",
    "Service",
    "Module",
    "Class",
    "Function",
    "Endpoint",
    "Database",
    "VectorStore",
    "Queue",
    # Phase 2 — human-authored intent, linked to the code graph.
    "Requirement",
    "DesignDecision",
    "ArchitectureNote",
    "Document",
    # Phase 3 — git history, PR/issue knowledge.
    "Commit",
    "PullRequest",
    "Issue",
)

RELATIONSHIP_TYPES: tuple[str, ...] = (
    "CONTAINS",
    "CALLS",
    "IMPORTS",
    "USES",
    "RUNS",
    "WRITES_TO",
    "READS_FROM",
    "IMPLEMENTS",
    "DEPENDS_ON",
    "EXTENDS",
    # Phase 2
    "SATISFIES",
    "DOCUMENTED_BY",
    "DECIDED_BY",
    "SUPERSEDES",
    "MENTIONS",
    # Phase 3
    "MODIFIES",
    "RESOLVES",
    "REFERENCES",
)

# Node property names DevGraph itself owns. `repo_id`/`name`/`file` are the
# identity key components (see graph/engine.py `identity_key`), and
# `source_file`/`source`/`sources` are the provenance properties per-file
# delete cleanup keys off, and `claims` records what each of a shared node's
# `sources` wrote (see graph/engine.py `_claims_of`). A per-project schema may not redeclare any of
# them: a user-defined field of the same name would silently collide with
# the value the pipeline writes. `extractor` marks nodes a schema-declared
# provider owns (see devgraph/indexer/providers/). `name_refs`,
# `name_ref_targets` and `name_ref_sources` record a code Module's by-name
# edges (see devgraph/indexer/common.py `name_ref_properties`). `mkeys` is a
# code symbol's match keys, which a pin's `@` flag tests (see `split_pin`).
RESERVED_NODE_PROPERTIES: frozenset[str] = frozenset({
    "repo_id", "name", "file", "source_file", "source", "sources", "claims", "extractor",
    "name_refs", "name_ref_targets", "name_ref_sources", "mkeys",
})

# The pipeline's own bookkeeping among those, plus the Repository node's
# `skipped_files` (the files a scan left out, see dispatch `_skip_marks`):
# hidden wherever a node's properties are shown (describe_node, the
# dashboard's node inspector).
INTERNAL_NODE_PROPERTIES: frozenset[str] = frozenset({
    "claims", "extractor", "name_refs", "name_ref_targets", "name_ref_sources", "mkeys", "skipped_files",
})

# An extracted edge's bookkeeping: `origins`, the files that wrote it
# (devgraph/graph/engine.py `_ADD_ORIGIN`). The dashboard's edge inspector
# hides it, from the same served list as the node properties above.
INTERNAL_EDGE_PROPERTIES: frozenset[str] = frozenset({"origins"})

# Labels other than Repository must be uniquely keyed on (repo_id, name)
# so incremental MERGE writes update in place instead of duplicating.
_REPO_SCOPED_LABELS = tuple(l for l in NODE_LABELS if l != "Repository")

# Class/Function/Service are keyed on (repo_id, name, file) instead: a bare
# name isn't unique across files (two files can each define a function
# called `main`, or two compose files can each declare an unrelated service
# called `api`), and a single (repo_id, name) constraint was silently
# merging those into one shared node. Every other label's `name` is either
# already a real file path (Module) or effectively singleton per repo
# (Endpoint, Database, ...), so it doesn't need the extra key component —
# and Container deliberately stays bare-name keyed too, since it represents
# a shared base image, not a per-file entity (see
# indexer/dispatch.py:_upsert_container_result).
FILE_SCOPED_LABELS = ("Class", "Function", "Service")

# The labels a file's own nodes can carry, by the property that names the
# file. A per-file re-index finds them through one `(repo_id, <property>)`
# index per label (see `lookup_index_statements`), never by scanning every
# node: `file` is on the file-scoped labels; `source_file` on a code Module,
# a docs note (Requirement, DesignDecision, ArchitectureNote), a mentions
# Document, and on Class/Function/Service nodes an older index wrote.
FILE_LABELS: tuple[str, ...] = FILE_SCOPED_LABELS
SOURCE_FILE_LABELS: tuple[str, ...] = (
    "Module", "Class", "Function", "Service", "Requirement", "DesignDecision", "ArchitectureNote", "Document",
)
# Shared nodes several files can claim through `source`/`sources` (see
# graph/engine.py `_claim_nodes_tx`): Container, Endpoint, a route's
# file-less handler-stub Function, a C++ out-of-class method's file-less
# Class stub, every datastore type (Cache included, though it is not in
# NODE_LABELS), and the file-less Service an older index wrote. Every
# claimed node has a `source` (its first claim's).
CLAIMED_LABELS: tuple[str, ...] = (
    "Container", "Endpoint", "Class", "Function", "Service", "Database", "VectorStore", "Queue", "Cache",
)
# Every built-in label with a `(repo_id, name)` index: the uniqueness
# constraints' and the file-scoped lookups', and Cache's own (it has no
# constraint). A query over every node of a repository seeks these, never
# `{repo_id}` alone.
NAMED_LABELS: tuple[str, ...] = tuple(label for label in NODE_LABELS if label != "Repository") + ("Cache",)


def constraint_statements() -> list[str]:
    """Cypher to create uniqueness constraints for every node label.

    Idempotent — `IF NOT EXISTS` makes this safe to run on every startup.
    Class/Function additionally DROP their old two-property constraint
    first: changing a constraint's definition in place isn't something
    `CREATE CONSTRAINT ... IF NOT EXISTS` can do (a same-named constraint
    with a different definition is just left alone), so an explicit drop is
    the only way an already-provisioned Neo4j instance picks up the new key.
    """
    statements = [
        "CREATE CONSTRAINT repository_id IF NOT EXISTS "
        "FOR (r:Repository) REQUIRE r.repo_id IS UNIQUE"
    ]
    for label in _REPO_SCOPED_LABELS:
        if label in FILE_SCOPED_LABELS:
            statements.append(f"DROP CONSTRAINT {label.lower()}_repo_name IF EXISTS")
            statements.append(
                f"CREATE CONSTRAINT {label.lower()}_repo_name_file IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE (n.repo_id, n.name, n.file) IS UNIQUE"
            )
        else:
            statements.append(
                f"CREATE CONSTRAINT {label.lower()}_repo_name IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE (n.repo_id, n.name) IS UNIQUE"
            )
    return statements


def lookup_index_statements() -> list[str]:
    """Cypher for the RANGE lookup indexes the built-in queries seek.

    A `(repo_id, name)` index on each file-scoped label: their uniqueness
    constraint indexes `(repo_id, name, file)`, which a bare-name match (an
    edge's target end, `describe_node`) can't seek, so without these it
    scans every node of the label; the same on Cache, which has no
    constraint (named so it never reads as a user type's generated index).
    Module's `(repo_id, dir)`, `(repo_id, dir_name)` and `(repo_id,
    basename)`, which a pinned IMPORTS row seeks (see engine._end_match). Then the provenance indexes a per-file re-index seeks (`(repo_id, file)`,
    `(repo_id, source_file)`, `(repo_id, source)` on FILE_LABELS,
    SOURCE_FILE_LABELS, CLAIMED_LABELS), so its cost follows the file, not
    the whole database. Idempotent, like
    `constraint_statements`; the names never collide with a generated
    user-type constraint or index (built-in labels can't be redeclared).
    """
    statements = [
        f"CREATE INDEX {label.lower()}_repo_name_lookup IF NOT EXISTS FOR (n:{label}) ON (n.repo_id, n.name)"
        for label in FILE_SCOPED_LABELS
    ]
    statements.append("CREATE INDEX cache_repo_name_index IF NOT EXISTS FOR (n:Cache) ON (n.repo_id, n.name)")
    statements += [
        f"CREATE INDEX module_repo_{prop}_lookup IF NOT EXISTS FOR (n:Module) ON (n.repo_id, n.{prop})"
        for prop in ("dir", "dir_name", "basename")
    ]
    for prop, labels in (("file", FILE_LABELS), ("source_file", SOURCE_FILE_LABELS), ("source", CLAIMED_LABELS)):
        statements += [
            f"CREATE INDEX {label.lower()}_repo_{prop}_lookup IF NOT EXISTS FOR (n:{label}) ON (n.repo_id, n.{prop})"
            for label in labels
        ]
    return statements


# The `name_refs` entry encoding (see devgraph/indexer/common.py
# `name_ref_properties`). Kept here, with no imports, so the graph engine and
# the indexer read them without importing each other.
#: Joins the fields of one `name_refs` entry.
NAME_REF_SEP = "\x1f"
#: Joins an entry's target pins.
NAME_REF_PIN_SEP = "\x1e"
#: A target pin to the file-less node ("" as a `to_file`).
NAME_REF_FILELESS = "\x1d"


# Path pins: a relationship row's `to_file` (see devgraph/indexer/common.py
# `pin_matches`, which the engine's Cypher mirrors). Kept here, with no
# imports, for the same reason as the name_refs encoding above.
#: Path pin kinds, highest precedence first. `loose_dir` is a directory pin
#: at package confidence (a method named on a typed receiver: another type
#: of the package can have a method of the name).
PIN_KINDS: tuple[str, ...] = ("file", "dir", "loose_dir", "suffix", "suffix_dir", "prefix")
#: Every pin kind's edge confidence: a pin's confidence never depends on the
#: other pins it was written with.
PIN_CONFIDENCE: dict[str, str] = {
    "file": "resolved", "dir": "resolved", "loose_dir": "package", "suffix": "package", "suffix_dir": "package",
    "prefix": "package",
}
#: The directory names a pin's `^` flag keeps out of the source root a file
#: is under (the part of its path before what the pin matches): a vendored
#: copy of code (`third_party/acme-util/...`) is not what an import names.
VENDORED_DIRS: tuple[str, ...] = ("third_party", "third-party", "thirdparty", "vendor", "vendored")
#: What `^t` keeps out besides: a conventional test source root, which main
#: code never sees: a Gradle/Maven test source set (`src/test`,
#: `src/androidTest`, ...) or a module's `test` directory holding language
#: source sets (`lib/test/java`). Each is a path fragment the source root,
#: wrapped in "/", must not contain; a directory merely named `test` (a
#: package, a tools folder) is not one.
TEST_ROOTS: tuple[str, ...] = (
    "/src/test/", "/src/androidTest/", "/src/testFixtures/", "/src/integrationTest/", "/test/java/",
    "/test/kotlin/",
)
#: The characters that start a path pin's flags (see `split_pin`); a resolver
#: never writes a path pin whose own path holds one.
PIN_FLAG_CHARS = "~?^!@"
#: The most match keys a pin's `@` flag carries (see `scoped_pin`).
MAX_SCOPE_KEYS = 32


class PinParts(NamedTuple):
    """A pin split into its path and its flags (see `split_pin`)."""

    base: str
    loose: bool = False
    only: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    not_suffix: str = ""
    skip_roots: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()


def _is_path_pin(pin: str) -> bool:
    return pin == "." or pin.startswith("/") or pin.endswith(("/", "/."))


_PIN_FLAGS = re.compile(
    r"(~)?(?:\?([^~?^!@/]*))?(\^t?)?(?:!([^~?^!@/]*))?(?:@([\w.+]+:[\w.+]+(?:,[\w.+]+:[\w.+]+)*))?"
)


def split_pin(pin: str) -> PinParts:
    """A pin and its flags, in this order after the path: `~` (a directory
    pin at package confidence, kind `loose_dir`), `?a,b` (a matched file must
    end with one of these suffixes: a language's extensions), `^` (no
    directory of the source root a matched file is under, the part of its
    path before what the pin matches, is vendored: `VENDORED_DIRS`; `^t`
    nor a test root: `TEST_ROOTS`), `!x` (a matched file must not end with
    `x`) and `@k1,k2` (the matched node's `mkeys` must hold one of these
    match keys, each `namespace:name`; see `scoped_pin`):
    `go/b/.~?.go!_test.go` is the Go files directly in go/b/, tests left
    out, at package confidence. Only a path pin (no repository-relative
    path is `.`, starts with "/" or ends with "/" or "/.") takes flags, so a
    file named `a!b.go` stays a file pin. A path pin whose own path holds a
    flag character (a directory named `x!y`) would be split there, so it is
    not supported: a resolver never writes one (`PIN_FLAG_CHARS`)."""
    for i, char in enumerate(pin):
        if char in PIN_FLAG_CHARS and _is_path_pin(pin[:i]):
            flags = _PIN_FLAGS.fullmatch(pin, i)
            if flags:
                loose, only, skip, not_suffix, scope = flags.groups()
                return PinParts(
                    pin[:i], bool(loose), tuple(s for s in (only or "").split(",") if s),
                    VENDORED_DIRS if skip else (), not_suffix or "", TEST_ROOTS if skip == "^t" else (),
                    tuple(sorted(set(scope.split(",")))) if scope else (),
                )
    return PinParts(pin)


def scoped_pin(pin: str, keys: Iterable[str]) -> str:
    """The path pin `pin` (no `@` flag of its own) limited to the nodes whose
    `mkeys` hold one of `keys`, written canonically (sorted, without
    repeats). With no keys, or more than MAX_SCOPE_KEYS, it is `pin` as it
    is: the site links by its path alone."""
    scope = sorted(set(keys))
    return pin + "@" + ",".join(scope) if 0 < len(scope) <= MAX_SCOPE_KEYS else pin


def unscoped_pin(pin: str) -> tuple[str, tuple[str, ...]]:
    """`pin` less its `@` flag's keys (the flag itself kept, as "@"), and
    those keys: a `name_refs` entry stores them once, not with each pin."""
    scope = split_pin(pin).scope
    return (pin.rsplit("@", 1)[0] + "@", scope) if scope else (pin, ())


def pin_root(pin: str, path: str) -> str:
    """The part of `path` before what the pin `pin` (one that matches it)
    matches: for a suffix pin the path less that suffix, for a suffix
    directory pin its directory less that suffix, else the whole path. The
    `^` flag tests its directories."""
    base = split_pin(pin).base
    kind = pin_kind(pin)
    if kind == "suffix":
        return ("/" + path)[: len(path) + 1 - len(base)]
    if kind == "suffix_dir":
        name = path.rsplit("/", 1)[-1]
        return ("/" + path)[: len(path) + 1 - len(name) - (len(base) - 1)]
    return path


def pin_kind(pin: str | None) -> str:
    """How a `to_file` matches: "name" (None: by name alone), "fileless" (""),
    "file", "dir" (`a/b/.`: files directly in a/b/; `.`: the root's files),
    "loose_dir" (the same, flagged `~`), "suffix" (`/a/b/C.java`: files whose
    "/"-prefixed path ends with it), "suffix_dir" (`/a/b/.`: files directly
    in any directory ending /a/b/) or "prefix" (`a/b/`: files anywhere under
    a/b/)."""
    if pin is None:
        return "name"
    parts = split_pin(pin)
    base = parts.base
    if base == "":
        return "fileless"
    if base == "." or base.endswith("/."):
        if base.startswith("/"):
            return "suffix_dir"
        return "loose_dir" if parts.loose else "dir"
    if base.startswith("/"):
        return "suffix"
    return "prefix" if base.endswith("/") else "file"


def pin_value(pin: str) -> dict:
    """A path pin as the map the engine's Cypher tests (`k` how it matches,
    `v` the path, directory or suffix it tests, `ns` the suffix a file must
    not end with, `os` the suffixes it must end with one of (empty: any),
    `nv` the directory names its source root may not hold, `pin_root`, `nr`
    the fragments it may not contain, wrapped in "/", `sc` the match keys
    one of which the node's `mkeys` must hold (empty: any node)): a
    directory pin's `v` is the directory with its trailing "/" ("" for the
    root), so `dir` and `suffix_dir` drop the final "."; a `loose_dir`
    matches as a `dir`."""
    parts = split_pin(pin)
    kind = pin_kind(pin)
    kind = "dir" if kind == "loose_dir" else kind
    value = parts.base[:-1] if kind in ("dir", "suffix_dir") else parts.base
    return {
        "k": kind, "v": value, "ns": parts.not_suffix, "os": list(parts.only), "nv": list(parts.skip),
        "nr": list(parts.skip_roots), "sc": list(parts.scope),
    }

