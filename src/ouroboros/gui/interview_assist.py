"""What the console's Interview mode reads from the server and the repository.

* :func:`readiness_view` is the readiness the page shows. It is read from the
  interview state the server persisted (the stored ambiguity score and its
  breakdown, the completion streak, the brownfield flag and the status) and
  evaluated with the completion gate's own predicates in
  ``bigbang/ambiguity.py``. The page only draws it: it holds no threshold.
* :func:`auto_confirm_source` applies the interview skill's PATH 1a rule to a
  repository investigation (``skills/interview/SKILL.md``): an answer is
  submitted without asking only when it is a checked, descriptive repository
  fact whose every citation is in one manifest or config file and whose cited
  values appear in the answer. Anything else is shown for one-click
  confirmation instead.
* :class:`RecordingInterviewHandler` keeps the parts of each response ``meta``
  the console uses, which ``HandlerInterviewBackend`` does not return: the
  server's ``code_investigation_request``, sent with every question that code
  may answer.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from typing import Any

#: Answer prefixes from the interview's code-fact answer contract
#: (``orchestrator/capabilities/interview_schemas.py``). Bare ``[from-code]`` is
#: the user-confirmed form; ``[auto-confirmed]`` marks a PATH 1a answer.
CONFIRMED_PREFIX = "[from-code]"
AUTO_CONFIRMED_PREFIX = "[from-code][auto-confirmed]"

#: The skill's Dialectic Rhythm Guard: after this many answers in a row that no
#: human judged (auto-confirmed, or a repository answer accepted with one
#: click), the next question goes to the human without a repository answer.
MAX_ASSISTED_IN_A_ROW = 3

# PATH 1a sources: manifests, lock files and CI config, which declare a fact
# rather than compute it. Source code, scripts and docs are never a PATH 1a
# source; an answer read from them is shown for confirmation.
_MANIFEST_NAMES = frozenset(
    {
        ".env.example",
        ".node-version",
        ".nvmrc",
        ".python-version",
        ".tool-versions",
        "Cargo.toml",
        "Gemfile",
        "Pipfile",
        "build.gradle",
        "build.gradle.kts",
        "compose.yaml",
        "compose.yml",
        "composer.json",
        "deno.json",
        "docker-compose.yaml",
        "docker-compose.yml",
        "go.mod",
        "package.json",
        "pom.xml",
        "pyproject.toml",
        "setup.cfg",
        "tsconfig.json",
    }
)
_LOCK_NAMES = frozenset(
    {
        "Cargo.lock",
        "Gemfile.lock",
        "Pipfile.lock",
        "bun.lock",
        "composer.lock",
        "go.sum",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_CI_FILES = frozenset({".gitlab-ci.yml", ".circleci/config.yml", "azure-pipelines.yml"})

_KEY = re.compile(r"""^\s*["']?[A-Za-z0-9_.-]+["']?\s*[:=](?![=<>~])""")
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*")
# Directive and literal words a quoted config line is made of; they say
# nothing about the answer, so matching one does not ground it.
_SYNTAX_WORDS = frozenset(
    {"and", "arg", "cmd", "env", "false", "from", "none", "null", "run", "the", "true", "with"}
)
_GENERIC_KEYS = frozenset(
    {"description", "license", "main", "name", "private", "readme", "type", "version"}
)


def is_manifest_path(path: str) -> bool:
    """Whether ``path`` (relative to the repository root) is a PATH 1a source."""
    posix = PurePosixPath(path.replace("\\", "/"))
    name = posix.name
    relative = posix.as_posix().removeprefix("./")
    if name in _MANIFEST_NAMES or name in _LOCK_NAMES or relative in _CI_FILES:
        return True
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    return relative.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))


