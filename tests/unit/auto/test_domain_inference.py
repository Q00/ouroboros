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

import pytest

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


def test_single_match_webhook() -> None:
    ledger = _bare_ledger("Build a webhook receiver service")
    _seed_section(ledger, "inputs", value="Incoming webhook POST payloads from GitHub")
    _seed_section(ledger, "outputs", value="DB row stored per event; log entry appended")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEBHOOK


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


# ---------------------------------------------------------------------------
# PR-ζ-A fourth-round review-feedback regression locks (#1264 exclusion
# phrasing blocker).
#
# After the modal/copular pass (above), the bot flagged a further
# exclusion-phrasing class: "without a CLI", "excluding a CLI",
# "instead of a CLI", "rather than a CLI", "sans CLI". The negation
# regex now treats these as additional negation cues alongside the
# round-2/3 forms, so all of them strip cleanly.
# ---------------------------------------------------------------------------


def test_cli_still_matches_on_affirmative_not_just_or_not_only_expansion() -> None:
    """Bot's round-5 reproduction: `not just a CLI` / `not only a CLI`
    are AFFIRMATIVE expansions ("a CLI and also something else"), not
    denials. The negation stripper must NOT collapse them, so the CLI
    signal survives and goal_signal fires."""
    for goal in (
        "Build not just a CLI for habits",
        "Build not only a CLI for habits",
        "Build a tool that is not only a CLI but also a library",
        "Build a tool that is not just a CLI but also a library",
        "Build a tool that should not just be a CLI but also a library",
    ):
        ledger = _bare_ledger(goal)
        # Neutral output so the gate is satisfied but no output/runtime
        # signal contributes — the test isolates the goal-signal path.
        _seed_section(ledger, "outputs", value="Persistent JSON state file")
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI in result.classes, (
            f"affirmative 'not just/only a CLI' must still match cli: "
            f"goal={goal!r}, result={result}"
        )


def test_cli_does_not_match_on_non_prefix_negation() -> None:
    """Bot's round-6 reproduction: "non-CLI" / "non CLI" prefix forms.
    These must be treated as exclusion, not as positive CLI evidence."""
    for goal in (
        "Build a non-CLI Python library",
        "Build a non CLI Python library",
        "Build a non-command-line Python library",
        "Build a non-command line Python library",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(
            ledger,
            "outputs",
            value="An importable Python package exposing a public API surface",
        )
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI not in result.classes, (
            f"non-prefix goal must not match cli: goal={goal!r}, result={result}"
        )


def test_cli_does_not_match_on_participle_negation() -> None:
    """Bot's round-7 reproductions: descriptive participles between the
    negation cue and the CLI token. With the connector whitelist
    replaced by a distance-bounded path + affirmative-flip blocker,
    arbitrary participles ("intended", "meant", "designed", "exposed",
    "for") no longer need to be enumerated."""
    for goal in (
        "Build a Python library that is not intended to be a CLI",
        "Build a Python library that is not meant to be a CLI",
        "Build a Python library that is not designed to be a CLI",
        "Build a Python library that is not exposed as a CLI",
        "Build a Python library, not for CLI use",
        # Multi-word phrase variants.
        "Build a Python library that is not intended to be a command-line tool",
        "Build a Python library that is not designed to be a command line tool",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(
            ledger,
            "outputs",
            value="An importable Python package exposing a public API surface",
        )
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI not in result.classes, (
            f"participle-negated goal must not match cli: goal={goal!r}, result={result}"
        )


def test_cli_token_unaffected_by_non_prefix_in_other_words() -> None:
    """Positive lock for the `non-` prefix regex: words like
    `non-client`, `non-clinic`, `nonprofit` must not be falsely
    stripped or affect the CLI signal."""
    # No CLI signal in any of these; the test ensures the strip regex
    # does not bleed into adjacent legitimate words that happen to
    # contain "non" + cl-prefixed letters.
    for goal in (
        "Build a non-client analytics dashboard",
        "Build a non-clinic appointment scheduler",
        "Build a nonprofit donation tracker",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(ledger, "outputs", value="Persistent JSON state file")
        result = derive_domain_from_ledger(ledger)
        # No CLI signal here — neither positive nor false-stripped.
        assert TaskClass.CLI not in result.classes, (
            f"unrelated `non-` word must not affect cli matcher: goal={goal!r}, result={result}"
        )


def test_cli_does_not_match_on_exclusion_phrasing() -> None:
    """Bot's named scenario (`Build a Python library, without a CLI`) +
    the other common exclusion shapes — each must drop CLI from the
    classification."""
    for goal in (
        "Build a Python library, without a CLI",
        "Build a Python library, excluding a CLI",
        "Build a Python library, sans CLI",
        "Build a Python library, instead of a CLI",
        "Build a Python library, rather than a CLI",
        # Multi-word "command line" / "command-line" variants.
        "Build a Python library, without a command line tool",
        "Build a Python library, instead of a command-line tool",
        "Build a Python library, rather than a command line tool",
    ):
        ledger = _bare_ledger(goal)
        _seed_section(
            ledger,
            "outputs",
            value="An importable Python package exposing a public API surface",
        )
        result = derive_domain_from_ledger(ledger)
        assert TaskClass.CLI not in result.classes, (
            f"exclusion-phrased goal must not match cli: goal={goal!r}, result={result}"
        )


# ---------------------------------------------------------------------------
# web_app inference and the package.json library false positive (#1813)
# ---------------------------------------------------------------------------


def test_web_app_requires_browser_context_signal() -> None:
    """Generic form/validation vocabulary without any browser-context signal
    must not classify as web_app — those words appear in many non-UI goals
    ("performance", "transform", "invalidation")."""
    ledger = _bare_ledger("Improve the settings handling")
    _seed_section(ledger, "outputs", value="A validation summary is printed per form entry")
    result = derive_domain_from_ledger(ledger)
    assert not (result.is_single and result.single is TaskClass.WEB_APP)


def test_package_json_filename_is_not_a_library_signal() -> None:
    ledger = _bare_ledger("Ship the project manifest")
    _seed_section(ledger, "outputs", value="Updated package.json with build scripts")
    assert _PATTERN_REGISTRY[TaskClass.LIBRARY](ledger) is False


def test_package_word_still_matches_library() -> None:
    ledger = _bare_ledger("Publish a reusable helper")
    _seed_section(ledger, "outputs", value="A reusable package published to PyPI")
    assert _PATTERN_REGISTRY[TaskClass.LIBRARY](ledger) is True


def test_ambiguous_browser_ui_with_rest_backend() -> None:
    """A browser frontend backed by REST endpoints fires both web_app and
    web_service; the interview driver (L1-c) disambiguates."""
    ledger = _bare_ledger("Build a metrics dashboard")
    _seed_section(
        ledger,
        "outputs",
        value=(
            "Browser frontend with a filters panel; "
            "multiple REST endpoints returning JSON body responses"
        ),
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_ambiguous
    assert TaskClass.WEB_APP in result.classes
    assert TaskClass.WEB_SERVICE in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a log summarizer, not a browser page",
        "Build a log summarizer that is not intended to be a browser page",
        "Build a log summarizer instead of a browser page",
        "Build a non-browser page viewer for logs",
    ],
)
def test_web_app_negation_forms_are_rejected(goal: str) -> None:
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Plain text summary written to a file")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI, not a browser or web app",
        "Build a CLI that is not a browser, web app, or frontend",
    ],
)
def test_coordinated_negation_strips_all_browser_alternatives(goal: str) -> None:
    """R4 probe: one denial covers every coordinated browser alternative."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive terminal output with deterministic stdout")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    "outputs",
    [
        "A dashboard page with charts and navigation",
        "A responsive dashboard with charts and navigation",
    ],
)
def test_dashboard_outputs_are_ui_evidence(outputs: str) -> None:
    """R4 probe: ordinary rendered-dashboard descriptions must match."""
    ledger = _bare_ledger("Build a web application dashboard")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "An importable package with signup form helpers",
        "An importable package with settings panel helpers",
        "An importable package of dashboard templates",
    ],
)
def test_widget_named_library_artifacts_are_not_ui_evidence(outputs: str) -> None:
    """R5 probe: a widget word modifying helpers/templates describes an
    API artifact, not a produced UI."""
    ledger = _bare_ledger("Build a browser automation library")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "manifest",
    ["packages/web/package.json", "package-lock.json"],
)
def test_manifest_paths_and_lockfiles_are_not_library_signals(manifest: str) -> None:
    """R5 probe: monorepo manifest paths and lockfiles must be masked
    whole, and the directory word "packages" is not the library word."""
    ledger = _bare_ledger("Build a signup page that runs in the browser")
    _seed_section(
        ledger,
        "outputs",
        value=f"Signup form with a settings panel shown in the browser; {manifest}",
    )
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a terminal dashboard, not a browser-based UI or web app",
        "Build a terminal dashboard with no browser UI, web app, or frontend",
    ],
)
def test_modified_coordinated_denials_keep_cli_single(goal: str) -> None:
    """R6 probe: denials whose alternatives carry modifiers ("browser-based
    UI", "browser UI") must strip every coordinated alternative."""
    ledger = _bare_ledger(goal)
    _seed_section(
        ledger,
        "outputs",
        value="Interactive dashboard panels and buttons with deterministic stdout",
    )
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    "outputs",
    [
        "An importable package with API reference pages",
        "An importable package with documentation and example pages",
        "An importable package with signup form validation helpers",
    ],
)
def test_compound_api_artifacts_are_not_ui_evidence(outputs: str) -> None:
    """R6 probe: compound API/documentation artifact descriptions are
    routine library outputs, not a produced UI."""
    ledger = _bare_ledger("Build a browser automation library")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "outputs",
    [
        "Forms for login and signup with navigation",
        "Buttons for save and cancel with navigation",
        "Pages for login, account settings, and reports",
    ],
)
def test_noun_first_ui_outputs_match_web_app(outputs: str) -> None:
    """R6 probe: noun-first flow descriptions are ordinary affirmative
    web-app outputs and must not fall through to unmatched."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "A todo list where users can add, edit, and delete tasks",
        "A browser calendar with event creation and drag-and-drop",
    ],
)
def test_user_action_outputs_match_web_app(outputs: str) -> None:
    """R7 probe: explicit web-app intent plus clearly interactive outputs
    must not fall through to unmatched."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "A settings screen with buttons for save and cancel",
        "A login screen displayed in the browser",
    ],
)
def test_browser_ui_screens_match_web_app_not_game(outputs: str) -> None:
    """R8 probe: product-anchored screens are browser UI, not game scenes."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a web app", "A password reset screen with an email field and confirmation message"),
        ("Build a web app", "A shopping cart page lists products and totals"),
        ("Build a frontend", "A modal dialog lets users confirm deletion"),
    ],
)
def test_non_allowlisted_product_widgets_match_web_app(goal: str, outputs: str) -> None:
    """R9 probe: ordinary user-facing screens/pages/dialogs must classify
    without being enumerated in a finite anchor list."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "Rendered sprites with player movement and collisions",
        "The screen renders sprites with player movement",
    ],
)
def test_browser_hosted_games_keep_game_evidence(outputs: str) -> None:
    """R10 probe: browser deployment does not make game rendering UI
    evidence — game-domain vocabulary keeps render/screen ownership."""
    ledger = _bare_ledger("Build a browser game")
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in a browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.GAME_2D


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a browser automation library",
            "Reusable test pages for login flows and browser fixtures",
        ),
        (
            "Build a frontend component library",
            "Reusable modal dialogs and buttons for applications",
        ),
    ],
)
def test_reusable_browser_tooling_stays_library(goal: str, outputs: str) -> None:
    """R10 probe: outputs declaring artifact intent (reusable/importable
    components) describe what the library ships, not a produced app."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a browser app, not a game",
            "A login screen displayed with save and cancel buttons",
        ),
        (
            "Build a browser-based video player",
            "A player screen with playback buttons and navigation",
        ),
    ],
)
def test_negated_or_polysemous_game_words_do_not_own_ui(goal: str, outputs: str) -> None:
    """R11 probe: a negated game mention and the media sense of "player"
    are not affirmative game evidence."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a browser UI component library", "Modal dialogs and login forms"),
        ("Build a frontend SDK", "Settings panels and account forms"),
    ],
)
def test_goal_side_artifact_intent_stays_library(goal: str, outputs: str) -> None:
    """R11 probe: explicit library/SDK intent in the goal is authoritative
    for _matches_library and must void app UI evidence the same way."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a browser app, not a platformer", "A login screen with account settings"),
        ("Build a browser app without a canvas or game loop", "A settings page with account forms"),
    ],
)
def test_negated_game_vocabulary_covers_all_signals(goal: str, outputs: str) -> None:
    """R12 probe: every goal-side game signal — domain words and fast-path
    keywords alike — shares the negation treatment."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    ["Build a web app, not a library", "Build a web app, not an SDK"],
)
def test_negated_library_intent_does_not_suppress_web_app(goal: str) -> None:
    """R12 probe: a denied artifact class is not library intent — neither
    for the web_app guard nor for _matches_library itself."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login forms and settings panels")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a frontend dashboard", "Reusable modal dialogs and settings panels"),
        ("Build a browser app", "Reusable login forms for account access"),
    ],
)
def test_reusable_alone_is_not_library_intent(goal: str, outputs: str) -> None:
    """R12 probe: "reusable" is not a _matches_library signal; without
    genuine library evidence it must not suppress an explicit browser
    application."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "Responsive navigation bar and an account menu",
        "A toolbar, sidebar, and data table",
    ],
)
def test_layout_and_navigation_vocabulary_is_ui_composition(outputs: str) -> None:
    """R13 probe: routine composed browser UIs must not fall through."""
    ledger = _bare_ledger("Build a web application dashboard")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app, not a browser extension",
        "Build a web app but not a browser game",
    ],
)
def test_denial_of_other_artifacts_keeps_affirmative_web_app(goal: str) -> None:
    """R15 probe: denying a browser extension or a browser game does not
    deny the independently asserted web app."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login forms and settings panels")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app, not a frontend SDK",
        "Build a web app, not a browser extension for login pages",
    ],
)
def test_denied_head_noun_scopes_dominance(goal: str) -> None:
    """R16 probe: dominance keys on the artifact actually denied (SDK,
    extension) — modifiers and PP objects inside the denied phrase do not
    make it an app denial."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login forms and settings panels")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "Browser dashboard with login forms and a public API",
        "Browser dashboard with login forms and a companion SDK",
    ],
)
def test_co_produced_api_in_outputs_keeps_web_app_ownership(outputs: str) -> None:
    """R17 probe: outputs enumerate all deliverables — an API/SDK produced
    alongside an affirmative browser UI must not convert the result into
    library-only semantics."""
    ledger = _bare_ledger("Build a web application with a public API")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a browser-based drawing application",
            "Canvas drawing surface with a color toolbar and settings panel",
        ),
        (
            "Build a browser vector-scene editor",
            "Scene editing surface with layer panels and a toolbar",
        ),
    ],
)
def test_canvas_and_scene_uis_are_not_conclusive_game_evidence(goal: str, outputs: str) -> None:
    """R18 probe: canvas/scene are shared rendering vocabulary — browser
    drawing and editing UIs are not games."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


def test_negated_canvas_does_not_fire_game_without_browser() -> None:
    ledger = _bare_ledger("Build a local report generator without a canvas")
    _seed_section(ledger, "outputs", value="Plain text summary written to a file")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.GAME_2D not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a tool with an importable public API",
        "Build a toolkit which exposes an SDK",
        "Build a tool that is importable",
    ],
)
def test_artifact_defining_clauses_keep_library_ownership(goal: str) -> None:
    """R19 probe: relative/prepositional clauses can define the artifact
    shape — blanket clause removal must not erase library ownership."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="A well-documented module")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


