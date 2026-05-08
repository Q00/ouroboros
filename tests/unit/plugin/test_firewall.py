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


def test_stale_trust_version_blocks_invocation(tmp_path: Path) -> None:
    """Regression: a trust file from an older version of the plugin must
    not satisfy the firewall after the plugin is upgraded.

    Locked Q00/ouroboros-plugins#9 Q4 makes a version bump invalidate
    trust. The firewall enforces this by treating a TrustRecord whose
    `version` differs from the manifest as if no scopes were granted —
    otherwise an upgrade-without-reset would silently bypass the gate.
    """
    program = _make_program(tmp_path)  # manifest version = "0.1.0"
    # Trust file claims grants for an older release of the same plugin.
    stale_trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.0.9",  # stale: predates the installed manifest
        scope="github:read",
        granted_by="user:test",
    )
    runner_called = False

    def _spy(*args, **kwargs):
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=stale_trust,
        event_sink=events.append,
        correlation_id="corr-stale",
        subprocess_runner=_spy,
    )
    # The firewall must refuse the call and never reach the runner.
    assert result.status == "blocked"
    assert runner_called is False, "stale trust must not let the entrypoint launch"
    # Only `plugin.failed` (status=blocked); no `plugin.invoked` slipped through.
    types = [e["event_type"] for e in events]
    assert types == ["plugin.failed"]
    assert events[0]["result"]["status"] == "blocked"
    # Blocked-message must guide the user to re-trust the same scope.
    assert "github:read" in result.message
    # And the emitted event must NOT label the plugin "trusted" while it
    # is in fact being blocked — that was the consistency bug.
    assert events[0]["trust_state"] != "trusted"


