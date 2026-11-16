"""Closure labels for interview events: who called, who answered, why it closed.

The interview events record how many questions an interview asks and why it does
or does not close, split by the surface that drove it. This module builds the
fields for that, and nothing else:

* :func:`surface_fields` labels ``interview.started`` with the calling surface
  (``console``, ``mcp``, ``cli_init``, ``auto``) and, inside an MCP request, the
  host family the MCP server already normalized from ``clientInfo``.
* :func:`answer_source` labels ``interview.response.recorded`` with who answered.
* :func:`closure_snapshot` describes the closure gate after a scored turn.

Every value is a number or a member of a closed label set. Nothing here reads
question, answer or justification text into a field; :func:`answer_source` looks
only at the advertised ``[from-*]`` prefix. The labels are descriptive only:
nothing may use them to authorize, route or relax anything.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AUTO_COMPLETE_STREAK_REQUIRED,
    BROWNFIELD_CONTEXT_CLARITY_FLOOR,
    CONSTRAINT_CLARITY_FLOOR,
    GOAL_CLARITY_FLOOR,
    SUCCESS_CRITERIA_CLARITY_FLOOR,
    AmbiguityScore,
    get_completion_floor_failures,
)
from ouroboros.bigbang.answer_provenance import ANSWER_PROVENANCE_ARGUMENT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.bigbang.interview import InterviewState


class InterviewSurface(StrEnum):
    """The caller that started an interview."""

    CONSOLE = "console"
    MCP = "mcp"
    CLI_INIT = "cli_init"
    AUTO = "auto"
    UNKNOWN = "unknown"


class AnswerSource(StrEnum):
    """Who produced a recorded answer, as far as the interview can tell."""

    FROM_USER = "from_user"
    FROM_CODE = "from_code"
    AUTO_CONFIRMED = "auto_confirmed"
    FROM_RESEARCH = "from_research"
    FROM_AUTO = "from_auto"
    UNKNOWN = "unknown"


class ClosureVerdict(StrEnum):
    """Where an interview stood against the closure gate after a turn."""

    UNSCORED = "unscored"
    ABOVE_THRESHOLD = "above_threshold"
    FLOORS_UNMET = "floors_unmet"
    QUALIFIED = "qualified"
    PASSED = "passed"
    SAFE_DEFAULT = "safe_default"


#: Values of ``ouroboros.mcp.host_context.HostFamily``. Kept as data so this
#: domain module does not import the MCP layer; a test pins the two together.
HOST_LABELS: frozenset[str] = frozenset(
    {"claude_code", "codex", "opencode", "other_known", "unknown"}
)

_current_surface: ContextVar[InterviewSurface | None] = ContextVar(
    "ouroboros_interview_surface",
    default=None,
)


@contextmanager
def use_interview_surface(surface: InterviewSurface) -> Iterator[None]:
    """Label every interview started inside this block with ``surface``.

    In-process callers that own an interview loop (the console back end and
    ``ooo auto``) enter this around their handler calls. It is request scoped,
    like the MCP host context, so a handler instance shared between surfaces is
    labeled by whoever is calling it now.
    """
    token = _current_surface.set(InterviewSurface(surface))
    try:
        yield
    finally:
        _current_surface.reset(token)


def current_interview_surface() -> InterviewSurface | None:
    """Return the surface set by :func:`use_interview_surface`, if any."""
    return _current_surface.get()


def surface_fields(host_context: Any = None) -> dict[str, str | None]:
    """Return the ``surface`` and ``host`` labels for ``interview.started``.

    ``host_context`` is the MCP server's request context
    (``current_mcp_host_context()``); pass ``None`` outside the MCP layer. An
    explicit :func:`use_interview_surface` label wins. Otherwise a call made
    inside an MCP request is ``mcp``, and anything else is ``unknown``. ``host``
    is the normalized host family for calls inside an MCP request, else ``None``.
    """
    host: str | None = None
    if host_context is not None and getattr(host_context, "is_mcp_host", False) is True:
        family = getattr(getattr(host_context, "host_family", None), "value", None)
        host = family if family in HOST_LABELS else "unknown"
    surface = _current_surface.get()
    if surface is None:
        surface = InterviewSurface.MCP if host is not None else InterviewSurface.UNKNOWN
    return {"surface": surface.value, "host": host}


# Checked in order, so a longer prefix comes before the shorter one it extends.
_PREFIX_SOURCES: tuple[tuple[str, AnswerSource], ...] = (
    ("[from-code][auto-confirmed]", AnswerSource.AUTO_CONFIRMED),
    ("[from-code]", AnswerSource.FROM_CODE),
    ("[from-repo]", AnswerSource.FROM_CODE),
    ("[from-research]", AnswerSource.FROM_RESEARCH),
    ("[from-user]", AnswerSource.FROM_USER),
    # ``ooo auto`` relaying the requester's own answer.
    ("[from-auto][requester]", AnswerSource.FROM_USER),
    ("[from-auto]", AnswerSource.FROM_AUTO),
    ("[from-safe-default]", AnswerSource.FROM_AUTO),
    ("[from-assumption]", AnswerSource.FROM_AUTO),
    ("[from-inference]", AnswerSource.FROM_AUTO),
)

# Provenance a caller declares with ``answer_provenance`` (see answer_provenance.py).
_DECLARED_SOURCES: dict[str, AnswerSource] = {
    "requester": AnswerSource.FROM_USER,
    "repo_fact": AnswerSource.FROM_CODE,
    "assumption": AnswerSource.FROM_AUTO,
}

# Surfaces where a person types every unprefixed answer into the interview.
_TYPED_BY_PERSON = frozenset({InterviewSurface.CONSOLE, InterviewSurface.CLI_INIT})


def answer_source(
    answer: object,
    arguments: Mapping[str, Any] | None = None,
    *,
    surface: InterviewSurface | None = None,
) -> str:
    """Return the :class:`AnswerSource` label for one recorded answer.

    A declared ``answer_provenance`` wins, then the advertised ``[from-*]``
    prefix. An unprefixed answer is ``from_user`` on a surface where a person
    types it (console, CLI ``init``) and ``unknown`` elsewhere, because an MCP
    host or ``ooo auto`` may have written it. Only the prefix is inspected.
    """
    declared = (arguments or {}).get(ANSWER_PROVENANCE_ARGUMENT)
    if isinstance(declared, str) and declared in _DECLARED_SOURCES:
        return _DECLARED_SOURCES[declared].value
    text = answer.lstrip()[:64].casefold() if isinstance(answer, str) else ""
    for prefix, source in _PREFIX_SOURCES:
        if text.startswith(prefix):
            return source.value
    caller = surface if surface is not None else _current_surface.get()
    if text and not text.startswith("[from-") and caller in _TYPED_BY_PERSON:
        return AnswerSource.FROM_USER.value
    return AnswerSource.UNKNOWN.value


_FLOORS: tuple[tuple[str, float], ...] = (
    ("goal_clarity", GOAL_CLARITY_FLOOR),
    ("constraint_clarity", CONSTRAINT_CLARITY_FLOOR),
    ("success_criteria_clarity", SUCCESS_CRITERIA_CLARITY_FLOOR),
)
_BROWNFIELD_FLOOR: tuple[str, float] = ("context_clarity", BROWNFIELD_CONTEXT_CLARITY_FLOOR)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def closure_snapshot(
    state: InterviewState,
    score: AmbiguityScore | None,
    *,
    safe_default: bool = False,
) -> dict[str, Any]:
    """Describe the closure gate for ``state`` after a turn scored as ``score``.

    ``score`` is the ambiguity result that decided this turn, or ``None`` when
    the turn was not scored. ``safe_default`` marks a close by ``ooo auto``'s
    audited safe-default synthesis, which bypasses the score. The verdict uses
    the same predicates as the gate (``is_ready_for_seed`` and
    ``get_completion_floor_failures``); ``floors_passed`` names each floor that
    applies to this interview.
    """
    is_brownfield = bool(state.is_brownfield)
    streak = int(state.completion_candidate_streak or 0)
    snapshot: dict[str, Any] = {
        "answered_rounds": sum(1 for item in state.rounds if item.user_response is not None),
        "is_brownfield": is_brownfield,
        "threshold": AMBIGUITY_THRESHOLD,
        "ambiguity_score": None,
        "clarity": None,
        "floors_passed": None,
        "streak": streak,
        "streak_required": AUTO_COMPLETE_STREAK_REQUIRED,
        "verdict": (ClosureVerdict.SAFE_DEFAULT if safe_default else ClosureVerdict.UNSCORED).value,
    }
    if score is None:
        return snapshot

    floors = _FLOORS + ((_BROWNFIELD_FLOOR,) if is_brownfield else ())
    clarity: dict[str, float | None] = {}
    floors_passed: dict[str, bool] = {}
    for name, floor in floors:
        component = getattr(score.breakdown, name, None)
        value = None if component is None else float(component.clarity_score)
        clarity[name] = _rounded(value)
        floors_passed[name] = value is not None and value >= floor

    if safe_default:
        verdict = ClosureVerdict.SAFE_DEFAULT
    elif not score.is_ready_for_seed:
        verdict = ClosureVerdict.ABOVE_THRESHOLD
    elif get_completion_floor_failures(score, is_brownfield=is_brownfield):
        verdict = ClosureVerdict.FLOORS_UNMET
    elif streak >= AUTO_COMPLETE_STREAK_REQUIRED:
        verdict = ClosureVerdict.PASSED
    else:
        verdict = ClosureVerdict.QUALIFIED
    snapshot.update(
        ambiguity_score=_rounded(score.overall_score),
        clarity=clarity,
        floors_passed=floors_passed,
        verdict=verdict.value,
    )
    return snapshot


__all__ = [
    "HOST_LABELS",
    "AnswerSource",
    "ClosureVerdict",
    "InterviewSurface",
    "answer_source",
    "closure_snapshot",
    "current_interview_surface",
    "surface_fields",
    "use_interview_surface",
]
