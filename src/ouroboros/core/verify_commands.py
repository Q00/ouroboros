"""Lexical checks for acceptance-criteria verify commands.

Seed contracts must emit verify commands whose exit code is the verdict,
so this module owns the quote- and expansion-aware scanners that reject
heredoc syntax and always-succeeding status-masking fallbacks (#2155).
It is shared by SeedGenerator and persisted Seed validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _starts_posix_shell_comment(char: str, *, word_started: bool) -> bool:
    """Return whether an unquoted ``#`` begins a fresh POSIX comment token."""
    return char == "#" and not word_started


def _ends_posix_shell_word(value: str, index: int) -> bool:
    """Return whether ``index`` starts a shell token boundary after a word."""
    return index >= len(value) or value[index].isspace() or value[index] in ";&|(){}<>"


@dataclass
class _PosixCaseTracker:
    """Track the reserved-word and pattern phases of nested POSIX case commands."""

    states: list[str] = field(default_factory=list)
    command_position: bool = True
    word_started: bool = False
    function_name_candidate: bool = False
    awaiting_function_body: bool = False
    for_phase: str | None = None
    invalid: bool = False

    @property
    def incomplete(self) -> bool:
        """Return whether a compound construct is unfinished or malformed."""
        return bool(self.states or self.for_phase is not None or self.invalid)

    def consume_segment(self) -> None:
        """Record a quote, escape, or expansion joined to the current shell word."""
        self.word_started = True
        self.function_name_candidate = False

    def consume_word(self, word: str, *, token_ends: bool) -> None:
        """Consume one unquoted identifier while preserving token provenance."""
        if self.awaiting_function_body:
            # POSIX function bodies are compound commands. Braces and subshells
            # are handled by ``consume_grouping``; the remaining direct forms
            # begin with one of these reserved words.
            if (
                not self.word_started
                and token_ends
                and word in {"case", "if", "while", "until", "for"}
            ):
                self.awaiting_function_body = False
                self.command_position = True
            else:
                # Reject malformed bodies so a case pattern cannot expose outer
                # acceptance-criterion fields.
                self.invalid = True
                self.awaiting_function_body = False
        reserved = not self.word_started and token_ends
        if self.for_phase == "await_name":
            self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            if token_ends:
                self.for_phase = "after_name"
            return
        if self.for_phase == "after_name":
            if reserved and word == "in":
                self.for_phase = "in_list"
            else:
                self.invalid = True
            self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            return
        if self.for_phase == "in_list":
            self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            return
        if self.for_phase == "await_do":
            if reserved and word == "do":
                self.for_phase = None
                self.command_position = True
            else:
                self.invalid = True
                self.command_position = False
            self.function_name_candidate = False
            self.word_started = True
            return
        state = self.states[-1] if self.states else None
        if state == "pattern" and not (reserved and self.command_position and word == "esac"):
            self.word_started = True
            self.function_name_candidate = False
            return
        was_command_position = self.command_position
        if reserved and self.command_position and word == "case":
            self.states.append("await_in")
            self.command_position = False
        elif reserved and state == "await_in" and word == "in":
            self.states[-1] = "pattern"
            self.command_position = True
        elif (
            reserved
            and self.states
            and self.command_position
            and word == "esac"
            and self.states[-1] in {"pattern", "action"}
        ):
            self.states.pop()
            self.command_position = False
        elif (
            reserved
            and self.command_position
            and word in {"if", "then", "elif", "else", "while", "until", "do", "for"}
        ):
            if word == "for":
                self.for_phase = "await_name"
                self.command_position = False
            else:
                self.command_position = True
        else:
            self.command_position = False
        self.function_name_candidate = (
            was_command_position
            and token_ends
            and not (
                reserved
                and word in {"case", "if", "then", "elif", "else", "while", "until", "do", "for"}
            )
        )
        self.word_started = self.for_phase != "await_name"

    def consume_function_header(self, value: str, index: int) -> int:
        """Consume the ``()`` after a plain command-position function name."""
        if self.function_name_candidate and value.startswith("()", index):
            self.function_name_candidate = False
            self.awaiting_function_body = True
            self.command_position = False
            self.word_started = False
            return 2
        return 0

    def consume_grouping(self, char: str) -> None:
        """Enter or leave a brace/subshell compound-command boundary."""
        if char == "{" and (self.command_position or self.awaiting_function_body):
            self.awaiting_function_body = False
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
        elif char == "(":
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
        else:
            self.command_position = False
            self.word_started = False
            self.function_name_candidate = False

    def consume_case_operator(self, value: str, index: int) -> int:
        """Consume case-only syntax and return the number of bytes claimed."""
        if self.states and self.states[-1] == "action" and value.startswith(";;", index):
            self.states[-1] = "pattern"
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
            return 2
        if self.states and self.states[-1] == "pattern":
            char = value[index]
            if char == ")":
                self.states[-1] = "action"
                self.command_position = True
                self.word_started = False
                self.function_name_candidate = False
                return 1
            if char in "(|":
                self.word_started = False
                self.function_name_candidate = False
                return 1
        return 0

    def consume_ordinary(self, char: str) -> None:
        """Update token position for non-case shell text."""
        if char.isspace():
            if self.for_phase == "await_name" and self.word_started:
                self.for_phase = "after_name"
            if char == "\n" and self.for_phase in {"after_name", "in_list"}:
                self.for_phase = "await_do"
                self.command_position = True
            self.word_started = False
        elif char in ";|&\n" or (char == "!" and self.command_position and not self.word_started):
            if self.for_phase == "await_name" and self.word_started:
                self.for_phase = "after_name"
            if char == ";" and self.for_phase in {"after_name", "in_list"}:
                self.for_phase = "await_do"
            elif self.for_phase is not None and char in "|&":
                self.invalid = True
            self.command_position = True
            self.word_started = False
            self.function_name_candidate = False
        else:
            self.word_started = True
            self.function_name_candidate = False


