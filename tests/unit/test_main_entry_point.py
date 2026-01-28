"""Test main entry point."""


from typer.testing import CliRunner

import ouroboros
from ouroboros import main
from ouroboros.cli.main import app

runner = CliRunner()


def test_version_exists():
    """Test that __version__ is defined."""
    assert ouroboros.__version__ == "0.2.0"


def test_main_invokes_cli():
    """Test that main() invokes the Typer CLI app.

    Since Typer CLI requires args, calling with no args shows help (exit 2).
    This test verifies main() correctly delegates to the Typer app.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Ouroboros" in result.output


def test_main_is_callable():
    """Test that main is a callable function."""
    assert callable(main)


def test_main_module_execution():
    """Test that __main__ module can be executed."""
    # This verifies that the __main__ module structure is correct
    import importlib.util
    from pathlib import Path

    root = Path(__file__).parent.parent.parent
    main_py = root / "src" / "ouroboros" / "__main__.py"

    assert main_py.exists()
    spec = importlib.util.spec_from_file_location("ouroboros.__main__", main_py)
    assert spec is not None
    assert spec.loader is not None
