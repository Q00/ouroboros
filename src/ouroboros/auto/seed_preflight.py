"""Deterministic executability preflight for a generated Seed.

An LLM Seed QA judge grades whether the contract *reads* well; it cannot
notice that a verify script does not exist on disk, that an environment
variable in a ``verify_command`` has no binding, or that a brownfield
context reference is a concept ("Obsidian Vault") rather than a real path.
Those are exactly the fabrications that survive prompt-level review and
kill the run later.

This module checks the Seed's own claims against the filesystem. It never
rewrites the Seed: every finding carries the open question a human (or the
interview ledger) must answer before the contract is executable.

Blocking findings are fabricated or unbound *claims*:

* a ``brownfield_context.context_references`` path that does not resolve;
* an ``existing_dependencies`` entry naming a workspace file that does not
  exist even though the Seed claims it pre-exists;
* an environment variable in a ``verify_command`` that nothing binds.

Advisory findings are open questions that do not prove fabrication: an AC
without any success contract, a verify-command file reference that is
neither claimed nor present yet, or two ACs sharing one verify command.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import sys

from ouroboros.core.seed import AcceptanceCriterionSpec, Seed

__all__ = [
    "PreflightFinding",
    "SeedPreflightReport",
    "run_seed_preflight",
]

# Variables `/bin/sh` establishes even when launched with an empty environment.
_SHELL_INITIALIZED_ENV_VARS = frozenset(
    {
        "IFS",
        "OPTIND",
        "PATH",
        "PPID",
        "PS1",
        "PS2",
        "PS4",
        "PWD",
        *({"SHELL"} if sys.platform == "darwin" else set()),
    }
)
_BASH_INITIALIZED_ENV_VARS = frozenset(
    {
        "BASH",
        "BASHPID",
        "BASHOPTS",
        "BASH_VERSION",
        "BASH_VERSINFO",
        "DIRSTACK",
        "EUID",
        "GROUPS",
        "HOSTNAME",
        "HOSTTYPE",
        "LINENO",
        "MACHTYPE",
        "OSTYPE",
        "PIPESTATUS",
        "RANDOM",
        "SECONDS",
        "SHELLOPTS",
        "SHLVL",
        "UID",
    }
)
_POSIX_SPECIAL_BUILTINS = frozenset(
    {
        ":",
        "break",
        "continue",
        "eval",
        "exec",
        "exit",
        "export",
        "readonly",
        "return",
        "set",
        "shift",
        "times",
        "trap",
        "unset",
    }
)


def _host_bound_env_vars() -> frozenset[str]:
    """Return the bindings inherited by the production shell executor."""
    return frozenset(os.environ) | _SHELL_INITIALIZED_ENV_VARS


_ENV_VAR_RE = re.compile(
    r"\$(?:\{#?(?P<braced>[A-Za-z_][A-Za-z0-9_]*)|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
# Variables the command binds itself: shell assignments (``tmp=$(mktemp)``),
# ``for x in ...`` loop variables, and parameter expansions that do not
# require a pre-existing value (defaults, assignments, and optional alternates).
_ENV_ASSIGN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=")
_FOR_VAR_RE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in(?P<items>[^;\n]*)[;\n]")
_OPTIONAL_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?[-=+]")
_ASSIGNING_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=")
_URL_RE = re.compile(r"\S+://\S+")
# A workspace-file token inside a command: at least one directory separator
# and a short file extension, e.g. ``scripts/verify.py`` or ``./bin/run.sh``.
_FILE_TOKEN_RE = re.compile(
    r"(?:(?:\.\.?/)+|/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5}\b"
    r"|(?:(?:\.\.?/)+|/)(?:[\w.-]+/)*[\w.-]+\b"
    r"|(?:\./)[\w.-]+\b"
    r"|(?<![\w./-])[\w-]+\.[A-Za-z0-9]{1,5}\b"
)


def _shell_variable_events(
    command: str, *, shell_dialect: str = "sh"
) -> tuple[tuple[int, str, str], ...]:
    """Return ordered shell assignment/expansion events outside literal quoting."""
    command = _strip_shell_comments(command)
    expandable = [True] * len(command)
    unquoted = [True] * len(command)
    quote: str | None = None
    escaped = False
    for index, character in enumerate(command):
        if escaped:
            expandable[index] = False
            escaped = False
            continue
        if character == "\\" and quote != "'":
            expandable[index] = False
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            expandable[index] = False
            unquoted[index] = False
            continue
        if quote == "'":
            expandable[index] = False
        if quote is not None:
            unquoted[index] = False

    segments: list[tuple[int, int, str | None, str | None]] = []
    segment_start = 0
    separator_before: str | None = None
    quote = None
    escaped = False
    substitution_depth = 0
    for index, character in enumerate(command):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if character in {"'", '"'} and substitution_depth == 0:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote != "'" and command[index : index + 2] == "$(":
            substitution_depth += 1
            continue
        if quote != "'" and character == ")" and substitution_depth:
            substitution_depth -= 1
            continue
        if quote is None and substitution_depth == 0 and character in ";&|\n":
            if index < segment_start:
                continue
            separator = character
            if character in "&|" and command[index : index + 2] == character * 2:
                separator = character * 2
            if index > segment_start and command[index - 1] == character:
                continue
            segments.append((segment_start, index, separator_before, separator))
            segment_start = index + len(separator)
            separator_before = separator
    segments.append((segment_start, len(command), separator_before, None))

    def shell_words(segment: str) -> tuple[str, ...]:
        words: list[str] = []
        word: list[str] = []
        local_quote: str | None = None
        local_escaped = False
        local_depth = 0
        index = 0
        while index < len(segment):
            character = segment[index]
            if local_escaped:
                word.append(character)
                local_escaped = False
                index += 1
                continue
            if character == "\\" and local_quote != "'":
                word.append(character)
                local_escaped = True
                index += 1
                continue
            if character in {"'", '"'} and local_depth == 0:
                if local_quote is None:
                    local_quote = character
                elif local_quote == character:
                    local_quote = None
                word.append(character)
                index += 1
                continue
            if local_quote != "'" and segment[index : index + 2] == "$(":
                local_depth += 1
                word.extend(("$", "("))
                index += 2
                continue
            if local_quote != "'" and character == ")" and local_depth:
                local_depth -= 1
                word.append(character)
                index += 1
                continue
            if character.isspace() and local_quote is None and local_depth == 0:
                if word:
                    words.append("".join(word))
                    word = []
            else:
                word.append(character)
            index += 1
        if word:
            words.append("".join(word))
        return tuple(words)

    assignment = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
    persistent_bindings: list[tuple[int, str]] = []
    persistent_unbindings: list[tuple[int, str]] = []
    subshell_separators = {"&", "|"}
    for start, end, separator_before, separator_after in segments:
        words = shell_words(command[start:end].strip())
        if not words:
            continue
        assignment_only = all(assignment.fullmatch(word) for word in words)
        command_index = next(
            (index for index, word in enumerate(words) if assignment.fullmatch(word) is None),
            len(words),
        )
        command_name = Path(words[command_index]).name if command_index < len(words) else ""
        assignment_builtin = command_name in {"export", "readonly"} or (
            shell_dialect == "bash" and command_name in {"declare", "local", "typeset"}
        )
        special_builtin = command_name in _POSIX_SPECIAL_BUILTINS
        persists = (
            separator_before not in subshell_separators | {"&&", "||"}
            and separator_after not in subshell_separators
        )
        if special_builtin and persists:
            # POSIX assignment prefixes on special builtins update the current
            # shell environment, including builtins whose operands are not
            # assignments themselves (for example ``FOO=bar :``).
            persistent_bindings.extend(
                (end, word.partition("=")[0])
                for word in words[:command_index]
                if assignment.fullmatch(word)
            )
        if assignment_only and persists:
            persistent_bindings.extend(
                (end, word.partition("=")[0]) for word in words if assignment.fullmatch(word)
            )
        elif assignment_builtin and persists:
            persistent_bindings.extend(
                (end, word.partition("=")[0])
                for word in (*words[:command_index], *words[command_index + 1 :])
                if assignment.fullmatch(word)
            )
        elif command_name == "read" and persists:
            for word in words[command_index + 1 :]:
                if word == "-r":
                    continue
                if word == "--":
                    continue
                if any(operator in word for operator in ("<", ">")):
                    break
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", word):
                    persistent_bindings.append((end, word))
                else:
                    # Unknown option/operand grammar is not authoritative.
                    break
        elif command_name == "getopts" and persists:
            # POSIX getopts assigns its second operand on every successful
            # parse.  Bind the target conservatively when the option-string
            # and variable operands are syntactically identifiable.
            operands = [word for word in words[command_index + 1 :] if word != "--"]
            if len(operands) >= 2 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", operands[1]):
                persistent_bindings.append((end, operands[1]))
        elif command_name == "unset" and persists:
            unset_variables = True
            for word in words[command_index + 1 :]:
                if word in {"--", "-v"}:
                    continue
                if word == "-f":
                    unset_variables = False
                    continue
                if word.startswith("-"):
                    break
                if unset_variables and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", word):
                    persistent_unbindings.append((end, word))

    events: list[tuple[int, str, str]] = []
    events.extend((position, "bind", name) for position, name in persistent_bindings)
    events.extend((position, "unbind", name) for position, name in persistent_unbindings)

    def in_command_substitution(position: int) -> bool:
        depth = 0
        backquote = False
        quote: str | None = None
        escaped = False
        index = 0
        while index < position:
            character = command[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if character == "\\" and quote != "'":
                escaped = True
                index += 1
                continue
            if character in {"'", '"'}:
                if quote is None:
                    quote = character
                elif quote == character:
                    quote = None
                index += 1
                continue
            if quote != "'" and command[index : index + 2] == "$(":
                depth += 1
                index += 2
                continue
            if quote != "'" and character == ")" and depth:
                depth -= 1
            elif quote != "'" and character == "`":
                backquote = not backquote
            index += 1
        return depth > 0 or backquote

    def in_subprocess_segment(position: int) -> bool:
        boundary = max(command.rfind(";", 0, position), command.rfind("\n", 0, position))
        subprocess_separator = max(command.rfind("|", 0, position), command.rfind("&", 0, position))
        return subprocess_separator > boundary

    for match in _FOR_VAR_RE.finditer(command):
        items = match.group("items").strip()
        if unquoted[match.start()] and items and not re.search(r"[$`]", items):
            events.append((match.start(), "bind", match.group(1)))
    for match in _ENV_VAR_RE.finditer(command):
        if not expandable[match.start()]:
            continue
        variable = match.group("braced") or match.group("bare")
        if _OPTIONAL_VAR_RE.match(command, match.start()) is not None:
            assigning = _ASSIGNING_VAR_RE.match(command, match.start())
            if assigning is not None and not (
                in_command_substitution(match.start()) or in_subprocess_segment(match.start())
            ):
                events.append((assigning.end(), "bind", variable))
            continue
        events.append((match.start(), "expand", variable))
    return tuple(sorted(events))


def _strip_shell_comments(command: str) -> str:
    """Mask unquoted POSIX comments while preserving source offsets."""
    output = list(command)
    quote: str | None = None
    escaped = False
    comment = False
    for index, character in enumerate(command):
        if comment:
            if character == "\n":
                comment = False
            else:
                output[index] = " "
            continue
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (index == 0 or command[index - 1].isspace()):
            output[index] = " "
            comment = True
    return "".join(output)


_UNSUPPORTED_SHELL_STATE_GRAMMAR = re.compile(
    r"\b(?:if|then|elif|else|fi|case|esac|while|until|function)\b"
    r"|(?:^|[;&|]\s*)\{(?:\s|$)"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)\s*\{",
    re.MULTILINE,
)

_UNSUPPORTED_BASH_STATE_MUTATION = re.compile(
    r"\bprintf\s+(?:(?:-[^\s]+|--[^\s]+)\s+)*-v(?:\s+|=)"
    r"|(?:^|[;&|]\s*)let(?:\s|$)"
    r"|\(\(",
    re.MULTILINE,
)

_DYNAMIC_SHELL_STATE_MUTATION = re.compile(
    r"(?:^|(?:&&|\|\||[;&|\n])\s*)"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:command(?:\s+(?:--|-p|-v|-V))*\s+)?eval(?:\s|$)",
    re.MULTILINE,
)


def _has_dynamic_shell_state_mutation(command: str) -> bool:
    """Return whether runtime text can mutate later shell state."""
    return _DYNAMIC_SHELL_STATE_MUTATION.search(_strip_shell_comments(command)) is not None


def _has_unsupported_shell_state_grammar(command: str, *, shell_dialect: str = "sh") -> bool:
    """Return whether state flow is too rich for authoritative blocking."""
    stripped = _strip_shell_comments(command)
    return (
        _UNSUPPORTED_SHELL_STATE_GRAMMAR.search(stripped) is not None
        or _DYNAMIC_SHELL_STATE_MUTATION.search(stripped) is not None
        or shell_dialect == "bash"
        and _UNSUPPORTED_BASH_STATE_MUTATION.search(stripped) is not None
    )


def _is_shell_assignment_position(command: str, start: int) -> bool:
    """Return whether ``NAME=`` starts a shell assignment word, not an argument."""
    segment_start = max(command.rfind(token, 0, start) for token in (";", "&", "|", "\n")) + 1
    prefix = command[segment_start:start].strip()
    if not prefix:
        return True
    try:
        words = shlex.split(prefix, posix=True)
    except ValueError:
        return False
    assignment = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
    if all(assignment.fullmatch(word) for word in words):
        return True
    if words and Path(words[0]).name == "env":
        return all(word.startswith("-") or assignment.fullmatch(word) for word in words[1:])
    return False


def _is_subprocess_separator(separator: str | None) -> bool:
    """Return whether a separator isolates assignments in another process."""
    return separator == "&" or (separator is not None and "|" in separator and separator != "||")


def _shell_command_payload(segment: list[str]) -> tuple[str, str] | None:
    """Return an option-bearing ``sh``/``bash`` command-string invocation."""
    if not segment or Path(segment[0]).name not in {"sh", "bash"}:
        return None
    dialect = Path(segment[0]).name
    index = 1
    while index < len(segment):
        token = segment[index]
        if token in {"-c", "--command"}:
            return (segment[index + 1], dialect) if index + 1 < len(segment) else None
        if token.startswith("--command="):
            return token.partition("=")[2], dialect
        if token in {"-o", "-O", "--init-file", "--rcfile"}:
            index += 2
            continue
        if token.startswith("-") and not token.startswith("--"):
            flags = token[1:]
            if "c" in flags:
                return (segment[index + 1], dialect) if index + 1 < len(segment) else None
            index += 1
            continue
        if token.startswith("--"):
            index += 1
            continue
        return None
    return None


def _nested_shell_scopes(
    command: str,
    inherited: frozenset[str] | None = None,
    *,
    _nested_payload: bool = False,
    _shell_dialect: str = "sh",
) -> tuple[tuple[str, frozenset[str], str], ...]:
    """Return nested ``sh -c`` payloads with launcher-exported bindings."""
    if inherited is None:
        inherited = _host_bound_env_vars()
    if not _nested_payload:
        command = _mask_outer_double_quote_expansions(command)
    command = _mask_statically_unreachable_program_clauses(command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        # A newline is a sequential command separator in POSIX shells. Keep
        # it out of whitespace so the state interpreter cannot merge an
        # assignment-only command into the next nested-shell launcher.
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        parts = list(lexer)
    except ValueError:
        return ()
    segments: list[tuple[list[str], str | None, str | None]] = [([], None, None)]
    for part in parts:
        if re.fullmatch(r"[;&|\n]+", part):
            current, before, _after = segments[-1]
            segments[-1] = (current, before, part)
            segments.append(([], part, None))
        else:
            segments[-1][0].append(part)
    nested: list[tuple[str, frozenset[str], str]] = []
    assignment = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
    sequential_export_names = set(inherited)
    sequential_values = set(inherited)
    allexport = False

    def unwrap(segment: list[str], bound: set[str]) -> list[str]:
        remaining = list(segment)
        while remaining:
            command_name = Path(remaining[0]).name
            if command_name == "env":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-S", "--split-string"} and len(remaining) >= 2:
                        try:
                            remaining = shlex.split(remaining[1], posix=True) + remaining[2:]
                            continue
                        except ValueError:
                            return []
                    if token.startswith("--split-string="):
                        try:
                            remaining = (
                                shlex.split(token.partition("=")[2], posix=True) + remaining[1:]
                            )
                            continue
                        except ValueError:
                            return []
                    if token in {"-i", "--ignore-environment"}:
                        bound.clear()
                        remaining = remaining[1:]
                        continue
                    if token in {"-u", "--unset"}:
                        if len(remaining) >= 2:
                            bound.discard(remaining[1])
                        remaining = remaining[2:]
                        continue
                    if token.startswith("--unset="):
                        bound.discard(token.partition("=")[2])
                        remaining = remaining[1:]
                        continue
                    if token in {"-C", "--chdir"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-") or assignment.fullmatch(token):
                        if assignment.fullmatch(token):
                            bound.add(token.partition("=")[0])
                        remaining = remaining[1:]
                        continue
                    break
                continue
            if command_name == "timeout":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-s", "--signal", "-k", "--kill-after"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-"):
                        remaining = remaining[1:]
                        continue
                    break
                if remaining:
                    remaining = remaining[1:]
                continue
            if command_name == "nice":
                remaining = remaining[1:]
                if remaining and remaining[0] in {"-n", "--adjustment"}:
                    remaining = remaining[2:]
                elif remaining and (
                    re.fullmatch(r"-n?\d+", remaining[0])
                    or remaining[0].startswith("--adjustment=")
                ):
                    remaining = remaining[1:]
                if remaining and remaining[0] == "--":
                    remaining = remaining[1:]
                continue
            if command_name == "nohup":
                remaining = remaining[1:]
                if remaining and remaining[0] == "--":
                    remaining = remaining[1:]
                continue
            if command_name == "setsid":
                remaining = remaining[1:]
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    remaining = remaining[1:]
                continue
            if command_name == "stdbuf":
                remaining = remaining[1:]
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    if token in {"-i", "-o", "-e", "--input", "--output", "--error"}:
                        remaining = remaining[2:] if len(remaining) >= 2 else []
                    elif token.startswith(("--input=", "--output=", "--error=")):
                        remaining = remaining[1:]
                    else:
                        remaining = remaining[1:]
                continue
            if command_name == "time":
                remaining = remaining[1:]
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    if token in {"-f", "--format", "-o", "--output"}:
                        remaining = remaining[2:] if len(remaining) >= 2 else []
                    else:
                        remaining = remaining[1:]
                continue
            if command_name == "command":
                remaining = remaining[1:]
                while remaining and remaining[0] in {"--", "-p", "-v", "-V"}:
                    remaining = remaining[1:]
                continue
            break
        return remaining

    for segment, separator_before, separator_after in segments:
        persists = not _is_subprocess_separator(separator_before) and not _is_subprocess_separator(
            separator_after
        )
        state_segment = list(segment)
        assignment_prefixes: list[str] = []
        while state_segment and assignment.fullmatch(state_segment[0]):
            assignment_prefixes.append(state_segment.pop(0))
        if persists and state_segment and Path(state_segment[0]).name in _POSIX_SPECIAL_BUILTINS:
            prefix_names = {token.partition("=")[0] for token in assignment_prefixes}
            sequential_values.update(prefix_names)
            if allexport:
                sequential_export_names.update(prefix_names)
        if persists and segment and all(assignment.fullmatch(token) for token in segment):
            sequential_values.update(token.partition("=")[0] for token in segment)
            if allexport:
                sequential_export_names.update(token.partition("=")[0] for token in segment)
        if persists and state_segment and Path(state_segment[0]).name == "set":
            set_args = state_segment[1:]
            if "-a" in set_args or "-o" in set_args and "allexport" in set_args:
                allexport = True
            elif "+a" in set_args or "+o" in set_args and "allexport" in set_args:
                # Disabling allexport does not un-export variables already
                # marked for export; only subsequent assignments stop being
                # exported.
                allexport = False
        if persists and state_segment and Path(state_segment[0]).name == "export":
            # Assignment prefixes on POSIX special builtins affect the current
            # shell. Process them before ``export`` so a later nested shell
            # inherits the newly bound and exported name.
            sequential_values.update(token.partition("=")[0] for token in assignment_prefixes)
            export_names = True
            for token in state_segment[1:]:
                if _shell_dialect == "bash" and token in {"-n", "--no-export"}:
                    export_names = False
                    continue
                if token == "--":
                    continue
                if assignment.fullmatch(token):
                    name = token.partition("=")[0]
                    sequential_values.add(name)
                    if export_names:
                        sequential_export_names.add(name)
                    else:
                        sequential_export_names.discard(name)
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
                    if export_names:
                        sequential_export_names.add(token)
                    else:
                        sequential_export_names.discard(token)
        if persists and state_segment:
            command_name = Path(state_segment[0]).name
            builtin_bound_names: set[str] = set()
            if command_name == "readonly":
                builtin_bound_names.update(
                    token.partition("=")[0]
                    for token in state_segment[1:]
                    if assignment.fullmatch(token)
                )
            elif command_name == "read":
                builtin_bound_names.update(
                    token
                    for token in state_segment[1:]
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token)
                )
            elif command_name == "getopts" and len(state_segment) >= 3:
                target = state_segment[2]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target):
                    builtin_bound_names.add(target)
            sequential_values.update(builtin_bound_names)
            if allexport:
                sequential_export_names.update(builtin_bound_names)
        if (
            persists
            and _shell_dialect == "bash"
            and state_segment
            and Path(state_segment[0]).name in {"declare", "typeset"}
            and "+x" in state_segment[1:]
        ):
            for token in state_segment[state_segment.index("+x") + 1 :]:
                if assignment.fullmatch(token):
                    name = token.partition("=")[0]
                    sequential_values.add(name)
                    sequential_export_names.discard(name)
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
                    sequential_export_names.discard(token)
                elif token.startswith("-") or token.startswith("+"):
                    continue
                else:
                    break
        if persists and state_segment and Path(state_segment[0]).name == "unset":
            unset_variables = True
            for token in state_segment[1:]:
                if token in {"--", "-v"}:
                    continue
                if token == "-f":
                    unset_variables = False
                    continue
                if unset_variables and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
                    sequential_values.discard(token)
                    sequential_export_names.discard(token)
        launcher_bound = sequential_export_names & sequential_values
        launcher_bound.update(token.partition("=")[0] for token in assignment_prefixes)
        segment = state_segment
        segment = unwrap(segment, launcher_bound)
        shell_payload = _shell_command_payload(segment)
        if shell_payload is not None:
            payload, dialect = shell_payload
            scope = frozenset(launcher_bound | _SHELL_INITIALIZED_ENV_VARS)
            nested.append((payload, scope, dialect))
            nested.extend(
                _nested_shell_scopes(
                    payload,
                    scope,
                    _nested_payload=True,
                    _shell_dialect=dialect,
                )
            )
    return tuple(nested)


def _mask_outer_double_quote_expansions(command: str) -> str:
    """Keep nested payload ownership faithful to the outer shell.

    In ``sh -c "echo $FOO"`` the outer shell expands ``$FOO`` before launching
    the child; an escaped ``\\$FOO`` survives for the child instead. The token
    parser cannot retain that distinction after removing the launcher's quotes,
    so replace only these outer-owned expansions before tokenization.
    """
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            output.append(character)
            index += 1
            continue
        if quote == '"' and character == "\\" and index + 1 < len(command):
            if command[index + 1] == "$":
                output.append("$")
                index += 2
                continue
            output.extend((character, command[index + 1]))
            index += 2
            continue
        if quote == '"' and character == "$":
            match = re.match(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", command[index:])
            if match is not None:
                output.append("__outer_expansion_")
                index += len(match.group(0))
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _outer_shell_scope(command: str) -> str:
    """Mask nested-shell payloads while retaining their outer launcher scope."""
    masked = list(command)
    shell_entry = re.compile(r"(?<![\w./-])(?:sh|bash)\s+(?:-c|--command)\s+")
    for match in shell_entry.finditer(command):
        start = match.end()
        if start >= len(command):
            continue
        quote = command[start] if command[start] in {"'", '"'} else None
        cursor = start + 1 if quote is not None else start
        escaped = False
        while cursor < len(command):
            character = command[cursor]
            if escaped:
                escaped = False
            elif character == "\\" and quote != "'":
                escaped = True
            elif (quote is not None and character == quote) or (
                quote is None and (character.isspace() or character in ";&|\n")
            ):
                break
            masked[cursor] = " "
            cursor += 1
    return "".join(masked)


def _mask_statically_unreachable_program_clauses(command: str) -> str:
    """Mask program operands behind literal-false shell control edges."""
    output = list(command)
    patterns = (
        re.compile(
            r"(?:^|[;\n]\s*)if\s+(?:false|!\s*true)\s*;?\s*then\s*(?P<body>.*?)"
            r"(?=\b(?:else|elif|fi)\b)",
            re.DOTALL,
        ),
        re.compile(
            r"(?:^|[;\n]\s*)for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s*;\s*do\s*"
            r"(?P<body>.*?)(?=\bdone\b)",
            re.DOTALL,
        ),
        re.compile(
            r"(?:^|[;\n]\s*)(?:while\s+false|until\s+true)\s*;?\s*do\s*"
            r"(?P<body>.*?)(?=\bdone\b)",
            re.DOTALL,
        ),
        re.compile(
            r"(?:^|[;\n]\s*)false\s*&&\s*(?P<body>.*?)(?=\|\||[;\n]|$)",
            re.DOTALL,
        ),
        re.compile(
            r"(?:^|[;\n]\s*)true\s*\|\|\s*(?P<body>.*?)(?=&&|[;\n]|$)",
            re.DOTALL,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(command):
            start, end = match.span("body")
            output[start:end] = " " * (end - start)
    return "".join(output)


def _program_flow_inconclusive(command: str, token_start: int, token_end: int) -> bool:
    """Return whether one program occurrence depends on runtime shell state."""
    stripped = _strip_shell_comments(command)
    prefix = stripped[:token_start]
    if_matches = tuple(re.finditer(r"\bif\b", prefix))
    if len(if_matches) > sum(1 for _ in re.finditer(r"\bfi\b", prefix)):
        active_scope = prefix[if_matches[-1].start() :]
        static_condition = re.match(
            r"if\s+(?P<negated>!\s*)?(?P<value>true|false)\s*;?\s*then\b",
            active_scope,
        )
        if static_condition is None:
            return True
        condition_true = (static_condition.group("value") == "true") != bool(
            static_condition.group("negated")
        )
        in_else_branch = bool(re.search(r"\belse\b", active_scope))
        if condition_true == in_else_branch:
            return True
    if sum(1 for _ in re.finditer(r"\b(?:while|until|for)\b", prefix)) > sum(
        1 for _ in re.finditer(r"\bdone\b", prefix)
    ):
        return True
    if sum(1 for _ in re.finditer(r"\bcase\b", prefix)) > sum(
        1 for _ in re.finditer(r"\besac\b", prefix)
    ):
        return True
    segment_start = max(stripped.rfind(separator, 0, token_start) for separator in (";", "\n")) + 1
    segment = stripped[segment_start:token_end]
    if "||" in segment:
        return True
    return "&&" in segment and bool(re.search(r"[;\n]", stripped[token_end:]))


# A standalone dependency entry that claims a workspace file. Requires a
# directory separator so plain product names ("next.js", "Obsidian Vault")
# are never treated as file claims.
_FILE_CLAIM_RE = re.compile(r"\.?/?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5}")


@dataclass(frozen=True)
class PreflightFinding:
    """One deterministic executability defect found in a Seed."""

    code: str
    blocking: bool
    subject: str
    detail: str
    question: str


@dataclass(frozen=True)
class SeedPreflightReport:
    """Outcome of :func:`run_seed_preflight`."""

    findings: tuple[PreflightFinding, ...]

    @property
    def blocking_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def passed(self) -> bool:
        return not self.blocking_findings

    @property
    def open_questions(self) -> tuple[str, ...]:
        """Every finding's question, blocking first, deduplicated."""
        ordered = (
            *self.blocking_findings,
            *(finding for finding in self.findings if not finding.blocking),
        )
        return tuple(dict.fromkeys(finding.question for finding in ordered))


