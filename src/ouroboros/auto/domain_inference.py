"""Ledger-derived task-class inference (L1-b of #1157 / #1171).

The Socratic interview already extracts structured `SeedDraftLedger`
entries (`actors`, `inputs`, `outputs`, `runtime_context`, …) and
*standardizes them toward canonical vocabulary* (e.g. "do you mean
stdout, stderr, or both?"). L1-b derives the task class from those
already-standardized entries by deterministic pattern matching — no
new LLM call, no eval set, no accuracy floor.

The matcher returns one of three outcomes:

- ``DomainInference.single(...)`` — exactly one class predicate fired.
- ``DomainInference.ambiguous(...)`` — multiple classes fired; the
  interview driver should ask a disambiguation question (L1-c).
- ``DomainInference.single(LIBRARY, reason="unmatched")`` — no
  predicate fired; falls to the safest default and emits a
  ``domain_unmatched`` telemetry signal so maintainers can grow the
  catalog.

Adding a new task class = adding a ``_matches_<name>`` function +
registering it in ``_PATTERN_REGISTRY`` + a unit test. ~10 LoC PR per
class, not an ML eval-set re-curation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ouroboros.auto.ledger import LedgerSection, LedgerStatus, SeedDraftLedger
from ouroboros.auto.task_classes import TaskClass

__all__ = [
    "DomainInference",
    "derive_domain_from_ledger",
    "register_pattern",
]


_PatternFn = Callable[[SeedDraftLedger], bool]


@dataclass(frozen=True, slots=True)
class DomainInference:
    """Outcome of pattern-matching a ledger against the L1-a catalog.

    ``classes`` carries every class whose predicate fired:

    - len(classes) == 0 → unmatched; ``fallback`` carries the safe default
      and ``reason == "unmatched"``.
    - len(classes) == 1 → single, deterministic match; ``fallback`` is
      ``None``.
    - len(classes) >= 2 → ambiguous; ``fallback`` is ``None`` and the
      interview driver should disambiguate before proceeding.
    """

    classes: frozenset[TaskClass]
    reason: str
    fallback: TaskClass | None = None
    matched_signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_single(self) -> bool:
        return len(self.classes) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.classes) >= 2

    @property
    def is_unmatched(self) -> bool:
        return len(self.classes) == 0

    @property
    def single(self) -> TaskClass | None:
        """Return the single matched class, or ``fallback`` for unmatched.

        Ambiguous outcomes return ``None`` — callers should branch on
        :attr:`is_ambiguous` before reading this.
        """
        if self.is_single:
            return next(iter(self.classes))
        if self.is_unmatched:
            return self.fallback
        return None


# ---------------------------------------------------------------------------
# Pattern functions — one per class. Each consumes the ledger's already-
# standardized entries (lowercase substring matching after normalization)
# and returns True iff *its* class is plausible.
#
# Patterns are intentionally conservative: a pattern can fire even when
# another also fires (that produces an ambiguous DomainInference, which
# the interview driver disambiguates). A class never fires when the
# corresponding interview answer is absent or empty.
# ---------------------------------------------------------------------------


def _section_text(ledger: SeedDraftLedger, section: str) -> str:
    """Return the concatenated active-entry text for *section*, lowercased.

    Inactive statuses (WEAK / CONFLICTING / BLOCKED) are excluded — the
    interview standardizer's confirmed/defaulted/inferred entries are what
    represents the user's *current best understanding*.
    """
    sec: LedgerSection | None = ledger.sections.get(section)
    if sec is None:
        return ""
    inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    parts: list[str] = []
    for entry in sec.entries:
        if entry.status in inactive:
            continue
        if not entry.value:
            continue
        parts.append(entry.value)
    return "\n".join(parts).lower()


def _any_of(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _goal_text(ledger: SeedDraftLedger) -> str:
    return _section_text(ledger, "goal")


def _matches_cli(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    runtime = _section_text(ledger, "runtime_context")
    if not (outputs or runtime):
        return False
    output_signal = _any_of(
        outputs,
        ("stdout", "exit code", "printed", "console output", "command output"),
    )
    runtime_signal = _any_of(runtime, ("shell", "terminal", "subprocess", "command line"))
    goal_signal = _any_of(_goal_text(ledger), ("cli", "command line", "command-line"))
    # CLI requires either explicit runtime *or* a goal+output combo.
    return runtime_signal or (output_signal and (goal_signal or outputs))


def _matches_webhook(ledger: SeedDraftLedger) -> bool:
    inputs = _section_text(ledger, "inputs")
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (inputs or outputs or goal):
        return False
    has_webhook_in = _any_of(
        inputs + " " + goal,
        ("webhook", "http post", "incoming event", "event payload", "callback url"),
    )
    has_side_effect = _any_of(
        outputs,
        ("side effect", "db row", "database row", "log entry", "stored", "external call"),
    )
    return has_webhook_in and has_side_effect


def _matches_web_service(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    api_signal = _any_of(
        outputs + " " + goal,
        (
            "rest endpoint",
            "rest api",
            "http response",
            "json body",
            "multiple endpoints",
            "web service",
            "web server",
            "http server",
        ),
    )
    return api_signal and not _matches_webhook(ledger)


def _matches_data_pipeline(ledger: SeedDraftLedger) -> bool:
    inputs = _section_text(ledger, "inputs")
    outputs = _section_text(ledger, "outputs")
    if not (inputs and outputs):
        return False
    input_signal = _any_of(
        inputs,
        ("dataset", "csv", "parquet", "log file", "log files", "input file", "batch"),
    )
    output_signal = _any_of(
        outputs,
        ("aggregated", "transformed", "parquet", "summarized", "rolled up", "output dataset"),
    )
    return input_signal and output_signal


def _matches_game_2d(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    return _any_of(
        outputs + " " + goal,
        ("render", "frame", "canvas", "screen", "game loop", "playable", "2d game", "scene"),
    )


def _matches_refactor_in_place(ledger: SeedDraftLedger) -> bool:
    goal = _goal_text(ledger)
    constraints = _section_text(ledger, "constraints")
    if not goal:
        return False
    refactor_intent = _any_of(
        goal,
        ("refactor", "rewrite", "restructure", "extract module", "split module"),
    )
    preserve_behaviour = _any_of(
        constraints + " " + goal,
        ("preserve behavior", "preserve behaviour", "same tests", "behaviour preserved"),
    )
    # Intent alone is enough; the constraint just strengthens confidence.
    return refactor_intent or (
        # Some users phrase as a constraint without saying "refactor" in goal.
        _any_of(goal, ("clean up", "tidy", "reorganize", "reorganise")) and preserve_behaviour
    )


def _matches_library(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    return _any_of(
        outputs + " " + goal,
        (
            "library",
            "package",
            "module",
            "api surface",
            "importable",
            "public api",
            "sdk",
        ),
    )


_PATTERN_REGISTRY: dict[TaskClass, _PatternFn] = {
    TaskClass.CLI: _matches_cli,
    TaskClass.WEBHOOK: _matches_webhook,
    TaskClass.WEB_SERVICE: _matches_web_service,
    TaskClass.DATA_PIPELINE: _matches_data_pipeline,
    TaskClass.GAME_2D: _matches_game_2d,
    TaskClass.REFACTOR_IN_PLACE: _matches_refactor_in_place,
    TaskClass.LIBRARY: _matches_library,
}


def register_pattern(task_class: TaskClass, pattern_fn: _PatternFn) -> None:
    """Register a pattern function for *task_class*.

    Intended for tests and future extension PRs that add a new
    :class:`TaskClass` value. Production code should not call this — the
    static :data:`_PATTERN_REGISTRY` covers the 7-class catalog
    declared in #1173.
    """
    _PATTERN_REGISTRY[task_class] = pattern_fn


def derive_domain_from_ledger(ledger: SeedDraftLedger) -> DomainInference:
    """Run every registered pattern against *ledger* and classify.

    Outcomes:

    - **Single match** — exactly one pattern fired. Returns
      ``DomainInference`` with ``classes = {that_class}`` and
      ``reason = "single pattern match"``.
    - **Ambiguous** — two or more patterns fired. Returns
      ``DomainInference`` with the fired classes and
      ``reason = "multiple patterns matched"``. The interview driver
      should ask a disambiguation question (L1-c).
    - **Unmatched** — no pattern fired. Returns ``DomainInference`` with
      empty ``classes``, ``fallback = LIBRARY`` (the safest completion
      gate), and ``reason = "unmatched"``. Callers should also emit a
      ``domain_unmatched`` telemetry event.
    """
    fired: list[TaskClass] = []
    signals: list[str] = []
    for task_class, pattern_fn in _PATTERN_REGISTRY.items():
        if pattern_fn(ledger):
            fired.append(task_class)
            signals.append(task_class.value)
    if not fired:
        return DomainInference(
            classes=frozenset(),
            reason="unmatched",
            fallback=TaskClass.LIBRARY,
            matched_signals=(),
        )
    if len(fired) == 1:
        return DomainInference(
            classes=frozenset(fired),
            reason="single pattern match",
            fallback=None,
            matched_signals=tuple(signals),
        )
    return DomainInference(
        classes=frozenset(fired),
        reason="multiple patterns matched",
        fallback=None,
        matched_signals=tuple(signals),
    )