def _posix_expansion_end(value: str, index: int) -> int | None:
    """Return the end of one balanced ``$()``/``${}`` shell expansion.

    A command substitution is not merely parenthesis-balanced shell text:
    each POSIX ``case`` pattern has an unmatched syntactic ``)``.  Track the
    small reserved-word state needed to keep those pattern terminators inside
    their substitution while leaving ordinary subshell parentheses on the
    existing frame stack.
    """
    if index + 1 >= len(value) or value[index] != "$" or value[index + 1] not in "({":
        return None
    frames: list[tuple[str, str | None, _PosixCaseTracker | None]] = [
        (")", None, _PosixCaseTracker()) if value[index + 1] == "(" else ("}", None, None)
    ]
    quote: str | None = None
    escaped = False
    cursor = index + 2
    while cursor < len(value):
        char = value[cursor]
        if quote == "'":
            if char == "'":
                quote = None
            cursor += 1
            continue
        if escaped:
            escaped = False
            cursor += 1
            continue
        if char == "\\":
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            escaped = True
            cursor += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                cursor += 1
                continue
            if char == "$" and cursor + 1 < len(value) and value[cursor + 1] in "({":
                tracker = frames[-1][2]
                if tracker is not None:
                    tracker.consume_segment()
                frames.append(
                    (")", quote, _PosixCaseTracker())
                    if value[cursor + 1] == "("
                    else ("}", quote, None)
                )
                quote = None
                cursor += 2
                continue
            if char == "`":
                substitution_end = _posix_backtick_substitution_end(value, cursor)
                if substitution_end is None:
                    return None
                tracker = frames[-1][2]
                if tracker is not None:
                    tracker.consume_segment()
                cursor = substitution_end
                continue
            cursor += 1
            continue
        if char in {"'", '"'}:
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            quote = char
            cursor += 1
            continue
        if char == "`":
            substitution_end = _posix_backtick_substitution_end(value, cursor)
            if substitution_end is None:
                return None
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            cursor = substitution_end
            continue
        if char == "$" and cursor + 1 < len(value) and value[cursor + 1] in "({":
            tracker = frames[-1][2]
            if tracker is not None:
                tracker.consume_segment()
            frames.append(
                (")", quote, _PosixCaseTracker())
                if value[cursor + 1] == "("
                else ("}", quote, None)
            )
            quote = None
            cursor += 2
            continue
        closer, _, tracker = frames[-1]
        if tracker is not None and _starts_posix_shell_comment(
            char, word_started=tracker.word_started
        ):
            newline = value.find("\n", cursor + 1)
            if newline < 0:
                return None
            tracker.consume_ordinary("\n")
            cursor = newline + 1
            continue
        if tracker is not None and (char.isascii() and (char.isalpha() or char == "_")):
            word_end = cursor + 1
            while word_end < len(value) and (
                value[word_end].isascii() and (value[word_end].isalnum() or value[word_end] == "_")
            ):
                word_end += 1
            tracker.consume_word(
                value[cursor:word_end],
                token_ends=_ends_posix_shell_word(value, word_end),
            )
            cursor = word_end
            continue
        if tracker is not None:
            claimed = tracker.consume_case_operator(value, cursor)
            if claimed:
                cursor += claimed
                continue
            claimed = tracker.consume_function_header(value, cursor)
            if claimed:
                cursor += claimed
                continue
        if char == "(":
            arithmetic = cursor >= 2 and value[cursor - 2 : cursor] == "$("
            frames.append((")", quote, None if arithmetic else _PosixCaseTracker()))
            cursor += 1
            continue
        if char == frames[-1][0]:
            _, return_quote, closing_tracker = frames.pop()
            if closing_tracker is not None and closing_tracker.incomplete:
                return None
            quote = return_quote
            cursor += 1
            if not frames:
                return cursor
            parent_tracker = frames[-1][2]
            if parent_tracker is not None:
                parent_tracker.consume_segment()
            continue
        if tracker is not None:
            if char in "{}":
                tracker.consume_grouping(char)
                cursor += 1
                continue
            tracker.consume_ordinary(char)
        cursor += 1
    return None


