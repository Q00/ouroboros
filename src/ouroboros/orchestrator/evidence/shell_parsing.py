"""Shell command parsing helpers for evidence verification."""

from __future__ import annotations

from pathlib import Path
import re
import shlex

from ouroboros.orchestrator.evidence.common import (
    _normalize_exact_command,
    _normalized_evidence_text,
)


def _looks_like_test_command(command: str) -> bool:
    """Return True for common whole-suite or targeted test invocations."""
    return _test_command_invocation(command) is not None


# Executables that identify a formal test-runner invocation. Unlike
# ``_looks_like_test_command`` this recognizes the runner even when the command
# disables tests (``mvn -DskipTests``): those must keep flowing through the
# strict test-runner semantics so a skip invocation stays rejected, rather than
# being accepted as a directly-executed verification command.
_TEST_RUNNER_EXECUTABLES = frozenset(
    {"pytest", "py.test", "tox", "nox", "gradle", "gradlew", "mvn", "mvnw", "unittest"}
)


def _command_executable_names(command: str) -> tuple[str, ...]:
    """Return executable names invoked by a command, unwrapping shell + preamble."""
    normalized = command.strip().lower()
    if not normalized:
        return ()
    shell_body = _shell_command_body(normalized)
    body = shell_body if shell_body else normalized
    segments = _segments_after_safe_shell_preamble(body) or (body,)
    executables: list[str] = []
    for segment in segments:
        try:
            parts = shlex.split(segment)
        except ValueError:
            parts = segment.split()
        parts = _strip_env_prefix(parts)
        if not parts:
            continue
        executable = Path(parts[0]).name
        executables.append(executable)
        if executable in {"python", "python3"} and len(parts) >= 3 and parts[1] == "-m":
            executables.append(parts[2])
        if executable == "uv" and len(parts) >= 3 and parts[1] == "run":
            executables.append(Path(parts[2]).name)
    return tuple(executables)


_INLINE_CODE_INTERPRETERS = frozenset(
    {"python", "python3", "python2", "node", "nodejs", "deno", "bun", "ruby", "perl", "php"}
)
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval"})

# A raised/aborting check whose failure would make the interpreter exit non-zero.
# This is what separates a real inline verification (``assert greet('x') == ...``)
# from a bare ``print('OK')`` that always succeeds regardless of correctness.
# ``sys.exit``/``os._exit``/``process.exit`` are included because an inline check
# commonly gates the exit code that way; a bare ``exit(0)`` is not a check but is
# also not something a worker writes to *prove* a test, and requiring one of these
# keyword-level constructs keeps the boundary conservative.
_INLINE_VERIFICATION_CONSTRUCT_RE = re.compile(
    r"\b(assert|assertequal|asserttrue|raise|throw|die|unittest|pytest|expect)\b"
    r"|\b(sys\.exit|os\._exit|process\.exit)\s*\(",
)


def _inline_code_payload(parts: list[str]) -> str | None:
    """Return the inline code string following a ``-c`` / ``-e`` / ``--eval`` flag."""
    for index, part in enumerate(parts):
        if part in _INLINE_CODE_FLAGS and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _is_inline_verification_command(command: str) -> bool:
    """Return True for an interpreter running inline code that performs a real check.

    Such a command executes code whose non-zero exit meaningfully signals a
    failed check (a failed ``assert`` raises and exits non-zero), so a successful
    run can back a ``tests_passed`` claim even without a formal test runner.

    Deliberately narrow on TWO axes so it cannot widen the anti-fabrication
    boundary:

    - Interpreter form: only an inline-code interpreter invocation
      (``python3 -c "..."``, ``node -e "..."``). Read-only / print commands
      (``cat``, ``echo``) and formal test runners are excluded — the former
      carry no verification semantics and the latter must keep flowing through
      the strict node-id / no-tests-ran path.
    - Payload substance: the inline code must contain an assertion / verification
      construct (``assert``, ``raise``, ``throw``, ``unittest``, ``sys.exit(``,
      ...). A bare ``python3 -c "print('OK')"`` performs no check — its exit 0 is
      meaningless as test evidence — so it does NOT qualify.
    """
    normalized = command.strip().lower()
    if not normalized:
        return False
    shell_body = _shell_command_body(normalized)
    body = shell_body if shell_body else normalized
    for segment in _segments_after_safe_shell_preamble(body) or (body,):
        try:
            parts = shlex.split(segment)
        except ValueError:
            parts = segment.split()
        parts = _strip_env_prefix(parts)
        if not parts or Path(parts[0]).name not in _INLINE_CODE_INTERPRETERS:
            continue
        payload = _inline_code_payload(parts)
        if payload is not None and _INLINE_VERIFICATION_CONSTRUCT_RE.search(payload):
            return True
    return False


