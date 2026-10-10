"""CALLS rows and stoplists shared by the language extractors.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md. An
extractor resolves each call site to the pins its callee can be at (or to a
bare name) and collapses every site of one caller to one callee name; this
module turns that into rows, so every language writes them the same way.
"""

from __future__ import annotations

from devgraph.graph.schema import PIN_CONFIDENCE, pin_kind
from devgraph.indexer.common import GraphRelationship, module_pin_name, pin_excludes

__all__ = ["STOP_METHODS", "STOP_TYPES", "call_rows", "import_rows"]

_JS_METHODS = frozenset({
    # Array
    "push", "pop", "shift", "unshift", "slice", "splice", "concat", "join", "reverse", "sort", "indexOf",
    "lastIndexOf", "includes", "find", "findIndex", "findLast", "findLastIndex", "filter", "map", "forEach",
    "reduce", "reduceRight", "some", "every", "flat", "flatMap", "fill", "at", "entries", "keys", "values",
    # Map / Set / WeakMap
    "get", "set", "has", "delete", "clear", "add",
    # Promise
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled", "race", "any",
    # String / Number / Date
    "charAt", "charCodeAt", "codePointAt", "endsWith", "startsWith", "localeCompare", "match", "matchAll",
    "normalize", "padEnd", "padStart", "repeat", "replace", "replaceAll", "search", "split", "substring", "substr",
    "toLowerCase", "toUpperCase", "toLocaleLowerCase", "toLocaleUpperCase", "trim", "trimStart", "trimEnd",
    "toString", "valueOf", "toFixed", "toISOString", "toLocaleDateString", "toLocaleTimeString", "toLocaleString",
    "toJSON", "getTime",
    # Object / JSON / console
    "assign", "create", "defineProperty", "freeze", "fromEntries", "hasOwnProperty", "parse", "stringify", "log",
    "info", "warn", "error", "debug", "trace", "table",
    # DOM, Storage, fetch
    "addEventListener", "removeEventListener", "dispatchEvent", "getElementById", "querySelector",
    "querySelectorAll", "appendChild", "removeChild", "setAttribute", "getAttribute", "getItem", "setItem",
    "removeItem", "json", "text", "sendBeacon", "preventDefault", "stopPropagation",
})

_JVM_METHODS = frozenset({
    # Collection / List / Map / Set / Iterator
    "add", "addAll", "remove", "removeAll", "retainAll", "contains", "containsAll", "size", "isEmpty", "clear",
    "get", "set", "put", "putAll", "putIfAbsent", "getOrDefault", "containsKey", "containsValue", "keySet",
    "values", "entrySet", "iterator", "hasNext", "next", "forEach", "toArray", "subList", "indexOf",
    # Optional / Stream
    "stream", "map", "filter", "collect", "reduce", "sorted", "distinct", "limit", "findFirst", "findAny",
    "anyMatch", "allMatch", "noneMatch", "toList", "count", "orElse", "orElseGet", "orElseThrow", "isPresent",
    "ifPresent", "flatMap", "of",
    # Object / String / StringBuilder
    "equals", "hashCode", "toString", "length", "charAt", "substring", "trim", "split", "replace", "startsWith",
    "endsWith", "toUpperCase", "toLowerCase", "format", "append", "insert", "println", "print",
    # Kotlin scope and collection functions
    "let", "also", "apply", "run", "takeIf", "use", "first", "last", "firstOrNull", "lastOrNull", "joinToString",
    "mapNotNull", "filterNot", "any", "all", "none", "sortedBy", "groupBy", "associate", "associateBy",
    "lowercase", "uppercase", "trimEnd", "trimStart", "padStart", "padEnd", "take", "drop", "getOrPut",
})

