"""Container extractor for parsing Containerfile, Dockerfile, and docker-compose/podman-compose files."""

import posixpath
from dataclasses import dataclass, field
from typing import List, Optional

from devgraph.config.project_tools import YAML_LOAD_ERRORS
from devgraph.config.yaml_bound import YAML_MAX_NODES, bounded_safe_load


@dataclass
class ContainerNode:
    """Represents a Container node to be added to the graph."""

    name: str
    image: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class ServiceNode:
    """Represents a Service node to be added to the graph."""

    name: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class NetworkNode:
    """Represents a Network node to be added to the graph."""

    name: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class VolumeNode:
    """Represents a Volume node to be added to the graph."""

    name: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    """Represents a relationship between two nodes.

    from_file: set when the source endpoint is a Service (now keyed on
    (repo_id, name, file) — see ServiceNode) so the edge matches the exact
    Service that declared it, not every same-named Service across every
    compose file in the repo. Container/Volume stay bare-name matched
    (deliberately not file-scoped -- see _upsert_container_result).
    """

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str
    properties: dict = field(default_factory=dict)
    from_file: Optional[str] = None
    to_file: Optional[str] = None


@dataclass
class ExtractionResult:
    """Result of container extraction."""

    containers: List[ContainerNode] = field(default_factory=list)
    services: List[ServiceNode] = field(default_factory=list)
    networks: List[NetworkNode] = field(default_factory=list)
    volumes: List[VolumeNode] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)