def _is_test_runner_family_command(command: str) -> bool:
    """Return True when a command invokes a known test-runner executable.

    Used by ``tests_passed`` matching to decide whether a claim must go through
    the strict test-runner semantics (which reject skip-test invocations) or may
    be treated as a directly-executed verification command. Recognizes the
    executable regardless of skip flags, after unwrapping a leading shell wrapper
    and setup preambles.
    """
    if any(name in _TEST_RUNNER_EXECUTABLES for name in _command_executable_names(command)):
        return True
    for name in {"npm", "pnpm", "yarn"}:
        if re.search(rf"(?<![\w.-]){name}\s+test\b", command.lower()):
            return True
    return False


def _test_command_invocation(command: str) -> str | None:
    """Return the backed inner test invocation for a direct or wrapped command.

    Output plumbing (``2>&1``, ``| tail -20``) is peeled first so a clean
    invocation can be extracted from a ``<cmd> 2>&1 | tail -20`` runtime
    command. ``_strip_command_output_plumbing`` is deliberately narrow — only
    presentation-only tails are peeled — so an evidence-altering filter such
    as ``| grep passed`` survives the strip and is rejected downstream by
    ``_test_invocation_from_prefix`` rather than being silently dropped.
    """
    normalized = command.strip().lower()
    if not normalized:
        return None

    direct_candidate = _strip_command_output_plumbing(normalized)
    if (
        _has_trailing_output_filter_pipeline(normalized)
        and not _output_filter_pipeline_is_pipefail_protected(normalized)
        and _test_invocation_from_prefix(direct_candidate) is not None
    ):
        direct_candidate = normalized
    direct = _test_invocation_from_prefix(direct_candidate)
    if direct is not None:
        return direct

    body = _shell_command_body(normalized)
    if body is None:
        return None
    return _test_invocation_from_shell_body(body)


def _test_command_invocation_allowing_output_plumbing(command: str) -> str | None:
    """Return a test invocation after stripping output plumbing unconditionally.

    This must not be used as command proof. It exists only to classify rejected
    evidence forms for diagnostics while preserving the #1208 masking guard.
    """
    normalized = command.strip().lower()
    if not normalized:
        return None
    direct = _test_invocation_from_prefix(_strip_command_output_plumbing(normalized))
    if direct is not None:
        return direct
    body = _shell_command_body(normalized)
    if body is None:
        return None
    for segment, _pipefail_enabled in _segments_after_safe_shell_preamble_with_pipefail(body):
        invocation = _test_invocation_from_prefix(_strip_command_output_plumbing(segment))
        if invocation is not None:
            return invocation
        return None
    return None