def test_co_requested_game_and_admin_app_is_ambiguous() -> None:
    """R20 probe: an explicit multi-product goal keeps both classes."""
    ledger = _bare_ledger("Build a browser game and a separate admin web app")
    _seed_section(
        ledger,
        "outputs",
        value="Playable game loop on a canvas plus an admin dashboard with login forms",
    )
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes
    assert TaskClass.GAME_2D in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a tool for use as an SDK",
        "Build a tool intended for import as a library",
    ],
)
def test_artifact_defining_for_clauses_keep_library(goal: str) -> None:
    """R20 probe: a for-clause can define the artifact, not its subject."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="A well-documented module")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "goal",
    [
        "Build a package delivery tracking web app",
        "Build an SDK documentation web app",
        "Build a web app for SDK documentation",
    ],
)
def test_library_subject_modifiers_do_not_erase_web_app_head(goal: str) -> None:
    """R21 probe: ownership follows the goal's artifact head — package/SDK
    subject vocabulary modifying an explicit web app is not library
    ownership."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Browser dashboard with search and a navigation bar")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "There are no errors in the signup form",
        "No validation errors appear in the signup form",
    ],
)
def test_quality_statements_are_not_ui_denials(outputs: str) -> None:
    """R22 probe: "no errors in the form" denies errors, not the form."""
    ledger = _bare_ledger("Build a browser application")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a website with a contact form",
            "Responsive landing page with contact form and navigation",
        ),
        (
            "Build a web UI for account management",
            "Login page with settings panel and navigation",
        ),
    ],
)
def test_website_and_web_ui_are_browser_context(goal: str, outputs: str) -> None:
    """R22 probe: "website" and "web UI" are direct browser-UI artifact
    names."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "runtime",
    ["Non-browser desktop runtime", "No browser; local desktop runtime"],
)
def test_non_browser_runtime_context_is_not_browser_evidence(runtime: str) -> None:
    """R23 probe: runtime/output browser context must be token- and
    negation-aware — a desktop app is not a web app."""
    ledger = _bare_ledger("Build a desktop account manager")
    _seed_section(ledger, "outputs", value="Login form with a settings panel")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "outputs",
    [
        "Login form submits credentials to a public API and shows validation errors",
        "Account page showing tokens from the payments SDK",
    ],
)
def test_consumed_apis_are_not_library_artifacts(outputs: str) -> None:
    """R23 probe: an API/SDK the UI consumes is a dependency, not a
    produced library artifact."""
    ledger = _bare_ledger("Build a website for account management")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a server-rendered web app, not a single-page application",
        "Build a multi-page website rather than a single-page app",
    ],
)
def test_subtype_denial_does_not_deny_the_web_class(goal: str) -> None:
    """R24 probe: denying the single-page subtype affirms, not denies,
    the web application class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login form rendered with validation feedback")
    _seed_section(ledger, "runtime_context", value="Browser runtime")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "No desktop app; build a web app for accounts",
        "No native UI. Create a website for accounts",
    ],
)
def test_negation_does_not_cross_sentence_punctuation(goal: str) -> None:
    """R26 probe: a denial in one clause cannot consume the affirmative
    artifact of the next."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login form with settings panel")
    _seed_section(ledger, "runtime_context", value="Browser runtime")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a web app using Stripe's SDK", "A login form with an account settings panel"),
        (
            "Build a website for payments",
            "Checkout form using Stripe's SDK and a confirmation page",
        ),
    ],
)
def test_possessive_dependencies_are_not_library_ownership(goal: str, outputs: str) -> None:
    """R26 probe: possessive vendor dependencies are consumed, not
    produced."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a React web app", "Responsive layout with accessible controls and routing"),
        ("Build a website", "Homepage with responsive layout and accessibility"),
    ],
)
def test_layout_and_homepage_vocabulary_is_ui_composition(goal: str, outputs: str) -> None:
    """R26 probe: routine layout/control vocabulary is affirmative web-app
    composition."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build an ecommerce web app", "Product catalog with shopping cart and checkout flow"),
        ("Build a blog website", "Article list, comments, tags, and pagination"),
        ("Build a web app for photo sharing", "Photo gallery, uploads, likes, and comments"),
    ],
)
def test_requested_web_product_owns_its_output_description(goal: str, outputs: str) -> None:
    """R27 probe: an affirmative web-app artifact request owns outputs
    that describe the requested product — no widget catalog required."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a web app using a command-line image converter",
            "Photo gallery, uploads, and comments",
        ),
        (
            "Build a web app using an existing REST API",
            "Product catalog with shopping cart and checkout flow",
        ),
    ],
)
def test_consumed_tooling_and_services_stay_dependencies(goal: str, outputs: str) -> None:
    """R28 probe: the produced-versus-consumed rule is shared across
    classes — consumed CLIs and REST APIs are dependencies."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a web app powered by an existing REST API",
            "Product catalog with shopping cart and checkout flow",
        ),
        (
            "Build a web app for shopping",
            "Dashboard fetches data from a REST API and shows product listings",
        ),
        (
            "Build a web app that invokes an existing CLI",
            "Photo gallery, uploads, and comments",
        ),
    ],
)
def test_dependency_relations_do_not_fabricate_competing_classes(goal: str, outputs: str) -> None:
    """R29 probe: powered-by/fetches-from/invokes name dependencies; the
    normalization covers goals and outputs alike."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build an SDK targeting web apps",
        "Build a library supporting web apps",
        "Build a package used by web apps",
    ],
)
def test_participial_consumer_web_apps_are_not_the_product(goal: str) -> None:
    """R29 probe: a web app the artifact targets/supports/serves is the
    consumer, not the produced artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="An importable package with a public API")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "goal",
    ["Build a backend to expose a REST API", "Build a backend to provide a REST API"],
)
def test_production_purpose_clauses_are_not_consumed_dependencies(goal: str) -> None:
    """R30 probe: "to expose/provide a REST API" produces the API."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="JSON responses for clients")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_SERVICE


def test_with_joined_web_co_product_owns_web_regardless_of_wording() -> None:
    """R30 probe: an explicit web co-product retains web ownership even
    when outputs use no fixed widget vocabulary."""
    ledger = _bare_ledger("Build a Python library with an admin web app")
    _seed_section(ledger, "outputs", value="An importable package and an admin portal")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes
    assert TaskClass.LIBRARY in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a browser-based dashboard for test results",
        "Build a browser monitoring dashboard",
    ],
)
def test_inspection_subject_vocabulary_does_not_veto_browser_product(goal: str) -> None:
    """R36 probe: test/monitoring describes a dashboard's subject; the
    final browser-UI product head still owns the artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts and navigation")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a browser dashboard showing test results",
        "Build a browser dashboard with monitoring charts",
        "Build a browser app for test results",
        "Build a browser testing dashboard showing pass and fail trends",
        "Build a browser app displaying monitoring charts",
    ],
)
def test_inspection_subject_clauses_follow_browser_product_head(goal: str) -> None:
    """R37 probe: inspection vocabulary after or before the UI head can
    describe product content without changing the produced artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts and navigation")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "Login form and panels showing HTTP responses from the API",
        "Login form and panels displaying a JSON body from the REST API",
    ],
)
def test_displayed_service_responses_do_not_create_service_ownership(outputs: str) -> None:
    """R36 probe: response payloads displayed by an API client are
    consumed data, not a produced endpoint/server surface."""
    ledger = _bare_ledger("Build a web app client for a REST API")
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Python library as opposed to a web app",
        "Build a Python library for browser tooling as opposed to a web app",
    ],
)
def test_as_opposed_to_denies_web_app(goal: str) -> None:
    """R36 probe: the shared denial grammar covers this adversative cue,
    including after a content clause."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="An importable package with a public API")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI, not web apps",
        "Build a CLI without web applications",
        "Build a CLI rather than web apps",
    ],
)
def test_plural_web_app_denials_are_recognized(goal: str) -> None:
    """R31 probe: plural denials share the signal/negation family."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Deterministic stdout and exit code 0")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app that catalogs sites which are not a web application",
        "Build a web app for detecting pages that are not a web application",
    ],
)
def test_denials_inside_content_clauses_do_not_govern_the_product(goal: str) -> None:
    """R31 probe: a denial describing the cataloged/detected content is
    scoped to that clause, not to the produced artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Dashboard with search form and results pages")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Python library integrating with a web app",
        "Build an SDK compatible with a web app",
        "Build a package used with a web app",
    ],
)
def test_integration_consumers_are_not_co_products(goal: str) -> None:
    """R31 probe: compatibility/integration relations name a consumer,
    not a co-produced web artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="An importable package with a public API")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app, not a REST API",
        "Build a web app without a REST API",
        "Build a web app rather than a web service",
        "Build a web app with no HTTP server",
    ],
)
def test_denied_service_vocabulary_is_not_service_evidence(goal: str) -> None:
    """R32 probe: web-service signals share the goal-side negation
    discipline."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login page with signup form and navigation")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Python library with documentation for web apps",
        "Build a Python library with plugins for web apps",
        "Build an SDK plus adapters for web apps",
    ],
)
def test_coordinated_target_phrases_are_not_co_products(goal: str) -> None:
    """R32 probe: a with/plus phrase whose web mention is the TARGET of
    documentation/plugins/adapters is not a co-produced app."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="An importable package with a public API")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


