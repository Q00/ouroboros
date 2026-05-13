"""End-to-end contract proof for the UserLevel plugin layer.

Implements Q00/ouroboros#733. Exercises the full chain:

  CLI install
    → CLI trust  (Path 1 only — Path 2 deliberately skips this)
    → firewall.invoke_plugin  (subprocess to the fixture entrypoint)
    → ledger envelope wrapping

Paths covered (per the locked spec):
  Path 1 — read-only success (full happy-path event sequence).
  Path 2 — trust violation: ONLY plugin.failed; explicit absence of
           plugin.invoked is asserted (locked Q00/ouroboros-plugins#9 Q1).
  Path 3 — subprocess failure after trust granted; exit code in result.

The fixture lives at `tests/fixtures/plugins-fixture/`; see its README.
The fixture entrypoint is deterministic and does NOT contact GitHub.

This test asserts the v0 reference manifest does NOT declare `merge`
(per Q00/ouroboros-plugins#7 lock); upstream drift fails this test
loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from typer.testing import CliRunner

from ouroboros.cli.commands.plugin import app as plugin_app
from ouroboros.plugin.digest import canonical_tree_hash
from ouroboros.plugin.firewall import invoke_plugin
from ouroboros.plugin.ledger_adapter import (
    AUDIT_EVENT_TYPES,
    make_event_sink,
    unwrap_plugin_event,
)
from ouroboros.plugin.manifest import load_manifest
from ouroboros.plugin.trust_store import TrustStore
from ouroboros.plugin.userlevel_registry import UserLevelProgramRegistry

FIXTURE_REPO = Path(__file__).resolve().parents[2] / "fixtures" / "plugins-fixture"
FIXTURE_PLUGIN_DIR = FIXTURE_REPO / "plugins" / "github-pr-ops"


def _runner() -> CliRunner:
    return CliRunner()


def _common_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "lockfile": tmp_path / "plugins.lock",
        "trust_root": tmp_path / "trust",
        "plugin_home_root": tmp_path / "plugin_homes",
    }


def _install_fixture_plugin(tmp_path: Path) -> dict[str, Path]:
    """Run `ooo plugin install <fixture>` and return the configured paths."""
    paths = _common_paths(tmp_path)
    runner = _runner()
    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(FIXTURE_PLUGIN_DIR),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    return paths


def _grant_trust(paths: dict[str, Path], scope: str = "github:read") -> None:
    runner = _runner()
    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            scope,
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output


def _build_program_for_invocation(paths: dict[str, Path]):
    """Load the installed manifest into a fresh UserLevel registry."""
    plugin_home = paths["plugin_home_root"] / "github-pr-ops"
    manifest = load_manifest(plugin_home / "ouroboros.plugin.json")
    registry = UserLevelProgramRegistry()
    return registry.register(manifest), plugin_home


def _python_exec_runner():
    """Build a subprocess_runner that pins ``python`` to ``sys.executable``.

    Rationale: the fixture manifest's ``entrypoint.command`` is
    ``python -m github_pr_ops`` for portability across the upstream
    plugin ecosystem; if the test ran the bare ``python`` token, it
    would resolve against ``$PATH``, which on dev machines is often
    Python 2 or a different minor version than the test interpreter.
    Pinning to ``sys.executable`` makes the launch deterministic without
    pre-bundling PYTHONPATH — the firewall now runs with
    ``cwd=plugin_home`` (set when ``invoke_plugin`` receives a
    ``plugin_home`` argument), so ``python -m github_pr_ops`` resolves
    the package from the install root the same way ``plugin_dispatch``
    invokes it in production. The previous shim injected PYTHONPATH,
    which masked exactly this contract — a regression in the
    cwd-from-plugin_home plumbing would have left the test green.
    """

    def _run(argv, *args, **kwargs):
        argv = [sys.executable if a == "python" else a for a in argv]
        return subprocess.run(argv, **kwargs)

    return _run


# ---------------------------------------------------------------------------
# Pre-flight: fixture sanity (catches upstream drift before the paths run).
# ---------------------------------------------------------------------------


def test_fixture_does_not_declare_merge() -> None:
    """Per Q00/ouroboros-plugins#7 lock: v0 reference is read-only-only.
    If upstream re-introduces `merge`, the fixture must be updated AND this
    test will fail loudly so the regression is visible."""
    raw = json.loads((FIXTURE_PLUGIN_DIR / "ouroboros.plugin.json").read_text())
    command_names = [c["name"] for c in raw["commands"]]
    assert "merge" not in command_names, (
        "merge is back in the fixture; check Q00/ouroboros-plugins#7 status"
    )
    assert command_names == ["review"]


def test_fixture_manifest_validates() -> None:
    """The fixture must validate against the vendored 0.1 schema."""
    manifest = load_manifest(FIXTURE_PLUGIN_DIR / "ouroboros.plugin.json")
    assert manifest.name == "github-pr-ops"
    assert manifest.version == "0.1.0"


# ---------------------------------------------------------------------------
# Path 1 — read-only success.
# ---------------------------------------------------------------------------


def test_path_1_read_only_success(tmp_path: Path) -> None:
    paths = _install_fixture_plugin(tmp_path)
    _grant_trust(paths)

    program, plugin_home = _build_program_for_invocation(paths)
    trust_record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    # Compute the canonical tree hash of the installed plugin home and
    # plumb it through so the firewall actually runs the pre-launch
    # digest check (RFC: "Trust identity"). Production
    # ``plugin_dispatch.py`` always supplies these arguments; without
    # them this test would prove the audit-event shape only, not the
    # critical-path contract a regression in digest plumbing or
    # cwd-from-plugin_home would break.
    expected_digest = canonical_tree_hash(plugin_home)

    envelopes: list[dict] = []
    sink = make_event_sink(envelopes.append, correlation_id="e2e-path-1")
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/123"],
        trust_record=trust_record,
        event_sink=sink,
        correlation_id="e2e-path-1",
        plugin_home=plugin_home,
        expected_artifact_digest=expected_digest,
        subprocess_runner=_python_exec_runner(),
    )

    assert result.status == "success", result.message
    assert result.exit_code == 0

    # Each emitted envelope unwraps to a schema-valid audit event.
    payloads = [unwrap_plugin_event(env) for env in envelopes]
    types = [p["event_type"] for p in payloads]
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.completed"]
    # All envelopes share the correlation id (= aggregate_id).
    assert {env["aggregate_id"] for env in envelopes} == {"e2e-path-1"}
    # No raw plugin stdout (the fixture prints "ok") leaks into events.
    serialized = json.dumps(envelopes)
    assert '"status": "ok"' not in serialized
    # sha256 hash recorded.
    completed = next(p for p in payloads if p["event_type"] == "plugin.completed")
    assert "stdout_sha256" in completed["provenance"]


# ---------------------------------------------------------------------------
# Path 2 — trust violation.
# ---------------------------------------------------------------------------


def test_path_2_trust_violation_no_invoked(tmp_path: Path) -> None:
    paths = _install_fixture_plugin(tmp_path)
    # Deliberately skip _grant_trust.

    program, plugin_home = _build_program_for_invocation(paths)
    # No trust record passed (or read; either way it's None).
    trust_record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert trust_record is None  # pre-condition

    envelopes: list[dict] = []
    sink = make_event_sink(envelopes.append, correlation_id="e2e-path-2")
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/123"],
        trust_record=None,
        event_sink=sink,
        correlation_id="e2e-path-2",
    )

    assert result.status == "blocked"
    assert result.exit_code is None

    payloads = [unwrap_plugin_event(env) for env in envelopes]
    types = [p["event_type"] for p in payloads]
    # ONLY plugin.failed — explicit absence of plugin.invoked.
    assert types == ["plugin.failed"]
    assert "plugin.invoked" not in types

    # Locked Q1 message format.
    assert "plugin requires `github:read` (read_only), which is not yet trusted" in result.message
    assert "Run: ooo plugin trust github-pr-ops --scope github:read" in result.message


# ---------------------------------------------------------------------------
# Path 3 — subprocess failure after trust.
# ---------------------------------------------------------------------------


def test_path_3_subprocess_failure(tmp_path: Path) -> None:
    paths = _install_fixture_plugin(tmp_path)
    _grant_trust(paths)

    program, plugin_home = _build_program_for_invocation(paths)
    trust_record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    expected_digest = canonical_tree_hash(plugin_home)

    envelopes: list[dict] = []
    sink = make_event_sink(envelopes.append, correlation_id="e2e-path-3")
    # The fixture exits 2 when the URL contains "fail".
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/please-fail"],
        trust_record=trust_record,
        event_sink=sink,
        correlation_id="e2e-path-3",
        plugin_home=plugin_home,
        expected_artifact_digest=expected_digest,
        subprocess_runner=_python_exec_runner(),
    )

    assert result.status == "failed"
    assert result.exit_code == 2

    payloads = [unwrap_plugin_event(env) for env in envelopes]
    types = [p["event_type"] for p in payloads]
    # invoked + permission_used + failed (NOT only failed — the subprocess
    # was actually launched, in contrast with Path 2).
    assert types == ["plugin.invoked", "plugin.permission_used", "plugin.failed"]
    failed = next(p for p in payloads if p["event_type"] == "plugin.failed")
    assert failed["result"]["status"] == "failed"
    assert "code 2" in failed["result"]["message"]


# ---------------------------------------------------------------------------
# Path 4 — pre-launch digest drift refusal.
# ---------------------------------------------------------------------------


def test_path_4_digest_drift_refuses_launch(tmp_path: Path) -> None:
    """When the installed plugin home is mutated post-grant, the firewall
    MUST recompute the canonical tree hash, see drift against the lockfile-
    recorded digest, and refuse to launch the subprocess. The terminal
    event is ``plugin.failed`` with ``result.status == "trust_subject_changed"``
    and ``current_artifact_digest`` in provenance — without this branch
    a tampered install would silently invoke.

    Production ``plugin_dispatch.py`` always supplies
    ``expected_artifact_digest``; this test fails red if a regression
    drops the digest argument or short-circuits the comparison.
    """
    paths = _install_fixture_plugin(tmp_path)
    _grant_trust(paths)

    program, plugin_home = _build_program_for_invocation(paths)
    trust_record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")

    # Snapshot the digest at install time (this is what the lockfile
    # would record). Then mutate the plugin home — the firewall will
    # recompute and detect drift.
    original_digest = canonical_tree_hash(plugin_home)
    tamper = plugin_home / "github_pr_ops" / "_tamper.py"
    tamper.write_text("# adversarial post-grant mutation\n")

    envelopes: list[dict] = []
    sink = make_event_sink(envelopes.append, correlation_id="e2e-path-4")
    result = invoke_plugin(
        program,
        command_name="review",
        argv=["https://example.com/pr/123"],
        trust_record=trust_record,
        event_sink=sink,
        correlation_id="e2e-path-4",
        plugin_home=plugin_home,
        expected_artifact_digest=original_digest,
        subprocess_runner=_python_exec_runner(),
    )

    # Subprocess MUST NOT have been launched.
    assert result.status == "blocked"

    payloads = [unwrap_plugin_event(env) for env in envelopes]
    types = [p["event_type"] for p in payloads]
    # ONLY plugin.failed — the trust check refused before any
    # ``plugin.invoked`` / ``plugin.permission_used`` could fire.
    assert types == ["plugin.failed"]
    failed = payloads[0]
    assert failed["result"]["status"] == "trust_subject_changed"
    assert "current_artifact_digest" in failed["provenance"]
    assert failed["provenance"]["current_artifact_digest"] != original_digest


# ---------------------------------------------------------------------------
# Cross-cutting safety check.
# ---------------------------------------------------------------------------


def test_no_token_shaped_strings_in_any_envelope(tmp_path: Path) -> None:
    """Across all 3 paths, the envelopes must not contain token-shaped
    substrings. This is a guard rail rather than a real attack — the
    fixture entrypoint never emits tokens."""
    paths = _install_fixture_plugin(tmp_path)
    _grant_trust(paths)
    program, plugin_home = _build_program_for_invocation(paths)
    trust_record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    expected_digest = canonical_tree_hash(plugin_home)

    all_envelopes: list[dict] = []
    sink = make_event_sink(all_envelopes.append, correlation_id="e2e-tokens")
    invoke_plugin(
        program,
        command_name="review",
        argv=["url"],
        trust_record=trust_record,
        event_sink=sink,
        correlation_id="e2e-tokens",
        plugin_home=plugin_home,
        expected_artifact_digest=expected_digest,
        subprocess_runner=_python_exec_runner(),
    )
    serialized = json.dumps(all_envelopes).lower()
    for forbidden in ("ghp_", "bearer ", "x-api-key", "token=", '"token":'):
        assert forbidden.lower() not in serialized, f"{forbidden!r} found in envelope"


def test_envelopes_carry_all_emitted_event_types() -> None:
    """The audit-event type tuple covers lifecycle and hook events.

    Path 1 exercises invoked, permission_used, completed; Path 2
    exercises failed; discovered/installed/trusted are emitted by the
    manager (#731). Hook-specific names are part of the closed audit
    vocabulary even though this E2E fixture does not execute hooks.
    """
    expected = {
        "plugin.discovered",
        "plugin.installed",
        "plugin.trusted",
        "plugin.invoked",
        "plugin.permission_used",
        "plugin.completed",
        "plugin.failed",
        "plugin.hook_started",
        "plugin.hook_completed",
        "plugin.hook_failed",
        "plugin.permission_denied",
    }
    assert set(AUDIT_EVENT_TYPES) == expected
