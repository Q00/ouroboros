"""Ouroboros - Self-Improving AI Workflow System."""

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:
    """Main entry point for the Ouroboros CLI.

    This function invokes the Typer app from ouroboros.cli.main.
    """
    from ouroboros.cli.main import app

    app()
