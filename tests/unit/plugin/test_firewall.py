"""Tests for the plugin invocation firewall (Q00/ouroboros#729)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from ouroboros.plugin.firewall import (
    DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS,
    invoke_plugin,
)
from ouroboros.plugin.hooks import HOOK_LIFECYCLE_READ_SCOPE
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


def _hook_manifest_payload(schema_version: str) -> dict:
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["schema_version"] = schema_version
    if schema_version == "0.3":
        payload["permissions"].append(
            {
                "scope": HOOK_LIFECYCLE_READ_SCOPE,
                "risk": "read_only",
                "required": False,
                "reason": "Allow v1 lifecycle hook observation.",
            }
        )
        hook_permissions = [HOOK_LIFECYCLE_READ_SCOPE]
    else:
        hook_permissions = []
    payload["hooks"] = [
        {
            "name": "before_invocation",
            "entrypoint": {"type": "command", "command": "python -m hook_before"},
            "permissions": hook_permissions,
            "failure_policy": "fail_closed",
        },
        {
            "name": "after_invocation",
            "entrypoint": {"type": "command", "command": "python -m hook_after"},
            "permissions": hook_permissions,
            "failure_policy": "fail_open",
        },
    ]
    return payload


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


def test_argv_summary_is_emitted_alongside_argv(tmp_path: Path) -> None:
    """Every event with an argv carries an `argv_summary` block that sizes
    the payload (argc, byte_length, sha256). The full argv stays in the
    envelope verbatim — this is the observation step before any cap or
    spill policy is decided. Two different argv lists with the same
    concatenation must not collide on sha256 (NUL separator invariant).
    """
    import hashlib

    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    events: list[dict] = []
    invoke_plugin(
        program,
        command_name="review",
        argv=["--input", "https://example.com/pr/1"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-summary",
        subprocess_runner=_fake_runner(),
    )
    assert events  # something was emitted
    for event in events:
        if "argv" not in event["command"]:
            continue
        summary = event["command"]["argv_summary"]
        assert summary["argc"] == 2
        assert summary["byte_length"] == len("--input") + len("https://example.com/pr/1")
        # The full argv survives unchanged — no truncation.
        assert event["command"]["argv"] == ["--input", "https://example.com/pr/1"]
        # sha256 deterministic and distinct from naive-concat collision.
        joined = b"\x00".join(s.encode("utf-8") for s in event["command"]["argv"])
        assert summary["sha256"] == hashlib.sha256(joined).hexdigest()
        # Collision guard: ["ab","cd"] and ["abcd"] hash differently.
        ab_cd = hashlib.sha256(b"ab\x00cd").hexdigest()
        abcd = hashlib.sha256(b"abcd").hexdigest()
        assert ab_cd != abcd


def test_argv_summary_omitted_when_argv_is_none(tmp_path: Path) -> None:
    """A plugin invocation with no argv (e.g. blocked-before-launch flow)
    must not produce an argv_summary either — the schema's
    `additionalProperties: false` rejects half-populated command dicts."""
    program = _make_program(tmp_path)
    events: list[dict] = []
    invoke_plugin(
        program,
        command_name="review",
        argv=[],
        trust_record=None,  # blocks before launch
        event_sink=events.append,
        correlation_id="corr-noargv",
        subprocess_runner=_fake_runner(),
    )
    # The blocked path emits plugin.failed only.
    failed = [e for e in events if e["event_type"] == "plugin.failed"]
    assert failed
    cmd = failed[0]["command"]
    if "argv" not in cmd:
        # No argv → no summary.
        assert "argv_summary" not in cmd


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


def test_v02_hook_manifest_does_not_run_or_emit_hook_events(tmp_path: Path) -> None:
    """v0.2 may load hooks for compatibility, but runtime dispatch is v0.3-only."""
    program = _make_program(tmp_path, _hook_manifest_payload("0.2"))
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/1"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-v02-hooks",
        subprocess_runner=runner,
    )

    assert result.status == "success"
    assert calls == [["python", "-m", "fake_plugin", "review", "https://example.com/pr/1"]]
    assert [event["event_type"] for event in events] == [
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.completed",
    ]


def test_v03_hook_manifest_runs_and_emits_hook_events(tmp_path: Path) -> None:
    program = _make_program(tmp_path, _hook_manifest_payload("0.3"))
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/1"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-v03-hooks",
        subprocess_runner=runner,
    )

    assert result.status == "success"
    assert calls == [
        ["python", "-m", "hook_before"],
        ["python", "-m", "fake_plugin", "review", "https://example.com/pr/1"],
        ["python", "-m", "hook_after"],
    ]
    assert [event["event_type"] for event in events] == [
        "plugin.hook.invoked",
        "plugin.hook.completed",
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.hook.invoked",
        "plugin.hook.completed",
        "plugin.completed",
    ]
    assert all(
        event["schema_version"] == "0.3"
        for event in events
        if event["event_type"].startswith("plugin.hook.")
    )


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


def test_confirmation_prompt_uses_redacted_argv(tmp_path: Path) -> None:
    """The one destructive-command prompt is user-visible output, so it
    must not echo argv secrets that the audit envelope would redact."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    prompts: list[str] = []

    result = invoke_plugin(
        program,
        command_name="merge",
        argv=["--token", "hunter2-supersecret"],
        trust_record=trust,
        event_sink=lambda _event: None,
        correlation_id="corr-confirm-redact",
        confirm=lambda prompt: prompts.append(prompt) or False,
        subprocess_runner=_fake_runner(),
    )

    assert result.status == "blocked"
    assert prompts
    assert "hunter2-supersecret" not in prompts[0]
    assert "Action: merge --token [redacted]" in prompts[0]


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

    # Plugin home and trust root MUST be separate directories — production
    # ``DEFAULT_TRUST_ROOT`` lives outside ``DEFAULT_PLUGIN_HOME_ROOT``
    # for exactly this reason: every trust write would otherwise mutate
    # the hashed plugin tree and trip the firewall's digest check on
    # the next invoke.
    plugin_home = tmp_path / "plugin_home"
    plugin_home.mkdir()
    program = _make_program(plugin_home)
    expected_digest = canonical_tree_hash(plugin_home)
    # Trust is granted under the full install subject (post-RFC contract):
    # version + source_type + source_identity + artifact_digest. The
    # firewall's ``_record_matches_subject`` requires every column to
    # be populated when the caller plumbs ``expected_*``, so a record
    # bound to the actual install subject is what proves "granted for
    # THIS install" — the legacy version-only record path is reserved
    # for tests that don't plumb subject expectations.
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
        source_type="local_path",
        source_identity=str(plugin_home),
        artifact_digest=expected_digest,
    )
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-match",
        plugin_home=plugin_home,
        expected_source_identity=str(plugin_home),
        expected_artifact_digest=expected_digest,
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    assert result.status == "success"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.completed"]


