"""Contract tests for documented CLI command strings.

User-facing documentation (skills, README, CLAUDE.md, in-code re-attach
guidance) routinely prints runnable command strings. When a command string
drifts from the installed argparse/typer tree — either the subcommand
vanishes, is renamed, or an option is removed — users follow stale guidance
into "unknown command" errors.

These tests walk the command strings we actually print to users and assert
each one parses cleanly under the installed CLI tree. We rely on ``--help``
to prove syntactic acceptance without executing side effects (no DB writes,
no MCP spin-up, no seed file required).

Scope:

- Every command line emitted by ``resume._format_reattach_guidance``.
- Every ``ouroboros ...`` directive documented in ``skills/resume/SKILL.md``.
- The bare ``ouroboros`` entrypoints listed in the CLAUDE.md ``ooo`` table
  that correspond to a real CLI subcommand (plugin-only skills such as
  ``ooo welcome`` are not CLI commands and are skipped).

If you add a new documented command string, add it here too. The test is
deliberately strict: it ignores nothing, because the whole point is to catch
the kind of drift flagged in PR #433's bot review (the ``ooo`` skill
surfaces pointing at a placeholder ``status execution`` handler).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.resume import _format_reattach_guidance
from ouroboros.cli.main import app as root_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_runner = CliRunner()


def _make_tracker(
    session_id: str = "sess-abc123",
    execution_id: str | None = "exec-xyz789",
    seed_id: str | None = "seed-001",
) -> MagicMock:
    """Return a minimal SessionTracker-like mock for guidance rendering."""
    from ouroboros.orchestrator.session import SessionStatus

    tracker = MagicMock()
    tracker.session_id = session_id
    tracker.execution_id = execution_id
    tracker.seed_id = seed_id
    tracker.status = SessionStatus("running")
    from datetime import UTC, datetime

    tracker.start_time = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
    return tracker


def _assert_command_parses(argv: list[str]) -> None:
    """Invoke ``argv + ['--help']`` and assert Typer accepted the chain.

    Appending ``--help`` gives us a zero-side-effect proof that every
    subcommand in the chain exists and every option is recognised.
    """
    result = _runner.invoke(root_app, [*argv, "--help"])
    assert result.exit_code == 0, (
        f"documented command string `ouroboros {' '.join(argv)}` did not parse:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# 1) Commands printed by `resume._format_reattach_guidance`
# ---------------------------------------------------------------------------


class TestReattachGuidanceCommandsAreReal:
    """Every command string emitted by the re-attach panel must be real."""

    def test_inspect_command_parses(self) -> None:
        """``ouroboros tui monitor`` must be a real subcommand chain."""
        tracker = _make_tracker()
        output = _format_reattach_guidance(tracker)
        assert "ouroboros tui monitor" in output, (
            "inspect guidance must surface `ouroboros tui monitor`"
        )
        _assert_command_parses(["tui", "monitor"])

    def test_resume_command_parses(self) -> None:
        """The ``run workflow --orchestrator --resume`` chain must parse."""
        tracker = _make_tracker()
        output = _format_reattach_guidance(tracker)
        assert "ouroboros run workflow --orchestrator --resume" in output
        # --orchestrator and --resume must both be accepted by run workflow.
        result = _runner.invoke(root_app, ["run", "workflow", "--help"])
        assert result.exit_code == 0
        assert "--orchestrator" in result.output
        assert "--resume" in result.output

    def test_guidance_does_not_point_at_placeholder(self) -> None:
        """Must NOT surface ``ouroboros status execution`` (placeholder handler).

        ``src/ouroboros/cli/commands/status.py:execution`` only prints
        "Would show details for execution: …". Surfacing it misleads users
        into thinking they can inspect an execution when they can't.
        """
        tracker = _make_tracker()
        output = _format_reattach_guidance(tracker)
        assert "ouroboros status execution" not in output


# ---------------------------------------------------------------------------
# 2) Every `ouroboros ...` directive in skills/resume/SKILL.md
# ---------------------------------------------------------------------------


# NB: these are the *user-facing* directives intended to be run verbatim.
# Placeholder forms (with ``<angle brackets>``) are rewritten to concrete
# stubs so the argparse tree can actually see each token. The goal is to
# verify the subcommand chain + options, not the argument values.
SKILL_RESUME_COMMANDS: list[list[str]] = [
    # Primary command the skill tells users to run.
    ["resume"],
    # Inspect (read-only) path printed after session selection.
    ["tui", "monitor"],
    # Resume execution path printed after session selection.
    # --resume takes a session_id; the seed file is a positional arg.
    ["run", "workflow", "--orchestrator", "--resume", "sess-abc123", "seed.yaml"],
]


@pytest.mark.parametrize("argv", SKILL_RESUME_COMMANDS)
def test_skill_resume_commands_parse(argv: list[str]) -> None:
    """Every runnable ``ouroboros ...`` string in the resume skill parses."""
    # For the seed-file positional, ``--help`` suffices to verify the chain
    # accepts the shape; we don't need the file to exist.
    _assert_command_parses(argv)


# ---------------------------------------------------------------------------
# 3) Real CLI subcommands listed under the CLAUDE.md ooo table
# ---------------------------------------------------------------------------


# The CLAUDE.md ooo table maps `ooo <verb>` to a SKILL.md file. Most verbs
# are Claude-Code skills (no CLI equivalent — e.g. ``ooo welcome``). The
# ones below are verbs whose SKILL.md genuinely shells out to ``ouroboros
# <verb>``, so the CLI subcommand must exist. If a verb here disappears
# from the CLI but stays in CLAUDE.md, users following the skill will hit
# "No such command".
CLAUDE_MD_BACKED_BY_CLI: list[list[str]] = [
    ["run", "--help"],
    ["init", "--help"],
    ["config", "--help"],
    ["status", "--help"],
    ["cancel", "--help"],
    ["mcp", "--help"],
    ["setup", "--help"],
    ["tui", "--help"],
    ["pm", "--help"],
    ["resume", "--help"],
    ["uninstall", "--help"],
]


@pytest.mark.parametrize("argv", CLAUDE_MD_BACKED_BY_CLI)
def test_claude_md_cli_subcommands_exist(argv: list[str]) -> None:
    """Every ``ouroboros <verb>`` referenced from skill guidance must resolve."""
    result = _runner.invoke(root_app, argv)
    assert result.exit_code == 0, (
        f"CLAUDE.md-referenced CLI `ouroboros {' '.join(argv[:-1])}` is missing:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# 4) Cancel guidance (the resume skill's "Next Steps" block points at it)
# ---------------------------------------------------------------------------


def test_cancel_execution_subcommand_parses() -> None:
    """``ooo cancel execution <exec_id>`` resolves to real ``cancel execution``."""
    _assert_command_parses(["cancel", "execution"])
