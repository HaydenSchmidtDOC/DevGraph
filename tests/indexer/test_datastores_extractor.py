"""Unit tests for datastore extractor."""

import pytest

from devgraph.indexer.datastores.extractor import DatastoreExtractor


class TestDatastoreExtractor:
    """Tests for DatastoreExtractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.repo_id = "test-repo"
        self.extractor = DatastoreExtractor(self.repo_id)

    def test_extract_postgresql_import(self):
        """Test detection of PostgreSQL via psycopg2 import."""
        code = """
import psycopg2

conn = psycopg2.connect(
    host="localhost",
    database="mydb",
    user="user",
    password="password"
)
"""
        result = self.extractor.extract_from_source(code, "database.py")

        assert len(result.datastores) >= 1
        postgres_ds = next(
            (d for d in result.datastores if d.provider == "PostgreSQL"), None
        )
        assert postgres_ds is not None
        assert postgres_ds.datastore_type == "Database"
        # Library can be either psycopg2 or psycopg (v3)
        assert postgres_ds.properties.get("library") in ["psycopg2", "psycopg"]

    def test_extract_redis_import(self):
        """Test detection of Redis via redis-py import."""
        code = """
import redis

cache = redis.Redis(host='localhost', port=6379, db=0)
cache.set('key', 'value')
"""
        result = self.extractor.extract_from_source(code, "cache.py")

        assert len(result.datastores) >= 1
        redis_ds = next((d for d in result.datastores if d.provider == "Redis"), None)
        assert redis_ds is not None
        assert redis_ds.datastore_type == "Cache"

    def test_extract_neo4j_driver(self):
        """Test detection of Neo4j driver."""
        code = """
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("user", "pass"))
"""
        result = self.extractor.extract_from_source(code, "graph.py")

        assert len(result.datastores) >= 1
        neo4j_ds = next((d for d in result.datastores if d.provider == "Neo4j"), None)
        assert neo4j_ds is not None
        assert neo4j_ds.datastore_type == "Database"

    def test_extract_qdrant_client(self):
        """Test detection of Qdrant vector store."""
        code = """
from qdrant_client import QdrantClient

client = QdrantClient(host="localhost", port=6333)
"""
        result = self.extractor.extract_from_source(code, "vectors.py")

        assert len(result.datastores) >= 1
        qdrant_ds = next(
            (d for d in result.datastores if d.provider == "Qdrant"), None
        )
        assert qdrant_ds is not None
        assert qdrant_ds.datastore_type == "VectorStore"

    def test_extract_kafka_producer(self):
        """Test detection of Kafka."""
        code = """
from kafka import KafkaProducer, KafkaConsumer

producer = KafkaProducer(bootstrap_servers=['localhost:9092'])
"""
        result = self.extractor.extract_from_source(code, "messaging.py")

        assert len(result.datastores) >= 1
        kafka_ds = next((d for d in result.datastores if d.provider == "Kafka"), None)
        assert kafka_ds is not None
        assert kafka_ds.datastore_type == "Queue"

    def test_extract_rabbitmq_pika(self):
        """Test detection of RabbitMQ via pika."""
        code = """
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()
"""
        result = self.extractor.extract_from_source(code, "queue.py")

        assert len(result.datastores) >= 1
        rabbit_ds = next(
            (d for d in result.datastores if d.provider == "RabbitMQ"), None
        )
        assert rabbit_ds is not None
        assert rabbit_ds.datastore_type == "Queue"

    def test_extract_rabbitmq_kombu(self):
        """Test detection of RabbitMQ via kombu."""
        code = """
from kombu import Connection, Queue

conn = Connection('amqp://guest:guest@localhost//')
q = Queue('myqueue', exchange=exchange, routing_key='key')
"""
        result = self.extractor.extract_from_source(code, "queue.py")

        assert len(result.datastores) >= 1
        rabbit_ds = next(
            (d for d in result.datastores if d.provider == "RabbitMQ"), None
        )
        assert rabbit_ds is not None

    def test_extract_mongodb(self):
        """Test detection of MongoDB via pymongo."""
        code = """
from pymongo import MongoClient

client = MongoClient('mongodb://localhost:27017/')
db = client['mydb']
"""
        result = self.extractor.extract_from_source(code, "database.py")

        assert len(result.datastores) >= 1
        mongo_ds = next(
            (d for d in result.datastores if d.provider == "MongoDB"), None
        )
        assert mongo_ds is not None
        assert mongo_ds.datastore_type == "Database"

    def test_extract_duckdb(self):
        """Test detection of DuckDB."""
        code = """
import duckdb

conn = duckdb.connect('my_database.duckdb')
result = conn.sql("SELECT * FROM table")
"""
        result = self.extractor.extract_from_source(code, "database.py")

        assert len(result.datastores) >= 1
        duckdb_ds = next((d for d in result.datastores if d.provider == "DuckDB"), None)
        assert duckdb_ds is not None
        assert duckdb_ds.datastore_type == "Database"

    def test_extract_sqlite(self):
        """Test detection of SQLite."""
        code = """
import sqlite3

