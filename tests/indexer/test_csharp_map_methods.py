"""ASP.NET Core minimal API `MapMethods` (devgraph/indexer/csharp/extractor.py
`_aspnet_routes`): one route per listed method. Fixture code is fictional."""

from devgraph.indexer.csharp.extractor import extract_csharp_file


def test_map_methods():
    source = """\
var app = WebApplication.Create(args);
app.MapMethods("/ping", new[] { "OPTIONS", "head" }, Ping);
app.MapMethods("/pong", new string[] { HttpMethods.Put }, Ping);
app.MapMethods("/any", methods, Ping);
app.MapMethods("/x", ["PATCH"], () => "x");

static string Ping() => "pong";
"""
    result = extract_csharp_file(source, "Program.cs", "r")
    assert {n.name for n in result.nodes if n.label == "Endpoint"} == {
        "OPTIONS /ping", "HEAD /ping", "PUT /pong", "PATCH /x",
    }
    implements = {
        (r.from_name, r.to_name, r.to_file, r.to_owner) for r in result.relationships if r.rel_type == "IMPLEMENTS"
    }
    assert ("HEAD /ping", "Ping", "Program.cs", "") in implements
    assert ("OPTIONS /ping", "Ping", "Program.cs", "") in implements
    assert not [row for row in implements if row[0] == "PATCH /x"]
