"""What the console's Interview mode reads from the server and the repository.

* :func:`readiness_view` is the readiness the page shows. It is read from the
  interview state the server persisted (the stored ambiguity score and its
  breakdown, the completion streak, the brownfield flag and the status) and
  evaluated with the completion gate's own predicates in
  ``bigbang/ambiguity.py``. The page only draws it: it holds no threshold.
* :func:`auto_confirm_source` applies the interview skill's PATH 1a rule to a
  repository investigation (``skills/interview/SKILL.md``): an answer is
  submitted without asking only when it is a checked, descriptive repository
  fact whose every citation is in one manifest or config file and declares
  the dependency or tool the answer names, at a version the answer does not
  contradict. Anything else is shown for one-click confirmation instead.
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

# ``key = value`` / ``"key": value`` / ``- key: value``; the key may be a scoped
# package name (``"@types/node"``). ``==``, ``<=`` and ``~=`` are requirement
# specifiers (``fastapi==0.110``), not assignments.
_KEY = re.compile(
    r"""^\s*(?:-\s+)?["']?(?P<key>@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)["']?\s*[:=](?![=<>~])"""
)
_TABLE_HEADER = re.compile(r"""^\s*\[\[?[^\]"'=]*\]\]?\s*$""")
# A name or version: ``react``, ``node.js``, ``3.12-slim``, ``@types/node``. An
# ``@`` inside a token separates a name from its version (``react@18``).
_SEGMENT = r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*"
_TOKEN = re.compile(rf"(?:(?<![A-Za-z0-9._+-])@)?{_SEGMENT}(?:/@?{_SEGMENT})*")
_VERSION = re.compile(r"v?\d[A-Za-z0-9._+-]*")
# Field, directive and connective words (``requires-python``, ``version = …``,
# ``FROM … AS …``, "3.11 or 3.12"). They name no dependency, so a value or a
# version never belongs to one.
_FIELD_WORDS = frozenset(
    {"and", "arg", "as", "cmd", "env", "false", "from", "none", "null", "or", "run", "the", "true"}
    | {"with", "max", "maximum", "min", "minimum", "require", "required", "requires", "version"}
    | {"versions"}
)
# Metadata fields: their value describes the project itself, not a dependency
# it names, so a line keyed by one never grounds an answer.
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


def _key_subject(key: str) -> str | None:
    """What a manifest key declares, or ``None`` for a metadata field.

    A package key is its own subject (``react``, ``@types/node``). A field key
    names the tool its version belongs to (``requires-python``,
    ``python-version`` → ``python``).
    """
    key = key.lower()
    if key in _GENERIC_KEYS:
        return None
    if key.startswith("@") or "/" in key:
        return key
    words = re.split(r"[-_]", key)
    kept = [word for word in words if word and word not in _FIELD_WORDS]
    if len(kept) == len(words):
        return key
    return "-".join(kept) or None


def _line_facts(line: str) -> tuple[dict[str, set[str]], bool]:
    """What one quoted manifest line declares: each name with the versions it holds.

    A version belongs to the name before it on the line (``python:3.12``,
    ``"typer>=0.12"``), or to the key's subject when the value is a bare version
    spec (``"react": "^18.2.0"``); a version with neither belongs to nothing. A
    line that versions something declares only what it versions (``FROM
    python:3.12 AS build`` declares ``python``). The flag says whether an answer
    must name every declared name: true on a line that versions nothing, where
    no word on it can be told apart as the subject.
    """
    if _TABLE_HEADER.match(line):
        return {}, False
    subject: str | None = None
    value = line
    match = _KEY.match(line)
    if match is not None:
        subject = _key_subject(match["key"])
        if subject is None:
            return {}, False
        value = line[match.end() :]
    facts: dict[str, set[str]] = {}
    current: str | None = None
    for token in _TOKEN.findall(value):
        lowered = token.lower()
        if _VERSION.fullmatch(lowered):
            owner = current or subject
            if owner is not None:
                facts.setdefault(owner, set()).add(lowered)
        elif len(lowered) >= 2 and lowered not in _FIELD_WORDS:
            current = lowered
            facts.setdefault(lowered, set())
    versioned = {name: versions for name, versions in facts.items() if versions}
    return (versioned, False) if versioned else (facts, True)


def _is_name(token: str, name: str) -> bool:
    """Whether an answer token names a cited name, as a whole name.

    ``react`` is named by ``React``, ``react@18`` and ``React.js``, not by
    ``preact``, ``react-dom`` or ``@types/react``.
    """
    return token == name or token.startswith(f"{name}.")


def _version_supported(stated: str, cited: set[str]) -> bool:
    """Whether a stated version is one a citation holds, or a prefix of one.

    ``18`` is supported by ``18.2.0`` and ``3.12`` by ``3.12-slim``; ``18.2``
    is not supported by ``18``, nor ``1.2`` by ``1.22``.
    """
    stated = stated.removeprefix("v")
    for version in cited:
        version = version.removeprefix("v")
        if version == stated or (version.startswith(stated) and version[len(stated)] in "._+-"):
            return True
    return False


def _supports(answer: str, quotes: list[str]) -> bool:
    """Whether every cited manifest line supports the answer's fact.

    Each quote must declare something the answer names: the cited key or
    dependency, not just a value it holds (``"react": "18"`` does not support
    "uses Vue 18"). Every version the answer states must follow a name the
    citations version, and be one they hold for it ("React 17" is contradicted
    by ``"react": "^18.2.0"``; in "React and Vue 18" the 18 is Vue's).
    """
    tokens = [token.lower() for token in _TOKEN.findall(answer)]
    cited: dict[str, set[str]] = {}
    for quote in quotes:
        supported = False
        for line in quote.splitlines():
            facts, all_names = _line_facts(line)
            for name, versions in facts.items():
                cited.setdefault(name, set()).update(versions)
            named = {name for name in facts if any(_is_name(token, name) for token in tokens)}
            if named and (named == set(facts) or not all_names):
                supported = True
        if not supported:
            return False
    owner: str | None = None  # the name the answer last mentioned
    for token in tokens:
        if _VERSION.fullmatch(token):
            held = {
                version
                for name, versions in cited.items()
                if owner is not None and _is_name(owner, name)
                for version in versions
            }
            if not _version_supported(token, held):
                return False
        elif len(token) >= 2 and token not in _FIELD_WORDS:
            owner = token
    return True


def auto_confirm_source(investigation: Any, workspace: Path) -> str | None:
    """The manifest a found answer comes from when PATH 1a allows auto-confirming it.

    All must hold, else ``None`` (the answer is shown for confirmation):

    - the investigator found it: a ``current_behavior`` question, a non-empty
      answer, at least one citation, and every citation passed the evidence
      check against the real workspace (``auto/investigator.judge_reply``);
    - every citation is in the same file, and that file is a manifest, lock
      file or CI config (one candidate source);
    - every citation declares something the answer names, the cited key or
      dependency and not only a value it holds, and every version the answer
      states is one the citations hold for it (an exact match, not an
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
    if not _supports(answer, [str(span.quote) for span in spans]):
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
