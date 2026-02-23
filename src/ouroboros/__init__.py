"""Ouroboros - Self-Improving AI Workflow System.

A workflow system that uses Socratic questioning and ontological analysis
to transform ambiguous requirements into executable specifications.

Example:
    # Using CLI
    ouroboros init start "I want to build a task management CLI"
    ouroboros run workflow seed.yaml

    # Using Python
    from ouroboros.core import Result, ValidationError
    from ouroboros.bigbang import InterviewEngine
"""

__version__ = "0.13.2"

__all__ = ["__version__", "main"]


def main() -> None:
    """Main entry point for the Ouroboros CLI.

    This function invokes the Typer app from ouroboros.cli.main.
    """
    from ouroboros.cli.main import app

    app()
