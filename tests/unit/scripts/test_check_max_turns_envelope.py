"""Self-tests for ``scripts/check-max-turns-envelope.py`` (PR #786 review-1).

The guard is the enforcement backstop for issue #781: every adapter call
site that passes ``max_turns=1`` must close its tool envelope on the
*same* call so a single tool-use block cannot consume the only allowed
turn.

The original implementation accepted ``allowed_tools=<Name>`` and tried
to resolve the binding via ``ast.walk`` over an enclosing scope. That is
order- and scope-unsafe — assignments after the call, in sibling/inner
functions, or inside nested classes were erroneously accepted, so the
loophole let through exactly the regression the guard is meant to block.

These tests exercise the strict literal-only rule (Approach A): direct
``[]`` literal or an ``IfExp`` with a ``[]`` branch, nothing else. They
cover the three negatives the bot reproduced plus a positive baseline,
and pin the production-tree scan so the guard keeps reporting OK on
every existing ``max_turns=1`` site.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-max-turns-envelope.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_max_turns_envelope", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def guard():
    return _load_module()


def _scan_source(guard, source: str) -> list[tuple[int, str]]:
    """Run the guard's per-file scanner against an in-memory source string."""
    import ast

    tree = ast.parse(source)
    findings: list[tuple[int, str]] = []
    lines = source.splitlines()
    for call in guard._find_max_turns_one_calls(tree):
        if guard._call_has_empty_envelope(call):
            continue
        line = lines[call.lineno - 1] if call.lineno - 1 < len(lines) else ""
        findings.append((call.lineno, line.rstrip()))
    return findings


# ---------------------------------------------------------------------------
# Positives — must be accepted (no findings).
# ---------------------------------------------------------------------------


def test_direct_empty_list_accepted(guard) -> None:
    src = "create_llm_adapter(max_turns=1, allowed_tools=[])\n"
    assert _scan_source(guard, src) == []


def test_ifexp_with_empty_list_branch_accepted(guard) -> None:
    src = "create_llm_adapter(\n    max_turns=1,\n    allowed_tools=[] if cond else None,\n)\n"
    assert _scan_source(guard, src) == []


def test_parenthesized_ifexp_with_empty_list_branch_accepted(guard) -> None:
    src = "create_llm_adapter(\n    max_turns=1,\n    allowed_tools=([] if cond else None),\n)\n"
    assert _scan_source(guard, src) == []


def test_inverted_ifexp_rejected(guard) -> None:
    """Do not accept the unsafe orientation for the canonical backend gate.

    ``None if backend_supports_tool_envelope(...) else []`` leaves supported
    backends without a closed envelope, which is exactly the future
    regression this guard must prevent.
    """
    src = (
        "create_llm_adapter(\n"
        "    max_turns=1,\n"
        "    allowed_tools=None if backend_supports_tool_envelope(backend) else [],\n"
        ")\n"
    )
    findings = _scan_source(guard, src)
    assert findings, f"expected guard to reject inverted conditional, got {findings!r}"


# ---------------------------------------------------------------------------
# Negatives — the bot's BLOCKING reproductions and friends.
# Each case asserts the strict guard now rejects the form. Under the
# previous Name-resolving implementation, every one of these returned 0
# findings (false negative).
# ---------------------------------------------------------------------------


def test_name_reference_rejected_even_with_correct_binding(guard) -> None:
    """Even a benign Name binding is rejected under Approach A — the
    rule is literal-only, no Name resolution. Migrate sites to inline
    literals."""
    src = (
        "def f(cond):\n"
        "    x = [] if cond else None\n"
        "    create_llm_adapter(max_turns=1, allowed_tools=x)\n"
    )
    findings = _scan_source(guard, src)
    assert findings, f"expected guard to reject Name reference, got {findings!r}"


def test_assignment_after_call_in_same_function_rejected(guard) -> None:
    """Bot's first reproduction: assignment to the bound name appears
    *after* the call. The previous AST-walk-the-enclosing-subtree code
    accepted this; Approach A rejects it (it never inspects Names)."""
    src = "def f():\n    create_llm_adapter(max_turns=1, allowed_tools=x)\n    x = []\n"
    findings = _scan_source(guard, src)
    assert findings, f"expected guard to reject post-call assignment, got {findings!r}"


def test_assignment_in_nested_inner_function_rejected(guard) -> None:
    """Bot's nested-function reproduction: ``x = []`` lives in a
    sibling/inner ``def g(): ...`` and must not satisfy the outer
    call's envelope."""
    src = (
        "def f():\n"
        "    create_llm_adapter(max_turns=1, allowed_tools=x)\n"
        "    def g():\n"
        "        x = []\n"
    )
    findings = _scan_source(guard, src)
    assert findings, f"expected guard to reject nested-function leak, got {findings!r}"


def test_assignment_in_different_outer_function_rejected(guard) -> None:
    """Bot's cross-function reproduction: ``x = []`` lives in an
    unrelated module-level function. The previous scope-walk code
    incorrectly accepted this when the guard re-walked Module."""
    src = "def g():\n    x = []\ndef f():\n    create_llm_adapter(max_turns=1, allowed_tools=x)\n"
    findings = _scan_source(guard, src)
    assert findings, f"expected guard to reject cross-function binding, got {findings!r}"


def test_assignment_in_nested_class_rejected(guard) -> None:
    """Class-body bindings must not satisfy outer call envelopes."""
    src = (
        "def f():\n"
        "    create_llm_adapter(max_turns=1, allowed_tools=x)\n"
        "    class C:\n"
        "        x = []\n"
    )
    findings = _scan_source(guard, src)
    assert findings, f"expected guard to reject class-body binding, got {findings!r}"


def test_missing_allowed_tools_rejected(guard) -> None:
    src = "create_llm_adapter(max_turns=1)\n"
    findings = _scan_source(guard, src)
    assert findings


def test_non_empty_list_rejected(guard) -> None:
    src = 'create_llm_adapter(max_turns=1, allowed_tools=["Read"])\n'
    findings = _scan_source(guard, src)
    assert findings


def test_function_call_value_rejected(guard) -> None:
    src = "create_llm_adapter(max_turns=1, allowed_tools=_interview_allowed_tools(backend))\n"
    findings = _scan_source(guard, src)
    assert findings


# ---------------------------------------------------------------------------
# Production-tree regression: every existing ``max_turns=1`` site must
# continue to satisfy the stricter rule (otherwise it's using a Name
# reference and needs to be migrated to a direct literal).
# ---------------------------------------------------------------------------


def test_production_scan_passes() -> None:
    """Run the script as a subprocess against the live ``src/ouroboros``
    tree. Pinned to the repo's actual scripts entrypoint so a regression
    in either the guard or any current call site fails loudly here."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"production scan failed under strict guard.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
