"""Datastore extractor for detecting database and cache client usage in Python code."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Set


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

        # Convert detected datastores to nodes. Sorted, not set order: two
        # libraries for the same provider in one file (e.g. psycopg2 and
        # psycopg) MERGE into one node, and the last one written wins its
        # `library` property.
        for provider, ds_type, library in sorted(detected_datastores):
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


# --- Other languages' clients ------------------------------------------------
#
# Each language's clients are found by literal text only: an import of a
# client library (a JS/TS module specifier, a Java/Kotlin/C# namespace, a Go
# import path), a connection URL's scheme at the start of a string literal,
# and a few calls and supertypes that name their store (a JDBC URL, Go's
# `sql.Open` driver name, EF Core's `UseNpgsql`, a Spring Data store's
# repository). Anything built at run time (a URL from an environment
# variable, a driver chosen by configuration) is not read. The node is the
# store's provider, as a Python client's is (one `PostgreSQL` Database
# however many files or services use it); an ORM whose store is configured
# outside the code (Prisma, Spring Data JPA, an EF Core DbContext) is named
# by the ORM.

#: (type, provider, library) of each client, by the import that names it: a
#: JS/TS module specifier (or its subpath), a Java/Kotlin package or C#
#: namespace (or one below it), a Go import path (or one below it).
CLIENT_IMPORTS: dict[str, dict[str, tuple[str, str, str]]] = {
    "js": {
        "pg": ("Database", "PostgreSQL", "pg"),
        "pg-pool": ("Database", "PostgreSQL", "pg"),
        "postgres": ("Database", "PostgreSQL", "postgres"),
        "mysql": ("Database", "MySQL", "mysql"),
        "mysql2": ("Database", "MySQL", "mysql2"),
        "mongodb": ("Database", "MongoDB", "mongodb"),
        "mongoose": ("Database", "MongoDB", "mongoose"),
        "sqlite3": ("Database", "SQLite", "sqlite3"),
        "better-sqlite3": ("Database", "SQLite", "better-sqlite3"),
        "neo4j-driver": ("Database", "Neo4j", "neo4j-driver"),
        "@prisma/client": ("Database", "Prisma", "prisma"),
        "redis": ("Cache", "Redis", "redis"),
        "ioredis": ("Cache", "Redis", "ioredis"),
        "kafkajs": ("Queue", "Kafka", "kafkajs"),
        "amqplib": ("Queue", "RabbitMQ", "amqplib"),
        "@aws-sdk/client-sqs": ("Queue", "SQS", "aws-sdk"),
        "@aws-sdk/client-s3": ("Database", "S3", "aws-sdk"),
    },
    "jvm": {
        "org.postgresql": ("Database", "PostgreSQL", "postgresql-jdbc"),
        "com.mongodb": ("Database", "MongoDB", "mongodb-driver"),
        "org.springframework.data.mongodb": ("Database", "MongoDB", "spring-data-mongodb"),
        "org.springframework.data.jpa": ("Database", "JPA", "spring-data-jpa"),
        "org.springframework.data.neo4j": ("Database", "Neo4j", "spring-data-neo4j"),
        "org.neo4j.driver": ("Database", "Neo4j", "neo4j-java-driver"),
        "redis.clients.jedis": ("Cache", "Redis", "jedis"),
        "io.lettuce.core": ("Cache", "Redis", "lettuce"),
        "org.springframework.data.redis": ("Cache", "Redis", "spring-data-redis"),
        "org.apache.kafka.clients": ("Queue", "Kafka", "kafka-clients"),
        "org.springframework.kafka": ("Queue", "Kafka", "spring-kafka"),
        "com.rabbitmq.client": ("Queue", "RabbitMQ", "amqp-client"),
        "org.springframework.amqp": ("Queue", "RabbitMQ", "spring-amqp"),
        "software.amazon.awssdk.services.sqs": ("Queue", "SQS", "aws-sdk"),
        "software.amazon.awssdk.services.s3": ("Database", "S3", "aws-sdk"),
    },
    "cs": {
        "Npgsql": ("Database", "PostgreSQL", "Npgsql"),
        "Microsoft.Data.SqlClient": ("Database", "SQL Server", "SqlClient"),
        "System.Data.SqlClient": ("Database", "SQL Server", "SqlClient"),
        "Microsoft.Data.Sqlite": ("Database", "SQLite", "Microsoft.Data.Sqlite"),
        "MySqlConnector": ("Database", "MySQL", "MySqlConnector"),
        "MySql.Data": ("Database", "MySQL", "MySql.Data"),
        "MongoDB.Driver": ("Database", "MongoDB", "MongoDB.Driver"),
        "StackExchange.Redis": ("Cache", "Redis", "StackExchange.Redis"),
        "Confluent.Kafka": ("Queue", "Kafka", "Confluent.Kafka"),
        "RabbitMQ.Client": ("Queue", "RabbitMQ", "RabbitMQ.Client"),
        "Amazon.SQS": ("Queue", "SQS", "aws-sdk"),
        "Amazon.S3": ("Database", "S3", "aws-sdk"),
    },
    "go": {
        "github.com/lib/pq": ("Database", "PostgreSQL", "pq"),
        "github.com/jackc/pgx": ("Database", "PostgreSQL", "pgx"),
        "github.com/go-sql-driver/mysql": ("Database", "MySQL", "go-sql-driver"),
        "github.com/mattn/go-sqlite3": ("Database", "SQLite", "go-sqlite3"),
        "modernc.org/sqlite": ("Database", "SQLite", "modernc-sqlite"),
        "go.mongodb.org/mongo-driver": ("Database", "MongoDB", "mongo-driver"),
        "github.com/redis/go-redis": ("Cache", "Redis", "go-redis"),
        "github.com/go-redis/redis": ("Cache", "Redis", "go-redis"),
        "github.com/IBM/sarama": ("Queue", "Kafka", "sarama"),
        "github.com/Shopify/sarama": ("Queue", "Kafka", "sarama"),
        "github.com/segmentio/kafka-go": ("Queue", "Kafka", "kafka-go"),
        "github.com/rabbitmq/amqp091-go": ("Queue", "RabbitMQ", "amqp091-go"),
        "github.com/aws/aws-sdk-go-v2/service/sqs": ("Queue", "SQS", "aws-sdk"),
        "github.com/aws/aws-sdk-go-v2/service/s3": ("Database", "S3", "aws-sdk"),
    },
}

#: A connection URL's scheme, at the start of a string literal: (type, provider).
CLIENT_URL_SCHEMES: dict[str, tuple[str, str]] = {
    "postgres": ("Database", "PostgreSQL"), "postgresql": ("Database", "PostgreSQL"),
    "mysql": ("Database", "MySQL"), "mongodb": ("Database", "MongoDB"), "mongodb+srv": ("Database", "MongoDB"),
    "redis": ("Cache", "Redis"), "rediss": ("Cache", "Redis"), "amqp": ("Queue", "RabbitMQ"),
    "amqps": ("Queue", "RabbitMQ"),
}
_URL_LITERAL = re.compile(r"""["'`](postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?|amqps?)://""")

