# DevGraph — Implementation Plan #3: Result Shaping, MRO-Scoped CALLS, Diff Impact

Build-ready plan for Design Brief #3's Items 1-3 (Item 4, embedding search, is
explicitly deferred to its own future design/plan per that brief). Same
granularity precedent as Implementation Plan #2: concrete file edits,
sequencing, and a live-verification pass against RAG4.

## Item 1 — Token-budgeted result shaping

**Files:** `devgraph/mcp/tools.py`, `devgraph/mcp/server.py`.

Add a small helper in `tools.py`:

```python
def _envelope(items: list[Any], max_results: int) -> dict[str, Any]:
    return {
        "count": len(items),
        "results": items[:max_results],
        "truncated": len(items) > max_results,
    }
```

Apply to these tools, each gaining `max_results: int = 15`:

- `search_component` — keep `LIMIT 50` in Cypher (cap query cost), wrap the
  50-row result in `_envelope(rows, max_results)` so `count`/`truncated`
  reflect the (already-capped-at-50) result, not a true global count. Note
  this in the docstring: `count` maxes out at 50 even if more exist.
- `find_callers` — wrap returned rows.
- `list_services` — wrap returned rows.
- `find_related_prs`, `issue_history_for` — wrap returned rows.
- `find_related_files` — apply per-list: `containing_modules`,
  `imported_modules`, `related_components` each become
  `{"count", "results", "truncated"}` sub-objects (collect uncapped in
  Cypher via `COLLECT(DISTINCT ...)` as today, slice/envelope in Python
  after — these lists are small enough that doing the cap in Python instead
  of Cypher is fine and keeps one code path).
- `impact_analysis` — same per-list treatment for `direct_dependents` and
  `transitive_dependents`. `risk_level` stays computed from the true
  (uncapped) `COUNT(DISTINCT dependent)` in Cypher, unaffected by envelope
  truncation.

