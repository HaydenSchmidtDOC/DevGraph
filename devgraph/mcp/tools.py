"""High-level MCP tool implementations against the DevGraph graph schema.

Each tool accepts a GraphEngine and returns structured data without exposing
raw Cypher to the AI. Tools filter by repo_id by default and support an
explicit cross_repo flag to opt-in to cross-repository results.

All Cypher is parameterized — user input never concatenates directly into
query strings.
"""

from pathlib import Path
from typing import Any

import git

from devgraph.graph.engine import GraphEngine
from devgraph.graph import schema
from devgraph.registry.store import RepoRegistry


def _envelope(items: list[Any], max_results: int) -> dict[str, Any]:
    """Wrap a list result with count/truncation metadata so callers can see
    the full match count without paying token cost for every row.
    """
    return {
        "count": len(items),
        "results": items[:max_results],
        "truncated": len(items) > max_results,
    }


def search_component(
    engine: GraphEngine,
    repo_id: str,
    query: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Search for components (modules, services, classes, functions) by name or description.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to search within (unless cross_repo=True)
        query: Search term (name substring or description keyword)
        cross_repo: If True, search across all repos; if False, limit to repo_id
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag. Note: count maxes out at 50
        (the Cypher LIMIT cap) even if more matches exist beyond that.
    """
    repo_filter = "" if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n)
    WHERE (n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint)
    AND (toLower(n.name) CONTAINS toLower($query) OR toLower(n.description) CONTAINS toLower($query))
    {repo_filter}
    RETURN n.name as name, labels(n) as labels, n.repo_id as repo_id,
           n.description as description LIMIT 50
    """
    params = {"query": query}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return _envelope(results, max_results)


def trace_request_flow(
    engine: GraphEngine,
    repo_id: str,
    start_endpoint: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Trace the request flow from an endpoint through services, datastores, and queues.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID (or cross-repo search if cross_repo=True)
        start_endpoint: Name of the endpoint to start tracing from
        cross_repo: If True, cross repository boundaries

    Returns:
        Dict containing the flow path and all traversed components
    """
    repo_filter = "" if cross_repo else "WHERE start.repo_id = $repo_id"
    cypher = f"""
    MATCH (start:Endpoint {{name: $endpoint_name}})
    {repo_filter}
    OPTIONAL MATCH path = (start)-[*1..5]->(node)
    WITH COLLECT(DISTINCT node) + COLLECT(DISTINCT start) as all_nodes,
         COLLECT(DISTINCT path) as paths
    UNWIND paths as p
    WITH all_nodes, COLLECT(DISTINCT relationships(p)) as all_rels
    UNWIND all_rels as rel_list
    UNWIND rel_list as rel
    WITH all_nodes, COLLECT(DISTINCT rel) as edges
    RETURN
        [n IN all_nodes | {{name: n.name, labels: labels(n), repo_id: n.repo_id}}] as components,
        [r IN edges | {{type: type(r)}}] as edges
    LIMIT 1
    """
    params = {"endpoint_name": start_endpoint}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    if results:
        return results[0]
    return {"components": [], "edges": []}


def get_service_dependencies(
    engine: GraphEngine,
    repo_id: str,
    service_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Get all dependencies (services, datastores, queues) for a given service.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        service_name: Name of the service to analyze
        cross_repo: If True, include cross-repo dependencies

    Returns:
        Dict with direct_dependencies, transitive_dependencies, and potential_impacts
    """
    repo_filter = "" if cross_repo else "WHERE s.repo_id = $repo_id"
    cypher = f"""
    MATCH (s:Service {{name: $service_name}})
    {repo_filter}
    OPTIONAL MATCH (s)-[:USES|DEPENDS_ON|RUNS]->(dep)
    OPTIONAL MATCH (s)-[:CALLS]->(called:Service)
    RETURN s.name as service,
           [d IN COLLECT(DISTINCT {{name: dep.name, type: labels(dep)[0], repo_id: dep.repo_id}}) WHERE d.name IS NOT NULL] as dependencies,
           [c IN COLLECT(DISTINCT {{name: called.name, repo_id: called.repo_id}}) WHERE c.name IS NOT NULL] as calls
    """
    params = {"service_name": service_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    if results:
        return results[0]
    return {"service": service_name, "dependencies": [], "calls": []}


def find_callers(
    engine: GraphEngine,
    repo_id: str,
    target_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    scope_to_class: str | None = None,
) -> dict[str, Any]:
    """Find all functions, services, or endpoints that call a given target.

    CALLS edges are name-based, not type-resolved: a call to `target_name`
    made from inside any class's method links to every Function node named
    `target_name` repo-wide, which can surface unrelated same-named methods
    as noise. When a method-body call's enclosing class is known at index
    time, the edge carries a `caller_class` property recording it — pass
    scope_to_class to narrow results to callers made from within a specific
    class's own methods (opt-in; omitted, behavior is unchanged/repo-wide).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        target_name: Name of the function/service/endpoint being called
        cross_repo: If True, find callers across repos
        max_results: Maximum number of results to return in the envelope
        scope_to_class: If given, only return callers whose call to
            target_name was made from within this class's own method bodies

    Returns:
        Dict with count, results, and truncated flag containing callers with their types and locations
    """
    repo_filter = "" if cross_repo else "AND target.repo_id = $repo_id"
    class_filter = "AND rel.caller_class = $scope_to_class" if scope_to_class else ""
    cypher = f"""
    MATCH (caller)-[rel:CALLS]->(target)
    WHERE target.name = $target_name
    {repo_filter}
    {class_filter}
    RETURN DISTINCT caller.name as name, labels(caller) as type, caller.repo_id as repo_id
    ORDER BY caller.name
    """
    params = {"target_name": target_name}
    if not cross_repo:
        params["repo_id"] = repo_id
    if scope_to_class:
        params["scope_to_class"] = scope_to_class

    results = engine.run_cypher(cypher, params)
    return _envelope(results, max_results)


def find_related_files(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Find all files related to a component (via CONTAINS, IMPORTS, CALLS relationships).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component
        cross_repo: If True, include cross-repo relationships
        max_results: Maximum number of results per list to return in the envelope

    Returns:
        Dict with containing_modules, imported_modules, and related_components,
        each wrapped as {count, results, truncated} envelope objects
    """
    repo_filter = "" if cross_repo else "WHERE n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})
    {repo_filter}
    OPTIONAL MATCH (m:Module)-[:CONTAINS*]->(n)
    OPTIONAL MATCH (n)-[:IMPORTS]->(imported)
    OPTIONAL MATCH (n)-[:CALLS]->(related)
    RETURN
        COLLECT(DISTINCT m.name) as containing_modules,
        COLLECT(DISTINCT imported.name) as imported_modules,
        COLLECT(DISTINCT related.name) as related_components
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    if results:
        row = results[0]
        return {
            "containing_modules": _envelope(row["containing_modules"], max_results),
            "imported_modules": _envelope(row["imported_modules"], max_results),
            "related_components": _envelope(row["related_components"], max_results),
        }
    return {
        "containing_modules": _envelope([], max_results),
        "imported_modules": _envelope([], max_results),
        "related_components": _envelope([], max_results),
    }


def summarise_repository(
    engine: GraphEngine,
    repo_id: str,
) -> dict[str, Any]:
    """Get a high-level summary of a repository's architecture.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to summarize

    Returns:
        Summary with service count, module count, dependency graph stats
    """
    # Each count runs as its own uncorrelated subquery (CALL {...}) rather
    # than chaining OPTIONAL MATCHes in one query. Chained OPTIONAL MATCHes
    # on independent label patterns force Neo4j to compute their cartesian
    # product before COUNT(DISTINCT ...) collapses it back down — on a real
    # repo with hundreds of Function/Class nodes that product explodes
    # combinatorially and the query effectively hangs. Subqueries keep each
    # count's cardinality independent.
    count_cypher = """
    CALL () { MATCH (s:Service {repo_id: $repo_id}) RETURN COUNT(s) as service_count }
    CALL () { MATCH (m:Module {repo_id: $repo_id}) RETURN COUNT(m) as module_count }
    CALL () { MATCH (c:Class {repo_id: $repo_id}) RETURN COUNT(c) as class_count }
    CALL () { MATCH (f:Function {repo_id: $repo_id}) RETURN COUNT(f) as function_count }
    CALL () { MATCH (e:Endpoint {repo_id: $repo_id}) RETURN COUNT(e) as endpoint_count }
    CALL () { MATCH (d:Database {repo_id: $repo_id}) RETURN COUNT(d) as database_count }
    CALL () { MATCH (v:VectorStore {repo_id: $repo_id}) RETURN COUNT(v) as vectorstore_count }
    CALL () { MATCH (q:Queue {repo_id: $repo_id}) RETURN COUNT(q) as queue_count }
    RETURN
        $repo_id as repo_name,
        service_count, module_count, class_count, function_count,
        endpoint_count, database_count, vectorstore_count, queue_count
    """
    results = engine.run_cypher(count_cypher, {"repo_id": repo_id})
    if results:
        return results[0]
    return {
        "repo_name": repo_id,
        "service_count": 0,
        "module_count": 0,
        "class_count": 0,
        "function_count": 0,
        "endpoint_count": 0,
        "database_count": 0,
        "vectorstore_count": 0,
        "queue_count": 0,
    }


def compare_branches(
    engine: GraphEngine,
    repo_id: str,
    branch_a: str,
    branch_b: str,
) -> dict[str, Any]:
    """Compare architecture between two branches (git metadata needed in graph).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        branch_a: First branch name
        branch_b: Second branch name

    Returns:
        Dict with components added, removed, and changed between branches

    Note: This is a stub until git metadata is indexed (Phase 3).
    """
    # Placeholder: full implementation requires git history indexing (Phase 3)
    return {
        "added_in_b": [],
        "removed_in_b": [],
        "changed": [],
        "note": "Git history integration planned for Phase 3",
    }


def impact_analysis(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Analyze the impact of changing a component on the rest of the system.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component to analyze
        cross_repo: If True, include cross-repo impacts
        max_results: Maximum number of results per dependents list to return in the envelope

    Returns:
        Dict with direct_dependents and transitive_dependents wrapped as {count, results, truncated}
        envelopes, plus risk_level (computed from true untruncated count)
    """
    repo_filter = "" if cross_repo else "WHERE n.repo_id = $repo_id"
    dependent_filter = (
        "" if cross_repo else "WHERE dependent IS NULL OR dependent.repo_id = $repo_id"
    )
    transitive_filter = (
        "" if cross_repo else "WHERE transitive IS NULL OR transitive.repo_id = $repo_id"
    )
    cypher = f"""
    MATCH (n {{name: $component_name}})
    {repo_filter}
    OPTIONAL MATCH (dependent)-[:CALLS|USES|DEPENDS_ON]->(n)
    {dependent_filter}
    OPTIONAL MATCH (transitive)-[:CALLS|USES|DEPENDS_ON*2..]->(n)
    {transitive_filter}
    RETURN
        COLLECT(DISTINCT {{name: dependent.name, type: labels(dependent)[0]}}) as direct_dependents,
        COLLECT(DISTINCT {{name: transitive.name, type: labels(transitive)[0]}}) as transitive_dependents,
        CASE
            WHEN COUNT(DISTINCT dependent) > 10 THEN 'high'
            WHEN COUNT(DISTINCT dependent) > 3 THEN 'medium'
            ELSE 'low'
        END as risk_level
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    if results:
        row = results[0]
        return {
            "direct_dependents": _envelope(row["direct_dependents"], max_results),
            "transitive_dependents": _envelope(row["transitive_dependents"], max_results),
            "risk_level": row["risk_level"],
        }
    return {
        "direct_dependents": _envelope([], max_results),
        "transitive_dependents": _envelope([], max_results),
        "risk_level": "low",
    }


def impact_analysis_for_diff(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    base_ref: str,
    head_ref: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Analyze the combined impact of every component changed between two git refs.

    Composes a local git diff (GitPython, no network — same constraint as
    index-history) with the same dependent-tracing Cypher impact_analysis
    uses, across every component touched by the diff at once, then unions
    and deduplicates the result. Both refs must already exist locally —
    this never fetches from a remote.

    Args:
        engine: GraphEngine instance
        registry: RepoRegistry, used to resolve repo_id to its registered root path
        repo_id: Repository ID
        base_ref: Git ref (branch/tag/sha) to diff from; must resolve locally
        head_ref: Git ref (branch/tag/sha) to diff to; must resolve locally
        cross_repo: If True, include cross-repo impacts
        max_results: Maximum number of results per dependents list to return in the envelope

    Returns:
        Dict with changed_files, changed_components, direct_dependents and
        transitive_dependents (each {count, results, truncated} envelopes),
        and risk_level. On an invalid ref or unregistered repo, returns an
        empty result with an "error" key instead of raising.
    """
    empty = {
        "changed_files": [],
        "changed_components": [],
        "direct_dependents": _envelope([], max_results),
        "transitive_dependents": _envelope([], max_results),
        "risk_level": "low",
    }

    repo = registry.get(repo_id)
    if repo is None:
        return {**empty, "error": f"no such repo_id: {repo_id}"}

    git_repo = None
    try:
        git_repo = git.Repo(str(repo.path))
        git_repo.commit(base_ref)
        git_repo.commit(head_ref)
        diff_output = git_repo.git.diff("--name-only", f"{base_ref}..{head_ref}")
    except Exception as exc:
        return {**empty, "error": f"could not diff {base_ref}..{head_ref}: {exc}"}
    finally:
        if git_repo is not None:
            git_repo.close()

    changed_files = [line for line in diff_output.splitlines() if line]
    if not changed_files:
        return empty

    component_cypher = """
    MATCH (m:Module {repo_id: $repo_id})
    WHERE m.name IN $changed_files
    OPTIONAL MATCH (m)-[:CONTAINS*1..2]->(comp)
    WHERE comp:Function OR comp:Class
    RETURN COLLECT(DISTINCT comp.name) as components
    """
    comp_results = engine.run_cypher(component_cypher, {"repo_id": repo_id, "changed_files": changed_files})
    changed_components = [c for c in (comp_results[0]["components"] if comp_results else []) if c is not None]

    if not changed_components:
        return {**empty, "changed_files": changed_files}

    repo_filter = "" if cross_repo else "AND n.repo_id = $repo_id"
    dependent_filter = (
        "" if cross_repo else "WHERE dependent IS NULL OR dependent.repo_id = $repo_id"
    )
    transitive_filter = (
        "" if cross_repo else "WHERE transitive IS NULL OR transitive.repo_id = $repo_id"
    )
    impact_cypher = f"""
    MATCH (n)
    WHERE n.name IN $changed_components
    {repo_filter}
    OPTIONAL MATCH (dependent)-[:CALLS|USES|DEPENDS_ON]->(n)
    {dependent_filter}
    OPTIONAL MATCH (transitive)-[:CALLS|USES|DEPENDS_ON*2..]->(n)
    {transitive_filter}
    RETURN
        COLLECT(DISTINCT {{name: dependent.name, type: labels(dependent)[0]}}) as direct_dependents,
        COLLECT(DISTINCT {{name: transitive.name, type: labels(transitive)[0]}}) as transitive_dependents,
        COUNT(DISTINCT dependent) as direct_count
    """
    params = {"changed_components": changed_components}
    if not cross_repo:
        params["repo_id"] = repo_id

    impact_results = engine.run_cypher(impact_cypher, params)
    if not impact_results:
        return {**empty, "changed_files": changed_files, "changed_components": changed_components}

    row = impact_results[0]
    direct_count = row["direct_count"]
    risk_level = "high" if direct_count > 10 else "medium" if direct_count > 3 else "low"

    return {
        "changed_files": changed_files,
        "changed_components": changed_components,
        "direct_dependents": _envelope(
            [d for d in row["direct_dependents"] if d.get("name") is not None], max_results
        ),
        "transitive_dependents": _envelope(
            [t for t in row["transitive_dependents"] if t.get("name") is not None], max_results
        ),
        "risk_level": risk_level,
    }


def explain_architecture(
    engine: GraphEngine,
    repo_id: str,
) -> dict[str, Any]:
    """Generate a high-level architectural explanation of the repository.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID

    Returns:
        Dict with layers, key services, data flow overview
    """
    cypher = """
    MATCH (s:Service {repo_id: $repo_id})
    OPTIONAL MATCH (s)-[:USES]->(ds:Database|VectorStore|Queue)
    WITH s, COLLECT(DISTINCT ds.name) as datastore_names
    OPTIONAL MATCH (ep:Endpoint {repo_id: $repo_id})-[:CALLS]->(s)
    WITH s, datastore_names, COLLECT(DISTINCT ep.name) as endpoint_names
    RETURN
        COLLECT(DISTINCT {service: s.name, uses: datastore_names}) as services_and_datastores,
        COLLECT(DISTINCT {endpoints: endpoint_names, calls: s.name}) as endpoints
    LIMIT 1
    """
    results = engine.run_cypher(cypher, {"repo_id": repo_id})
    if results:
        return {
            "services_and_datastores": results[0].get("services_and_datastores", []),
            "endpoints": results[0].get("endpoints", []),
        }
    return {"services_and_datastores": [], "endpoints": []}


def list_services(
    engine: GraphEngine,
    repo_id: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """List all services in a repository (or across repos if cross_repo=True).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID (ignored if cross_repo=True)
        cross_repo: If True, list services from all repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing services with their properties
    """
    repo_filter = "" if cross_repo else "WHERE s.repo_id = $repo_id"
    cypher = f"""
    MATCH (s:Service)
    {repo_filter}
    RETURN s.name as name, s.repo_id as repo_id, s.description as description
    ORDER BY s.repo_id, s.name
    """
    params = {}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return _envelope(results, max_results)


def explain_decision(
    engine: GraphEngine,
    repo_id: str,
    decision_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Explain a design decision: its rationale, what it documents, and history.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        decision_name: Name (id) of the DesignDecision note
        cross_repo: If True, search across repos

    Returns:
        Dict with the decision's properties, what it documents, what it
        supersedes, and any ArchitectureNote it's backed by.
    """
    repo_filter = "" if cross_repo else "WHERE d.repo_id = $repo_id"
    cypher = f"""
    MATCH (d:DesignDecision {{name: $decision_name}})
    {repo_filter}
    OPTIONAL MATCH (doc)-[:DOCUMENTED_BY]->(d)
    OPTIONAL MATCH (d)-[:SUPERSEDES]->(prior:DesignDecision)
    OPTIONAL MATCH (d)-[:DECIDED_BY]->(note:ArchitectureNote)
    RETURN d.name as name, d.title as title, d.body as body,
           COLLECT(DISTINCT doc.name) as documents,
           COLLECT(DISTINCT prior.name) as supersedes,
           COLLECT(DISTINCT note.name) as backed_by
    """
    params = {"decision_name": decision_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    if results:
        return results[0]
    return {"name": decision_name, "title": None, "body": None, "documents": [], "supersedes": [], "backed_by": []}


def find_requirements_for(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> list[dict[str, Any]]:
    """Find requirements a component (Module/Service/etc.) satisfies.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component
        cross_repo: If True, search across repos

    Returns:
        List of Requirement notes satisfied by this component
    """
    repo_filter = "" if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})-[:SATISFIES]->(r:Requirement)
    WHERE true
    {repo_filter}
    RETURN r.name as name, r.title as title, r.body as body
    ORDER BY r.name
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return results


def trace_design_rationale(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Trace the design rationale (decisions, notes, requirements) behind a component.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component (Module/Service/Class/etc.)
        cross_repo: If True, search across repos

    Returns:
        Dict grouping every Requirement/DesignDecision/ArchitectureNote linked
        to this component via SATISFIES or DOCUMENTED_BY.
    """
    repo_filter = "" if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})
    WHERE true
    {repo_filter}
    OPTIONAL MATCH (n)-[:SATISFIES]->(req:Requirement)
    OPTIONAL MATCH (n)-[:DOCUMENTED_BY]->(doc)
    RETURN n.name as component,
           COLLECT(DISTINCT {{name: req.name, title: req.title}}) as requirements,
           COLLECT(DISTINCT {{name: doc.name, title: doc.title, type: labels(doc)[0]}}) as notes
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    if results:
        return results[0]
    return {"component": component_name, "requirements": [], "notes": []}


def blame_component(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> list[dict[str, Any]]:
    """Find commits that modified a component's file, most recent first.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to look up commit history for
        cross_repo: If True, search across repos

    Returns:
        List of commits (sha, message, author, authored_date) that modified
        this component, ordered most-recent-first.
    """
    repo_filter = "" if cross_repo else "AND c.repo_id = $repo_id"
    cypher = f"""
    MATCH (c:Commit)-[:MODIFIES]->(m:Module {{name: $component_name}})
    WHERE true
    {repo_filter}
    RETURN c.name as sha, c.message as message, c.author as author,
           c.authored_date as authored_date
    ORDER BY c.authored_date DESC
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return results


def find_related_prs(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Find pull requests related to a component via commits that resolved issues touching it.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to find related PRs for
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing PullRequests linked
        (via RESOLVES on an Issue referenced by a commit that touched this component)
        to the component.
    """
    repo_filter = "" if cross_repo else "AND m.repo_id = $repo_id"
    cypher = f"""
    MATCH (m:Module {{name: $component_name}})
    WHERE true
    {repo_filter}
    MATCH (c:Commit)-[:MODIFIES]->(m)
    OPTIONAL MATCH (c)-[:REFERENCES]->(i:Issue)<-[:RESOLVES]-(pr:PullRequest)
    WITH DISTINCT pr
    WHERE pr IS NOT NULL
    RETURN pr.name as number, pr.title as title, pr.state as state, pr.url as url
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return _envelope(results, max_results)


def issue_history_for(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Find issues referenced by commits that touched a component.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to find issue history for
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing Issues referenced
        by commits that modified this component.
    """
    repo_filter = "" if cross_repo else "AND m.repo_id = $repo_id"
    cypher = f"""
    MATCH (m:Module {{name: $component_name}})
    WHERE true
    {repo_filter}
    MATCH (c:Commit)-[:MODIFIES]->(m)
    OPTIONAL MATCH (c)-[:REFERENCES]->(i:Issue)
    WITH DISTINCT i
    WHERE i IS NOT NULL
    RETURN i.name as number, i.title as title, i.state as state, i.url as url
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return _envelope(results, max_results)


def get_source(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Fetch a Function or Class's actual source text by reading its last-indexed line range.

    Reads live from disk using the graph's last-indexed start_line/end_line —
    a stale index could return the wrong lines; rescan first if freshness
    matters, same caveat as every other tool here.

    Args:
        engine: GraphEngine instance
        registry: RepoRegistry, used to resolve repo_id to its registered root path
        repo_id: Repository ID
        component_name: Name of the Function or Class to fetch source for
        cross_repo: If True, search across repos (the file is still read from
            whichever repo actually owns the matched node, via its own registry entry)

    Returns:
        Dict with name, label, file, start_line, end_line, source, and
        docstring_full (when present). Empty/None fields if no match found.
    """
    repo_filter = "" if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n)
    WHERE (n:Function OR n:Class) AND n.name = $component_name
    {repo_filter}
    RETURN n.name as name, labels(n) as labels, n.repo_id as repo_id,
           n.file as file, n.start_line as start_line, n.end_line as end_line,
           n.docstring_full as docstring_full
    LIMIT 1
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    empty = {
        "name": component_name, "label": None, "file": None,
        "start_line": None, "end_line": None, "source": None, "docstring_full": None,
    }
    if not results:
        return empty

    row = results[0]
    node_repo_id = row["repo_id"]
    file_rel_path = row["file"]
    start_line = row["start_line"]
    end_line = row["end_line"]
    if not file_rel_path or start_line is None or end_line is None:
        return empty

    repo = registry.get(node_repo_id)
    if repo is None:
        return empty

    file_path = (repo.path / file_rel_path).resolve()
    if not str(file_path).startswith(str(repo.path.resolve())):
        return empty  # never read outside the registered repo root

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return empty

    source_text = "\n".join(lines[start_line - 1 : end_line])
    label = next((l for l in row["labels"] if l in ("Function", "Class")), row["labels"][0])

    return {
        "name": row["name"],
        "label": label,
        "file": file_rel_path,
        "start_line": start_line,
        "end_line": end_line,
        "source": source_text,
        "docstring_full": row.get("docstring_full"),
    }


def find_mentions(
    engine: GraphEngine,
    repo_id: str,
    name: str,
    label: str | None = None,
    direction: str = "mentioned_by",
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Find Document nodes that mention an entity, or what a Document mentions.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        name: Entity name (for direction="mentioned_by") or Document repo-relative path (for direction="mentions")
        label: Optional node label filter for the target entity (validates against NODE_LABELS)
        direction: "mentioned_by" (default) to find Documents mentioning the entity, or
                   "mentions" to find what a Document mentions
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing nodes with name, label, and repo_id
    """
    # Validate and reject unrecognized labels
    if label is not None and label not in schema.NODE_LABELS:
        return _envelope([], max_results)

    # Build label filter using node label check (not parameterized label in node pattern)
    label_filter = f"AND $label IN labels(target)" if label else ""

    if direction == "mentions":
        # Document mentions target: (d:Document {name: $name})-[:MENTIONS]->(target)
        repo_filter = "" if cross_repo else "AND d.repo_id = $repo_id"
        cypher = f"""
        MATCH (d:Document {{name: $name}})-[:MENTIONS]->(target)
        WHERE true
        {repo_filter}
        {label_filter}
        RETURN target.name as name, labels(target) as labels, target.repo_id as repo_id
        ORDER BY target.name
        """
    else:
        # Mentioned by: (d:Document)-[:MENTIONS]->(target {name: $name})
        repo_filter = "" if cross_repo else "AND target.repo_id = $repo_id"
        cypher = f"""
        MATCH (d:Document)-[:MENTIONS]->(target {{name: $name}})
        WHERE true
        {repo_filter}
        {label_filter}
        RETURN d.name as name, labels(d) as labels, d.repo_id as repo_id
        ORDER BY d.name
        """

    params = {"name": name}
    if label is not None:
        params["label"] = label
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return _envelope(results, max_results)


def run_cypher(
    engine: GraphEngine,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Advanced escape hatch for direct Cypher queries.

    This should be gated behind devgraph.config.get_settings().enable_run_cypher
    and is NOT registered as a standard MCP tool by default.

    Args:
        engine: GraphEngine instance
        query: Cypher query string
        parameters: Optional parameters dict

    Returns:
        Raw query results
    """
    return engine.run_cypher(query, parameters or {})
