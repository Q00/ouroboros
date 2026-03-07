"""Language detection and preset commands for mechanical verification.

Auto-detects project language from marker files and provides appropriate
build/lint/test commands. Supports project-level overrides via
.ouroboros/mechanical.toml.

Usage:
    config = build_mechanical_config(Path("/path/to/project"))
    verifier = MechanicalVerifier(config)
"""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ouroboros.evaluation.mechanical import MechanicalConfig


@dataclass(frozen=True, slots=True)
class LanguagePreset:
    """Command preset for a detected project language.

    Attributes:
        name: Language/toolchain identifier (e.g. "python-uv", "zig", "rust")
        lint_command: Linting command, or None to skip
        build_command: Build/compile command, or None to skip
        test_command: Test runner command, or None to skip
        static_command: Static analysis command, or None to skip
        coverage_command: Coverage command, or None to skip
    """

    name: str
    lint_command: tuple[str, ...] | None = None
    build_command: tuple[str, ...] | None = None
    test_command: tuple[str, ...] | None = None
    static_command: tuple[str, ...] | None = None
    coverage_command: tuple[str, ...] | None = None


LANGUAGE_PRESETS: dict[str, LanguagePreset] = {
    "python-uv": LanguagePreset(
        name="python-uv",
        lint_command=("uv", "run", "ruff", "check", "."),
        build_command=("uv", "run", "python", "-m", "py_compile"),
        test_command=("uv", "run", "pytest", "--tb=short", "-q"),
        static_command=(
            "uv",
            "run",
            "mypy",
            ".",
            "--ignore-missing-imports",
        ),
        coverage_command=(
            "uv",
            "run",
            "pytest",
            "--cov",
            "--cov-report=term-missing",
            "-q",
        ),
    ),
    "python": LanguagePreset(
        name="python",
        lint_command=("ruff", "check", "."),
        build_command=("python", "-m", "py_compile"),
        test_command=("pytest", "--tb=short", "-q"),
        static_command=("mypy", ".", "--ignore-missing-imports"),
        coverage_command=(
            "pytest",
            "--cov",
            "--cov-report=term-missing",
            "-q",
        ),
    ),
    "zig": LanguagePreset(
        name="zig",
        build_command=("zig", "build"),
        test_command=("zig", "build", "test"),
    ),
    "rust": LanguagePreset(
        name="rust",
        lint_command=("cargo", "clippy"),
        build_command=("cargo", "build"),
        test_command=("cargo", "test"),
    ),
    "go": LanguagePreset(
        name="go",
        lint_command=("go", "vet", "./..."),
        build_command=("go", "build", "./..."),
        test_command=("go", "test", "./..."),
        coverage_command=("go", "test", "-cover", "./..."),
    ),
    "node-npm": LanguagePreset(
        name="node-npm",
        lint_command=("npm", "run", "lint"),
        build_command=("npm", "run", "build"),
        test_command=("npm", "test"),
    ),
    "node-pnpm": LanguagePreset(
        name="node-pnpm",
        lint_command=("pnpm", "lint"),
        build_command=("pnpm", "build"),
        test_command=("pnpm", "test"),
    ),
    "node-bun": LanguagePreset(
        name="node-bun",
        lint_command=("bun", "lint"),
        build_command=("bun", "run", "build"),
        test_command=("bun", "test"),
    ),
    "node-yarn": LanguagePreset(
        name="node-yarn",
        lint_command=("yarn", "lint"),
        build_command=("yarn", "build"),
        test_command=("yarn", "test"),
    ),
}

# Ordered list of (marker_file, preset_key) for detection priority.
# More specific markers come first (e.g. uv.lock before pyproject.toml).
_DETECTION_RULES: list[tuple[str, str]] = [
    # Python with uv (most specific Python marker)
    ("uv.lock", "python-uv"),
    # Zig
    ("build.zig", "zig"),
    # Rust
    ("Cargo.toml", "rust"),
    # Go
    ("go.mod", "go"),
    # Node.js package managers (check lockfiles before generic package.json)
    ("bun.lockb", "node-bun"),
    ("bun.lock", "node-bun"),
    ("pnpm-lock.yaml", "node-pnpm"),
    ("yarn.lock", "node-yarn"),
    ("package-lock.json", "node-npm"),
    # Generic Python (after uv, before generic Node)
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    # Generic Node (no lockfile found)
    ("package.json", "node-npm"),
]


