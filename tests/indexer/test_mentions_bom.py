"""A Markdown file's title is its first heading after a UTF-8 byte-order mark too."""

from devgraph.indexer.mentions.extractor import MentionsExtractor


def test_a_bom_prefixed_heading_is_the_title():
    result = MentionsExtractor("r").extract_from_source("﻿# Release notes\nCall it.\n", "docs/notes.md", [])
    assert [d.properties["title"] for d in result.documents] == ["Release notes"]
