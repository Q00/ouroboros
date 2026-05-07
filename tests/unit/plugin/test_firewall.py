"""Tests for the plugin invocation firewall (Q00/ouroboros#729)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from ouroboros.plugin.firewall import (
    invoke_plugin,
)
from ouroboros.plugin.manifest import load_manifest
from ouroboros.plugin.trust_store import TrustStore
from ouroboros.plugin.userlevel_registry import (
    UserLevelProgramRegistry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REFERENCE_MANIFEST: dict = {
    "schema_version": "0.1",
    "name": "github-pr-ops",
    "version": "0.1.0",
    "source": {"type": "local_path", "path": "plugins/github-pr-ops"},
    "commands": [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Review a pull request and summarize readiness.",
            "usage": "ooo github-pr review <pull-request-url>",
            "risk": "read_only",
            "requires_confirmation": False,
        },
        {
            "namespace": "github-pr",
            "name": "merge",
            "summary": "Merge a PR under policy.",
            "usage": "ooo github-pr merge <url>",
            "risk": "destructive",
            "requires_confirmation": True,
        },
    ],
    "capabilities": [
        {"name": "ledger", "access": "write"},
    ],
    "permissions": [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {"scope": "github:pull_request:write", "risk": "destructive", "required": False},
    ],
    "entrypoint": {"type": "command", "command": "python -m fake_plugin"},
}


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def _make_program(tmp_path: Path, payload: dict | None = None):
    """Load a manifest and register it into a fresh registry."""
    payload = payload if payload is not None else REFERENCE_MANIFEST
    manifest = load_manifest(_write_manifest(tmp_path, payload))
    registry = UserLevelProgramRegistry()
    return registry.register(manifest)


def _fake_runner(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raise_filenotfound: bool = False,
    raise_oserror: BaseException | None = None,
):
    """Build a stand-in for subprocess.run that returns canned data."""

    def _run(argv, *args, **kwargs) -> subprocess.CompletedProcess:
        if raise_filenotfound:
            raise FileNotFoundError(argv[0])
        if raise_oserror is not None:
            raise raise_oserror
        return subprocess.CompletedProcess(
            args=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return _run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_happy_path_emits_invoked_then_permission_then_completed(tmp_path: Path) -> None:
    """Test 1: trusted invocation emits invoked → permission_used → completed."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/1"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-1",
        subprocess_runner=_fake_runner(stdout="ok\n"),
    )
    assert result.status == "success"
    assert result.exit_code == 0
    assert [e["event_type"] for e in events] == [
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.completed",
    ]
    # plugin.invoked appears BEFORE permission_used (locked invocation order).
    assert events[1]["permissions_used"] == ["github:read"]
    assert events[2]["result"]["status"] == "success"
    # No raw stdout/stderr content in any event payload. The literal
    # bytes returned from the fake runner ("ok\n") must not leak into
    # any event.
    serialized = json.dumps(events)
    assert "ok\\n" not in serialized
    # sha256 hash recorded in completed.provenance.
    assert "stdout_sha256" in events[-1]["provenance"]


def test_trust_violation_only_emits_failed_no_invoked(tmp_path: Path) -> None:
    """Test 2: missing required scope → ONLY plugin.failed (status=blocked).

    Crucially, plugin.invoked must NOT be emitted when the trust check
    fails (locked Q1 of Q00/ouroboros-plugins#9).
    """
    program = _make_program(tmp_path)
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/1"],
        trust_record=None,  # not yet trusted
        event_sink=events.append,
        correlation_id="corr-2",
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    assert result.exit_code is None
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert "plugin.invoked" not in types  # explicit absence assertion
    # Message format per locked Q1.
    assert "github:read" in result.message
    assert "ooo plugin trust github-pr-ops --scope github:read" in result.message
    assert events[0]["result"]["status"] == "blocked"


def test_subprocess_failure_emits_failed_with_exit_code(tmp_path: Path) -> None:
    """Test 3: subprocess exits non-zero → invoked, permission_used, failed."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["bad-url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-3",
        subprocess_runner=_fake_runner(returncode=2, stderr="boom\n"),
    )
    assert result.status == "failed"
    assert result.exit_code == 2
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    assert events[-1]["result"]["status"] == "failed"
    assert "code 2" in events[-1]["result"]["message"]


def test_bounded_payload_records_sha_not_raw(tmp_path: Path) -> None:
    """Test 4: 1MB stdout — no part of it appears in any event;
    sha256 hash recorded instead."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    big_payload = "X" * (1024 * 1024)  # 1 MiB
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-4",
        subprocess_runner=_fake_runner(stdout=big_payload),
    )
    assert result.status == "success"
    assert result.stdout_sha256 is not None
    # No raw payload in any event (string check).
    serialized = json.dumps(events)
    assert "X" * 1000 not in serialized
    # sha256 hash present in completed event provenance.
    completed_event = next(e for e in events if e["event_type"] == "plugin.completed")
    assert completed_event["provenance"]["stdout_sha256"] == result.stdout_sha256


def test_confirmation_declined_blocks_with_no_subprocess(tmp_path: Path) -> None:
    """Test 5: requires_confirmation=true + confirm()=False → blocked.

    No subprocess launched; only plugin.failed (status=blocked) emitted.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    runner_called = False

    def _spy(*args, **kwargs):
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="merge",  # requires_confirmation = True
        argv=["https://example.com/pr/1"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-5",
        confirm=lambda _msg: False,  # user said No
        subprocess_runner=_spy,
    )
    assert result.status == "blocked"
    assert runner_called is False
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert "user declined" in result.message


def test_confirmation_accepted_proceeds(tmp_path: Path) -> None:
    """Test 6: requires_confirmation=true + confirm()=True → normal flow."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="merge",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-6",
        confirm=lambda _msg: True,
        subprocess_runner=_fake_runner(returncode=0, stdout="ok"),
    )
    assert result.status == "success"
    types = [e["event_type"] for e in events]
    # Standard happy-path order; only one permission emitted (github:read,
    # the required one). github:pull_request:write is required:false so
    # it's NOT emitted in v0 (Option (a) coarse rule).
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.completed"]
    assert events[1]["permissions_used"] == ["github:read"]


