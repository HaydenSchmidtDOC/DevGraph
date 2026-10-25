"""Endpoint nodes and IMPLEMENTS edges for the routes a non-Python
extractor finds (each extractor's `_..._routes` function reads its
frameworks' forms).

They take the shape apis/extractor.py gives a Python route: an `Endpoint`
named `"<METHOD> <path>"` claimed by the file (`framework`, `source`), a
file-less handler stub `Function` claimed the same way, and IMPLEMENTS from
the Endpoint to the stub (`to_file` "") and to the handler. A handler
declared in the route's file is pinned to that file and its owner, as a
FastAPI handler is; one another file declares is pinned to the files its
import names (`calls.call_rows`, as a call to it would be), so it is a
by-name edge the file records and a batch adding the handler relinks. A
handler written inline (a lambda, an arrow function) has no node: its
Endpoint IMPLEMENTS nothing.

Only literal paths are read: a path built from a variable, a constant or a
concatenation is skipped, and so is a route whose prefix is.

A route on a router or group whose mount the file does not show (an
Express router another file mounts, a gin `*gin.RouterGroup` parameter)
has an unknown prefix: its Endpoint is the file's own, named
`"<METHOD> ?<path> (<file>)"` (`unmounted_endpoint`), never merged with
another file's route of the same path (every `GET /` would otherwise meet
in one Endpoint that no request reaches as such). An exported Express
router, or a FastAPI router or Flask blueprint (apis/extractor.py), another
file mounts is then named by those mounts (`MountTable`, below).
"""

from __future__ import annotations

from devgraph.indexer.calls import call_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

#: Spring's mapping annotations and the method each maps (None: RequestMapping's `method`).
SPRING_MAPPINGS: dict[str, str | None] = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH", "RequestMapping": None,
}
SPRING_CONTROLLERS = frozenset({"RestController", "Controller"})


def join_route(*parts: str) -> str:
    """Route path segments joined with single slashes, with a leading one
    and no trailing one: `join_route("/api/", "users/")` is "/api/users",
    and no segment at all "/"."""
    return "/" + "/".join(segment for part in parts for segment in part.split("/") if segment)


def unmounted_endpoint(method: str, path: str, file_path: str) -> str:
    """The Endpoint name of a route under a prefix its file doesn't know."""
    return f"{method} ?{path} ({file_path})"


def add_route(
    result: ExtractionResult,
    repo_id: str,
    file_path: str,
    framework: str,
    method: str,
    path: str,
    handler: str | None = None,
    owner: str | None = None,
    pins: set[str] | None = None,
    mounted: bool = True,
) -> None:
    """Add one route's Endpoint, handler stub and IMPLEMENTS edges to
    `result`. `handler` None is an inline handler: the Endpoint alone.
    `pins` None is a handler of this file, of `owner` (None: any owner);
    else the files the handler can be in (`calls.call_rows` pins), none
    when nothing resolves it, which leaves the stub its only target.
    `mounted` False is a route under a prefix the file doesn't know
    (`unmounted_endpoint`)."""
    endpoint = f"{method} {path}" if mounted else unmounted_endpoint(method, path, file_path)
    result.nodes.append(GraphNode(
        label="Endpoint", repo_id=repo_id, name=endpoint, properties={"framework": framework, "source": file_path},
    ))
    if handler is None:
        return
    result.nodes.append(GraphNode(
        label="Function", repo_id=repo_id, name=handler, properties={"type": "handler", "source": file_path},
    ))
    stub = GraphRelationship(
        from_label="Endpoint", from_name=endpoint, rel_type="IMPLEMENTS", to_label="Function", to_name=handler,
        repo_id=repo_id, to_file="",
    )
    if pins is None:
        result.relationships += [stub, GraphRelationship(
            from_label="Endpoint", from_name=endpoint, rel_type="IMPLEMENTS", to_label="Function", to_name=handler,
            repo_id=repo_id, to_file=file_path, to_owner=owner,
        )]
        return
    result.relationships.append(stub)
    result.relationships += call_rows(
        "Endpoint", endpoint, handler, pins, False, None, file_path, repo_id, rel_type="IMPLEMENTS",
    )


def spring_routes(
    class_annotations: dict[str, dict[str, list[str] | None]],
    method_annotations: dict[str, dict[str, list[str] | None]],
) -> list[tuple[str, str]]:
    """The (method, path) pairs a Spring controller method maps.

    Each annotation is given as its arguments: `value` (the positional one,
    or `value=`/`path=`) as its literal strings, None when one of them is
    not a literal, absent when not given; `method` as the RequestMethod
    names. The class's RequestMapping paths prefix the method's; a
    RequestMapping with no `method` maps every method ("*")."""
    if not SPRING_CONTROLLERS & set(class_annotations):
        return []
    prefixes = class_annotations.get("RequestMapping", {}).get("value", [""])
    routes: list[tuple[str, str]] = []
    for name, http_method in SPRING_MAPPINGS.items():
        if name not in method_annotations:
            continue
        args = method_annotations[name]
        paths = args.get("value", [""])
        if prefixes is None or paths is None:
            continue  # a dynamic path
        methods = [http_method] if http_method else sorted(args.get("method") or ["*"])
        routes += [(each, join_route(prefix, path)) for prefix in prefixes for path in paths for each in methods]
    return routes