def _unsupported_verify_command_reason(command: str) -> str | None:
    if "\n" in command or "\r" in command:
        return "verify_command must be a single-line command"
    if _contains_posix_heredoc_operator(command):
        return "verify_command uses heredoc/multiline shell syntax; use python -c or pytest instead"
    return _status_masking_verify_command_reason(command)


def _status_masking_verify_command_reason(command: str) -> str | None:
    """Return the shared persisted-contract rejection for masked exit status."""
    if _contains_status_masking_fallback(command):
        return _STATUS_MASKING_REASON
    return None


_STATUS_MASKING_REASON = (
    "verify_command chains into an always-succeeding fallback such as `|| true`; "
    "the exit code is the verdict, so a masked command verifies nothing (and "
    "`|| true` cannot run on Windows); use one command that exits 0 only when "
    "the criterion is met"
)


@dataclass(frozen=True)
class _ShellToken:
    """One outer-shell lexical token with its executable syntax role."""

    kind: str
    value: str


def _contains_status_masking_fallback(command: str) -> bool:
    """Return whether a no-op success can replace the tested command's status.

    The verify contract is one command whose exit code is the verdict, so an
    always-succeeding fallback after ``||``, ``|``, ``;``, or ``&`` makes the
    final status independent of the tested command (#2155). ``|| true`` masks
    wherever it appears, including mid-command chains such as
    ``cmd || true; echo done`` whose tail looks legitimate; ``;``/``&``/pipe
    fallbacks matter only at the end of the command. Quoted text and shell
    expansions are data. ``&&`` preserves a left-side failure and stays valid,
    and single-``|`` case-pattern alternatives mid-command are unaffected.
    """
    tokens = _lex_posix_shell_tokens(command)
    for index, token in enumerate(tokens):
        if token.kind != "operator" or token.value not in {"||", ";", "&", "|"}:
            continue
        fallback_start = index + 1
        command_index = _first_command_word_after(tokens, fallback_start)
        assignment_only = _is_assignment_only_command(tokens, fallback_start)
        always_succeeds = (
            assignment_only
            if command_index is None
            else _is_always_successful_command(tokens, command_index)
        )
        if not always_succeeds:
            continue
        if token.value == "||" or _command_is_terminal(tokens, command_index or fallback_start):
            return True
    return False