def _relative(path: str, workspace: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return candidate.as_posix()


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in _TOKEN.findall(text):
        lowered = token.lower()
        if any(ch.isdigit() for ch in token):
            if len(token) >= 2:
                tokens.add(lowered)
        elif len(token) >= 3 and lowered not in _SYNTAX_WORDS:
            tokens.add(lowered)
    return tokens


def _value_tokens(quote: str) -> set[str]:
    """The tokens of quoted manifest lines that can carry a value.

    On a ``key = value`` / ``"key": value`` line the key only names the field,
    so only the value counts, unless the value is a bare version spec: then the
    key is the thing it versions (``fastapi = "^0.110"``), and counts too unless
    it is a generic metadata field (``version = "1.2.0"``).
    """
    tokens: set[str] = set()
    for line in quote.splitlines():
        match = _KEY.match(line)
        if match is None:
            tokens |= _tokens(line)
            continue
        value = _tokens(line[match.end() :])
        tokens |= value
        if not any(token[0].isalpha() for token in value):
            tokens |= _tokens(line[: match.end()]) - _GENERIC_KEYS
    return tokens


def _grounded(answer: str, quote: str) -> bool:
    """Whether the answer states a value the quoted manifest line holds."""
    lowered = answer.lower()
    return any(
        re.search(rf"(?<![\w.]){re.escape(token)}(?!\w)", lowered) for token in _value_tokens(quote)
    )


def auto_confirm_source(investigation: Any, workspace: Path) -> str | None:
    """The manifest a found answer comes from when PATH 1a allows auto-confirming it.

    All must hold, else ``None`` (the answer is shown for confirmation):

    - the investigator found it: a ``current_behavior`` question, a non-empty
      answer, at least one citation, and every citation passed the evidence
      check against the real workspace (``auto/investigator.judge_reply``);
    - every citation is in the same file, and that file is a manifest, lock
      file or CI config (one candidate source);
    - every citation holds a value the answer states (an exact match, not an
      inference from it).
    """
    if investigation is None or not getattr(investigation, "found", False):
        return None
    answer = str(getattr(investigation, "answer", "") or "").strip()
    spans = tuple(getattr(investigation, "checked", ()) or ())
    if not answer or not spans or getattr(investigation, "failures", ()):
        return None
    paths = {_relative(span.path, workspace) for span in spans}
    if len(paths) != 1:
        return None
    (path,) = paths
    if not is_manifest_path(path):
        return None
    if not all(_grounded(answer, span.quote) for span in spans):
        return None
    return path


def readiness_view(state: Any) -> dict[str, Any] | None:
    """The completion gate as the server evaluates it, for the page to draw.

    ``None`` until the server has scored the interview. ``complete`` is the
    server's own decision (the interview is marked complete); ``qualified`` is
    the gate's score-and-floors predicate; ``streak``/``streak_required`` are the
    two-turn stability check the server applies before it completes on its own.
    """
    from pydantic import ValidationError

    from ouroboros.bigbang.ambiguity import (
        AMBIGUITY_THRESHOLD,
        AUTO_COMPLETE_STREAK_REQUIRED,
        AmbiguityScore,
        ScoreBreakdown,
        completion_floor_checks,
        qualifies_for_seed_completion,
    )

    if state.ambiguity_score is None:
        return None
    is_brownfield = bool(state.is_brownfield)
    view: dict[str, Any] = {
        "score": float(state.ambiguity_score),
        "threshold": AMBIGUITY_THRESHOLD,
        "score_ok": None,
        "dimensions": [],
        "qualified": None,
        "streak": int(state.completion_candidate_streak),
        "streak_required": AUTO_COMPLETE_STREAK_REQUIRED,
        "complete": bool(state.is_complete),
        "brownfield": is_brownfield,
    }
    if not isinstance(state.ambiguity_breakdown, dict):
        return view
    try:
        breakdown = ScoreBreakdown.model_validate(state.ambiguity_breakdown)
    except ValidationError:
        return view
    score = AmbiguityScore(overall_score=float(state.ambiguity_score), breakdown=breakdown)
    view["score_ok"] = score.is_ready_for_seed
    view["qualified"] = qualifies_for_seed_completion(score, is_brownfield=is_brownfield)
    view["dimensions"] = [
        {
            "key": check.key,
            "label": check.label,
            "floor": check.floor,
            "clarity": check.clarity,
            "met": check.met,
        }
        for check in completion_floor_checks(score, is_brownfield=is_brownfield)
    ]
    return view


def issued_question(state: Any) -> str | None:
    """The question the server issued and is waiting on, exactly as it stored it.

    The response text shows it decorated (``(ambiguity: 0.55) …``); echoing that
    back as ``last_question`` would replace the stored question with the
    decorated one, because the server prefers a caller's ``last_question``.
    """
    rounds = state.rounds
    if rounds and rounds[-1].user_response is None and rounds[-1].question:
        return str(rounds[-1].question)
    return None


def load_interview_view(
    state_dir: Path, interview_session_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one interview's persisted state (read-only): its readiness and issued question."""
    from ouroboros.bigbang.interview import InterviewState

    path = state_dir / f"interview_{interview_session_id}.json"
    try:
        state = InterviewState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    return readiness_view(state), issued_question(state)


class RecordingInterviewHandler:
    """Forward to an ``InterviewHandler``, keeping each session's code-fact request.

    ``HandlerInterviewBackend`` turns a response into an ``InterviewTurn`` and
    drops its ``meta``; the console needs ``meta.code_investigation_request``,
    which the server sends with a question it expects code may answer.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._requests: dict[str, dict[str, Any] | None] = {}

    async def handle(self, arguments: dict[str, Any]) -> Any:
        outcome = await self._handler.handle(arguments)
        if not outcome.is_err:
            meta = outcome.value.meta or {}
            session_id = meta.get("session_id") or arguments.get("session_id")
            if isinstance(session_id, str) and session_id:
                request = meta.get("code_investigation_request")
                self._requests[session_id] = (
                    {
                        key: request.get(key)
                        for key in ("question", "question_identity", "answer_prefixes")
                    }
                    if isinstance(request, dict)
                    else None
                )
        return outcome

    def code_request(self, session_id: str) -> dict[str, Any] | None:
        """The code-fact request of the session's latest response, if it carried one."""
        return self._requests.get(session_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handler, name)


__all__ = [
    "AUTO_CONFIRMED_PREFIX",
    "CONFIRMED_PREFIX",
    "MAX_ASSISTED_IN_A_ROW",
    "RecordingInterviewHandler",
    "auto_confirm_source",
    "is_manifest_path",
    "issued_question",
    "load_interview_view",
    "readiness_view",
]