# ---------------------------------------------------------------------------
# Adversarial pre-probe pins (#1813 W1): confirmed violations found by
# bot-style probing ahead of review, fixed proactively.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected"),
    [
        (
            "Build a web app built around the Stripe SDK",
            "Login form with an account settings panel",
            "Browser",
            TaskClass.WEB_APP,
        ),
        (
            "Build a backend serving predictions via a REST API",
            "JSON responses for clients",
            None,
            TaskClass.WEB_SERVICE,
        ),
        (
            "Build a CLI wrapping the GitHub REST API",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            TaskClass.CLI,
        ),
        (
            "Build a Python client library wrapping the GitHub REST API",
            "An importable package with a public API",
            None,
            TaskClass.LIBRARY,
        ),
        (
            "Build a REST API compatible with the existing deploy CLI",
            "Multiple REST endpoints returning JSON body responses",
            None,
            TaskClass.WEB_SERVICE,
        ),
    ],
)
def test_wrapping_and_platform_relations_stay_consumed(goal, outputs, runtime, expected) -> None:
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    if runtime:
        _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Build an uptime monitor for our marketing website",
            "Status page checks recorded hourly; broken pages flagged in the summary",
            "Scheduled cron job on a Linux host",
        ),
        (
            "Build a web spider that indexes marketing websites",
            "Landing pages catalogued per domain with word counts",
            None,
        ),
        (
            "Write end-to-end browser tests for the checkout flow",
            "Checkout form regressions detected nightly",
            "CI pipeline running headless Chromium",
        ),
        (
            "Build a desktop tool with a web-app-style interface",
            "Native desktop window listing local files",
            "Native desktop runtime",
        ),
    ],
)
def test_monitoring_and_desktop_tools_never_become_web_apps(goal, outputs, runtime) -> None:
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    if runtime:
        _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs", "expected"),
    [
        (
            "Build an accessibility checker for corporate websites",
            "WCAG violations summarized per landing page in an HTML report",
            TaskClass.CLI,
        ),
        (
            "Build a packaging CLI and upload the docs to our website",
            "Deterministic stdout and exit code 0",
            TaskClass.CLI,
        ),
        (
            "Build a CLI to scaffold and deploy a web app",
            "Deterministic stdout and exit code 0",
            TaskClass.CLI,
        ),
        (
            "Build a link checker CLI that scans docs and reports broken website links",
            "Deterministic stdout and exit code 0",
            TaskClass.CLI,
        ),
        (
            "Build a frontend deployment CLI",
            "Deployment status printed to stdout and exit code 0",
            TaskClass.CLI,
        ),
        (
            "Build a website generator CLI",
            "Generated HTML files written to disk; build summary printed to stdout",
            TaskClass.CLI,
        ),
        (
            "Build a website uptime monitoring CLI",
            "Uptime results printed to stdout with exit code 0",
            TaskClass.CLI,
        ),
        (
            "Build a web app scaffolding CLI",
            "Generated project files and a summary printed to stdout",
            TaskClass.CLI,
        ),
        (
            "Build a single-page application audit tool",
            "Accessibility findings listed per route",
            TaskClass.CLI,
        ),
    ],
)
def test_web_modifiers_of_cli_heads_stay_cli(goal, outputs, expected) -> None:
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    ("goal", "outputs", "expected"),
    [
        (
            "Refactor the frontend authentication code into smaller modules, preserving behavior",
            "Same public behavior; modules extracted under src/auth",
            TaskClass.REFACTOR_IN_PLACE,
        ),
        (
            "Build a 2D platformer game hosted on my website",
            "Playable game loop with sprites and collision detection",
            TaskClass.GAME_2D,
        ),
    ],
)
def test_adjacent_classes_keep_ownership_over_web_modifiers(goal, outputs, expected) -> None:
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Python library alongside an admin web app",
        "Build a Python library as well as an admin web app",
    ],
)
def test_routine_coordination_forms_are_co_products(goal: str) -> None:
    """R33 probe: alongside / as well as coordinate co-products, and the
    library conjunct keeps its own goal evidence."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Generated documentation and an admin portal")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes
    assert TaskClass.LIBRARY in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Python library & an admin web app",
        "Build a Python library in addition to an admin web app",
        "Build a Python library but also an admin web app",
    ],
)
def test_ampersand_and_additive_coordinators_declare_co_products(goal: str) -> None:
    """R34 probe: routine explicit coordinators declare co-products."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Generated documentation and an admin portal")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes
    assert TaskClass.LIBRARY in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build either a Python library or an admin web app",
        "Build a Python library or an admin web app",
    ],
)
def test_alternative_requirements_keep_both_candidates(goal: str) -> None:
    """R35 probe: explicit alternatives are an honest ambiguity, not a
    last-phrase win."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Generated documentation and an admin portal")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes
    assert TaskClass.LIBRARY in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI for frontend developers although not a web app",
        "Build a CLI for frontend developers though not a web app",
    ],
)
def test_although_and_though_end_content_clauses(goal: str) -> None:
    """R35 probe: although/though are adversative boundaries like but."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive dashboard panels and deterministic stdout")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI, not a browser dashboard",
        "Build a CLI, not a web app because of accessibility constraints",
    ],
)
def test_denied_ui_product_heads_and_reason_clauses_dominate(goal: str) -> None:
    """R36 probe: the denied artifact head is parsed independently of
    trailing reason clauses, and supported UI product heads (dashboard)
    count as artifact denials."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login page and settings panel shown in the browser")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Build a desktop settings tool",
            "Settings panel interoperates with web apps",
            "Native desktop runtime",
        ),
        (
            "Build a sync CLI",
            "Config summary compatible with web apps, printed to stdout",
            "Local shell / terminal",
        ),
    ],
)
def test_output_side_consumer_relations_are_not_browser_context(goal, outputs, runtime) -> None:
    """R36 probe: outputs/runtime browser evidence routes through the same
    relationship-aware ownership rules — interop/compatibility targets are
    consumers, not the produced artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "runtime",
    [
        "The browser is not supported; native desktop runtime",
        "Browser support disabled; native desktop runtime",
    ],
)
def test_postpositive_browser_denials_are_not_context(runtime: str) -> None:
    """R37 probe: denials where the cue follows the browser term."""
    ledger = _bare_ledger("Build a desktop settings tool")
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "runtime",
    [
        "Browser functionality has been disabled; native desktop runtime",
        "Browser use is prohibited; native desktop runtime",
        "Browser access has been removed; native desktop runtime",
    ],
)
def test_postpositive_denials_with_auxiliaries(runtime: str) -> None:
    """R39 probe: perfect/passive auxiliaries and prohibition verbs."""
    ledger = _bare_ledger("Build a native desktop settings tool")
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a native desktop settings tool designed for mobile and web apps",
        "Build a native desktop settings tool intended for use with web apps",
        "Build a native desktop settings tool usable by web apps",
        "Build a native desktop settings tool for use in browsers",
    ],
)
def test_designed_for_and_usable_by_are_consumer_relations(goal: str) -> None:
    """R39 probe: design/intent/usability targets are consumers or
    deployment compatibility, not the produced artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app that uses a hosted command line service",
        "Build a web app dependent on a REST API",
    ],
)
def test_uses_and_dependent_on_are_consumed_dependencies(goal: str) -> None:
    """R39 probe: uses / dependent on name consumed dependencies."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login page with settings panel and navigation")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build a desktop tool; web apps are unsupported", "Native desktop runtime"),
        ("Build a desktop tool", "Browser support is explicitly disabled; native desktop runtime"),
        ("Build a desktop tool", "Browser access is denied; native desktop runtime"),
    ],
)
def test_postpositive_denials_cover_goal_and_predicates(goal: str, runtime: str) -> None:
    """R40 probe: postpositive denial normalization applies to the goal
    surface too, and covers adverbs and denial predicates."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a native desktop settings tool for web apps",
        "Build a native desktop settings tool for browsers",
        "Build a plugin available to web apps",
    ],
)
def test_plain_audience_targets_are_consumer_relations(goal: str) -> None:
    """R40 probe: a bare for/available-to web target names the audience."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "outputs",
    ["Settings panel syncs with web apps", "Settings panel connects to web apps"],
)
def test_sync_and_connect_relations_are_integration_targets(outputs: str) -> None:
    """R42 probe: sync/connect name integration targets."""
    ledger = _bare_ledger("Build a native desktop settings tool")
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a native desktop settings tool; browser apps won't be supported",
        "Build a native desktop settings tool; browser apps should not be supported",
    ],
)
def test_modal_postpositive_denials(goal: str) -> None:
    """R43 probe: modal denial forms compose like copular ones."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a native desktop settings tool for people interested in web apps",
        "Build a native desktop settings tool whose users build web apps",
        "Build a native desktop settings tool marketed to web app developers",
    ],
)
def test_indirect_audience_descriptions(goal: str) -> None:
    """R43 probe: interest, possessive-user, and marketing forms all
    describe the audience."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a native desktop companion used alongside web apps",
        "Build a native desktop companion installed into web apps",
    ],
)
def test_companion_and_installation_relations(goal: str) -> None:
    """R43 probe: alongside/installation targets are deployment
    compatibility."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel for local files")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a browser-based admin portal",
            "Role management and account administration",
        ),
        (
            "Build a browser admin console",
            "Manage accounts and permissions",
        ),
    ],
)
def test_browser_qualified_ui_heads_own_the_artifact(goal: str, outputs: str) -> None:
    """R44 probe: ordinary browser-qualified UI heads (portal, console)
    are affirmative ownership even when the outputs use no widget-catalog
    wording."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a desktop app that mimics a web app",
        "Build a native desktop companion that runs inside web apps",
        "Build a native desktop companion embedded within web apps",
    ],
)
def test_comparison_and_hosting_relations_are_not_ownership(goal: str) -> None:
    """R44 probe: imitation names a reference artifact and inside/within
    name an execution host — neither is the produced artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with local preferences")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI targeting browser admin portals",
        "Build a browser admin portal generator CLI",
    ],
)
def test_qualified_ui_heads_respect_relations_and_finality(goal: str) -> None:
    """R44 guard: the qualified-head grant cannot be fed by a relational
    browser mention, and a non-final head keeps its own artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Deterministic stdout and exit code 0")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal for the browser",
        "Build an admin portal that runs in the browser",
        "Build an admin console intended for browser use",
        "Build a customer portal accessible from a browser",
        "Build a billing dashboard available in the browser",
        "Build an account console served through the browser",
    ],
)
def test_postnominal_browser_qualifiers_own_the_artifact(goal: str) -> None:
    """R45 probe: ownership is word-order independent — a UI product head
    followed by a browser runtime/availability qualifier owns the
    artifact even when the outputs use no widget-catalog wording."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build an admin portal generator for the browser", "Local shell / terminal"),
        ("Build an admin portal that does not run in the browser", "Native desktop runtime"),
    ],
)
def test_postnominal_qualifiers_respect_reheading_and_negation(goal: str, runtime: str) -> None:
    """R45 guard: an artifact noun between head and qualifier re-heads
    the phrase, and a negated runtime qualifier is not ownership."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Deterministic generation logs")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web-based admin portal",
        "Build an admin portal that runs in a web browser",
        "Build an admin portal that runs in modern browsers",
    ],
)
def test_equivalent_browser_spellings_own_the_artifact(goal: str) -> None:
    """R46 probe: "web-based" and modified browser environments are the
    same ownership as the bare-browser forms."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build an admin portal for the browser extension", "Browser extension runtime"),
        (
            "Build a native admin console for the browser automation suite",
            "Native desktop runtime",
        ),
    ],
)
def test_trailing_artifact_nouns_reject_the_environment_reading(goal: str, runtime: str) -> None:
    """R46 guard: the environment phrase must end at the browser token —
    a trailing noun re-heads it into a consumed artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal, accessible in the browser",
        "Build an admin portal, which runs in the browser",
    ],
)
def test_comma_introduced_qualifiers_keep_ownership(goal: str) -> None:
    """R47 probe: a comma before the qualifier introduces a dependent
    qualifier, not a co-product conjunct."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Build an admin portal accessibility audit for the browser",
            "Audit findings and risk scores",
            "Local report generation",
        ),
        (
            "Build admin portal documentation for the browser",
            "Reference pages and usage guides",
            "Local documentation build",
        ),
        (
            "Build an admin portal test harness for the browser",
            "Deterministic test verdicts",
            "Local shell / terminal",
        ),
    ],
)
def test_nominal_tokens_after_the_ui_head_reject_ownership(
    goal: str, outputs: str, runtime: str
) -> None:
    """R47 guard: the head-to-environment span is predicative — a nominal
    token after the UI head re-heads the phrase (audit, documentation,
    test harness), regardless of any artifact-noun enumeration."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal for use in browsers",
        "Build an admin portal designed to run in browsers",
        "Build an admin portal built to operate in a web browser",
    ],
)
def test_infinitival_and_for_use_qualifiers_own_the_artifact(goal: str) -> None:
    """R48 probe: "for use in" and closed-set runtime infinitives declare
    the same environment as the direct prepositional forms."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Build an admin portal for use in browser extensions",
            "Role management and account administration",
            "Browser extension runtime",
        ),
        (
            "Build an admin portal designed to run in the browser automation suite",
            "Automation run summaries",
            "Local shell / terminal",
        ),
    ],
)
def test_infinitival_qualifiers_still_respect_reheading(
    goal: str, outputs: str, runtime: str
) -> None:
    """R48 guard: the infinitival forms compose with the trailing-noun
    rejection — a re-headed environment stays a consumed artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build documentation about an admin portal that runs in browsers",
        "Build a guide about a dashboard for browsers",
    ],
)
def test_subject_relationships_block_postnominal_ownership(goal: str) -> None:
    """R49 guard: "about" marks the UI noun as subject matter of another
    artifact — documentation about a portal is not the portal."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Reference pages and usage guides")
    _seed_section(ledger, "runtime_context", value="Local documentation build")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal that should run in browsers",
        "Build an admin portal designed to be used in browsers",
        "Build an admin portal intended to be accessible in browsers",
    ],
)
def test_modal_and_passive_infinitive_qualifiers_own_the_artifact(goal: str) -> None:
    """R49 probe: modal and passive-infinitive equivalents of the runtime
    qualifier are the same environment declaration."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build documentation introducing an admin portal that runs in browsers",
        "Build documentation presenting a billing dashboard for browsers",
    ],
)
def test_embedded_noun_phrases_block_postnominal_ownership(goal: str) -> None:
    """R50 guard: a determiner deeper in the chain opens an embedded noun
    phrase — the UI noun is a relationship target no matter which verb
    introduced it."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Reference guide and examples")
    _seed_section(ledger, "runtime_context", value="Local documentation build")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal expected to run in browsers",
        "Build an admin portal required to run in browsers",
        "Build an admin portal that needs to run in browsers",
    ],
)
def test_obligation_qualifiers_own_the_artifact(goal: str) -> None:
    """R50 probe: obligation and expectation constructions are the same
    environment declaration as the plain runtime forms."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Create a report evaluating dashboards used in browsers",
            "Risk scores and recommendations",
            "Local batch job",
        ),
        (
            "Build documentation summarizing admin portals available in browsers",
            "Reference guide and examples",
            "Local documentation build",
        ),
    ],
)
def test_unknown_participles_read_as_verbal_subjects(goal: str, outputs: str, runtime: str) -> None:
    """R51 guard: the attributive slot is a nominal-gerund allowlist — an
    unknown participle is verbal by default, so evaluation and summary
    subjects block without enumeration."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal that has to run in browsers",
        "Build an admin portal that ought to run in browsers",
    ],
)
def test_auxiliary_obligation_qualifiers_own_the_artifact(goal: str) -> None:
    """R51 probe: "has to" and "ought to" compose like the other
    obligation auxiliaries."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "I want to build an admin portal that runs in browsers",
        "Build a secure internal role management admin portal that runs in browsers",
        "Build an admin portal that is mandated to run in browsers",
    ],
)
def test_wrappers_long_modifiers_and_mandates_own_the_artifact(goal: str) -> None:
    """R52 probe: intent wrappers, long modifier chains, and obligation
    synonyms do not change what is produced."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected"),
    [
        (
            "Migrate from a legacy script to a CLI",
            "Converted workflows with progress logs",
            "Runs on developer machines",
            TaskClass.CLI,
        ),
        (
            "Convert the old tool to a CLI",
            "Converted workflows with progress logs",
            "Runs on developer machines",
            TaskClass.CLI,
        ),
        (
            "Migrate the old endpoint to a REST API",
            "JSON responses with status codes",
            "Containerized HTTP service",
            TaskClass.WEB_SERVICE,
        ),
    ],
)
def test_migration_destinations_are_produced_artifacts(
    goal: str, outputs: str, runtime: str, expected: TaskClass
) -> None:
    """R52 probe: after a migration/conversion verb, "to <artifact>"
    names the produced destination, not a consumed dependency."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal compatible with modern browsers",
        "Build an admin portal available across modern browsers",
        "Build an admin portal supporting modern browsers",
    ],
)
def test_compatibility_declarations_with_browser_targets_own(goal: str) -> None:
    """R53 probe: compatibility/support wording with a bare browser
    target is environment ownership; web-app targets stay consumers."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected"),
    [
        (
            "Adapt the existing script to a CLI",
            "Converted workflows with progress logs",
            "Runs on developer machines",
            TaskClass.CLI,
        ),
        (
            "Promote the existing script to a CLI",
            "Converted workflows with progress logs",
            "Runs on developer machines",
            TaskClass.CLI,
        ),
        (
            "Adapt the old endpoint to a REST API",
            "JSON responses with status codes",
            "Containerized HTTP service",
            TaskClass.WEB_SERVICE,
        ),
    ],
)
def test_destination_ownership_is_verb_independent(
    goal: str, outputs: str, runtime: str, expected: TaskClass
) -> None:
    """R53 probe: destination ownership rests on structure — a
    segment-initial non-production imperative with a definite object —
    not on which transformation verb was chosen."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    "goal",
    [
        "Build an admin portal that must function in browsers",
        "Build an admin portal that shall function in browsers",
    ],
)
def test_direct_modal_runtime_predicates_own_the_artifact(goal: str) -> None:
    """R54 probe: a modal directly governing a runtime verb is the same
    environment declaration as its to-infinitive equivalent — the runtime
    vocabulary is one shared set across grammar positions."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "excluded"),
    [
        ("Submit the credentials to the public API", "Submission confirmations", TaskClass.LIBRARY),
        ("Upload the artifact to the public API", "Upload confirmations", TaskClass.LIBRARY),
        ("Send the request to the REST API", "Delivery confirmations", TaskClass.WEB_SERVICE),
        ("Forward the event to the REST API", "Delivery confirmations", TaskClass.WEB_SERVICE),
    ],
)
def test_definite_destinations_are_transfers_not_transformations(
    goal: str, outputs: str, excluded: TaskClass
) -> None:
    """R54 guard: a transformation destination is a NEW artifact and
    takes an indefinite introduction ("to a CLI"); a definite destination
    ("to the public API") names an existing endpoint being addressed."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs on developer machines")
    result = derive_domain_from_ledger(ledger)
    assert excluded not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a static website generator CLI",
            "Generates a responsive landing page with a login form",
        ),
        (
            "Build a landing page scaffolder CLI",
            "Generates responsive landing pages with signup forms",
        ),
    ],
)
def test_generator_clis_keep_their_class_despite_widget_outputs(goal: str, outputs: str) -> None:
    """R57 guard: the shape gate shares final-head semantics with the
    direct grant — a generator/scaffolder CLI's widget-rich outputs
    describe what it produces, not what it is."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build an Electron desktop settings dashboard", "Embedded browser runtime in Electron"),
        ("Build a form automation harness", "Browser automation suite runtime"),
        ("Build a toolbar surface", "Browser plugin host"),
    ],
)
def test_reheaded_and_embedded_browser_runtimes_are_not_environments(
    goal: str, runtime: str
) -> None:
    """R58 guard: the strict environment reading applies on every
    browser-context path — an embedded or re-headed browser phrase is a
    component of another artifact even with UI-shaped outputs."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with account form")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Our deliverable should be an admin portal that runs in browsers",
        "This should be an admin portal that runs in browsers",
        "The goal is to build an admin portal that runs in browsers",
    ],
)
def test_specificational_and_intent_frames_own_the_artifact(goal: str) -> None:
    """R58 probe: possessive/demonstrative subjects and intent
    infinitives are the same specificational declaration as the
    "the product is" frame."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build a mobile app that embeds a web browser", "Native iOS runtime"),
        ("Build a desktop shell that contains a browser", "Native desktop runtime"),
    ],
)
def test_active_containment_of_a_browser_is_consumption(goal: str, runtime: str) -> None:
    """R59 guard: an artifact that embeds or contains a browser hosts a
    component — it is not the browser product."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with login form")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build a desktop wrapper around an existing website", "Native desktop runtime"),
        ("Build a mobile container around an existing PWA", "Native iOS runtime"),
        ("Build a native shell built around a web app", "Native desktop runtime"),
    ],
)
def test_native_wrappers_around_web_products_consume_them(goal: str, runtime: str) -> None:
    """R119 guard: the native wrapper/container/shell is the produced
    artifact; the existing web product after AROUND is its dependency."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Native window with settings panel and navigation")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a browser-based admin panel", "Login and role controls"),
        ("Build a browser-based signup form", "Client-side validation messages"),
    ],
)
def test_panel_and_form_heads_own_the_artifact(goal: str, outputs: str) -> None:
    """R59 probe: panel and form are direct UI product heads like page
    and dashboard."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Build a browser extension settings page",
            "Settings panel with permission toggles",
            "Browser extension runtime",
        ),
        (
            "Build a browser plugin admin page",
            "Role controls with an admin panel",
            "Browser plugin host",
        ),
    ],
)
def test_component_nouns_rehead_the_browser_qualifier(
    goal: str, outputs: str, runtime: str
) -> None:
    """R60 guard: extension/plugin nouns re-head the browser word — a
    component surface is not a standalone browser application, even with
    widget-rich outputs."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "excluded"),
    [
        ("Build a REST API documentation website", TaskClass.WEB_SERVICE),
        ("Build a CLI documentation website", TaskClass.CLI),
    ],
)
def test_attributive_artifact_mentions_follow_the_final_head(
    goal: str, excluded: TaskClass
) -> None:
    """R61 probe: the final-head rule is shared across classes — a
    documentation website about an API or CLI is the website."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Reference pages with navigation and search panel")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP
    assert excluded not in result.classes