def run_seed_preflight(seed: Seed, *, workspace_root: Path | None = None) -> SeedPreflightReport:
    """Check the Seed's executability claims against the filesystem.

    ``workspace_root`` anchors relative paths (the run workspace). When it is
    ``None`` a relative path's existence cannot be judged, so those checks
    are skipped rather than guessed.
    """
    findings: list[PreflightFinding] = []
    artifacts = _declared_artifacts(seed)
    claimed_files = _claimed_dependency_files(seed)

    findings.extend(_check_context_references(seed, workspace_root))
    findings.extend(_check_claimed_dependencies(claimed_files, workspace_root, artifacts))
    findings.extend(_check_verify_commands(seed, workspace_root, artifacts, claimed_files))
    findings.extend(_check_unverifiable_criteria(seed))
    findings.extend(_check_shared_verify_commands(seed))
    return SeedPreflightReport(findings=tuple(findings))


def _specs(seed: Seed) -> tuple[AcceptanceCriterionSpec, ...]:
    return tuple(
        criterion
        for criterion in seed.acceptance_criteria
        if isinstance(criterion, AcceptanceCriterionSpec)
    )


def _normalize_workspace_path(text: str) -> str:
    return text.strip().removeprefix("./")


def _declared_artifacts(seed: Seed) -> frozenset[str]:
    return frozenset(
        _normalize_workspace_path(artifact)
        for criterion in _specs(seed)
        for artifact in criterion.expected_artifacts
    )


