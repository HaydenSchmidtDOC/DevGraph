"""Python datastores are read from the syntax tree (datastores/extractor.py
`DatastoreExtractor`): a real import of a client module (the module itself
or one below it, never a look-alike name), a known client constructor on a
real call, and a connection URL passed straight to a call. A comment, a
docstring, any other string and a file under a test folder name none.
Fixture code is fictional."""

import textwrap

from devgraph.indexer.datastores.extractor import DatastoreExtractor


def providers(source, filename="shop/db.py"):
    result = DatastoreExtractor("repo").extract_from_source(textwrap.dedent(source), filename)
    return sorted(ds.provider for ds in result.datastores)


def test_a_look_alike_module_is_not_its_store():
    source = '''
        """We import redistricting data."""
        from redistricting import plan
        import kafkaesque
        import pikachu, duckdbx
    '''
    assert providers(source) == []


def test_comments_docstrings_and_strings_name_no_store():
    source = '''
        """Example:

            import pymongo
            conn = psycopg2.connect("postgresql://x")
        """
        # TODO: import kafka
        HELP = "connect with mysql://host"
        NOTE = "redis.Redis() is not called here"

        def f():
            """from neo4j import GraphDatabase"""
            return HELP
    '''
    assert providers(source) == []


def test_a_url_held_in_a_variable_is_not_read_but_one_passed_to_a_call_is():
    held = 'DSN = "postgresql://shop@db/orders"\n'
    assert providers(held) == []
    passed = '''
        engine = create_engine("postgresql://shop@db/orders")
        broker = Broker(url="amqp://guest@mq//")
    '''
    assert providers(passed) == ["PostgreSQL", "RabbitMQ"]


def test_a_test_folder_names_no_store():
    source = "import sqlite3\nconn = sqlite3.connect(':memory:')\n"
    assert providers(source, "tests/test_x.py") == []
    assert providers(source, "shop/tests/helpers.py") == []
    assert providers(source, "shop/store.py") == ["SQLite"]


def test_real_imports_name_their_stores():
    source = """
        import redis.asyncio as aioredis
        from qdrant_client.models import Distance
        import psycopg2.extras, os
        from pymongo import MongoClient

        def lazy():
            import duckdb
            return duckdb
    """
    assert providers(source) == ["DuckDB", "MongoDB", "PostgreSQL", "Qdrant", "Redis"]


def test_a_relative_import_is_not_a_client():
    source = "from .redis import cache\nfrom . import kafka\n"
    assert providers(source) == []


def test_a_known_constructor_call_names_its_store():
    source = """
        conn = psycopg2.connect(dsn)
        client = pymysql.connect(host="db")
    """
    assert providers(source) == ["MySQL", "PostgreSQL"]


def test_one_node_per_provider():
    source = "import psycopg2\nimport psycopg\npsycopg.connect('postgresql://x')\n"
    result = DatastoreExtractor("repo").extract_from_source(source, "shop/db.py")
    assert [(ds.provider, ds.properties["library"]) for ds in result.datastores] == [("PostgreSQL", "psycopg")]


def test_an_f_string_url_counts_by_its_literal_start():
    source = '''
        engine = create_engine(f"postgresql://{user}@{host}/db")
        other = connect(f"{scheme}://{host}")
    '''
    assert providers(source) == ["PostgreSQL"]


def test_a_sqlalchemy_dialect_driver_url_names_its_store():
    source = '''
        a = create_engine("mysql+pymysql://shop@db/orders")
        b = create_engine("postgresql+asyncpg://shop@db/orders")
        c = MongoClient("mongodb+srv://cluster/x")
    '''
    assert providers(source) == ["MongoDB", "MySQL", "PostgreSQL"]


def test_a_dialect_driver_url_in_another_language():
    from devgraph.indexer.datastores.extractor import client_datastores

    source = "const db = connect('postgresql+psycopg2://shop@db/orders');\n"
    assert [ds.provider for ds in client_datastores(source, "js", "repo", "src/db.js")] == ["PostgreSQL"]
