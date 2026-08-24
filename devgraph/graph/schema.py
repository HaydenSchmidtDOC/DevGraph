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
)

# Labels other than Repository must be uniquely keyed on (repo_id, name)
# so incremental MERGE writes update in place instead of duplicating.
_REPO_SCOPED_LABELS = tuple(l for l in NODE_LABELS if l != "Repository")


def constraint_statements() -> list[str]:
    """Cypher to create uniqueness constraints for every node label.

    Idempotent — `IF NOT EXISTS` makes this safe to run on every startup.
    """
    statements = [
        "CREATE CONSTRAINT repository_id IF NOT EXISTS "
        "FOR (r:Repository) REQUIRE r.repo_id IS UNIQUE"
    ]
    for label in _REPO_SCOPED_LABELS:
        statements.append(
            f"CREATE CONSTRAINT {label.lower()}_repo_name IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE (n.repo_id, n.name) IS UNIQUE"
        )
    return statements
