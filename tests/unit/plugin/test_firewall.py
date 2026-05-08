"""Tests for the plugin invocation firewall (Q00/ouroboros#729)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

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
):
    """Build a stand-in for subprocess.run that returns canned data."""

    def _run(argv, *args, **kwargs) -> subprocess.CompletedProcess:
        if raise_filenotfound:
            raise FileNotFoundError(argv[0])
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


def test_partial_grant_set_does_not_label_trusted(tmp_path: Path) -> None:
    """A trust record covering only some required scopes must be reported
    as `trust_state="installed"`, not `"trusted"`. Otherwise audit events
    and inspect/list output mis-label a permission boundary even though
    `_missing_required` will still block invocation.
    """
    # Manifest with TWO required permissions.
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["permissions"] = [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {"scope": "github:write", "risk": "destructive", "required": True},
    ]
    program = _make_program(tmp_path, payload)
    # User has granted only ONE of the two required scopes.
    partial = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=partial,
        event_sink=events.append,
        correlation_id="corr-partial",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    # Invocation is blocked by the missing scope (existing semantics).
    assert result.status == "blocked"
    assert "github:write" in result.message
    # Crucially: the emitted event reports the CORRECT trust_state.
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["trust_state"] == "installed", (
        f"partial grant set must not label as trusted; got {events[0]['trust_state']!r}"
    )


def test_stale_trust_record_after_version_bump_blocks(tmp_path: Path) -> None:
    """A trust record whose version no longer matches the manifest must NOT
    grant access at runtime — even if scopes are present.

    Per Q00/ouroboros-plugins#9 Q4 lock, a version bump invalidates prior
    grants. The firewall enforces this defensively: even if `add`/`install`
    failed to call `reset_for_version_bump`, an invocation with a stale
    record must be blocked.
    """
    # Pre-existing trust grant under v0.1.0.
    granted = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    # User upgrades to v0.2.0 — manifest changes but stale record persists
    # (simulating a missed reset_for_version_bump).
    bumped_payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    bumped_payload["version"] = "0.2.0"
    bumped = _make_program(tmp_path / "v2", bumped_payload)

    events: list[dict] = []
    result = invoke_plugin(
        bumped,
        command_name="review",
        argv=["url"],
        trust_record=granted,  # version='0.1.0' but manifest is now '0.2.0'
        event_sink=events.append,
        correlation_id="corr-stale",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    assert result.status == "blocked"
    assert "github:read" in result.message
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert "plugin.invoked" not in types
    # trust_state must NOT report "trusted" for a stale record.
    assert events[0]["trust_state"] == "installed"


# ---------------------------------------------------------------------------
# RFC trust-subject + cwd contract tests (`docs/rfc/userlevel-plugins.md`)
# ---------------------------------------------------------------------------


def test_artifact_digest_drift_blocks_with_trust_subject_changed(tmp_path: Path) -> None:
    """Per the RFC ("Trust identity"), the firewall recomputes the
    canonical tree hash of `plugin_home` before every invocation and
    refuses to launch on drift, with `result.status="trust_subject_changed"`.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    # Pretend the lockfile recorded a digest that does NOT match what's
    # currently on disk in `tmp_path`.
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-drift",
        plugin_home=tmp_path,
        expected_artifact_digest=(
            "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        ),
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert "plugin.invoked" not in types
    assert events[0]["result"]["status"] == "trust_subject_changed"
    assert "current_artifact_digest" in events[0]["provenance"]


def test_artifact_digest_match_proceeds(tmp_path: Path) -> None:
    """When the recomputed digest matches the lockfile-recorded digest,
    the trust check + invocation proceed normally.
    """
    from ouroboros.plugin.digest import canonical_tree_hash

    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    expected_digest = canonical_tree_hash(tmp_path)
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-match",
        plugin_home=tmp_path,
        expected_artifact_digest=expected_digest,
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    assert result.status == "success"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.completed"]


def test_disable_record_blocks_independently_of_trust(tmp_path: Path) -> None:
    """A disabled plugin must be refused by the firewall regardless of
    trust state. RFC: the firewall MUST consult the disable record before
    any invocation.
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
        trust_record=trust,  # fully trusted
        event_sink=events.append,
        correlation_id="corr-disabled",
        is_disabled=True,
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["trust_state"] == "disabled"
    assert events[0]["provenance"]["reason"] == "disabled"


def test_argv_redacts_secret_flags_and_high_confidence_tokens(tmp_path: Path) -> None:
    """Per the locked RFC, the firewall MUST redact secret-looking argv
    values before persistence. The flag-name policy covers `--token`,
    `--password`, etc. (both `--flag=value` and `--flag value` forms);
    the value-pattern policy covers Bearer tokens, GitHub PATs, OpenAI
    keys, AWS access key IDs, and JWT-shaped strings.

    Regression catch for the bot's BLOCKING finding on firewall.py:87.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    secret_argv = [
        "https://example.com/pr/1",
        "--token=ghp_thisIsClearlyASecretValue123456789",  # equals form
        "--password",  # bare flag
        "hunter2-supersecret",  # value follows
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload",  # high-confidence
        "AKIAIOSFODNN7EXAMPLE",  # AWS access key id
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",  # JWT
        "plain-arg",  # not secret
    ]
    events: list[dict] = []
    invoke_plugin(
        program,
        command_name="review",
        argv=secret_argv,
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-redact",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    invoked = next(e for e in events if e["event_type"] == "plugin.invoked")
    redacted = invoked["command"]["argv"]
    assert redacted[0] == "https://example.com/pr/1"
    # `--token=` keeps the flag name, replaces the value with [redacted].
    assert redacted[1] == "--token=[redacted]"
    # `--password` value redacted via the bare-flag-then-value rule.
    assert redacted[2] == "--password"
    assert redacted[3] == "[redacted]"
    # Bearer / AWS / JWT all match high-confidence patterns.
    assert redacted[4] == "[redacted]"
    assert redacted[5] == "[redacted]"
    assert redacted[6] == "[redacted]"
    # Non-secret string passed through unchanged.
    assert redacted[7] == "plain-arg"
    # No raw secret bytes anywhere in the serialized event stream.
    serialized = json.dumps(events)
    for needle in (
        "ghp_thisIsClearlyASecret",
        "hunter2-supersecret",
        "eyJhbGciOiJIUzI1NiJ9.payload",
        "AKIAIOSFODNN7EXAMPLE",
    ):
        assert needle not in serialized, needle
    # Forensic: argv_sha256 attached to provenance because redaction fired.
    assert "argv_sha256" in invoked["provenance"]


def test_argv_no_redaction_keeps_argv_verbatim(tmp_path: Path) -> None:
    """When no token in argv matches the redaction policy, argv passes
    through unchanged AND no `argv_sha256` is added (we only record the
    forensic hash when redaction actually fired)."""
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
        argv=["https://example.com/pr/1", "--verbose"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-clean",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    invoked = next(e for e in events if e["event_type"] == "plugin.invoked")
    assert invoked["command"]["argv"] == ["https://example.com/pr/1", "--verbose"]
    assert "argv_sha256" not in invoked.get("provenance", {})


def test_subprocess_invoked_with_plugin_home_as_cwd(tmp_path: Path) -> None:
    """When the caller plumbs `plugin_home`, the entrypoint subprocess
    must be launched with `cwd=plugin_home` so that
    `python -m github_pr_ops` resolves the plugin's modules from the
    installed root, not from the user's terminal cwd.

    Regression catch for the bot's BLOCKING finding on
    `firewall.py:319` (cwd / import-path adjustment missing).
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    seen_cwds: list[object] = []

    def _spy(argv, *args, **kwargs):
        seen_cwds.append(kwargs.get("cwd"))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=lambda _e: None,
        correlation_id="corr-cwd",
        plugin_home=tmp_path,
        subprocess_runner=_spy,
    )
    assert seen_cwds == [str(tmp_path)], seen_cwds


def test_subprocess_non_utf8_output_does_not_crash_firewall(tmp_path: Path) -> None:
    """A plugin that writes non-UTF-8 bytes to stdout/stderr must NOT
    propagate a UnicodeDecodeError out of `invoke_plugin`. The firewall
    captures bytes (no implicit decode) so arbitrary plugin output is
    handled — only the sha256 hash reaches the ledger anyway.

    Regression catch for the bot's BLOCKING finding on firewall.py:628.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )

    def _bytes_runner(argv, *args, **kwargs):
        # Lone surrogate in stdout (invalid UTF-8 sequence \x80\xff)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=b"valid\x80\xff prefix and \xc0\xc0 invalid\n",
            stderr=b"\xfe\xfe also bad\n",
        )

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-bytes",
        subprocess_runner=_bytes_runner,
    )
    # Firewall returns a structured success even with bytes output.
    assert result.status == "success"
    assert result.stdout_sha256 is not None
    assert result.stderr_sha256 is not None
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.completed"]
    # No raw bytes leak into events (only the hash).
    completed = events[-1]
    assert completed["provenance"]["stdout_sha256"] == result.stdout_sha256
    serialized = json.dumps(events)
    # Hex bytes must not appear in the event text.
    for forbidden in ("\\x80", "\\xff", "\\xfe", "\\xc0"):
        assert forbidden not in serialized, forbidden


