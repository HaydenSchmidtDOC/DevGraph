"""Unit tests for container extractor."""

import pytest

from devgraph.indexer.containers.extractor import ContainerExtractor


class TestContainerExtractor:
    """Tests for ContainerExtractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.repo_id = "test-repo"
        self.extractor = ContainerExtractor(self.repo_id)

    def test_extract_from_simple_containerfile(self):
        """Test extraction from a basic Containerfile."""
        containerfile_content = """FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

ENTRYPOINT ["python", "main.py"]
"""
        result = self.extractor.extract_from_containerfile(containerfile_content)

        assert len(result.containers) == 1
        assert result.containers[0].name == "python"
        assert result.containers[0].image == "python:3.13-slim"
        assert result.containers[0].repo_id == self.repo_id
        assert result.containers[0].properties["source"] == "Containerfile"

    def test_extract_from_containerfile_with_registry(self):
        """Test extraction with registry prefix in image name."""
        containerfile_content = "FROM docker.io/library/nginx:latest"
        result = self.extractor.extract_from_containerfile(containerfile_content)

        assert len(result.containers) == 1
        assert result.containers[0].name == "nginx"
        assert result.containers[0].image == "docker.io/library/nginx:latest"

    def test_extract_from_simple_compose_file(self):
        """Test extraction from a basic docker-compose.yml."""
        compose_content = """
version: '3.8'
services:
  web:
    image: python:3.13
    volumes:
      - ./code:/app
  db:
    image: postgres:15
    volumes:
      - db_data:/var/lib/postgresql/data

volumes:
  db_data:

networks:
  default:
"""
        result = self.extractor.extract_from_compose_file(compose_content, "docker-compose.yml")

        # Should have 2 services
        assert len(result.services) == 2
        service_names = {s.name for s in result.services}
        assert service_names == {"web", "db"}

        # Should have 2 containers
        assert len(result.containers) == 2
        container_images = {c.image for c in result.containers}
        assert "python:3.13" in container_images
        assert "postgres:15" in container_images

        # Should have 1 volume
        assert len(result.volumes) == 1
        assert result.volumes[0].name == "db_data"

        # Check relationships: services should RUNS containers
        runs_relationships = [
            r for r in result.relationships if r.relationship_type == "RUNS"
        ]
        assert len(runs_relationships) == 2

    def test_extract_services_using_volumes(self):
        """Test that service-volume relationships are created."""
        compose_content = """
version: '3.8'
services:
  app:
    image: app:latest
    volumes:
      - data:/data
      - ./logs:/logs

volumes:
  data:
"""
        result = self.extractor.extract_from_compose_file(compose_content)

        # Check that service uses named volume (bind mounts like ./logs are excluded)
        uses_relationships = [
            r for r in result.relationships if r.relationship_type == "USES"
        ]
        assert len(uses_relationships) == 1
        assert uses_relationships[0].source_name == "app"
        assert uses_relationships[0].target_name == "data"

    def test_extract_from_compose_with_networks(self):
        """Test extraction of networks from compose file."""
        compose_content = """
version: '3.8'
services:
  web:
    image: web:latest
    networks:
      - frontend
  api:
    image: api:latest
    networks:
      - backend

networks:
  frontend:
  backend:
"""
        result = self.extractor.extract_from_compose_file(compose_content)

        # Should have 2 networks
        assert len(result.networks) == 2
        network_names = {n.name for n in result.networks}
        assert network_names == {"frontend", "backend"}

    def test_extract_from_malformed_yaml(self):
        """Test handling of malformed YAML."""
        malformed_yaml = """