def detect_language(working_dir: Path) -> LanguagePreset | None:
    """Detect project language from marker files in working_dir.

    Checks for known project files in priority order. Returns the first
    matching preset, or None if no language is detected.

    Args:
        working_dir: Project root directory to scan

    Returns:
        LanguagePreset for the detected language, or None
    """
    for marker_file, preset_key in _DETECTION_RULES:
        if (working_dir / marker_file).exists():
            return LANGUAGE_PRESETS[preset_key]
    return None


def _load_project_overrides(working_dir: Path) -> dict[str, Any] | None:
    """Load .ouroboros/mechanical.toml if it exists.

    Args:
        working_dir: Project root directory

    Returns:
        Parsed TOML dict, or None if file doesn't exist
    """
    config_path = working_dir / ".ouroboros" / "mechanical.toml"
    if not config_path.exists():
        return None

    import tomllib

    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _parse_command(value: str) -> tuple[str, ...] | None:
    """Parse a command string into a tuple, or None if empty.

    Args:
        value: Shell command string (e.g. "cargo test --workspace")
               Empty string means "skip this check"

    Returns:
        Tuple of command parts, or None to skip
    """
    value = value.strip()
    if not value:
        return None
    return tuple(shlex.split(value))


def build_mechanical_config(
    working_dir: Path,
    overrides: dict[str, Any] | None = None,
) -> MechanicalConfig:
    """Build a MechanicalConfig by combining auto-detection with overrides.

    Priority (highest to lowest):
    1. Explicit overrides dict (from caller)
    2. .ouroboros/mechanical.toml in project
    3. Auto-detected language preset
    4. All commands None (all checks skip gracefully)

    Args:
        working_dir: Project root directory
        overrides: Optional dict of command overrides

    Returns:
        MechanicalConfig with resolved commands and working_dir set
    """
    # Start with auto-detected preset
    preset = detect_language(working_dir)

    # Base command values from preset (or all None)
    lint = preset.lint_command if preset else None
    build = preset.build_command if preset else None
    test = preset.test_command if preset else None
    static = preset.static_command if preset else None
    coverage = preset.coverage_command if preset else None
    timeout = 300
    coverage_threshold = 0.7

    # Layer on .ouroboros/mechanical.toml
    file_overrides = _load_project_overrides(working_dir)
    if file_overrides:
        if "lint" in file_overrides:
            lint = _parse_command(str(file_overrides["lint"]))
        if "build" in file_overrides:
            build = _parse_command(str(file_overrides["build"]))
        if "test" in file_overrides:
            test = _parse_command(str(file_overrides["test"]))
        if "static" in file_overrides:
            static = _parse_command(str(file_overrides["static"]))
        if "coverage" in file_overrides:
            coverage = _parse_command(str(file_overrides["coverage"]))
        if "timeout" in file_overrides:
            timeout = int(file_overrides["timeout"])
        if "coverage_threshold" in file_overrides:
            coverage_threshold = float(file_overrides["coverage_threshold"])

    # Layer on explicit overrides (from caller / MCP params)
    if overrides:
        if "lint" in overrides:
            lint = _parse_command(str(overrides["lint"]))
        if "build" in overrides:
            build = _parse_command(str(overrides["build"]))
        if "test" in overrides:
            test = _parse_command(str(overrides["test"]))
        if "static" in overrides:
            static = _parse_command(str(overrides["static"]))
        if "coverage" in overrides:
            coverage = _parse_command(str(overrides["coverage"]))
        if "timeout" in overrides:
            timeout = int(overrides["timeout"])
        if "coverage_threshold" in overrides:
            coverage_threshold = float(overrides["coverage_threshold"])

    return MechanicalConfig(
        lint_command=lint,
        build_command=build,
        test_command=test,
        static_command=static,
        coverage_command=coverage,
        timeout_seconds=timeout,
        coverage_threshold=coverage_threshold,
        working_dir=working_dir,
    )