#: Methods of a language's container, text and runtime types. A call of one
#: of these on a receiver nothing types links nothing, rather than every
#: same-named function in the repository.
STOP_METHODS: dict[str, frozenset[str]] = {
    "python": frozenset({
        # dict
        "get", "items", "keys", "values", "update", "pop", "popitem", "setdefault", "copy", "clear", "fromkeys",
        # list
        "append", "extend", "insert", "remove", "index", "count", "sort", "reverse",
        # str / bytes
        "join", "split", "rsplit", "splitlines", "strip", "lstrip", "rstrip", "format", "format_map", "replace",
        "startswith", "endswith", "lower", "upper", "casefold", "title", "capitalize", "encode", "decode", "find",
        "rfind", "partition", "rpartition", "zfill", "ljust", "rjust", "center", "removeprefix", "removesuffix",
        "isdigit", "isalpha", "isalnum", "isspace", "isidentifier", "islower", "isupper", "expandtabs", "translate",
        "hex",
        # set
        "add", "discard", "union", "intersection", "difference", "symmetric_difference", "issubset",
        "issuperset", "isdisjoint",
        # io
        "read", "write", "readline", "readlines", "writelines", "seek", "tell", "flush", "close", "truncate",
    }),
    "js": _JS_METHODS,
    "java": _JVM_METHODS,
    "kotlin": _JVM_METHODS,
    "csharp": frozenset({
        # List / Dictionary / HashSet
        "Add", "AddRange", "Remove", "RemoveAt", "Contains", "ContainsKey", "TryGetValue", "Clear", "Insert",
        "IndexOf",
        # LINQ
        "Select", "SelectMany", "Where", "First", "FirstOrDefault", "Last", "LastOrDefault", "Single",
        "SingleOrDefault", "Any", "All", "Count", "ToList", "ToArray", "ToDictionary", "OrderBy",
        "OrderByDescending", "ThenBy", "GroupBy", "Sum", "Max", "Min", "Average", "Aggregate", "Distinct", "Skip",
        "Take",
        # Task
        "Wait", "ContinueWith", "ConfigureAwait", "GetAwaiter", "GetResult",
        # object / string
        "ToString", "Equals", "GetHashCode", "GetType", "Split", "Trim", "Substring", "Replace", "StartsWith",
        "EndsWith", "ToUpper", "ToLower", "ToUpperInvariant", "ToLowerInvariant", "PadLeft", "PadRight",
        "WriteLine", "Write", "Dispose",
    }),
    "go": frozenset({
        # error / fmt.Stringer / sync / context / io / strings.Builder / bytes.Buffer
        "Error", "String", "Lock", "Unlock", "RLock", "RUnlock", "Close", "Done", "Err", "Value", "Deadline",
        "Wait", "Add", "Write", "WriteString", "WriteByte", "WriteRune", "Read", "Len", "Reset", "Bytes", "Grow",
        "Unwrap", "Is", "As",
    }),
    "rust": frozenset({
        # Vec / slices / String
        "push", "pop", "len", "is_empty", "iter", "iter_mut", "into_iter", "insert", "remove", "clear", "extend",
        "sort", "sort_by", "sort_by_key", "dedup", "truncate", "push_str", "as_str", "chars", "bytes", "split",
        "trim", "join", "contains", "starts_with", "ends_with", "to_lowercase", "to_uppercase", "lines",
        # Iterator
        "map", "filter", "filter_map", "collect", "fold", "any", "all", "find", "enumerate", "zip", "rev", "take",
        "skip", "count", "sum", "max", "min", "next", "cloned", "copied", "flat_map", "for_each", "chain",
        # Option / Result
        "unwrap", "expect", "unwrap_or", "unwrap_or_else", "unwrap_or_default", "ok", "err", "ok_or",
        "ok_or_else", "is_some", "is_none", "is_ok", "is_err", "as_ref", "as_mut", "and_then", "or_else",
        "map_err",
        # Clone / ToString / HashMap / Arc / Mutex / RefCell
        "clone", "to_string", "to_owned", "into", "get", "get_mut", "contains_key", "entry", "or_insert",
        "or_insert_with", "or_default", "keys", "values", "lock", "read", "write", "borrow", "borrow_mut", "fmt",
    }),
    "cpp": frozenset({
        # STL containers, strings, smart pointers, mutexes
        "push_back", "emplace_back", "pop_back", "push_front", "pop_front", "size", "empty", "begin", "end",
        "cbegin", "cend", "rbegin", "rend", "find", "insert", "emplace", "erase", "clear", "at", "front", "back",
        "count", "reserve", "resize", "data", "c_str", "substr", "append", "length", "compare", "reset", "get",
        "release", "lock", "unlock", "try_lock", "swap",
    }),
}

