"""Shell command parsing helpers for evidence verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import shlex

from ouroboros.orchestrator.evidence.common import (
    _normalize_exact_command,
    _normalized_evidence_text,
)


def _looks_like_test_command(command: str) -> bool:
    """Return True for common whole-suite or targeted test invocations."""
    return _test_command_invocation(command) is not None


def _test_command_invocation(command: str) -> str | None:
    """Return the backed inner test invocation for a direct or wrapped command.

    Output plumbing (``2>&1``, ``| tail -20``) is peeled first so a clean
    invocation can be extracted from a ``<cmd> 2>&1 | tail -20`` runtime
    command. ``_strip_command_output_plumbing`` is deliberately narrow — only
    presentation-only tails are peeled — so an evidence-altering filter such
    as ``| grep passed`` survives the strip and is rejected downstream by
    ``_test_invocation_from_prefix`` rather than being silently dropped.
    """
    normalized = command.strip()
    if not normalized:
        return None

    leading_cd = _split_leading_cd(normalized)
    if leading_cd is not None:
        return _test_invocation_from_prefix(leading_cd[1])

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
    invocation = _test_invocation_from_shell_body(body)
    if invocation is not None:
        return invocation
    # Nested wrapper (``zsh -lc "zsh -lc '<test cmd>'"``): the body is itself a
    # wrapper, so look one layer further. Recursion ends when the body is no
    # longer a shell ``-c`` form.
    return _test_command_invocation(body)


# Characters that give a bare ``cd <dir> && <cmd>`` any shell meaning beyond
# "change directory, then run one command". The single ``&&`` is checked
# separately; any other control operator, redirection, substitution, grouping,
# or line break keeps the command unrecognized.
_LEADING_CD_FORBIDDEN_CHARACTERS = frozenset("`$;|<>(){}\n\r")


def _split_leading_cd(command: str) -> tuple[str, str] | None:
    """Return ``(relative_dir, remainder)`` for ``cd <relative-dir> && <cmd>``.

    Recognized only when the whole text is exactly one ``cd`` with one
    workspace-relative directory, one standalone ``&&`` token, and a remainder
    free of shell operators. A second ``&&``, ``;``, ``||``, a pipe (including
    ``| tail``), a redirection, or an absolute, home-relative, or ``..``
    directory returns None, so such text stays unrecognized as before.
    """
    text = command.strip()
    if text.count("&") != 2 or "&&" not in text:
        return None
    if any(char in _LEADING_CD_FORBIDDEN_CHARACTERS for char in text):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if len(parts) < 4 or parts[0] != "cd" or parts[2] != "&&":
        return None
    if not _is_workspace_relative_directory(parts[1]):
        return None
    remainder = text.split("&&", 1)[1].strip()
    if not remainder:
        return None
    return parts[1], remainder


def _is_workspace_relative_directory(value: str) -> bool:
    """Return True for a lexically workspace-confined relative directory."""
    if not value or value[0] in {"/", "~", "-"}:
        return False
    if any(char in value for char in "\\*?["):
        return False
    return ".." not in PurePosixPath(value).parts


def _test_command_invocation_allowing_output_plumbing(command: str) -> str | None:
    """Return a test invocation after stripping output plumbing unconditionally.

    This must not be used as command proof. It exists only to classify rejected
    evidence forms for diagnostics while preserving the #1208 masking guard.
    """
    normalized = command.strip()
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
    """Return the command body when the command starts with a shell wrapper.

    Codex uses POSIX ``sh -lc`` wrappers on Unix and a native
    ``pwsh.exe -Command`` wrapper on Windows.  Both are transport wrappers;
    evidence may cite the exact inner command without repeating the wrapper.
    """
    if _has_unquoted_status_masking_control(command):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    argv = tuple(parts)
    return _shell_command_body_from_argv(argv) or _powershell_command_body_from_argv(argv)


_POWERSHELL_EXECUTABLES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
_POWERSHELL_FLAG_OPTIONS = frozenset(
    {
        "-nologo",
        "-noprofile",
        "-noninteractive",
        "-noni",
        "-sta",
        "-mta",
    }
)
_POWERSHELL_VALUE_OPTIONS = frozenset(
    {
        "-executionpolicy",
        "-inputformat",
        "-outputformat",
        "-windowstyle",
        "-workingdirectory",
    }
)


def _powershell_command_body_from_argv(argv: tuple[str, ...]) -> str | None:
    """Return a PowerShell ``-Command`` body from a narrow wrapper prefix.

    Only non-executing host options are allowed before ``-Command`` and no
    trailing arguments are accepted.  This keeps aliasing exact and prevents a
    partial inner command from proving a larger PowerShell script.
    """
    if len(argv) < 3:
        return None
    executable = Path(argv[0].replace("\\", "/")).name.lower()
    if executable not in _POWERSHELL_EXECUTABLES:
        return None

    index = 1
    while index < len(argv):
        option = argv[index].lower()
        if option in {"-command", "-c"}:
            if index + 2 != len(argv):
                return None
            return argv[index + 1].strip()
        if option in _POWERSHELL_FLAG_OPTIONS:
            index += 1
            continue
        if option in _POWERSHELL_VALUE_OPTIONS:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        return None
    return None


_SHELL_OPTIONS_WITH_ARGUMENT = frozenset({"-O", "+O", "-o", "+o", "--init-file", "--rcfile"})
_SHELL_NON_EXECUTING_OPTIONS = frozenset(
    {
        "-n",
        "--noexec",
        "--version",
        "--help",
        "-D",
        "--dump-strings",
        "--dump-po-strings",
        "--pretty-print",
    }
)


def _shell_command_body_from_argv(argv: tuple[str, ...]) -> str | None:
    """Return a shell ``-c`` body only while parsing the option prefix.

    Once a script operand is encountered, later values are positional
    arguments even when they happen to be spelled ``-c``. Options that consume
    a following value remain within the prefix but cannot expose that value as
    a command body.
    """
    if len(argv) < 3:
        return None
    shell_name = Path(argv[0]).name
    if shell_name not in {"bash", "zsh", "sh"}:
        return None
    index = 1
    while index < len(argv):
        option = argv[index]
        if option in _SHELL_NON_EXECUTING_OPTIONS or (
            option.startswith("-") and not option.startswith("--") and "n" in option[1:]
        ):
            return None
        if option in {"-c", "-lc", "-cl"}:
            if index + 1 >= len(argv) or index + 2 != len(argv):
                return None
            return argv[index + 1].strip()
        if option in _SHELL_OPTIONS_WITH_ARGUMENT:
            if index + 1 >= len(argv):
                return None
            option_value = argv[index + 1].strip().lower().replace("_", "").replace("-", "")
            if option == "-o" and option_value == "noexec":
                return None
            index += 2
            continue
        if option.startswith("-o"):
            option_value = option[2:].strip().lower().replace("_", "").replace("-", "")
            if option_value == "noexec":
                return None
        if option == "--" or option == "-" or not option.startswith(("-", "+")):
            return None
        index += 1
    return None


def _test_invocation_from_shell_body(body: str) -> str | None:
    """Return a test invocation after conservative shell setup preambles."""
    segments = tuple(_segments_after_safe_shell_preamble_with_pipefail(body))
    if len(segments) != 1:
        return None
    segment, pipefail_enabled = segments[0]
    candidate = _strip_command_output_plumbing(segment)
    if (
        _has_trailing_output_filter_pipeline(segment)
        and not pipefail_enabled
        and _test_invocation_from_prefix(candidate) is not None
    ):
        candidate = segment
    return _test_invocation_from_prefix(candidate)


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


def _is_python_executable(value: str) -> bool:
    """Return True for standard versioned Python interpreter names."""
    executable = Path(value).name.lower()
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable))


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


def _has_unquoted_status_masking_control(command: str) -> bool:
    """Return whether shell control syntax can hide a test command's status."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
            continue
        if quote == '"':
            if char == '"':
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in {"|", ";", "\n", "\r"}:
            return True
        if char == "&":
            previous = command[index - 1] if index > 0 else ""
            following = command[index + 1] if index + 1 < len(command) else ""
            if previous == "&" or following == "&":
                return True
            if previous not in {">", "<"} and following != ">":
                return True
    return False