def test_subprocess_permission_error_emits_failed_with_exit_126(tmp_path: Path) -> None:
    """Per the RFC, the firewall MUST always emit a terminal
    `plugin.failed` event for a launch failure. Previously only
    `FileNotFoundError` was caught, so `PermissionError` (entrypoint
    not executable) and other `OSError` subclasses would escape the
    firewall entirely — crashing the caller and skipping the audit
    trail. The catch is now broadened to `OSError` and uses POSIX
    convention exit code 126 ("found but not executable").

    Regression catch for the bot's BLOCKING finding on firewall.py:635.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )

    def _boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied", args[0][0] if args else "?")

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-perm",
        subprocess_runner=_boom,
    )
    assert result.status == "failed"
    assert result.exit_code == 126
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    failed = events[-1]
    assert failed["result"]["status"] == "failed"
    assert "PermissionError" in failed["result"]["message"]
    assert failed["provenance"]["exception_type"] == "PermissionError"


def test_entrypoint_unmatched_quote_emits_failed_not_crash(tmp_path: Path) -> None:
    """A manifest whose ``entrypoint.command`` carries an unmatched quote
    is installable today — the schema only enforces ``minLength: 1`` on
    the field, leaving lexical validity to the dispatcher. Without
    explicit handling, ``shlex.split`` would raise ``ValueError`` BEFORE
    any error path in ``invoke_plugin``, the exception would escape the
    firewall, and the caller would crash without ever seeing the
    required terminal ``plugin.failed`` event. The firewall must emit
    a controlled ``plugin.failed`` instead.

    Regression catch for the bot's BLOCKING finding on firewall.py:640.
    """
    import dataclasses

    from ouroboros.plugin.manifest import Entrypoint

    program = _make_program(tmp_path)
    bad_manifest = dataclasses.replace(
        program.manifest,
        entrypoint=Entrypoint(type="command", command='python -m "broken'),
    )
    bad_program = dataclasses.replace(program, manifest=bad_manifest)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    result = invoke_plugin(
        bad_program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-shlex",
    )
    assert result.status == "failed"
    assert result.exit_code == 126
    types = [e["event_type"] for e in events]
    # The terminal event MUST be plugin.failed and the dispatcher must
    # not have raised. The exact prefix of events (whether
    # ``plugin.invoked`` was emitted) is a refinement; what's
    # contractually required is that ``plugin.failed`` is the last
    # event and that the runtime did not crash.
    assert types[-1] == "plugin.failed"
    failed = events[-1]
    assert failed["result"]["status"] == "failed"
    assert "not parseable" in failed["result"]["message"]
    assert failed["provenance"]["exception_type"] == "ValueError"


def test_entrypoint_whitespace_only_command_emits_failed(tmp_path: Path) -> None:
    """``entrypoint.command`` containing only whitespace tokenises to
    ``[]`` via ``shlex.split``. Without explicit handling, the
    concatenated argv would be ``[command_name, *argv]`` and the runtime
    would attempt to launch the user-facing command name as if it were
    the executable — masking a manifest validation failure as a
    "command not found" runtime failure. Surface the empty-tokenisation
    case as a controlled ``plugin.failed`` instead.

    Regression catch for the bot's BLOCKING finding on firewall.py:640.
    """
    import dataclasses

    from ouroboros.plugin.manifest import Entrypoint

    program = _make_program(tmp_path)
    bad_manifest = dataclasses.replace(
        program.manifest,
        entrypoint=Entrypoint(type="command", command="   "),
    )
    bad_program = dataclasses.replace(program, manifest=bad_manifest)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    events: list[dict] = []
    result = invoke_plugin(
        bad_program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-empty",
    )
    assert result.status == "failed"
    assert result.exit_code == 126
    types = [e["event_type"] for e in events]
    assert types[-1] == "plugin.failed"
    failed = events[-1]
    assert "empty" in failed["result"]["message"].lower()


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