# --- Express, FastAPI and Flask routers another file mounts -------------------
#
# A router file's routes on a router it exports, and that it doesn't mount
# itself, have an unknown prefix in its own text: their Endpoints are its
# own (`unmounted_endpoint`) and its Module records them as `route_exports`
# entries (export name, method, path, and the path its own Endpoint has
# when that differs: a Flask blueprint's export for a mount replacing its
# own prefix, apis/extractor.py REPLACED). A file mounting an imported router
# (`app.use('/api', usersRouter)`, `app.include_router(users.router,
# prefix='/api')`) records the mount in its Module's
# `route_mounts`: (base, prefix, export, the files its import can name),
# base "" when the router it is mounted on is at a known prefix (prefix is
# then the full one), else the name under which the file exports that
# router (prefix is then relative to that router's own mounts). The
# dispatcher then names a router file's routes by every mount reaching
# them (`MountTable.prefixes`, `mount_routes`) and records the prefixes it
# used as `route_prefixes` (export, prefix), so a file whose mounts changed
# under it is found and written again (see indexer/dispatch.py
# `_settle_mounts`).

#: Separates a `route_mounts`/`route_exports`/`route_prefixes` entry's fields,
#: and a mount's candidate files.
MOUNT_SEP = "\x1f"
MOUNT_PIN_SEP = "\x1e"
#: The most cross-file mounts a chain from an app to a router's routes holds:
#: an app mounting an api router that mounts the users router is two.
MAX_MOUNT_HOPS = 2
#: The Module properties of the mounts, all lists, None when empty.
MOUNT_PROPERTIES = ("route_mounts", "route_exports", "route_prefixes")


def mount_entry(base: str, prefix: str, export: str, files: list[str]) -> str:
    """A `route_mounts` entry."""
    return MOUNT_SEP.join((base, prefix, export, MOUNT_PIN_SEP.join(files)))


class MountTable:
    """Every Module's `route_mounts` (module -> entries), which a file's
    routers' prefixes are read from."""

    def __init__(self, mounts: dict[str, list[str]]):
        self.by_file: dict[str, list[tuple[str, str, str, str]]] = {}
        self.targets: dict[str, set[str]] = {}  # module -> the files its mounts can name
        for module, entries in mounts.items():
            self.set(module, entries)

    def set(self, module: str, entries: list[str] | None) -> None:
        """Replace `module`'s mounts with `entries`."""
        for file in self.targets.pop(module, ()):
            self.by_file[file] = [row for row in self.by_file[file] if row[0] != module]
        for entry in entries or []:
            base, prefix, export, files = entry.split(MOUNT_SEP)
            for file in files.split(MOUNT_PIN_SEP):
                self.by_file.setdefault(file, []).append((module, base, prefix, export))
                self.targets.setdefault(module, set()).add(file)

    def prefixes(self, file: str, export: str, hops: int = 1) -> set[str]:
        """The full prefixes `file`'s router exported as `export` is mounted
        at, through at most MAX_MOUNT_HOPS mounts."""
        found: set[str] = set()
        for module, base, prefix, mounted in self.by_file.get(file, ()):
            if mounted != export:
                continue
            if not base:
                found.add(prefix)
            elif hops < MAX_MOUNT_HOPS:
                found |= {join_route(above, prefix) for above in self.prefixes(module, base, hops + 1)}
        return found

    def route_prefixes(self, file: str, exports: list[str] | None) -> list[str] | None:
        """The `route_prefixes` of a file with these `route_exports`: each
        export's prefixes, sorted, None when there are none."""
        names = sorted({entry.split(MOUNT_SEP)[0] for entry in exports or []})
        found = sorted(MOUNT_SEP.join((name, prefix)) for name in names for prefix in self.prefixes(file, name))
        return found or None


def mount_routes(
    nodes: list[dict], rels: list[dict], file_path: str, exports: list[str] | None, prefixes: list[str] | None
) -> tuple[list[dict], list[dict]]:
    """A router file's extraction (node and relationship dicts) with each
    route of an exported router renamed from its own Endpoint
    (`unmounted_endpoint`) to one per prefix it is mounted at (`prefixes`,
    `MountTable.route_prefixes`); a route mounted nowhere keeps its own."""
    by_export: dict[str, list[str]] = {}
    for entry in prefixes or []:
        export, prefix = entry.split(MOUNT_SEP)
        by_export.setdefault(export, []).append(prefix)
    renamed: dict[str, set[str]] = {}
    for entry in exports or []:
        export, method, path, *own = entry.split(MOUNT_SEP)
        for prefix in by_export.get(export, ()):
            renamed.setdefault(unmounted_endpoint(method, own[0] if own else path, file_path), set()).add(
                f"{method} {join_route(prefix, path)}"
            )
    if not renamed:
        return nodes, rels

    def names(name: str) -> list[str]:
        return sorted(renamed.get(name, {name}))

    seen: set[str] = set()
    mounted_nodes = []
    for node in nodes:
        if node["label"] != "Endpoint":
            mounted_nodes.append(node)
            continue
        for name in names(node["name"]):
            if name not in seen:
                seen.add(name)
                mounted_nodes.append({**node, "name": name})
    mounted_rels = [
        {**rel, "from_name": name} if rel["from_label"] == "Endpoint" else rel
        for rel in rels
        for name in (names(rel["from_name"]) if rel["from_label"] == "Endpoint" else [rel["from_name"]])
    ]
    return mounted_nodes, mounted_rels
