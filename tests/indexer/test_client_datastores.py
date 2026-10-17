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
        "const url = `postgres://db:5432/shop`;\n",
        "js",
        [("Cache", "Redis", "redis"), ("Database", "MongoDB", "mongodb"), ("Database", "PostgreSQL", "url")],
    ),
    (
        "package shop;\n\nimport redis.clients.jedis.Jedis;\nimport org.apache.kafka.clients.producer.KafkaProducer;\n"
        "interface Orders extends MongoRepository<Order, String> {}\n"
        'class Db { String url = "jdbc:postgresql://db:5432/shop"; }\n',
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
    # C++ and Rust clients are not read.
    ('#include <pqxx/pqxx>\nauto url = "postgres://db";\n', "cpp"),
])
def test_nothing_built_at_run_time_or_not_a_client(content, language):
    assert found(content, language) == []


def test_one_node_per_provider_with_the_first_library():
    content = "import Redis from 'ioredis';\nimport { createClient } from 'redis';\nconst u = 'redis://cache';\n"
    assert found(content, "js") == [("Cache", "Redis", "ioredis")]