def _shell_command_body(command: str) -> str | None:
    """Return the ``-c`` body when the command starts with a shell wrapper."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    shell_name = Path(parts[0]).name
    if shell_name not in {"bash", "zsh", "sh"}:
        return None
    option_index = next(
        (index for index, part in enumerate(parts[1:], start=1) if part in {"-c", "-lc", "-cl"}),
        None,
    )
    if option_index is None or option_index + 1 >= len(parts):
        return None
    return parts[option_index + 1].strip()


def _test_invocation_from_shell_body(body: str) -> str | None:
    """Return a test invocation after conservative shell setup preambles."""
    for segment, pipefail_enabled in _segments_after_safe_shell_preamble_with_pipefail(body):
        candidate = _strip_command_output_plumbing(segment)
        if (
            _has_trailing_output_filter_pipeline(segment)
            and not pipefail_enabled
            and _test_invocation_from_prefix(candidate) is not None
        ):
            candidate = segment
        invocation = _test_invocation_from_prefix(candidate)
        if invocation is not None:
            return invocation
        return None
    return None


def _single_command_after_safe_shell_preamble(command: str) -> str | None:
    """Return a wrapped inner command after only safe setup preambles.

    Generic ``commands_run`` evidence may cite the useful command inside a
    runtime-recorded shell wrapper such as ``cd /work && python scripts/gen.py``.
    Keep this narrower than substring containment: only ignore setup-only
    preambles and only when exactly one non-preamble command remains.
    """
    body = _shell_command_body(command)
    if body is None:
        return None
    segments = tuple(_segments_after_safe_shell_preamble(body))
    if len(segments) != 1:
        return None
    segment = segments[0]
    stripped = _strip_command_output_plumbing(segment)
    if (
        _has_trailing_output_filter_pipeline(segment)
        and not _output_filter_pipeline_is_pipefail_protected(body)
        and _looks_like_test_command(stripped)
    ):
        return None
    return _normalized_evidence_text(stripped)


def _segments_after_safe_shell_preamble(body: str) -> tuple[str, ...]:
    """Return non-preamble shell segments after setup-only commands."""
    return tuple(
        segment
        for segment, _pipefail_enabled in _segments_after_safe_shell_preamble_with_pipefail(body)
    )


def _segments_after_safe_shell_preamble_with_pipefail(body: str) -> tuple[tuple[str, bool], ...]:
    """Return non-preamble segments with pipefail state active before each one."""
    remaining: list[tuple[str, bool]] = []
    pipefail_enabled = False
    for segment in re.split(r"\s*&&\s*", body.strip()):
        normalized_segment = segment.strip()
        if not normalized_segment:
            continue
        if not remaining and _is_safe_test_command_preamble(normalized_segment):
            if _is_pipefail_preamble(normalized_segment):
                pipefail_enabled = True
            continue
        remaining.append((normalized_segment, pipefail_enabled))
    return tuple(remaining)


def _is_safe_test_command_preamble(segment: str) -> bool:
    """Return True for shell setup segments that do not execute tests themselves."""
    try:
        parts = shlex.split(segment)
    except ValueError:
        return False
    if not parts:
        return True
    if parts[0] == "cd" and len(parts) == 2:
        return True
    if _is_pipefail_parts(parts):
        return True
    if parts[0] == "export" and len(parts) > 1:
        return all(_is_env_assignment(part) for part in parts[1:])
    return all(_is_env_assignment(part) for part in parts)


def _is_env_assignment(value: str) -> bool:
    """Return True for a simple shell environment assignment token."""
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", value))


def _strip_env_prefix(parts: list[str]) -> list[str]:
    """Remove leading env assignment tokens before command recognition."""
    index = 0
    if parts and parts[0] == "env":
        index = 1
    while index < len(parts) and _is_env_assignment(parts[index]):
        index += 1
    return parts[index:]


def _has_gradle_or_maven_test_skip(parts: list[str]) -> bool:
    """Return True when a Gradle/Maven command explicitly disables tests."""

    def maven_skip_property_disables_tests(value: str) -> bool:
        normalized_value = value.lower()
        if normalized_value in {"skiptests", "maven.test.skip"}:
            return True
        if normalized_value.startswith("skiptests=") or normalized_value.startswith(
            "maven.test.skip="
        ):
            _, _, property_value = normalized_value.partition("=")
            return property_value not in {"false", "0", "no", "off"}
        return False

    for index, part in enumerate(parts):
        normalized = part.lower()
        if normalized == "-d" and index + 1 < len(parts):
            if maven_skip_property_disables_tests(parts[index + 1]):
                return True
        if normalized == "--define" and index + 1 < len(parts):
            if maven_skip_property_disables_tests(parts[index + 1]):
                return True
        if normalized.startswith("--define="):
            _, _, define_value = normalized.partition("=")
            if maven_skip_property_disables_tests(define_value):
                return True
        if normalized.startswith("-d") and maven_skip_property_disables_tests(normalized[2:]):
            return True
        if normalized == "--exclude-task" and index + 1 < len(parts):
            excluded_task = parts[index + 1].lower().lstrip(":")
            if excluded_task == "test" or excluded_task.endswith(":test"):
                return True
        if normalized.startswith("--exclude-task="):
            _, _, excluded_task = normalized.partition("=")
            excluded_task = excluded_task.lstrip(":")
            if excluded_task == "test" or excluded_task.endswith(":test"):
                return True
        if normalized == "-x" and index + 1 < len(parts):
            excluded_task = parts[index + 1].lower().lstrip(":")
            if excluded_task == "test" or excluded_task.endswith(":test"):
                return True
        if normalized.startswith("-x") and len(normalized) > 2:
            excluded_task = normalized[2:].lstrip(":")
            if excluded_task == "test" or excluded_task.endswith(":test"):
                return True
    return False


def _test_invocation_from_prefix(command: str) -> str | None:
    """Return a normalized test invocation only when it starts the command text.

    Refuses to extract from commands that still contain a residual shell pipe
    after presentation plumbing has been peeled (``pytest x | grep passed``).
    A residual pipe means the runtime command is followed by an
    evidence-transforming filter (``grep`` / ``wc`` / ``tee``); treating the
    bare prefix as the clean test invocation there would let a filtered run
    silently back a clean ``tests_passed`` / ``commands_run`` claim via the
    ``startswith`` widening in ``_runtime_message_supports_command_claim``.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.replace('"', "").replace("'", "").split()
    parts = _strip_env_prefix(parts)
    if not parts:
        return None
    if "|" in parts:
        return None

    if parts[0] in {"pytest", "py.test", "tox", "nox"}:
        return _normalized_evidence_text(" ".join(parts))
    if len(parts) >= 2 and parts[0] in {"npm", "pnpm", "yarn"} and parts[1] == "test":
        return _normalized_evidence_text(" ".join(parts))
    if len(parts) >= 3 and parts[:3] == ["uv", "run", "pytest"]:
        return _normalized_evidence_text(" ".join(parts))
    if (
        len(parts) >= 3
        and parts[:2] == ["python", "-m"]
        and parts[2]
        in {
            "pytest",
            "unittest",
        }
    ):
        return _normalized_evidence_text(" ".join(parts))
    executable = Path(parts[0]).name
    if (
        executable in {"gradle", "gradlew", "mvn", "mvnw"}
        and not _has_gradle_or_maven_test_skip(parts[1:])
        and any(part in {"test", "check", "verify"} or part.endswith(":test") for part in parts[1:])
    ):
        return _normalized_evidence_text(" ".join(parts))
    return None