@pytest.mark.parametrize(
    "goal",
    ["Build a recruiting dashboard", "Build a fundraising portal"],
)
def test_phrase_initial_participles_are_attributive(goal: str) -> None:
    """R62 probe: an unknown participle in phrase-initial position is a
    nominal modifier — position decides, not a vocabulary list."""
    ledger = _bare_ledger(goal)
    _seed_section(
        ledger, "outputs", value="Interactive account table, settings panel, and navigation bar"
    )
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    ["Build a plugin management dashboard", "Build an automation reporting dashboard"],
)
def test_component_subjects_do_not_veto_ui_products(goal: str) -> None:
    """R62 probe: a component word owns only in a browser-component
    compound — a dashboard that manages or reports on components is
    still the dashboard."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive panel with navigation")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a browser-based kanban board",
        "Build a web-based survey builder",
        "Build an online spreadsheet that runs in browsers",
        "Build an internal admin workspace that runs in browsers",
    ],
)
def test_non_enumerated_product_heads_own_with_browser_evidence(goal: str) -> None:
    """R63 probe: the shape gate is default-allow — legitimate UI product
    nouns need no pre-enumeration; only report/tool/component-class
    heads keep their own identity."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Users can drag and drop cards between columns")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Chrome extension settings page",
        "Build a Firefox add-on options page",
        "Build a Safari extension configuration panel",
        "Build a Chrome plugin popup page",
    ],
)
def test_brand_qualified_components_own_their_surfaces(goal: str) -> None:
    """R63 guard: component ownership is positional — a qualified
    component (browser word or brand) owns its surfaces, so extension
    and plugin pages stay outside web_app."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive settings panel with navigation and buttons")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Create browser-based kanban board", "Users can drag and drop cards between columns"),
        ("Build online spreadsheet that runs in browsers", "Editable table with formula bar"),
    ],
)
def test_articleless_imperative_goals_reach_their_product_np(goal: str, outputs: str) -> None:
    """R64 probe: content after the imperative marks the product NP —
    routine articleless goals own like their articled equivalents."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a Firefox sidebar for tab management", "Interactive tab list with pinned groups"),
        (
            "Build a Chrome popup for bookmark management",
            "Interactive bookmark list with folders panel",
        ),
    ],
)
def test_native_component_surfaces_stay_outside_web_app(goal: str, outputs: str) -> None:
    """R64 guard: qualified sidebar/popup heads are native browser
    component surfaces, analogous to extension and plugin pages."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a sidebar for Firefox", "Interactive tab list with pinned groups"),
        ("Build a popup for Chrome", "Interactive bookmark list with folders panel"),
        ("Build a context menu for Chrome", "Interactive action list with shortcuts"),
    ],
)
def test_component_heads_own_regardless_of_qualifier_order(goal: str, outputs: str) -> None:
    """R65 guard: a component noun in head position is component-owned —
    "a sidebar for Firefox" is the same surface as "a Firefox
    sidebar"."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a desktop app that opens browser pages", "Settings panel with recent files list"),
        ("Build a dashboard about browser performance", "Interactive charts with a filters panel"),
    ],
)
def test_manipulated_and_subject_browser_mentions_are_not_context(goal: str, outputs: str) -> None:
    """R66 guard: a browser mention that is a manipulation target or a
    subject-matter topic is not the artifact's environment."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Desktop")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a desktop dashboard that lists websites", "Interactive list with a filters panel"),
        (
            "Build a desktop bookmark manager that organizes websites",
            "Folder tree with a settings panel",
        ),
    ],
)
def test_relative_clause_content_is_not_browser_context(goal: str, outputs: str) -> None:
    """R67 guard: an action relative clause names the artifact's content
    ("that lists websites"); copular clauses keep stating identity."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Desktop")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings dashboard within our Chrome browser extension",
        "Build a settings dashboard for Chrome's browser extension",
        "Build a settings dashboard for our company browser extension",
    ],
)
def test_possessive_component_targets_own_their_surfaces(goal: str) -> None:
    """R67 guard: possessive and brand-qualified component phrases are
    the same component target as the plain-article forms."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build an extension settings page",
        "Build a plugin options page",
        "Build our extension's settings dashboard",
    ],
)
def test_component_first_surfaces_are_component_owned(goal: str) -> None:
    """R68 guard: component ownership is word-order independent — a
    component followed by anything but an activity noun owns its
    surface, and possessives own outright."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a desktop dashboard tracking browser usage",
        "Build a desktop dashboard comparing browser performance",
        "Build a desktop dashboard showing web app uptime",
        "Build a desktop dashboard analyzing frontend bundle sizes",
    ],
)
def test_participial_content_clauses_are_not_browser_context(goal: str) -> None:
    """R68 guard: a participle taking an object names the artifact's
    data; one followed by a preposition declares its environment and
    keeps its signal."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Desktop application")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings page attached to a browser extension",
        "Build a settings page used by a browser extension",
        "Build a settings page belonging to a browser extension",
        "Build a settings page alongside a browser extension",
    ],
)
def test_relational_component_targets_own_their_surfaces(goal: str) -> None:
    """R69 guard: any relational introduction reaches the component —
    attached/belonging to, used by, alongside — not only the plain
    prepositions."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a desktop dashboard focused on browser crashes",
        "Build a desktop dashboard centered on browser security findings",
        "Build a desktop dashboard devoted to web app incident reports",
    ],
)
def test_topical_predicates_are_not_browser_context(goal: str) -> None:
    """R70 guard: an adjective/participle plus its preposition opens a
    topic — everything to the clause boundary is subject matter."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Desktop application")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build the preferences UI exposed through a browser extension",
        "Build a settings page hosted through a browser extension",
        "Build a settings page associated with the installed browser extension",
    ],
)
def test_participial_component_relations_own_their_surfaces(goal: str) -> None:
    """R70 guard: participial relations (exposed through, hosted
    through, associated with) reach the component like the bare
    prepositions, while bare "with" stays outside the veto."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a desktop dashboard on browser crash reports",
        "Build a desktop dashboard concerned with browser crash reports",
    ],
)
def test_bare_on_topics_are_not_browser_context(goal: str) -> None:
    """R71 guard: a bare on-phrase is topical unless its object ends at
    a browser token — "on browser crash reports" is subject matter,
    "runs on browsers" stays the environment."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Local desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings page hosted on a browser extension",
        "Build an options page mounted on a browser extension",
        "Build a preferences UI running under a browser extension",
    ],
)
def test_hosting_prepositions_reach_the_component(goal: str) -> None:
    """R71 guard: hosted/mounted ON and running/nested UNDER reach the
    component through their participle, while a bare topical "on" stays
    outside the veto."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a desktop dashboard with browser crash analytics",
        "Build a desktop dashboard with web app incident metrics",
    ],
)
def test_with_phrase_topics_are_not_browser_context(goal: str) -> None:
    """R72 guard: a with-phrase is topical unless its object ends at a
    web signal — quality clauses ("with no errors in the web UI") and
    compatibility targets keep their reading."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Local desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings page on a browser extension",
        "Build a preferences UI under a browser extension",
    ],
)
def test_determined_host_prepositions_reach_the_component(goal: str) -> None:
    """R72 guard: a bare host preposition with a determiner marks a host
    instance ("on a browser extension"); the determinerless form stays
    topical."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a browser crash dashboard",
        "Build a browser security dashboard",
        "Build a web app incident dashboard",
    ],
)
def test_prenominal_subject_compounds_are_not_execution_qualifiers(goal: str) -> None:
    """R73 guard: a subject/event noun after the browser qualifier
    re-heads it into topic vocabulary — "browser crash dashboard"
    reports on crashes while "browser admin console" stays a console."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Local desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a local desktop browser cookie dashboard",
        "Build a local desktop browser privacy dashboard",
    ],
)
def test_runtime_is_the_authority_over_goal_prose(goal: str) -> None:
    """R74 guard: a competing environment qualifier voids the browser
    qualifier inside the NP, and an explicitly non-browser standardized
    runtime bars goal prose from supplying browser context."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive cookie charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Local desktop application, no browser runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings page on Chrome browser extension",
        "Build a settings page on Firefox browser extension",
    ],
)
def test_brand_qualified_determinerless_hosts_reach_the_component(goal: str) -> None:
    """R74 guard: a singular host object reaches the component with or
    without a determiner; the topical plural stays outside the veto."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings page packaged as a Chrome browser extension",
        "Build a settings page distributed as a Firefox browser add-on",
    ],
)
def test_identity_relations_reach_the_component(goal: str) -> None:
    """R75 guard: "packaged/distributed as" declares the artifact's
    identity — the surface belongs to the component it ships as."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    ["Build a browser certificate dashboard", "Build a browser policy dashboard"],
)
def test_plain_noun_browser_compounds_need_section_confirmation(goal: str) -> None:
    """R76 guard: a bare browser token re-headed by a plain noun is a
    topic compound structurally — no vocabulary list — while gerund and
    hyphen compounds ("monitoring", "vector-scene") keep ownership."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a local dashboard; browser UI is outside scope",
        "Build a dashboard; browser support is outside the scope",
        "Build a dashboard; exclude the browser UI",
        "Build a browser-free dashboard",
    ],
)
def test_productive_exclusion_forms_deny_the_browser(goal: str) -> None:
    """R76 guard: outside-scope, imperative exclude, and the -free
    suffix all remove the browser signal."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a settings page. It is a Chrome browser extension.",
        "Build a settings page; it ships as a Chrome browser extension.",
        "Build a settings page that is a Chrome browser extension",
    ],
)
def test_component_identity_resolves_across_the_whole_goal(goal: str) -> None:
    """R76 guard: copular and later-clause identity declarations reach
    the component before the web-app grant."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app; browsers are unsupported",
        "Build a web app; browser execution is disabled",
        "Build a website; browser use is forbidden",
    ],
)
def test_postpositive_goal_denials_precede_every_grant(goal: str) -> None:
    """R77 guard: an explicit postpositive exclusion bars the class even
    when the goal also names a web artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