def _lex_posix_shell_tokens(command: str) -> list[_ShellToken]:
    """Return outer-shell words and operators after quote removal and escaping.

    This is deliberately a lexical classifier rather than a shell parser. The
    status rule needs to distinguish executable operator tokens from quoted or
    escaped lookalikes, while treating substitutions as dynamic word content.
    """
    tokens: list[_ShellToken] = []
    word: list[str] = []
    quote: str | None = None
    word_started = False
    index = 0

    def flush_word() -> None:
        nonlocal word_started
        if word_started:
            tokens.append(_ShellToken("word", "".join(word)))
        word.clear()
        word_started = False

    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                word.append(char)
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
            if char == "\\" and index + 1 < len(command):
                escaped = command[index + 1]
                if escaped in {"$", "`", '"', "\\", "\n"}:
                    if escaped != "\n":
                        word.append(escaped)
                    index += 2
                    continue
            if char == "$" and index + 1 < len(command) and command[index + 1] in "({":
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    word.append("\0")
                    index = expansion_end
                    continue
            if char == "`":
                expansion_end = _posix_backtick_substitution_end(command, index)
                if expansion_end is not None:
                    word.append("\0")
                    index = expansion_end
                    continue
            word.append(char)
            index += 1
            continue
        if char == "\\":
            word_started = True
            if index + 1 < len(command):
                word.append(command[index + 1])
                index += 2
            else:
                word.append(char)
                index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            word_started = True
            index += 1
            continue
        if char == "$" and index + 1 < len(command) and command[index + 1] in "({":
            expansion_end = _posix_expansion_end(command, index)
            if expansion_end is not None:
                word.append("\0")
                word_started = True
                index = expansion_end
                continue
        if char == "`":
            expansion_end = _posix_backtick_substitution_end(command, index)
            if expansion_end is not None:
                word.append("\0")
                word_started = True
                index = expansion_end
                continue
        if char.isspace():
            flush_word()
            index += 1
            continue
        if char == "#" and not word_started:
            break
        if char in ";|&":
            flush_word()
            if char in "|&" and index + 1 < len(command) and command[index + 1] == char:
                tokens.append(_ShellToken("operator", char * 2))
                index += 2
            else:
                tokens.append(_ShellToken("operator", char))
                index += 1
            continue
        if char in "<>":
            if word and "".join(word).isdigit():
                word.clear()
                word_started = False
            else:
                flush_word()
            end = index + 1
            if end < len(command) and command[end] in "<>&|-":
                end += 1
            tokens.append(_ShellToken("redirect", ""))
            index = end
            continue
        if char in "(){}":
            flush_word()
            tokens.append(_ShellToken("group", char))
            index += 1
            continue
        word.append(char)
        word_started = True
        index += 1
    flush_word()
    return tokens


def _first_command_word_after(tokens: list[_ShellToken], index: int) -> int | None:
    """Return the next executable word, skipping redirections and assignments."""
    while index < len(tokens):
        token = tokens[index]
        if token.kind in {"operator", "group"}:
            return None
        if token.kind == "redirect":
            index += 2
            continue
        if _is_posix_assignment_word(token.value):
            index += 1
            continue
        return index
    return None


def _is_posix_assignment_word(value: str) -> bool:
    """Return whether a lexed word is a leading POSIX shell assignment."""
    name, separator, _ = value.partition("=")
    return bool(
        separator
        and name
        and name.isascii()
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character == "_" for character in name[1:])
    )


def _is_assignment_only_command(tokens: list[_ShellToken], index: int) -> bool:
    """Return whether a simple command contains only assignments/redirections."""
    found_assignment = False
    while index < len(tokens):
        token = tokens[index]
        if token.kind in {"operator", "group"}:
            return False
        if token.kind == "redirect":
            index += 2
            continue
        if not _is_posix_assignment_word(token.value):
            return False
        found_assignment = True
        index += 1
    return found_assignment


def _is_always_successful_command(tokens: list[_ShellToken], index: int) -> bool:
    """Return whether the command beginning at ``index`` has a fixed success status."""
    command = tokens[index].value
    if command in {"true", ":"}:
        return True
    if command != "exit":
        return False
    status_index = _first_command_word_after(tokens, index + 1)
    return status_index is not None and tokens[status_index].value == "0"


def _command_is_terminal(tokens: list[_ShellToken], index: int) -> bool:
    """Return whether no outer command-list operator follows this command."""
    return all(token.kind != "operator" for token in tokens[index + 1 :])