def _unittest_command_invocation(command: str) -> str | None:
    """Return the embedded ``python -m unittest`` invocation, if present."""
    invocation = _test_command_invocation(command)
    if invocation is None:
        return None
    parts = invocation.split()
    if len(parts) >= 3 and parts[:3] == ["python", "-m", "unittest"]:
        return invocation
    return None


def _looks_like_unittest_command(command: str) -> bool:
    """Return True when a shell command invokes stdlib unittest."""
    return _unittest_command_invocation(command) is not None


# Output-only shell filters: a trailing pipe into one of these is presentation
# or paging, not the work an evidence claim is about.
#
# Deliberately narrow: only filters that pass the output stream through (or
# truncate it positionally) are allowed. ``grep``/``egrep``/``fgrep`` are
# excluded because they can hide failure lines and make a filtered run back a
# clean ``commands_run`` / ``tests_passed`` claim (e.g. ``pytest ... | grep
# passed``). ``tee`` and ``wc`` are excluded for the same reason: ``tee`` can
# redirect the stream and ``wc`` collapses it to a count, both of which alter
# what the runtime would have observed and so weaken anti-fabrication.
_OUTPUT_FILTER_COMMANDS = frozenset({"tail", "head", "cat", "less", "more"})

# Trailing shell output redirection (``2>&1``, ``> log``, ``2> err``, ``&> out``).
_TRAILING_REDIRECT_RE = re.compile(
    r"\s*(?:[0-9]*>{1,2}\s*(?:&[0-9]+|[^\s|]+)|&>{1,2}\s*[^\s|]+)\s*$"
)