_UV_VALUE_OPTIONS = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--config-settings-package",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--exclude-newer-package",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--from",
        "--group",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-editable-package",
        "--no-extra",
        "--no-group",
        "--no-sources-package",
        "--only-group",
        "--package",
        "--prerelease",
        "--project",
        "--python",
        "--python-platform",
        "--preview-features",
        "--refresh-package",
        "--reinstall-package",
        "--resolution",
        "--upgrade-group",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
    }
)
_UV_FLAG_OPTIONS = frozenset(
    {
        "--active",
        "--all-extras",
        "--all-groups",
        "--all-packages",
        "--compile-bytecode",
        "--exact",
        "--frozen",
        "--isolated",
        "--locked",
        "--managed-python",
        "--native-tls",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-cache",
        "--no-config",
        "--no-default-groups",
        "--no-dev",
        "--no-editable",
        "--no-env-file",
        "--no-index",
        "--no-managed-python",
        "--no-progress",
        "--no-project",
        "--no-python-downloads",
        "--no-sources",
        "--no-sync",
        "--offline",
        "--only-dev",
        "--quiet",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "--verbose",
    }
)
_UV_SHORT_VALUE_OPTIONS = frozenset({"C", "f", "i", "p", "P", "w"})
_UV_SHORT_FLAG_OPTIONS = frozenset({"n", "q", "U", "v"})