def test_legacy_trust_record_refused_when_subject_plumbed(tmp_path: Path) -> None:
    """A pre-RFC trust record (blank ``source_type`` /
    ``source_identity`` / ``artifact_digest``) MUST NOT match a
    same-version reinstall when the dispatcher plumbs the new install
    subject. Otherwise an operator who upgrades into the trust-subject
    contract would silently keep their old grants for a freshly
    installed plugin from a different repo or with different bytes —
    exactly the boundary the new model is meant to enforce.
    """
    from ouroboros.plugin.digest import canonical_tree_hash

    plugin_home = tmp_path / "plugin_home"
    plugin_home.mkdir()
    program = _make_program(plugin_home)
    expected_digest = canonical_tree_hash(plugin_home)
    # Pre-RFC grant: only `version` + `scope` recorded. The firewall
    # cannot prove this was granted for the current install subject.
    legacy_trust = TrustStore(root=tmp_path / "trust").grant(
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
        trust_record=legacy_trust,
        event_sink=events.append,
        correlation_id="corr-legacy",
        plugin_home=plugin_home,
        expected_source_identity=str(plugin_home),
        expected_artifact_digest=expected_digest,
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert "plugin.invoked" not in types
    assert types[-1] == "plugin.failed"


def test_digest_fails_closed_when_plugin_home_replaced_with_file(tmp_path: Path) -> None:
    """``canonical_tree_hash`` raises ``NotADirectoryError`` when
    ``plugin_home`` has been replaced with a regular file. The
    firewall MUST refuse with ``result.status="trust_subject_changed"``
    rather than letting the exception escape past the audit trail.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    not_a_dir = tmp_path / "ph_file"
    not_a_dir.write_text("not a plugin")
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-notadir",
        plugin_home=not_a_dir,
        expected_artifact_digest="sha256:" + "0" * 64,
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["result"]["status"] == "trust_subject_changed"


def test_digest_fails_closed_on_escaping_symlink(tmp_path: Path) -> None:
    """``canonical_tree_hash`` raises ``EscapingSymlinkError`` for
    symlinks that escape the plugin root (post-install tampering).
    The firewall MUST fail closed with ``trust_subject_changed``,
    not let the exception escape.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    plugin_home = tmp_path / "ph_escaping"
    plugin_home.mkdir()
    (plugin_home / "ouroboros.plugin.json").write_text("{}")
    outside = tmp_path / "outside.txt"
    outside.write_text("escaped")
    # Symlink inside plugin_home pointing to a sibling outside the root.
    (plugin_home / "leak").symlink_to(outside)
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-escaping",
        plugin_home=plugin_home,
        expected_artifact_digest="sha256:" + "0" * 64,
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["result"]["status"] == "trust_subject_changed"
    assert events[0]["provenance"]["exception_type"] == "EscapingSymlinkError"


def test_digest_fails_closed_on_plugin_home_symlink_loop_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some ``Path.resolve(strict=True)`` implementations report a
    symlink loop at ``plugin_home`` as ``RuntimeError``. The firewall
    must still fail closed and emit the terminal audit event.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    plugin_home = tmp_path / "ph_loop"
    plugin_home.symlink_to(plugin_home)
    original_resolve = Path.resolve

    def resolve_with_runtime_error(
        self: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if self == plugin_home:
            raise RuntimeError(f"Symlink loop from {plugin_home!s}")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_runtime_error)
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-loop-runtime",
        plugin_home=plugin_home,
        expected_artifact_digest="sha256:" + "0" * 64,
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["result"]["status"] == "trust_subject_changed"
    assert events[0]["provenance"]["reason"] == "plugin_home_unreadable"
    assert events[0]["provenance"]["exception_type"] == "RuntimeError"


def test_digest_fails_closed_on_unsupported_file_type(tmp_path: Path) -> None:
    """``canonical_tree_hash`` raises ``UnsupportedFileTypeError`` for
    devices, FIFOs, and sockets inside the plugin tree. The firewall
    MUST fail closed with ``trust_subject_changed`` and a tampered-
    home provenance reason rather than letting the exception escape.
    """
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("FIFO requires POSIX")

    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    plugin_home = tmp_path / "ph_fifo"
    plugin_home.mkdir()
    (plugin_home / "ouroboros.plugin.json").write_text("{}")
    fifo_path = plugin_home / "stub.fifo"
    os.mkfifo(fifo_path)
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-fifo",
        plugin_home=plugin_home,
        expected_artifact_digest="sha256:" + "0" * 64,
        subprocess_runner=_fake_runner(),
    )
    assert result.status == "blocked"
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["result"]["status"] == "trust_subject_changed"
    assert events[0]["provenance"]["exception_type"] == "UnsupportedFileTypeError"


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
        "--authorization",
        "Bearer",
        "opaqueSecret123",
        "--bearer=Bearer",
        "opaqueSecret456",
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
    # Split Bearer forms redact both the marker and opaque tail.
    assert redacted[5] == "--authorization"
    assert redacted[6] == "[redacted]"
    assert redacted[7] == "[redacted]"
    assert redacted[8] == "--bearer=[redacted]"
    assert redacted[9] == "[redacted]"
    assert redacted[10] == "[redacted]"
    assert redacted[11] == "[redacted]"
    # Non-secret string passed through unchanged.
    assert redacted[12] == "plain-arg"
    # No raw secret bytes anywhere in the serialized event stream.
    serialized = json.dumps(events)
    for needle in (
        "ghp_thisIsClearlyASecret",
        "hunter2-supersecret",
        "eyJhbGciOiJIUzI1NiJ9.payload",
        "opaqueSecret123",
        "opaqueSecret456",
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
    clean_argv = ["https://example.com/pr/1", "Bearer", "--force", "plain-tail"]
    events: list[dict] = []
    invoke_plugin(
        program,
        command_name="review",
        argv=clean_argv,
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-clean",
        subprocess_runner=_fake_runner(stdout="ok"),
    )
    invoked = next(e for e in events if e["event_type"] == "plugin.invoked")
    assert invoked["command"]["argv"] == clean_argv
    assert "argv_sha256" not in invoked.get("provenance", {})


def test_confirmation_prompt_keeps_standalone_bearer_arguments(
    tmp_path: Path,
) -> None:
    """A literal ``Bearer`` token is not secret context by itself.

    Regression catch for the bot design note on PR #857: over-redacting
    ``["Bearer", "--force"]`` would hide real destructive flags from the
    user-visible confirmation prompt and from audit argv.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    prompts: list[str] = []
    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="merge",
        argv=["Bearer", "--force"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-standalone-bearer",
        confirm=lambda prompt: prompts.append(prompt) or False,
        subprocess_runner=_fake_runner(),
    )

    assert result.status == "blocked"
    assert prompts
    assert "Action: merge Bearer --force" in prompts[0]
    failed = next(e for e in events if e["event_type"] == "plugin.failed")
    assert failed["command"]["argv"] == ["Bearer", "--force"]
    assert "argv_sha256" not in failed.get("provenance", {})


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


def test_subprocess_invocation_uses_default_timeout(tmp_path: Path) -> None:
    """The firewall owns the external plugin process boundary, so the
    subprocess launch must always carry a finite timeout."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    observed_kwargs: dict = {}

    def _spy_runner(argv, *args, **kwargs) -> subprocess.CompletedProcess:  # noqa: ARG001
        observed_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok", stderr="")

    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=lambda _event: None,
        correlation_id="corr-timeout-kw",
        subprocess_runner=_spy_runner,
    )

    assert result.status == "success"
    assert observed_kwargs["timeout"] == DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS


def test_subprocess_timeout_emits_terminal_failed_event(tmp_path: Path) -> None:
    """TimeoutExpired must not escape the firewall or leave the audit
    sequence without a terminal plugin.failed event."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )

    def _timeout_runner(argv, *args, **kwargs) -> subprocess.CompletedProcess:  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-timeout",
        subprocess_runner=_timeout_runner,
    )

    assert result.status == "failed"
    assert result.exit_code == 124
    assert "timed out" in result.message
    assert [event["event_type"] for event in events] == [
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.failed",
    ]
    failed = events[-1]
    assert failed["result"]["status"] == "failed"
    assert failed["provenance"]["reason"] == "timeout"
    assert failed["provenance"]["exception_type"] == "TimeoutExpired"


def test_subprocess_timeout_preserves_partial_output_hashes_only_in_events(
    tmp_path: Path,
) -> None:
    """TimeoutExpired may carry partial stdout/stderr buffers. The
    firewall must return those bytes to in-process callers and put only
    their hashes in the terminal audit event."""
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    partial_stdout = b"partial stdout before timeout\n"
    partial_stderr = b"partial stderr before timeout\n"

    def _timeout_runner(argv, *args, **kwargs) -> subprocess.CompletedProcess:  # noqa: ARG001
        raise subprocess.TimeoutExpired(
            cmd=argv,
            timeout=kwargs["timeout"],
            output=partial_stdout,
            stderr=partial_stderr,
        )

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-timeout-partial",
        subprocess_runner=_timeout_runner,
    )

    expected_stdout_hash = hashlib.sha256(partial_stdout).hexdigest()
    expected_stderr_hash = hashlib.sha256(partial_stderr).hexdigest()
    assert result.status == "failed"
    assert result.exit_code == 124
    assert result.stdout_bytes == partial_stdout
    assert result.stderr_bytes == partial_stderr
    assert result.stdout_sha256 == expected_stdout_hash
    assert result.stderr_sha256 == expected_stderr_hash

    assert [event["event_type"] for event in events] == [
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.failed",
    ]
    failed = events[-1]
    assert failed["result"]["status"] == "failed"
    assert failed["provenance"]["reason"] == "timeout"
    assert failed["provenance"]["stdout_sha256"] == expected_stdout_hash
    assert failed["provenance"]["stderr_sha256"] == expected_stderr_hash

    serialized = json.dumps(events)
    assert "partial stdout before timeout" not in serialized
    assert "partial stderr before timeout" not in serialized
