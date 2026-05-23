"""Task-class catalog for L1 of #1157.

`ooo auto` historically treated every goal as if it were a library with
unit-test acceptance. L1 of the Meta SSOT (#1157) introduces a *task
class* concept — a coarse classification of *what shape of artifact the
goal produces* — so the Seed Architect can inject class-appropriate
default acceptance criteria and the runtime evidence layer (L3) can
bind class-appropriate probes.

This module is **catalog data only** (L1-a sub-PR of #1171). It does
*not* implement domain inference from the ledger (L1-b), interview-driver
disambiguation (L1-c), the Seed AC injection hook (L1-d), or the
result-envelope surface (L1-e).

Design constraints honored:

- **Plain strings, no LLM, no eval set.** The 7-class enum is frozen at
  this PR; growth happens via PR-per-class (~10 LoC + a unit test),
  not via re-curating a training corpus.
- **Decoupled from `domain_profile.DomainProfile`.** The existing
  `DomainProfile` is a *meta-domain* concept (coding / research /
  design) and carries cross-domain machinery (`repo_context_extractor`,
  `intent_classifier`, `vague_terms`, `detector`, `verifiable_predicates`,
  `safe_defaults`). Task classes are *within-meta-domain* shapes
  (cli / webhook / game-2d / ...) and carry only shape-specific data
  (completion mode, default AC, probe-kind hints). The two taxonomies
  are orthogonal; `safe_defaults` remains intentionally on the
  meta-domain layer and is not duplicated here. The earlier #1171
  schema sketch listed `safe_defaults` as a per-class field, but on
  implementation review that field is meta-domain-scoped (e.g.
  "default to pytest" applies to all coding task classes equally) and
  belongs on `DomainProfile`, not on this catalog.
- **`default_ac_template` is `tuple[str, ...]` matching
  `Seed.acceptance_criteria` exactly.** No new AC dataclass is needed
  because the Seed already accepts plain strings.
- **`runtime_probe_kinds` is a `tuple[str, ...]` placeholder.** L3 is
  still pending its minimal-substrate audit; the strings stay
  human-readable for now and become a typed enum when L3 lands.
- **Serialization uses underscored identifiers** (`web_service`,
  `data_pipeline`, `game_2d`, `refactor_in_place`) so `TaskClass.value`
  is a valid Python identifier and a JSON-safe ledger key. Prose docs
  in #1157 / #1171 may render the same names with hyphens; both refer
  to the same class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "CompletionMode",
    "TASK_CLASS_CATALOG",
    "TaskClass",
    "TaskClassProfile",
    "get_task_class_profile",
]


class CompletionMode(StrEnum):
    """How a task is judged "done".

    - ``CODE_COMPLETE``: tests + lint + (for libraries) a usable API surface.
      Sufficient when the goal is a library / refactor whose value is fully
      captured by passing tests.
    - ``PRODUCT_COMPLETE``: ``CODE_COMPLETE`` *plus* a runtime acceptance
      probe (L3) demonstrates the produced artifact actually runs. Used
      for CLIs / services / games / pipelines where the user's value
      comes from execution, not just compilation.
    """

    CODE_COMPLETE = "code_complete"
    PRODUCT_COMPLETE = "product_complete"


class TaskClass(StrEnum):
    """Canonical task classes for L1-a.

    Frozen at this PR. Additional classes (``game_3d``, ``desktop_app``,
    ``notebook_analysis``, ...) land as their own follow-up PRs of
    ~10 LoC plus a unit test once a canonical scenario demonstrates
    real need.
    """

    LIBRARY = "library"
    CLI = "cli"
    WEB_SERVICE = "web_service"
    WEBHOOK = "webhook"
    DATA_PIPELINE = "data_pipeline"
    GAME_2D = "game_2d"
    REFACTOR_IN_PLACE = "refactor_in_place"


@dataclass(frozen=True, slots=True)
class TaskClassProfile:
    """Per-class catalog entry.

    Attributes
    ----------
    name:
        The :class:`TaskClass` value, surfaced as a plain string for
        downstream envelope consumers that should not import the enum.
    default_completion_mode:
        Which :class:`CompletionMode` the auto pipeline uses when this
        class is inferred and the user did not pick a mode explicitly.
    default_ac_template:
        The acceptance criteria the Seed Architect prepends to the
        user-supplied AC when this class is active. Plain strings to
        match ``Seed.acceptance_criteria``'s shape exactly.
    runtime_probe_kinds:
        Placeholder identifiers (plain strings) for the runtime probes
        L3 will bind to this class. The set is intentionally narrow at
        v1; L3's design audit (#1157 freshness sync 2026-05-22) will
        likely converge on ``headless_run`` plus a small number of
        scenario-specific add-ons rather than the original 4-probe
        ambition.
    """

    name: str
    default_completion_mode: CompletionMode
    default_ac_template: tuple[str, ...]
    runtime_probe_kinds: tuple[str, ...]


def _profile(
    *,
    name: TaskClass,
    completion: CompletionMode,
    ac_template: tuple[str, ...],
    probes: tuple[str, ...],
) -> TaskClassProfile:
    return TaskClassProfile(
        name=name.value,
        default_completion_mode=completion,
        default_ac_template=ac_template,
        runtime_probe_kinds=probes,
    )


_CATALOG: dict[TaskClass, TaskClassProfile] = {
    TaskClass.LIBRARY: _profile(
        name=TaskClass.LIBRARY,
        completion=CompletionMode.CODE_COMPLETE,
        ac_template=(
            "All public API symbols are importable from the documented module path.",
            "Unit tests cover every public function/method's primary success path.",
            "`ruff check` and the project's type-check command exit 0.",
        ),
        probes=("import_smoke", "unit_tests"),
    ),
    TaskClass.CLI: _profile(
        name=TaskClass.CLI,
        completion=CompletionMode.PRODUCT_COMPLETE,
        ac_template=(
            "Invoking the command with the documented arguments exits with status 0.",
            "Stdout is deterministic for a given input (no embedded timestamps, no random ids).",
            "An invalid argument exits with a non-zero status and prints a human-readable error.",
        ),
        probes=("headless_run", "stdout_golden"),
    ),
    TaskClass.WEB_SERVICE: _profile(
        name=TaskClass.WEB_SERVICE,
        completion=CompletionMode.PRODUCT_COMPLETE,
        ac_template=(
            "Each documented endpoint returns the contracted response shape under a smoke request.",
            "An unknown endpoint returns 404; a malformed body returns 4xx (not 5xx).",
            "The service starts and shuts down cleanly via the documented launch command.",
        ),
        probes=("headless_run", "api_smoke"),
    ),
    TaskClass.WEBHOOK: _profile(
        name=TaskClass.WEBHOOK,
        completion=CompletionMode.PRODUCT_COMPLETE,
        ac_template=(
            "Posting the documented payload to the receiver endpoint returns 2xx.",
            "The documented side effect (DB row / file write / external call) is observable after the request.",
            "An invalid payload returns a 4xx without raising an unhandled exception.",
        ),
        probes=("api_smoke", "side_effect_probe"),
    ),
    TaskClass.DATA_PIPELINE: _profile(
        name=TaskClass.DATA_PIPELINE,
        completion=CompletionMode.PRODUCT_COMPLETE,
        ac_template=(
            "Running the pipeline against the input fixture produces an output that matches the expected shape exactly.",
            "Re-running with the same input is deterministic (no time/random drift).",
            "An empty or malformed input is rejected with a clear error rather than producing partial output.",
        ),
        probes=("headless_run", "output_fixture_diff"),
    ),
    TaskClass.GAME_2D: _profile(
        name=TaskClass.GAME_2D,
        completion=CompletionMode.PRODUCT_COMPLETE,
        ac_template=(
            "A headless simulation of N input ticks produces a state-change trace (player position, score, or scene transition).",
            "The game's main loop terminates cleanly on a quit signal (no hang, no crash).",
            "The runnable build (script or browser bundle) launches without missing-asset errors.",
        ),
        probes=("sim_trace", "headless_run"),
    ),
    TaskClass.REFACTOR_IN_PLACE: _profile(
        name=TaskClass.REFACTOR_IN_PLACE,
        completion=CompletionMode.CODE_COMPLETE,
        ac_template=(
            "The full pre-existing test suite passes after the refactor with no skips added.",
            "Public API surface (importable symbols, function signatures) is preserved unless the refactor goal explicitly broadens it.",
            "No new top-level dependencies are added unless explicitly requested.",
        ),
        probes=("test_suite_parity",),
    ),
}


TASK_CLASS_CATALOG: Mapping[TaskClass, TaskClassProfile] = MappingProxyType(_CATALOG)
"""Immutable view of the frozen 7-class catalog.

Callers should treat this as the authoritative source of per-class
defaults. Adding a class = adding an entry here + a unit test. Modifying
an existing class's ``default_ac_template`` is allowed but should be
audited against existing canonical scenarios that pin those AC.
"""


def get_task_class_profile(task_class: TaskClass) -> TaskClassProfile:
    """Return the catalog entry for *task_class*.

    Raises ``KeyError`` if *task_class* is not in the catalog — that
    should not happen for any value of the :class:`TaskClass` enum
    because the enum and catalog are kept in lockstep (enforced by
    ``test_task_classes_match_catalog``).
    """
    return TASK_CLASS_CATALOG[task_class]
