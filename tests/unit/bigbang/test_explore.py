"""Unit tests for ouroboros.bigbang.explore.detect_brownfield."""

from pathlib import Path

from ouroboros.bigbang.explore import detect_brownfield


class TestDetectBrownfield:
    """Test detect_brownfield function."""

    def test_detect_brownfield_with_config_file(self, tmp_path: Path) -> None:
        """Returns True when a recognised config file exists."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_empty_dir(self, tmp_path: Path) -> None:
        """Returns False for an empty directory."""
        assert detect_brownfield(tmp_path) is False

    def test_detect_brownfield_nonexistent_path(self, tmp_path: Path) -> None:
        """Returns False for a path that does not exist."""
        assert detect_brownfield(tmp_path / "nonexistent") is False

    def test_detect_brownfield_with_package_json(self, tmp_path: Path) -> None:
        """Returns True for a JavaScript/TypeScript project."""
        (tmp_path / "package.json").write_text('{"name": "demo"}')
        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_with_go_mod(self, tmp_path: Path) -> None:
        """Returns True for a Go project."""
        (tmp_path / "go.mod").write_text("module example.com/demo\n")
        assert detect_brownfield(tmp_path) is True

    def test_detect_brownfield_string_path(self, tmp_path: Path) -> None:
        """Accepts string paths in addition to Path objects."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
        assert detect_brownfield(str(tmp_path)) is True
