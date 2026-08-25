"""High-level MCP tool implementations against the DevGraph graph schema.

Each tool accepts a GraphEngine and returns structured data without exposing
raw Cypher to the AI. Tools filter by repo_id by default and support an
explicit cross_repo flag to opt-in to cross-repository results.

All Cypher is parameterized — user input never concatenates directly into
query strings.
"""

from typing import Any

from devgraph.graph.engine import GraphEngine


def search_component(
    engine: GraphEngine,
    repo_id: str,
    query: str,
    cross_repo: bool = False,
) -> list[dict[str, Any]]:
    """Search for components (modules, services, classes, functions) by name or description.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to search within (unless cross_repo=True)
        query: Search term (name substring or description keyword)
        cross_repo: If True, search across all repos; if False, limit to repo_id

    Returns:
        List of components with their basic properties
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
    return results


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
           COLLECT(DISTINCT {{name: dep.name, type: labels(dep)[0], repo_id: dep.repo_id}}) as dependencies,
           COLLECT(DISTINCT {{name: called.name, repo_id: called.repo_id}}) as calls
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
) -> list[dict[str, Any]]:
    """Find all functions, services, or endpoints that call a given target.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        target_name: Name of the function/service/endpoint being called
        cross_repo: If True, find callers across repos

    Returns:
        List of callers with their types and locations
    """
    repo_filter = "" if cross_repo else "AND target.repo_id = $repo_id"
    cypher = f"""
    MATCH (caller)-[:CALLS]->(target)
    WHERE target.name = $target_name
    {repo_filter}
    RETURN DISTINCT caller.name as name, labels(caller) as type, caller.repo_id as repo_id
    ORDER BY caller.name
    """
    params = {"target_name": target_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = engine.run_cypher(cypher, params)
    return results


def find_related_files(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Find all files related to a component (via CONTAINS, IMPORTS, CALLS relationships).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component
        cross_repo: If True, include cross-repo relationships

    Returns:
        Dict with containing files, imported files, and related files
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
        return results[0]
    return {"containing_modules": [], "imported_modules": [], "related_components": []}


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
) -> dict[str, Any]:
    """Analyze the impact of changing a component on the rest of the system.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component to analyze
        cross_repo: If True, include cross-repo impacts

    Returns:
        Dict with direct_dependents, transitive_dependents, and risk_level
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
        return results[0]
    return {
        "direct_dependents": [],
        "transitive_dependents": [],
        "risk_level": "low",
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
) -> list[dict[str, Any]]:
    """List all services in a repository (or across repos if cross_repo=True).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID (ignored if cross_repo=True)
        cross_repo: If True, list services from all repos

    Returns:
        List of services with their properties
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
    return results


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
) -> list[dict[str, Any]]:
    """Find pull requests related to a component via commits that resolved issues touching it.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to find related PRs for
        cross_repo: If True, search across repos

    Returns:
        List of PullRequests linked (via RESOLVES on an Issue referenced by
        a commit that touched this component) to the component.
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
    return results


def issue_history_for(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> list[dict[str, Any]]:
    """Find issues referenced by commits that touched a component.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to find issue history for
        cross_repo: If True, search across repos

    Returns:
        List of Issues referenced by commits that modified this component.
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
    return results


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
