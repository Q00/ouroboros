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


# ---------------------------------------------------------------------------
# #1170 R2 regression locks (PR-ζ-A)
#
# The PR-β ledger_only closure path produces ledgers whose `outputs` and
# `runtime_context` are filled by conservative defaults that do NOT
# contain cli-specific vocabulary (stdout / exit code / shell / …).
# Before PR-ζ-A, ``_matches_cli`` made ``goal_signal`` structurally
# redundant — only runtime/output vocabulary could classify cli — which
# left cli-todo terminating BLOCKED with ``active_task_class='library'``.
# The cases below lock the goal-signal sufficiency in, and tighten
# ``_matches_library`` so the generic word "module" no longer shadows
# other classes.
# ---------------------------------------------------------------------------


def test_cli_matches_on_goal_signal_alone() -> None:
    """A ledger whose `goal` says CLI but whose `outputs` lacks cli
    vocabulary should still classify as CLI as long as the
    ledger-evidence gate is satisfied (outputs OR runtime is non-empty)."""
    ledger = _bare_ledger("Build a habit-tracker CLI for end users")
    # Generic non-cli output vocabulary — what a ledger_only closure
    # would typically write.
    _seed_section(ledger, "outputs", value="JSON file stored in the working directory")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


def test_cli_matches_on_conservative_default_ledger() -> None:
    """R2 evidence reproduction: conservative-default-heavy ledger whose
    goal explicitly says "CLI" must classify as CLI, not fall back to
    LIBRARY. Locks #1170 R2 root cause RC-A."""
    ledger = _bare_ledger(
        "Build a small habit-tracker CLI that lets the user add, list, "
        "and check off habits, persisting them as JSON in the working "
        "directory."
    )
    # Mimic CONSERVATIVE_DEFAULT entries seen in R2-cli-todo evidence:
    # vocabulary chosen by the standardizer's safe defaults rather than
    # by user confirmation, so cli-specific tokens are absent.
    _seed_section(
        ledger,
        "outputs",
        value="Persistent JSON state file in the working directory",
        source=LedgerSource.CONSERVATIVE_DEFAULT,
    )
    _seed_section(
        ledger,
        "runtime_context",
        value="Local Python 3.x environment",
        source=LedgerSource.CONSERVATIVE_DEFAULT,
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_single, f"expected single match, got {result}"
    assert result.single is TaskClass.CLI


def test_cli_and_library_no_longer_dual_match_on_module_keyword() -> None:
    """Before PR-ζ-A, an output saying "Python module" caused
    ``_matches_library`` to fire on the generic Python-module sense
    (any code unit), shadowing cli/web-service classification. After
    removing "module" from the library keyword set, this should NOT
    fire library."""
    ledger = _bare_ledger("Build a habit-tracker CLI")
    _seed_section(
        ledger,
        "outputs",
        value="A small Python module that prints habit list to stdout",
    )
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.LIBRARY not in result.classes
    # Positive: cli should still fire from goal_signal + output_signal
    # (stdout is a cli token).
    assert result.is_single
    assert result.single is TaskClass.CLI


def test_library_still_matches_on_explicit_surface_keywords() -> None:
    """Positive regression lock — removing "module" must not weaken
    the library predicate on its actual distinctive keywords."""
    for surface in (
        "An importable Python package",
        "Public API surface for downstream consumers",
        "An SDK for the foo service",
        "A reusable library exposing helpers",
    ):
        ledger = _bare_ledger("Publish a foo helper")
        _seed_section(ledger, "outputs", value=surface)
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.LIBRARY in result.classes, (
            f"library should still match on surface={surface!r}"
        )


def test_cli_does_not_fire_without_any_ledger_evidence() -> None:
    """The ledger-evidence gate must remain in force: a goal that says
    "cli" but with empty outputs AND empty runtime_context must NOT
    classify as cli. This preserves the SSOT #1157 L1 invariant that
    classification is ledger-derived, not goal-text-derived alone."""
    ledger = _bare_ledger("Build a CLI tool")
    # Deliberately seed only non-output/non-runtime sections so the gate
    # fails. (The gate requires outputs OR runtime_context to be
    # non-empty before any signal contributes.)
    _seed_section(ledger, "actors", value="Single end user")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.CLI not in result.classes


# ---------------------------------------------------------------------------
# PR-ζ-A review-feedback regression locks (#1264 word-boundary blocker).
#
# ouroboros-agent[bot] flagged that promoting `goal_signal` to be
# independently sufficient (above) exposed the underlying *substring*
# matcher: a goal like "Build a Python client library for the Foo API"
# would have CLI fire because "client" contains "cli", producing an
# ambiguous {CLI, LIBRARY} match. Ambiguous classifications skip default
# AC injection (pipeline.py:732-740), so legitimate client-library tasks
# would lose their library contract.
#
# The fix tightens the goal-side "cli" check to a token-bounded regex
# (`\bcli\b`) so substrings inside other words no longer trigger.
# Multi-word phrases ("command line", "command-line") still match as
# literal substrings — they are not vulnerable to this class of false
# positive.
# ---------------------------------------------------------------------------


def test_cli_does_not_false_match_on_client_library_goal() -> None:
    """Goal: "Build a Python client library for the Foo API" with library
    outputs must NOT trigger CLI via substring "cli" inside "client".
    Locks the PR #1264 reviewer's blocker scenario."""
    ledger = _bare_ledger("Build a Python client library for the Foo API")
    _seed_section(
        ledger,
        "outputs",
        value="An importable Python package exposing a public API surface",
    )
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.CLI not in result.classes, (
        f"cli must not false-match on 'client library' goal, got {result}"
    )
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


def test_cli_token_does_not_false_match_on_click_clinic_etc() -> None:
    """Token-boundary regression lock for other words containing the
    letters c-l-i: click (CLI framework name, but inside a noun), clinic,
    cliché, etc. None of these should fire CLI from goal alone."""
    for false_friend in (
        "Build a click-tracking analytics dashboard",
        "Build a clinic appointment scheduler",
        "Build a clipboard manager",
        "Build a clipper service for short URLs",
    ):
        ledger = _bare_ledger(false_friend)
        # Provide neutral outputs so the gate is satisfied but no other
        # signal fires CLI from outputs/runtime.
        _seed_section(ledger, "outputs", value="Persistent JSON state file")
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI not in result.classes, (
            f"cli must not match goal={false_friend!r}; got {result}"
        )


def test_cli_still_matches_on_standalone_cli_token() -> None:
    """Positive regression lock: standalone "cli" as a word (with
    surrounding whitespace, punctuation, or end-of-string) must still
    fire the goal-signal CLI path after the regex tightening."""
    for goal in (
        "Build a CLI for habits",
        "Make a small cli.",
        "habit-tracker cli, persisting JSON",
        "build cli",
    ):
        ledger = _bare_ledger(goal)
        # Gate-satisfying neutral output.
        _seed_section(ledger, "outputs", value="Persistent JSON state file")
        result = derive_domain_from_ledger(ledger)
        assert result.is_single, f"expected single match for goal={goal!r}, got {result}"
        assert result.single is TaskClass.CLI, (
            f"goal={goal!r} should classify as cli, got {result.single}"
        )


def test_cli_still_matches_on_command_line_phrase() -> None:
    """The multi-word "command line" / "command-line" goal phrases are
    not vulnerable to the substring false-positive class and remain
    plain substring matches."""
    for goal in (
        "Build a command line habit tracker",
        "Build a command-line habit tracker",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(ledger, "outputs", value="Persistent JSON state file")
        result = derive_domain_from_ledger(ledger)
        assert result.is_single
        assert result.single is TaskClass.CLI


# ---------------------------------------------------------------------------
# PR-ζ-A second-round review-feedback regression locks (#1264 negation
# blocker).
#
# After tightening the goal-side `cli` substring to a word-boundary
# token (above), ouroboros-agent[bot] flagged a follow-up false-positive
# class: explicitly *negated* CLI mentions. A goal like "Build a Python
# client library for the Foo API, not a CLI" still matches `\bcli\b`
# against the literal "CLI" inside "not a CLI", so the inference
# returned {CLI, LIBRARY} — ambiguous. Ambiguous classifications skip
# default AC injection (pipeline.py:732-740), so a goal that *explicitly*
# excludes CLI would silently lose its library contract.
#
# The fix adds a small negation-context regex
# (`_NEGATED_CLI_GOAL_RE`) and re-checks the goal text with the
# negated mentions stripped. The tests below lock the named scenarios
# from the bot's review plus a positive case (mixed negation + positive
# assertion).
# ---------------------------------------------------------------------------


def test_cli_does_not_match_on_explicitly_negated_goal() -> None:
    """Reviewer's exact named scenario: "Build a Python client library
    for the Foo API, not a CLI" with library outputs must return a
    single LIBRARY classification, not ambiguous {CLI, LIBRARY}."""
    ledger = _bare_ledger("Build a Python client library for the Foo API, not a CLI")
    _seed_section(
        ledger,
        "outputs",
        value="An importable Python package exposing a public API surface",
    )
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.CLI not in result.classes, f"cli must not match negated goal; got {result}"
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


def test_cli_does_not_match_on_various_negation_forms() -> None:
    """Cover the common natural-language negation shapes the matcher
    must reject — "not a CLI", "no CLI", "isn't a CLI", "never a CLI",
    and the same shapes wrapping the "command line" / "command-line"
    multi-word phrase."""
    for goal in (
        "Build a Python library, not a CLI",
        "Build a Python library — no CLI here",
        "Publish an SDK; isn't a CLI",
        "Ship a package; never a CLI",
        "Build a Python library, not a command line tool",
        "Build a Python library, not a command-line tool",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(
            ledger,
            "outputs",
            value="An importable Python package exposing a public API surface",
        )
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI not in result.classes, (
            f"negated goal must not match cli: goal={goal!r}, result={result}"
        )


def test_cli_still_matches_when_negation_is_about_other_class() -> None:
    """Positive lock: when the negation clause is about a *different*
    class (e.g. "not a webhook"), the CLI signal from the rest of the
    goal must still fire."""
    ledger = _bare_ledger("Build a CLI for habit tracking, not a webhook receiver")
    _seed_section(ledger, "outputs", value="Persistent JSON state file")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.CLI in result.classes
    # Webhook should not fire because outputs lacks the side-effect
    # signals; CLI is the sole match.
    assert result.is_single
    assert result.single is TaskClass.CLI


def test_cli_still_matches_when_negated_mention_appears_alongside_positive_one() -> None:
    """Edge case: a goal that contains both a positive CLI assertion AND
    a negated CLI mention (e.g. "CLI, not a CLI library") must still
    classify as CLI — the positive mention survives the strip."""
    ledger = _bare_ledger("Build a CLI for habits — not a CLI testing library")
    _seed_section(ledger, "outputs", value="Persistent JSON state file")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.CLI in result.classes


# ---------------------------------------------------------------------------
# PR-ζ-A third-round design-notes regression locks (#1264 modal/copular
# negation gap).
#
# After the direct-negation pass (above), the bot's design notes still
# flagged modal/copular negation forms — "should not be a CLI",
# "shouldn't be a CLI", "cannot be a CLI", "must not be a CLI" — where
# short connector words ("be", "a") sit between the negation cue and
# the CLI token. The negation regex now allows up to five whitelisted
# connectors between the cue and the CLI signal, which closes the
# residual design-notes weak point.
# ---------------------------------------------------------------------------


def test_cli_does_not_match_on_modal_copular_negation() -> None:
    """Bot's named modal/copular scenarios — distance-1 to distance-3
    connectors between the negation cue and the CLI token."""
    for goal in (
        "Build a Python library that should not be a CLI",
        "Build a Python library that shouldn't be a CLI",
        "Build a Python library that must not be a CLI",
        "Build a Python library that cannot be a CLI",
        "Build a Python library that can't be a CLI",
        "Build a Python library that doesn't have to be a CLI",
        "Build a Python library that doesn't need to be a CLI",
        "Build a Python library that wouldn't be a CLI",
        "Build a Python library that won't be a CLI",
        # Same shapes wrapping the multi-word command-line phrase.
        "Build a Python library that should not be a command line tool",
        "Build a Python library that shouldn't be a command-line tool",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(
            ledger,
            "outputs",
            value="An importable Python package exposing a public API surface",
        )
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI not in result.classes, (
            f"modal-negated goal must not match cli: goal={goal!r}, result={result}"
        )


def test_cli_still_matches_on_positive_modal_goal() -> None:
    """Positive lock: when the modal/copular phrase asserts CLI rather
    than negates it ("should be a CLI"), the goal MUST still classify
    as CLI — only *negated* shapes should be stripped."""
    for goal in (
        "Build a tool that should be a CLI",
        "Build a tool that must be a CLI",
        "Build a tool that has to be a CLI",
        "Build a tool that needs to be a CLI",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(ledger, "outputs", value="Persistent JSON state file")
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI in result.classes, (
            f"positive modal goal must still match cli: goal={goal!r}, result={result}"
        )
