"""The mentions matcher's candidate prefilter keeps the original per-name
matching semantics exactly.

`_reference_*` below is the matcher as it was before the prefilter: every
known name run through the per-name regexes. The extractor must produce the
same relationships, in the same order, and `mentions_any` the same answer,
on real Markdown and on random fuzz inputs.
"""

import random
import re
from pathlib import Path

from devgraph.indexer.mentions import extractor as mentions
from devgraph.indexer.mentions.extractor import MentionsExtractor, mentions_any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _reference_in_code_regions(content, name, code_regions):
    pattern = r"\b" + re.escape(name) + r"\b"
    return any(re.search(pattern, content[start:end]) for start, end in code_regions)


def _reference_find_matches(content, name, code_regions):
    if _reference_in_code_regions(content, name, code_regions):
        return True
    if re.search(r"\b" + re.escape(name) + r"\s*\(", content):
        return True
    keywords = "|".join(re.escape(kw) for kw in mentions._DECLARATION_KEYWORDS)
    declaration_pattern = r"\b(" + keywords + r")\s+" + re.escape(name) + r"\b"
    return any(re.search(declaration_pattern, line) for line in content.splitlines())


def _reference_relationships(content, filename, known_entities, ambiguous_mode):
    code_regions = mentions._parse_code_regions(content)
    entity_map: dict[str, list[str]] = {}
    for name, label in known_entities:
        entity_map.setdefault(name, []).append(label)
    linked = set()
    out = []
    for name, label in known_entities:
        if name in linked:
            continue
        if not _reference_find_matches(content, name, code_regions):
            continue
        labels = entity_map[name]
        if len(labels) > 1 and ambiguous_mode == "skip":
            linked.add(name)
            continue
        for target_label in labels if ambiguous_mode == "all" else [label]:
            out.append(("Document", filename, "MENTIONS", target_label, name))
        linked.add(name)
    return out


def _actual_relationships(content, filename, known_entities, ambiguous_mode):
    result = MentionsExtractor("r", ambiguous_mode=ambiguous_mode).extract_from_source(
        content, filename, known_entities
    )
    return [
        (r.source_label, r.source_name, r.relationship_type, r.target_label, r.target_name)
        for r in result.relationships
    ]


def _assert_equivalent(content, known_entities):
    for mode in ("all", "skip"):
        expected = _reference_relationships(content, "doc.md", known_entities, mode)
        assert _actual_relationships(content, "doc.md", known_entities, mode) == expected, (content, mode)
    names = {name for name, _ in known_entities}
    code_regions = mentions._parse_code_regions(content)
    expected_any = any(_reference_find_matches(content, name, code_regions) for name in names)
    assert mentions_any(content, names) == expected_any, content


# Pieces that exercise every rule: word boundaries (ASCII, Unicode letters
# and digits, underscore), code spans and fences, escapes, call and
# declaration syntax, and every whitespace and line-break kind \s and
# splitlines know.
_WORDS = ["foo", "Foo", "foo_bar", "bar", "x1", "_x", "9z", "café", "ß", "Σx", "名前", "class", "def", "int", "var"]
_SEPARATORS = [
    " ", "  ", "\t", "\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x85", " ", " ", "　",
    "`", "``", "```", "~~~", "\\", "(", ")", " (", ".", "::", "-", "$", "#", "*", ",", "é",
]
_NAME_EXTRAS = ["", " ", "foo.bar", "a::b", "operator()", "$x", "-x", "x-", "foo(", "(", "`", "foo bar", "é", "\n"]


def _random_text(rng: random.Random) -> str:
    return "".join(
        rng.choice(_WORDS) if rng.random() < 0.5 else rng.choice(_SEPARATORS) for _ in range(rng.randint(0, 40))
    )


def _random_name(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.6:
        return rng.choice(_WORDS)
    if roll < 0.8:
        return rng.choice(_NAME_EXTRAS)
    return _random_text(rng)[: rng.randint(1, 8)]


def test_equivalent_on_random_inputs():
    rng = random.Random(20261011)
    labels = ["Function", "Class", "Module"]
    for _ in range(3000):
        known = [(_random_name(rng), rng.choice(labels)) for _ in range(rng.randint(0, 12))]
        _assert_equivalent(_random_text(rng), known)


def test_equivalent_on_repo_markdown():
    docs = sorted(REPO_ROOT.glob("*.md")) + sorted((REPO_ROOT / "docs").rglob("*.md"))[:10]
    source = (REPO_ROOT / "devgraph" / "indexer" / "dispatch.py").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"\b(?:def|class)\s+(\w+)", source)))
    names += ["index_file", "GraphEngine", "README", "get", "run", "a.b", "--help", "devgraph add", ""]
    known = [(name, "Function" if index % 3 else "Class") for index, name in enumerate(names)]
    known += [(name, "Module") for name in names[::7]]  # ambiguous names
    assert docs and len(names) > 50
    for path in docs:
        _assert_equivalent(path.read_text(encoding="utf-8")[:12000], known)


def test_names_absent_from_the_text_skip_the_regex_matcher(monkeypatch):
    """Only names that occur in the text reach the per-name regexes: a save
    must not cost one regex pass per name in the repository."""
    checked = []
    real = mentions._find_matches

    def spy(content, name, code_regions):
        checked.append(name)
        return real(content, name, code_regions)

    monkeypatch.setattr(mentions, "_find_matches", spy)
    known = [(f"name_{index}", "Function") for index in range(5000)] + [("Present", "Class")]
    result = MentionsExtractor("r").extract_from_source("Call `Present` here; name_x is not one.", "d.md", known)

    assert [rel.target_name for rel in result.relationships] == ["Present"]
    assert checked == ["Present"]