#: A JDBC URL's subprotocol, at the start of a string literal (Java, Kotlin).
JDBC_PROVIDERS: dict[str, str] = {
    "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB", "sqlserver": "SQL Server",
    "sqlite": "SQLite", "oracle": "Oracle", "h2": "H2",
}
_JDBC_LITERAL = re.compile(r'"jdbc:(\w+):')
#: A Spring Data store's repository supertype (Java, Kotlin), `JpaRepository<...>`.
SPRING_DATA_REPOSITORIES: dict[str, tuple[str, str]] = {
    "JpaRepository": ("Database", "JPA"), "MongoRepository": ("Database", "MongoDB"),
    "ReactiveMongoRepository": ("Database", "MongoDB"), "Neo4jRepository": ("Database", "Neo4j"),
    "CassandraRepository": ("Database", "Cassandra"), "ElasticsearchRepository": ("Database", "Elasticsearch"),
}
_SPRING_REPOSITORY = re.compile(r"\b(" + "|".join(SPRING_DATA_REPOSITORIES) + r")\s*<")
#: An EF Core provider call (`options.UseNpgsql(...)`), and a DbContext subclass.
EF_CORE_PROVIDERS: dict[str, str] = {
    "UseNpgsql": "PostgreSQL", "UseSqlServer": "SQL Server", "UseSqlite": "SQLite", "UseMySql": "MySQL",
    "UseMySQL": "MySQL", "UseCosmos": "Cosmos DB", "UseOracle": "Oracle",
}
_EF_CORE_CALL = re.compile(r"\.(" + "|".join(EF_CORE_PROVIDERS) + r")\s*\(")
_DB_CONTEXT = re.compile(r"\bclass\s+\w+(?:<[^>]*>)?\s*(?:\([^)]*\))?\s*:\s*(?:[\w.]+\.)?DbContext\b")
#: Go's `sql.Open` driver names.
GO_SQL_DRIVERS: dict[str, str] = {
    "postgres": "PostgreSQL", "pgx": "PostgreSQL", "mysql": "MySQL", "sqlite3": "SQLite", "sqlite": "SQLite",
    "sqlserver": "SQL Server", "mssql": "SQL Server",
}
_GO_SQL_OPEN = re.compile(r'\bsql\.Open\(\s*"(\w+)"')

