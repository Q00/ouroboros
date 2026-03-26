"""Tests for language detection and mechanical config building."""

from pathlib import Path

from ouroboros.evaluation.languages import (
    _parse_command,
    build_mechanical_config,
    detect_language,
)


class TestDetectLanguage:
    """Tests for detect_language()."""

    def test_detect_zig(self, tmp_path: Path) -> None:
        (tmp_path / "build.zig").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "zig"

    def test_detect_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "rust"

    def test_detect_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "go"

    def test_detect_python_uv(self, tmp_path: Path) -> None:
        (tmp_path / "uv.lock").touch()
        (tmp_path / "pyproject.toml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "python-uv"

    def test_detect_python_generic(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "python"

    def test_detect_python_setup_py(self, tmp_path: Path) -> None:
        (tmp_path / "setup.py").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "python"

    def test_detect_java_maven(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "java-maven"
        assert preset.build_command == ("mvn", "clean", "compile")
        assert preset.test_command == ("mvn", "test")

    def test_detect_java_maven_wrapper_does_not_change_preset(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").touch()
        (tmp_path / "mvnw").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "java-maven"
        assert preset.build_command == ("mvn", "clean", "compile")
        assert preset.test_command == ("mvn", "test")

    def test_detect_node_npm(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").touch()
        (tmp_path / "package-lock.json").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "node-npm"

    def test_detect_node_pnpm(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").touch()
        (tmp_path / "pnpm-lock.yaml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "node-pnpm"

    def test_detect_node_bun(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").touch()
        (tmp_path / "bun.lockb").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "node-bun"

    def test_detect_node_yarn(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").touch()
        (tmp_path / "yarn.lock").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "node-yarn"

    def test_detect_node_generic(self, tmp_path: Path) -> None:
        """package.json without a lockfile defaults to npm."""
        (tmp_path / "package.json").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "node-npm"

    def test_detect_unknown(self, tmp_path: Path) -> None:
        """Empty directory returns None."""
        preset = detect_language(tmp_path)
        assert preset is None

    def test_pom_xml_detected_before_package_json(self, tmp_path: Path) -> None:
        """pom.xml takes priority over package.json (Maven before Node)."""
        (tmp_path / "pom.xml").touch()
        (tmp_path / "package.json").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "java-maven"

    def test_pom_xml_detected_before_node_lockfiles(self, tmp_path: Path) -> None:
        """pom.xml takes priority over Node lockfiles (Maven before Node)."""
        for lockfile in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"):
            d = tmp_path / lockfile.replace(".", "_")
            d.mkdir()
            (d / "pom.xml").touch()
            (d / lockfile).touch()
            preset = detect_language(d)
            assert preset is not None, f"Failed for {lockfile}"
            assert preset.name == "java-maven", f"Expected java-maven over {lockfile}"

    def test_go_mod_detected_before_pom_xml(self, tmp_path: Path) -> None:
        """go.mod takes priority over pom.xml (Go before Maven)."""
        (tmp_path / "go.mod").touch()
        (tmp_path / "pom.xml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "go"

    def test_uv_takes_priority_over_pyproject(self, tmp_path: Path) -> None:
        """uv.lock is checked before pyproject.toml."""
        (tmp_path / "uv.lock").touch()
        (tmp_path / "pyproject.toml").touch()
        preset = detect_language(tmp_path)
        assert preset is not None
        assert preset.name == "python-uv"


class TestParseCommand:
    """Tests for _parse_command()."""

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

    def test_quoted_arguments(self) -> None:
        assert _parse_command('echo "hello world"', trusted=True) == ("echo", "hello world")

    def test_blocked_executable(self) -> None:
        assert _parse_command("rm -rf /") is None

    def test_allowed_executable(self) -> None:
        assert _parse_command("cargo test") == ("cargo", "test")

    def test_path_based_maven_wrapper_override_is_blocked(self) -> None:
        assert _parse_command("./mvnw test") is None

    def test_path_traversal_maven_wrapper_override_is_blocked(self) -> None:
        assert _parse_command("../../tmp/mvnw test") is None


class TestBuildMechanicalConfig:
    """Tests for build_mechanical_config()."""

    def test_auto_detect_zig(self, tmp_path: Path) -> None:
        (tmp_path / "build.zig").touch()
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("zig", "build")
        assert config.test_command == ("zig", "build", "test")
        assert config.lint_command is None
        assert config.static_command is None
        assert config.coverage_command is None
        assert config.working_dir == tmp_path

    def test_auto_detect_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").touch()
        config = build_mechanical_config(tmp_path)
        assert config.lint_command == ("cargo", "clippy")
        assert config.build_command == ("cargo", "build")
        assert config.test_command == ("cargo", "test")

    def test_unknown_language_all_none(self, tmp_path: Path) -> None:
        """Unknown project type results in all commands None (all checks skip)."""
        config = build_mechanical_config(tmp_path)
        assert config.lint_command is None
        assert config.build_command is None
        assert config.test_command is None
        assert config.static_command is None
        assert config.coverage_command is None
        assert config.working_dir == tmp_path

    def test_toml_override(self, tmp_path: Path) -> None:
        """TOML file overrides auto-detected commands."""
        (tmp_path / "Cargo.toml").touch()
        ouroboros_dir = tmp_path / ".ouroboros"
        ouroboros_dir.mkdir()
        (ouroboros_dir / "mechanical.toml").write_text(
            'test = "cargo test --workspace"\nlint = ""\n'  # skip lint
        )
        config = build_mechanical_config(tmp_path)
        assert config.test_command == ("cargo", "test", "--workspace")
        assert config.lint_command is None  # skipped via empty string
        assert config.build_command == ("cargo", "build")  # preserved from preset

    def test_toml_override_timeout(self, tmp_path: Path) -> None:
        ouroboros_dir = tmp_path / ".ouroboros"
        ouroboros_dir.mkdir()
        (ouroboros_dir / "mechanical.toml").write_text("timeout = 600\n")
        config = build_mechanical_config(tmp_path)
        assert config.timeout_seconds == 600

    def test_toml_override_coverage_threshold(self, tmp_path: Path) -> None:
        ouroboros_dir = tmp_path / ".ouroboros"
        ouroboros_dir.mkdir()
        (ouroboros_dir / "mechanical.toml").write_text("coverage_threshold = 0.5\n")
        config = build_mechanical_config(tmp_path)
        assert config.coverage_threshold == 0.5

    def test_explicit_overrides_beat_toml(self, tmp_path: Path) -> None:
        """Caller overrides take highest priority."""
        (tmp_path / "Cargo.toml").touch()
        ouroboros_dir = tmp_path / ".ouroboros"
        ouroboros_dir.mkdir()
        (ouroboros_dir / "mechanical.toml").write_text('test = "cargo test --workspace"\n')
        config = build_mechanical_config(
            tmp_path,
            overrides={"test": "cargo nextest run"},
        )
        assert config.test_command == ("cargo", "nextest", "run")

    def test_explicit_overrides_without_detection(self, tmp_path: Path) -> None:
        """Overrides work even when no language is detected."""
        config = build_mechanical_config(
            tmp_path,
            overrides={"build": "make", "test": "make test"},
        )
        assert config.build_command == ("make",)
        assert config.test_command == ("make", "test")
        assert config.lint_command is None

    def test_auto_detect_java_maven(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").touch()
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("mvn", "clean", "compile")
        assert config.test_command == ("mvn", "test")
        assert config.lint_command is None
        assert config.static_command is None
        assert config.coverage_command is None
        assert config.working_dir == tmp_path

    def test_auto_detect_java_maven_prefers_executable_wrapper(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").touch()
        wrapper = tmp_path / "mvnw"
        wrapper.touch()
        wrapper.chmod(0o755)
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("./mvnw", "clean", "compile")
        assert config.test_command == ("./mvnw", "test")

    def test_auto_detect_java_maven_falls_back_when_wrapper_not_executable(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pom.xml").touch()
        wrapper = tmp_path / "mvnw"
        wrapper.touch()
        wrapper.chmod(0o644)
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("mvn", "clean", "compile")
        assert config.test_command == ("mvn", "test")

    def test_auto_detect_java_maven_uses_windows_wrapper(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "pom.xml").touch()
        (tmp_path / "mvnw.cmd").touch()
        monkeypatch.setattr("ouroboros.evaluation.languages.os.name", "nt")
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("mvnw.cmd", "clean", "compile")
        assert config.test_command == ("mvnw.cmd", "test")

    def test_auto_detect_java_maven_falls_back_when_wrapper_is_directory(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pom.xml").touch()
        wrapper = tmp_path / "mvnw"
        wrapper.mkdir()
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("mvn", "clean", "compile")
        assert config.test_command == ("mvn", "test")

    def test_auto_detect_java_maven_falls_back_when_windows_wrapper_is_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        (tmp_path / "pom.xml").touch()
        wrapper = tmp_path / "mvnw.cmd"
        wrapper.mkdir()
        monkeypatch.setattr("ouroboros.evaluation.languages.os.name", "nt")
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("mvn", "clean", "compile")
        assert config.test_command == ("mvn", "test")

    def test_no_toml_file_no_error(self, tmp_path: Path) -> None:
        """Missing .ouroboros/mechanical.toml is not an error."""
        (tmp_path / "build.zig").touch()
        config = build_mechanical_config(tmp_path)
        assert config.build_command == ("zig", "build")


class TestLanguagePresetCommands:
    """Verify preset commands are reasonable for each language."""

    def test_python_uv_preset_has_all_commands(self) -> None:
        from ouroboros.evaluation.languages import LANGUAGE_PRESETS

        preset = LANGUAGE_PRESETS["python-uv"]
        assert preset.lint_command is not None
        assert preset.build_command is not None
        assert preset.test_command is not None
        assert preset.static_command is not None
        assert preset.coverage_command is not None

    def test_zig_preset_has_build_and_test(self) -> None:
        from ouroboros.evaluation.languages import LANGUAGE_PRESETS

        preset = LANGUAGE_PRESETS["zig"]
        assert preset.build_command is not None
        assert preset.test_command is not None
        assert preset.lint_command is None
        assert preset.static_command is None

    def test_go_preset_has_lint_build_test_coverage(self) -> None:
        from ouroboros.evaluation.languages import LANGUAGE_PRESETS

        preset = LANGUAGE_PRESETS["go"]
        assert preset.lint_command is not None
        assert preset.build_command is not None
        assert preset.test_command is not None
        assert preset.coverage_command is not None

    def test_java_maven_preset_has_build_and_test_only(self) -> None:
        from ouroboros.evaluation.languages import LANGUAGE_PRESETS

        preset = LANGUAGE_PRESETS["java-maven"]
        assert preset.name == "java-maven"
        assert preset.build_command == ("mvn", "clean", "compile")
        assert preset.test_command == ("mvn", "test")
        assert preset.lint_command is None
        assert preset.static_command is None
        assert preset.coverage_command is None

    def test_java_maven_preset_no_quiet_flags(self) -> None:
        """Maven commands must not include quiet flags (-q or --quiet)."""
        from ouroboros.evaluation.languages import LANGUAGE_PRESETS

        preset = LANGUAGE_PRESETS["java-maven"]
        for cmd in (preset.build_command, preset.test_command):
            assert cmd is not None
            assert "-q" not in cmd
            assert "--quiet" not in cmd

    def test_java_maven_preset_is_frozen(self) -> None:
        """java-maven preset is immutable (frozen dataclass)."""
        from ouroboros.evaluation.languages import LANGUAGE_PRESETS

        preset = LANGUAGE_PRESETS["java-maven"]
        import pytest

        with pytest.raises(AttributeError):
            preset.name = "modified"  # type: ignore[misc]
