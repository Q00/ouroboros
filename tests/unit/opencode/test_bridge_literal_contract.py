"""Pin the two literals the OpenCode bridge duplicates from Python.

``src/ouroboros/opencode/plugin/ouroboros-bridge.ts`` carries a comment saying
these constants "are the Python constants in mcp/tools/advisory_dispatch.py;
they cannot be imported across the language boundary, so a test pins them equal
instead."

The parity check previously lived inside the larger advisory-dispatch integration
test. This focused module owns the source-level language-boundary contract while
that integration test exercises recognition of the resulting bridge echo.

It matters more than a normal duplication: the marker is how the server-side
gatekeeper recognises a dispatch the plugin produced. If the two drift, nothing
raises. The plugin keeps announcing itself with a string the server no longer
looks for, and the fan-out silently stops being recognised.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from ouroboros.mcp.tools.advisory_dispatch import (
    _BRIDGE_NOTICE_OPENING,
    QUESTION_ADVISORY_DISPATCH_MARKER,
)

BRIDGE_TS = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "ouroboros"
    / "opencode"
    / "plugin"
    / "ouroboros-bridge.ts"
)


def _ts_literal(name: str) -> str:
    """Read ``export const <name> = "..."`` out of the bridge source."""
    source = BRIDGE_TS.read_text(encoding="utf-8")
    match = re.search(rf'^export const {re.escape(name)} = ("(?:[^"\\]|\\.)*")$', source, re.M)
    if match is None:
        pytest.fail(
            f"{name} is no longer declared as a double-quoted `export const` in "
            f"{BRIDGE_TS.name}. If it moved or changed shape, update this reader; "
            "do not delete the check, because the two sides still have to agree."
        )
    try:
        literal = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{name} is not a valid JSON-compatible TypeScript string: {exc.msg}")
    assert isinstance(literal, str)
    return literal


@pytest.mark.parametrize(
    ("typescript_name", "python_literal"),
    [
        ("OUROBOROS_DISPATCH_MARKER", QUESTION_ADVISORY_DISPATCH_MARKER),
        ("BRIDGE_NOTICE_OPENING", _BRIDGE_NOTICE_OPENING),
    ],
)
def test_bridge_literals_match_python(typescript_name: str, python_literal: str) -> None:
    assert _ts_literal(typescript_name) == python_literal
