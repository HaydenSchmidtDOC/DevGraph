"""NestJS controllers' routes (jsts/extractor.py `_nest_routes`): a
`@Controller` class's prefix joined to each `@Get`/`@Post`/... method's
path, the Endpoint implementing that method of the class. Fixture code is
fictional."""

import textwrap

from devgraph.indexer.jsts.extractor import extract_js_file


def _routes(code, file_path="src/users.controller.ts"):
    result = extract_js_file(textwrap.dedent(code), file_path, "repo")
    endpoints = sorted(n.name for n in result.nodes if n.label == "Endpoint")
    implements = sorted(
        (r.from_name, r.to_name, r.to_file, r.to_owner)
        for r in result.relationships if r.rel_type == "IMPLEMENTS" and r.to_file
    )
    frameworks = {n.properties["framework"] for n in result.nodes if n.label == "Endpoint"}
    return endpoints, implements, frameworks


def test_a_controller_prefix_and_its_method_routes():
    endpoints, implements, frameworks = _routes("""\
        import { Controller, Get, Post, Delete, All, Param } from '@nestjs/common';

        @Controller('users')
        export class UsersController {
          @Get(':id')
          findOne(@Param('id') id: string) {}

          @Post()
          create() {}

          @Delete('/:id/')
          remove() {}

          @All('any')
          anything() {}

          helper() {}
        }
    """)
    assert endpoints == ["* /users/any", "DELETE /users/:id", "GET /users/:id", "POST /users"]
    path = "src/users.controller.ts"
    assert implements == [
        ("* /users/any", "anything", path, "UsersController"),
        ("DELETE /users/:id", "remove", path, "UsersController"),
        ("GET /users/:id", "findOne", path, "UsersController"),
        ("POST /users", "create", path, "UsersController"),
    ]
    assert frameworks == {"NestJS"}


def test_a_controller_without_a_prefix_an_object_path_and_path_lists():
    endpoints, implements, _ = _routes("""\
        import { Controller, Get, Put, Patch } from '@nestjs/common';

        @Controller()
        class HealthController {
          @Get()
          check() {}
        }

        @Controller({ path: 'admin', host: 'admin.example.com' })
        class AdminController {
          @Put(['a', 'b'])
          both() {}

          @Patch(`settings`)
          settings() {}
        }

        @Controller(['v1/items', 'v2/items'])
        class ItemsController {
          @Get(':id')
          one() {}
        }
    """)
    assert endpoints == [
        "GET /", "GET /v1/items/:id", "GET /v2/items/:id",
        "PATCH /admin/settings", "PUT /admin/a", "PUT /admin/b",
    ]
    assert ("GET /", "check", "src/users.controller.ts", "HealthController") in implements


def test_a_dynamic_path_or_a_file_without_nest_is_no_route():
    endpoints, _, _ = _routes("""\
        import { Controller, Get } from '@nestjs/common';

        const PREFIX = 'orders';

        @Controller(PREFIX)
        class OrdersController {
          @Get()
          list() {}
        }

        @Controller('carts')
        class CartsController {
          @Get(ROUTE)
          list() {}

          @Get('mine')
          mine() {}
        }
    """)
    assert endpoints == ["GET /carts/mine"]
    # The same decorators from anything but @nestjs/common are not Nest's.
    endpoints, _, _ = _routes("""\
        import { Controller, Get } from './decorators';

        @Controller('users')
        class UsersController {
          @Get(':id')
          findOne() {}
        }
    """)
    assert endpoints == []


def test_an_aliased_import_and_a_default_export():
    endpoints, implements, _ = _routes("""\
        import { Controller as Ctl, Get as Read } from '@nestjs/common';

        @Ctl('books')
        export default class BooksController {
          @Read()
          list() {}
        }
    """)
    assert endpoints == ["GET /books"]
    assert implements == [("GET /books", "list", "src/users.controller.ts", "BooksController")]
