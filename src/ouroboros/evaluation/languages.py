"""Mechanical command configuration built from ``.ouroboros/mechanical.toml``.

Ouroboros no longer ships hardcoded per-language presets. Instead,
``ouroboros.evaluation.detector`` runs a single AI call that inspects the
repo and writes ``.ouroboros/mechanical.toml`` with commands this project
can actually execute. This module is the deterministic reader for that
file: Stage 1 trusts the toml and nothing else.

Usage:
    config = build_mechanical_config(Path("/path/to/project"))
    verifier = MechanicalVerifier(config)

When the toml is absent, all commands resolve to ``None`` and Stage 1
skips gracefully rather than running the wrong tool.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
from typing import Any

import structlog

from ouroboros.evaluation.mechanical import MechanicalConfig

log = structlog.get_logger()


# Executables accepted in ``.ouroboros/mechanical.toml`` (authored by the AI
# detector or by hand). Any token outside this set is dropped before reaching
# ``create_subprocess_exec``. Curated to cover the common zero-cost gate
# tools — build runners, language package managers, and well-behaved linters
# / test runners — while refusing arbitrary shell utilities (``rm``, ``curl``,
# ``bash`` …) that could turn a Stage 1 run into remote code execution in CI.
_ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        # Python
        "python",
        "python3",
        "uv",
        "poetry",
        "pdm",
        "hatch",
        "pip",
        "ruff",
        "mypy",
        "pytest",
        "pyright",
        "black",
        "isort",
        "flake8",
        "pylint",
        "bandit",
        "tox",
        "nox",
        # Zig
        "zig",
        # Rust
        "cargo",
        "rustc",
        "clippy-driver",
        # Go
        "go",
        "golangci-lint",
        "gofmt",
        # Node.js
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "node",
        "deno",
        "tsc",
        "vitest",
        "jest",
        "mocha",
        "eslint",
        "biome",
        "prettier",
        # General build tools
        "make",
        "gmake",
        "cmake",
        "ninja",
        "bazel",
        "buck",
        "gradle",
        "gradlew",
        "gradlew.bat",
        "mvn",
        "mvnw",
        "mvnw.cmd",
        "ant",
        "just",
        "task",
        # Other languages
        "cabal",
        "stack",
        "ghc",
        "dotnet",
        "mix",
        "elixir",
        "swift",
        "swiftc",
        "xcodebuild",
        "javac",
        "java",
        "kotlinc",
        "clang",
        "clang-tidy",
        "clang-format",
        "gcc",
        "g++",
        "shellcheck",
        # Ruby
        "ruby",
        "bundle",
        "rake",
        "rspec",
        "rubocop",
        # PHP
        "php",
        "composer",
        "phpunit",
    }
)


def _load_project_overrides(working_dir: Path) -> dict[str, Any] | None:
    """Load ``.ouroboros/mechanical.toml`` if it exists.

    Returns the parsed TOML dict, or ``None`` when the file is missing or
    malformed. Never raises.
    """
    config_path = working_dir / ".ouroboros" / "mechanical.toml"
    if not config_path.exists():
        return None

    import tomllib

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        log.warning("mechanical.toml_parse_error", path=str(config_path), error=str(e))
        return None


def _parse_command(
    value: str,
    *,
    working_dir: Path | None = None,
) -> tuple[str, ...] | None:
    """Parse a command string into a tuple, or ``None`` if empty/blocked.

    Guarantees:

    * Empty / whitespace-only inputs mean "skip this check".
    * Malformed quoting (unterminated string literals etc.) is swallowed
      rather than raised — Stage 1 must never crash on repo-authored toml.
    * Shell operators (``&&``, pipes, redirects, command substitution) are
      refused so ``mechanical.toml`` cannot smuggle shell constructs past
      ``create_subprocess_exec``.
    * The leading executable is checked against the curated allowlist.
    * When ``working_dir`` is provided, the command is additionally fed
      through the detector's on-disk entry-point validator so hand-authored
      toml cannot bypass the same repo-coupling contract the AI path
      enforces (``npm install`` / ``cargo publish`` / ``/tmp/mvnw`` etc.
      are rejected).
    """
    value = value.strip()
    if not value:
        return None
    if any(token in value for token in ("&&", "||", "|", ";", ">", "<", "`", "$(")):
        log.warning("mechanical.toml_shell_operator_blocked", command=value)
        return None
    try:
        parts = tuple(shlex.split(value, posix=(os.name != "nt")))
    except ValueError as exc:
        log.warning("mechanical.toml_parse_error", command=value, error=str(exc))
        return None
    if not parts:
        return None
    head = parts[0]
    # Reject absolute paths and other non-allowlisted forms. Basename-only
    # allowlisting still accepts project-local wrappers like ``./mvnw``
    # because ``Path("./mvnw").name == "mvnw"`` matches the allowlist.
    if head.startswith("/") or head.startswith("~"):
        log.warning("mechanical.toml_absolute_path_blocked", command=value)
        return None
    executable = Path(head).name
    if executable not in _ALLOWED_EXECUTABLES:
        log.warning(
            "mechanical.blocked_executable",
            executable=executable,
            command=value,
            hint="Add to _ALLOWED_EXECUTABLES or revise mechanical.toml",
        )
        return None
    if working_dir is not None:
        # Lazy import to avoid a circular dep: detector imports
        # ``_ALLOWED_EXECUTABLES`` from this module.
        from ouroboros.evaluation.detector import _command_is_valid

        if not _command_is_valid(working_dir, value):
            log.warning(
                "mechanical.toml_entry_point_invalid",
                command=value,
                working_dir=str(working_dir),
            )
            return None
    return parts


def _apply_overrides(
    current: dict[str, Any],
    source: dict[str, Any],
    *,
    working_dir: Path | None = None,
) -> None:
    """Merge ``source`` command/threshold entries into ``current``.

    ``working_dir`` is forwarded to :func:`_parse_command` so file-loaded
    overrides (``.ouroboros/mechanical.toml``) get the same entry-point
    validation the detector applies; explicit caller-provided overrides
    pass ``None`` because they are trusted MCP inputs that have already
    been vetted upstream.
    """
    for key in ("lint", "build", "test", "static", "coverage"):
        if key in source:
            current[key] = _parse_command(str(source[key]), working_dir=working_dir)
    if "timeout" in source:
        try:
            current["timeout"] = int(source["timeout"])
        except (TypeError, ValueError):
            log.warning("mechanical.toml_bad_timeout", value=source["timeout"])
    if "coverage_threshold" in source:
        try:
            current["coverage_threshold"] = float(source["coverage_threshold"])
        except (TypeError, ValueError):
            log.warning(
                "mechanical.toml_bad_coverage_threshold",
                value=source["coverage_threshold"],
            )


def build_mechanical_config(
    working_dir: Path,
    overrides: dict[str, Any] | None = None,
) -> MechanicalConfig:
    """Build a ``MechanicalConfig`` from ``mechanical.toml`` plus overrides.

    Priority (highest first):
        1. Explicit ``overrides`` dict (MCP caller)
        2. ``.ouroboros/mechanical.toml`` authored by the detector or user
        3. All commands ``None`` → Stage 1 skips every check

    Args:
        working_dir: Project root directory.
        overrides: Optional dict of command overrides.

    Returns:
        Deterministic ``MechanicalConfig`` with ``working_dir`` set.
    """
    current: dict[str, Any] = {
        "lint": None,
        "build": None,
        "test": None,
        "static": None,
        "coverage": None,
        "timeout": 300,
        "coverage_threshold": 0.7,
    }

    file_overrides = _load_project_overrides(working_dir)
    if file_overrides:
        _apply_overrides(current, file_overrides, working_dir=working_dir)

    if overrides:
        _apply_overrides(current, overrides)

    return MechanicalConfig(
        lint_command=current["lint"],
        build_command=current["build"],
        test_command=current["test"],
        static_command=current["static"],
        coverage_command=current["coverage"],
        timeout_seconds=current["timeout"],
        coverage_threshold=current["coverage_threshold"],
        working_dir=working_dir,
    )


__all__ = ["build_mechanical_config"]
