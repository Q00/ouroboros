"""What the console's overview, health and runs views report (#3046).

No agent CLI runs: the Connect cards are stubbed where the overview needs them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.gui import api


def _state() -> api.ConsoleState:
    return api.ConsoleState(stop=lambda *_args, **_kwargs: None)


@pytest.fixture
def no_client_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.clients_mod, "list_clients", lambda **_kwargs: [])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_ISOLATED_UVX = {
    "mcpServers": {
        "ouroboros": {
            "command": "uvx",
            "args": [
                "--isolated",
                "--python",
                ">=3.12",
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
                "--runtime",
                "claude-cli",
                "--llm-backend",
                "claude_code",
            ],
        }
    }
}


def _claude_plugin(home: Path, *, manifest: str | None, launcher: bool) -> None:
    root = home / ".claude" / "plugins"
    if manifest is not None:
        _write(
            root / "marketplaces" / "ouroboros" / ".claude-plugin" / "plugin.json",
            json.dumps({"version": manifest}),
        )
    if launcher:
        _write(
            root / "cache" / "ouroboros" / "ouroboros" / "0.1.0" / ".mcp.json",
            json.dumps(_ISOLATED_UVX),
        )


# ── health: optional integrations ────────────────────────────────────


def test_a_fresh_install_has_no_warnings_only_unset_optional_integrations(
    isolated_home: Path, no_client_probes: None
) -> None:
    surfaces = api.get_doctor(_state(), query={}, body=None)["surfaces"]
    overview = api.get_overview(_state(), query={}, body=None)

    unset = {surface["name"] for surface in surfaces if surface["optional"]}
    assert unset == {
        "codex_personal_plugin",
        "codex_personal_plugin_launcher",
        "claude_plugin_marketplace",
        "claude_plugin_launcher",
        "zcode_bridge_plugin",
        "zcode_bridge_launcher",
    }
    # The doctor's own verdict is kept as it reported it.
    assert all(surface["status"] == "warn" for surface in surfaces if surface["optional"])
    assert overview["doctor"] == {"pass": 2, "warn": 0, "fail": 0, "optional": 6}


def test_a_stale_plugin_stays_a_warning_and_its_unset_neighbours_do_not(
    isolated_home: Path, no_client_probes: None
) -> None:
    _claude_plugin(isolated_home, manifest="0.0.1", launcher=True)

    surfaces = {s["name"]: s for s in api.get_doctor(_state(), query={}, body=None)["surfaces"]}
    overview = api.get_overview(_state(), query={}, body=None)

    assert surfaces["claude_plugin_marketplace"]["status"] == "warn"
    assert surfaces["claude_plugin_marketplace"]["optional"] is False
    assert surfaces["claude_plugin_launcher"]["optional"] is False
    assert surfaces["codex_personal_plugin"]["optional"] is True
    assert overview["doctor"]["warn"] == 1
    assert overview["doctor"]["optional"] == 4


def test_a_half_installed_integration_is_a_warning_on_every_missing_part(
    isolated_home: Path, no_client_probes: None
) -> None:
    import ouroboros

    _claude_plugin(isolated_home, manifest=ouroboros.__version__, launcher=False)

    surfaces = {s["name"]: s for s in api.get_doctor(_state(), query={}, body=None)["surfaces"]}

    launcher = surfaces["claude_plugin_launcher"]
    assert (launcher["status"], launcher["optional"]) == ("warn", False)
    assert api.get_overview(_state(), query={}, body=None)["doctor"]["warn"] == 1


def test_a_folder_that_exists_but_is_empty_is_not_an_unset_integration(
    isolated_home: Path, no_client_probes: None
) -> None:
    (isolated_home / ".claude" / "plugins" / "cache" / "ouroboros" / "ouroboros").mkdir(
        parents=True
    )

    surfaces = {s["name"]: s for s in api.get_doctor(_state(), query={}, body=None)["surfaces"]}

    assert surfaces["claude_plugin_launcher"]["status"] == "warn"
    assert surfaces["claude_plugin_launcher"]["optional"] is False
    assert surfaces["claude_plugin_marketplace"]["optional"] is False


def test_failures_are_never_counted_as_optional(
    isolated_home: Path, no_client_probes: None
) -> None:
    _write(isolated_home / "plugins" / "ouroboros" / ".codex-plugin" / "plugin.json", "not json")

    surfaces = {s["name"]: s for s in api.get_doctor(_state(), query={}, body=None)["surfaces"]}

    assert surfaces["codex_personal_plugin"]["status"] == "fail"
    assert surfaces["codex_personal_plugin"]["optional"] is False
    assert surfaces["codex_personal_plugin_launcher"]["optional"] is False
    assert api.get_overview(_state(), query={}, body=None)["doctor"]["fail"] == 1


# ── paths: the home folder and symlinked roots ───────────────────────


def test_the_overview_names_the_home_and_only_links_that_exist(
    isolated_home: Path, no_client_probes: None
) -> None:
    overview = api.get_overview(_state(), query={}, body=None)

    assert overview["home"] == str(isolated_home)
    aliases = dict(overview["path_aliases"])
    for link in ("/tmp", "/var", "/etc"):
        target = Path(link)
        if target.is_symlink():
            assert aliases[str(target.resolve())] == link
        else:
            assert link not in aliases.values()
    lengths = [len(real) for real, _shown in overview["path_aliases"]]
    assert lengths == sorted(lengths, reverse=True)


def test_a_home_reached_through_a_link_maps_back_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_client_probes: None
) -> None:
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link))

    paths = api._display_paths()

    assert paths["home"] == str(link)
    assert [str(real.resolve()), str(link)] in paths["path_aliases"]


# ── runs: a refused read is said, not shown as no runs ───────────────


def test_runs_that_cannot_be_read_are_reported_not_hidden(
    monkeypatch: pytest.MonkeyPatch, no_client_probes: None
) -> None:
    from ouroboros.dashboard_web import reader

    def refuse(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise reader.PickerIndexContractError(frozenset(), detail="contains unprojected events")

    monkeypatch.setattr(reader, "list_recent_executions", refuse)

    runs = api.get_runs(_state(), query={}, body=None)
    overview = api.get_overview(_state(), query={}, body=None)

    assert runs["runs"] == []
    assert runs["runs_error"] == "dashboard picker projection contains unprojected events"
    assert overview["runs_error"] == runs["runs_error"]


def test_readable_runs_carry_no_error(isolated_home: Path, no_client_probes: None) -> None:
    assert not (isolated_home / ".ouroboros" / "ouroboros.db").exists()
    runs = api.get_runs(_state(), query={}, body=None)
    assert (runs["runs"], runs["runs_error"]) == ([], None)