def test_companion_component_runtimes_do_not_veto() -> None:
    """R78 guard: a companion component in the runtime is a co-product,
    not the runtime's identity — explicit web ownership survives."""
    ledger = _bare_ledger("Build a web app and a companion browser extension")
    _seed_section(ledger, "outputs", value="Login form with a settings panel shown in the browser")
    _seed_section(ledger, "runtime_context", value="Browser app plus companion extension runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


@pytest.mark.parametrize(
    "runtime",
    [
        "Browser extension runtime and standalone website runtime",
        "Standalone website runtime and browser extension runtime",
    ],
)
def test_two_product_runtimes_are_order_independent(runtime: str) -> None:
    """R79 guard: the component owns only when no browser/web identity
    survives outside its noun phrase — whichever side is named first."""
    ledger = _bare_ledger("Build a standalone website and a browser extension")
    _seed_section(ledger, "outputs", value="Login form with a settings panel shown in the browser")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app and a native desktop client where embedded browsers are disabled",
        "Build a web app and a compatibility report; unsupported browsers are excluded from the report",
    ],
)
def test_conjunct_scoped_denials_do_not_erase_the_web_app(goal: str) -> None:
    """R79 guard: a where-clause denial and a from-scoped exclusion
    modify their own conjunct, not the whole ledger."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login form with a settings panel shown in the browser")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


def test_prepositional_scope_tails_keep_the_web_app() -> None:
    """R80 guard: any prepositional tail names the denial's own scope —
    "unsupported for the desktop client" belongs to the client."""
    ledger = _bare_ledger(
        "Build a web app and a desktop client; browsers are unsupported for the desktop client"
    )
    _seed_section(ledger, "outputs", value="Login form with a settings panel shown in the browser")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app to manage browser extensions",
        "Build a web app for managing browser extensions",
    ],
)
def test_managed_components_are_content_not_identity(goal: str) -> None:
    """R81 guard: an infinitive purpose or manipulation verb marks the
    component as the web app's managed content — the produced artifact
    keeps its class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Extension catalog with install buttons")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app; browsers are unsupported in production",
        "Build a web app; browser support is unavailable for now",
    ],
)
def test_condition_qualified_exclusions_still_deny(goal: str) -> None:
    """R81 guard: a condition/time qualifier ("in production", "for
    now") is not a scope object — the exclusion stays ledger-wide."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Native desktop runtime")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app for configuring browser extensions",
        "Build a web app for enabling browser extensions",
        "Build a web app for comparing browser extensions",
    ],
)
def test_gerund_targets_are_managed_content(goal: str) -> None:
    """R82 guard: a gerund in the target NP marks the component as
    managed content whatever the activity — structural, no verb list."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Extension catalog with install buttons")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app; browsers are unsupported when offline",
        "Build a web app; browsers are unsupported if JavaScript is disabled",
    ],
)
def test_conditional_clauses_qualify_rather_than_deny(goal: str) -> None:
    """R82 guard: when/if clauses constrain operating conditions — they
    do not deny that the artifact is a web app."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app alongside a browser extension",
        "Build a web app as well as a browser extension",
        "Build a web app for users of browser extensions",
        "Build a web app showing the status of browser extensions",
    ],
)
def test_association_relations_spare_explicit_web_artifacts(goal: str) -> None:
    """R83 guard: an explicit web artifact before an association
    relation is an independent co-product, audience, or content owner —
    only identity relations re-head it."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login form with a settings panel shown in the browser")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app; browser support is unavailable to guest users",
        "Build a web app; browser support is unavailable until sign-in",
    ],
)
def test_audience_and_temporal_limits_preserve_ownership(goal: str) -> None:
    """R84 guard: an availability limit scoped to an audience ("to
    guest users") or a condition ("until sign-in") does not deny the
    artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build a browser menu settings page", "Browser menu runtime"),
        ("Build a web app that is a browser pop-up", "Runs in browsers"),
    ],
)
def test_menu_and_popup_components_share_the_vocabulary(goal: str, runtime: str) -> None:
    """R84 guard: menus and pop-ups are components in every identity
    decision — one vocabulary SSOT."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app; browsers are unavailable because the backend is offline",
        "Build a web app; browsers are unavailable since authentication is down",
        "Build a web app; browsers are unavailable due to maintenance",
    ],
)
def test_causal_availability_conditions_preserve_ownership(goal: str) -> None:
    """R85 guard: causal qualifiers (because/since/due to) explain an
    availability condition — they do not deny the artifact."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a Chrome browser action settings page",
        "Build a Chrome page action popup",
    ],
)
def test_browser_action_surfaces_are_components_under_generic_runtimes(goal: str) -> None:
    """R86 guard: browser-action and page-action surfaces are components
    in the shared vocabulary — a generic browser runtime cannot hand
    them standalone web-app semantics."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "runtime",
    ["Tracks user actions in real time", "Records user actions for analytics"],
)
def test_ordinary_user_actions_are_not_component_identity(runtime: str) -> None:
    """R87 guard: action surfaces are components only in their qualified
    forms — bare "actions" in runtime prose is ordinary vocabulary."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value="Settings panel with save button")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "runtime",
    [
        "Browser automation via Playwright for smoke tests",
        "Chrome browser driver via Selenium",
        "Playwright automation in CI",
    ],
)
def test_verification_tooling_is_not_the_artifacts_identity(runtime: str) -> None:
    """R88 guard: test tooling in the runtime verifies the explicitly
    requested web app — it is not the app; surface components stay
    authoritative."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value="Interactive signup page")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a browser testing framework", "Reusable test fixtures and assertions"),
        ("Build an end-to-end test runner", "Test results and failure reports"),
    ],
)
def test_browser_test_tools_are_not_ui_products(goal: str, outputs: str) -> None:
    """R89 guard: a testing framework or runner under a browser runtime
    is a tool — affirmative UI-product ownership is required, and the
    monitoring/testing DASHBOARD pins keep theirs."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "runtime",
    [
        "Playwright tests with a browser extension",
        "Compatibility tests for Chrome extensions",
        "Compatibility tests for Chrome extensions in CI",
        "Playwright covers a browser extension tested in the nightly suite",
        "Playwright tests exercise a Chrome browser extension installed temporarily",
        "Playwright tests exercise a Chrome browser extension provisioned briefly",
        "Playwright tests exercise a mock Chrome browser extension used by client fixtures",
        "Playwright tests exercise a Chrome browser extension generated for every scenario",
        "Playwright tests exercise a Chrome browser extension reset before each run",
        "Playwright tests exercise a Chrome browser extension configured with seeded accounts"
        " and isolated by the harness for every scenario",
        "Playwright tests exercise a Chrome browser extension configured with seeded accounts"
        " and discarded after every scenario",
        "Playwright tests exercise a Chrome browser extension configured with seeded accounts"
        " that the harness replaces after every scenario",
    ],
)
def test_components_inside_verification_context_do_not_veto(runtime: str) -> None:
    """R89/R94/R96 guard: components used or targeted by verification
    tooling are not the runtime's identity — a verification-owned tail
    ("in CI", "tested in the nightly suite") or a fixture description
    without an ownership anchor ("installed temporarily") is the
    tooling's detail, and the explicit web app keeps its durable
    class."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value="Interactive signup page")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build a browser-based admin portal",
        "Build an admin portal that runs in browsers",
    ],
)
def test_verification_exemption_covers_qualified_web_ownership(goal: str) -> None:
    """R95 guard: the verification exemption follows every affirmative
    web-ownership path — a qualified or postnominal browser goal keeps
    its class when the runtime describes only verification tooling."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive signup page")
    _seed_section(ledger, "runtime_context", value="Playwright tests with a browser extension")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Create a catalog of web apps", "Curated catalog entries"),
        ("Create an inventory of web applications", "Inventory records"),
        ("Write a comparison of web apps", "Comparison document"),
        ("Generate a list of websites", "List of URLs"),
        ("Analyze web apps", "Analysis report"),
    ],
)
def test_web_nouns_as_relation_objects_do_not_own(goal: str, outputs: str) -> None:
    """R101 guard: a noun relation re-heads the phrase — a catalog,
    inventory, list, or comparison OF web apps produces the container,
    and an inspection-verb goal studies its object rather than
    producing it."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Local files")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build a Chrome-compatible admin dashboard", "Chrome and Firefox"),
        ("Build a Chrome-compatible admin dashboard", "Safari"),
        ("Build a Chrome-compatible admin dashboard", "Chromium"),
        ("Build a Firefox admin console", "Firefox"),
    ],
)
def test_concrete_browser_names_are_browser_evidence(goal: str, runtime: str) -> None:
    """R104 guard: concrete browser runtimes declare the same
    environment as "Modern browsers" — the closed name set is ordinary
    browser-UI evidence."""
    ledger = _bare_ledger(goal)
    _seed_section(
        ledger, "outputs", value="Interactive login form, navigation menu, and settings panels"
    )
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Build a Chrome-compatible admin dashboard",
            "Interactive login form, navigation menu, and settings panels",
            "Chrome extension",
        ),
        (
            "Build an interactive desktop dashboard",
            "Interactive charts with a filters panel",
            "Native desktop application; Playwright launches Chrome in CI",
        ),
    ],
)
def test_browser_names_keep_component_and_harness_exclusions(
    goal: str, outputs: str, runtime: str
) -> None:
    """R104 guard: the name vocabulary inherits the existing exclusions —
    a Chrome extension is still a component and a harness-launched
    Chrome is still the test environment."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a reusable widget consumed in web apps",
        "Build a reusable widget used inside web apps",
        "Build a reusable widget loaded by web apps",
        "Build a reusable widget imported by web apps",
        "Build a reusable widget called from web apps",
    ],
)
def test_passive_consumption_relations_do_not_own(goal: str) -> None:
    """R103 guard: passive consumption reads the same through any
    preposition — a widget consumed in, loaded by, imported by, or
    called from web apps is their component, not the app."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Signup form component and settings panel")
    _seed_section(ledger, "runtime_context", value="Python process")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a web app that invokes an existing CLI",
            "Exit code and stdout shown in a status panel",
        ),
        (
            "Build an admin web app",
            "Login page and settings panel; REST API responses displayed to users",
        ),
        ("Build a browser-based dashboard", "SDK authentication token status dashboard"),
    ],
)
def test_displayed_content_carries_no_foreign_ownership(goal: str, outputs: str) -> None:
    """R107 guard: what the app renders from a consumed tool, service, or
    SDK — displayed exit codes, displayed API responses, long library-
    subject compounds ending at a UI head — is content, and the web app
    keeps its clean single class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Generate a fillable PDF",
            "Registration form and submit button",
            "Rendered in Chrome and Firefox",
        ),
        (
            "Build an interactive PDF form",
            "Registration form and submit button",
            "Displayed in modern browsers",
        ),
        (
            "Build an HTML email template",
            "Header banner and CTA button",
            "Rendered in browsers and email clients",
        ),
    ],
)
def test_passively_consumed_documents_are_not_browser_apps(
    goal: str, outputs: str, runtime: str
) -> None:
    """R118 guard: a document rendered, displayed, or presented in a
    browser is browser-consumed, not browser-executed — the passive
    relation covers coordinated consumers too."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a kanban board for CLI users", "Drag-and-drop cards and navigation"),
        ("Build a kanban board showing CLI command examples", "Drag-and-drop cards and navigation"),
        ("Build a kanban board for REST API operators", "Drag-and-drop cards and navigation"),
        ("Build a kanban board showing REST API status", "Drag-and-drop cards and navigation"),
        ("Build a kanban board for teams who use an SDK", "Drag-and-drop cards and navigation"),
        (
            "Build a browser-based spreadsheet showing package metadata",
            "Editable table cells and column filters",
        ),
    ],
)
def test_audience_and_displayed_subjects_own_nothing(goal: str, outputs: str) -> None:
    """R109 guard: audience phrases and displayed-subject participles
    carry CLI/service/library vocabulary without producing those
    artifacts — the custom-headed browser product keeps its clean
    single web_app class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "outputs",
    [
        "Login form that communicates with a REST API",
        "Login form that queries a REST API",
        "Login form that accesses a REST API",
        "Login form that communicates with a payments SDK",
        "Login form that imports a payments SDK",
    ],
)
def test_ui_relative_clause_objects_are_dependencies(outputs: str) -> None:
    """R113 guard: a relative clause whose subject is a UI artifact
    names what the UI acts on — the API or SDK is consumed through
    whatever verb, and the web app keeps its clean single class."""
    ledger = _bare_ledger("Build an admin web app")
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


def test_produced_declarations_in_ui_clauses_still_own() -> None:
    """R113 guard: production verbs are the exception — a dashboard that
    EXPOSES a REST API produces it, keeping the honest ambiguity."""
    ledger = _bare_ledger("Build an admin web app")
    _seed_section(ledger, "outputs", value="Admin pages and a dashboard that exposes a REST API")
    _seed_section(ledger, "runtime_context", value="Modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.classes == frozenset({TaskClass.WEB_APP, TaskClass.WEB_SERVICE})


def test_artifact_defining_library_clauses_still_own() -> None:
    """R109 guard: an artifact-defining clause ("which exposes an SDK")
    is not audience or content — the toolkit keeps its library class."""
    ledger = _bare_ledger("Build a toolkit which exposes an SDK")
    _seed_section(ledger, "outputs", value="Importable package with public API")
    _seed_section(ledger, "runtime_context", value="Local Python process")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    "runtime",
    [
        "Electron desktop runtime embeds Chromium runtime",
        "Electron desktop runtime uses a Chromium runtime",
        "Electron desktop runtime bundles Chromium for rendering",
        "Electron desktop runtime includes Chromium tabs",
    ],
)
def test_contained_engines_are_not_the_environment(runtime: str) -> None:
    """R110 guard: a browser token that is the object of a containment
    or usage verb is the host's bundled engine — the verbal forms behave
    exactly like the fixed-phrase attachments."""
    ledger = _bare_ledger("Build a native desktop dashboard")
    _seed_section(ledger, "outputs", value="Interactive settings form and navigation")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        ("Build a PDF report generator", "Report pages rendered to disk"),
        ("Build an image renderer", "PNG frames written to the output folder"),
        ("Build a documentation compiler", "HTML files rendered from markdown"),
    ],
)
def test_shared_render_vocabulary_needs_positive_game_ownership(goal: str, outputs: str) -> None:
    """R108 guard: render/screen/frame vocabulary owns game_2d only with
    game-domain evidence — not being a web app is not being a game."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Local batch process")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.GAME_2D not in result.classes


def test_later_ui_conjunct_takes_browser_corroboration() -> None:
    """R106 guard: a later UI-headed conjunct gets the same browser
    corroboration as the first noun phrase — a backend with an admin
    dashboard against browsers is an honest service/app ambiguity."""
    ledger = _bare_ledger("Build a backend with an admin dashboard")
    _seed_section(ledger, "outputs", value="REST endpoints and admin pages")
    _seed_section(ledger, "runtime_context", value="Server process and modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.classes == frozenset({TaskClass.WEB_SERVICE, TaskClass.WEB_APP})


@pytest.mark.parametrize(
    "goal",
    [
        "Build a web app in TypeScript",
        "Build a web app using React",
    ],
)
def test_implementation_adjuncts_do_not_rehead(goal: str) -> None:
    """R116 guard: an implementation adjunct after the head is an
    adverbial, not a re-heading compound — the explicit web app owns
    without widget-rich outputs or a runtime to rescue it."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Production bundle emitted to dist")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


def test_neither_nor_reaches_the_cli_path() -> None:
    """R115 guard: the CLI matcher shares the neither/nor denial — the
    denied CLI cannot pollute the library's single class."""
    ledger = _bare_ledger("Build neither a CLI nor a web app; create a library")
    _seed_section(ledger, "outputs", value="Importable package with public API")
    _seed_section(ledger, "runtime_context", value="Local Python process")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime"),
    [
        (
            "Write an interactive document",
            "Document form and confirmation button",
            "Shown through common browsers",
        ),
        (
            "Create an HTML email template",
            "Newsletter layout and call-to-action button",
            "Presented via Chrome, Safari, and Firefox",
        ),
    ],
)
def test_passive_browser_consumption_covers_document_and_email_variants(
    goal: str, outputs: str, runtime: str
) -> None:
    """R118 guard: passive browser consumption does not reclassify a
    document or email whose first NP also contains UI-shaped vocabulary."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "Build a PDF editor web app",
        "Build a browser-based PDF editor",
        "Build a PDF editor for modern browsers",
    ],
)
def test_explicit_browser_owned_pdf_editors_remain_web_apps(goal: str) -> None:
    """R118 control: document subject vocabulary cannot erase an
    explicitly owned web product or browser-qualified interaction head."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Editor canvas, toolbar, and save button")
    _seed_section(ledger, "runtime_context", value="Displayed in modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected_only"),
    [
        (
            "Build a native mobile app with an embedded web UI",
            "Login form and account settings panel",
            "Native iOS runtime",
            None,
        ),
        (
            "Build a CLI with an embedded web UI for help",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            TaskClass.CLI,
        ),
    ],
)
def test_embedded_web_phrases_are_components_not_co_products(
    goal: str, outputs: str, runtime: str, expected_only: TaskClass | None
) -> None:
    """R111 guard: an adjectival containment premodifier ("an embedded
    web UI") marks the web phrase as its host's component — the native
    or CLI host keeps its actual classification."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes
    if expected_only is not None:
        assert result.is_single
        assert result.single is expected_only


@pytest.mark.parametrize(
    "goal",
    [
        "Build a native mobile app with web-style forms",
        "Build a native mobile app with a web-inspired dashboard",
        "Build a native mobile app with no web-style forms",
    ],
)
def test_web_similarity_conjuncts_do_not_own(goal: str) -> None:
    """R117 guard: bare web similarity modifiers compare rather than
    produce — a native app with web-style forms stays native."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Login form and account page")
    _seed_section(ledger, "runtime_context", value="Native iOS and Android application")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