version: '3.8'
services:
  web:
    image: python:3.13
    this is invalid yaml {{{
"""
        result = self.extractor.extract_from_compose_file(malformed_yaml)

        # Should return empty result for malformed YAML
        assert len(result.services) == 0
        assert len(result.containers) == 0

    def test_extract_from_compose_missing_image(self):
        """Test handling of service without image specified."""
        compose_content = """
version: '3.8'
services:
  custom:
    build: ./custom
"""
        result = self.extractor.extract_from_compose_file(compose_content)

        # Should still create service node
        assert len(result.services) == 1
        assert result.services[0].name == "custom"

        # But no container node (since no image)
        assert len(result.containers) == 0

    def test_multiple_services_with_relationships(self):
        """Test complex scenario with multiple services and relationships."""
        compose_content = """
version: '3.8'
services:
  web:
    image: nginx:latest
    volumes:
      - cache:/var/cache/nginx
  app:
    image: app:1.0
    volumes:
      - cache:/app/cache
      - logs:/var/log/app

volumes:
  cache:
  logs:
"""
        result = self.extractor.extract_from_compose_file(compose_content)

        assert len(result.services) == 2
        assert len(result.containers) == 2
        assert len(result.volumes) == 2

        # Both services should use cache volume
        uses_relationships = [
            r for r in result.relationships if r.relationship_type == "USES"
        ]
        cache_users = [r.source_name for r in uses_relationships if r.target_name == "cache"]
        assert len(cache_users) == 2

    def test_repo_id_scoping(self):
        """Test that all extracted nodes have correct repo_id."""
        compose_content = """
version: '3.8'
services:
  api:
    image: api:latest

volumes:
  data:

networks:
  default:
"""
        result = self.extractor.extract_from_compose_file(compose_content)

        for service in result.services:
            assert service.repo_id == self.repo_id

        for container in result.containers:
            assert container.repo_id == self.repo_id

        for volume in result.volumes:
            assert volume.repo_id == self.repo_id

        for network in result.networks:
            assert network.repo_id == self.repo_id

    def test_image_name_normalization(self):
        """Test that image names are normalized correctly."""
        test_cases = [
            ("node:18", "node"),
            ("docker.io/library/node:18", "node"),
            ("registry.example.com/myteam/node:18", "node"),
            ("node", "node"),
            ("MyImage:latest", "myimage"),
        ]

        for full_ref, expected_name in test_cases:
            result = ContainerExtractor._extract_image_name(full_ref)
            assert result == expected_name

    def test_build_context_shorthand_string(self):
        """build: ./services/api (string shorthand) -> build_context 'services/api'."""
        content = "services:\n  api:\n    build: ./services/api\n"
        result = self.extractor.extract_from_compose_file(content)
        assert result.services[0].properties["build_context"] == "services/api"

    def test_build_context_long_form(self):
        """build: {context: ./services/api} -> build_context 'services/api'."""
        content = "services:\n  api:\n    build:\n      context: ./services/api\n"
        result = self.extractor.extract_from_compose_file(content)
        assert result.services[0].properties["build_context"] == "services/api"

    def test_build_context_falls_back_to_dockerfile_directory(self):
        """When context is repo root but dockerfile points into a
        subdirectory (a common shared-build-context pattern), the service's
        source directory is the Dockerfile's own directory.
        """
        content = (
            "services:\n"
            "  worker:\n"
            "    build:\n"
            "      context: .\n"
            "      dockerfile: services/ingestion/Dockerfile\n"
        )
        result = self.extractor.extract_from_compose_file(content)
        assert result.services[0].properties["build_context"] == "services/ingestion"

    def test_no_build_context_for_image_only_service(self):
        """A service with only `image:` (no `build:`) has no source
        directory of its own — build_context should be absent, not empty.
        """
        content = "services:\n  db:\n    image: postgres:15\n"
        result = self.extractor.extract_from_compose_file(content)
        assert "build_context" not in result.services[0].properties

    def test_build_context_at_repo_root_is_none(self):
        """build: . (repo root) has no meaningful subdirectory to attribute
        files to, so build_context should be absent."""
        content = "services:\n  api:\n    build: .\n"
        result = self.extractor.extract_from_compose_file(content)
        assert "build_context" not in result.services[0].properties