def _claimed_dependency_files(seed: Seed) -> frozenset[str]:
    claims: set[str] = set()
    for entry in seed.brownfield_context.existing_dependencies:
        stripped = entry.strip()
        if " " in stripped or "://" in stripped:
            continue
        if _FILE_CLAIM_RE.fullmatch(stripped):
            claims.add(_normalize_workspace_path(stripped))
    return frozenset(claims)


def _path_exists(path_text: str, workspace_root: Path | None) -> bool | None:
    """Return existence when decidable; ``None`` for relative paths without a root."""
    candidate = Path(path_text).expanduser()
    if candidate.is_absolute():
        return candidate.exists()
    if workspace_root is None:
        return None
    return (workspace_root / candidate).exists()


_STATIC_SHELL_WORD = r"""(?:[^\s;&|\\'\"]+|\\.|'[^']*'|\"(?:\\.|[^\"])*\")+"""
_CD_COMMAND_PREFIX = (
    rf"(?:[A-Za-z_][A-Za-z0-9_]*=(?:{_STATIC_SHELL_WORD})?\s+)*"
    r"(?:command(?:\s+(?:-p|--))*\s+)?"
)
_DETERMINISTIC_CWD_RE = re.compile(
    rf"(?:^|(?:&&|\|\||[;\n])\s*|then\s+|\{{\s*){_CD_COMMAND_PREFIX}"
    rf"cd(?P<cd_home>\s*)(?=(?:&&|\|\||[;&|\n]|$))"
    rf"|(?:^|(?:&&|\|\||[;\n])\s*|then\s+|\{{\s*){_CD_COMMAND_PREFIX}cd\s+"
    rf"(?:(?:-L|-P|--)\s+)*(?P<cd>{_STATIC_SHELL_WORD})"
    rf"|\benv\s+(?:[^;&|]*?\s)?(?:-C|--chdir)=(?P<env_eq>{_STATIC_SHELL_WORD})"
    rf"|\benv\s+(?:[^;&|]*?\s)?(?:-C|--chdir)\s+(?P<env>{_STATIC_SHELL_WORD})"
)
_NESTED_SHELL_PAYLOAD_RE = re.compile(
    r"\b(?:sh|bash)"
    r"(?:\s+(?!(?:-c|--command)(?:\s|=|$)|-[A-Za-z]*c[A-Za-z]*(?:\s|$))\S+)*"
    r"\s+(?:--command|-c|-[A-Za-z]*c[A-Za-z]*)\s+(?P<quote>['\"])"
    r"(?P<payload>.*?)"
    r"(?P=quote)",
    re.DOTALL,
)


