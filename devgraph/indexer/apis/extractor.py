"""API extractor for parsing FastAPI, Flask, and Django route definitions."""

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EndpointNode:
    """Represents an API Endpoint node."""

    path: str
    method: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class FunctionNode:
    """Represents a Function node (handler)."""

    name: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    """Represents a relationship between nodes."""

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str
    properties: dict = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """Result of API extraction."""

    endpoints: List[EndpointNode] = field(default_factory=list)
    functions: List[FunctionNode] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)


class APIExtractor:
    """Extracts API endpoints and their handlers from Python source code."""

    def __init__(self, repo_id: str):
        """Initialize the API extractor.

        Args:
            repo_id: The repository identifier for scoping extracted nodes.
        """
        self.repo_id = repo_id

    def extract_from_source(self, content: str, filename: str = "unknown.py") -> ExtractionResult:
        """Extract API endpoints from Python source code.

        Supports FastAPI, Flask, and Django patterns.

        Args:
            content: The Python source code content.
            filename: The source filename for context.

        Returns:
            ExtractionResult containing extracted Endpoint and Function nodes.
        """
        result = ExtractionResult()

        # Try FastAPI extraction
        self._extract_fastapi_routes(content, result, filename)

        # Try Flask extraction
        self._extract_flask_routes(content, result, filename)

        # Try Django extraction
        self._extract_django_urls(content, result, filename)

        return result

    def _extract_fastapi_routes(
        self, content: str, result: ExtractionResult, filename: str
    ) -> None:
        """Extract FastAPI routes from source code."""
        # Pattern for @app.get/post/put/delete/patch decorators
        fastapi_pattern = r"@(?:app|router)\.(?P<method>get|post|put|delete|patch|options|head)\(['\"](?P<path>[^'\"]*)['\"]"

        matches = re.finditer(fastapi_pattern, content, re.IGNORECASE)

        for match in matches:
            method = match.group("method").upper()
            path = match.group("path")

            # Extract the handler function name (appears after the decorator)
            decorator_end = match.end()
            remaining = content[decorator_end:]
            func_match = re.search(r"async\s+def\s+(\w+)|def\s+(\w+)", remaining)

            handler_name = None
            if func_match:
                handler_name = func_match.group(1) or func_match.group(2)

            # Create Endpoint node
            endpoint_id = f"{method} {path}"
            endpoint = EndpointNode(
                path=path,
                method=method,
                repo_id=self.repo_id,
                properties={"framework": "FastAPI", "source": filename},
            )
            result.endpoints.append(endpoint)

            # Create Function node and relationship if handler identified
            if handler_name:
                function = FunctionNode(
                    name=handler_name,
                    repo_id=self.repo_id,
                    properties={"type": "handler", "source": filename},
                )
                result.functions.append(function)

                relationship = Relationship(
                    source_label="Endpoint",
                    source_name=endpoint_id,
                    relationship_type="IMPLEMENTS",
                    target_label="Function",
                    target_name=handler_name,
                )
                result.relationships.append(relationship)

    def _extract_flask_routes(
        self, content: str, result: ExtractionResult, filename: str
    ) -> None:
        """Extract Flask routes from source code."""
        # Pattern for @app.route() or @app.get(), @app.post(), etc.
        flask_pattern = r"@app\.(?:route|get|post|put|delete|patch)\(['\"](?P<path>[^'\"]*)['\"](?:[^)]*methods=['\"](?P<methods>[^'\"]*)['\"])?"

        matches = re.finditer(flask_pattern, content, re.IGNORECASE)

        for match in matches:
            path = match.group("path")
            methods_str = match.group("methods")

            # Default to GET if not specified
            methods = methods_str.split(",") if methods_str else ["GET"]
            methods = [m.strip().upper() for m in methods]

            for method in methods:
                # Extract the handler function name
                decorator_end = match.end()
                remaining = content[decorator_end:]
                func_match = re.search(r"def\s+(\w+)", remaining)

                handler_name = None
                if func_match:
                    handler_name = func_match.group(1)

                # Create Endpoint node
                endpoint_id = f"{method} {path}"
                endpoint = EndpointNode(
                    path=path,
                    method=method,
                    repo_id=self.repo_id,
                    properties={"framework": "Flask", "source": filename},
                )
                result.endpoints.append(endpoint)

                # Create Function node and relationship if handler identified
                if handler_name:
                    function = FunctionNode(
                        name=handler_name,
                        repo_id=self.repo_id,
                        properties={"type": "handler", "source": filename},
                    )
                    result.functions.append(function)

                    relationship = Relationship(
                        source_label="Endpoint",
                        source_name=endpoint_id,
                        relationship_type="IMPLEMENTS",
                        target_label="Function",
                        target_name=handler_name,
                    )
                    result.relationships.append(relationship)

    def _extract_django_urls(
        self, content: str, result: ExtractionResult, filename: str
    ) -> None:
        """Extract Django URL patterns from urls.py files."""
        # Pattern for Django path() and re_path() calls
        django_pattern = r"(?:path|re_path)\(['\"](?P<pattern>[^'\"]*)['\"][^)]*,\s*(?P<view>\w+)"

        matches = re.finditer(django_pattern, content)

        for match in matches:
            pattern = match.group("pattern")
            view_name = match.group("view")

            # For Django, we generally don't have explicit HTTP methods in urls.py
            # Create an Endpoint node (methods will be inferred from the view)
            endpoint_id = f"* {pattern}"
            endpoint = EndpointNode(
                path=pattern,
                method="*",  # Django views handle multiple methods
                repo_id=self.repo_id,
                properties={"framework": "Django", "source": filename},
            )
            result.endpoints.append(endpoint)

            # Create Function (view) node and relationship
            function = FunctionNode(
                name=view_name,
                repo_id=self.repo_id,
                properties={"type": "view", "source": filename},
            )
            result.functions.append(function)

            relationship = Relationship(
                source_label="Endpoint",
                source_name=endpoint_id,
                relationship_type="IMPLEMENTS",
                target_label="Function",
                target_name=view_name,
            )
            result.relationships.append(relationship)