def test_entrypoint_permission_error_emits_failed_126(tmp_path: Path) -> None:
    """Regression: PermissionError at subprocess launch must reach a
    terminal `plugin.failed` event instead of escaping the firewall.

    Previously only FileNotFoundError was caught, so an entrypoint that
    existed but lacked the exec bit (or any other OSError surfaced at
    spawn-time) crashed the caller with no audit trail. The firewall
    now widens the catch to OSError and uses conventional shell exit
    codes (126 = found-but-not-executable).
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )

    def _runner(argv, *args, **kwargs):
        raise PermissionError(13, "Permission denied", argv[0])

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-perm",
        subprocess_runner=_runner,
    )
    assert result.status == "failed"
    assert result.exit_code == 126
    # invoked + permission_used + failed (does not raise).
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    assert events[-1]["result"]["status"] == "failed"
    assert "not executable" in result.message


def test_subprocess_runs_with_cwd_for_local_path_manifest(tmp_path: Path) -> None:
    """Regression: `local_path` / `plugin_home` manifests carry their
    own location, and relative entrypoints (`./run.sh`) or commands
    that read files from the plugin directory only work when the
    subprocess is anchored there. The firewall must pass `cwd=...`
    to subprocess.run, not silently inherit the caller's cwd.
    """
    # Use a sandboxed relative path that resolves into tmp_path. The
    # manifest loader rejects absolute paths for `local_path` /
    # `plugin_home` (#745 sandbox), and then anchors the relative slug
    # to the manifest's directory — which is `tmp_path` for this test.
    payload = json.loads(json.dumps(REFERENCE_MANIFEST))
    payload["source"] = {"type": "local_path", "path": "."}
    program = _make_program(tmp_path, payload)

    captured: dict = {}

    def _runner(argv, *args, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

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
        correlation_id="corr-cwd",
        subprocess_runner=_runner,
    )
    assert result.status == "success"
    # subprocess was invoked with cwd anchored to the plugin's source.path,
    # not to wherever the caller happened to be.
    assert captured["cwd"] is not None
    assert Path(captured["cwd"]).resolve() == tmp_path.resolve()


def test_subprocess_inherits_cwd_for_first_party_manifest(tmp_path: Path) -> None:
    """First-party plugins ship inside the binary and have no plugin-
    local directory, so the firewall must NOT pin a `cwd`."""
    fp = json.loads(json.dumps(REFERENCE_MANIFEST))
    fp["name"] = "ooo-builtin"
    fp["source"] = {"type": "first_party"}
    fp["permissions"] = []
    fp["commands"] = [
        {
            "namespace": "builtin",
            "name": "run",
            "summary": "Run.",
            "usage": "ooo builtin run",
            "risk": "write",
        }
    ]
    program = _make_program(tmp_path, fp)

    captured: dict = {}

    def _runner(argv, *args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    events: list[dict] = []
    invoke_plugin(
        program,
        command_name="run",
        argv=[],
        trust_record=None,
        event_sink=events.append,
        correlation_id="corr-fp",
        subprocess_runner=_runner,
    )
    assert captured["cwd"] is None, (
        "first-party plugins must inherit the caller's cwd; "
        "the firewall has no plugin-local directory to pin"
    )


def test_missing_plugin_cwd_does_not_inherit_callers_cwd(tmp_path: Path) -> None:
    """Regression: when a `local_path` / `plugin_home` plugin's
    `source.path` does not exist on disk, the firewall must NOT
    fall back to `cwd=None` (which silently inherits the operator's
    current working directory). With a relative entrypoint like
    `./run.sh` or any command that reads plugin-local files, that
    fallback can execute the wrong program or the wrong inputs.

    The firewall now passes the (non-existent) candidate path to
    `subprocess.run`, where the kernel's `chdir` call raises
    `OSError(ENOENT)`. `invoke_plugin` already wraps that into a
    terminal `plugin.failed` event with exit 126 and the missing-dir
    reason in the message, which is the correct surface for this
    failure mode.
    """
    from ouroboros.plugin.manifest import (
        Capability,
        CommandSpec,
        Entrypoint,
        Permission,
        PluginManifest,
        SourceSpec,
    )
    from ouroboros.plugin.userlevel_registry import UserLevelProgramRegistry

    # Construct the manifest with a guaranteed-missing absolute path.
    # We build it in-memory rather than going through `load_manifest`
    # because the sandbox check rejects absolute paths from on-disk
    # manifests; here we're testing the firewall's own behavior when
    # the resolved path no longer exists at launch time.
    missing_dir = tmp_path / "vanished_plugin_home"
    assert not missing_dir.exists()
    manifest = PluginManifest(
        schema_version="0.1",
        name="github-pr-ops",
        version="0.1.0",
        source=SourceSpec(type="local_path", path=str(missing_dir)),
        commands=(
            CommandSpec(
                namespace="github-pr",
                name="review",
                summary="x",
                usage="x",
                risk="read_only",
                requires_confirmation=False,
                arguments=(),
            ),
        ),
        capabilities=(Capability(name="ledger", access="write", reason="x"),),
        permissions=(Permission(scope="github:read", risk="read_only", required=True),),
        entrypoint=Entrypoint(type="command", command="python -m github_pr_ops"),
        description="",
    )
    registry = UserLevelProgramRegistry()
    program = registry.register(manifest)

    captured: dict = {}

    def _runner(argv, *args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        # Real `subprocess.run` raises FileNotFoundError when cwd
        # doesn't exist; emulate that so the firewall's OSError
        # branch runs and we observe the documented `plugin.failed`.
        raise FileNotFoundError(2, "No such file or directory", str(kwargs.get("cwd")))

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
        correlation_id="corr-missing",
        subprocess_runner=_runner,
    )

    # The firewall pinned `cwd` to the resolved (non-existent) path,
    # NOT to None — this is what made the kernel raise ENOENT.
    assert captured["cwd"] is not None, (
        "missing-dir must not fall back to caller's cwd; the firewall "
        "should pass the non-existent path so subprocess.run raises ENOENT"
    )
    assert Path(captured["cwd"]).resolve() == missing_dir.resolve()
    # And the failure surfaces as a terminal plugin.failed (the
    # OSError-wrapping path established in round 7).
    assert result.status == "failed"
    types = [e["event_type"] for e in events]
    assert types[-1] == "plugin.failed"


def test_malformed_entrypoint_quoting_emits_failed_not_crash(tmp_path: Path) -> None:
    """Regression: an `entrypoint.command` with malformed shell
    quoting (unterminated quote) passes the schema's `\\S` pattern
    check and `load_manifest()`, but `shlex.split()` raises
    `ValueError` — so the firewall used to crash *after* having
    emitted `plugin.invoked` and friends, leaving the audit log
    partially written and bubbling the exception up to the caller.

    The firewall contract is to convert every launch failure into
    a terminal `plugin.failed` event with a clean audit trail and
    a structured `InvocationResult`. This test asserts that the
    `shlex.split` path now obeys that contract for malformed
    quoting too.
    """
    from ouroboros.plugin.manifest import (
        Capability,
        CommandSpec,
        Entrypoint,
        PluginManifest,
        SourceSpec,
    )
    from ouroboros.plugin.userlevel_registry import UserLevelProgramRegistry

    # Build the manifest in-memory so the malformed `command` survives
    # — the schema would reject a manifest with truly nonsense quoting,
    # but a future schema relaxation, a fixture, or a mistake in the
    # manager pipeline could still produce one. The firewall must
    # tolerate it.
    bad = PluginManifest(
        schema_version="0.1",
        name="github-pr-ops",
        version="0.1.0",
        source=SourceSpec(type="first_party"),  # no cwd anchoring needed
        commands=(
            CommandSpec(
                namespace="github-pr",
                name="review",
                summary="x",
                usage="x",
                risk="read_only",
                requires_confirmation=False,
                arguments=(),
            ),
        ),
        capabilities=(Capability(name="ledger", access="write", reason="x"),),
        permissions=(),
        entrypoint=Entrypoint(type="command", command='"unterminated'),
    )
    registry = UserLevelProgramRegistry()
    program = registry.register(bad)

    captured: dict = {"called": False}

    def _runner(argv, *args, **kwargs):
        # If the firewall reaches this, the shlex error wasn't caught
        # and the runner is being invoked with garbage argv. Fail
        # the test loudly rather than letting it pass silently.
        captured["called"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=[],
        trust_record=None,
        event_sink=events.append,
        correlation_id="corr-bad-quote",
        subprocess_runner=_runner,
    )

    # The firewall short-circuited before launching the subprocess,
    # so the runner must NOT have been called.
    assert captured["called"] is False
    # Terminal event sequence is invoked → permission_used* → failed.
    types = [e["event_type"] for e in events]
    assert types[-1] == "plugin.failed"
    assert result.status == "failed"
    assert result.exit_code == 126
    assert "malformed" in result.message or "quoting" in result.message


def test_whitespace_entrypoint_emits_failed_not_crash(tmp_path: Path) -> None:
    """Regression: an entrypoint command that's only whitespace passed
    `minLength: 1` schema validation in earlier revisions and made
    `shlex.split(" ")` return `[]`. The firewall must still emit a
    terminal `plugin.failed` event with a clear message rather than
    crash the caller.

    The schema now also rejects whitespace-only commands, but the
    firewall keeps a defense-in-depth runtime guard so a constructed
    manifest (e.g. via a fixture or a future lax schema) cannot bypass
    the audit-output contract.
    """
    # Build the manifest manually so it bypasses the schema's tightened
    # `\\S` pattern — we're testing the firewall's runtime guard, not
    # the schema.
    from ouroboros.plugin.manifest import (
        CommandSpec,
        Entrypoint,
        Permission,
        PluginManifest,
        SourceSpec,
    )
    from ouroboros.plugin.userlevel_registry import UserLevelProgramRegistry

    bad_manifest = PluginManifest(
        schema_version="0.1",
        name="github-pr-ops",
        version="0.1.0",
        source=SourceSpec(type="local_path", path=str(tmp_path)),
        commands=(
            CommandSpec(
                namespace="github-pr",
                name="review",
                summary="x",
                usage="x",
                risk="read_only",
                requires_confirmation=False,
                arguments=(),
            ),
        ),
        capabilities=(),
        permissions=(Permission(scope="github:read", risk="read_only", required=True),),
        entrypoint=Entrypoint(type="command", command="   "),
    )
    program = UserLevelProgramRegistry().register(bad_manifest)
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
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-empty-cmd",
        subprocess_runner=_spy,
    )
    assert result.status == "failed"
    assert result.exit_code == 126
    assert runner_called is False, "must not even attempt subprocess.run([...])"
    types = [e["event_type"] for e in events]
    assert types[-1] == "plugin.failed"
    assert "empty or whitespace" in result.message


def test_entrypoint_generic_oserror_emits_failed_126(tmp_path: Path) -> None:
    """Regression: a generic OSError (e.g. ENOEXEC) must also land on a
    `plugin.failed` event with a 126-class exit code rather than
    propagating up and crashing the caller.
    """
    program = _make_program(tmp_path)
    trust = TrustStore(root=tmp_path / "trust").grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )

    def _runner(argv, *args, **kwargs):
        raise OSError(8, "Exec format error", argv[0])

    events: list[dict] = []
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust,
        event_sink=events.append,
        correlation_id="corr-enoexec",
        subprocess_runner=_runner,
    )
    assert result.status == "failed"
    assert result.exit_code == 126
    types = [e["event_type"] for e in events]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    assert "failed to launch" in result.message