Do **not** touch `summarise_repository`, `explain_architecture`,
`get_service_dependencies`, `explain_decision`, `trace_design_rationale`,
`get_source`, `trace_request_flow`, `compare_branches`, `blame_component`
(single bounded object or already inherently small/ordered — Brief's
exclusion list, plus `blame_component`/`trace_request_flow` added here since
they're similarly bounded-by-nature, not a cardinality risk).

Update `server.py`'s `@server.tool()` wrappers for the changed functions to
accept and pass through `max_results: int = 15`, and update each tool's
docstring one line to mention the `count`/`results`/`truncated` shape.

**Verification:** update `tests/mcp/test_tools.py` /
`test_tools_phase3.py` assertions that currently expect a bare list back
from `find_callers`/`list_services`/etc. to expect the envelope dict
instead. Add one new test per changed tool asserting `truncated: true` when
result count exceeds `max_results`, and `truncated: false` otherwise. Run
full suite; then run `find_callers` against RAG4 for a common name (e.g.
`get` or `run`) and confirm `truncated`/`count` reflect the real over-500
row count in the pre-Item-2 state (informative baseline for Item 2's
before/after).

---

## Item 2 — MRO-scoped `CALLS` resolution

**File:** `devgraph/indexer/python/extractor.py` only.

### Design

Restructure `extract_python_file` into two passes over the same parse tree:

1. **Pass 1 (class-hierarchy pass):** walk the tree once, collecting every
   `class_definition` in this file into `class_bases: dict[str, list[str]]`
   (class name -> its direct base-class names, from the existing
   `_extract_base_class_names`). This is scoped to the current file only —
   no graph read, no cross-file resolution. Also collect, for every class in
   this file, the set of method names it directly defines:
   `class_methods: dict[str, set[str]]` (class name -> method names found in
   its body via a shallow scan for `function_definition`/
   `decorated_definition` children).

2. **Pass 2 (existing visit_block/_visit_class/_visit_function pass):**
   unchanged in structure, except `_emit_call` (called from inside
   `_visit_function` for a method body) gains the enclosing class name as
   context and does MRO-scoped resolution before falling back to the
   existing repo-wide name-based target.

### MRO resolution logic

Add a helper:

```python
def _resolve_mro_chain(class_name: str, class_bases: dict[str, list[str]]) -> list[str]:
    """Depth-first walk of class_name's base chain within this file only.
    Returns [class_name, *ancestors-in-this-file], stopping at any base not
    defined in this file (an imported/external base — no data to walk
    further; that's fine, this is a same-file-only, best-effort MRO, not a
    full cross-file resolution)."""
    chain = [class_name]
    seen = {class_name}
    frontier = list(class_bases.get(class_name, []))
    while frontier:
        base = frontier.pop(0)
        if base in seen or base not in class_bases:
            if base not in seen:
                chain.append(base)  # external/unknown base: record but don't expand
                seen.add(base)
            continue
        chain.append(base)
        seen.add(base)
        frontier.extend(class_bases.get(base, []))
    return chain
```

Then in `_visit_function`, when body-call extraction runs and
`parent_label == "Class"` (i.e. this function is a method, `parent_name` is
its enclosing class):

```python
mro_chain = _resolve_mro_chain(parent_name, class_bases) if parent_name else []
mro_method_names = {
    name for cls in mro_chain for name in class_methods.get(cls, set())
}
for target in _extract_call_targets(body_node, source_bytes):
    if target in mro_method_names:
        # Emit CALLS only to the specific class(es) in the MRO chain that
        # actually define this method name (narrower than repo-wide, and
        # narrower than "every class in the MRO" when only one defines it).
        for cls in mro_chain:
            if target in class_methods.get(cls, set()):
                _emit_call_scoped(func_name, "Function", target, cls)
    else:
        _emit_call(func_name, "Function", target)  # unchanged repo-wide fallback
```

`_emit_call_scoped` cannot target "the Function node owned by class X" by
name alone, because `Function` nodes are keyed on `(repo_id, name)` — a
method name, not name+owning-class (this is an existing schema property,
unchanged by this plan; renaming Function's key is out of scope and would
break every other tool). So `_emit_call_scoped`'s only real lever is:
**when an MRO-scoped match exists, emit CALLS only to that bare method name
once** (still `to_label="Function", to_name=target`, identical shape to
today) and **skip the repo-wide fallback** — the actual behavior change is
suppressing the over-link when the caller's own class hierarchy already
tells us the call resolves to a method defined somewhere in that hierarchy.
This means `_resolve_mro_chain`/`class_methods` don't need to change the
edge's target shape at all, only the *decision of whether to treat this
call as resolved* — meaning there is nothing to disambiguate beyond current
behavior when two classes in the same MRO chain define the same method name
(shadowing) — both would still 1206-style link to the same `Function` node
by name, exactly as today, since `Function` nodes are name-keyed
repo-wide regardless of owning class. **State this explicitly as a known
limitation carried over unchanged**, not newly introduced.

Practically, this means the effective change is narrower and cheaper than
the Brief's framing suggests once schema constraints are taken into
account: for a `self.foo()` call inside a method, if `foo` is a name defined
somewhere in the caller's own file-local MRO chain, CALLS still emits to
every repo-wide `Function` named `foo` (schema can't do otherwise), but if
`foo` is *not* found anywhere in the caller's MRO chain (a duck-typed/
composition call, or a name that happens to collide with an unrelated
class's method elsewhere in the repo), **do not emit a CALLS edge at all**
rather than the current always-link-repo-wide-by-name behavior.

Re-derive this into the actual algorithm before coding:

- If `target` name is found in `mro_method_names` (this file's MRO chain for
  the caller's class): emit `CALLS` to `target` (repo-wide name match, same
  as before — schema can't narrow further) — this is the common, correct
  case, now confirmed rather than assumed.
- If `target` name is **not** found in `mro_method_names` **and** the
  calling context is `self.X()`/`obj.X()` (an attribute-call, not a bare
  name): this is exactly the case Design Brief #3 identifies as noise
  (`self.foo()` linking to an unrelated class's same-named method) — but
  since Tree-sitter has no type info, DevGraph cannot know if `obj` is
  something entirely different from `self`'s hierarchy on purpose (e.g.
  genuine composition: `self.logger.close()`). **Do not suppress this
  edge either** — suppressing it would under-link a real, common pattern
  (calling a method on a composed attribute), trading one class of noise
  for a worse class of silent gaps. This confirms the Brief's item 2 as
  described is not achievable as a pure filter without type resolution;
  the achievable win is narrower.

**Revised, honest scope for Item 2:** distinguish `self.foo()` /
`cls.foo()` calls (where the receiver name is literally `self` or `cls` —
Tree-sitter gives us this for free from the attribute node's `object`
field) from `obj.foo()` calls on any other receiver name. For a
`self.foo()`/`cls.foo()` call specifically: if `foo` resolves within the
caller's own file-local MRO chain, tag the emitted relationship
unchanged (still repo-wide by name, per the schema constraint above) —
**no suppression** — but additionally emit a same-shaped edge is
unnecessary; the actual, shippable improvement is confined to `find_callers`
output filtering, not extraction. **Given this analysis, recommend to the
implementer: Item 2 as literally specified in the Brief is not achievable
at the extraction layer without either (a) a schema change to key Function
nodes by (repo_id, owning_class, name), which is a bigger, riskier change
explicitly not authorized by this brief, or (b) moving the MRO-narrowing
into `find_callers`/`impact_analysis` at query time instead of extraction
time.**

### Actual implementation: move MRO-narrowing to query time, not extraction

Given the schema constraint above, implement Item 2 as follows instead
(smaller, safer, and still delivers the Brief's actual goal — less noise in
`find_callers`):

1. **Extraction side (`extractor.py`):** no CALLS emission changes at all.
   Instead, when emitting a `CALLS` edge from a method body
   (`parent_label == "Class"`), additionally record the **caller's
   enclosing class name** as a property on the relationship-equivalent:
   since `upsert_relationship` has no edge-property support today, add one
   new optional parameter to `GraphEngine.upsert_relationship` —
   `properties: dict | None = None` — defaulting to `None` (no behavior
   change for every other call site), and set it via `SET r += $properties`
   after the `MERGE`. Extractor passes `{"caller_class": parent_name}` only
   for method-body calls; bare-function calls pass no properties (`None`).
2. **Query side (`tools.py`'s `find_callers`):** add an optional
   `scope_to_class: str | None = None` parameter. When given, filter
   `caller-[:CALLS {caller_class: $scope_to_class}]->target` in addition to
   the existing name match, letting an agent that already knows the
   relevant class ask a narrowed question. When omitted, behavior is
   unchanged (repo-wide, as today) — **fully backward compatible, opt-in
   narrowing**, not a default-behavior change that could silently drop
   edges an existing caller relies on.

This delivers the Brief's actual intent (an agent can avoid noise from
unrelated same-named methods) using data already available at extraction
time (`parent_name` was already in scope), without a schema-breaking key
change and without the false precision of a same-file-only MRO guess that
can't be expressed in the current schema anyway.

**Files touched:** `devgraph/graph/engine.py` (`upsert_relationship` gains
optional `properties` param), `devgraph/indexer/python/extractor.py`
(`GraphRelationship` dataclass gains optional `properties: dict | None =
None`, `_emit_call` sets `{"caller_class": parent_name}` when
`parent_label == "Class"`), `devgraph/mcp/tools.py` /`server.py`
(`find_callers` gains `scope_to_class` param).

**Verification:** extend `tests/indexer/test_python_extractor.py` with a
case asserting a `CALLS` edge's properties include `caller_class` for a
method-body call and omit it for a module-level bare call. Extend
`tests/mcp/test_tools.py`'s `find_callers` tests with a `scope_to_class`
case. Then re-run full extraction against RAG4 and confirm the CALLS edge
count is unchanged (1207, or whatever the current count is after Item 1's
changes — this must not regress, since this Item's design deliberately adds
a property rather than removing/narrowing edges) and spot-check that a
`find_callers` call with `scope_to_class` set actually narrows results on a
real overloaded method name in RAG4.

---

## Item 3 — `impact_analysis_for_diff`

**Files:** new function in `devgraph/mcp/tools.py`, new tool registration in
`devgraph/mcp/server.py`, registry lookup for repo root (same pattern as
`get_source`).

### Implementation

```python
def impact_analysis_for_diff(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    base_ref: str,
    head_ref: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    ...
```

Steps inside:

1. Resolve `repo_id` via `registry.get(repo_id)` -> repo root path (same as
   `get_source`). 404-equivalent (empty result dict) if not registered.
2. Local git diff only, via GitPython (`from git import Repo`, already a
   dependency): `repo.git.diff("--name-only", f"{base_ref}..{head_ref}")` —
   **never** any `fetch`/`pull`/remote call. Validate both refs resolve
   locally first via `repo.commit(base_ref)` / `repo.commit(head_ref)`
   (raises `git.BadName` / similar if not local) and catch that into a
   clean error dict rather than letting GitPython's exception propagate —
   this is the "ref validation" risk the Brief flags explicitly. Refs are
   passed to GitPython's own API (`repo.commit(...)`, `repo.git.diff(...)`
   argument list form), never interpolated into a shell string — same
   injection posture as `git_history/extractor.py` already has (it never
   shells out to a string either).
3. Map each changed file path (already repo-relative from `--name-only`) to
   contained Function/Class nodes in one Cypher pass:
   ```cypher
   MATCH (m:Module {repo_id: $repo_id})
   WHERE m.name IN $changed_files
   OPTIONAL MATCH (m)-[:CONTAINS*1..2]->(comp)
   WHERE comp:Function OR comp:Class
   RETURN m.name as file, COLLECT(DISTINCT comp.name) as components
   ```
   (`*1..2` covers Module->Class->Function and Module->Function directly;
   matches the existing `CONTAINS` depth used elsewhere, e.g.
   `find_related_files`'s `CONTAINS*`.)
4. Union all `components` across changed files into one deduplicated list
   (`changed_components`).
5. One parameterized Cypher query for combined dependents (avoid N calls
   into `impact_analysis` internally — Brief's explicit ask):
   ```cypher
   MATCH (n) WHERE n.name IN $changed_components
   {repo_filter}
   OPTIONAL MATCH (dependent)-[:CALLS|USES|DEPENDS_ON]->(n)
   {dependent_filter}
   OPTIONAL MATCH (transitive)-[:CALLS|USES|DEPENDS_ON*2..]->(n)
   {transitive_filter}
   RETURN
     COLLECT(DISTINCT {name: dependent.name, type: labels(dependent)[0]}) as direct_dependents,
     COLLECT(DISTINCT {name: transitive.name, type: labels(transitive)[0]}) as transitive_dependents,
     COUNT(DISTINCT dependent) as direct_count
   ```
   (reuses `impact_analysis`'s exact pattern, just parameterized over a list
   instead of one name — confirms the Brief's "restructure as one
   parameterized query" suggestion).
6. `risk_level` rolled up as `high` if `direct_count > 10`, `medium` if `>
   3`, else `low` — same thresholds as `impact_analysis` for consistency,
   applied to the unioned count per the Brief.
7. Wrap `direct_dependents`/`transitive_dependents` in Item 1's `_envelope`
   using `max_results` — Item 3 explicitly depends on Item 1 per the Brief,
   so this only makes sense built after Item 1 lands.
8. Return shape:
   ```python
   {
       "changed_files": [...],
       "changed_components": [...],
       "direct_dependents": {"count", "results", "truncated"},
       "transitive_dependents": {"count", "results", "truncated"},
       "risk_level": "high" | "medium" | "low",
   }
   ```

Register as the 18th MCP tool in `server.py`, following the `get_source`
pattern for passing `registry` through.

**Verification:** new `tests/mcp/test_tools_impact_diff.py` covering: (a) a
diff touching one file with a known dependent, (b) an invalid `base_ref`/
`head_ref` returning a clean error rather than raising, (c) an empty diff
(`base_ref == head_ref`) returning empty lists and `risk_level: "low"`. Then
live-verify against RAG4: pick two real commits from its git log
(`blame_component`/git log can supply candidates), run
`impact_analysis_for_diff` between them, and manually cross-check
`changed_components` against `git diff --name-only` for those two SHAs plus
one `find_related_files` call on a known-changed component, confirming the
tool's aggregate matches manual composition.

---

## Sequencing

1. Item 1 (independent, cheapest, unblocks Item 3).
2. Item 2 (independent of 1 and 3; can run in parallel with Item 1).
3. Item 3 (after Item 1 lands, since it reuses `_envelope`).

## Out of scope (explicit)

- Item 4 (embedding-backed semantic search) — separate design pass per the
  Brief, not touched by this plan.
- Any Function-node schema/key change (e.g. keying by owning class) — the
  Item 2 analysis above found the Brief's literal MRO-suppression proposal
  isn't achievable without one, and explicitly declines to make that
  change here; flag for a future brief if full type-scoped CALLS is ever
  wanted.
