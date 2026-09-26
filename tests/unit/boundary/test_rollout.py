"""Randomized default assignment for the check package (boundary/rollout.py)."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from ouroboros import telemetry
from ouroboros.boundary.rollout import (
    CHECK_PACKAGE_EXPERIMENT_KEY,
    Arm,
    AssignmentSource,
    randomized_arm,
    resolve_check_package_assignment,
)
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key

INSTALL_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def _resolve(
    cli: bool | None = None,
    *,
    env: str | None = None,
    configured: str | None = None,
    identity: str | None = None,
):
    environ = {} if env is None else {"OUROBOROS_CHECK_PACKAGE": env}
    return resolve_check_package_assignment(
        cli, configured=configured, environ=environ, identity_reader=lambda: identity
    )


def test_randomized_arm_is_deterministic_and_roughly_even() -> None:
    assert randomized_arm(INSTALL_ID) is randomized_arm(INSTALL_ID)
    ids = [str(uuid.UUID(int=index * 7919 + 1)) for index in range(4000)]
    share_on = sum(randomized_arm(item) is Arm.ON for item in ids) / len(ids)
    assert 0.46 < share_on < 0.54


def test_randomized_arm_respects_the_fraction_and_the_experiment_key() -> None:
    assert randomized_arm(INSTALL_ID, on_percent=0) is Arm.OFF
    assert randomized_arm(INSTALL_ID, on_percent=100) is Arm.ON
    ids = [str(uuid.UUID(int=index * 104729 + 3)) for index in range(400)]
    same = sum(
        randomized_arm(item)
        is randomized_arm(item, experiment_key=f"{CHECK_PACKAGE_EXPERIMENT_KEY}.x")
        for item in ids
    )
    # A different key re-randomizes: agreement is near chance, not total.
    assert 150 < same < 250


@pytest.mark.parametrize(
    ("kwargs", "arm", "source"),
    [
        (
            {"cli": True, "env": "off", "configured": "off", "identity": INSTALL_ID},
            "on",
            "user_forced_on",
        ),
        (
            {"cli": False, "env": "on", "configured": "on", "identity": INSTALL_ID},
            "off",
            "user_forced_off",
        ),
        ({"env": "on", "configured": "off"}, "on", "user_forced_on"),
        ({"env": "off", "configured": "on", "identity": INSTALL_ID}, "off", "user_forced_off"),
        ({"env": "garbage", "configured": "on"}, "on", "user_forced_on"),
        ({"configured": "off", "identity": INSTALL_ID}, "off", "user_forced_off"),
        ({"configured": "on"}, "on", "user_forced_on"),
        ({}, "off", "fallback"),
        ({"env": ""}, "off", "fallback"),
    ],
)
def test_explicit_settings_override_the_assignment(kwargs: dict, arm: str, source: str) -> None:
    assignment = _resolve(kwargs.pop("cli", None), **kwargs)
    assert (assignment.arm.value, assignment.source.value) == (arm, source)


def test_unset_switch_with_an_eligible_identity_is_randomized() -> None:
    assignment = _resolve(identity=INSTALL_ID)
    assert assignment.source is AssignmentSource.RANDOMIZED
    assert assignment.arm is randomized_arm(INSTALL_ID)


def test_identity_reader_failure_falls_back_to_off() -> None:
    def broken() -> str | None:
        raise OSError("unreadable")

    assignment = resolve_check_package_assignment(
        None, configured=None, environ={}, identity_reader=broken
    )
    assert (assignment.arm, assignment.source) == (Arm.OFF, AssignmentSource.FALLBACK)


def test_project_env_cannot_force_the_arm() -> None:
    assert is_untrusted_env_denied_key("OUROBOROS_CHECK_PACKAGE")


class TestRolloutIdentity:
    @pytest.fixture(autouse=True)
    def _telemetry_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
        monkeypatch.delenv("OUROBOROS_TELEMETRY", raising=False)
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        telemetry._reset_for_tests()
        yield
        telemetry._reset_for_tests()

    def _write_state(self, tmp_path: Path, **fields: object) -> Path:
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "telemetry.json"
        path.write_text(json.dumps({"distinct_id": INSTALL_ID, **fields}), encoding="utf-8")
        return path

    def test_disabled_telemetry_has_no_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._write_state(tmp_path, notice_shown=True, notice_version=telemetry._NOTICE_VERSION)
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
        assert telemetry.rollout_identity() is None

    def test_missing_state_is_not_created(self, tmp_path: Path) -> None:
        assert telemetry.rollout_identity() is None
        assert not (tmp_path / ".ouroboros" / "telemetry.json").exists()

    def test_identity_requires_the_current_notice(self, tmp_path: Path) -> None:
        path = self._write_state(tmp_path, notice_shown=True)
        before = path.read_text(encoding="utf-8")
        assert telemetry.rollout_identity() is None
        assert path.read_text(encoding="utf-8") == before  # read-only: no repair write
        self._write_state(tmp_path, notice_shown=True, notice_version=telemetry._NOTICE_VERSION)
        assert telemetry.rollout_identity() == INSTALL_ID
        self._write_state(tmp_path, notice_shown=False, notice_version=telemetry._NOTICE_VERSION)
        assert telemetry.rollout_identity() is None
