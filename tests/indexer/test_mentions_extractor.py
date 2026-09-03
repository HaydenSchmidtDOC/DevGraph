"""Unit tests for the Phase 2 mentions extractor."""

from devgraph.indexer.mentions.extractor import MentionsExtractor


class TestMentionsExtractor:
    def setup_method(self):
        self.repo_id = "test-repo"
        self.extractor = MentionsExtractor(self.repo_id, ambiguous_mode="all")

    def test_code_span_match(self):
        """Detect a name mentioned in inline code (backticks)."""
        content = "This function uses `MyFunction` in the implementation."
        known_entities = [("MyFunction", "Function")]

        result = self.extractor.extract_from_source(content, "test.md", known_entities)

        assert len(result.documents) == 1
        assert result.documents[0].name == "test.md"
        assert result.documents[0].label == "Document"

        assert len(result.relationships) == 1
        rel = result.relationships[0]
        assert rel.source_label == "Document"
        assert rel.source_name == "test.md"
        assert rel.relationship_type == "MENTIONS"
        assert rel.target_label == "Function"
        assert rel.target_name == "MyFunction"

    def test_fenced_block_match(self):
        """Detect a name mentioned in a fenced code block."""
        content = """Here's an example:

```python
def do_work():
    call_helper()
    return MyClass()
```

The MyClass is defined elsewhere.
"""
        known_entities = [("MyClass", "Class"), ("call_helper", "Function")]

        result = self.extractor.extract_from_source(content, "docs.md", known_entities)

        assert len(result.documents) == 1
        assert len(result.relationships) == 2

        names = {rel.target_name for rel in result.relationships}
        assert "MyClass" in names
        assert "call_helper" in names

    def test_call_syntax_match(self):
        """Detect a name used in call syntax (Name\s*\()."""
        content = "You can call process_data() directly in your code."
        known_entities = [("process_data", "Function")]

        result = self.extractor.extract_from_source(content, "guide.md", known_entities)

        assert len(result.relationships) == 1
        assert result.relationships[0].target_name == "process_data"

    def test_call_syntax_with_spacing(self):
        """Detect call syntax even with extra whitespace before parenthesis."""
        content = "The process_data  ( ) function is documented here."
        known_entities = [("process_data", "Function")]

        result = self.extractor.extract_from_source(content, "guide.md", known_entities)

        assert len(result.relationships) == 1

    def test_declaration_syntax_class_keyword(self):
        """Detect declaration syntax with 'class' keyword."""
        content = "You might declare a class MyService like this on the same line."
        known_entities = [("MyService", "Class")]

        result = self.extractor.extract_from_source(content, "ref.md", known_entities)

        assert len(result.relationships) == 1
        assert result.relationships[0].target_name == "MyService"

    def test_declaration_syntax_def_keyword(self):
        """Detect declaration syntax with 'def' keyword."""
        content = "The def my_function is the entry point."
        known_entities = [("my_function", "Function")]

        result = self.extractor.extract_from_source(content, "ref.md", known_entities)

        assert len(result.relationships) == 1

    def test_declaration_syntax_struct_keyword(self):
        """Detect declaration syntax with 'struct' keyword."""
        content = "A struct User is used to represent users."
        known_entities = [("User", "Class")]

        result = self.extractor.extract_from_source(content, "ref.md", known_entities)

        assert len(result.relationships) == 1

    def test_declaration_syntax_function_keyword(self):
        """Detect declaration syntax with 'function' keyword."""
        content = "The function deployService is responsible for deployment."
        known_entities = [("deployService", "Function")]

        result = self.extractor.extract_from_source(content, "ref.md", known_entities)

        assert len(result.relationships) == 1

    def test_declaration_syntax_interface_keyword(self):
        """Detect declaration syntax with 'interface' keyword."""
        content = "An interface DataProvider must support streaming."
        known_entities = [("DataProvider", "Class")]

        result = self.extractor.extract_from_source(content, "ref.md", known_entities)

        assert len(result.relationships) == 1

    def test_declaration_syntax_type_keywords(self):
        """Detect declaration syntax with type keywords (int, string, bool, etc)."""
        content = "For example: int counter, string message, bool flag, const PI."
        known_entities = [("counter", "Variable"), ("message", "Variable"), ("flag", "Variable"), ("PI", "Variable")]

        result = self.extractor.extract_from_source(content, "ref.md", known_entities)

        assert len(result.relationships) == 4

    def test_negative_case_bare_prose(self):
        """No mention if name appears only in bare prose (not code-like context)."""
        content = "The MyFunction is an important part of the system but is mentioned only in plain text here."
        known_entities = [("MyFunction", "Function")]

        result = self.extractor.extract_from_source(content, "text.md", known_entities)

        assert len(result.documents) == 1
        assert len(result.relationships) == 0

    def test_negative_case_no_match_at_all(self):
        """No relationships when entity name doesn't appear anywhere."""
        content = "This document discusses important concepts."
        known_entities = [("UnrelatedFunction", "Function")]

        result = self.extractor.extract_from_source(content, "text.md", known_entities)

        assert len(result.documents) == 1
        assert len(result.relationships) == 0

    def test_ambiguous_name_all_mode(self):
        """In 'all' mode, ambiguous names (multiple labels) create links to all."""
        content = "`Helper` is mentioned in code."
        # Same name, different labels (e.g., method in one class, function elsewhere)
        known_entities = [("Helper", "Function"), ("Helper", "Class")]

        extractor_all = MentionsExtractor(self.repo_id, ambiguous_mode="all")
        result = extractor_all.extract_from_source(content, "ambig.md", known_entities)

        assert len(result.relationships) == 2
        labels = {rel.target_label for rel in result.relationships}
        assert labels == {"Function", "Class"}

    def test_ambiguous_name_skip_mode(self):
        """In 'skip' mode, ambiguous names are skipped entirely."""
        content = "`Helper` is mentioned in code."
        # Same name, different labels
        known_entities = [("Helper", "Function"), ("Helper", "Class")]

        extractor_skip = MentionsExtractor(self.repo_id, ambiguous_mode="skip")
        result = extractor_skip.extract_from_source(content, "ambig.md", known_entities)

        assert len(result.relationships) == 0

    def test_document_node_properties(self):
        """Verify Document node has expected properties."""
        content = "# My Guide\n\nSome content about `Helper`."
        known_entities = [("Helper", "Function")]

        result = self.extractor.extract_from_source(content, "guide.md", known_entities)

        doc = result.documents[0]
        assert doc.name == "guide.md"
        assert doc.label == "Document"
        assert doc.repo_id == self.repo_id
        assert doc.properties["source_file"] == "guide.md"
        assert doc.properties["title"] == "My Guide"

    def test_heading_extraction_as_title(self):
        """Extract first H1 heading as title."""
        content = "# Main Section\n\n## Subsection\n\nContent."
        known_entities = []

        result = self.extractor.extract_from_source(content, "multi.md", known_entities)

        assert result.documents[0].properties["title"] == "Main Section"

    def test_title_defaults_to_filename_stem(self):
        """Use filename stem as title when no H1 heading exists."""
        content = "## No H1 heading here\n\nJust content."
        known_entities = []

        result = self.extractor.extract_from_source(content, "no-heading.md", known_entities)

        assert result.documents[0].properties["title"] == "no-heading"

    def test_multiple_mentions_of_same_entity(self):
        """Only one MENTIONS relationship per entity even if mentioned multiple times."""
        content = "`MyFunction` is used in line 1, and `MyFunction` is also on line 2."
        known_entities = [("MyFunction", "Function")]

        result = self.extractor.extract_from_source(content, "dup.md", known_entities)

        assert len(result.relationships) == 1

    def test_repo_id_scoping(self):
        """Document and relationships are scoped to repo_id."""
        content = "`Entity` is here."
        known_entities = [("Entity", "Class")]

        result = self.extractor.extract_from_source(content, "test.md", known_entities)

        assert all(doc.repo_id == self.repo_id for doc in result.documents)
        # Note: relationships use source_name (document) not repo_id, but are created within context
        assert result.relationships[0].source_name == "test.md"

    def test_nested_inline_code(self):
        """Detect names in nested/escaped contexts within backticks."""
        content = "Use `outer_func()` as shown."
        known_entities = [("outer_func", "Function")]

        result = self.extractor.extract_from_source(content, "code.md", known_entities)

        assert len(result.relationships) == 1

    def test_word_boundaries_in_code_spans(self):
        """Word boundaries are respected: 'name' doesn't match 'myname'."""
        content = "`myname` is different from `name`."
        known_entities = [("name", "Variable"), ("myname", "Variable")]

        result = self.extractor.extract_from_source(content, "bounds.md", known_entities)

        # Should match both because they're in code regions
        assert len(result.relationships) == 2

    def test_empty_content(self):
        """Gracefully handle empty content."""
        known_entities = [("Something", "Class")]

        result = self.extractor.extract_from_source("", "empty.md", known_entities)

        assert len(result.documents) == 1
        assert len(result.relationships) == 0

    def test_empty_known_entities(self):
        """Gracefully handle no known entities."""
        content = "`Function` and other code here."

        result = self.extractor.extract_from_source(content, "orphan.md", [])

        assert len(result.documents) == 1
        assert len(result.relationships) == 0

    def test_unrecognized_ambiguous_mode_falls_back_to_all(self):
        """An unrecognized ambiguous_mode value should fall back to 'all' behavior.

        This tests the fix for missing settings validation where an invalid
        mode would fall through to an undocumented third behavior instead of
        falling back to 'all' with a warning logged.
        """
        content = "`Helper` is mentioned in code."
        known_entities = [("Helper", "Function"), ("Helper", "Class")]

        # Create extractor with invalid ambiguous_mode
        extractor_invalid = MentionsExtractor(self.repo_id, ambiguous_mode="invalid_mode")
        result = extractor_invalid.extract_from_source(content, "test.md", known_entities)

        # Should fall back to "all" behavior: create relationships for all labels
        assert len(result.relationships) == 2
        labels = {rel.target_label for rel in result.relationships}
        assert labels == {"Function", "Class"}

    def test_unrecognized_ambiguous_mode_logs_warning(self, caplog):
        """An unrecognized ambiguous_mode should log a warning."""
        content = "`Entity` is in code."
        known_entities = [("Entity", "Class")]

        extractor_invalid = MentionsExtractor(self.repo_id, ambiguous_mode="bogus")
        result = extractor_invalid.extract_from_source(content, "test.md", known_entities)

        # Check that warning was logged (caplog captures logging output)
        assert any("bogus" in record.message for record in caplog.records if record.levelname == "WARNING")
