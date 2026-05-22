"""Pattern-matcher tests for L1-b (#1171).

Tests cover the four outcome shapes:

- **Single match** — one class's predicate fires for a representative
  ledger configuration.
- **Ambiguous** — two predicates fire; ``DomainInference.is_ambiguous``
  is True and the interview driver gets the disambiguation cue.
- **Unmatched** — no predicate fires; falls to ``LIBRARY`` with
  ``reason == "unmatched"``.
- **Empty ledger** — bare ``from_goal`` ledger does not crash the
  matcher.

Adding a new task class requires adding a positive test here; the
``test_every_task_class_has_a_pattern`` guard fails otherwise.
"""

from __future__ import annotations

from ouroboros.auto.domain_inference import (
    _PATTERN_REGISTRY,
    DomainInference,
    derive_domain_from_ledger,
)
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.task_classes import TaskClass


def _seed_section(
    ledger: SeedDraftLedger,
    section: str,
    *,
    value: str,
    key: str | None = None,
    status: LedgerStatus = LedgerStatus.CONFIRMED,
    source: LedgerSource = LedgerSource.USER_PREFERENCE,
    confidence: float = 0.9,
) -> None:
    """Convenience helper for tests — append a CONFIRMED entry to *section*."""
    ledger.add_entry(
        section,
        LedgerEntry(
            key=key or f"{section}.test_entry",
            value=value,
            source=source,
            confidence=confidence,
            status=status,
        ),
    )


def _bare_ledger(goal: str = "Build a tiny local CLI") -> SeedDraftLedger:
    return SeedDraftLedger.from_goal(goal)


# ---------------------------------------------------------------------------
# Single matches — one per task class
# ---------------------------------------------------------------------------


def test_single_match_cli() -> None:
    ledger = _bare_ledger("Build a habit-tracker CLI tool")
    _seed_section(ledger, "outputs", value="Deterministic stdout and exit code 0")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


def test_single_match_webhook() -> None:
    ledger = _bare_ledger("Build a webhook receiver service")
    _seed_section(ledger, "inputs", value="Incoming webhook POST payloads from GitHub")
    _seed_section(ledger, "outputs", value="DB row stored per event; log entry appended")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEBHOOK


def test_single_match_web_service() -> None:
    ledger = _bare_ledger("Build a REST API for blog posts")
    _seed_section(
        ledger,
        "outputs",
        value="Multiple REST endpoints returning JSON body responses",
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_SERVICE


def test_single_match_data_pipeline() -> None:
    ledger = _bare_ledger("Aggregate daily logs into Parquet")
    _seed_section(ledger, "inputs", value="Dataset of log files split per day")
    _seed_section(ledger, "outputs", value="Aggregated output dataset in Parquet")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.DATA_PIPELINE


def test_single_match_game_2d() -> None:
    ledger = _bare_ledger("Build a small 2D game scene")
    _seed_section(
        ledger,
        "outputs",
        value="Each frame renders a canvas with the playable scene state",
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.GAME_2D


def test_single_match_refactor_in_place() -> None:
    ledger = _bare_ledger("Refactor src/foo into vertical slices")
    _seed_section(
        ledger,
        "constraints",
        value="Preserve behavior so the same tests keep passing",
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.REFACTOR_IN_PLACE


def test_single_match_library() -> None:
    ledger = _bare_ledger("Publish a JSON-schema parsing library")
    _seed_section(
        ledger,
        "outputs",
        value="An importable Python package exposing a public API surface",
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


# ---------------------------------------------------------------------------
# Ambiguous and unmatched
# ---------------------------------------------------------------------------


def test_ambiguous_when_two_patterns_fire() -> None:
    """A CLI that also exposes a webhook receiver — both CLI and
    WEBHOOK fire. Matcher should surface the ambiguity; the interview
    driver (L1-c) disambiguates."""
    ledger = _bare_ledger("Build a CLI tool that also receives webhooks")
    _seed_section(ledger, "outputs", value="Stdout shows status; DB row stored on each event")
    _seed_section(ledger, "runtime_context", value="Local shell or background daemon")
    _seed_section(ledger, "inputs", value="CLI args plus incoming webhook payloads")
    result = derive_domain_from_ledger(ledger)
    assert result.is_ambiguous
    assert TaskClass.CLI in result.classes
    assert TaskClass.WEBHOOK in result.classes
    assert result.single is None
    assert result.reason == "multiple patterns matched"


def test_unmatched_falls_back_to_library() -> None:
    """A ledger whose entries contain no task-class signal at all falls
    to LIBRARY (safest completion gate) with ``reason='unmatched'``."""
    ledger = _bare_ledger("Make a thing that does the thing")  # deliberately vague
    # Add weak entries that should not fire any pattern — purposely free
    # of canonical vocabulary.
    _seed_section(ledger, "actors", value="Some user")
    _seed_section(ledger, "constraints", value="Be nice")
    result = derive_domain_from_ledger(ledger)
    assert result.is_unmatched
    assert result.single is TaskClass.LIBRARY
    assert result.fallback is TaskClass.LIBRARY
    assert result.reason == "unmatched"


def test_empty_ledger_does_not_crash() -> None:
    """A bare ``from_goal`` ledger with no extra entries: the matcher
    must not raise, and the goal text alone may match no pattern (→
    unmatched)."""
    ledger = SeedDraftLedger.from_goal("")
    result = derive_domain_from_ledger(ledger)
    assert isinstance(result, DomainInference)


# ---------------------------------------------------------------------------
# Active-status discipline
# ---------------------------------------------------------------------------


def test_inactive_entries_do_not_trigger_patterns() -> None:
    """Entries with WEAK / CONFLICTING / BLOCKED status must be ignored.

    The interview's standardizer marks superseded answers as CONFLICTING;
    those should not bleed into the inference output."""
    ledger = _bare_ledger("Build a small project")
    _seed_section(
        ledger,
        "outputs",
        value="stdout exit code",  # would normally trigger CLI
        status=LedgerStatus.CONFLICTING,
    )
    result = derive_domain_from_ledger(ledger)
    # The CLI pattern depends on runtime_context too, but the outputs
    # signal alone (CONFLICTING) must not fire any class.
    assert TaskClass.CLI not in result.classes


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


def test_every_task_class_has_a_pattern() -> None:
    """L1-b registry covers every L1-a TaskClass enum value. Adding a new
    class without a pattern function (or vice versa) fails here."""
    assert set(_PATTERN_REGISTRY.keys()) == set(TaskClass)


def test_domain_inference_dataclass_properties() -> None:
    """Spot-check the convenience properties exposed by DomainInference."""
    single = DomainInference(
        classes=frozenset({TaskClass.CLI}),
        reason="single pattern match",
    )
    assert single.is_single
    assert not single.is_ambiguous
    assert not single.is_unmatched
    assert single.single is TaskClass.CLI

    ambiguous = DomainInference(
        classes=frozenset({TaskClass.CLI, TaskClass.WEBHOOK}),
        reason="multiple patterns matched",
    )
    assert ambiguous.is_ambiguous
    assert not ambiguous.is_single
    assert ambiguous.single is None

    unmatched = DomainInference(
        classes=frozenset(),
        reason="unmatched",
        fallback=TaskClass.LIBRARY,
    )
    assert unmatched.is_unmatched
    assert unmatched.single is TaskClass.LIBRARY