def _normalized_shell_words_text(command: str) -> str | None:
    """Return a quote-insensitive normalized argv spelling for one shell command.

    This keeps command evidence matching exact at the argv level while allowing
    common shell spelling differences such as ``--tests "ClassName"`` versus
    ``--tests ClassName``. Commands that cannot be parsed, still contain a
    pipeline, or contain shell control operators are left to the stricter raw
    aliases.
    """
    text = command.strip()
    if not text:
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if not parts or any(part in {"|", "&&", ";", "||"} for part in parts):
        return None
    return _normalized_evidence_text(" ".join(parts))


def _strip_command_output_plumbing(command: str) -> str:
    """Return a command with trailing output redirection and pager pipes removed.

    Agents routinely run ``<cmd> 2>&1 | tail -20`` while their ``commands_run``
    evidence cites the clean ``<cmd>``. The trailing redirection and the
    output-only pager pipe are presentation plumbing, not the work being
    claimed, so they must not block a match. Deliberately conservative:

    - Only trailing output redirections (``2>&1``, ``> log``, ``2> err``,
      ``&> out``) and pipes into a pager-style filter listed in
      ``_OUTPUT_FILTER_COMMANDS`` (``tail``/``head``/``cat``/``less``/``more``)
      are dropped. These pass the underlying stream through (or truncate it
      positionally), so the runtime evidence is unchanged in kind.
    - Filters that *transform* the stream — ``grep`` family, ``wc``, ``tee`` —
      are intentionally not stripped, because they can hide failure lines,
      collapse the stream to a count, or divert it to a file, which would let
      a filtered run back a clean ``commands_run`` / ``tests_passed`` claim.
    - Meaningful pipelines such as ``a | python process.py`` are kept, so a
      partial ``a`` claim is still not proven by an ``a | python process.py``
      runtime command.
    """
    text = command.strip()
    if not text:
        return text
    # Peel output-only filter pipes from the tail (``... | tail -n 20``).
    while "|" in text:
        head, _, tail_segment = text.rpartition("|")
        tail_tokens = tail_segment.split()
        if tail_tokens and tail_tokens[0].lower() in _OUTPUT_FILTER_COMMANDS:
            text = head.strip()
            continue
        break
    # Peel trailing output redirections, possibly several (``2>&1 > log``).
    prev: str | None = None
    while prev != text:
        prev = text
        text = _TRAILING_REDIRECT_RE.sub("", text).strip()
    return text


def _has_trailing_output_filter_pipeline(command: str) -> bool:
    """Return True when ``command`` ends in a pager-style output pipe."""
    text = command.strip()
    while "|" in text:
        head, _, tail_segment = text.rpartition("|")
        tail_tokens = tail_segment.split()
        if tail_tokens and tail_tokens[0].lower() in _OUTPUT_FILTER_COMMANDS:
            return True
        text = head.strip()
    return False


