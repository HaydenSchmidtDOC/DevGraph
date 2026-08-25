"""Unit tests for the Phase 2 docs extractor."""

from devgraph.indexer.docs.extractor import DocsExtractor


class TestDocsExtractor:
    def setup_method(self):
        self.repo_id = "test-repo"
        self.extractor = DocsExtractor(self.repo_id)

    def test_extract_requirement(self):
        content = """---
type: requirement
id: req-auth-001
links: [AuthService]
---
# Users must authenticate before accessing protected endpoints
Body text describing the requirement.
"""
        result = self.extractor.extract_from_source(content, "req-auth.md")

        assert len(result.docs) == 1
        doc = result.docs[0]
        assert doc.label == "Requirement"
        assert doc.name == "req-auth-001"
        assert doc.repo_id == self.repo_id
        assert doc.properties["title"] == "Users must authenticate before accessing protected endpoints"

        assert len(result.relationships) == 1
        rel = result.relationships[0]
        assert rel.source_label == "Module"
        assert rel.source_name == "AuthService"
        assert rel.relationship_type == "SATISFIES"
        assert rel.target_label == "Requirement"
        assert rel.target_name == "req-auth-001"

    def test_extract_design_decision_with_supersedes_and_decided_by(self):
        content = """---
type: design_decision
id: dd-002
links: [PaymentService]
supersedes: dd-001
decided_by: note-perf-analysis
---
# Switch to async payment processing
"""
        result = self.extractor.extract_from_source(content, "dd-002.md")

        assert len(result.docs) == 1
        assert result.docs[0].label == "DesignDecision"

        rel_types = {(r.relationship_type, r.source_name, r.target_name) for r in result.relationships}
        assert ("DOCUMENTED_BY", "PaymentService", "dd-002") in rel_types
        assert ("SUPERSEDES", "dd-002", "dd-001") in rel_types
        assert ("DECIDED_BY", "dd-002", "note-perf-analysis") in rel_types

    def test_extract_architecture_note(self):
        content = """---
type: architecture_note
id: note-caching
links: [CacheLayer, AuthService]
---
# Caching strategy
"""
        result = self.extractor.extract_from_source(content, "note.md")

        assert len(result.docs) == 1
        assert result.docs[0].label == "ArchitectureNote"
        assert len(result.relationships) == 2
        for rel in result.relationships:
            assert rel.relationship_type == "DOCUMENTED_BY"

    def test_id_defaults_to_filename_stem(self):
        content = """---
type: requirement
---
# No explicit id
"""
        result = self.extractor.extract_from_source(content, "req-fallback-id.md")
        assert result.docs[0].name == "req-fallback-id"

    def test_missing_frontmatter_is_skipped(self):
        content = "# Just a regular markdown file\nNo front-matter here.\n"
        result = self.extractor.extract_from_source(content, "readme.md")
        assert result.docs == []
        assert result.relationships == []

    def test_unknown_type_is_skipped(self):
        content = """---
type: changelog_entry
id: irrelevant
---
Not a DevGraph note type.
"""
        result = self.extractor.extract_from_source(content, "changelog.md")
        assert result.docs == []

    def test_malformed_yaml_is_skipped(self):
        content = """---
type: requirement
  bad indent: [unclosed
---
Body
"""
        result = self.extractor.extract_from_source(content, "bad.md")
        assert result.docs == []

    def test_no_links_produces_no_relationships(self):
        content = """---
type: requirement
id: req-standalone
---
# Standalone requirement with no linked component
"""
        result = self.extractor.extract_from_source(content, "req.md")
        assert len(result.docs) == 1
        assert result.relationships == []

    def test_repo_id_scoping(self):
        content = """---
type: architecture_note
id: note-x
---
# X
"""
        result = self.extractor.extract_from_source(content)
        assert all(d.repo_id == self.repo_id for d in result.docs)