class ContainerExtractor:
    """Extracts container and service definitions from Containerfiles and compose files."""

    def __init__(self, repo_id: str):
        """Initialize the extractor for a specific repository.

        Args:
            repo_id: The repository identifier for scoping extracted nodes.
        """
        self.repo_id = repo_id

    def extract_from_containerfile(self, content: str, filename: str = "Containerfile") -> ExtractionResult:
        """Extract container information from a Containerfile/Dockerfile.

        Args:
            content: The raw Containerfile/Dockerfile content.
            filename: The repo-relative path of this Containerfile, recorded
                as provenance. Container nodes stay bare-name keyed
                regardless (see _upsert_container_result) -- this only
                matters if/when a Containerfile-only node type becomes
                file-scoped in the future.

        Returns:
            ExtractionResult containing extracted Container nodes.
        """
        result = ExtractionResult()
        lines = content.split("\n")

        # Parse FROM instruction to get base image
        base_image = None
        for line in lines:
            line = line.strip()
            if line.upper().startswith("FROM"):
                # Extract image name from FROM instruction
                parts = line.split(maxsplit=1)
                if len(parts) > 1:
                    base_image = parts[1].split()[0]  # Remove any aliases
                    break

        if base_image:
            # Create a container node based on the base image
            container = ContainerNode(
                name=self._extract_image_name(base_image),
                image=base_image,
                repo_id=self.repo_id,
                properties={"source": filename},
            )
            result.containers.append(container)

        return result

    def extract_from_compose_file(
        self, content: str, filename: str = "docker-compose.yml"
    ) -> ExtractionResult:
        """Extract services and container definitions from a compose file (docker-compose or podman-compose).

        Args:
            content: The raw YAML content of the compose file.
            filename: The name of the compose file for context.

        Returns:
            ExtractionResult containing extracted Service, Container, Network, and Volume nodes,
            and Service RUNS/USES/DEPENDS_ON relationships.
        """
        result = ExtractionResult()

        try:
            compose_data = bounded_safe_load(content, YAML_MAX_NODES)
        except YAML_LOAD_ERRORS:
            # Return empty result if YAML is malformed
            return result

        if not isinstance(compose_data, dict):
            return result

        # Extract services
        services = compose_data.get("services", {})
        if isinstance(services, dict):
            for service_name, service_config in services.items():
                if not isinstance(service_config, dict):
                    continue

                # Create Service node. build_context (when present) is the
                # service's source directory relative to the repository
                # ("" its root), resolved from the compose file's folder —
                # used by devgraph/indexer/dispatch.py to link a file's
                # datastore/Endpoint nodes back to the Service that owns
                # it, via directory containment.
                build_context = self._extract_build_context(service_config.get("build"), posixpath.dirname(filename))
                # `file` (in addition to `source`) is what makes
                # _upsert_nodes_tx key this Service on (repo_id, name, file)
                # instead of bare (repo_id, name) -- two compose files
                # declaring a same-named but unrelated service (a common
                # monorepo pattern) would otherwise silently merge into one
                # node, same root cause as the Function/Class collision.
                properties = {"source": filename, "file": filename}
                if build_context is not None:
                    properties["build_context"] = build_context
                service = ServiceNode(
                    name=service_name,
                    repo_id=self.repo_id,
                    properties=properties,
                )
                result.services.append(service)

                # Extract Container reference if image is specified
                image = service_config.get("image")
                if image:
                    container = ContainerNode(
                        name=self._extract_image_name(image),
                        image=image,
                        repo_id=self.repo_id,
                        properties={"source": filename},
                    )
                    result.containers.append(container)

                    # Service RUNS Container relationship. from_file pins
                    # this to the exact Service that declared it (now
                    # file-scoped); Container stays bare-name matched since
                    # it deliberately isn't file-scoped (shared base image).
                    relationship = Relationship(
                        source_label="Service",
                        source_name=service_name,
                        relationship_type="RUNS",
                        target_label="Container",
                        target_name=self._extract_image_name(image),
                        from_file=filename,
                    )
                    result.relationships.append(relationship)

                # Service DEPENDS_ON Service, from `depends_on` (a list, or a
                # dict keyed by service name) and legacy `links` ("db" or
                # "db:alias"). Both ends are pinned to this compose file:
                # compose resolves these names among its own services.
                for dependency in self._service_dependencies(service_config):
                    result.relationships.append(
                        Relationship(
                            source_label="Service",
                            source_name=service_name,
                            relationship_type="DEPENDS_ON",
                            target_label="Service",
                            target_name=dependency,
                            from_file=filename,
                            to_file=filename,
                        )
                    )

                # Extract volumes referenced by this service
                volumes = service_config.get("volumes", [])
                if isinstance(volumes, list):
                    for vol_ref in volumes:
                        if isinstance(vol_ref, str):
                            vol_name = vol_ref.split(":")[0]
                            if vol_name and not vol_name.startswith("/") and not vol_name.startswith("."):
                                # It's a named volume, not a bind mount (which starts with / or .)
                                service_rel = Relationship(
                                    source_label="Service",
                                    source_name=service_name,
                                    relationship_type="USES",
                                    target_label="Volume",
                                    target_name=vol_name,
                                    from_file=filename,
                                )
                                result.relationships.append(service_rel)

        # Extract top-level networks
        networks = compose_data.get("networks", {})
        if isinstance(networks, dict):
            for network_name in networks.keys():
                network = NetworkNode(
                    name=network_name,
                    repo_id=self.repo_id,
                    properties={"source": filename},
                )
                result.networks.append(network)

        # Extract top-level volumes
        volumes = compose_data.get("volumes", {})
        if isinstance(volumes, dict):
            for volume_name in volumes.keys():
                volume = VolumeNode(
                    name=volume_name,
                    repo_id=self.repo_id,
                    properties={"source": filename},
                )
                result.volumes.append(volume)

        return result

    @staticmethod
    def _service_dependencies(service_config: dict) -> list[str]:
        """The service names a compose service's `depends_on` and `links` name, deduplicated, in order."""
        names: list[str] = []
        depends_on = service_config.get("depends_on")
        if isinstance(depends_on, (list, dict)):
            names += [name for name in depends_on if isinstance(name, str)]
        links = service_config.get("links")
        if isinstance(links, list):
            names += [link.split(":", 1)[0] for link in links if isinstance(link, str)]
        return [name for name in dict.fromkeys(name.strip() for name in names) if name]

    @staticmethod
    def _extract_build_context(build_config, compose_dir: str = "") -> Optional[str]:
        """A compose service's `build` field as a repository-relative
        directory ("" the repository root), resolved from `compose_dir`, the
        compose file's own folder, as compose resolves it.

        Handles:
        - shorthand ('build: ./services/api')
        - long form with a real context ('build: {context: ./services/api}')
        - long form where the context is the compose file's own folder or
          one above it and the dockerfile points into a subdirectory of the
          context ('build: {context: .., dockerfile:
          services/ingestion/Dockerfile}') — a common compose pattern
          (single shared build context, per-service Dockerfile) where the
          actual service source lives at the Dockerfile's own directory
          (relative to the context), not at the context itself.

        Returns None for services with no `build` key (image-only services
        like databases/brokers — those correctly have no source directory of
        their own to attribute files to), or a context outside the
        repository or not a folder (an absolute path, a Git URL).
        """
        if isinstance(build_config, str):
            return ContainerExtractor._resolve_build_dir(compose_dir, build_config)

        if not isinstance(build_config, dict):
            return None

        context = build_config.get("context", ".")
        if not isinstance(context, str):
            return None
        context_dir = ContainerExtractor._resolve_build_dir(compose_dir, context)
        if context_dir is None:
            return None

        # A shared context (the compose file's own folder or one above it)
        # with a per-service Dockerfile in a folder of it: the Dockerfile's
        # own directory, which is where the service's source lives. A
        # service's own context (`./api`) stays its source, wherever its
        # Dockerfile is.
        dockerfile = build_config.get("dockerfile")
        shared = context_dir == "" or compose_dir == context_dir or compose_dir.startswith(context_dir + "/")
        if shared and isinstance(dockerfile, str):
            dockerfile_dir = dockerfile.replace("\\", "/").rsplit("/", 1)
            if len(dockerfile_dir) == 2:
                resolved = ContainerExtractor._resolve_build_dir(context_dir, dockerfile_dir[0])
                if resolved is not None:
                    return resolved

        return context_dir

    @staticmethod
    def _resolve_build_dir(base: str, raw: str) -> Optional[str]:
        """`raw`, a build directory written relative to the repository
        directory `base`, as a normalized repository-relative one ("" the
        root); None for an empty, absolute or URL one, or one leaving the
        repository."""
        normalized = raw.replace("\\", "/").strip()
        if not normalized or normalized.startswith("/") or "://" in normalized:
            return None
        resolved = posixpath.normpath(posixpath.join(base, normalized))
        if resolved == ".." or resolved.startswith("../"):
            return None
        return "" if resolved == "." else resolved

    @staticmethod
    def _extract_image_name(image_ref: str) -> str:
        """Extract a simple name from a full image reference.

        Args:
            image_ref: Full image reference (e.g., 'postgres:15' or 'docker.io/library/nginx:latest')

        Returns:
            Simple image name suitable for a node identifier.
        """
        # Remove registry prefix if present
        if "/" in image_ref:
            image_ref = image_ref.split("/")[-1]

        # Remove tag if present
        if ":" in image_ref:
            image_ref = image_ref.split(":")[0]

        return image_ref.lower()