_JS_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|^\s*import\s+)(['"])([^'"\n]+)\1""", re.M)
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)
_CS_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", re.M)
_GO_IMPORT_BLOCK = re.compile(r"^import\s*\(([^)]*)\)", re.M)
_GO_IMPORT_LINE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_GO_IMPORT_SPEC = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)

#: The client family of each language extractor's route.
CLIENT_LANGUAGES = {"js": "js", "java": "jvm", "kt": "jvm", "cs": "cs", "go": "go"}


def _imported(language: str, content: str) -> list[str]:
    """What the file imports, as `CLIENT_IMPORTS` keys it."""
    if language == "js":
        return [match.group(2) for match in _JS_SPECIFIER.finditer(content)]
    if language == "jvm":
        return _JVM_IMPORT.findall(content)
    if language == "cs":
        return _CS_USING.findall(content)
    paths = _GO_IMPORT_LINE.findall(content)
    for block in _GO_IMPORT_BLOCK.findall(content):
        paths += _GO_IMPORT_SPEC.findall(block)
    return paths


def _import_client(language: str, imported: str) -> tuple[str, str, str] | None:
    """The client an import names: the longest `CLIENT_IMPORTS` key it is or is below."""
    separator = "." if language in ("jvm", "cs") else "/"
    clients = CLIENT_IMPORTS[language]
    parts = imported.split(separator)
    for end in range(len(parts), 0, -1):
        found = clients.get(separator.join(parts[:end]))
        if found is not None:
            return found
    return None


def client_datastores(content: str, language: str, repo_id: str, filename: str) -> list[DatastoreNode]:
    """The stores a non-Python source file's clients name (see CLIENT_IMPORTS),
    one node per provider, `language` an extractor route (`CLIENT_LANGUAGES`;
    another language has none). Its `library` is the alphabetically first of
    the file's clients of that provider."""
    family = CLIENT_LANGUAGES.get(language)
    if family is None:
        return []
    found: set[tuple[str, str, str]] = set()
    for imported in _imported(family, content):
        client = _import_client(family, imported)
        if client is not None:
            found.add(client)
    found |= {(*CLIENT_URL_SCHEMES[scheme], "url") for scheme in _URL_LITERAL.findall(content)}
    if family == "jvm":
        found |= {("Database", JDBC_PROVIDERS[sub], "jdbc") for sub in _JDBC_LITERAL.findall(content)
                  if sub in JDBC_PROVIDERS}
        found |= {(*SPRING_DATA_REPOSITORIES[base], "spring-data") for base in _SPRING_REPOSITORY.findall(content)}
    elif family == "cs":
        found |= {("Database", EF_CORE_PROVIDERS[call], "EF Core") for call in _EF_CORE_CALL.findall(content)}
        if _DB_CONTEXT.search(content):
            found.add(("Database", "EF Core", "EF Core"))
    elif family == "go":
        found |= {("Database", GO_SQL_DRIVERS[driver], "database/sql") for driver in _GO_SQL_OPEN.findall(content)
                  if driver in GO_SQL_DRIVERS}
    by_provider: dict[tuple[str, str], str] = {}
    for ds_type, provider, library in sorted(found):
        by_provider.setdefault((ds_type, provider), library)
    return [
        DatastoreNode(
            name=provider, datastore_type=ds_type, provider=provider, repo_id=repo_id,
            properties={"library": library, "source": filename},
        )
        for (ds_type, provider), library in sorted(by_provider.items(), key=lambda item: item[0][1])
    ]
