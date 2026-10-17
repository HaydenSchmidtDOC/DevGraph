"""Deeply nested sources, one per language: a long `+` chain and a long
`else if` chain, each one tree level per term or branch, beside a plain
function. Shared by the extractor and dispatch tests (audit 6, item 1)."""

# file name -> source builder(terms, branches); every file defines the
# functions in DEEP_FUNCTIONS[ext] (the plain one, the chain, the branches).


# One term or branch per line: a single long line would be left out as
# minified or generated before any extractor saw it.
def _terms(call: str, n: int) -> str:
    return " +\n    ".join(f"{call}({i})" for i in range(n))


def python(terms: int, branches: int) -> str:
    arms = "".join(f"    elif x == {i}:\n        return {i}\n" for i in range(1, branches))
    return (
        "def keep_me():\n    return 1\n\n\n"
        f"def big(g):\n    return (\n    {_terms('g', terms)})\n\n\n"
        f"def branches(x):\n    if x == 0:\n        return 0\n{arms}    return -1\n"
    )


def _c_like(head: str, plain: str, big: str, branches: str, terms: int, n: int, eq: str = "==", tail: str = "") -> str:
    arms = " else ".join(f"if (x {eq} {i}) {{\n    return {i};\n}}" for i in range(n))
    return (
        f"{head}{plain} {{ return 1; }}\n"
        f"{big} {{ return {_terms('g', terms)}; }}\n"
        f"{branches} {{ {arms} return -1; }}\n{tail}"
    )


def javascript(terms: int, branches: int) -> str:
    return _c_like("", "function keepMe()", "function big(g)", "function branches(x)", terms, branches, "===")


def typescript(terms: int, branches: int) -> str:
    return _c_like(
        "", "function keepMe(): number", "function big(g: (i: number) => number): number",
        "function branches(x: number): number", terms, branches, "===",
    )


def java(terms: int, branches: int) -> str:
    return _c_like(
        "class Deep {\n", "int keepMe()", "int big()", "int branches(int x)", terms, branches,
        tail="int g(int i) { return i; }\n}\n",
    )


def csharp(terms: int, branches: int) -> str:
    return _c_like(
        "class Deep {\n", "int KeepMe()", "int Big()", "int Branches(int x)", terms, branches,
        tail="int g(int i) { return i; }\n}\n",
    )


def cpp(terms: int, branches: int) -> str:
    return _c_like("int g(int i) { return i; }\n", "int keep_me()", "int big()", "int branches(int x)", terms, branches)


def go(terms: int, branches: int) -> str:
    arms = " else ".join(f"if x == {i} {{\n\treturn {i}\n}}" for i in range(branches))
    return (
        "package deep\n\nfunc g(i int) int { return i }\n"
        "func KeepMe() int { return 1 }\n"
        f"func Big() int {{ return {_terms('g', terms)} }}\n"
        f"func Branches(x int) int {{ {arms}\n\treturn -1 }}\n"
    )


def rust(terms: int, branches: int) -> str:
    arms = " else ".join(f"if x == {i} {{\n    {i}\n}}" for i in range(branches))
    return (
        "fn g(i: i32) -> i32 { i }\n"
        "fn keep_me() -> i32 { 1 }\n"
        f"fn big() -> i32 {{ {_terms('g', terms)} }}\n"
        f"fn branches(x: i32) -> i32 {{ {arms} else {{ -1 }} }}\n"
    )


def kotlin(terms: int, branches: int) -> str:
    arms = " else ".join(f"if (x == {i}) {{\n    return {i}\n}}" for i in range(branches))
    return (
        "fun g(i: Int): Int = i\n"
        "fun keepMe(): Int = 1\n"
        f"fun big(): Int {{ return {_terms('g', terms)} }}\n"
        f"fun branches(x: Int): Int {{ {arms}\n    return -1 }}\n"
    )


DEEP_SOURCES = {
    "deep.py": python, "deep.js": javascript, "deep.ts": typescript, "Deep.java": java, "Deep.cs": csharp,
    "deep.cpp": cpp, "deep.go": go, "deep.rs": rust, "deep.kt": kotlin,
}
DEEP_FUNCTIONS = {
    "deep.py": {"keep_me", "big", "branches"},
    "deep.js": {"keepMe", "big", "branches"},
    "deep.ts": {"keepMe", "big", "branches"},
    "Deep.java": {"keepMe", "big", "branches"},
    "Deep.cs": {"KeepMe", "Big", "Branches"},
    "deep.cpp": {"keep_me", "big", "branches"},
    "deep.go": {"KeepMe", "Big", "Branches"},
    "deep.rs": {"keep_me", "big", "branches"},
    "deep.kt": {"keepMe", "big", "branches"},
}
