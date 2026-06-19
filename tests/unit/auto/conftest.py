"""Shared fixtures for ``tests/unit/auto``.

The production unsafe-context bank (``safe_defaults._UNSAFE_CONTEXT_PATTERNS``)
is intentionally empty under the freedom policy: ``ooo auto`` safe-default
closure is never vetoed on a keyword match, so a benign goal that merely
mentions ``contract``/``license``/``security``/``deploy`` is no longer
blocked.

The matcher + lateral-escalation MACHINERY is still live code, though — an
operator can re-populate the bank for a stricter deployment, and the lateral
escalation (Issue #1248) only triggers when the matcher fires. The
``_legacy_unsafe_bank`` fixture re-injects the historical bank for the
duration of a test so those mechanism tests keep exercising the matcher,
escalation, blocking, and observability paths independently of the empty
production default. Mechanism test modules opt in with::

    pytestmark = pytest.mark.usefixtures("_legacy_unsafe_bank")
"""

from __future__ import annotations

import pytest

import ouroboros.auto.safe_defaults as _safe_defaults

# The historical six-pattern bank, preserved verbatim so the mechanism tests
# assert the exact reason strings ("legal/medical judgment",
# "credentials/secrets", "ambiguous external side effect", …) they were
# written against. Keep in sync with the docstring in
# ``safe_defaults._UNSAFE_CONTEXT_PATTERNS`` if a stricter default is ever
# restored.
LEGACY_UNSAFE_CONTEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "credentials/secrets",
        r"\b(credential|credentials|secret|secrets|access token|auth token|private key|api key|password|"
        r"passphrase)\b",
    ),
    (
        "destructive production action",
        r"\b(delete|drop|erase|wipe|destroy|remove|truncate)\b.+\b(production|prod|live|database|db|"
        r"branch|bucket|account)\b|\b(production|prod|live)\b.+\b(delete|drop|erase|wipe|destroy|"
        r"remove|truncate)\b",
    ),
    (
        "payment/billing",
        r"\b(payment|billing|paid service|credit card|bank account|invoice|charge|purchase|subscribe|"
        r"subscription)\b",
    ),
    (
        "legal/medical judgment",
        r"\b(legal|compliance|license|contract|liability|medical|clinical|diagnosis|treatment|"
        r"healthcare|patient)\b",
    ),
    (
        "security-sensitive choice",
        r"\b(security|encryption|authentication|authorization|authz|oauth|sso|access control|"
        r"permissions|vulnerability|exploit|threat model)\b",
    ),
    (
        "ambiguous external side effect",
        r"\b(deploy|release|publish|send email|webhook|notify users|"
        r"create account|delete branch|database migration|"
        r"go live|going live|push live)\b",
    ),
)


@pytest.fixture
def _legacy_unsafe_bank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-inject the historical unsafe-context bank for a mechanism test."""
    monkeypatch.setattr(
        _safe_defaults,
        "_UNSAFE_CONTEXT_PATTERNS",
        LEGACY_UNSAFE_CONTEXT_PATTERNS,
    )