def _decode_static_shell_word(raw: str) -> str | None:
    """Decode one shell word when it has no runtime-dependent expansion."""
    quote: str | None = None
    escaped = False
    for character in raw:
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote != "'" and character in {"$", "`"}:
            return None
    if escaped or quote is not None:
        return None
    try:
        decoded = shlex.split(raw, posix=True)
    except ValueError:
        return None
    return decoded[0] if len(decoded) == 1 else None


def _effective_program_root(
    command: str,
    workspace_root: Path,
    token_position: int,
) -> Path | None:
    """Return the deterministic cwd at one command position."""
    effective_root = workspace_root
    preceding = [
        match for match in _DETERMINISTIC_CWD_RE.finditer(command) if match.start() < token_position
    ]
    for match in preceding:
        raw_directory = match.group("cd")
        if match.group("cd_home") is not None:
            directory = str(Path.home())
        elif raw_directory is None:
            continue
        else:
            directory = _decode_static_shell_word(raw_directory)
            if directory is None:
                return None
        # A plain/conditional ``cd`` does not establish the verifier's cwd:
        # it may fail or be skipped while a later command after ``;`` still
        # runs.  It is authoritative only while the verifier remains guarded
        # by the same successful ``&&`` chain.
        matched_prefix = match.group(0).lstrip()
        suffix = command[match.end() : token_position]
        cwd_guard_scope = re.split(r"[;\n]", suffix, maxsplit=1)[0]
        conditional_failure_exits = (
            re.fullmatch(r"\s*\|\|\s*(?:exit|return)(?:\s+\d+)?\s*", cwd_guard_scope) is not None
        )
        if matched_prefix.startswith("||") or re.search(
            r"(?<!&)&(?!&)|(?<!\|)\|(?!\|)", cwd_guard_scope
        ):
            return None
        if "||" in cwd_guard_scope and not conditional_failure_exits:
            return None
        if matched_prefix.startswith("&&") and re.search(r"[;\n]", suffix):
            return None
        if matched_prefix.startswith("then"):
            condition = command[: match.start()]
            if re.search(r"\bif\s+(?:true|:)(?:\s*;)?\s*$", condition) is None:
                return None
        changed = Path(directory).expanduser()
        changed_root = changed if changed.is_absolute() else effective_root / changed
        if re.search(r"[;\n]", suffix) and not changed_root.is_dir():
            return None
        effective_root = changed_root

    segment_start = max(
        (command.rfind(separator, 0, token_position) for separator in (";", "\n", "&", "|")),
        default=-1,
    )
    local_env = next(
        (
            match
            for match in reversed(preceding)
            if (match.group("env") is not None or match.group("env_eq") is not None)
            and match.start() > segment_start
        ),
        None,
    )
    if local_env is not None:
        raw_directory = local_env.group("env") or local_env.group("env_eq")
        if raw_directory is None:
            return None
        directory = _decode_static_shell_word(raw_directory)
        if directory is None:
            return None
        changed = Path(directory).expanduser()
        effective_root = changed if changed.is_absolute() else effective_root / changed
    return effective_root


