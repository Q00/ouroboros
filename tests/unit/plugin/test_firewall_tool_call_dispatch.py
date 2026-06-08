"""Tests for #939 PR F-2 tool-call hook dispatch in the plugin firewall.

F-1 (#1277) reserved the ``before_tool_call`` / ``after_tool_call`` hook
kinds, the ``plugin:tool:intercept`` / ``plugin:tool:observe`` scopes, and
the four ``plugin.tool.*`` audit event names but left runtime dispatch a
no-op. These tests pin the dispatcher behavior specified by
``docs/rfc/plugin-tool-call-hook-contract.md`` (§3 payload, §5 failure
policy, §6 audit events).
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from ouroboros.plugin.firewall import (
    TOOL_CALL_ARGS_PREVIEW_LIMIT,
    TOOL_CALL_HOOK_PAYLOAD_ENV,
    dispatch_after_tool_call,
    dispatch_before_tool_call,
)
from ouroboros.plugin.hooks import (
    HOOK_FAILED_EVENT,
    HOOK_TOOL_INTERCEPT_BLOCKED_EVENT,
    HOOK_TOOL_INTERCEPT_COMPLETED_EVENT,
    HOOK_TOOL_INTERCEPT_REQUESTED_EVENT,
    HOOK_TOOL_OBSERVE_RECORDED_EVENT,
    HOOK_TOOL_OBSERVE_SCOPE,
)
from ouroboros.plugin.manifest import load_manifest
from tests.unit.plugin.test_manifest_schema_0_4 import _tool_call_hook, _v04_manifest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(returncode: int = 0, *, env_sink: list[dict] | None = None):
    """A fake subprocess runner returning a fixed return code."""

    def run(argv, **kwargs):
        if env_sink is not None:
            env_sink.append(kwargs.get("env", {}))
        return _FakeCompleted(returncode=returncode)

    return run


def _timeout_runner(argv, **kwargs):
    raise subprocess.TimeoutExpired(cmd=argv, timeout=5)


def _runner_must_not_execute(argv, **kwargs):
    raise AssertionError("unauthorized intercept hook subprocess was invoked")


def _manifest_with_hooks(tmp_path: Path, hooks: list[dict]):
    payload = _v04_manifest()
    payload["hooks"] = hooks
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return load_manifest(target)


def _types(events) -> list[str]:
    return [e["event_type"] for e in events]


def _noop_sink(event: dict) -> None:
    return None


_BEFORE_KW = {
    "tool": "github.create_pr",
    "args_digest": "sha256:abc123",
    "args_preview": '{"title": "hi"}',
    "correlation_id": "corr-1",
    "invocation_id": "inv-1",
}
_AFTER_KW = {
    "tool": "github.create_pr",
    "status": "success",
    "output_digest": "sha256:def456",
    "duration_ms": 42,
    "correlation_id": "corr-1",
    "invocation_id": "inv-1",
}


# ---------------------------------------------------------------------------
# before_tool_call — intercept (may veto)
# ---------------------------------------------------------------------------


class TestBeforeToolCallIntercept:
    def test_intercept_success_allows_and_emits_requested_then_completed(
        self, tmp_path: Path
    ) -> None:
        manifest = _manifest_with_hooks(
            tmp_path, [_tool_call_hook(name="before_tool_call", failure_policy="fail_closed")]
        )
        events: list[dict] = []
        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(0),
            **_BEFORE_KW,
        )
        assert decision.allowed is True
        assert decision.status == "allowed"
        assert _types(events) == [
            HOOK_TOOL_INTERCEPT_REQUESTED_EVENT,
            HOOK_TOOL_INTERCEPT_COMPLETED_EVENT,
        ]
        assert decision.events == tuple(events)

    def test_fail_closed_intercept_nonzero_blocks_the_call(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path, [_tool_call_hook(name="before_tool_call", failure_policy="fail_closed")]
        )
        events: list[dict] = []
        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(3),
            **_BEFORE_KW,
        )
        assert decision.allowed is False
        assert decision.status == "blocked"
        assert "exited with code 3" in decision.message
        assert _types(events) == [
            HOOK_TOOL_INTERCEPT_REQUESTED_EVENT,
            HOOK_TOOL_INTERCEPT_BLOCKED_EVENT,
        ]
        blocked = events[-1]
        assert blocked["result"]["status"] == "blocked"

    def test_fail_open_intercept_failure_does_not_block(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path,
            [_tool_call_hook(name="before_tool_call", failure_policy="fail_open")],
        )
        events: list[dict] = []
        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(1),
            **_BEFORE_KW,
        )
        assert decision.allowed is True
        # fail_open intercept failure is recorded via the shared hook.failed
        # event, NOT a parallel tool-specific failure event (RFC § 6).
        assert HOOK_FAILED_EVENT in _types(events)
        assert HOOK_TOOL_INTERCEPT_BLOCKED_EVENT not in _types(events)

    def test_fail_closed_intercept_timeout_blocks(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path, [_tool_call_hook(name="before_tool_call", failure_policy="fail_closed")]
        )
        events: list[dict] = []
        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_timeout_runner,
            **_BEFORE_KW,
        )
        assert decision.allowed is False
        assert "timed out" in decision.message
        assert _types(events)[-1] == HOOK_TOOL_INTERCEPT_BLOCKED_EVENT

    def test_unauthorized_intercept_blocks_before_subprocess_execution(
        self, tmp_path: Path
    ) -> None:
        payload = _v04_manifest()
        payload["permissions"].append(
            {
                "scope": "github:write",
                "risk": "write",
                "required": False,
                "reason": "Optional write access not granted by the trust gate.",
            }
        )
        payload["hooks"] = [
            _tool_call_hook(name="before_tool_call", failure_policy="fail_closed")
        ]
        target = tmp_path / "ouroboros.plugin.json"
        target.write_text(json.dumps(payload))
        manifest = load_manifest(target)
        events: list[dict] = []

        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner_must_not_execute,
            tool_permissions=["github:write"],
            **_BEFORE_KW,
        )

        assert decision.allowed is False
        assert decision.status == "blocked"
        assert "github:write" in decision.message
        assert _types(events) == [
            HOOK_TOOL_INTERCEPT_REQUESTED_EVENT,
            HOOK_TOOL_INTERCEPT_BLOCKED_EVENT,
        ]
        blocked = events[-1]
        assert blocked["result"]["status"] == "blocked"
        assert blocked["provenance"]["missing_tool_permissions"] == '["github:write"]'


# ---------------------------------------------------------------------------
# before_tool_call — observe-only (never vetoes)
# ---------------------------------------------------------------------------


class TestBeforeToolCallObserve:
    def test_observe_only_records_and_never_requests_intercept(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path,
            [
                _tool_call_hook(
                    name="before_tool_call",
                    failure_policy="fail_open",
                    scope=HOOK_TOOL_OBSERVE_SCOPE,
                )
            ],
        )
        events: list[dict] = []
        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(0),
            **_BEFORE_KW,
        )
        assert decision.allowed is True
        assert _types(events) == [HOOK_TOOL_OBSERVE_RECORDED_EVENT]
        assert HOOK_TOOL_INTERCEPT_REQUESTED_EVENT not in _types(events)

    def test_observe_only_still_runs_without_current_tool_authorization(
        self, tmp_path: Path
    ) -> None:
        payload = _v04_manifest()
        payload["permissions"].append(
            {
                "scope": "github:write",
                "risk": "write",
                "required": False,
                "reason": "Optional write access not granted by the trust gate.",
            }
        )
        payload["hooks"] = [
            _tool_call_hook(
                name="before_tool_call",
                failure_policy="fail_open",
                scope=HOOK_TOOL_OBSERVE_SCOPE,
            )
        ]
        target = tmp_path / "ouroboros.plugin.json"
        target.write_text(json.dumps(payload))
        manifest = load_manifest(target)
        env_sink: list[dict] = []
        events: list[dict] = []

        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(0, env_sink=env_sink),
            tool_permissions=["github:write"],
            **_BEFORE_KW,
        )

        assert decision.allowed is True
        assert _types(events) == [HOOK_TOOL_OBSERVE_RECORDED_EVENT]
        payload = json.loads(env_sink[0][TOOL_CALL_HOOK_PAYLOAD_ENV])
        assert payload["permissions"] == ["github:write"]


# ---------------------------------------------------------------------------
# after_tool_call — observation only, never blocks
# ---------------------------------------------------------------------------


class TestAfterToolCall:
    def test_after_success_records_observation(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path,
            [
                _tool_call_hook(
                    name="after_tool_call",
                    failure_policy="fail_open",
                    scope=HOOK_TOOL_OBSERVE_SCOPE,
                )
            ],
        )
        events: list[dict] = []
        decision = dispatch_after_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(0),
            **_AFTER_KW,
        )
        assert decision.allowed is True
        assert _types(events) == [HOOK_TOOL_OBSERVE_RECORDED_EVENT]

    def test_after_failure_never_blocks(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path,
            [
                _tool_call_hook(
                    name="after_tool_call",
                    failure_policy="fail_open",
                    scope=HOOK_TOOL_OBSERVE_SCOPE,
                )
            ],
        )
        events: list[dict] = []
        decision = dispatch_after_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(2),
            **_AFTER_KW,
        )
        assert decision.allowed is True
        assert _types(events) == [HOOK_FAILED_EVENT]


# ---------------------------------------------------------------------------
# Trust-boundary payload (only digests + bounded preview cross)
# ---------------------------------------------------------------------------


class TestPayloadBoundary:
    def test_args_preview_truncated_with_sentinel(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path,
            [
                _tool_call_hook(
                    name="before_tool_call",
                    failure_policy="fail_open",
                    scope=HOOK_TOOL_OBSERVE_SCOPE,
                )
            ],
        )
        env_sink: list[dict] = []
        long_preview = "x" * 5000
        kw = dict(_BEFORE_KW)
        kw["args_preview"] = long_preview
        dispatch_before_tool_call(
            manifest=manifest,
            event_sink=_noop_sink,
            subprocess_runner=_runner(0, env_sink=env_sink),
            **kw,
        )
        payload = json.loads(env_sink[0][TOOL_CALL_HOOK_PAYLOAD_ENV])
        assert len(payload["args_preview"]) == TOOL_CALL_ARGS_PREVIEW_LIMIT
        assert payload["args_preview"].endswith("…")
        # The raw 5000-char argument body never crosses the boundary.
        assert long_preview not in env_sink[0][TOOL_CALL_HOOK_PAYLOAD_ENV]

    def test_payload_carries_digest_not_raw_args(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(
            tmp_path,
            [
                _tool_call_hook(
                    name="before_tool_call",
                    failure_policy="fail_open",
                    scope=HOOK_TOOL_OBSERVE_SCOPE,
                )
            ],
        )
        env_sink: list[dict] = []
        dispatch_before_tool_call(
            manifest=manifest,
            event_sink=_noop_sink,
            subprocess_runner=_runner(0, env_sink=env_sink),
            **_BEFORE_KW,
        )
        payload = json.loads(env_sink[0][TOOL_CALL_HOOK_PAYLOAD_ENV])
        assert payload["args_digest"] == "sha256:abc123"
        assert payload["correlation_id"] == "corr-1"
        assert payload["invocation_id"] == "inv-1"


# ---------------------------------------------------------------------------
# Schema-version gating — tool-call hooks are a v0.4 vocabulary addition
# ---------------------------------------------------------------------------


class TestSchemaGating:
    def test_no_hooks_is_a_noop_allow(self, tmp_path: Path) -> None:
        manifest = _manifest_with_hooks(tmp_path, [])
        events: list[dict] = []
        decision = dispatch_before_tool_call(
            manifest=manifest,
            event_sink=events.append,
            subprocess_runner=_runner(0),
            **_BEFORE_KW,
        )
        assert decision.allowed is True
        assert events == []
