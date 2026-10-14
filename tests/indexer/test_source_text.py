"""Decoding source files for extraction: identifiers survive non-UTF-8 files."""

import logging

from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import _index_single_path
from devgraph.indexer.source_text import decode_source, read_source


def test_valid_utf8_is_unchanged():
    text = "def café():\n    return '→ ok'\n"
    assert decode_source(text.encode("utf-8")) == text
    assert decode_source(text.encode("utf-8"), python=True) == text


def test_a_pep_263_latin_1_declaration_is_honoured():
    text = "# -*- coding: latin-1 -*-\ndef café():\n    pass\n"
    assert decode_source(text.encode("latin-1"), python=True) == text


def test_a_coding_line_on_the_second_line_counts():
    text = "#!/usr/bin/env python\n# vim: set fileencoding=cp1252 :\nname = 'café – x'\n"
    assert decode_source(text.encode("cp1252"), python=True) == text


def test_a_coding_line_is_only_honoured_for_python():
    data = "# coding: latin-1\nx = 'é'\n".encode("utf-8")
    assert decode_source(data) == "# coding: latin-1\nx = 'é'\n"


def test_an_undeclared_cp1252_file_falls_back_from_utf8():
    text = "def café():\n    return “quoted” – dash\n"
    assert decode_source(text.encode("cp1252")) == text
    assert decode_source(text.encode("cp1252"), python=True) == text


def test_bytes_cp1252_leaves_undefined_fall_back_to_latin_1():
    data = b"name_\x81\xe9 = 1\n"
    assert decode_source(data) == "name_\x81é = 1\n"


def test_an_unknown_declared_codec_falls_back():
    data = "# coding: no-such-codec\nx = 'é'\n".encode("utf-8")
    assert decode_source(data, python=True) == "# coding: no-such-codec\nx = 'é'\n"


def test_read_source_picks_python_rules_by_suffix(tmp_path):
    (tmp_path / "a.py").write_bytes("# coding: latin-1\nx = 'é'\n".encode("latin-1"))
    assert read_source(tmp_path / "a.py") == "# coding: latin-1\nx = 'é'\n"


def test_a_latin_1_python_file_keeps_its_identifier(tmp_path, caplog):
    path = tmp_path / "shop.py"
    path.write_bytes("# -*- coding: latin-1 -*-\ndef café():\n    pass\n".encode("latin-1"))
    nodes: list = []

    class Engine:
        def replace_file_nodes(self, repo_id, rel_path, file_nodes, rels, **kwargs):
            nodes.extend(file_nodes)

    with caplog.at_level(logging.WARNING):
        _index_single_path(
            Engine(), "_unit_decode", tmp_path, path, "shop.py", None, False, None,
            [], {}, [], {}, [], {}, [], {}, [], {}, [], {}, [], {}, {}, [], [], set(),
        )
    assert "café" in {node["name"] for node in nodes if node["label"] == "Function"}


def test_referrer_text_is_decoded_the_same_way(tmp_path):
    path = tmp_path / "a.java"
    path.write_bytes("class Café {}\n".encode("cp1252"))
    assert dispatch._read_text(path) == "class Café {}\n"