def _program_path_exists(
    path_text: str,
    command: str,
    workspace_root: Path | None,
    *,
    token_position: int,
    artifacts: frozenset[str] = frozenset(),
    claimed_files: frozenset[str] = frozenset(),
    _claim_root: Path | None = None,
) -> bool | None:
    """Resolve verifier programs after deterministic shell directory changes.

    Dynamic directory changes are intentionally inconclusive: blocking on the
    launcher's root in that case would reject a command that the real shell can
    execute from its effective working directory.
    """
    candidate = Path(path_text).expanduser()
    if workspace_root is None or candidate.is_absolute():
        return _path_exists(path_text, workspace_root)
    claim_root = _claim_root or workspace_root
    for nested in _NESTED_SHELL_PAYLOAD_RE.finditer(command):
        payload_start, payload_end = nested.span("payload")
        if payload_start <= token_position < payload_end:
            outer_root = _effective_program_root(command, workspace_root, nested.start())
            if outer_root is None:
                return None
            payload = nested.group("payload")
            return _program_path_exists(
                path_text,
                payload,
                outer_root,
                token_position=token_position - payload_start,
                artifacts=artifacts,
                claimed_files=claimed_files,
                _claim_root=claim_root,
            )
    effective_root = _effective_program_root(command, workspace_root, token_position)
    if effective_root is None:
        return None
    effective_path = effective_root / candidate
    try:
        effective_claim = _normalize_workspace_path(str(effective_path.relative_to(claim_root)))
    except ValueError:
        effective_claim = ""
    if effective_claim in artifacts or effective_claim in claimed_files:
        return True
    return effective_path.exists()