def test_coordinated_web_dashboard_keeps_honest_ambiguity() -> None:
    """R105 guard: a coordinated conjunct uses the same ownership grammar
    as a standalone goal — "a CLI and a local web dashboard" is an
    honest cli/web_app ambiguity, not a single cli."""
    ledger = _bare_ledger("Build a CLI and a local web dashboard")
    _seed_section(ledger, "outputs", value="Deterministic stdout and an interactive admin panel")
    _seed_section(ledger, "runtime_context", value="Local shell and browser")
    result = derive_domain_from_ledger(ledger)
    assert result.classes == frozenset({TaskClass.CLI, TaskClass.WEB_APP})


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CLI; avoid a browser UI",
        "Build a CLI and avoid a browser UI",
        "Build a CLI; skip the web UI",
    ],
)
def test_exclusion_predicates_deny_like_negators(goal: str) -> None:
    """R112 guard: "avoid a browser UI" rejects the UI it names — the
    excluded phrase cannot become a co-product and the CLI keeps its
    single class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Deterministic stdout and exit code 0")
    _seed_section(ledger, "runtime_context", value="Local shell / terminal")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    "goal",
    [
        "Existing system is a CLI. Replace it with a web app.",
        "We currently expose a REST API. Replace it with a browser dashboard.",
        "Migrate from a CLI to a web app.",
    ],
)
def test_replaced_background_artifacts_do_not_own(goal: str) -> None:
    """R114 guard: background/current-state clauses describe the
    artifact being replaced — only the replacement product owns, so the
    web app keeps its clean single class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive signup form and settings panel")
    _seed_section(ledger, "runtime_context", value="Modern browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected_only"),
    [
        (
            "Build a report analyzing web applications",
            "PDF analysis report written to disk",
            "Local batch process",
            None,
        ),
        (
            "Build a library indexing websites",
            "Reusable indexing functions",
            "Local Python process",
            TaskClass.LIBRARY,
        ),
        (
            "Build a CLI comparing web apps",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            TaskClass.CLI,
        ),
        (
            "Build a scorecard evaluating web apps",
            "Score summaries written to disk",
            "Local batch process",
            None,
        ),
        ("Build a document describing websites", "Markdown document", "Local files", None),
    ],
)
def test_transitive_inspection_participles_do_not_own(
    goal: str, outputs: str, runtime: str, expected_only: TaskClass | None
) -> None:
    """R102 guard: a transitive inspection participle after the produced
    head names the studied object — the report/library/CLI keeps its own
    class and the web nouns own nothing."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes
    if expected_only is not None:
        assert result.is_single
        assert result.single is expected_only


@pytest.mark.parametrize(
    "goal",
    [
        "Build a browser compatible dashboard",
        "Build a browser ready dashboard",
        "Build a browser-compatible dashboard",
    ],
)
def test_spaced_browser_qualifiers_own_like_hyphenated(goal: str) -> None:
    """R101 guard: a qualifier adjective premodifying a UI head reads
    the same spaced or hyphenated — "browser compatible dashboard" owns
    exactly like "browser-compatible dashboard"."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Interactive charts and filters panel")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("runtime", "expect_web_app"),
    [
        ("Native desktop application; Playwright tests run in browsers", False),
        ("Native desktop application; Selenium tests run in browsers", False),
        ("Runs in browsers; Playwright tests run in CI", True),
    ],
)
def test_test_environment_browsers_are_not_section_evidence(
    runtime: str, expect_web_app: bool
) -> None:
    """R100 guard: a browser that hosts the tests is not the product's
    execution environment — the explicit native runtime keeps authority,
    while a genuine browser runtime clause survives the strip."""
    ledger = _bare_ledger("Build an interactive dashboard")
    _seed_section(ledger, "outputs", value="Interactive charts with a filters panel")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert (TaskClass.WEB_APP in result.classes) is expect_web_app


@pytest.mark.parametrize(
    "runtime",
    [
        "Production runtime is a Chrome browser extension; Playwright tests run in CI",
        "Playwright tests run in CI; production runtime is a Chrome browser extension",
        "Production runtime is a Chrome browser extension and Playwright tests run in CI",
        "Production runtime is a Chrome browser extension, Playwright tests run in CI",
        "Chrome browser extension runtime tested with Playwright in CI",
        "Chrome browser extension runtime exercised by Playwright in CI",
        "Playwright-tested Chrome browser extension runtime in CI",
        "Playwright tests cover the production Chrome browser extension runtime",
        "End-to-end tests exercise the production Chrome browser extension runtime",
        "Playwright tests exercise the Chrome browser extension runtime",
        "Playwright tests cover a Chrome browser extension shipped to users",
        "Playwright verifies the Chrome browser extension that is deployed in production",
        "Playwright tests run against a Chrome browser extension released to customers",
        "Playwright tests exercise a Chrome browser extension running in production",
        "Playwright tests exercise a Chrome browser extension currently deployed to users",
        "Playwright tests exercise a Chrome browser extension now live for customers",
        "Playwright tests exercise a Chrome browser extension used in production",
        "Playwright tests exercise a customer-facing Chrome browser extension",
        "Playwright tests exercise a Chrome browser extension serving paying customers",
        "Playwright tests exercise a Chrome browser extension deployed globally",
        "Playwright tests exercise the Chrome browser extension customers use daily",
        "Playwright tests exercise the Chrome browser extension bundled with our desktop application",
        "Playwright tests exercise the Chrome browser extension preinstalled on company laptops",
        "Playwright tests exercise the Chrome browser extension managed through enterprise policy",
        "Playwright tests exercise the Chrome browser extension that ships with our desktop application",
        "Production Chrome browser extension includes test diagnostics",
        "Chrome browser extension includes a QA feedback panel for administrators",
        "Chrome browser extension includes testing controls for administrators",
        "Chrome browser extension provides a compatibility dashboard to customers",
    ],
)
def test_production_identity_survives_adjacent_test_clauses(runtime: str) -> None:
    """R90-R94 guard: the verification exemption is scoped per relation —
    a production identity keeps its authority whether the verification
    prose sits in its own punctuated clause, shares the segment through
    a conjunction or comma, attaches within the same clause as a
    participial, passive, or hyphenated-compound modifier, or follows
    the verification prose as a production-marked object. The marking is
    structural: a prenominal qualifier, a runtime head, or any
    postnominal predication tail anchoring the component to an
    environment through a prepositional phrase."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value="Interactive signup page")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    "goal",
    [
        "The product is an admin portal that runs in browsers",
        "The deliverable is an admin portal available in browsers",
    ],
)
def test_copular_product_declarations_own_the_artifact(goal: str) -> None:
    """R55 probe: a specificational copula — definite subject, copula,
    indefinite NP — declares the produced artifact directly."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Role management and account administration")
    _seed_section(ledger, "runtime_context", value="Browser")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected"),
    [
        (
            "Migrate the existing script to the CLI described in the requirements",
            "Converted workflows with progress logs",
            "Runs on developer machines",
            TaskClass.CLI,
        ),
        (
            "Convert the legacy tool to the command-line application requested above",
            "Converted workflows with progress logs",
            "Runs on developer machines",
            TaskClass.CLI,
        ),
        (
            "Promote the old endpoint to the REST API specified in the contract",
            "JSON responses with status codes",
            "Containerized HTTP service",
            TaskClass.WEB_SERVICE,
        ),
    ],
)
def test_definite_produced_destinations_keep_ownership(
    goal: str, outputs: str, runtime: str, expected: TaskClass
) -> None:
    """R55 probe: definiteness decides nothing by itself — a
    transformation context keeps its produced destination even when the
    destination is introduced with "the"."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a service worker for a PWA",
            "Offline support for the login page and checkout form",
        ),
        (
            "Build a web worker for the analytics dashboard",
            "Background aggregation for the metrics pages",
        ),
    ],
)
def test_supporting_worker_artifacts_do_not_own(goal: str, outputs: str) -> None:
    """R120 probe: a worker is a supporting component head — the web
    product it serves is its consumer, and page mentions in its outputs
    are targets of the support, not produced UI."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "runtime"),
    [
        ("Build a browser game", "Runs in browsers"),
        ("Build a web game", "Modern browsers"),
        ("Build an online game", "Runs in browsers"),
    ],
)
def test_explicit_game_product_heads_stay_games(goal: str, runtime: str) -> None:
    """R120 probe: an explicit game-product goal head owns game_2d even
    with ordinary standardized outputs — browser hosting must not swap
    the task class to web_app and bind browser AC/probes."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Player controls, score updates, and level transitions")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.GAME_2D


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime", "expected"),
    [
        (
            "Build neither a CLI nor a web app but a REST API",
            "JSON responses with status codes",
            "Containerized HTTP service",
            TaskClass.WEB_SERVICE,
        ),
        (
            "Build neither a CLI nor a desktop tool but a web app",
            "Interactive signup page and settings panels",
            "Runs in browsers",
            TaskClass.WEB_APP,
        ),
        (
            "Build neither a web app nor a service but a CLI",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            TaskClass.CLI,
        ),
    ],
)
def test_neither_nor_spares_the_adversative_pivot(
    goal: str, outputs: str, runtime: str, expected: TaskClass
) -> None:
    """R121 probe: the neither/nor strip stops at but/yet — the denied
    alternatives leave while the affirmed product keeps its durable
    class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is expected


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        (
            "Build a web app, not a webhook receiver",
            "Interactive signup page with stored preferences",
        ),
        (
            "Build a browser dashboard without webhooks",
            "Login form with stored history and settings panels",
        ),
        (
            "Build a website for users who configure webhooks",
            "Interactive settings pages with stored preferences",
        ),
    ],
)
def test_webhook_topic_words_do_not_own(goal: str, outputs: str) -> None:
    """R121 probe: webhook evidence routes through the shared negation,
    audience, and dependency rules and needs affirmative receiver
    semantics — a topic word plus a stored-output cannot fabricate the
    ambiguity that blocks the web_app class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "runtime",
    [
        "Development server launched from a terminal",
        "Run npm start in the shell to serve modern browsers",
        "Local shell starts the Vite server; app runs in browsers",
    ],
)
def test_shell_launch_mechanisms_do_not_own_cli(runtime: str) -> None:
    """R121 probe: a shell/terminal that merely launches the product is
    a consumer/launcher relation — with an affirmative browser product
    and no CLI-shaped evidence, it cannot assert CLI ownership."""
    ledger = _bare_ledger("Build a web app")
    _seed_section(ledger, "outputs", value="Interactive signup page")
    _seed_section(ledger, "runtime_context", value=runtime)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    "goal",
    [
        "Build not a web app but a browser game",
        "Build not a game but a browser game",
        "Build neither a CLI nor a web app but a browser game",
        "Build neither a CLI nor a web app, but a browser game",
    ],
)
def test_adversative_pivots_keep_the_affirmed_game(goal: str) -> None:
    """R122 probe: the affirmed game after but/yet re-heads the goal —
    the denial prefix cannot suppress the produced game class."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value="Player controls, score updates, and level transitions")
    _seed_section(ledger, "runtime_context", value="Runs in browsers")
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.GAME_2D


