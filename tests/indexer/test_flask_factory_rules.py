"""A Flask app factory's `add_url_rule` view defined in the factory itself
(apis/extractor.py `_rule_views`): the route implements that nested
function of its file, never every function of its name in the repository.
Fixture code is fictional."""

import textwrap

from devgraph.indexer.apis.extractor import APIExtractor


def _targets(code, filename="shop/app.py"):
    result = APIExtractor("repo").extract_from_source(textwrap.dedent(code), filename)
    return sorted((r.source_name, r.target_name, r.to_file, r.to_owner) for r in result.relationships)


def test_a_view_defined_in_the_factory_is_pinned_to_it():
    assert _targets("""\
        from flask import Flask

        def create_app():
            app = Flask(__name__)

            def index():
                return "home"

            app.add_url_rule("/", view_func=index)
            return app
    """) == [("GET /", "index", "", None), ("GET /", "index", "shop/app.py", "create_app")]


def test_a_module_function_used_in_the_factory_and_a_nested_scope():
    assert _targets("""\
        from flask import Flask

        def health():
            return "ok"

        def create_app():
            app = Flask(__name__)

            def register():
                def status():
                    return "up"

                app.add_url_rule("/status", view_func=status)
                app.add_url_rule("/health", view_func=health)

            register()
            return app
    """) == [
        ("GET /health", "health", "", None), ("GET /health", "health", "shop/app.py", ""),
        ("GET /status", "status", "", None), ("GET /status", "status", "shop/app.py", "create_app.register"),
    ]
