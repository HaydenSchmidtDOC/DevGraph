"""Datastore extractor for detecting database and cache client usage in Python code."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set


class DatastoreType(str, Enum):
    """Types of datastores that can be detected."""

    DATABASE = "Database"
    VECTORSTORE = "VectorStore"
    QUEUE = "Queue"
    CACHE = "Cache"


@dataclass
class DatastoreNode:
    """Represents a datastore node (Database, VectorStore, Queue, etc.)."""

    name: str
    datastore_type: str  # Database, VectorStore, Queue
    provider: str  # e.g., "PostgreSQL", "Qdrant", "Redis", "RabbitMQ"
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
    """Result of datastore extraction."""

    datastores: List[DatastoreNode] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)


class DatastoreExtractor:
    """Detects datastore client usage and imports in Python source code."""

    # Mapping of package imports to datastore info
    DATASTORE_LIBRARY_MAP = {
        # PostgreSQL
        r"psycopg2": ("Database", "PostgreSQL", "psycopg2"),
        r"psycopg": ("Database", "PostgreSQL", "psycopg"),
        # Redis
        r"redis": ("Cache", "Redis", "redis-py"),
        # Neo4j
        r"neo4j": ("Database", "Neo4j", "neo4j-python-driver"),
        # Qdrant
        r"qdrant_client": ("VectorStore", "Qdrant", "qdrant-client"),
        # Kafka
        r"kafka": ("Queue", "Kafka", "kafka-python"),
        # RabbitMQ
        r"pika": ("Queue", "RabbitMQ", "pika"),
        r"kombu": ("Queue", "RabbitMQ", "kombu"),
        # MongoDB
        r"pymongo": ("Database", "MongoDB", "pymongo"),
        # DuckDB
        r"duckdb": ("Database", "DuckDB", "duckdb"),
        # SQLite (stdlib, but if imported explicitly)
        r"sqlite3": ("Database", "SQLite", "sqlite3"),
        # Valkey (Redis compatible)
        r"valkey": ("Cache", "Valkey", "valkey"),
        # Weaviate
        r"weaviate": ("VectorStore", "Weaviate", "weaviate-client"),
        # Pinecone
        r"pinecone": ("VectorStore", "Pinecone", "pinecone-client"),
        # LangChain integrations (various datastores)
        r"langchain.*vector": ("VectorStore", "LangChain", "langchain"),
    }

    def __init__(self, repo_id: str):
        """Initialize the datastore extractor.

        Args:
            repo_id: The repository identifier for scoping extracted nodes.
        """
        self.repo_id = repo_id

    def extract_from_source(self, content: str, filename: str = "unknown.py") -> ExtractionResult:
        """Extract datastore usage from Python source code.

        Args:
            content: The Python source code content.
            filename: The source filename for context.

        Returns:
            ExtractionResult containing extracted Datastore nodes.
        """
        result = ExtractionResult()
        detected_datastores: Set[tuple] = set()  # (provider, type, library)

        # Extract from import statements
        self._extract_from_imports(content, detected_datastores)

        # Extract from connection string patterns and client instantiations
        self._extract_from_usage(content, detected_datastores)

        # Convert detected datastores to nodes
        for provider, ds_type, library in detected_datastores:
            datastore = DatastoreNode(
                name=provider,
                datastore_type=ds_type,
                provider=provider,
                repo_id=self.repo_id,
                properties={"library": library, "source": filename},
            )
            result.datastores.append(datastore)

        return result

    def _extract_from_imports(
        self, content: str, detected_datastores: Set[tuple]
    ) -> None:
        """Extract datastore information from import statements."""
        # Pattern for: import <module> or from <module> import <name>
        import_pattern = r"(?:from\s+(\S+)\s+)?import\s+(\S+)"

        for match in re.finditer(import_pattern, content):
            module = match.group(1) or match.group(2)
            # Extract the top-level module name
            module_name = module.split(".")[0]

            for lib_pattern, (ds_type, provider, library) in self.DATASTORE_LIBRARY_MAP.items():
                if re.match(lib_pattern, module_name):
                    detected_datastores.add((provider, ds_type, library))

    def _extract_from_usage(
        self, content: str, detected_datastores: Set[tuple]
    ) -> None:
        """Extract datastore usage from client instantiation and connection patterns."""
        # PostgreSQL connection strings
        if re.search(
            r"(psycopg2|psycopg)\.connect|postgresql://", content, re.IGNORECASE
        ):
            detected_datastores.add(("PostgreSQL", "Database", "psycopg2"))

        # MySQL connection strings
        if re.search(r"mysql://|pymysql", content, re.IGNORECASE):
            detected_datastores.add(("MySQL", "Database", "pymysql"))

        # Redis client instantiation
        if re.search(
            r"redis\.Redis|redis\.StrictRedis|redis\.ConnectionPool", content
        ):
            detected_datastores.add(("Redis", "Cache", "redis-py"))

        # Neo4j driver instantiation
        if re.search(
            r"neo4j\.GraphDatabase\.driver|from neo4j import GraphDatabase",
            content,
        ):
            detected_datastores.add(("Neo4j", "Database", "neo4j-python-driver"))

        # Qdrant client
        if re.search(r"qdrant_client\.QdrantClient|from qdrant_client import", content):
            detected_datastores.add(("Qdrant", "VectorStore", "qdrant-client"))

        # Kafka producer/consumer
        if re.search(
            r"KafkaProducer|KafkaConsumer|kafka\.KafkaProducer",
            content,
        ):
            detected_datastores.add(("Kafka", "Queue", "kafka-python"))

        # RabbitMQ - pika
        if re.search(r"pika\.BlockingConnection|pika\.ConnectionParameters", content):
            detected_datastores.add(("RabbitMQ", "Queue", "pika"))

        # RabbitMQ - kombu
        if re.search(
            r"kombu\.Connection|kombu\.Queue",
            content,
        ):
            detected_datastores.add(("RabbitMQ", "Queue", "kombu"))

        # MongoDB
        if re.search(r"pymongo\.MongoClient|MongoClient\(", content):
            detected_datastores.add(("MongoDB", "Database", "pymongo"))

        # DuckDB
        if re.search(r"duckdb\.connect|duckdb\.sql", content):
            detected_datastores.add(("DuckDB", "Database", "duckdb"))

        # Weaviate
        if re.search(r"weaviate\.Client|from weaviate import", content):
            detected_datastores.add(("Weaviate", "VectorStore", "weaviate-client"))

        # Pinecone
        if re.search(r"pinecone\.Index|pinecone\.init", content):
            detected_datastores.add(("Pinecone", "VectorStore", "pinecone-client"))

        # SQLite explicit usage
        if re.search(
            r"sqlite3\.connect|sqlite3\.Connection",
            content,
        ):
            detected_datastores.add(("SQLite", "Database", "sqlite3"))
