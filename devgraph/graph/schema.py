"""Node labels and relationship types from Design Brief #1.

Every node label listed here must carry a `repo_id` property except
`Repository` itself, which is the scoping root. Do not add new labels or
relationship types without updating the brief — this module is the single
source of truth extractors and MCP tools should import from rather than
hardcoding label strings.
"""

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
_FILE_SCOPED_LABELS = ("Class", "Function", "Service")


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
        if label in _FILE_SCOPED_LABELS:
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