conn = sqlite3.connect(':memory:')
cursor = conn.cursor()
"""
        result = self.extractor.extract_from_source(code, "database.py")

        assert len(result.datastores) >= 1
        sqlite_ds = next((d for d in result.datastores if d.provider == "SQLite"), None)
        assert sqlite_ds is not None
        assert sqlite_ds.datastore_type == "Database"

    def test_extract_weaviate(self):
        """Test detection of Weaviate vector store."""
        code = """
import weaviate

client = weaviate.Client("http://localhost:8080")
"""
        result = self.extractor.extract_from_source(code, "vectors.py")

        assert len(result.datastores) >= 1
        weaviate_ds = next(
            (d for d in result.datastores if d.provider == "Weaviate"), None
        )
        assert weaviate_ds is not None
        assert weaviate_ds.datastore_type == "VectorStore"

    def test_extract_pinecone(self):
        """Test detection of Pinecone vector store."""
        code = """
import pinecone

pinecone.init(api_key="key")
index = pinecone.Index("index-name")
"""
        result = self.extractor.extract_from_source(code, "vectors.py")

        assert len(result.datastores) >= 1
        pinecone_ds = next(
            (d for d in result.datastores if d.provider == "Pinecone"), None
        )
        assert pinecone_ds is not None
        assert pinecone_ds.datastore_type == "VectorStore"

    def test_extract_multiple_datastores(self):
        """Test extraction of multiple datastores in single source."""
        code = """
import psycopg2
import redis
from qdrant_client import QdrantClient

# Database
db = psycopg2.connect()

# Cache
cache = redis.Redis()

# Vector store
vectors = QdrantClient()
"""
        result = self.extractor.extract_from_source(code, "services.py")

        assert len(result.datastores) >= 3
        providers = {d.provider for d in result.datastores}
        assert "PostgreSQL" in providers
        assert "Redis" in providers
        assert "Qdrant" in providers

    def test_repo_id_scoping(self):
        """Test that all extracted datastores have correct repo_id."""
        code = """
import redis
import psycopg2
"""
        result = self.extractor.extract_from_source(code)

        for datastore in result.datastores:
            assert datastore.repo_id == self.repo_id

    def test_source_file_tracking(self):
        """Test that extracted datastores track their source file."""
        code = "import redis"
        result = self.extractor.extract_from_source(code, "cache_module.py")

        if result.datastores:
            for ds in result.datastores:
                assert ds.properties.get("source") == "cache_module.py"

    def test_empty_source(self):
        """Test handling of empty source code."""
        result = self.extractor.extract_from_source("")

        assert len(result.datastores) == 0

    def test_source_without_datastores(self):
        """Test handling of source with no datastore usage."""
        code = """
def helper_function():
    return "Not related to datastores"

class MyClass:
    def method(self):
        pass
"""
        result = self.extractor.extract_from_source(code)

        assert len(result.datastores) == 0

    def test_postgresql_connection_string(self):
        """Test detection via PostgreSQL connection string."""
        code = """
connection_string = "postgresql://user:pass@localhost/mydb"
conn = psycopg2.connect(connection_string)
"""
        result = self.extractor.extract_from_source(code, "db.py")

        postgres_ds = next(
            (d for d in result.datastores if d.provider == "PostgreSQL"), None
        )
        assert postgres_ds is not None

    def test_valkey_redis_compatible(self):
        """Test detection of Valkey (Redis-compatible)."""
        code = """
import valkey

v = valkey.Valkey(host='localhost', port=6379)
"""
        result = self.extractor.extract_from_source(code, "cache.py")

        valkey_ds = next((d for d in result.datastores if d.provider == "Valkey"), None)
        assert valkey_ds is not None
        assert valkey_ds.datastore_type == "Cache"

    def test_from_import_extraction(self):
        """Test extraction from 'from X import Y' statements."""
        code = """
from redis import Redis, StrictRedis
from neo4j import GraphDatabase
from kafka import KafkaProducer
"""
        result = self.extractor.extract_from_source(code, "imports.py")

        providers = {d.provider for d in result.datastores}
        assert "Redis" in providers
        assert "Neo4j" in providers
        assert "Kafka" in providers

    def test_library_metadata(self):
        """Test that extracted datastores have correct library information."""
        code = "from qdrant_client import QdrantClient"
        result = self.extractor.extract_from_source(code, "vec.py")

        if result.datastores:
            qdrant = next((d for d in result.datastores if d.provider == "Qdrant"), None)
            if qdrant:
                assert qdrant.properties.get("library") == "qdrant-client"

    def test_datastore_type_consistency(self):
        """Test that datastore types match expected schema labels."""
        from devgraph.graph.schema import NODE_LABELS

        valid_types = {"Database", "VectorStore", "Queue", "Cache"}
        assert {"Database", "VectorStore", "Queue"} <= set(NODE_LABELS)

        code = """
import psycopg2
import redis
from qdrant_client import QdrantClient
from kafka import KafkaProducer
"""
        result = self.extractor.extract_from_source(code)

        for datastore in result.datastores:
            # Cache is a subtype; note that the extractor may use it
            # but the graph schema only has Database, VectorStore, Queue
            assert datastore.datastore_type in valid_types or datastore.datastore_type == "Cache"