def _contains_posix_heredoc_operator(command: str) -> bool:
    """Return whether shell code contains an active ``<<``/``<<-`` operator.

    The delimiter is a POSIX shell word, so quoting may be backslash-based or
    fragmented (``<<\\EOF``, ``<<E"OF"``). Looking for a delimiter spelling
    with one regex is therefore bypassable. Instead, recognize the operator
    in shell lexical context: quoted strings, comments, parameter expansion,
    and arithmetic expansion cannot introduce a heredoc operator themselves.
    Ordinary comparison text embedded in those contexts remains valid.
    """
    quote: str | None = None
    escaped = False
    word_started = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if escaped:
            escaped = False
            word_started = True
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
            if char == "$" and index + 1 < len(command):
                is_parameter = command[index + 1] == "{"
                is_arithmetic = command.startswith("$((", index)
                if is_parameter or is_arithmetic:
                    expansion_end = _posix_expansion_end(command, index)
                    if expansion_end is not None:
                        inner_start = index + (3 if is_arithmetic else 2)
                        inner_end = expansion_end - (2 if is_arithmetic else 1)
                        if _contains_nested_shell_substitution_heredoc(
                            command[inner_start:inner_end]
                        ):
                            return True
                        index = expansion_end
                        continue
            if (
                char == "$"
                and command.startswith("$(", index)
                and not command.startswith("$((", index)
            ):
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    if _contains_posix_heredoc_operator(command[index + 2 : expansion_end - 1]):
                        return True
                    index = expansion_end
                    continue
            if char == "`":
                substitution_end = _posix_backtick_substitution_end(command, index)
                if substitution_end is None:
                    return True
                if _contains_posix_heredoc_operator(command[index + 1 : substitution_end - 1]):
                    return True
                index = substitution_end
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            word_started = True
            index += 1
            continue
        if char == "`":
            substitution_end = _posix_backtick_substitution_end(command, index)
            if substitution_end is None:
                return True
            if _contains_posix_heredoc_operator(command[index + 1 : substitution_end - 1]):
                return True
            word_started = True
            index = substitution_end
            continue
        if char == "#" and not word_started:
            return False
        if char == "$" and index + 1 < len(command):
            is_parameter = command[index + 1] == "{"
            is_arithmetic = command.startswith("$((", index)
            if is_parameter or is_arithmetic:
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    inner_start = index + (3 if is_arithmetic else 2)
                    inner_end = expansion_end - (2 if is_arithmetic else 1)
                    if _contains_nested_shell_substitution_heredoc(command[inner_start:inner_end]):
                        return True
                    word_started = True
                    index = expansion_end
                    continue
            if command[index + 1] == "(":
                expansion_end = _posix_expansion_end(command, index)
                if expansion_end is not None:
                    if _contains_posix_heredoc_operator(command[index + 2 : expansion_end - 1]):
                        return True
                    word_started = True
                    index = expansion_end
                    continue
        if char == "<" and index + 1 < len(command) and command[index + 1] == "<":
            return True
        if char.isspace() or char in ";|&()<>":
            word_started = False
        else:
            word_started = True
        index += 1
    return False


def _posix_backtick_substitution_end(value: str, index: int) -> int | None:
    """Return just past the next unescaped legacy command-substitution backtick."""
    if index >= len(value) or value[index] != "`":
        return None
    escaped = False
    cursor = index + 1
    while cursor < len(value):
        char = value[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            return cursor + 1
        cursor += 1
    return None


def _contains_nested_shell_substitution_heredoc(value: str) -> bool:
    """Inspect only executable substitutions inside parameter/arithmetic text.

    Raw ``<<`` in these frames is data or an arithmetic shift, not shell
    redirection. Nested ``$()`` and legacy backticks execute shell code and
    must therefore be scanned with the full heredoc detector.
    """
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == "$" and index + 1 < len(value) and value[index + 1] in "({":
            expansion_end = _posix_expansion_end(value, index)
            if expansion_end is not None:
                is_arithmetic = value.startswith("$((", index)
                is_parameter = value[index + 1] == "{"
                if is_arithmetic or is_parameter:
                    inner_start = index + (3 if is_arithmetic else 2)
                    inner_end = expansion_end - (2 if is_arithmetic else 1)
                    if _contains_nested_shell_substitution_heredoc(value[inner_start:inner_end]):
                        return True
                elif _contains_posix_heredoc_operator(value[index + 2 : expansion_end - 1]):
                    return True
                index = expansion_end
                continue
        if char == "`":
            substitution_end = _posix_backtick_substitution_end(value, index)
            if substitution_end is None:
                return True
            if _contains_posix_heredoc_operator(value[index + 1 : substitution_end - 1]):
                return True
            index = substitution_end
            continue
        index += 1
    return False