def _uv_run_pytest_parts(parts: list[str]) -> list[str] | None:
    """Parse a ``uv run``/``uvx`` prefix and return pytest plus its arguments."""
    if len(parts) >= 3 and parts[:2] == ["uv", "run"]:
        index = 2
    elif len(parts) >= 2 and parts[0] == "uvx":
        index = 1
    else:
        return None
    while index < len(parts):
        token = parts[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            break
        if token in {"--help", "-h", "--version", "-V", "--script", "-s", "--gui-script"}:
            return None
        if token in {"--module", "-m"}:
            index += 1
            continue
        if token.startswith("--"):
            option, separator, attached = token.partition("=")
            if option in _UV_FLAG_OPTIONS:
                if separator:
                    return None
                index += 1
                continue
            if option not in _UV_VALUE_OPTIONS:
                return None
            index += 1
            if separator:
                if not attached:
                    return None
            else:
                if index >= len(parts) or parts[index] == "--" or parts[index].startswith("-"):
                    return None
                index += 1
            continue
        cluster = token[1:]
        position = 0
        while position < len(cluster):
            option = cluster[position]
            if option in {"h", "s"}:
                return None
            if option == "m" or option in _UV_SHORT_FLAG_OPTIONS:
                position += 1
                continue
            if option not in _UV_SHORT_VALUE_OPTIONS:
                return None
            if position + 1 == len(cluster):
                index += 1
                if index >= len(parts) or parts[index] == "--" or parts[index].startswith("-"):
                    return None
            position = len(cluster)
        index += 1
    if index >= len(parts):
        return None
    if parts[index] in {"pytest", "py.test"}:
        return parts[index:]
    if (
        len(parts) >= index + 3
        and _is_python_executable(parts[index])
        and parts[index + 1 : index + 3] == ["-m", "pytest"]
    ):
        return parts[index:]
    return None


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
    if _has_unquoted_status_masking_control(command):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.replace('"', "").replace("'", "").split()
    parts = _strip_env_prefix(parts)
    if not parts:
        return None
    if any(part in {"|", "||", ";", "&"} for part in parts):
        return None
    uv_pytest_parts = _uv_run_pytest_parts(parts)
    if uv_pytest_parts is not None:
        if _has_non_executing_test_mode(uv_pytest_parts):
            return None
        return _normalized_evidence_text(" ".join(parts))
    if _has_non_executing_test_mode(parts):
        return None

    if parts[0] in {"pytest", "py.test", "tox", "nox"}:
        return _normalized_evidence_text(" ".join(parts))
    if len(parts) >= 2 and parts[0] in {"npm", "pnpm", "yarn"} and parts[1] == "test":
        return _normalized_evidence_text(" ".join(parts))
    if (
        len(parts) >= 3
        and _is_python_executable(parts[0])
        and parts[1] == "-m"
        and parts[2] in {"pytest", "unittest"}
    ):
        return _normalized_evidence_text(" ".join(parts))
    if _is_django_test_subcommand(parts) or _project_test_runner_script(parts) is not None:
        return _normalized_evidence_text(" ".join(parts))
    executable = Path(parts[0]).name
    if (
        executable in {"gradle", "gradlew", "mvn", "mvnw"}
        and not _has_gradle_or_maven_test_skip(parts[1:])
        and any(part in {"test", "check", "verify"} or part.endswith(":test") for part in parts[1:])
    ):
        return _normalized_evidence_text(" ".join(parts))
    return None


def _is_django_test_subcommand(parts: Sequence[str]) -> bool:
    """Return True for Django's installed ``test`` management command.

    ``django-admin test`` and ``python -m django test``: the executable or
    module is Django itself and the subcommand, which Django reads from the
    first argument, is ``test``. Like ``pytest``, the program comes from the
    environment, so the executable must be the bare name.
    """
    if len(parts) >= 2 and parts[0] == "django-admin" and parts[1] == "test":
        return True
    return (
        len(parts) >= 4
        and _is_python_executable(parts[0])
        and parts[1:4] == ["-m", "django", "test"]
    )


def _project_test_runner_script(parts: Sequence[str]) -> str | None:
    """Return the script token when argv runs a project's own test-runner script.

    Recognized by the script's name (and subcommand), never by substring:

    - ``runtests.py`` (Django's ``tests/runtests.py``);
    - ``manage.py test`` (a Django project's test command);
    - ``bin/test`` and ``bin/doctest`` (SymPy); the last two path components
      must be exactly ``bin/test`` or ``bin/doctest`` and the path must be
      relative, because a bare or absolute ``test`` is the shell builtin or
      ``/usr/bin/test``.

    The script is either the first argument of a Python interpreter
    (``python tests/runtests.py``) or argv[0] given as a path
    (``./tests/runtests.py``); a bare argv[0] would be a PATH lookup, not the
    project's file. Re-execution additionally requires the script to be a
    regular file inside the workspace, as an inline program's imported module
    must be a workspace file to anchor anything.
    """
    if len(parts) >= 2 and _is_python_executable(parts[0]):
        index = 1
    elif parts and "/" in parts[0]:
        index = 0
    else:
        return None
    script = parts[index]
    if not script or script.startswith("-") or "\\" in script:
        return None
    path = PurePosixPath(script)
    if path.name == "runtests.py":
        return script
    if path.name == "manage.py":
        return script if len(parts) > index + 1 and parts[index + 1] == "test" else None
    if (
        path.parts[-2:] in {("bin", "test"), ("bin", "doctest")}
        and not path.is_absolute()
        and ".." not in path.parts
    ):
        return script
    return None


_GENERIC_NON_EXECUTING_TEST_OPTIONS = frozenset({"-h", "--help", "-V", "--version", "--dry-run"})
_PYTEST_NON_EXECUTING_OPTIONS = frozenset(
    {
        "--collect-only",
        "--collectonly",
        "--co",
        "--fixtures",
        "--fixtures-per-test",
        "--funcargs",
        "--markers",
        "--setup-only",
        "--setup-plan",
        "--setuponly",
        "--setupplan",
    }
)


def _has_non_executing_test_mode(parts: list[str]) -> bool:
    """Return whether a recognized test runner is configured not to run tests."""
    if any(part in _GENERIC_NON_EXECUTING_TEST_OPTIONS for part in parts[1:]):
        return True

    runner_parts = parts
    if len(parts) >= 3 and (
        parts[:3] == ["uv", "run", "pytest"]
        or (
            _is_python_executable(parts[0])
            and parts[1] == "-m"
            and parts[2] in {"pytest", "unittest"}
        )
    ):
        runner_parts = parts[2:]

    runner = Path(runner_parts[0]).name if runner_parts else ""
    options = runner_parts[1:]
    if runner in {"pytest", "py.test"} and any(
        option in _PYTEST_NON_EXECUTING_OPTIONS for option in options
    ):
        return True
    if runner == "tox" and any(
        option in {"-l", "--listenvs", "--showconfig"} for option in options
    ):
        return True
    if runner == "nox" and any(option in {"-l", "--list"} for option in options):
        return True
    return runner in {"gradle", "gradlew"} and "-m" in options


def _unittest_command_invocation(command: str) -> str | None:
    """Return the embedded ``python -m unittest`` invocation, if present."""
    invocation = _test_command_invocation(command)
    if invocation is None:
        return None
    parts = invocation.split()
    if len(parts) >= 3 and _is_python_executable(parts[0]) and parts[1:3] == ["-m", "unittest"]:
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


def _top_level_shell_character_positions(command: str, character: str) -> tuple[int, ...]:
    """Return unquoted occurrences outside shell grouping and substitutions."""
    positions: list[int] = []
    quote: str | None = None
    escaped = False
    nesting: list[str] = []
    closing = {"(": ")", "{": "}"}
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char in closing:
            nesting.append(closing[char])
            continue
        if nesting and char == nesting[-1]:
            nesting.pop()
            continue
        if not nesting and char == character:
            positions.append(index)
    return tuple(positions)


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
    while True:
        pipe_positions = tuple(
            position
            for position in _top_level_shell_character_positions(text, "|")
            if (position == 0 or text[position - 1] != "|")
            and (position + 1 >= len(text) or text[position + 1] != "|")
        )
        if not pipe_positions:
            break
        pipe_position = pipe_positions[-1]
        head = text[:pipe_position]
        tail_segment = text[pipe_position + 1 :]
        tail_tokens = tail_segment.split()
        if tail_tokens and tail_tokens[0].lower() in _OUTPUT_FILTER_COMMANDS:
            text = head.strip()
            continue
        break
    # Peel trailing output redirections, possibly several (``2>&1 > log``).
    prev: str | None = None
    while prev != text:
        prev = text
        match = _TRAILING_REDIRECT_RE.search(text)
        if match is None:
            break
        redirect_positions = _top_level_shell_character_positions(text, ">")
        if not any(match.start() <= position < match.end() for position in redirect_positions):
            break
        text = text[: match.start()].strip()
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
    """Return True when pipefail is enabled before the first stripped pipeline.

    A later ``set`` that names ``pipefail`` without enabling it
    (``set +o pipefail``) turns the protection off again.
    """
    pipefail_enabled = False
    for segment in re.split(r"\s*(?:&&|;)\s*", command.strip()):
        normalized_segment = segment.strip()
        if not normalized_segment:
            continue
        if _is_pipefail_preamble(normalized_segment):
            pipefail_enabled = True
            continue
        if _may_disable_pipefail(normalized_segment):
            pipefail_enabled = False
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


def _may_disable_pipefail(segment: str) -> bool:
    """Return True for a ``set`` naming ``pipefail`` that does not enable it
    (``set +o pipefail``, ``set +euo pipefail``, unparseable text)."""
    try:
        parts = shlex.split(segment)
    except ValueError:
        return "pipefail" in segment
    return bool(parts) and parts[0] == "set" and "pipefail" in parts


def _is_pipefail_preamble(segment: str) -> bool:
    try:
        parts = shlex.split(segment)
    except ValueError:
        return False
    return _is_pipefail_parts(parts)


# ``set`` options that may accompany ``pipefail`` in a recognized preamble:
# each only makes a failure more visible, none prints or runs anything.
_PIPEFAIL_COMPANION_OPTIONS = frozenset({"pipefail", "errexit", "nounset"})
_SET_OPTION_CLUSTER_RE = re.compile(r"-[eu]*o?")


def _is_pipefail_parts(parts: list[str]) -> bool:
    """Return True for a ``set`` that enables pipefail.

    ``set -o pipefail``, and the same with ``-e``/``-u`` or
    ``-o errexit``/``-o nounset`` beside it, clustered or not
    (``set -euo pipefail``, ``set -e -o pipefail``). Any other option is not
    recognized.
    """
    if len(parts) < 3 or parts[0] != "set":
        return False
    pipefail = False
    index = 1
    while index < len(parts):
        token = parts[index]
        if token == "-" or not _SET_OPTION_CLUSTER_RE.fullmatch(token):
            return False
        index += 1
        if token.endswith("o"):
            if index >= len(parts) or parts[index] not in _PIPEFAIL_COMPANION_OPTIONS:
                return False
            pipefail = pipefail or parts[index] == "pipefail"
            index += 1
    return pipefail


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
    # A leaf sometimes nests the wrapper -- ``/bin/zsh -lc "/bin/zsh -lc
    # '<body>'"`` (observed on codex when it re-issues a command it read from
    # a seed). Each layer is the same runtime-backed wrapper around the same
    # argv vector, so peel until the body is no longer a shell ``-c`` form.
    # Bounded so a pathological input cannot loop; each layer must parse as
    # an exact wrapper, so this never widens proof to arbitrary substrings.
    shell_body = _shell_command_body(command)
    for _ in range(4):
        normalized_shell_body = _normalized_evidence_text(shell_body) if shell_body else None
        append_alias(normalized_shell_body)
        append_alias(_normalized_shell_words_text(shell_body) if shell_body else None)
        inner_body = _shell_command_body(shell_body) if shell_body else None
        if not inner_body or inner_body == shell_body:
            break
        shell_body = inner_body
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


# ---------------------------------------------------------------------------
# Executed-command analysis. What a command line runs: its simple commands,
# the runtime shell wrappers around it, and, for each simple command, the
# wrappers and launchers in front of the program that finally runs. Replay
# authorization and the denylist (``replay_policy``), test
# target linkage and inline-import anchoring (``test_detection``) all consume
# this one analysis instead of reading the command text themselves.
# ---------------------------------------------------------------------------

_MAX_RESOLVE_DEPTH = 6
# Programs whose argv cannot be known from the transcript, or that run a
# command line through a shell of their own. Never replayed.
REFUSED_WRAPPERS = frozenset({"xargs", "watch", "script", "flock", "parallel"})

_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_NUMERIC_OPTION_RE = re.compile(r"-\d+")


@dataclass(frozen=True, slots=True)
class _OptionSpec:
    """Options a wrapper or launcher accepts before the program it runs."""

    values: frozenset[str] = frozenset()
    flags: frozenset[str] = frozenset()
    refused: frozenset[str] = frozenset()
    positionals: int = 0
    positional_re: re.Pattern[str] | None = None
    assignments: bool = False
    numeric_flags: bool = False


def _spec(
    values: set[str] | frozenset[str] = frozenset(),
    flags: set[str] | frozenset[str] = frozenset(),
    refused: set[str] | frozenset[str] = frozenset(),
    **options: object,
) -> _OptionSpec:
    return _OptionSpec(
        values=frozenset(values),
        flags=frozenset(flags),
        refused=frozenset(refused),
        **options,  # type: ignore[arg-type]
    )


# Wrappers: they run the rest of the argv. Options taking a separate value are
# listed so the value is never mistaken for the program.
_WRAPPERS: Mapping[str, _OptionSpec] = {
    "timeout": _spec(
        {"-s", "--signal", "-k", "--kill-after"},
        {"--preserve-status", "--foreground", "-v", "--verbose", "-f", "-p"},
        positionals=1,
        positional_re=_DURATION_RE,
    ),
    "stdbuf": _spec({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "time": _spec(
        {"-o", "--output", "-f", "--format"},
        {"-p", "-a", "--append", "-v", "--verbose", "-q", "--quiet", "-l", "--portability"},
    ),
    "nice": _spec({"-n", "--adjustment"}, numeric_flags=True),
    "ionice": _spec(
        {"-c", "--class", "-n", "--classdata"},
        {"-t", "--ignore"},
        {"-p", "--pid", "-P", "--pgid", "-u", "--uid"},
    ),
    "env": _spec(
        {"-u", "--unset"},
        {"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"},
        {"-C", "--chdir", "-S", "--split-string", "-P"},
        assignments=True,
    ),
    "nohup": _spec(),
    "command": _spec(flags={"-p"}, refused={"-v", "-V"}),
    "exec": _spec({"-a"}, {"-c", "-l"}),
    "setsid": _spec(flags={"-c", "--ctty", "-w", "--wait", "-f", "--fork"}),
}

_UV_RUN_SPEC = _spec(
    (_UV_VALUE_OPTIONS - {"--directory", "--project"})
    | {f"-{option}" for option in _UV_SHORT_VALUE_OPTIONS},
    _UV_FLAG_OPTIONS | {f"-{option}" for option in _UV_SHORT_FLAG_OPTIONS},
    {"--directory", "--project", "--script", "-s", "--gui-script", "-m", "--module"}
    | {"--env-file"},
)
# Launchers: they run another program by name. The launched program must pass
# the allowlist itself.
_LAUNCHERS: Mapping[tuple[str, ...], _OptionSpec] = {
    ("uv", "run"): _UV_RUN_SPEC,
    ("uvx",): _UV_RUN_SPEC,
    ("poetry", "run"): _spec(
        flags={"-q", "--quiet", "-v", "-vv", "-vvv", "--verbose", "-n", "--no-interaction"}
        | {"--ansi", "--no-ansi"},
        refused={"-C", "--directory", "-P", "--project"},
    ),
    ("pipenv", "run"): _spec(),
    ("pdm", "run"): _spec(
        flags={"-v", "-q", "--verbose", "--quiet"},
        refused={"-p", "--project", "-g", "--global"},
    ),
    ("bundle", "exec"): _spec(flags={"--keep-file-descriptors"}),
    ("npx",): _spec(
        {"-p", "--package"},
        {"-y", "--yes", "--no", "-q", "--quiet", "--no-install", "--ignore-existing"}
        | {"--prefer-offline", "--offline"},
        {"-c", "--call"},
    ),
    ("bunx",): _spec({"-p", "--package"}, {"--bun"}),
}

_PYTHON_FLAGS = frozenset(
    {"-u", "-B", "-O", "-OO", "-E", "-s", "-S", "-I", "-b", "-bb", "-q", "-P", "-d", "-R"}
)
_PYTHON_VALUE_OPTIONS = frozenset({"-W", "-X"})


def _program_name(value: str) -> str:
    name = PurePosixPath(value.replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _skip_options(parts: Sequence[str], index: int, spec: _OptionSpec) -> int | None:
    """Return the index of the program after ``parts[index:]``'s options, or None.

    None when an option is refused or unknown, a value is missing, or no
    program follows: the program cannot be identified with certainty.
    """
    remaining = spec.positionals
    while index < len(parts):
        token = parts[index]
        if token == "--":
            index += 1
            return index if remaining == 0 and index < len(parts) else None
        if spec.assignments and _is_env_assignment(token):
            index += 1
            continue
        if token.startswith("-") and token != "-":
            name, separator, _ = token.partition("=")
            if token.startswith("--"):
                if name in spec.refused:
                    return None
                if name in spec.values:
                    index += 1 if separator else 2
                elif name in spec.flags and not separator:
                    index += 1
                else:
                    return None
                continue
            short = token[:2]
            if short in spec.refused or token in spec.refused:
                return None
            if short in spec.values:
                index += 1 if len(token) > 2 else 2
            elif token in spec.flags or (
                spec.numeric_flags and _NUMERIC_OPTION_RE.fullmatch(token)
            ):
                index += 1
            else:
                return None
            continue
        if remaining:
            if spec.positional_re is not None and not spec.positional_re.fullmatch(token):
                return None
            remaining -= 1
            index += 1
            continue
        return index
    return None


def _walk(parts: tuple[str, ...]) -> tuple[tuple[tuple[str, ...], ...] | None, tuple[str, ...]]:
    """Walk ``parts`` through its wrappers and launchers.

    Returns ``(programs, assignments)``. ``programs`` is the argv of each
    launcher in the chain followed by the argv of the program that finally
    runs, or None when a wrapper or launcher option cannot be classified with
    certainty, a refused wrapper appears, or the chain is too deep.
    ``assignments`` are the ``NAME=value`` tokens consumed by ``env`` wrappers
    up to where the walk stopped. A path program (``./uv``, ``.venv/bin/uv``)
    is never a launcher: launchers are matched by bare name only.
    """
    programs: list[tuple[str, ...]] = []
    assignments: list[str] = []
    for _ in range(2 * _MAX_RESOLVE_DEPTH):
        if not parts:
            return None, tuple(assignments)
        name = _program_name(parts[0])
        if name in REFUSED_WRAPPERS:
            return None, tuple(assignments)
        spec = _WRAPPERS.get(name)
        if spec is not None:
            index = _skip_options(parts, 1, spec)
            if index is None:
                return None, tuple(assignments)
            if spec.assignments:
                assignments.extend(token for token in parts[1:index] if _is_env_assignment(token))
            parts = parts[index:]
            continue
        launcher = next(
            (
                (key, option_spec)
                for key, option_spec in _LAUNCHERS.items()
                if tuple(token.lower() for token in parts[: len(key)]) == key
            ),
            None,
        )
        if launcher is None:
            programs.append(parts)
            return tuple(programs), tuple(assignments)
        index = _skip_options(parts, len(launcher[0]), launcher[1])
        if index is None:
            return None, tuple(assignments)
        programs.append(parts)
        parts = parts[index:]
    return None, tuple(assignments)


def program_chain(argv: Sequence[str]) -> tuple[tuple[str, ...], ...] | None:
    """Return the argv of every program ``argv`` runs, or None when unknown.

    Wrappers (``timeout``, ``env``, ...) are peeled; each launcher (``uv run``,
    ``npx``, ``bundle exec``, ...) is kept with its own argv and followed to
    the program it launches, whose argv comes last. ``uv run .venv/bin/pip
    install x`` yields ``("uv", "run", ...)`` and ``(".venv/bin/pip",
    "install", "x")``. This is the one resolution the allowlist
    (``replay_policy.resolve_replay_program``), the denylist
    (``replay_policy.replay_denied``) and the command-line assignments
    (``command_line_assignments``) share.
    """
    programs, _ = _walk(tuple(argv))
    return programs


def command_line_assignments(argv: Sequence[str]) -> tuple[str, ...]:
    """Return the ``NAME=value`` tokens ``argv`` sets for the program it runs.

    Leading assignments, and those consumed by an ``env`` wrapper anywhere in
    the chain of wrappers and launchers (``timeout 60 env X=1 pytest``,
    ``uv run env X=1 pytest``), as ``program_chain`` walks it.
    """
    parts = tuple(argv)
    index = 0
    while index < len(parts) and _is_env_assignment(parts[index]):
        index += 1
    _, assignments = _walk(parts[index:])
    return (*parts[:index], *assignments)


def python_inline_program(argv: Sequence[str]) -> str | None:
    """Return the program text ``argv`` runs with ``python -c``, or None.

    The program is the last one ``program_chain`` resolves (``timeout 5 uv
    run python3 -c ...`` counts), and it must be a Python interpreter whose
    options, read with the interpreter option tables replay admission uses, end in ``-c``
    (alone, as the last letter of a flag cluster such as ``-Bc``, or with the
    program attached as in ``-cCODE``). A script, ``-m`` or an unknown option
    before any ``-c`` means no inline program.
    """
    programs = program_chain(argv)
    if not programs or not _is_python_executable(_program_name(programs[-1][0])):
        return None
    parts = programs[-1]
    index = 1
    while index < len(parts):
        token = parts[index]
        if token in _PYTHON_FLAGS:
            index += 1
            continue
        if token in _PYTHON_VALUE_OPTIONS:
            index += 2
            continue
        if not token.startswith("-") or token.startswith("--") or token == "-":
            return None
        cluster = token[1:]
        for position, letter in enumerate(cluster):
            if letter == "c":
                rest = cluster[position + 1 :]
                if rest:
                    return rest
                return parts[index + 1] if index + 1 < len(parts) else None
            if f"-{letter}" in _PYTHON_VALUE_OPTIONS:
                break
            if f"-{letter}" not in _PYTHON_FLAGS:
                return None
        index += 1
    return None


# Shell control operators that end one simple command in a compound line.
_SHELL_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&"})


def _simple_commands(command: str) -> list[list[str]]:
    """Split a shell line into the argv of each simple command, or [] if unparsable.

    Operators inside quotes stay in their token; anything that is not a
    separator but consists only of shell punctuation (redirections, subshell
    parentheses) ends the current command as well, so no argv spans it.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS or (token and set(token) <= set("();<>|&")):
            commands.append([])
        else:
            commands[-1].append(token)
    return [argv for argv in commands if argv]


_MAX_SHELL_WRAPPER_DEPTH = 4


def _peel_shell_wrappers(command: str) -> str:
    text = command.strip()
    for _ in range(_MAX_SHELL_WRAPPER_DEPTH):
        body = _shell_command_body(text)
        if body is None or body.strip() == text:
            break
        text = body.strip()
    return text