def _output_filter_pipeline_is_pipefail_protected(command: str) -> bool:
    """Return True when pipefail is enabled before the first stripped pipeline."""
    pipefail_enabled = False
    for segment in re.split(r"\s*(?:&&|;)\s*", command.strip()):
        normalized_segment = segment.strip()
        if not normalized_segment:
            continue
        if _is_pipefail_preamble(normalized_segment):
            pipefail_enabled = True
            continue
        if _has_trailing_output_filter_pipeline(normalized_segment):
            return pipefail_enabled
    return False


def _uses_pipefail(command: str) -> bool:
    """Return True when shell text explicitly preserves upstream pipe status."""
    for segment in re.split(r"\s*(?:&&|;)\s*", command.strip()):
        try:
            parts = shlex.split(segment)
        except ValueError:
            continue
        if _is_pipefail_parts(parts):
            return True
    return False


def _is_pipefail_preamble(segment: str) -> bool:
    try:
        parts = shlex.split(segment)
    except ValueError:
        return False
    return _is_pipefail_parts(parts)


def _is_pipefail_parts(parts: list[str]) -> bool:
    return parts == ["set", "-o", "pipefail"]


def _normalized_command_claim_aliases(command: str) -> tuple[str, ...]:
    """Return normalized command forms that a concise evidence claim may use.

    Structured Bash tool inputs may wrap the user command as
    ``/bin/zsh -lc '<body>'``.  The wrapper itself is runtime-backed, so an
    evidence claim may cite the exact shell body without re-stating the wrapper.
    Keep this alias exact: test-command-specific helpers handle conservative
    setup preambles, while generic ``commands_run`` claims should not be proven
    by partial substrings of arbitrary shell scripts.
    """
    normalized = _normalized_evidence_text(command)
    aliases = [normalized] if normalized else []

    def append_alias(candidate: str | None) -> None:
        if candidate and candidate not in aliases:
            aliases.append(candidate)

    append_alias(_normalized_shell_words_text(command))
    shell_body = _shell_command_body(command)
    normalized_shell_body = _normalized_evidence_text(shell_body) if shell_body else None
    append_alias(normalized_shell_body)
    append_alias(_normalized_shell_words_text(shell_body) if shell_body else None)
    test_invocation = _test_command_invocation(command)
    append_alias(test_invocation)
    # A recorded command may append output plumbing (``... 2>&1 | tail -20``)
    # that a concise ``commands_run`` claim omits. Add plumbing-stripped variants
    # so the two still match. Alias matching stays exact (set intersection), so
    # this does not widen proof to arbitrary substrings. Also add argv-normalized
    # plumbing-stripped forms so quoted arguments in the runtime command match
    # unquoted evidence claims for the same argv vector.
    for base in tuple(aliases):
        stripped_raw = _strip_command_output_plumbing(base)
        stripped = _normalized_evidence_text(stripped_raw)
        if (
            stripped
            and stripped != base
            and _has_trailing_output_filter_pipeline(base)
            and _looks_like_test_command(stripped)
            and not _output_filter_pipeline_is_pipefail_protected(base)
        ):
            continue
        append_alias(stripped)
        append_alias(_normalized_shell_words_text(stripped_raw))
    return tuple(aliases)


def _runtime_command_evidence_aliases(command: str) -> tuple[str, ...]:
    """Return exact runtime command aliases for sibling reconciliation."""
    aliases = [_normalize_exact_command(command)]
    single_inner_command = _single_exact_command_after_safe_shell_preamble(command)
    if single_inner_command and single_inner_command not in aliases:
        aliases.append(single_inner_command)
    return tuple(alias for alias in aliases if alias)


def _single_exact_command_after_safe_shell_preamble(command: str) -> str | None:
    """Return one wrapped inner command without lowercasing exact evidence."""
    body = _shell_command_body(command)
    if body is None:
        return None
    segments = tuple(_segments_after_safe_shell_preamble(body))
    if len(segments) != 1:
        return None
    return _normalize_exact_command(segments[0])
