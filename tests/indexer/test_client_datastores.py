"""Other languages' datastore clients (datastores/extractor.py
`client_datastores`): literal imports, connection URLs and the calls and
supertypes that name a store, never a value built at run time. Fixture code
is fictional."""

import pytest

from devgraph.indexer.datastores.extractor import client_datastores


def found(content, language):
    return sorted(
        (ds.datastore_type, ds.name, ds.properties["library"])
        for ds in client_datastores(content, language, "r", "f")
    )


@pytest.mark.parametrize("content, language, expected", [
    (
        "import { Pool } from 'pg';\nimport Redis from 'ioredis';\nconst { Kafka } = require('kafkajs');\n"
        "import mysql from 'mysql2/promise';\nimport { S3Client } from '@aws-sdk/client-s3';\n"
        "import { SQSClient } from '@aws-sdk/client-sqs';\nimport amqp from 'amqplib';\n"
        "import mongoose from 'mongoose';\nimport { PrismaClient } from '@prisma/client';\n",
        "js",
        [("Cache", "Redis", "ioredis"), ("Database", "MongoDB", "mongoose"), ("Database", "MySQL", "mysql2"),
         ("Database", "PostgreSQL", "pg"), ("Database", "Prisma", "prisma"), ("Database", "S3", "aws-sdk"),
         ("Queue", "Kafka", "kafkajs"), ("Queue", "RabbitMQ", "amqplib"), ("Queue", "SQS", "aws-sdk")],
    ),
    # A dynamic import, a side-effect import and a URL literal; a lookalike package is no client.
    (
        "const m = await import('mongodb');\nimport 'redis';\nimport pgx from 'pg-extras-lookalike';\n"
        "const pool = new Pool(`postgres://db:5432/shop`);\n",
        "js",
        [("Cache", "Redis", "redis"), ("Database", "MongoDB", "mongodb"), ("Database", "PostgreSQL", "url")],
    ),
    (
        "package shop;\n\nimport redis.clients.jedis.Jedis;\nimport org.apache.kafka.clients.producer.KafkaProducer;\n"
        "interface Orders extends MongoRepository<Order, String> {}\n"
        'class Db { Connection c = DriverManager.getConnection("jdbc:postgresql://db:5432/shop", user); }\n',
        "java",
        [("Cache", "Redis", "jedis"), ("Database", "MongoDB", "spring-data"), ("Database", "PostgreSQL", "jdbc"),
         ("Queue", "Kafka", "kafka-clients")],
    ),
    (
        "package shop\n\nimport io.lettuce.core.RedisClient\n\ninterface Users : JpaRepository<User, Long>\n",
        "kt",
        [("Cache", "Redis", "lettuce"), ("Database", "JPA", "spring-data")],
    ),
    (
        "using Npgsql;\nusing StackExchange.Redis;\nusing Db = MongoDB.Driver;\n"
        "public class ShopContext : DbContext {}\n"
        "class Startup { void Configure(DbContextOptionsBuilder o) { o.UseSqlServer(conn); } }\n",
        "cs",
        [("Cache", "Redis", "StackExchange.Redis"), ("Database", "EF Core", "EF Core"),
         ("Database", "MongoDB", "MongoDB.Driver"), ("Database", "PostgreSQL", "Npgsql"),
         ("Database", "SQL Server", "EF Core")],
    ),
    (
        'package shop\n\nimport (\n\t"database/sql"\n\t"github.com/jackc/pgx/v5/pgxpool"\n'
        '\tredis "github.com/redis/go-redis/v9"\n\t"github.com/segmentio/kafka-go"\n)\n\n'
        'func open() { sql.Open("mysql", dsn) }\n',
        "go",
        [("Cache", "Redis", "go-redis"), ("Database", "MySQL", "database/sql"), ("Database", "PostgreSQL", "pgx"),
         ("Queue", "Kafka", "kafka-go")],
    ),
    ('package shop\n\nimport "go.mongodb.org/mongo-driver/mongo"\n', "go", [("Database", "MongoDB", "mongo-driver")]),
])
def test_clients_by_language(content, language, expected):
    assert found(content, language) == expected


@pytest.mark.parametrize("content, language", [
    # A URL, driver or namespace built at run time, a mere mention in a
    # comment's text, and a namespace merely named like a client.
    ("const url = process.env.DATABASE_URL;\n// talks to postgres://\n", "js"),
    ('package shop\n\nfunc open() { sql.Open(driver, dsn) }\n', "go"),
    ("using NpgsqlTypes;\nusing Shop.Redis;\n", "cs"),
    ("import shop.redis.Cache;\n", "java"),
    # A URL held in a variable rather than passed to a call.
    ("const url = 'postgres://db/shop';\nconst pool = new Pool(url);\n", "js"),
    ('class Db { String url = "jdbc:postgresql://db:5432/shop"; }\n', "java"),
    # C++ and Rust clients are not read.
    ('#include <pqxx/pqxx>\nauto url = "postgres://db";\n', "cpp"),
])
def test_nothing_built_at_run_time_or_not_a_client(content, language):
    assert found(content, language) == []


def test_one_node_per_provider_with_the_first_library():
    content = "import Redis from 'ioredis';\nimport { createClient } from 'redis';\nconst u = 'redis://cache';\n"
    assert found(content, "js") == [("Cache", "Redis", "ioredis")]


@pytest.mark.parametrize("content, language", [
    # Type-only imports bring in no client.
    ("import type { Pool } from 'pg';\nexport type { Redis } from 'ioredis';\n", "js"),
    # Imports, URLs, repositories and EF Core calls inside comments.
    ("// import { Pool } from 'pg';\n/* const r = require('ioredis'); connect('redis://x') */\n", "js"),
    ("// import redis.clients.jedis.Jedis;\n/* interface A extends JpaRepository<A, Long> {} */\n", "java"),
    ("// o.UseSqlServer(conn);\n/* public class C : DbContext {} */\n", "cs"),
    ('package shop\n\n// import "github.com/lib/pq"\nfunc f() { /* sql.Open("mysql", d) */ }\n', "go"),
    # Import-looking text and URLs inside a string that is no specifier or call argument.
    ("const help = \"import { Pool } from 'pg'\";\nconst doc = 'see redis://cache';\n", "js"),
    ('String doc = "import redis.clients.jedis.Jedis;";\n', "java"),
    # A repository type named as a field or parameter, not a supertype.
    ("class Service { private JpaRepository<User, Long> users; }\n", "java"),
    ("class Service(val users: List<MongoRepository<User, String>>)\n", "kt"),
])
def test_text_that_is_no_client_use(content, language):
    assert found(content, language) == []


def test_spring_repository_after_other_supertypes():
    content = "interface Users extends Base<User>, JpaRepository<User, Long> {}\n"
    assert found(content, "java") == [("Database", "JPA", "spring-data")]


@pytest.mark.parametrize("path", [
    "test/db.ts", "web/tests/db.ts", "web/__tests__/db.ts", "src/test/java/Db.java", "Shop.Tests/Db.cs",
])
def test_files_under_test_folders_name_no_store(path):
    content = "import { Pool } from 'pg';\nusing Npgsql;\nimport org.postgresql.Driver;\n"
    language = {"ts": "js", "java": "java", "cs": "cs"}[path.rsplit(".", 1)[1]]
    assert client_datastores(content, language, "r", path) == []
    assert client_datastores(content, language, "r", path.replace("test", "app").replace("Tests", "App")) != []
