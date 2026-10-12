"""The generated release launcher must not float or capture user launchers."""

from importlib import metadata
from unittest.mock import patch

from ouroboros.cli.codex_mcp_launcher import (
    is_setup_owned_codex_uvx_args,
    release_codex_mcp_launcher,
)

ARGS = (
    "--isolated",
    "--python",
    ">=3.12",
    "--from",
    "ouroboros-ai[mcp]",
    "ouroboros",
    "mcp",
    "serve",
)


def launcher():
    return release_codex_mcp_launcher(
        which=lambda name: "/bin/uvx" if name == "uvx" else None,
        run=lambda *_args, **_kwargs: None,
        executable="/not-an-interpreter",
        find_spec=lambda _name: None,
        uvx_args=ARGS,
        direct_args=("mcp", "serve"),
        module_args=("-m", "ouroboros", "mcp", "serve"),
    )


def test_release_launcher_pins_the_installed_setup_version():
    with patch("ouroboros.cli.codex_mcp_launcher.metadata.version", return_value="0.51.15"):
        command, args = launcher()
    assert command == "/bin/uvx"
    assert args == [*ARGS[:4], "ouroboros-ai[mcp]==0.51.15", *ARGS[5:]]


def test_missing_metadata_does_not_silently_generate_a_floating_launcher():
    with patch(
        "ouroboros.cli.codex_mcp_launcher.metadata.version",
        side_effect=metadata.PackageNotFoundError("ouroboros-ai"),
    ):
        assert launcher() is None


def test_source_only_version_cannot_be_published_as_a_release_pin():
    with patch("ouroboros.cli.codex_mcp_launcher.metadata.version", return_value="0+unknown"):
        assert launcher() is None


def test_historical_floating_entry_remains_setup_owned():
    assert is_setup_owned_codex_uvx_args(ARGS, (ARGS,), has_managed_comment=False)


def test_marked_prior_release_pin_can_refresh_without_becoming_user_owned():
    pinned = [*ARGS[:4], "ouroboros-ai[mcp]==0.51.14", *ARGS[5:]]
    assert is_setup_owned_codex_uvx_args(pinned, (ARGS,), has_managed_comment=True)
    assert not is_setup_owned_codex_uvx_args(pinned, (ARGS,), has_managed_comment=False)


def test_managed_comment_does_not_claim_a_fork_range_or_extra_argument():
    for requirement in ["/opt/private/ouroboros-fork", "ouroboros-ai[mcp]>=0.51.14"]:
        args = [*ARGS[:4], requirement, *ARGS[5:]]
        assert not is_setup_owned_codex_uvx_args(args, (ARGS,), has_managed_comment=True)
    pinned = [*ARGS[:4], "ouroboros-ai[mcp]==0.51.14", *ARGS[5:], "--port", "9999"]
    assert not is_setup_owned_codex_uvx_args(pinned, (ARGS,), has_managed_comment=True)
