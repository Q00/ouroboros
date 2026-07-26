"""Single-source live decomposition and durable replay limits.

Live decomposition is admitted only when its worst-case complete child tree can
be represented by the durable completion and pause projections.  Keep the
public input range and the replay node envelope derived from the same constants
so CLI, Seed, runner, executor, persistence, and replay cannot drift apart.
"""

from __future__ import annotations

from typing import cast

from ouroboros.orchestrator.decomposition_policy import MAX_CHILDREN, MIN_CHILDREN

DEFAULT_MAX_DECOMPOSITION_DEPTH = 2
MAX_DECOMPOSITION_DEPTH = 4
MIN_DECOMPOSITION_CHILDREN = MIN_CHILDREN
MAX_DECOMPOSITION_CHILDREN = MAX_CHILDREN


def _complete_decomposition_child_capacity(max_depth: int) -> int:
    """Return the largest complete child tree for an admitted live depth."""

    return sum(MAX_DECOMPOSITION_CHILDREN**depth for depth in range(1, max_depth + 1))


# A depth-four five-way tree contains 5 + 25 + 125 + 625 child nodes.  Durable
# completion and pause projections exclude the top-level root, so 780 is the
# exact population that every accepted live override must be able to replay.
MAX_DECOMPOSITION_REPLAY_NODES = _complete_decomposition_child_capacity(MAX_DECOMPOSITION_DEPTH)


def validate_max_decomposition_depth(
    value: object,
    *,
    source: str = "max_decomposition_depth",
) -> int:
    """Validate the shared live-depth contract at any public entry point."""

    error = (
        f"{source} must be an integer between 0 and "
        f"{MAX_DECOMPOSITION_DEPTH} inclusive so completed trees remain replayable; "
        f"migrate older values above {MAX_DECOMPOSITION_DEPTH} by reducing the value"
    )
    if type(value) is not int:
        raise ValueError(error)
    depth = cast(int, value)
    if not 0 <= depth <= MAX_DECOMPOSITION_DEPTH:
        raise ValueError(error)
    return depth
