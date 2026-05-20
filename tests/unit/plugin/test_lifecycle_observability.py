"""Wave 1 PR E coverage for ``on_error`` / ``on_cancel`` observability hooks.

Promoted out of the deferred bucket by Q00/ouroboros#1131 (refs #939). These
v0.3-additive lifecycle hooks observe terminal plugin-wrapper outcomes only
and must satisfy a strict contract:

* the hook name is accepted by the v0.3 manifest schema and the manifest
  loader without bumping ``plugin_schema_version`` to ``0.4``;
* the hook MUST declare a lifecycle permission and the manifest enforces
  ``fail_open`` because terminal observability cannot mask the original
  cause;
* the firewall dispatches them after the terminal ``plugin.failed`` event,
  through the same ``plugin.hook.invoked`` / ``plugin.hook.completed``
  audit path used by ``before_invocation`` / ``after_invocation``;
* a fail-open hook failure emits ``plugin.hook.failed`` but the
  ``InvocationResult`` and the terminal ``plugin.failed`` event preserve the
  original error/cancel cause.

The v0.3 conformance baseline (#1119) is checked separately — these tests
only assert the new hook contract.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from ouroboros.plugin.digest import canonical_tree_hash
from ouroboros.plugin.firewall import invoke_plugin
from ouroboros.plugin.hooks import (
    HOOK_BLOCKED_EVENT,
    HOOK_COMPLETED_EVENT,
    HOOK_FAILED_EVENT,
    HOOK_INVOKED_EVENT,
    HOOK_LIFECYCLE_READ_SCOPE,
    TERMINAL_OBSERVABILITY_HOOK_NAMES,
    HookKind,
)
from ouroboros.plugin.manifest import (
    PluginManifestError,
    load_manifest,
)
from ouroboros.plugin.trust_store import TrustStore
from ouroboros.plugin.userlevel_registry import UserLevelProgramRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_REFERENCE_MANIFEST: dict = {
    "schema_version": "0.3",
    "name": "github-pr-ops",
    "version": "0.1.0",
    "source": {"type": "local_path", "path": "plugins/github-pr-ops"},
    "commands": [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Review a pull request.",
            "usage": "ooo github-pr review <pull-request-url>",
            "risk": "read_only",
        }
    ],
    "capabilities": [{"name": "ledger", "access": "write"}],
    "permissions": [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {
            "scope": HOOK_LIFECYCLE_READ_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Allow v1 lifecycle hook observation.",
        },
    ],
    "entrypoint": {"type": "command", "command": "python -m fake_plugin"},
}


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _payload_with_hooks(*hook_names: str) -> dict:
    payload = json.loads(json.dumps(_REFERENCE_MANIFEST))
    payload["hooks"] = [
        {
            "name": name,
            "entrypoint": {
                "type": "command",
                "command": f"python -m hook_{name}",
            },
            "permissions": [HOOK_LIFECYCLE_READ_SCOPE],
            "failure_policy": "fail_open",
        }
        for name in hook_names
    ]
    return payload


def _register(tmp_path: Path, payload: dict):
    manifest = load_manifest(_write_manifest(tmp_path, payload))
    registry = UserLevelProgramRegistry()
    return registry.register(manifest)


def _grant_full_trust(tmp_path: Path):
    trust_store = TrustStore(root=tmp_path / "trust")
    trust = trust_store.grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    trust = trust_store.grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope=HOOK_LIFECYCLE_READ_SCOPE,
        granted_by="user:test",
    )
    return trust


# ---------------------------------------------------------------------------
# Manifest / schema contract
# ---------------------------------------------------------------------------


class TestManifestContract:
    """The v0.3 manifest must accept on_error/on_cancel with fail_open only."""

    def test_terminal_observability_hook_names_match_hookkind(self) -> None:
        # Sanity: the runtime contract names exactly the hooks promoted by
        # PR #1131 and they are inside the v1 HookKind vocabulary.
        assert frozenset({"on_error", "on_cancel"}) == TERMINAL_OBSERVABILITY_HOOK_NAMES
        for name in TERMINAL_OBSERVABILITY_HOOK_NAMES:
            assert name in {kind.value for kind in HookKind}

    @pytest.mark.parametrize("hook_name", sorted(TERMINAL_OBSERVABILITY_HOOK_NAMES))
    def test_v03_manifest_accepts_observability_hook(self, tmp_path: Path, hook_name: str) -> None:
        payload = _payload_with_hooks(hook_name)
        manifest = load_manifest(_write_manifest(tmp_path, payload))
        # No plugin_schema_version bump — the hooks are additive to v0.3.
        assert manifest.schema_version == "0.3"
        names = {hook.name for hook in manifest.hooks}
        assert names == {hook_name}

    @pytest.mark.parametrize("hook_name", sorted(TERMINAL_OBSERVABILITY_HOOK_NAMES))
    def test_fail_closed_observability_hook_rejected(self, tmp_path: Path, hook_name: str) -> None:
        payload = _payload_with_hooks(hook_name)
        payload["hooks"][0]["failure_policy"] = "fail_closed"

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write_manifest(tmp_path, payload))

        # Schema- or loader-layer rejection both point at failure_policy.
        assert exc_info.value.json_pointer == "/hooks/0/failure_policy"

    @pytest.mark.parametrize("hook_name", sorted(TERMINAL_OBSERVABILITY_HOOK_NAMES))
    def test_observability_hook_requires_lifecycle_permission(
        self, tmp_path: Path, hook_name: str
    ) -> None:
        payload = _payload_with_hooks(hook_name)
        payload["hooks"][0]["permissions"] = []

        with pytest.raises(PluginManifestError) as exc_info:
            load_manifest(_write_manifest(tmp_path, payload))

        assert exc_info.value.json_pointer == "/hooks/0/permissions"


# ---------------------------------------------------------------------------
# Firewall dispatch for on_error
# ---------------------------------------------------------------------------


def _runner_factory(
    hook_returncode: int = 0,
    hook_raises: type[BaseException] | None = None,
    *,
    command_returncode: int = 0,
):
    """Build a subprocess.run stand-in distinguishing hook vs command."""

    def runner(argv, *args, **kwargs):  # noqa: ARG001
        if argv[:2] == ["python", "-m"] and argv[2].startswith("hook_"):
            if hook_raises is not None:
                raise hook_raises(argv[0])  # type: ignore[call-arg]
            return subprocess.CompletedProcess(
                args=argv,
                returncode=hook_returncode,
                stdout=b"raw hook stdout",
                stderr=b"raw hook stderr",
            )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=command_returncode,
            stdout=b"raw command stdout",
            stderr=b"raw command stderr",
        )

    return runner


class TestOnErrorDispatch:
    """``on_error`` runs after the terminal ``plugin.failed`` event."""

    def test_nonzero_exit_dispatches_on_error_after_terminal_failed(self, tmp_path: Path) -> None:
        program = _register(tmp_path, _payload_with_hooks("on_error"))
        trust = _grant_full_trust(tmp_path)

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-error-nonzero",
            subprocess_runner=_runner_factory(command_returncode=42),
        )

        # Terminal status preserves the original non-zero exit cause.
        assert result.status == "failed"
        assert result.exit_code == 42

        names = [event["event_type"] for event in events]
        # plugin.hook.invoked / plugin.hook.completed for on_error must
        # appear AFTER the terminal plugin.failed event.
        terminal_index = names.index("plugin.failed")
        hook_indices = [i for i, n in enumerate(names) if n.startswith("plugin.hook.")]
        assert hook_indices, "on_error must emit hook audit events"
        assert all(i > terminal_index for i in hook_indices)

        # Bounded payload: raw stdout/stderr from the command never lands in
        # any persisted audit event.
        serialized = json.dumps(events)
        assert "raw command stdout" not in serialized
        assert "raw command stderr" not in serialized
        assert "raw hook stdout" not in serialized

    def test_nonzero_exit_dispatches_after_invocation_before_on_error(self, tmp_path: Path) -> None:
        program = _register(tmp_path, _payload_with_hooks("after_invocation", "on_error"))
        trust = _grant_full_trust(tmp_path)

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-error-after-order",
            subprocess_runner=_runner_factory(command_returncode=42),
        )

        assert result.status == "failed"

        terminal_index = next(
            index for index, event in enumerate(events) if event["event_type"] == "plugin.failed"
        )
        after_indices = [
            index
            for index, event in enumerate(events)
            if event["event_type"].startswith("plugin.hook.")
            and event["provenance"]["hook_name"] == "after_invocation"
        ]
        on_error_indices = [
            index
            for index, event in enumerate(events)
            if event["event_type"].startswith("plugin.hook.")
            and event["provenance"]["hook_name"] == "on_error"
        ]

        assert after_indices
        assert on_error_indices
        assert terminal_index < min(after_indices)
        assert max(after_indices) < min(on_error_indices)

    def test_timeout_dispatches_after_invocation_before_on_error(self, tmp_path: Path) -> None:
        program = _register(tmp_path, _payload_with_hooks("after_invocation", "on_error"))
        trust = _grant_full_trust(tmp_path)

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            if argv[:2] == ["python", "-m"] and argv[2].startswith("hook_"):
                return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=1, output=b"partial stdout", stderr=b"partial stderr"
            )

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-error-timeout-order",
            subprocess_runner=runner,
        )

        assert result.status == "failed"
        assert result.exit_code == 124
        assert result.stdout_bytes == b"partial stdout"
        assert result.stderr_bytes == b"partial stderr"

        terminal_index = next(
            index for index, event in enumerate(events) if event["event_type"] == "plugin.failed"
        )
        after_indices = [
            index
            for index, event in enumerate(events)
            if event["event_type"].startswith("plugin.hook.")
            and event["provenance"]["hook_name"] == "after_invocation"
        ]
        on_error_indices = [
            index
            for index, event in enumerate(events)
            if event["event_type"].startswith("plugin.hook.")
            and event["provenance"]["hook_name"] == "on_error"
        ]

        assert after_indices
        assert on_error_indices
        assert terminal_index < min(after_indices)
        assert max(after_indices) < min(on_error_indices)

    @pytest.mark.parametrize("exc_type", [OSError, PermissionError])
    def test_launch_oserror_dispatches_after_invocation_before_on_error(
        self, tmp_path: Path, exc_type: type[OSError]
    ) -> None:
        program = _register(tmp_path, _payload_with_hooks("after_invocation", "on_error"))
        trust = _grant_full_trust(tmp_path)

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            if argv[:2] == ["python", "-m"] and argv[2].startswith("hook_"):
                return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")
            raise exc_type("launch denied")

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id=f"corr-on-error-{exc_type.__name__.lower()}-order",
            subprocess_runner=runner,
        )

        assert result.status == "failed"
        assert result.exit_code == 126
        assert f"entrypoint failed to start: {exc_type.__name__}" in result.message

        terminal_index = next(
            index for index, event in enumerate(events) if event["event_type"] == "plugin.failed"
        )
        after_indices = [
            index
            for index, event in enumerate(events)
            if event["event_type"].startswith("plugin.hook.")
            and event["provenance"]["hook_name"] == "after_invocation"
        ]
        on_error_indices = [
            index
            for index, event in enumerate(events)
            if event["event_type"].startswith("plugin.hook.")
            and event["provenance"]["hook_name"] == "on_error"
        ]

        assert after_indices
        assert on_error_indices
        assert terminal_index < min(after_indices)
        assert max(after_indices) < min(on_error_indices)
        assert events[terminal_index]["result"]["message"] == result.message

    def test_no_on_error_hook_declared_does_not_emit_hook_events(self, tmp_path: Path) -> None:
        # Conformance baseline: no on_error/on_cancel hooks declared →
        # no plugin.hook.* events for the failure path.
        program = _register(tmp_path, json.loads(json.dumps(_REFERENCE_MANIFEST)))
        trust = _grant_full_trust(tmp_path)

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-error-absent",
            subprocess_runner=_runner_factory(command_returncode=2),
        )

        assert result.status == "failed"
        types = {event["event_type"] for event in events}
        assert not any(name.startswith("plugin.hook.") for name in types)

    def test_on_error_hook_failure_does_not_mask_original_error(self, tmp_path: Path) -> None:
        """Fail-open: hook subprocess returning non-zero must NOT change
        the terminal plugin.failed cause or the InvocationResult."""

        program = _register(tmp_path, _payload_with_hooks("on_error"))
        trust = _grant_full_trust(tmp_path)

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-error-hook-fail",
            subprocess_runner=_runner_factory(hook_returncode=7, command_returncode=42),
        )

        assert result.status == "failed"
        assert result.exit_code == 42
        assert "exited with code 42" in result.message

        # The terminal plugin.failed event records the original cause.
        terminal_failed = [event for event in events if event["event_type"] == "plugin.failed"]
        assert terminal_failed
        assert terminal_failed[-1]["result"]["message"] == result.message

        # The hook failure surfaces as a separate plugin.hook.failed
        # audit event (isolated from the original cause).
        hook_failed = [event for event in events if event["event_type"] == HOOK_FAILED_EVENT]
        assert hook_failed, "fail_open hook non-zero exit must emit plugin.hook.failed"
        assert hook_failed[0]["result"]["status"] == "failed"


# ---------------------------------------------------------------------------
# Firewall dispatch for on_cancel
# ---------------------------------------------------------------------------


class TestOnCancelDispatch:
    """``on_cancel`` runs after a cancellation signal forces plugin.failed."""

    def test_cancel_disabled_plugin_does_not_dispatch_on_cancel(self, tmp_path: Path) -> None:
        program = _register(tmp_path, _payload_with_hooks("on_cancel"))
        trust = _grant_full_trust(tmp_path)
        command_calls: list[list[str]] = []

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            command_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-cancel-disabled",
            subprocess_runner=runner,
            cancellation_requested=True,
            is_disabled=True,
        )

        assert result.status == "blocked"
        assert "disabled" in result.message
        assert command_calls == []
        assert [event["event_type"] for event in events] == ["plugin.failed"]
        assert not any(event["event_type"].startswith("plugin.hook.") for event in events)

    def test_cancel_untrusted_plugin_does_not_dispatch_on_cancel(self, tmp_path: Path) -> None:
        program = _register(tmp_path, _payload_with_hooks("on_cancel"))
        command_calls: list[list[str]] = []

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            command_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=None,
            event_sink=events.append,
            correlation_id="corr-on-cancel-untrusted",
            subprocess_runner=runner,
            cancellation_requested=True,
        )

        assert result.status == "blocked"
        assert "not yet trusted" in result.message
        assert command_calls == []
        assert [event["event_type"] for event in events] == ["plugin.failed"]
        assert not any(event["event_type"].startswith("plugin.hook.") for event in events)

    def test_cancel_tampered_plugin_home_does_not_dispatch_on_cancel(self, tmp_path: Path) -> None:
        program = _register(tmp_path, _payload_with_hooks("on_cancel"))
        trust = _grant_full_trust(tmp_path)
        plugin_home = tmp_path / "plugin-home"
        plugin_home.mkdir()
        (plugin_home / "module.py").write_text("print('before')\n")
        expected_digest = canonical_tree_hash(plugin_home)
        (plugin_home / "module.py").write_text("print('after')\n")
        command_calls: list[list[str]] = []

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            command_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-cancel-tampered",
            subprocess_runner=runner,
            plugin_home=plugin_home,
            expected_artifact_digest=expected_digest,
            cancellation_requested=True,
        )

        assert result.status == "blocked"
        assert "bytes have changed" in result.message
        assert command_calls == []
        assert [event["event_type"] for event in events] == ["plugin.failed"]
        assert not any(event["event_type"].startswith("plugin.hook.") for event in events)

    def test_cancellation_signal_short_circuits_and_dispatches_on_cancel(
        self, tmp_path: Path
    ) -> None:
        program = _register(tmp_path, _payload_with_hooks("on_cancel"))
        trust = _grant_full_trust(tmp_path)
        command_calls: list[list[str]] = []

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            command_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-cancel-signal",
            subprocess_runner=runner,
            cancellation_requested=True,
        )

        # The original cancel cause reaches the caller in the
        # InvocationResult and the terminal plugin.failed event.
        assert result.status == "failed"
        assert "cancelled" in result.message

        names = [event["event_type"] for event in events]
        # The cancel branch must not launch hook_before nor the command,
        # and must not run on_error.
        assert names[0] == "plugin.failed"
        assert all(call[2] == "hook_on_cancel" for call in command_calls)
        assert "plugin.invoked" not in names
        assert HOOK_INVOKED_EVENT in names
        # Order: plugin.failed → on_cancel hook events.
        assert names[1:] == [HOOK_INVOKED_EVENT, HOOK_COMPLETED_EVENT]

        terminal_failed = events[0]
        assert terminal_failed["result"]["message"] == result.message
        assert terminal_failed["provenance"]["reason"] == "cancelled"

    def test_on_cancel_hook_failure_does_not_mask_cancellation(self, tmp_path: Path) -> None:
        """Fail-open: hook failure during cancel branch never changes the
        terminal cancel cause or the InvocationResult."""

        program = _register(tmp_path, _payload_with_hooks("on_cancel"))
        trust = _grant_full_trust(tmp_path)

        def runner(argv, *args, **kwargs):  # noqa: ARG001
            if argv[:2] == ["python", "-m"] and argv[2] == "hook_on_cancel":
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=11,
                    stdout=b"raw hook stdout",
                    stderr=b"raw hook stderr",
                )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name="review",
            argv=["https://example.com/pr/1"],
            trust_record=trust,
            event_sink=events.append,
            correlation_id="corr-on-cancel-hook-fail",
            subprocess_runner=runner,
            cancellation_requested=True,
        )

        # Original cancel cause preserved end-to-end.
        assert result.status == "failed"
        assert "cancelled" in result.message
        assert events[0]["event_type"] == "plugin.failed"
        assert events[0]["provenance"]["reason"] == "cancelled"

        # Hook-internal failure is isolated into plugin.hook.failed and
        # does NOT mutate the terminal plugin.failed cause.
        hook_failed = [event for event in events if event["event_type"] == HOOK_FAILED_EVENT]
        assert hook_failed, "fail_open hook non-zero exit must emit plugin.hook.failed"
        # The terminal plugin.failed event is exactly one and was emitted
        # before the hook failure event — the hook cannot rewrite history.
        plugin_failed = [event for event in events if event["event_type"] == "plugin.failed"]
        assert len(plugin_failed) == 1
        assert events.index(plugin_failed[0]) < events.index(hook_failed[0])

        # Bounded payload: raw stdout/stderr never lands in any audit event.
        serialized = json.dumps(events)
        assert "raw hook stdout" not in serialized
        assert "raw hook stderr" not in serialized

    def test_blocked_hook_event_constant_export(self) -> None:
        # Cross-check the audit event names referenced by the contract
        # tests so future renames cannot drift the dispatch path.
        assert HOOK_INVOKED_EVENT == "plugin.hook.invoked"
        assert HOOK_COMPLETED_EVENT == "plugin.hook.completed"
        assert HOOK_BLOCKED_EVENT == "plugin.hook.blocked"
        assert HOOK_FAILED_EVENT == "plugin.hook.failed"