def _check_context_references(seed: Seed, workspace_root: Path | None) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for reference in seed.brownfield_context.context_references:
        if _path_exists(reference.path, workspace_root) is False:
            findings.append(
                PreflightFinding(
                    code="context_reference_unresolved",
                    blocking=True,
                    subject=reference.path,
                    detail=(
                        f"brownfield context reference {reference.path!r} "
                        f"(role {reference.role!r}) does not resolve to an existing path"
                    ),
                    question=(
                        "What is the real, absolute path for the referenced context "
                        f"{reference.path!r}?"
                    ),
                )
            )
    return findings


def _check_claimed_dependencies(
    claimed_files: frozenset[str],
    workspace_root: Path | None,
    artifacts: frozenset[str],
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for claim in sorted(claimed_files):
        if claim in artifacts:
            continue
        if _path_exists(claim, workspace_root) is False:
            findings.append(
                PreflightFinding(
                    code="claimed_dependency_missing",
                    blocking=True,
                    subject=claim,
                    detail=(
                        f"existing_dependencies claims {claim!r} pre-exists, "
                        "but it does not exist in the workspace"
                    ),
                    question=(
                        f"{claim!r} is claimed as an existing dependency but is absent — "
                        "does it exist elsewhere, or must it be created first?"
                    ),
                )
            )
    return findings


def _check_verify_commands(
    seed: Seed,
    workspace_root: Path | None,
    artifacts: frozenset[str],
    claimed_files: frozenset[str],
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    seen_vars: set[str] = set()
    seen_tokens: set[str] = set()
    reported_missing_tokens: set[tuple[str, bool]] = set()
    for criterion in _specs(seed):
        command = criterion.verify_command
        if not command:
            continue
        scannable = _URL_RE.sub(" ", command)
        sourced_programs = _sourced_program_tokens(scannable)
        if sourced_programs:
            findings.append(
                PreflightFinding(
                    code="verify_shell_state_inconclusive",
                    blocking=False,
                    subject=", ".join(sorted(sourced_programs)),
                    detail=(
                        "verify_command sources code into the current shell, so its later "
                        "environment and working-directory state cannot be proven statically"
                    ),
                    question=(
                        "Can the sourced setup be folded into a deterministic verifier script "
                        "or described as an explicit Seed dependency?"
                    ),
                )
            )
        host_bound = _host_bound_env_vars()
        nested_shell_scopes = _nested_shell_scopes(scannable, host_bound)
        outer_shell_command = _outer_shell_scope(scannable)
        shell_scopes = ((outer_shell_command, host_bound, "sh"), *nested_shell_scopes)
        # ``eval`` can establish variables, directories, functions, aliases,
        # and more from runtime text.  Until that payload is modeled exactly,
        # neither environment nor verifier-path findings are authoritative.
        if any(_has_dynamic_shell_state_mutation(scope[0]) for scope in shell_scopes):
            continue
        for scope_index, (shell_command, inherited_bindings, shell_dialect) in enumerate(
            shell_scopes
        ):
            if scope_index == 0:
                # A variable in a double-quoted ``sh -c`` payload is expanded
                # by this outer shell before the child starts. Keep that raw
                # event visible; the nested scope separately checks only what
                # survives into the child.
                shell_command = scannable
            if _has_unsupported_shell_state_grammar(shell_command, shell_dialect=shell_dialect):
                continue
            # Dot/source executes arbitrary code in the current shell. Its
            # environment and cwd effects make later static variable/path
            # conclusions non-authoritative; the sourced file itself remains
            # a decidable program dependency below.
            if _sourced_program_tokens(shell_command):
                continue
            command_bound: set[str] = set(inherited_bindings) | set(_SHELL_INITIALIZED_ENV_VARS)
            if shell_dialect == "bash":
                command_bound.update(_BASH_INITIALIZED_ENV_VARS)
            for position, event, variable in _shell_variable_events(
                shell_command, shell_dialect=shell_dialect
            ):
                if event == "bind":
                    command_bound.add(variable)
                    continue
                if event == "unbind":
                    command_bound.discard(variable)
                    continue
                if variable == "OLDPWD" and re.search(
                    r"(?:^|[;&|\n]\s*)cd(?:\s|$)", shell_command[:position]
                ):
                    command_bound.add(variable)
                if variable in command_bound or variable in seen_vars:
                    continue
                seen_vars.add(variable)
                findings.append(
                    PreflightFinding(
                        code="unbound_env_var",
                        blocking=True,
                        subject=f"${variable}",
                        detail=(
                            f"verify_command for {criterion.description[:80]!r} references "
                            f"${variable}, but nothing in the Seed binds it to a value"
                        ),
                        question=(
                            f"What concrete value must ${variable} hold when the verify command runs?"
                        ),
                    )
                )
        program_scannable = _mask_statically_unreachable_program_clauses(scannable)
        program_spans: list[tuple[int, int]] = []
        occurrences: list[tuple[str, int, int, bool]] = []
        for word_match in re.finditer(_STATIC_SHELL_WORD, program_scannable):
            token = _decode_static_shell_word(word_match.group(0))
            if token is None:
                continue
            normalized = _normalize_workspace_path(token)
            segment_start = (
                max(
                    program_scannable.rfind(separator, 0, word_match.start())
                    for separator in (";", "\n", "&", "|")
                )
                + 1
            )
            occurrence_command = program_scannable[segment_start : word_match.end()]
            if _FILE_TOKEN_RE.search(token) is None or normalized not in _command_program_tokens(
                occurrence_command
            ):
                continue
            program_spans.append(word_match.span())
            occurrences.append((token, word_match.start(), word_match.end(), True))
        for token_match in _FILE_TOKEN_RE.finditer(program_scannable):
            if any(
                start <= token_match.start() and token_match.end() <= end
                for start, end in program_spans
            ):
                continue
            occurrences.append(
                (token_match.group(0), token_match.start(), token_match.end(), False)
            )
        occurrences.sort(key=lambda item: item[1])
        for token, token_start, token_end, known_program in occurrences:
            normalized = _normalize_workspace_path(token)
            segment_start = (
                max(
                    scannable.rfind(separator, 0, token_start)
                    for separator in (";", "\n", "&", "|")
                )
                + 1
            )
            occurrence_command = scannable[segment_start:token_end]
            for quote in ("'", '"'):
                if occurrence_command.count(quote) % 2:
                    occurrence_command += quote
            is_program = known_program or normalized in _command_program_tokens(occurrence_command)
            if not is_program and (normalized in artifacts or normalized in claimed_files):
                continue
            if not is_program and normalized in seen_tokens:
                continue
            seen_tokens.add(normalized)
            path_status = (
                _program_path_exists(
                    token,
                    scannable,
                    workspace_root,
                    token_position=token_start,
                    artifacts=artifacts,
                    claimed_files=claimed_files,
                )
                if is_program
                else _path_exists(token, workspace_root)
            )
            if sourced_programs and normalized not in sourced_programs:
                path_status = None
            if _program_flow_inconclusive(scannable, token_start, token_end):
                path_status = None
            if path_status is False:
                finding_key = (normalized, is_program)
                if finding_key in reported_missing_tokens:
                    continue
                reported_missing_tokens.add(finding_key)
                findings.append(
                    PreflightFinding(
                        code=(
                            "verify_program_missing" if is_program else "verify_script_unconfirmed"
                        ),
                        blocking=is_program,
                        subject=normalized,
                        detail=(
                            f"verify_command references {normalized!r}, which neither "
                            "exists in the workspace nor is declared as an expected artifact"
                        ),
                        question=(
                            f"Does {normalized!r} already exist, or must the run create it? "
                            "If the run creates it, declare it in expected_artifacts."
                        ),
                    )
                )
    return findings


def _command_program_tokens(command: str) -> frozenset[str]:
    """Return file operands that are the executable's verification program.

    This deliberately handles only unambiguous shell forms. A missing operand
    after ``python``/``bash``/``node`` or an explicit ``./program`` is a
    decidable fabrication; ordinary command arguments remain advisory.
    """
    runners = frozenset(
        {
            "bash",
            "lua",
            "luajit",
            "node",
            "perl",
            "php",
            "ruby",
            "sh",
        }
    )
    option_operands = {
        "bash": frozenset({"-O", "-o"}),
        "sh": frozenset({"-o"}),
        "node": frozenset({"-r", "--require", "--loader", "--import"}),
        "ruby": frozenset({"-I", "-r", "--require", "-C", "-E"}),
        "perl": frozenset({"-I", "-M", "-m", "-F", "-i"}),
        "php": frozenset({"-c", "-d"}),
        "lua": frozenset({"-l"}),
        "luajit": frozenset({"-l", "-j", "-O"}),
    }

    def is_runner(token: str) -> bool:
        executable = Path(token).name
        return (
            executable in runners
            or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?t?", executable) is not None
            or re.fullmatch(r"pypy\d*", executable) is not None
        )

    programs: set[str] = set()

    def unwrap_command(segment: list[str]) -> list[str]:
        """Strip standard execution wrappers without guessing their payload."""
        remaining = list(segment)
        while remaining:
            command_name = Path(remaining[0]).name
            if command_name == "env":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-S", "--split-string"} and len(remaining) >= 2:
                        try:
                            scan(shell_tokens(remaining[1]) + remaining[2:])
                        except ValueError:
                            pass
                        return []
                    if token.startswith("--split-string="):
                        try:
                            scan(shell_tokens(token.partition("=")[2]) + remaining[1:])
                        except ValueError:
                            pass
                        return []
                    if token in {"-u", "--unset", "-C", "--chdir"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                        remaining = remaining[1:]
                        continue
                    break
                continue
            if command_name == "timeout":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token in {"-s", "--signal", "-k", "--kill-after"}:
                        remaining = remaining[2:]
                        continue
                    if token.startswith("-"):
                        remaining = remaining[1:]
                        continue
                    break
                if remaining:  # duration operand
                    remaining = remaining[1:]
                continue
            if command_name == "uv" and len(remaining) >= 2 and remaining[1] == "run":
                remaining = remaining[2:]
                option_operands = {
                    "--directory",
                    "--env-file",
                    "--package",
                    "--project",
                    "--python",
                    "--python-platform",
                    "--with",
                }
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    remaining = (
                        remaining[2:]
                        if token in option_operands and len(remaining) >= 2
                        else remaining[1:]
                    )
                continue
            package_runner = (
                command_name == "npx"
                or command_name in {"npm", "pnpm", "yarn"}
                and len(remaining) >= 2
                and remaining[1] == "exec"
                or command_name == "bun"
                and len(remaining) >= 2
                and remaining[1] == "run"
            )
            if package_runner:
                remaining = remaining[1:] if command_name == "npx" else remaining[2:]
                option_operands = {
                    "--cache",
                    "--call",
                    "--package",
                    "--package-manager",
                    "--registry",
                    "--shell",
                    "-c",
                    "-p",
                }
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    remaining = (
                        remaining[2:]
                        if token in option_operands and len(remaining) >= 2
                        else remaining[1:]
                    )
                continue
            if command_name == "nice":
                remaining = remaining[1:]
                if remaining and remaining[0] in {"-n", "--adjustment"}:
                    remaining = remaining[2:]
                elif remaining and (
                    re.fullmatch(r"-n?\d+", remaining[0])
                    or remaining[0].startswith("--adjustment=")
                ):
                    remaining = remaining[1:]
                if remaining and remaining[0] == "--":
                    remaining = remaining[1:]
                continue
            if command_name == "nohup":
                remaining = remaining[1:]
                if remaining and remaining[0] == "--":
                    remaining = remaining[1:]
                continue
            if command_name == "setsid":
                remaining = remaining[1:]
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    remaining = remaining[1:]
                continue
            if command_name == "stdbuf":
                remaining = remaining[1:]
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    if token in {"-i", "-o", "-e", "--input", "--output", "--error"}:
                        remaining = remaining[2:] if len(remaining) >= 2 else []
                    elif token.startswith(("--input=", "--output=", "--error=")):
                        remaining = remaining[1:]
                    else:
                        remaining = remaining[1:]
                continue
            if command_name == "time":
                remaining = remaining[1:]
                while remaining and remaining[0].startswith("-"):
                    token = remaining[0]
                    if token == "--":
                        remaining = remaining[1:]
                        break
                    if token in {"-f", "--format", "-o", "--output"}:
                        remaining = remaining[2:] if len(remaining) >= 2 else []
                    else:
                        remaining = remaining[1:]
                continue
            if command_name == "command":
                remaining = remaining[1:]
                while remaining and remaining[0] in {"--", "-p", "-v", "-V"}:
                    option = remaining[0]
                    remaining = remaining[1:]
                    if option in {"-v", "-V"}:
                        # ``command -v``/``command -V`` query shell command
                        # resolution; their operands are names to inspect, not
                        # programs this verification command will execute.
                        return []
                continue
            if command_name == "exec":
                remaining = remaining[1:]
                while remaining:
                    token = remaining[0]
                    if token == "-a" and len(remaining) >= 2:
                        remaining = remaining[2:]
                        continue
                    if token in {"--", "-c", "-l"}:
                        remaining = remaining[1:]
                        continue
                    break
                continue
            break
        return remaining

    def shell_tokens(value: str) -> list[str]:
        lexer = shlex.shlex(value, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)

    def scan(parts: list[str]) -> None:
        segments: list[list[str]] = [[]]
        for part in parts:
            if re.fullmatch(r"[;&|\n]+", part):
                segments.append([])
            else:
                segments[-1].append(part)
        for segment in segments:
            while segment and segment[0] in {"{", "(", "then", "else", "do", "!"}:
                segment = segment[1:]
            while segment and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[0]):
                segment = segment[1:]
            segment = unwrap_command(segment)
            if segment and "/" in segment[0] and not is_runner(segment[0]):
                programs.add(_normalize_workspace_path(segment[0]))
        for segment in segments:
            while segment and segment[0] in {"{", "(", "then", "else", "do", "!"}:
                segment = segment[1:]
            while segment and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[0]):
                segment = segment[1:]
            segment = unwrap_command(segment)
            if not segment:
                continue
            executable = Path(segment[0]).name
            if (segment[0] == "." or Path(segment[0]).name == "source") and len(segment) >= 2:
                programs.add(_normalize_workspace_path(segment[1]))
                continue
            shell_payload = _shell_command_payload(segment)
            if shell_payload is not None:
                try:
                    scan(shell_tokens(shell_payload[0]))
                except ValueError:
                    pass
                continue
            if "/" in segment[0] and not is_runner(segment[0]):
                programs.add(_normalize_workspace_path(segment[0]))
            if not is_runner(segment[0]):
                continue
            runner = executable
            cursor = 1
            while cursor < len(segment) and segment[cursor].startswith("-"):
                option = segment[cursor]
                if (
                    runner in {"node", "ruby", "perl", "lua", "luajit"}
                    and option in {"-e", "-E", "--eval", "-p", "--print"}
                    or runner == "php"
                    and option in {"-r", "--run"}
                ) or (
                    re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?t?|pypy\d*)", runner)
                    and option in {"-c", "--command"}
                ):
                    cursor = len(segment)
                    break
                if option in {"-m", "--module"}:
                    cursor = len(segment)
                    break
                if option in {
                    "-X",
                    "-W",
                    "--check-hash-based-pycs",
                    *option_operands.get(runner, ()),
                }:
                    cursor += 2
                    continue
                cursor += 1
            if cursor < len(segment) and not segment[cursor].startswith("-"):
                programs.add(_normalize_workspace_path(segment[cursor]))

    try:
        scan(shell_tokens(command))
    except ValueError:
        return frozenset()
    return frozenset(programs)


