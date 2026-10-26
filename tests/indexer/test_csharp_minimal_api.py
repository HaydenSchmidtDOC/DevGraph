"""C# minimal APIs (devgraph/indexer/csharp/extractor.py): a top-level
`Program.cs`'s local functions are Functions its top-level code calls, and
a minimal-API handler resolves as a call to it would, into another class
and file too. Fixture code is fictional."""

from devgraph.indexer.csharp.extractor import extract_csharp_file

PROGRAM = """\
using Todo.Api;

var app = WebApplication.CreateBuilder(args).Build();
app.MapGet("/todos", GetTodos);
app.MapGet("/todos/{id}", TodoHandlers.GetById);
var count = Count();
app.Run();

/// <summary>Every todo.</summary>
static IResult GetTodos(int page) { return Helper(); }
static IResult Helper() => Results.Ok();
static int Count() => 1;
"""
PATH = "src/Api/Program.cs"


def _functions(result) -> dict[str, dict]:
    return {n.name: n.properties for n in result.nodes if n.label == "Function" and n.properties.get("file")}


def _rows(result, rel_type: str) -> set[tuple]:
    return {
        (r.from_name, r.to_name, r.to_file, r.to_owner) for r in result.relationships if r.rel_type == rel_type
    }


def test_top_level_local_functions_are_functions():
    result = extract_csharp_file(PROGRAM, PATH, "r")
    functions = _functions(result)
    assert set(functions) == {"GetTodos", "Helper", "Count"}
    assert functions["GetTodos"]["owner"] == ""
    assert functions["GetTodos"]["description"] == "Every todo."
    contains = _rows(result, "CONTAINS")
    assert (PATH, "GetTodos", PATH, "") in contains


def test_calls_to_and_from_top_level_local_functions():
    result = extract_csharp_file(PROGRAM, PATH, "r")
    calls = _rows(result, "CALLS")
    assert (PATH, "Count", PATH, "") in calls
    assert ("GetTodos", "Helper", PATH, "") in calls
    assert not [c for c in calls if c[1] in ("Count", "Helper") and c[2] is None]


def test_a_local_function_in_a_method_stays_unextracted():
    source = """\
class Jobs
{
    void Run()
    {
        Step();
        void Step() { }
    }
}
"""
    assert set(_functions(extract_csharp_file(source, "Jobs.cs", "r"))) == {"Run"}


def test_minimal_api_handlers_resolve_as_calls():
    result = extract_csharp_file(PROGRAM, PATH, "r")
    implements = _rows(result, "IMPLEMENTS")
    # A top-level local function of the file.
    assert ("GET /todos", "GetTodos", PATH, "") in implements
    # Another file's class, by the keys a call `TodoHandlers.GetById()` gets.
    assert ("GET /todos/{id}", "GetById", "/.?.cs^@.:TodoHandlers;Todo.Api:TodoHandlers", None) in implements

