"""`yaml.safe_load` with bounds on alias expansion and nesting, for untrusted config YAML.

`yaml.safe_load` builds aliases as shared references, so a "billion laughs"
document loads cheaply -- and then hangs whatever walks or compares the result.
`bounded_safe_load` measures the alias-expanded size and depth on the composed
node graph first and refuses an oversized document before anything is
constructed. Nesting is bounded while composing, so a deeply nested document
never reaches Python's recursion limit (nor a JSON serialiser's depth limit
once it is returned to a browser), and a constructor error (`2020-02-30`,
`!!bool maybe`) is reported as a YAML error like any other malformed input.
"""

from __future__ import annotations

from typing import Any

import yaml

# Most nodes a config document may expand to once every alias is followed.
YAML_MAX_NODES = 10_000
# Most levels a config document may nest, aliases followed; the root is level 1.
YAML_MAX_DEPTH = 64

# What a SafeLoader constructor raises on a well-formed but unconstructible value.
_CONSTRUCTOR_ERRORS = (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError)


class YAMLBoundError(yaml.YAMLError):
    """A document is too large or too deep, an alias refers to itself, or a value can't be constructed."""


def _too_deep() -> YAMLBoundError:
    return YAMLBoundError(f"the document nests more than {YAML_MAX_DEPTH} levels deep")


class _DepthBoundLoader(yaml.SafeLoader):
    """A SafeLoader that refuses to compose a node nested deeper than `YAML_MAX_DEPTH`."""

    _depth = 0

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node:
        if self._depth >= YAML_MAX_DEPTH:
            raise _too_deep()
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def bounded_safe_load(text: str, max_nodes: int = YAML_MAX_NODES) -> Any:
    """`yaml.safe_load`, refusing a document that is too large, too deep or unconstructible.

    Nesting is bounded at `YAML_MAX_DEPTH` while composing. The alias-expanded size
    and depth are then measured on the composed node graph before anything is
    constructed, counting an aliased node once per reference and memoising per node,
    so a billion-laughs document is measured without being built. A recursive alias
    is infinitely large and refused. A constructor error is re-raised as a
    `YAMLBoundError`, so every refusal is a `yaml.YAMLError`.
    """
    loader = _DepthBoundLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_expanded_size(node, max_nodes)
        try:
            return loader.construct_document(node)
        except _CONSTRUCTOR_ERRORS as exc:
            raise YAMLBoundError(f"a value cannot be constructed: {exc}") from exc
    finally:
        loader.dispose()


def _check_expanded_size(root: yaml.Node, max_nodes: int) -> None:
    """Refuse a node graph over `max_nodes` or `YAML_MAX_DEPTH` once every alias is followed."""
    sizes: dict[int, tuple[int, int]] = {}  # node id -> (expanded size, expanded height)
    in_progress: set[int] = set()

    def size(node: yaml.Node, level: int) -> tuple[int, int]:
        known = sizes.get(id(node))
        if known is not None:
            if level - 1 + known[1] > YAML_MAX_DEPTH:
                raise _too_deep()
            return known
        if level > YAML_MAX_DEPTH:
            raise _too_deep()
        if id(node) in in_progress:
            raise YAMLBoundError("a YAML alias refers to itself; the document has no finite expansion")
        in_progress.add(id(node))
        if isinstance(node, yaml.MappingNode):
            children = [child for pair in node.value for child in pair]
        elif isinstance(node, yaml.SequenceNode):
            children = node.value
        else:
            children = []
        total, height = 1, 1
        for child in children:
            child_size, child_height = size(child, level + 1)
            total += child_size
            height = max(height, child_height + 1)
            if total > max_nodes:
                raise YAMLBoundError(f"the document expands to more than {max_nodes} YAML nodes")
        in_progress.discard(id(node))
        sizes[id(node)] = (total, height)
        return total, height

    size(root, 1)