def test_optional_permission_not_emitted(tmp_path: Path) -> None:
    """Test 7: required:false permission is NOT emitted in v0.

    The reference manifest has 'github:pull_request:write' with
    required:false. After invocation, no plugin.permission_used event
    should reference it (locked Option (a) coarse emission rule).
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-7",
        subprocess_runner=_fake_runner(stdout=""),
    )
    permission_events = [e for e in events if e["event_type"] == "plugin.permission_used"]
    scopes_emitted = {p for e in permission_events for p in e["permissions_used"]}
    assert scopes_emitted == {"github:read"}
    assert "github:pull_request:write" not in scopes_emitted


def test_first_party_skips_trust_check(tmp_path: Path) -> None:
    """Test 8: source.type=first_party bypasses trust check (Q00/ouroboros-plugins#8 lock)."""
    fp = json.loads(json.dumps(REFERENCE_MANIFEST))
    fp["name"] = "ooo-auto"
    fp["source"] = {"type": "first_party"}
    fp["permissions"] = []  # first-party with no external scopes
    fp["commands"] = [
        {
            "namespace": "auto",
            "name": "run",
            "summary": "Run auto.",
            "usage": "ooo auto",
            "risk": "write",
        }
    ]
    program = _make_program(tmp_path, fp)
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="run",
        argv=["my goal"],
        trust_record=None,  # no trust at all
        event_sink=events.append,
        correlation_id="corr-8",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    assert result.status == "success"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.completed"]
    # trust_state field reports "first_party"
    assert all(e["trust_state"] == "first_party" for e in events)


def test_entrypoint_missing_emits_failed_127(tmp_path: Path) -> None:
    """Test 9: subprocess FileNotFoundError → status=failed, exit_code=127."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-9",
        subprocess_runner=_fake_runner(raise_filenotfound=True),
    )
    assert result.status == "failed"
    assert result.exit_code == 127
    # invoked + permission_used + failed
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    assert "not found" in result.message.lower()


def test_required_permission_order_matches_manifest_declaration(tmp_path: Path) -> None:
    """Manifest declaration order must drive both the blocked-scope error
    message (which names the FIRST missing required scope) and the
    `plugin.permission_used` event order.

    Pre-fix, `manifest.permissions` was a `frozenset`, so iteration order
    was undefined and the firewall's UX/audit ordering became
    process-dependent. Now `permissions` is a tuple preserving the JSON
    declaration order; both downstream behaviors must follow.
    """
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    # Three required permissions in a specific JSON order.
    payload["permissions"] = [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {"scope": "shell:execute", "risk": "destructive", "required": True},
        {"scope": "github:repo:read", "risk": "read_only", "required": True},
    ]
    program = _make_program(tmp_path, payload)

    # Untrusted call -> blocked message names the FIRST declared scope.
    blocked_events: list[dict] = []
    blocked = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=None,
        event_sink=blocked_events.append,
        correlation_id="corr-order-blocked",
        subprocess_runner=_fake_runner(),
    )
    assert blocked.status == "blocked"
    assert "github:read" in blocked.message
    assert blocked.message.index("github:read") < blocked.message.index("github-pr-ops")

    # Fully trust then re-invoke; permission_used events follow declaration order.
    store = TrustStore(root=tmp_path / "trust")
    for scope in ("github:read", "shell:execute", "github:repo:read"):
        store.grant(plugin="github-pr-ops", version="0.1.0", scope=scope, granted_by="u")
    trust = store.read("github-pr-ops")
    happy_events: list[dict] = []
    happy = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=happy_events.append,
        correlation_id="corr-order-happy",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    assert happy.status == "success"
    permission_events = [
        e for e in happy_events if e["event_type"] == "plugin.permission_used"
    ]
    scopes_in_order = [e["permissions_used"][0] for e in permission_events]
    assert scopes_in_order == ["github:read", "shell:execute", "github:repo:read"]


@pytest.mark.parametrize(
    "exc, expected_code, descriptor_substr",
    [
        (PermissionError(13, "Permission denied"), 126, "not executable"),
        (IsADirectoryError(21, "Is a directory"), 126, "is a directory"),
        (NotADirectoryError(20, "Not a directory"), 126, "not a directory"),
        (OSError(5, "I/O error"), 126, "launcher error"),
    ],
)
def test_launcher_oserror_emits_terminal_failed(
    tmp_path: Path,
    exc: OSError,
    expected_code: int,
    descriptor_substr: str,
) -> None:
    """All launcher-side OSErrors must produce a terminal `plugin.failed`.

    Pre-fix the firewall only caught `FileNotFoundError`. Anything else
    escaped as an exception AFTER `plugin.invoked` and
    `plugin.permission_used` had already been emitted, leaving an
    incomplete audit trail and breaking the documented contract that
    `invoke_plugin` always returns an `InvocationResult`.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-oserror",
        subprocess_runner=_fake_runner(raise_oserror=exc),
    )
    assert result.status == "failed"
    assert result.exit_code == expected_code
    assert descriptor_substr in result.message.lower()
    types = [e["event_type"] for e in events]
    # Contract: once `plugin.invoked` is emitted, a terminal event MUST
    # follow. permission_used appears between them per locked order.
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    assert events[-1]["result"]["status"] == "failed"
