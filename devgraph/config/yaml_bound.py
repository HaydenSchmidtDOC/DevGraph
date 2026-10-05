"""`yaml.safe_load` with a bound on alias expansion, for untrusted config YAML.

`yaml.safe_load` builds aliases as shared references, so a "billion laughs"
document loads cheaply -- and then hangs whatever walks or compares the result.
`bounded_safe_load` measures the alias-expanded size on the composed node graph
first and refuses an oversized document before anything is constructed.
"""

from __future__ import annotations

from typing import Any

import yaml

# Most nodes a config document may expand to once every alias is followed.
YAML_MAX_NODES = 10_000


class YAMLBoundError(yaml.YAMLError):
    """A document is too large once its aliases are expanded, or an alias refers to itself."""


def bounded_safe_load(text: str, max_nodes: int = YAML_MAX_NODES) -> Any:
    """`yaml.safe_load`, refusing a document whose alias-expanded size exceeds `max_nodes`.

    The size is measured on the composed node graph before anything is constructed,
    counting an aliased node once per reference and memoising per node, so a
    billion-laughs document is measured without being built. A recursive alias is
    infinitely large and refused. Every refusal is a `yaml.YAMLError`.
    """
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_expanded_size(node, max_nodes)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _check_expanded_size(root: yaml.Node, max_nodes: int) -> None:
    sizes: dict[int, int] = {}
    in_progress: set[int] = set()

    def size(node: yaml.Node) -> int:
        known = sizes.get(id(node))
        if known is not None:
            return known
        if id(node) in in_progress:
            raise YAMLBoundError("a YAML alias refers to itself; the document has no finite expansion")
        in_progress.add(id(node))
        if isinstance(node, yaml.MappingNode):
            children = [child for pair in node.value for child in pair]
        elif isinstance(node, yaml.SequenceNode):
            children = node.value
        else:
            children = []
        total = 1
        for child in children:
            total += size(child)
            if total > max_nodes:
                raise YAMLBoundError(f"the document expands to more than {max_nodes} YAML nodes")
        in_progress.discard(id(node))
        sizes[id(node)] = total
        return total

    size(root)
