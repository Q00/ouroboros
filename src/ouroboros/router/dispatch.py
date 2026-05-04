"""Shared deterministic router for ``ooo`` skill dispatch.

This module implements the stateless resolver exported by
:mod:`ouroboros.router`. It owns only deterministic parsing and frontmatter
normalization: command-prefix parsing, packaged ``SKILL.md`` lookup,
``mcp_tool``/``mcp_args`` validation, argument extraction, and deterministic
template substitution.

Runtime-specific concerns stay outside this module. The Codex CLI, Hermes, and
Opencode runtimes pass a :class:`ResolveRequest`, inspect the returned
:data:`ResolveResult` variant, and then handle their own structured logging,
``AgentMessage`` assembly, and MCP handler invocation. The router itself keeps
no mutable state and intentionally performs no logging or MCP calls.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import re
import shlex
import shutil
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from ouroboros.codex import resolve_packaged_codex_skill_path
from ouroboros.router.command_parser import parse_ooo_command
from ouroboros.router.registry import packaged_skill_dispatch_registry
from ouroboros.router.types import (
    DispatchTarget,
    DispatchTargetKind,
    InvalidInputReason,
    InvalidSkill,
    MCPDispatchTarget,
    McpDispatchTarget,
    MCPFrontmatterArgs,
    MCPFrontmatterScalar,
    MCPFrontmatterValue,
    NoMatchReason,
    NormalizedMCPFrontmatter,
    NormalizedMcpFrontmatter,
    NotHandled,
    ParsedOooCommand,
    Resolved,
    ResolveOutcome,
    ResolveResult,
)

_MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SKILL_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DISPATCH_TEMPLATE_PATTERN = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|1)(?![A-Za-z0-9_])")
_DISPATCH_TEMPLATE_EXACT_PATTERN = re.compile(
    r"^\$(?P<name>[A-Za-z_][A-Za-z0-9_]*|1)(?![A-Za-z0-9_])$"
)
_INTEGER_OPTION_PATTERN = re.compile(r"^[+-]?\d+$")
_BOOLEAN_OPTION_NAMES = frozenset({"skip_run"})
_VALUE_OPTION_NAMES = frozenset({"resume", "max_interview_rounds", "max_repair_rounds"})
_CONTROL_OPTION_NAMES = _BOOLEAN_OPTION_NAMES | _VALUE_OPTION_NAMES
# Windows literal path payloads (drive-letter `C:\…` or UNC `\\server\share\…`)
# must skip shell tokenization — `shlex.split` treats backslash as an escape and
# silently drops it, so `C:\temp\seed.yaml` would dispatch as `C:tempseed.yaml`.
_WINDOWS_LITERAL_PATH_PATTERN = re.compile(r"^(?:[A-Za-z]:\\|\\\\)")
_REQUIRED_MCP_FRONTMATTER_KEYS = ("mcp_tool", "mcp_args")
_MCP_FRONTMATTER_VALUE_TYPES = "string, finite number, boolean, null, list, or mapping"
_PACKAGED_SKILL_CACHE: TemporaryDirectory[str] | None = None


@dataclass(frozen=True, slots=True)
class ResolveRequest:
    """Runtime caller input for deterministic skill dispatch resolution.

    Attributes:
        prompt: Full user or orchestrator prompt to inspect for a supported
            deterministic skill prefix.
        cwd: Runtime working directory used when substituting ``$CWD`` in
            ``mcp_args`` templates.
        skills_dir: Optional packaged-skill override directory. Runtimes pass
            this through when tests or local installations need non-default
            skill assets.
    """

    prompt: str
    cwd: str | Path
    skills_dir: str | Path | None = None


RouterRequest = ResolveRequest


def _packaged_skill_cache_root() -> Path:
    """Return a process-lifetime cache root for packaged skill entrypoints."""
    global _PACKAGED_SKILL_CACHE

    if _PACKAGED_SKILL_CACHE is None:
        _PACKAGED_SKILL_CACHE = TemporaryDirectory(prefix="ouroboros-router-skills-")
    return Path(_PACKAGED_SKILL_CACHE.name)


def _cache_packaged_skill_entrypoint(skill_name: str, skill_path: Path) -> Path:
    """Copy one packaged ``SKILL.md`` into a stable process-lifetime cache."""
    safe_skill_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", skill_name.strip()).strip("._")
    if not safe_skill_name:
        safe_skill_name = "skill"

    cached_path = _packaged_skill_cache_root() / safe_skill_name / skill_path.name
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_path, cached_path)
    return cached_path


@contextmanager
def resolve_packaged_skill_path(
    skill_name: str,
    *,
    skills_dir: str | Path | None = None,
) -> Iterator[Path]:
    """Resolve a packaged skill entrypoint for the lifetime of the context.

    Packaged resources may be materialized through ``importlib.resources.as_file``,
    so default package lookups are copied into a process-lifetime cache before
    yielding. Explicit ``skills_dir`` lookups yield the caller-owned path.
    """
    with resolve_packaged_codex_skill_path(skill_name, skills_dir=skills_dir) as skill_path:
        if skills_dir is None:
            yield _cache_packaged_skill_entrypoint(skill_name, skill_path)
            return
        yield skill_path


def load_skill_frontmatter(skill_md_path: Path) -> dict[str, Any]:
    """Load YAML frontmatter from a packaged ``SKILL.md`` file."""
    content = skill_md_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        msg = f"Unterminated frontmatter in {skill_md_path}"
        raise ValueError(msg)

    frontmatter_text = "\n".join(lines[1:closing_index]).strip()
    if not frontmatter_text:
        return {}

    parsed = yaml.safe_load(frontmatter_text)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")

    return parsed


def _format_dispatch_value_path(parent_path: str, key: str) -> str:
    """Format a readable validation path for nested MCP frontmatter values."""
    if key.isidentifier():
        return f"{parent_path}.{key}"
    return f"{parent_path}[{key!r}]"


def _validate_dispatch_mapping(value: Mapping[Any, Any], *, path: str) -> str | None:
    """Validate one MCP frontmatter mapping recursively."""
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            return f"{path} keys must be non-empty strings"

        error = _validate_dispatch_value(
            item,
            path=_format_dispatch_value_path(path, key),
        )
        if error is not None:
            return error

    return None


def _validate_dispatch_value(value: Any, *, path: str) -> str | None:
    """Validate one frontmatter MCP argument value recursively."""
    if value is None or isinstance(value, str | bool | int):
        return None

    if isinstance(value, float):
        if math.isfinite(value):
            return None
        return f"{path} must be a finite number"

    if isinstance(value, list):
        for index, item in enumerate(value):
            error = _validate_dispatch_value(item, path=f"{path}[{index}]")
            if error is not None:
                return error
        return None

    if isinstance(value, Mapping):
        return _validate_dispatch_mapping(value, path=path)

    return (
        f"{path} has unsupported type {type(value).__name__}; "
        f"expected {_MCP_FRONTMATTER_VALUE_TYPES}"
    )


def _clone_dispatch_value(value: Any) -> Any:
    """Clone validated metadata into canonical plain Python containers."""
    if isinstance(value, Mapping):
        return {key: _clone_dispatch_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_dispatch_value(item) for item in value]
    return value


def _missing_required_frontmatter_key(frontmatter: Mapping[str, Any]) -> str | None:
    """Return the first missing key required for MCP skill dispatch."""
    return next(
        (key for key in _REQUIRED_MCP_FRONTMATTER_KEYS if key not in frontmatter),
        None,
    )


def normalize_mcp_frontmatter(
    frontmatter: Mapping[str, Any],
) -> tuple[NormalizedMCPFrontmatter | None, str | None]:
    """Validate skill MCP frontmatter and return canonical dispatch metadata.

    Valid MCP frontmatter has a non-empty ``mcp_tool`` identifier and an
    ``mcp_args`` mapping with non-empty string keys and YAML-safe values. The
    returned dataclass is detached from caller-owned inputs and uses plain
    ``dict`` and ``list`` containers recursively.
    """
    if not isinstance(frontmatter, Mapping):
        return None, "SKILL.md frontmatter must be a mapping"

    missing_key = _missing_required_frontmatter_key(frontmatter)
    if missing_key is not None:
        return None, f"missing required frontmatter key: {missing_key}"

    raw_mcp_tool = frontmatter["mcp_tool"]
    if not isinstance(raw_mcp_tool, str) or not raw_mcp_tool.strip():
        return None, "mcp_tool must be a non-empty string"

    mcp_tool = raw_mcp_tool.strip()
    if _MCP_TOOL_NAME_PATTERN.fullmatch(mcp_tool) is None:
        return None, "mcp_tool must contain only letters, digits, and underscores"

    raw_mcp_args = frontmatter["mcp_args"]
    if not isinstance(raw_mcp_args, Mapping):
        return None, "mcp_args must be a mapping with string keys and YAML-safe values"

    validation_error = _validate_dispatch_mapping(raw_mcp_args, path="mcp_args")
    if validation_error is not None:
        return None, validation_error

    return (
        NormalizedMCPFrontmatter(
            mcp_tool=mcp_tool,
            mcp_args=_clone_dispatch_value(raw_mcp_args),
        ),
        None,
    )


def _try_extract_quoted_windows_literal_payload(stripped: str) -> str | None:
    """Preserve a quoted Windows literal path while shell-normalizing the tail.

    Quoting is the natural way to pass UNC or drive-letter paths that contain
    spaces (e.g. ``"\\\\server\\share\\dir name\\seed.yaml" --strict``).
    POSIX ``shlex`` would interpret the embedded backslashes as escape
    characters and corrupt the path, so we peek inside a leading quote pair
    and only short-circuit when the inner token matches
    :data:`_WINDOWS_LITERAL_PATH_PATTERN`. The path itself is returned
    verbatim (backslashes intact); any trailing tokens go through
    :func:`shlex.split` exactly like the rest of the parser, so callers see
    the same quote-stripped, single-space-joined tail they would get for any
    other quoted prefix. The closing quote must be followed by either
    end-of-string or whitespace, so an embedded mid-token quote like
    ``"C:\\Pro"gram Files\\seed.yaml`` cannot silently truncate the payload to
    a ``C:\\Pro`` prefix.
    """
    if not stripped or stripped[0] not in ('"', "'"):
        return None
    quote = stripped[0]
    closing_index = stripped.find(quote, 1)
    if closing_index == -1:
        return None
    after_close = stripped[closing_index + 1 :]
    if after_close and not after_close[0].isspace():
        return None
    inner = stripped[1:closing_index]
    if not _WINDOWS_LITERAL_PATH_PATTERN.match(inner):
        return None
    tail = after_close.lstrip()
    if not tail:
        return inner
    try:
        tail_parts = shlex.split(tail)
    except ValueError:
        tail_parts = tail.split()
    if not tail_parts:
        return inner
    return " ".join([inner, *tail_parts])


def extract_first_argument(remainder: str | None) -> str | None:
    """Extract the full argument payload following a skill command prefix.

    The legacy name is preserved for API stability, but the semantics cover the
    whole remainder. Multiline payloads are preserved exactly for inline
    content such as Seed YAML. Unquoted Windows literal path payloads (drive-
    letter or UNC) are preserved verbatim. When the user wraps a Windows path
    in quotes — the natural form for UNC paths that contain spaces — the path
    itself stays verbatim while any trailing tokens still flow through the
    standard shell-style normalization, so quoted-tail behavior matches every
    other quoted payload. Other single-line payloads still use shell-style
    tokenization purely to strip matching quotes and escape sequences, then
    tokens are rejoined with single spaces so natural-language usage like
    ``ooo interview add dark mode to settings`` yields the full phrase rather
    than just ``add``. Quoted forms such as ``ooo interview "add dark mode"``
    produce the same unquoted result. If shell tokenization fails (unterminated
    quote), a whitespace split is used as fallback.
    """
    if remainder is None or not remainder.strip():
        return None
    if re.search(r"[\r\n].*\S", remainder):
        return remainder
    stripped = remainder.strip()
    if _WINDOWS_LITERAL_PATH_PATTERN.match(stripped):
        return remainder
    quoted_windows = _try_extract_quoted_windows_literal_payload(stripped)
    if quoted_windows is not None:
        return quoted_windows
    try:
        parts = shlex.split(remainder)
    except ValueError:
        parts = remainder.split()
    return " ".join(parts) if parts else None


def _shell_split_remainder(remainder: str | None) -> list[str]:
    """Return shell-normalized tokens for a single-line command remainder."""
    if remainder is None or not remainder.strip() or re.search(r"[\r\n].*\S", remainder):
        return []
    stripped = remainder.strip()
    if _WINDOWS_LITERAL_PATH_PATTERN.match(stripped):
        return [remainder]
    quoted_windows = _try_extract_quoted_windows_literal_payload(stripped)
    if quoted_windows is not None:
        return shlex.split(quoted_windows) if quoted_windows.strip() else []
    try:
        return shlex.split(remainder)
    except ValueError:
        return remainder.split()


def _starts_with_quoted_payload(remainder: str | None) -> bool:
    """Return whether the single-line payload begins with a shell-quoted argument."""
    return bool(remainder and remainder.lstrip().startswith(("'", '"')))


def _coerce_named_option_value(value: str | bool) -> str | int | bool:
    """Coerce deterministic CLI-style option values for MCP JSON payloads."""
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if _INTEGER_OPTION_PATTERN.fullmatch(value.strip()) is not None:
        return int(value)
    return value


def _extract_dispatch_template_values(
    remainder: str | None,
    *,
    first_argument: str | None,
    cwd: str | Path,
) -> dict[str, Any]:
    """Build template values from deterministic command arguments.

    ``$1`` remains the legacy full remainder payload for compatibility. New
    named placeholders (for example ``$resume`` or ``$skip_run``) are resolved
    from long ``--kebab-case`` options, while ``$args``/``$goal`` contain the
    shell-normalized positional payload with those options removed.
    """
    values: dict[str, Any] = {
        "1": first_argument or "",
        "CWD": str(cwd),
        "resume": "",
        "skip_run": "",
        "max_interview_rounds": "",
        "max_repair_rounds": "",
    }
    tokens = _shell_split_remainder(remainder)
    if not tokens and first_argument:
        # Multiline payloads intentionally skip shell tokenization so Seed YAML
        # and free-form goals keep their original bytes. Named-template skills
        # must therefore fall back to the legacy full-remainder payload instead
        # of treating the goal as empty.
        values["args"] = first_argument
        values["goal"] = first_argument
        return values

    positional: list[str] = []
    parse_trailing_options = _starts_with_quoted_payload(remainder)
    seen_positional = False
    positional_count = 0
    literal_control_cues = {
        "about",
        "around",
        "document",
        "documents",
        "documenting",
        "explain",
        "explains",
        "explaining",
        "for",
        "mention",
        "mentions",
        "mentioning",
        "regarding",
        "support",
        "supports",
        "supporting",
        "to",
        "with",
    }

    def append_positional(token: str) -> None:
        nonlocal parse_trailing_options, positional_count, seen_positional
        positional.append(token)
        seen_positional = True
        positional_count += 1
        if positional_count > 1:
            parse_trailing_options = False

    def option_name_for(token: str) -> str:
        return token[2:].split("=", 1)[0].strip().replace("-", "_")

    def control_suffix_starts_at(start: int) -> bool:
        suffix_index = start
        while suffix_index < len(tokens):
            suffix_token = tokens[suffix_index]
            if not suffix_token.startswith("--") or suffix_token == "--":
                return False
            suffix_option_name = option_name_for(suffix_token)
            if suffix_option_name not in _CONTROL_OPTION_NAMES:
                return False
            if "=" in suffix_token or suffix_option_name in _BOOLEAN_OPTION_NAMES:
                suffix_index += 1
                continue
            if (
                suffix_option_name in _VALUE_OPTION_NAMES
                and suffix_index + 1 < len(tokens)
                and not tokens[suffix_index + 1].startswith("--")
            ):
                suffix_index += 2
                continue
            return False
        return True

    def previous_token_marks_literal_control() -> bool:
        return bool(positional and positional[-1].strip().lower() in literal_control_cues)

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--") or token == "--":
            append_positional(token)
            index += 1
            continue

        if seen_positional and not parse_trailing_options:
            if not control_suffix_starts_at(index) or previous_token_marks_literal_control():
                append_positional(token)
                index += 1
                continue

        option = token[2:]
        if not option:
            index += 1
            continue
        option_name = option.split("=", 1)[0].strip().replace("-", "_")
        if option_name not in _CONTROL_OPTION_NAMES:
            append_positional(token)
            index += 1
            continue
        if "=" in option:
            raw_name, raw_value = option.split("=", 1)
            option_value: str | bool = raw_value
        elif option_name in _BOOLEAN_OPTION_NAMES:
            raw_name = option
            option_value = True
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            raw_name = option
            option_value = tokens[index + 1]
            index += 1
        elif option_name in _VALUE_OPTION_NAMES:
            raise ValueError(f"--{option} requires a value")
        else:
            raw_name = option
            option_value = True

        name = raw_name.strip().replace("-", "_")
        if name:
            values[name] = _coerce_named_option_value(option_value)
        index += 1

    positional_payload = " ".join(positional)
    values["args"] = positional_payload
    values["goal"] = positional_payload
    return values


def resolve_dispatch_templates(
    value: Any,
    *,
    first_argument: str | None,
    cwd: str | Path = "",
    template_values: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve deterministic frontmatter template values."""
    resolved_cwd = str(cwd)
    replacements = dict(template_values or {"1": first_argument or "", "CWD": resolved_cwd})
    if isinstance(value, str):
        exact = _DISPATCH_TEMPLATE_EXACT_PATTERN.fullmatch(value)
        if exact is not None:
            return replacements.get(exact.group("name"), value)

        return _DISPATCH_TEMPLATE_PATTERN.sub(
            lambda match: str(replacements.get(match.group(0)[1:], match.group(0))),
            value,
        )
    if isinstance(value, Mapping):
        return {
            key: resolve_dispatch_templates(
                item,
                first_argument=first_argument,
                cwd=resolved_cwd,
                template_values=replacements,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_dispatch_templates(
                item,
                first_argument=first_argument,
                cwd=resolved_cwd,
                template_values=replacements,
            )
            for item in value
        ]
    return value


def _reconstruct_prompt_from_parsed_command(parsed: ParsedOooCommand) -> str:
    """Build a canonical prompt when a caller only has parsed command data."""
    if parsed.remainder is None:
        return parsed.command_prefix
    separator = "\n" if "\n" in parsed.remainder or "\r" in parsed.remainder else " "
    return f"{parsed.command_prefix}{separator}{parsed.remainder}"


def _validate_parsed_command(parsed: ParsedOooCommand) -> str | None:
    """Return a deterministic validation error for non-canonical parsed commands."""
    if not isinstance(parsed.skill_name, str):
        return "malformed parsed command: skill_name must be a string"
    if _SKILL_IDENTIFIER_PATTERN.fullmatch(parsed.skill_name) is None:
        return "malformed parsed command: skill_name must be a valid skill identifier"
    if not isinstance(parsed.command_prefix, str):
        return "malformed parsed command: command_prefix must be a string"
    valid_prefixes = (f"ooo {parsed.skill_name}", f"/ouroboros:{parsed.skill_name}")
    if parsed.command_prefix not in valid_prefixes:
        return "malformed parsed command: command_prefix must match skill_name"
    if parsed.remainder is not None and not isinstance(parsed.remainder, str):
        return "malformed parsed command: remainder must be a string or null"
    return None


def resolve_parsed_skill_dispatch(
    parsed: ParsedOooCommand,
    *,
    prompt: str | None = None,
    cwd: str | Path = "",
    skills_dir: str | Path | None = None,
) -> ResolveResult:
    """Resolve parsed skill command data to runtime-neutral dispatch metadata.

    ``parsed`` must be the immutable command object returned by
    :func:`parse_ooo_command`. This function performs the complete deterministic
    resolve step from a known command identifier to canonical skill target,
    validated MCP dispatch metadata, and resolved templates. It performs no
    logging and never invokes MCP handlers.
    """
    parsed_validation_error = _validate_parsed_command(parsed)
    if parsed_validation_error is not None:
        return InvalidSkill(
            reason=parsed_validation_error,
            skill_path=Path(parsed.skill_name if isinstance(parsed.skill_name, str) else ""),
            category=InvalidInputReason.MALFORMED_PARSED_COMMAND,
        )

    resolved_skill_path: Path | None = None
    try:
        with packaged_skill_dispatch_registry(skills_dir=skills_dir) as registry:
            target = registry.resolve(parsed.skill_name)
            if isinstance(target, NotHandled):
                return target

            resolved_skill_path = target.skill_path
            if skills_dir is None:
                resolved_skill_path = _cache_packaged_skill_entrypoint(
                    target.skill_name,
                    target.skill_path,
                )
            frontmatter = load_skill_frontmatter(resolved_skill_path)
    except FileNotFoundError:
        return NotHandled(reason="skill not found", category=NoMatchReason.SKILL_NOT_FOUND)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        skill_path = resolved_skill_path or Path(parsed.skill_name)
        return InvalidSkill(
            reason=str(exc),
            skill_path=skill_path,
            category=InvalidInputReason.FRONTMATTER_LOAD_ERROR,
        )

    normalized, validation_error = normalize_mcp_frontmatter(frontmatter)
    if normalized is None:
        return InvalidSkill(
            reason=validation_error or "invalid MCP frontmatter",
            skill_path=resolved_skill_path,
        )

    first_argument = extract_first_argument(parsed.remainder)
    mcp_tool, mcp_args = normalized
    try:
        template_values = _extract_dispatch_template_values(
            parsed.remainder,
            first_argument=first_argument,
            cwd=cwd,
        )
        resolved_mcp_args = resolve_dispatch_templates(
            mcp_args,
            first_argument=first_argument,
            cwd=cwd,
            template_values=template_values,
        )
    except Exception as exc:
        return InvalidSkill(
            reason=f"template resolution failed: {str(exc) or type(exc).__name__}",
            skill_path=resolved_skill_path,
            category=InvalidInputReason.TEMPLATE_RESOLUTION_ERROR,
        )

    return Resolved(
        skill_name=target.skill_name,
        command_prefix=parsed.command_prefix,
        prompt=prompt if prompt is not None else _reconstruct_prompt_from_parsed_command(parsed),
        skill_path=resolved_skill_path,
        mcp_tool=mcp_tool,
        mcp_args=resolved_mcp_args,
        first_argument=first_argument,
    )


class SkillDispatchRouter:
    """Stateless resolver for deterministic ``ooo`` skill dispatch.

    Instances carry no mutable state; constructing one is optional because
    :func:`resolve_skill_dispatch` creates an instance for single-call use.
    """

    def resolve(
        self,
        request: ResolveRequest | ParsedOooCommand | str,
        *,
        skills_dir: str | Path | None = None,
        cwd: str | Path | None = None,
    ) -> ResolveResult:
        """Resolve caller input to one of the public dispatch result variants."""
        if isinstance(request, ResolveRequest):
            prompt = request.prompt
            effective_skills_dir = request.skills_dir
            effective_cwd = request.cwd
        elif isinstance(request, ParsedOooCommand):
            return resolve_parsed_skill_dispatch(
                request,
                cwd="" if cwd is None else cwd,
                skills_dir=skills_dir,
            )
        else:
            prompt = request
            effective_skills_dir = skills_dir
            effective_cwd = "" if cwd is None else cwd

        parsed = parse_ooo_command(prompt)
        if parsed is None:
            return NotHandled(
                reason="not a skill command",
                category=NoMatchReason.NOT_A_SKILL_COMMAND,
            )

        return resolve_parsed_skill_dispatch(
            parsed,
            prompt=prompt,
            cwd=effective_cwd,
            skills_dir=effective_skills_dir,
        )


def resolve_skill_dispatch(
    request: ResolveRequest | ParsedOooCommand | str,
    *,
    skills_dir: str | Path | None = None,
    cwd: str | Path | None = None,
) -> ResolveResult:
    """Resolve deterministic skill dispatch without instantiating a router.

    This is the intended entry point for Codex CLI, Hermes, and Opencode runtime
    adapters. Pass ``ResolveRequest(prompt=..., cwd=..., skills_dir=...)`` for
    explicit runtime context, pass a parsed command object from
    :func:`parse_ooo_command`, or pass a prompt string plus keyword arguments
    for direct tests and lightweight callers.
    """
    return SkillDispatchRouter().resolve(request, skills_dir=skills_dir, cwd=cwd)


__all__ = [
    "DispatchTarget",
    "DispatchTargetKind",
    "InvalidSkill",
    "InvalidInputReason",
    "MCPDispatchTarget",
    "MCPFrontmatterArgs",
    "MCPFrontmatterScalar",
    "MCPFrontmatterValue",
    "McpDispatchTarget",
    "NoMatchReason",
    "NotHandled",
    "NormalizedMCPFrontmatter",
    "NormalizedMcpFrontmatter",
    "ParsedOooCommand",
    "ResolveRequest",
    "ResolveOutcome",
    "ResolveResult",
    "Resolved",
    "RouterRequest",
    "SkillDispatchRouter",
    "extract_first_argument",
    "load_skill_frontmatter",
    "normalize_mcp_frontmatter",
    "parse_ooo_command",
    "resolve_dispatch_templates",
    "resolve_packaged_skill_path",
    "resolve_parsed_skill_dispatch",
    "resolve_skill_dispatch",
]
