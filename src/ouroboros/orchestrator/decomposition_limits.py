"""Single-source live decomposition and durable replay limits.

The historical live input contract accepts any non-negative depth. Routing D
adds stronger crash replay only for the bounded subset whose worst-case complete
tree can be represented by its durable projections. Keep that boundary explicit
so compatibility and replay claims cannot drift apart.
"""

from __future__ import annotations

from typing import cast

from ouroboros.orchestrator.decomposition_policy import MAX_CHILDREN, MIN_CHILDREN

DEFAULT_MAX_DECOMPOSITION_DEPTH = 2
MAX_DURABLE_DECOMPOSITION_DEPTH = 4
# Back-compatible name for callers that need the strongest durable depth. It is
# not a maximum for the historical live decomposition input contract.
MAX_DECOMPOSITION_DEPTH = MAX_DURABLE_DECOMPOSITION_DEPTH
MIN_DECOMPOSITION_CHILDREN = MIN_CHILDREN
MAX_DECOMPOSITION_CHILDREN = MAX_CHILDREN


def _complete_decomposition_child_capacity(max_depth: int) -> int:
    """Return the largest complete child tree for an admitted live depth."""

    return sum(MAX_DECOMPOSITION_CHILDREN**depth for depth in range(1, max_depth + 1))


# A depth-four five-way tree contains 5 + 25 + 125 + 625 child nodes.  Durable
# completion and pause projections exclude the top-level root, so 780 is the
# exact population that every accepted live override must be able to replay.
MAX_DECOMPOSITION_REPLAY_NODES = _complete_decomposition_child_capacity(
    MAX_DURABLE_DECOMPOSITION_DEPTH
)


def validate_max_decomposition_depth(
    value: object,
    *,
    source: str = "max_decomposition_depth",
) -> int:
    """Validate the shared live-depth contract at any public entry point."""

    error = f"{source} must be a non-negative integer"
    if type(value) is not int:
        raise ValueError(error)
    depth = cast(int, value)
    if depth < 0:
        raise ValueError(error)
    return depth


def has_durable_decomposition_replay(depth: int) -> bool:
    """Return whether Routing D can own crash replay for this live depth."""

    return 0 <= depth <= MAX_DURABLE_DECOMPOSITION_DEPTH
