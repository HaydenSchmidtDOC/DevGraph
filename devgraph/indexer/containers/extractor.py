"""Container extractor for parsing Containerfile, Dockerfile, and docker-compose/podman-compose files."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import re

import yaml


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
    """Represents a relationship between two nodes."""

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str
    properties: dict = field(default_factory=dict)


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

    def extract_from_containerfile(self, content: str) -> ExtractionResult:
        """Extract container information from a Containerfile/Dockerfile.

        Args:
            content: The raw Containerfile/Dockerfile content.

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
                properties={"source": "Containerfile"},
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
            ExtractionResult containing extracted Service, Container, Network, and Volume nodes.
        """
        result = ExtractionResult()

        try:
            compose_data = yaml.safe_load(content)
        except yaml.YAMLError:
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

                # Create Service node
                service = ServiceNode(
                    name=service_name,
                    repo_id=self.repo_id,
                    properties={"source": filename},
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

                    # Service RUNS Container relationship
                    relationship = Relationship(
                        source_label="Service",
                        source_name=service_name,
                        relationship_type="RUNS",
                        target_label="Container",
                        target_name=self._extract_image_name(image),
                    )
                    result.relationships.append(relationship)

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
