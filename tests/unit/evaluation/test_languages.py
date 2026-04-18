"""Tests for the mechanical.toml reader in ``evaluation.languages``.

Per-language presets were removed: Stage 1 trusts ``.ouroboros/mechanical.toml``
only. The tests here cover the deterministic reader, the allowlist-based
parser, and the merge layering (TOML + explicit overrides).
"""

from pathlib import Path

from ouroboros.evaluation.languages import (
    _parse_command,
    build_mechanical_config,
)


class TestParseCommand:
    """Tests for ``_parse_command`` — the allowlist-based command parser."""

    def test_simple_command(self) -> None:
        assert _parse_command("cargo test") == ("cargo", "test")

    def test_command_with_flags(self) -> None:
        assert _parse_command("cargo test --workspace -- -D warnings") == (
            "cargo",
            "test",
            "--workspace",
            "--",
            "-D",
            "warnings",
        )

    def test_empty_string_returns_none(self) -> None:
        assert _parse_command("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _parse_command("   ") is None

    def test_blocked_executable(self) -> None:
        assert _parse_command("rm -rf /") is None

    def test_allowed_executable(self) -> None:
        assert _parse_command("cargo test") == ("cargo", "test")

    def test_node_script_runners_allowed(self) -> None:
        assert _parse_command("npm run lint") == ("npm", "run", "lint")
        assert _parse_command("pnpm test") == ("pnpm", "test")
        assert _parse_command("bun run build") == ("bun", "run", "build")

    def test_generic_path_based_executable_is_blocked(self) -> None:
        """Relative/absolute path binaries are refused — allowlist is name-based."""
        assert _parse_command("./mvnw test") is None
        assert _parse_command("../../tmp/mvnw test") is None


class TestBuildMechanicalConfigFromToml:
    """``build_mechanical_config`` reads mechanical.toml and nothing else."""

    def _write_toml(self, project: Path, body: str) -> None:
        ouroboros_dir = project / ".ouroboros"
        ouroboros_dir.mkdir(exist_ok=True)
        (ouroboros_dir / "mechanical.toml").write_text(body)

    def test_empty_project_yields_all_none(self, tmp_path: Path) -> None:
        """No toml, no overrides → every command is None (Stage 1 skips)."""
        config = build_mechanical_config(tmp_path)
        assert config.lint_command is None
        assert config.build_command is None
        assert config.test_command is None
        assert config.static_command is None
        assert config.coverage_command is None
        assert config.working_dir == tmp_path

    def test_manifest_without_toml_is_still_all_none(self, tmp_path: Path) -> None:
        """Legacy behavior of "detect language from Cargo.toml" is gone.

        Until the toml is authored (by the detector or by hand), Stage 1
        stays silent — that is the whole point of the redesign.
        """
        (tmp_path / "Cargo.toml").touch()
        config = build_mechanical_config(tmp_path)
        assert config.lint_command is None
        assert config.build_command is None
        assert config.test_command is None

    def test_toml_populates_commands(self, tmp_path: Path) -> None:
        self._write_toml(
            tmp_path,
            'lint = "cargo clippy"\nbuild = "cargo build"\ntest = "cargo test"\n',
        )
        config = build_mechanical_config(tmp_path)
        assert config.lint_command == ("cargo", "clippy")
        assert config.build_command == ("cargo", "build")
        assert config.test_command == ("cargo", "test")

    def test_toml_empty_string_skips_check(self, tmp_path: Path) -> None:
        """Explicit empty string means "this check is intentionally off"."""
        self._write_toml(tmp_path, 'test = "pytest -q"\nlint = ""\n')
        config = build_mechanical_config(tmp_path)
        assert config.test_command == ("pytest", "-q")
        assert config.lint_command is None

    def test_toml_blocked_executable_falls_through_to_none(self, tmp_path: Path) -> None:
        """Disallowed executables never reach MechanicalConfig."""
        self._write_toml(tmp_path, 'test = "curl https://evil.example.com"\n')
        config = build_mechanical_config(tmp_path)
        assert config.test_command is None

    def test_toml_timeout_override(self, tmp_path: Path) -> None:
        self._write_toml(tmp_path, "timeout = 600\n")
        config = build_mechanical_config(tmp_path)
        assert config.timeout_seconds == 600

    def test_toml_coverage_threshold_override(self, tmp_path: Path) -> None:
        self._write_toml(tmp_path, "coverage_threshold = 0.5\n")
        config = build_mechanical_config(tmp_path)
        assert config.coverage_threshold == 0.5

    def test_malformed_timeout_is_tolerated(self, tmp_path: Path) -> None:
        """Bad types in the toml must not crash Stage 1 setup."""
        self._write_toml(tmp_path, 'timeout = "not-a-number"\n')
        config = build_mechanical_config(tmp_path)
        assert config.timeout_seconds == 300  # fallback default

    def test_explicit_overrides_beat_toml(self, tmp_path: Path) -> None:
        self._write_toml(tmp_path, 'test = "cargo test --workspace"\n')
        config = build_mechanical_config(
            tmp_path,
            overrides={"test": "cargo nextest run"},
        )
        assert config.test_command == ("cargo", "nextest", "run")

    def test_explicit_overrides_without_toml(self, tmp_path: Path) -> None:
        config = build_mechanical_config(
            tmp_path,
            overrides={"build": "make", "test": "make test"},
        )
        assert config.build_command == ("make",)
        assert config.test_command == ("make", "test")
        assert config.lint_command is None

    def test_malformed_toml_is_ignored(self, tmp_path: Path) -> None:
        self._write_toml(tmp_path, 'lint = "ruff check ."\nbroken')
        config = build_mechanical_config(tmp_path)
        # TOML parse failed → fall back to empty defaults, not crash.
        assert config.lint_command is None