#: Library types whose static calls and constructors link nothing
#: (`Vec::new()`, `String.valueOf()`, `Console.WriteLine()`).
STOP_TYPES: dict[str, frozenset[str]] = {
    "js": frozenset({
        "Object", "Array", "JSON", "Promise", "Math", "Number", "String", "Date", "Reflect", "Symbol", "console",
        "Intl", "URL", "Map", "Set",
    }),
    "java": frozenset({
        "String", "Math", "Objects", "List", "Map", "Set", "Arrays", "Collections", "Optional", "Stream",
        "Collectors", "Integer", "Long", "Double", "Boolean", "Character", "System", "Thread", "StringBuilder",
    }),
    "kotlin": frozenset({
        "String", "Math", "Objects", "List", "Map", "Set", "Arrays", "Collections", "Optional", "Integer", "Long",
        "Double", "Boolean", "System", "Thread", "StringBuilder", "Regex",
    }),
    "csharp": frozenset({
        "Console", "Task", "Enumerable", "Math", "String", "string", "Convert", "DateTime", "Guid", "List",
        "Dictionary", "Path", "File", "Directory", "Environment", "Activator",
    }),
    "rust": frozenset({
        "Vec", "String", "HashMap", "HashSet", "BTreeMap", "BTreeSet", "VecDeque", "Box", "Arc", "Rc", "Mutex",
        "RwLock", "RefCell", "Cell", "Option", "Result", "Some", "Ok", "Err", "PathBuf", "Path", "Duration",
        "Instant",
    }),
}


def call_rows(
    caller_label: str,
    caller_name: str,
    name: str,
    pins: set[str],
    bare: bool,
    caller_class: str | None,
    file_path: str,
    repo_id: str,
    no_self: bool = False,
) -> list[GraphRelationship]:
    """One caller's CALLS rows to one callee name, every call site to it
    collapsed together, so no two rows can meet in one edge with different
    properties: a row per pin, highest precedence first, whose confidence is
    its kind's (graph/schema.py `PIN_CONFIDENCE`) and which leaves out what a
    stronger pin matches (`pin_excludes`); or else, when no call site
    resolved, one bare-name row ("name"). `caller_class` is the least of the
    call sites' enclosing classes. `no_self` (every bare call site was a
    member call on an untyped receiver) keeps the bare row off the caller
    itself."""

    def row(to_file: str | None, confidence: str, exclude: list[str] | None = None) -> GraphRelationship:
        properties = {"confidence": confidence}
        if caller_class:
            properties["caller_class"] = caller_class
        return GraphRelationship(
            from_label=caller_label, from_name=caller_name, rel_type="CALLS", to_label="Function", to_name=name,
            repo_id=repo_id, properties=properties, to_file=to_file, exclude=exclude,
            no_self=no_self and to_file is None,
        )

    if not pins:
        return [row(None, "name")] if bare else []
    return [
        row(pin, PIN_CONFIDENCE[pin_kind(pin)], excluded or None)
        for pin, excluded in pin_excludes(pins, file_path).items()
    ]


def import_rows(file_path: str, repo_id: str, files: set[str], pins: set[str]) -> list[GraphRelationship]:
    """A file's IMPORTS rows: one to each Module path in `files` (by name, no
    confidence, as every IMPORTS edge before path pins), and one per
    directory or suffix pin in `pins`, sought by `module_pin_name`, with its
    kind's confidence. A file's imports all end at Modules, so they collapse
    together like one call's pins: each pin leaves out what a stronger pin,
    or one of `files`, matches."""
    rows = [
        GraphRelationship(
            from_label="Module", from_name=file_path, rel_type="IMPORTS", to_label="Module", to_name=target,
            repo_id=repo_id,
        )
        for target in sorted(files - {file_path})
    ]
    for pin, excluded in pin_excludes(files | pins, file_path).items():
        if pin in pins and pin not in files:
            rows.append(GraphRelationship(
                from_label="Module", from_name=file_path, rel_type="IMPORTS", to_label="Module",
                to_name=module_pin_name(pin), repo_id=repo_id, properties={"confidence": PIN_CONFIDENCE[pin_kind(pin)]},
                to_file=pin, exclude=sorted(set(excluded) | {file_path}),
            ))
    return rows