# ---------------------------------------------------------------------------
# Consolidated case corpora
#
# Each param below was previously its own test function; the ``id`` is that
# function's original name and the comment above it is its original rationale.
# The ledger construction and assertions are unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime_context"),
    [
        pytest.param(
            "Build a habit-tracker CLI tool",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            id="single_match_cli",
        ),
        # "Build a CLI, not a browser page" is CLI evidence, not web_app evidence.
        pytest.param(
            "Build a CLI, not a browser page",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            id="negated_browser_goal_keeps_cli_single",
        ),
        # R7 probe: arbitrary descriptive modifiers inside a denied alternative ("browser-based
        # graphical UI") must not shield the next coordinated signal.
        pytest.param(
            "Build a terminal dashboard, not a browser-based graphical UI or web app",
            "Interactive dashboard panels and buttons with deterministic stdout",
            "Local shell / terminal",
            id="descriptive_modifier_in_denial_strips_whole_coordination",
        ),
        # R8 probe: a denied alternative longer than three modifier words must still strip up to the
        # coordination boundary.
        pytest.param(
            "Build a terminal UI, not a browser-based graphical user interface or web app",
            "Interactive dashboard panels and buttons with deterministic stdout",
            "Local shell / terminal",
            id="long_denied_alternative_strips_completely",
        ),
        # R14 probe: browser-auditing tooling denies the web-app artifact type; the domain word
        # "browser" and the inspected pages in its report are not evidence that it IS one.
        pytest.param(
            "Build a CLI that audits browser pages, not a web app",
            "Accessibility report for login pages and settings panels, printed to stdout",
            "Local shell / terminal",
            id="denied_artifact_type_overrides_browser_domain_mentions",
        ),
        # R14 probe: "web application firewall, not a browser UI" denies the artifact type despite the
        # affirmative-looking domain phrase.
        pytest.param(
            "Build a web application firewall, not a browser UI",
            "Filter rules for login forms and settings panels, printed to stdout",
            "Local shell / terminal",
            id="web_application_firewall_denial_is_not_web_app",
        ),
        # R15 probe: an artifact-type denial in the goal governs the whole ledger — a report naturally
        # described as a browser report cannot revive the denied web-app classification.
        pytest.param(
            "Build a CLI that audits browser pages, not a web app",
            "Browser accessibility report for login pages and settings panels, printed to stdout",
            "Local shell / terminal",
            id="artifact_denial_covers_browser_wording_in_outputs",
        ),
        # R16 probe: "non-web-app" is an artifact-type denial too.
        pytest.param(
            "Build a non-web-app CLI for browser audits",
            "Login forms and settings panels listed in the report, printed to stdout",
            "Local shell / terminal",
            id="prefix_style_web_app_denial_dominates",
        ),
        # R19 probe: the shared-shape gate must read the negation-filtered text — "without a canvas"
        # is not game shape.
        pytest.param(
            "Build a CLI without a canvas",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            id="negated_canvas_does_not_fire_game_for_cli",
        ),
        pytest.param(
            "Build a CLI for browser error diagnostics",
            "Browser error pages listed with deterministic stdout",
            "Local shell / terminal",
            id="error_pages_in_cli_output_are_not_ui_composition",
        ),
        # R21 probe: a report enumerating audited pages is data, not a UI.
        pytest.param(
            "Build a CLI for browser page audits",
            "Login pages listed with deterministic stdout",
            "Local shell / terminal",
            id="pages_listed_in_report_are_not_ui_composition",
        ),
        # R21 probe: "No user interface" is an explicit output-side denial.
        pytest.param(
            "Build a browser automation CLI",
            "No user interface; deterministic stdout and exit codes",
            "Local shell / terminal",
            id="negated_output_ui_description_is_not_ui_composition",
        ),
        # R24 probe: "form errors printed to stdout" is audit data.
        pytest.param(
            "Build a CLI to audit browser form errors",
            "Login form errors printed to stdout",
            "Local shell / terminal",
            id="widget_data_nouns_before_report_verbs_are_not_ui",
        ),
        # R25 probe: report data separated from its verb by a conjunction is still report data.
        pytest.param(
            "Build a browser audit CLI",
            "Audited pages and URLs printed to stdout",
            "Local shell / terminal",
            id="coordinated_report_data_is_not_ui_composition",
        ),
        # R26 probe: a CLI that manipulates login forms in a browser does not produce those forms.
        pytest.param(
            "Build a shell CLI for browser logins",
            "Opens a browser and submits login forms",
            "Local shell / terminal",
            id="automation_target_widgets_are_not_produced_ui",
        ),
        # R28 probe: "to test a web app" names a target, not the product.
        pytest.param(
            "Build a CLI to test a web app",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            id="infinitive_target_web_app_is_not_the_produced_artifact",
        ),
        # R33 probe: "for browser audits but not a web app" — the denial after "but" returns to the
        # main artifact claim.
        pytest.param(
            "Build a CLI for browser audits but not a web app",
            "Interactive dashboard panels with deterministic stdout",
            "Local shell / terminal",
            id="but_coordination_ends_the_content_clause",
        ),
        # R34 probe: the denial after "but" is top-level even when the for-clause carries no
        # inspection keyword.
        pytest.param(
            "Build a CLI for frontend developers but not a web app",
            "Interactive dashboard panels and deterministic stdout",
            "Local shell / terminal",
            id="adversative_but_ends_content_clause_without_tool_keyword",
        ),
        # R121 guard: genuine CLI ledgers keep their shell runtime signal.
        pytest.param(
            "Build a habit-tracker CLI tool",
            "Deterministic stdout and exit code 0",
            "Local shell / terminal",
            id="shell_runtime_still_owns_cli_products",
        ),
    ],
)
def test_goal_output_runtime_corpus_resolves_to_cli(
    goal: str, outputs: str, runtime_context: str
) -> None:
    """Corpus of goal/outputs/runtime_context ledgers that must resolve to a single CLI."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime_context)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        pytest.param(
            "Build a REST API for blog posts",
            "Multiple REST endpoints returning JSON body responses",
            id="single_match_web_service",
        ),
        # R20 probe: "error pages returned in a JSON response" is service data, not a produced UI.
        pytest.param(
            "Build a web service for browser error analysis",
            "Browser error pages returned in a JSON response",
            id="error_pages_in_service_output_are_not_ui_composition",
        ),
        # R37 probe: response vocabulary still owns a service when a server artifact explicitly
        # produces it.
        pytest.param(
            "Build a backend",
            "Server returns HTTP responses with JSON bodies",
            id="server_produced_response_shape_is_web_service_evidence",
        ),
    ],
)
def test_goal_output_corpus_resolves_to_web_service(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers that must resolve to a single WEB_SERVICE."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_SERVICE


@pytest.mark.parametrize(
    ("goal", "inputs", "outputs"),
    [
        pytest.param(
            "Aggregate daily logs into Parquet",
            "Dataset of log files split per day",
            "Aggregated output dataset in Parquet",
            id="single_match_data_pipeline",
        ),
        pytest.param(
            "Build a data pipeline for browser analytics",
            "Dataset of crawled sessions",
            "Aggregated login page metadata in Parquet",
            id="pipeline_widget_metadata_is_not_produced_ui",
        ),
        pytest.param(
            "Build a data pipeline to analyze a web app",
            "Dataset of access logs",
            "Aggregated output dataset in Parquet",
            id="infinitive_target_web_app_pipeline_variant",
        ),
        # R116 guard: coordinated manipulated targets are consumed whole and a grouping adverbial
        # names data organization, not the runtime — the aggregation keeps its single data_pipeline
        # class.
        pytest.param(
            "Aggregate browser compatibility datasets into a matrix",
            "Compatibility datasets in CSV",
            "Aggregated login pages and signup forms by browser",
            id="coordinated_manipulated_targets_strip_whole",
        ),
    ],
)
def test_goal_input_output_corpus_resolves_to_data_pipeline(
    goal: str, inputs: str, outputs: str
) -> None:
    """Corpus of goal/inputs/outputs ledgers that must resolve to a single DATA_PIPELINE."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "inputs", value=inputs)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.DATA_PIPELINE


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        pytest.param(
            "Build a small 2D game scene",
            "Each frame renders a canvas with the playable scene state",
            id="single_match_game_2d",
        ),
        # R7 probe: bounding the render signal must stay context-sensitive — passive "rendered
        # sprites" is still a game description.
        pytest.param(
            "Build a platformer",
            "Rendered sprites with player movement and collisions",
            id="passive_rendered_game_output_still_matches_game_2d",
        ),
        # Bounding "screen" must stay context-sensitive: unanchored screens are still game evidence.
        pytest.param(
            "Build an arcade shooter",
            "A title screen and a game-over screen with the final score",
            id="game_screens_still_match_game_2d",
        ),
    ],
)
def test_goal_output_corpus_resolves_to_game_2d(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers that must resolve to a single GAME_2D."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.GAME_2D


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        pytest.param(
            "Publish a JSON-schema parsing library",
            "An importable Python package exposing a public API surface",
            id="single_match_library",
        ),
        pytest.param(
            "Publish a reusable helper, not a browser page",
            "A reusable package published to PyPI",
            id="negated_browser_goal_keeps_library_single",
        ),
        # R2 probe: "browser form parsing" names the data domain, not a UI — with no user interface
        # requested, the library classification must not be dragged into ambiguity.
        pytest.param(
            "Build a library for browser form parsing without a user interface",
            "An importable parsing package with a public api",
            id="goal_domain_vocabulary_is_not_ui_evidence",
        ),
        # R3 probe: bare "validation" in a library's outputs is not a UI.
        pytest.param(
            "Build a browser protocol library",
            "An importable package with validation helpers",
            id="validation_helpers_output_is_not_ui_evidence",
        ),
        # R3 probe: "documentation pages" are prose artifacts, not a UI.
        pytest.param(
            "Build a browser automation library",
            "An importable package plus documentation pages",
            id="documentation_pages_output_is_not_ui_evidence",
        ),
        # R4 probe: the phrase inside a library's outputs names an API domain.
        pytest.param(
            "Build a browser library for client-side validation",
            "An importable package providing client-side validation helpers",
            id="client_side_validation_helpers_are_not_ui_evidence",
        ),
        # R4 probe: interactive API documentation is a docs artifact, not a UI.
        pytest.param(
            "Build a browser automation library",
            "An importable package with interactive API documentation",
            id="interactive_documentation_is_not_ui_evidence",
        ),
        # R7 probe: Page Object artifacts are testing-library outputs.
        pytest.param(
            "Build a browser automation library",
            "An importable package with login page objects and test fixtures",
            id="page_objects_are_not_ui_evidence",
        ),
        # R13 probe: the guard must carry the complete _matches_library signal set — including the
        # singular package word.
        pytest.param(
            "Build a frontend Python package",
            "Login forms and settings panels",
            id="package_goal_intent_stays_library",
        ),
        pytest.param(
            "Build an SDK supporting websites, web apps, and browser UIs",
            "An importable package with a public API",
            id="coordinated_supporting_list_keeps_library_single",
        ),
    ],
)
def test_goal_output_corpus_resolves_to_library(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers that must resolve to a single LIBRARY."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.LIBRARY


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        # A ledger whose `goal` says CLI but whose `outputs` lacks cli vocabulary should still
        # classify as CLI as long as the ledger-evidence gate is satisfied (outputs OR runtime is non-
        # empty).
        # Generic non-cli output vocabulary — what a ledger_only closure
        # would typically write.
        pytest.param(
            "Build a habit-tracker CLI for end users",
            "JSON file stored in the working directory",
            id="cli_matches_on_goal_signal_alone",
        ),
        pytest.param(
            "Turn the reporting web app into a CLI; the deliverable is not a web app anymore",
            "Report tables printed to stdout with exit codes",
            id="trailing_adverb_does_not_defeat_denial",
        ),
        pytest.param(
            "Build a terminal report tool, not a browser-based graphical dashboard, interactive web app, or responsive frontend",
            "Usage tables printed to stdout",
            id="adjective_led_denial_alternatives_are_consumed",
        ),
    ],
)
def test_goal_output_corpus_resolves_to_cli(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers that must resolve to a single CLI."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.CLI


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        # The #1813 reproduction: browser form/panel/validation signals plus a ``package.json``
        # mention must resolve to the product-complete web_app class instead of being dragged to
        # library by the manifest filename.
        pytest.param(
            "Build a signup page that runs in the browser",
            "Signup form with a settings panel shown in the browser; client-side validation messages; package.json build scripts",
            id="single_match_web_app",
        ),
        pytest.param(
            "Build a single-page application for notes",
            "A notes form with an edit panel",
            id="single_page_application_phrase_is_browser_context",
        ),
        # R3 follow-up: an explicit web-application goal with interactive outputs must not fall
        # through to unmatched.
        pytest.param(
            "Build a web application dashboard",
            "Interactive charts and navigation",
            id="web_application_dashboard_matches_web_app",
        ),
        # R5 probe: a plain named page with navigation is an ordinary web-app description and must not
        # fall through to unmatched.
        pytest.param(
            "Build a web app",
            "A responsive login page with navigation",
            id="ordinary_login_page_output_matches_web_app",
        ),
        # R5 probe: "rendered" is normal UI wording; the game predicate must not claim the passive
        # participle.
        pytest.param(
            "Build a web app",
            "A login page rendered in the browser",
            id="rendered_page_does_not_dual_fire_game_2d",
        ),
        # R9 probe: "is rendered on initial load" is a UI clause; grammatical variants must not leak
        # into the game classifier.
        pytest.param(
            "Build a web app",
            "A login page is rendered on initial load",
            id="rendered_ui_clause_variants_do_not_fire_game",
        ),
        # R13 probe: "iframe" must not satisfy the game fast path's "frame".
        pytest.param(
            "Build a browser dashboard",
            "An embedded iframe and a settings panel",
            id="iframe_is_not_game_frame_evidence",
        ),
        # R16 probe: "for game leaderboards" is the app's subject, not its artifact shape.
        pytest.param(
            "Build a web app for game leaderboards",
            "Leaderboard page with a filters panel",
            id="game_subject_matter_does_not_erase_requested_web_app",
        ),
        # R17 probe: output-side game subject vocabulary suppresses web_app only when the game
        # predicate itself has artifact-shape evidence.
        pytest.param(
            "Build a web app for game leaderboards",
            "Game leaderboard page with a filters panel",
            id="output_game_vocabulary_without_game_shape_keeps_web_app",
        ),
        # R18 probe: a relative clause naming SDK documentation is subject matter, not library
        # artifact-shape evidence.
        pytest.param(
            "Build a web app that displays SDK documentation",
            "Browser dashboard with search and a navigation bar",
            id="sdk_documentation_subject_does_not_own_the_web_app",
        ),
        # R18 probe: physical package-delivery vocabulary is not the library package signal.
        pytest.param(
            "Build a package-delivery tracking web app",
            "Tracking dashboard with a search form and status pages",
            id="package_delivery_subject_does_not_own_the_web_app",
        ),
        # R23 probe: "no errors in the web UI" targets the errors — the artifact-denial path needs the
        # same quality rule as the strip.
        pytest.param(
            "Build a dashboard with no errors in the web UI",
            "Login page with settings panel and navigation",
            id="goal_quality_statement_is_not_artifact_denial",
        ),
        # R37 probe: REST API modifies client; it is not a produced service.
        pytest.param(
            "Build a frontend REST API client",
            "Login panels displaying HTTP responses received from upstream",
            id="prenominal_api_client_is_a_consumed_dependency",
        ),
        pytest.param(
            "Build a recipe website with no signup, plus a browser UI for browsing recipes",
            "Homepage with responsive layout and recipe detail pages",
            id="feature_denial_cannot_cross_a_comma",
        ),
        pytest.param(
            "Build a website that lists non-browser apps for offline work",
            "Homepage with responsive layout and app detail pages",
            id="prefix_denial_inside_content_clause_is_scoped",
        ),
        # R37 probe: later items in a dependency list are dependencies too.
        pytest.param(
            "Build a web app compatible with a REST API and command-line tools",
            "Login page with settings panel and navigation",
            id="coordinated_dependency_lists_are_fully_consumed",
        ),
    ],
)
def test_goal_output_corpus_resolves_to_web_app(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers that must resolve to a single WEB_APP."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        # R1 probe: "form" inside "performance" must not count as UI composition.
        pytest.param(
            "Build browser performance benchmarking tools",
            "Performance report with timing metrics",
            id="browser_performance_vocabulary_is_not_ui_evidence",
        ),
        # R1 probe: "validation" inside "invalidation" must not count either.
        pytest.param(
            "Implement browser cache invalidation",
            "Invalidation events are logged",
            id="browser_cache_invalidation_is_not_ui_evidence",
        ),
        # R2 probe: "without forms or pages" is a denial, not UI composition.
        pytest.param(
            "Build a browser benchmark without forms or pages",
            "A performance report with timing metrics",
            id="negated_ui_vocabulary_in_goal_is_not_ui_evidence",
        ),
        # R3 probe: "single page PDF report" is a document, not an SPA.
        pytest.param(
            "Generate a single page PDF report",
            "A PDF page containing the monthly totals",
            id="single_page_document_goal_is_not_browser_context",
        ),
        # R19 probe: "non-browser user interface" denies the artifact through its head noun; later
        # browser subject mentions cannot resurrect it.
        pytest.param(
            "Build a non-browser user interface for browser accessibility audits",
            "Login dashboard with account pages",
            id="prefix_denial_head_noun_dominates_subject_mentions",
        ),
        pytest.param(
            "Build a browser audit report",
            "Signup form failures listed in JSON",
            id="widget_failure_data_is_not_ui",
        ),
        pytest.param(
            "Build a browser crawler",
            "Screenshots login pages and saves them to disk",
            id="crawler_screenshot_targets_are_not_produced_ui",
        ),
        # R27 probe: screenshots of login pages are inspected content.
        pytest.param(
            "Build a browser extension for account security",
            "Login page screenshots with risk scores",
            id="extension_screenshot_content_is_not_produced_ui",
        ),
        # R27 probe: a test suite verifying a page does not produce it.
        pytest.param(
            "Build a browser automation test suite",
            "Verifies the login page is displayed correctly",
            id="verification_target_is_not_produced_ui",
        ),
        # R30 probe: an automation suite's checkout-form results are inspected content, not a produced
        # web app.
        pytest.param(
            "Build a browser automation suite that tests checkout forms",
            "Checkout form results and screenshots",
            id="inspection_tool_goal_voids_target_widget_outputs",
        ),
    ],
)
def test_goal_output_corpus_withholds_web_app(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers that must not yield WEB_APP."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs"),
    [
        # "not just a browser page" asserts the page — the signal must survive.
        # UI composition must come from standardized outputs (R2); the goal
        # contributes only the surviving affirmative browser signal.
        pytest.param(
            "Build a dashboard that is not just a browser page",
            "A charts panel refreshed from a data feed",
            id="affirmative_expansion_keeps_web_app_signal",
        ),
        # R16 probe: a co-produced public API must not suppress the explicit browser UI — web_app
        # survives, alone or in honest ambiguity.
        pytest.param(
            "Build a web application with a public API",
            "Login forms and settings panels",
            id="co_produced_api_does_not_erase_requested_web_app",
        ),
        # R20 probe: a goal requesting both artifacts must not let library evidence veto the web app —
        # independent ownership, honest ambiguity.
        pytest.param(
            "Build a web app and a companion SDK",
            "Browser dashboard with login forms and a companion SDK",
            id="co_requested_sdk_keeps_web_app_in_honest_ambiguity",
        ),
        # R22 probe: a with-clause can introduce a co-produced web app.
        pytest.param(
            "Build an SDK with an admin web app",
            "Browser admin dashboard with login forms and a companion SDK",
            id="with_joined_web_app_co_product_keeps_web_ownership",
        ),
    ],
)
def test_goal_output_corpus_keeps_web_app_candidate(goal: str, outputs: str) -> None:
    """Corpus of goal/outputs ledgers where WEB_APP must stay among the candidates."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP in result.classes


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime_context"),
    [
        # R25 probe: an SDK the app is built WITH is a dependency — the consumed-dependency rule
        # applies to goal ownership too.
        pytest.param(
            "Build a web app using the Stripe SDK",
            "A login form with an account settings panel",
            "Browser",
            id="goal_side_consumed_sdk_is_not_library_ownership",
        ),
        # R50 guard: nominal gerund modifiers ("landing page") keep the grant while the participle bar
        # stops verbal chain tokens.
        pytest.param(
            "Build a landing page for the browser",
            "Signup conversion copy and forms",
            "Browser",
            id="attributive_gerunds_stay_product_vocabulary",
        ),
        # R56 probe: runtime_context declares the execution environment, so "Runs in browsers" is
        # affirmative browser context and must not be erased by the consumer-relation normalization.
        pytest.param(
            "Create a customer dashboard",
            "Interactive charts and settings panel",
            "Runs in browsers",
            id="runtime_context_environment_declarations_are_not_consumer_relations",
        ),
        # R57 probe: an affirmative UI-product head with a standardized browser execution environment
        # owns without widget vocabulary — ordinary user-flow outputs suffice.
        pytest.param(
            "Build an admin portal",
            "Users authenticate, manage roles, and update account settings",
            "Runs in browsers",
            id="ui_head_plus_browser_runtime_is_semantic_ui_evidence",
        ),
        # R60 probe: "web" directly modifying a UI product head is the bounded ownership form —
        # deployment prose need not repeat "browser".
        pytest.param(
            "Build a web dashboard",
            "Login page with settings panel and navigation",
            "Containerized service",
            id="bare_web_qualifier_owns_a_ui_head",
        ),
        # R61 probe: the inspection veto's product-head exception accepts standardized runtime
        # evidence — monitoring is the dashboard's subject, not its artifact type.
        pytest.param(
            "Build a webhook monitoring dashboard",
            "Interactive charts and a filters panel",
            "Runs in browsers",
            id="monitoring_subject_dashboards_own_with_runtime_evidence",
        ),
        # R65 probe: a lone leading noun is a product NP — only a bare action verb fails to mark one.
        pytest.param(
            "Spreadsheet in the browser",
            "Editable cells with formula bar",
            "Runs in browsers",
            id="one_word_product_declarations_reach_their_np",
        ),
        # R78 guard: a denial the app DISPLAYS ("showing which browsers are unsupported") lives in a
        # content clause and keeps the web-app request intact.
        pytest.param(
            "Build a web app showing which browsers are unsupported",
            "Interactive compatibility matrix with a filters panel",
            "Browser",
            id="displayed_denials_are_content_not_scope",
        ),
        # R78 guard: "No browser extension" in the runtime is a denial, not the runtime's identity —
        # the standalone website keeps its class.
        pytest.param(
            "Build a standalone website",
            "Landing pages with signup forms",
            "No browser extension; runs as a standalone website",
            id="negated_component_runtimes_do_not_veto",
        ),
        # R80 guard: a denial inside displayed content ("displaying websites that are unavailable")
        # never erases the web app.
        pytest.param(
            "Build a web app displaying websites that are unavailable",
            "Availability table with a filters panel",
            "Browser",
            id="participial_content_denials_stay_content",
        ),
        # R85 guard: a bare-preposition association inside a content clause is displayed content — the
        # dashboard with browser runtime owns, while relation-participles (belonging to) still veto.
        pytest.param(
            "Build a dashboard showing the status of browser extensions",
            "Extension status table with filters and install buttons",
            "Runs in browsers",
            id="displayed_component_status_keeps_the_dashboard",
        ),
        # R106 guard: "package search form" and "package detail pages" render content about packages —
        # the registry frontend keeps a clean single web_app class.
        pytest.param(
            "Build a package registry frontend",
            "Package search form and package detail pages",
            "Runs in browsers",
            id="library_tokens_premodifying_ui_heads_are_content",
        ),
        # R115 guard: "SPA" is the canonical abbreviation of the single-page-application subtype and
        # owns the class outright.
        pytest.param(
            "Build a React SPA",
            "Interactive login form and settings panel",
            "Vite dev server",
            id="spa_abbreviation_owns_like_its_expansion",
        ),
        # R117 guard: PWA is the canonical progressive-web-application abbreviation and owns the web-
        # app class outright.
        pytest.param(
            "Build a PWA",
            "Interactive signup page and settings panel",
            "Service worker and installable manifest",
            id="pwa_abbreviation_owns_like_its_expansion",
        ),
        # R105 guard: a transitive exclusion is the app's own compatibility policy — "a web app that
        # excludes unsupported browsers" is still the requested web app.
        pytest.param(
            "Build a web app that excludes unsupported browsers",
            "Interactive account settings panel",
            "Modern browsers",
            id="capability_exclusion_is_not_artifact_denial",
        ),
        # R120 guard: the game-product head is read from the negation-stripped goal, so a denial
        # cannot claim game ownership.
        pytest.param(
            "Build a web app, not a browser game",
            "Interactive signup page and settings panels",
            "Runs in browsers",
            id="denied_game_heads_do_not_claim_ownership",
        ),
        # R122 guard: a pivot into a non-game product keeps its own class.
        pytest.param(
            "Build not a browser game but a web app",
            "Interactive signup page and settings panels",
            "Runs in browsers",
            id="denied_game_pivots_still_release_ownership",
        ),
    ],
)
def test_goal_output_runtime_corpus_resolves_to_web_app(
    goal: str, outputs: str, runtime_context: str
) -> None:
    """Corpus of goal/outputs/runtime_context ledgers that must resolve to a single WEB_APP."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime_context)
    result = derive_domain_from_ledger(ledger)
    assert result.is_single
    assert result.single is TaskClass.WEB_APP