def _sourced_program_tokens(command: str) -> frozenset[str]:
    """Return statically named files executed in the current shell."""

    programs: set[str] = set()

    def shell_tokens(value: str) -> list[str]:
        lexer = shlex.shlex(value, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)

    def scan(parts: list[str]) -> None:
        segments: list[list[str]] = [[]]
        for part in parts:
            if re.fullmatch(r"[;&|\n]+", part):
                segments.append([])
            else:
                segments[-1].append(part)
        for segment in segments:
            while segment and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[0]):
                segment = segment[1:]
            if segment and Path(segment[0]).name == "command":
                segment = segment[1:]
                while segment and segment[0] in {"--", "-p", "-v", "-V"}:
                    segment = segment[1:]
            if not segment:
                continue
            if (segment[0] == "." or Path(segment[0]).name == "source") and len(segment) >= 2:
                programs.add(_normalize_workspace_path(segment[1]))
                continue
            shell_payload = _shell_command_payload(segment)
            if shell_payload is not None:
                try:
                    scan(shell_tokens(shell_payload[0]))
                except ValueError:
                    pass

    try:
        scan(shell_tokens(command))
    except ValueError:
        return frozenset()
    return frozenset(programs)


def _check_unverifiable_criteria(seed: Seed) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for criterion in _specs(seed):
        if criterion.has_success_contract:
            continue
        findings.append(
            PreflightFinding(
                code="unverifiable_criterion",
                blocking=False,
                subject=criterion.description[:80],
                detail=(
                    f"acceptance criterion {criterion.description[:80]!r} declares no "
                    "verify_command, expected_artifacts, or output_assertion"
                ),
                question=(
                    f"What deterministic command or artifact proves {criterion.description[:80]!r}?"
                ),
            )
        )
    return findings


def _check_shared_verify_commands(seed: Seed) -> list[PreflightFinding]:
    grouped: dict[str, list[str]] = {}
    for criterion in _specs(seed):
        if not criterion.verify_command:
            continue
        normalized = " ".join(criterion.verify_command.split())
        grouped.setdefault(normalized, []).append(criterion.description[:80])
    findings: list[PreflightFinding] = []
    for command, descriptions in grouped.items():
        if len(descriptions) < 2:
            continue
        rendered = "; ".join(repr(description) for description in descriptions)
        findings.append(
            PreflightFinding(
                code="shared_verify_command",
                blocking=False,
                subject=command[:120],
                detail=(
                    f"acceptance criteria {rendered} share one verify_command, so their "
                    "verification results cannot be distinguished"
                ),
                question=(
                    f"Criteria {rendered} share one verify command — what distinct check "
                    "proves each one separately?"
                ),
            )
        )
    return findings