@pytest.mark.parametrize(
    ("goal", "outputs", "runtime_context"),
    [
        # R37 probe: a web target later in a coordinated consumer list is still a consumer.
        pytest.param(
            "Build a desktop tool compatible with mobile apps and web apps",
            "Settings panel for local files",
            "Native desktop runtime",
            id="coordinated_consumer_lists_are_fully_consumed",
        ),
        # R38 probe: the denied capability noun is generic, not just "support".
        pytest.param(
            "Build a native desktop settings tool",
            "Settings panel for local files",
            "Browser integration is disabled; native desktop runtime",
            id="postpositive_denial_with_capability_noun",
        ),
        # R38 probe: "as well as" coordinates consumer targets too.
        pytest.param(
            "Build a desktop tool compatible with mobile apps as well as web apps",
            "Settings panel for local files",
            "Native desktop runtime",
            id="as_well_as_joins_consumer_lists",
        ),
        # R41 probe: "for developers who build web apps" describes the users' work — a human relative
        # clause marks the whole for-phrase as audience.
        pytest.param(
            "Build a native desktop settings tool for developers who build web apps",
            "Settings panel for local files",
            "Native desktop runtime",
            id="relative_clause_audiences_are_not_ownership",
        ),
        # R41 probe: "not available" composes like "unavailable".
        pytest.param(
            "Build a native desktop settings tool; browser support is not available",
            "Settings panel for local files",
            "Native desktop runtime",
            id="compositional_not_available_denial",
        ),
        # R42 probe: "aren't supported" composes like "are not supported".
        pytest.param(
            "Build a native desktop settings tool; browser apps aren't supported",
            "Settings panel for local files",
            "Native desktop runtime",
            id="contracted_postpositive_denials",
        ),
        # R42 probe: "for teams that use web apps" is audience activity.
        pytest.param(
            "Build a native desktop settings tool for teams that use web apps",
            "Settings panel for local files",
            "Native desktop runtime",
            id="that_clause_audiences_with_activity_verbs",
        ),
        # R44 probe: "browser-like" compares the panel to a browser; the produced artifact is the
        # native desktop app.
        pytest.param(
            "Build a native desktop app with a browser-like settings panel",
            "Settings panel with local preferences",
            "Native desktop runtime",
            id="similarity_modifier_is_not_browser_ownership",
        ),
        # R55 guard: output composition corroborates ownership but cannot create it — a goal whose
        # first noun phrase is a report keeps its report identity regardless of widget-rich output
        # wording.
        pytest.param(
            "Build a report on browser pages",
            "Login page vulnerabilities and settings panel risks",
            "Local batch job",
            id="report_artifacts_cannot_gain_ownership_from_outputs",
        ),
        # R56 guard: a bare browser modifier is a domain attribute — the first NP's actual head
        # decides, so "browser performance report" stays a report while "browser-based" qualifiers
        # keep predicating the environment.
        pytest.param(
            "Build a browser performance report",
            "Login page timings and settings panel latency",
            "Local batch job",
            id="attributive_browser_modifiers_do_not_make_a_report_a_product",
        ),
        # R61 guard: component ownership survives generic runtime wording — "Runs in browsers" cannot
        # hand an extension surface to web_app.
        pytest.param(
            "Build a browser extension settings page",
            "Settings panel with permission toggles",
            "Runs in browsers",
            id="component_goals_resist_generic_browser_runtimes",
        ),
        # R66 guard: a UI surface declared FOR a component belongs to that component even under a
        # legitimate generic browser runtime.
        pytest.param(
            "Build a settings dashboard for a browser extension",
            "Settings panel with save button",
            "Browser",
            id="postfix_component_targets_own_their_surfaces",
        ),
        # R69 guard: a browser word re-headed by a measurement noun ("browser usage metrics") is what
        # the artifact reports on, and the "for browser use" idiom stays a terminal environment form.
        pytest.param(
            "Build a desktop dashboard with browser usage metrics",
            "Interactive charts with a filters panel",
            "Desktop application",
            id="browser_usage_compounds_are_subject_matter",
        ),
        # R75 guard: a bare browser qualifier is the ambiguous ownership form — under a competing
        # runtime it reads as a subject compound, whatever the topical noun, while explicit web
        # artifact vocabulary still overrides.
        pytest.param(
            "Build a browser compliance dashboard",
            "Interactive charts with a filters panel",
            "Runs as a local Python process",
            id="bare_qualifier_defers_to_a_competing_runtime",
        ),
        # R75 guard: "browser support is out of scope" removes the browser signal like the other
        # postpositive denials.
        pytest.param(
            "Build a dashboard; browser support is out of scope",
            "Interactive charts with a filters panel",
            "Runs as a local Python process",
            id="out_of_scope_is_a_postpositive_denial",
        ),
        # R77 guard: a standardized extension/plugin runtime owns the surface before any goal-side
        # grant — no per-surface vocabulary (browser action, page, panel) is needed.
        pytest.param(
            "Build a Chrome browser action settings page",
            "Settings panel with save button",
            "Browser extension runtime",
            id="component_runtime_is_authoritative_over_the_grant",
        ),
        # R80 guard: "a web app that is a Chrome browser extension" is an extension — identity
        # resolves before every affirmative grant.
        pytest.param(
            "Build a web app that is a Chrome browser extension",
            "Settings panel with save button",
            "Runs in browsers",
            id="component_identity_precedes_the_explicit_grant",
        ),
        # R83 guard: drivers and automation are components in every decision — goal identity and
        # runtime identity alike.
        pytest.param(
            "Build a web app that is a browser driver",
            "Settings panel with save button",
            "Browser driver runtime",
            id="component_vocabulary_is_shared_across_decisions",
        ),
        # R100 guard: a participial content clause names what the product displays — "a desktop
        # dashboard showing a list of web apps" shows web apps and is not one.
        pytest.param(
            "Build a desktop dashboard showing a list of web apps",
            "Interactive charts with a filters panel",
            "Desktop application",
            id="displayed_web_apps_are_content_not_artifact",
        ),
        # R106 guard: an engine attachment names the host's implementation — a desktop app powered by
        # Chromium is hosted natively and the non-browser host dominates.
        pytest.param(
            "Build a native desktop settings dashboard",
            "Settings form and navigation sidebar",
            "Desktop application powered by Chromium",
            id="engine_attachment_is_not_the_environment",
        ),
        # R108 guard: a bare accompaniment preposition before the engine name ships the engine inside
        # the host — an Electron desktop runtime with Chromium stays native on every grant path.
        pytest.param(
            "Build a native desktop dashboard",
            "Settings form and navigation sidebar",
            "Electron desktop runtime with Chromium",
            id="bundled_engine_accompaniment_keeps_native_ownership",
        ),
        # R115 guard: bare "frontend" is not inherently browser-hosted — a native app's frontend stays
        # native under an authoritative non-browser runtime, while a frontend against browsers owns.
        pytest.param(
            "Build the frontend of a native mobile app",
            "Login form and settings page",
            "Native iOS and Android application",
            id="bare_frontend_respects_native_runtime",
        ),
        # R117 guard: a browser merely viewing a document is its consumer, not proof that the produced
        # artifact is a browser application.
        pytest.param(
            "Generate a PDF ebook",
            "Downloadable PDF with chapters",
            "Viewed in modern browsers",
            id="browser_consuming_a_document_is_not_web_app_ownership",
        ),
        # R118 guard: no browser in a passive Oxford-comma consumer list may survive normalization and
        # independently mint browser context.
        pytest.param(
            "Build a kanban board",
            "Drag-and-drop cards and navigation",
            "Presented via Chrome, Safari, and Firefox",
            id="passive_consumer_strips_entire_oxford_browser_list",
        ),
        # R102 guard: "neither a web app nor a website" denies BOTH alternatives — a denied artifact
        # can never become a match or a co-product.
        pytest.param(
            "Build a native desktop dashboard, neither a web app nor a website",
            "Interactive settings panel and navigation bar",
            "Native desktop application",
            id="neither_nor_denies_both_alternatives",
        ),
    ],
)
def test_goal_output_runtime_corpus_withholds_web_app(
    goal: str, outputs: str, runtime_context: str
) -> None:
    """Corpus of goal/outputs/runtime_context ledgers that must not yield WEB_APP."""
    ledger = _bare_ledger(goal)
    _seed_section(ledger, "outputs", value=outputs)
    _seed_section(ledger, "runtime_context", value=runtime_context)
    result = derive_domain_from_ledger(ledger)
    assert TaskClass.WEB_APP not in result.classes
