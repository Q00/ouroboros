"""Anonymous usage telemetry (PostHog).

Privacy contract — see TELEMETRY.md at the repository root:

- Never collects code, prompts, seed content, file paths, or arguments.
  Only closed command/status dimensions, counts, runtime backend, version,
  OS, and failure reason codes are sent. Country is derived by PostHog from
  the request, not sent by Ouroboros.
- Identity is a random UUID stored in ``~/.ouroboros/telemetry.json``.
  No PII, no machine fingerprinting.
- Opt out any time: ``DO_NOT_TRACK=1``, ``OUROBOROS_TELEMETRY=0``, or
  ``telemetry.enabled: false`` in ``~/.ouroboros/config.yaml``.
- Only ``usage_daily`` and ``workflow_outcome`` reach the network from this
  module (``_NETWORK_EVENTS``). Commands, MCP starts and fan-out dispatches
  are counted in a local daily rollup (``telemetry_rollup``) sent once per
  day; verify-gate, acceptance and runtime-drift observations are folded
  into their run's ``workflow_outcome``.
- Ordinary capture queues events asynchronously for a daemon thread using
  stdlib urllib, without waiting for delivery. Explicit exit flushes may
  delay CLI return by up to 1.5 seconds or detached worker exit by up to
  5 seconds. These waits are best effort, not delivery guarantees; events
  still queued or in flight when the process exits may be dropped, except
  a daily rollup, which stays on disk until a later process delivers it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import ssl
import threading
import time
from typing import Any
import urllib.request
import uuid

from ouroboros import __version__, telemetry_rollup
from ouroboros.mcp.failure_taxonomy import (
    RUN_FAILURE_CAUSES,
    UNKNOWN_RUN_FAILURE_CAUSE,
    classify_failure,
)

# PostHog project API key. This is a *public, write-only* key (it can only
# ingest events, never read them) — embedding it in an open-source repo is
# the documented PostHog pattern. An empty constant (e.g. stripped in a fork)
# disables telemetry entirely; a blank/unset OUROBOROS_POSTHOG_API_KEY env
# var does not — it just falls back to this embedded key (see _api_key()).
_EMBEDDED_API_KEY = "phc_mSoetD4ExLDDCi3vNua635NhwRTgHfRaCG9WYNKmrvv5"
_DEFAULT_HOST = "https://us.i.posthog.com"

_QUEUE_MAX = 256
_BATCH_MAX = 25
_HTTP_TIMEOUT_SECONDS = 4.0


# Lifecycle MCP tools and the stable ``command`` each counts under, so the
# interview -> seed -> run -> evaluate -> evolve funnel can be built without
# knowing tool names.
_TOOL_FUNNEL: dict[str, str] = {
    "ouroboros_interview": "interview",
    "ouroboros_pm_interview": "pm",
    "ouroboros_generate_seed": "seed",
    "ouroboros_execute_seed": "run",
    "ouroboros_start_execute_seed": "run",
    "ouroboros_evolve_step": "evolve",
    "ouroboros_start_evolve_step": "evolve",
    "ouroboros_evolve_rewind": "evolve",
    "ouroboros_auto": "auto",
    "ouroboros_start_auto": "auto",
    "ouroboros_evaluate": "evaluate",
    "ouroboros_start_evaluate": "evaluate",
    "ouroboros_qa": "qa",
    "ouroboros_start_qa": "qa",
    "ouroboros_ralph": "ralph",
    "ouroboros_start_ralph": "ralph",
    "ouroboros_lateral_think": "unstuck",
}

# Internal CLI plumbing is not counted: `mcp` (a host starting the server is
# counted as `mcp_serve_starts`), `job` and `dispatch` (called by tools, not
# typed by people).
_CLI_SKIP = frozenset({"dispatch", "job", "mcp"})
_CLI_FUNNEL = {"init": "interview"}

# The audited privacy contract for a CLI command's `cmd_*` usage key,
# mirroring _CANONICAL_TOOL_NAMES'/_CANONICAL_JOB_TYPES' rationale:
# `_PluginAwareGroup.get_command()` (src/ouroboros/cli/main.py) resolves any
# name NOT in this static table as a dynamically-installed plugin command
# via build_plugin_dispatch_command, so `ctx.invoked_subcommand` is exactly
# as caller-controlled as an MCP tool name or a job_type -- a plugin can be
# named anything. `_CLI_SKIP` above is a separate, earlier gate (product
# reasons, not privacy) and does not affect which names are audited here.
#
# Derived programmatically, not guessed: `typer.main.get_command(app)`
# converts the real Typer app into its underlying click Group exactly the
# way the actual `ooo` entrypoint does, and `.commands.keys()` is the
# statically-registered set -- `_PluginAwareGroup`'s dynamic fallback only
# triggers for names NOT in that dict, so this enumeration inherently
# excludes plugin dispatch. 28 names: every `app.command(name=...)` /
# `app.add_typer(..., name=...)` registration plus two hidden top-level
# aliases (`monitor`, `dispatch`).
_CANONICAL_CLI_COMMANDS = frozenset(
    {
        "artifacts",
        "auto",
        "cancel",
        "cleanup",
        "codex",
        "config",
        "detect",
        "doctor",
        "dispatch",
        "gui",
        "harness",
        "init",
        "interview",
        "job",
        "mcp",
        "monitor",
        "plugin",
        "pm",
        "qa",
        "resume",
        "run",
        "seed",
        "service",
        "setup",
        "status",
        "tui",
        "uninstall",
        "update",
        "workflow-ir",
        "zcode",
    }
)
_EXTENSION_CLI_COMMAND = "extension_command"

# Every SHIPPED built-in ouroboros_* MCP tool name, and nothing else. Each one
# is classified below as counted usage (_USAGE_TOOLS) or not (_UNCOUNTED_TOOLS),
# so a new tool cannot start counting, or silently stop, without a decision. Deliberately a static literal
# set, NOT the adapter's mutable tool registry -- `register_tool()` accepts
# arbitrary names (extensions, custom tools registered by a plugin/host),
# and a registered-but-not-ours name is exactly as caller-controlled as an
# unregistered one from telemetry's point of view. The charset backstop
# above only rejects garbage shapes; this rejects anything that merely
# *looks* like a real tool but isn't one of the ones this project ships and
# has actually audited for privacy-safe naming.
#
# Derived by enumerating BOTH real composition entry points, not guessed:
# 1. create_ouroboros_server()'s default tool_handlers assembly
#    (src/ouroboros/mcp/server/adapter.py) -- the primary `ouroboros mcp
#    serve` path, read via `{h.definition.name for h in tool_handlers}`.
# 2. runtime_tool_composition.py's explicit tuple -- a second, narrower
#    composition path (used for a different runtime mode) that additionally
#    registers `ouroboros_checklist_verify`, a genuine top-level tool absent
#    from (1)'s default composition.
# _TOOL_FUNNEL must remain a subset of this set.
_CANONICAL_TOOL_NAMES = frozenset(
    {
        "ouroboros_ac_dashboard",
        "ouroboros_ac_tree_hud",
        "ouroboros_auto",
        "ouroboros_brownfield",
        "ouroboros_cancel_execution",
        "ouroboros_cancel_job",
        "ouroboros_checklist_verify",
        "ouroboros_evaluate",
        "ouroboros_evolve_rewind",
        "ouroboros_evolve_step",
        "ouroboros_execute_seed",
        "ouroboros_fetch_artifact",
        "ouroboros_generate_seed",
        "ouroboros_interview",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lateral_think",
        "ouroboros_lineage_status",
        "ouroboros_measure_drift",
        "ouroboros_pm_interview",
        "ouroboros_project_status",
        "ouroboros_qa",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_ralph",
        "ouroboros_record_conductor_decision",
        "ouroboros_session_signal",
        "ouroboros_session_signal_targets",
        "ouroboros_session_status",
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_qa",
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
        "ouroboros_start_ralph",
        "ouroboros_submit_fanout_results",
    }
)

# MCP tools a person (or their agent, on their behalf) calls to do work: each
# call adds one to `cmd_<command>` and `surface_mcp` in today's rollup.
_USAGE_TOOLS: dict[str, str] = {
    **_TOOL_FUNNEL,
    "ouroboros_brownfield": "brownfield",
    "ouroboros_cancel_execution": "cancel",
    "ouroboros_cancel_job": "cancel",
}
# Polling, status, read-only and internal plumbing tools. A call to one of
# these is not usage: a host waiting on a background job calls
# ouroboros_job_wait many times per run. Only their failures are counted
# (`mcp_tool_errors`).
_UNCOUNTED_TOOLS = frozenset(
    {
        "ouroboros_ac_dashboard",
        "ouroboros_ac_tree_hud",
        "ouroboros_checklist_verify",
        "ouroboros_fetch_artifact",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lineage_status",
        "ouroboros_measure_drift",
        "ouroboros_project_status",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_record_conductor_decision",
        "ouroboros_session_signal",
        "ouroboros_session_signal_targets",
        "ouroboros_session_status",
        "ouroboros_submit_fanout_results",
    }
)

_JOB_FUNNEL: dict[str, str] = {
    "execute_seed": "run",
    "evolve_step": "evolve",
    "auto": "auto",
    "evaluate": "evaluate",
    "qa": "qa",
    "ralph": "ralph",
}

# Internal shipped job types that reach JobTelemetryBoundary.observe (and
# therefore capture_job_outcome) but aren't part of the interview -> seed ->
# run -> evolve -> auto -> evaluate funnel: detached_worker.py's
# process-lifetime probes (_run_probe / _run_nested_probe -- exercised by
# the detached-job integration suite and any process verifying a detached
# worker survives its parent exiting). Shipped and non-identifying, so
# folding them to _EXTENSION_JOB_COMMAND would mislabel a first-party
# diagnostic job as a foreign/registered-by-an-extension one. Investigated
# by grepping every `job_type=` call site under src/: execute_seed,
# evolve_step, auto, evaluate, and ralph (the _JOB_FUNNEL keys above) plus
# exactly these two are the only literals JobManager.start_job() is ever
# called with from shipped code.
_INTERNAL_SHIPPED_JOB_TYPES = frozenset({"detached_probe", "detached_nested_probe"})

# The audited privacy contract for workflow_outcome's `command`:
# JobManager.start_job() accepts an arbitrary job_type string from whatever
# code calls it -- a third-party extension using the same job manager
# included -- and JobTelemetryBoundary.observe fires for every terminal job
# regardless of who started it. A job_type outside this static set is
# registered but not ours, so it folds to a fixed literal command rather than
# being forwarded verbatim.
_CANONICAL_JOB_TYPES = frozenset(_JOB_FUNNEL) | _INTERNAL_SHIPPED_JOB_TYPES
_EXTENSION_JOB_COMMAND = "extension_job"

# Exact minimal contract paired with TELEMETRY.md. Anything else is dropped.
_CONTEXT_ALLOWLIST = frozenset({"runtime_backend"})
_BASE_KEYS = frozenset({"runtime_backend", "app_version", "os", "ci"})

# The complete set of event names this module can put on the wire. Every other
# name is dropped before anything is queued. (install.sh adds install_started
# and install_completed; nothing else sends.)
_NETWORK_EVENTS = frozenset({"usage_daily", "workflow_outcome"})

# --- workflow_outcome: one row per terminal run/job --------------------------
#
# The AC verify-gate rejection causes. SSOT pairing with the orchestrator's
# closed _VERIFY_GATE_CAUSES vocabulary (orchestrator/verify_gate_outcome.py --
# edit both together). Folded into the run's workflow_outcome as
# verify_rejections + verify_top_cause; never a command, path, or output.
_AC_VERIFY_CAUSES = frozenset(
    {
        "invalid_contract",
        "artifacts_missing",
        "artifacts_missing_found_elsewhere",
        "environment_unverifiable",
        "timeout",
        "exit_nonzero",
        "output_assertion_unmatched",
        "workspace_mutated",
        "no_evidence",
    }
)
# A frozen runtime authority input changed after a runtime initialized. SSOT
# pairing with orchestrator/runtime_drift.py ``RUNTIME_DRIFT_KINDS`` -- edit
# both together. Folded as runtime_drifts + runtime_drift_kind.
_RUNTIME_DRIFT_KINDS = frozenset(
    {
        "codex_config",
        "cli_executable",
        "skill_dispatcher",
        "mcp_handler_registry",
        "skill_dispatch_registry",
        "profile_routing",
        "baseline_unavailable",
        "attestation_timeout",
    }
)
# Why the check package did not decide a criterion the run accepted without
# evidence. SSOT pairing with boundary/no_evidence.py -- edit them together.
# Folded as no_evidence_criteria + no_evidence_top_reason.
_NO_EVIDENCE_PACKAGE_REASONS = frozenset(
    {
        "uncovered",
        "no_admitted_package",
        "no_binding",
        "no_binding_after_request",
        "no_binding_budget_exhausted",
        "script_check_advisory",
        "no_held_out_case",
        "no_reproduction_check",
    }
)
_NO_EVIDENCE_COVERAGES = frozenset({"full", "partial", "low"})
# What each controller-run artifact check observed. SSOT pairing with
# ``base_regression.ArtifactCheckOutcome``; edit them together.
_ARTIFACT_CHECK_OUTCOMES = frozenset(
    {
        "rejected",
        "exempted",
        "passed",
        "timeout",
        "base_runner_crash",
        "no_selected_files",
        "unsupported_runner",
        "not_a_test_result",
        "unavailable",
        "imported_outside_copy",
        "not_run",
        "no_admitted_command",
        "unconfirmed",
    }
)
_UNKNOWN_VALUE = "unknown"
# Folded counts are bounded ints; anything larger is clamped.
_AC_TALLY_CAP = 10_000

_WORKFLOW_OUTCOME_KEYS = (
    frozenset(
        {
            "command",
            "terminal_status",
            "verified",
            "failure_reason_code",
            "failure_cause",
            "ac_passed",
            "ac_total",
            "$insert_id",
            # Folded from the run's verify gate, acceptance and runtime (see
            # OutcomeTally). Each is present only when observed.
            "verify_rejections",
            "verify_top_cause",
            "runtime_drifts",
            "runtime_drift_kind",
            "no_evidence_criteria",
            "no_evidence_top_reason",
            "verification_coverage",
            "base_regression",
            "worker_tests",
            "artifact_failed_criteria",
        }
    )
    | _BASE_KEYS
)

# --- usage_daily: one row per install per UTC day ----------------------------
#
# ``origin`` of an ``ouroboros_generate_seed`` call, counted only for the two
# interview-less entrances: is that path used, and does it produce a Seed or
# bounce back gap questions? (``interview`` is the rest of `cmd_seed`.)
_SEED_CONTEXT_ORIGINS = frozenset({"session_context", "session_context_gap"})
_SURFACES = frozenset({"cli", "mcp", "console"})
# Set by the console (gui/jobs.py) on the CLI processes it starts for the user,
# so their usage counts under `surface_console` instead of `surface_cli`.
_SURFACE_ENV = "_OUROBOROS_TELEMETRY_SURFACE"


def _usage_key(command: str) -> str:
    return "cmd_" + command.replace("-", "_")


_USAGE_COMMANDS = (
    frozenset(_USAGE_TOOLS.values())
    | frozenset(
        _CLI_FUNNEL.get(name, name) for name in _CANONICAL_CLI_COMMANDS if name not in _CLI_SKIP
    )
    | {_EXTENSION_CLI_COMMAND}
)
_USAGE_COUNT_KEYS = (
    frozenset(_usage_key(command) for command in _USAGE_COMMANDS)
    | frozenset(f"surface_{surface}" for surface in _SURFACES)
    | frozenset(f"seed_{origin}" for origin in _SEED_CONTEXT_ORIGINS)
    | {"mcp_serve_starts", "subagent_dispatches", "mcp_tool_errors"}
)
_USAGE_DAILY_KEYS = _USAGE_COUNT_KEYS | {"day"} | _BASE_KEYS

# Bound on any single string property. Dropped, not truncated -- a truncated
# value could still leak the start of a prompt or path.
_MAX_PROPERTY_STR_LEN = 200

_lock = threading.Lock()
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_context: dict[str, Any] = {}
_state_cache: dict[str, Any] | None = None
# Delivery callbacks for queued rollup events, keyed by event uuid. The worker
# calls each with True (posted) or False (failed) once its batch settles.
_delivery_callbacks: dict[str, Callable[[bool], None]] = {}
# UTC day this process last checked the rollup for days ready to send.
_rollup_checked_day: str | None = None


def _api_key() -> str:
    return os.environ.get("OUROBOROS_POSTHOG_API_KEY", "").strip() or _EMBEDDED_API_KEY


def _host() -> str:
    return os.environ.get("OUROBOROS_POSTHOG_HOST", "").strip() or _DEFAULT_HOST


def is_enabled() -> bool:
    """Whether telemetry may send events (key present + user has not opted out)."""
    if not _api_key():
        return False
    try:
        from ouroboros.config.loader import get_telemetry_enabled

        return get_telemetry_enabled()
    except Exception:
        return False


def _state_dir() -> Path:
    return Path.home() / ".ouroboros"


def _state_path() -> Path:
    return _state_dir() / "telemetry.json"


# Canonical hyphenated UUID: 8-4-4-4-12 hex, case-insensitive on read,
# lowercase on write. Deliberately nothing else -- no braces, no
# `urn:uuid:` prefix, no unhyphenated 32-hex -- so this pattern and the
# installer's shell regex (scripts/install.sh) accept exactly the same
# strings without either side needing to know the other's language.
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Salvage pattern for malformed JSON: a UUID-shaped distinct_id value can
# still be regex-extracted from otherwise-broken text (see
# _build_repair_candidate). Same character classes as _UUID_PATTERN.
_DISTINCT_ID_FIELD_PATTERN = re.compile(
    r'"distinct_id"\s*:\s*"'
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r'"'
)
_NOTICE_SHOWN_TRUE_PATTERN = re.compile(r'"notice_shown"\s*:\s*true\b')


def _canonical_distinct_id(value: Any) -> str | None:
    """Validate `value` as a canonical-hyphenated UUID; return it lowercased.

    None means `value` fails the identity contract: not a string, wrong
    shape, or a plausible-looking but non-UUID value (an email address, a
    hostname, anything). A non-UUID identity was previously accepted as
    long as it was a non-empty string and forwarded to the transport
    unsanitized -- this is the fix.
    """
    if not isinstance(value, str):
        return None
    if _UUID_PATTERN.fullmatch(value):
        return value.lower()
    return None


def _validate_state(raw: Any) -> dict[str, Any] | None:
    """Validate a parsed JSON value as telemetry state; canonicalize the id
    and structurally validate notice_shown.

    Returns `raw` with `distinct_id` replaced by its canonical lowercase
    form and `notice_shown` coerced to a literal bool, when the shape and
    the id are both valid; None otherwise (invalid id -- e.g. missing,
    nested, or not a UUID -- is still the only thing that invalidates the
    whole state; notice_shown is repaired in place, never a rejection
    reason). Centralizing this means every read path (a fresh parse in
    _load_state, _read_valid_state's re-read-and-adopt loops, the
    repair-lock re-validation in _repair_state) agrees on exactly what
    "valid" means.

    notice_shown must be a real bool, not merely truthy: show_first_run_notice()
    reads it with plain truthiness, so a corrupted value like "false" -- a
    non-empty, therefore truthy, STRING -- would otherwise be read as
    "already shown" and permanently suppress the disclosure the notice
    exists to guarantee. Anything that isn't a
    literal bool -- missing, a string (even one that spells "true"), an
    int, anything -- is coerced to False (never-shown). A benign repeated
    notice print costs nothing; treating corrupted state as "already
    disclosed" would violate the contract. This fails toward disclosure
    even when the corrupted value happened to look truthy.
    """
    if not isinstance(raw, dict):
        return None
    canonical = _canonical_distinct_id(raw.get("distinct_id"))
    if canonical is None:
        return None

    notice_shown = raw.get("notice_shown")
    if not isinstance(notice_shown, bool):
        notice_shown = False

    if canonical == raw.get("distinct_id") and notice_shown == raw.get("notice_shown"):
        return raw
    return {**raw, "distinct_id": canonical, "notice_shown": notice_shown}


def _read_valid_state(path: Path) -> dict[str, Any] | None:
    """Read and validate telemetry.json, or None if absent/unparseable/invalid."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _validate_state(raw)


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    """Write state via a same-directory temp file + os.replace (atomic swap).

    Unlike a direct ``path.write_text``, this can never leave a truncated or
    half-written file on disk for another process to read mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.{os.getpid()}.tmp"
    try:
        tmp_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _fresh_candidate(*, notice_shown: bool = False) -> dict[str, Any]:
    return {
        "distinct_id": str(uuid.uuid4()),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notice_shown": notice_shown,
    }


def _build_repair_candidate(raw_text: str) -> dict[str, Any]:
    """Build the repair candidate for an existing-but-invalid state file.

    Valid JSON with a non-UUID distinct_id (e.g. an email) has nothing to
    salvage -- the value we have IS the identity-contract violation, so
    mint fresh. Genuinely malformed (unparseable) JSON might still contain a
    UUID-shaped distinct_id in the raw bytes; salvaging that instead of
    minting a new one is what keeps a Python-side repair convergent with an
    installer-side (shell) repair of the same malformed file, rather than
    each process fragmenting identity by minting its own. notice_shown is
    preserved (best-effort textual match) if the raw text plainly contains
    `"notice_shown": true` -- a repeated first-run notice is benign, but
    losing a value we could plainly see would not be.
    """
    try:
        json.loads(raw_text)
        malformed = False
    except Exception:
        malformed = True

    if not malformed:
        return _fresh_candidate()

    match = _DISTINCT_ID_FIELD_PATTERN.search(raw_text)
    canonical = _canonical_distinct_id(match.group(1)) if match else None
    if canonical is None:
        return _fresh_candidate()

    notice_shown = bool(_NOTICE_SHOWN_TRUE_PATTERN.search(raw_text))
    return {
        "distinct_id": canonical,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notice_shown": notice_shown,
    }


def _load_state() -> dict[str, Any] | None:
    """Load (or create/repair) telemetry state, or None if identity is
    unavailable this attempt.

    None means neither an already-valid identity could be read, nor a
    repaired one could be persisted and reread back as valid (e.g. a
    read-only ~/.ouroboros). Callers must treat that as "no identity right
    now" and drop rather than emit under a process-local, never-persisted
    candidate -- two fresh processes hitting the same failure would
    otherwise each mint and report a different id for what PostHog would
    count as the same installation. A None outcome is never cached: the
    filesystem may become writable later, and every attempt is bounded file
    I/O, so the next capture() gets a fresh try instead of being stuck.
    """
    global _state_cache
    with _lock:
        if _state_cache is not None:
            return _state_cache
        path = _state_path()
        # Distinguish ABSENT (nothing to fix; race is over who creates it
        # first) from INVALID (a file exists but is corrupt/unparseable, or
        # parses fine but fails the identity contract -- e.g. a non-UUID
        # distinct_id; a create-if-not-exists primitive cannot fix either --
        # see _repair_state / _build_repair_candidate).
        file_absent = False
        raw_text: str | None
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw_text = None
            file_absent = True
        except Exception:
            raw_text = None  # exists but unreadable -- treat as invalid, not absent

        state: dict[str, Any] = {}
        if raw_text is not None:
            try:
                raw = json.loads(raw_text)
            except Exception:
                raw = None
            if raw is not None:
                validated = _validate_state(raw)
                if validated is not None:
                    state = validated
                    if isinstance(raw, dict) and raw != validated:
                        # _validate_state repaired something in place without
                        # rejecting the whole file -- a non-canonical UUID
                        # case (e.g. uppercase) or a structurally invalid
                        # notice_shown (e.g. the string "false"). Persist the
                        # repaired form so every future reader, this process
                        # included, converges on the exact same bytes instead
                        # of re-repairing on every load. Best-effort: the
                        # underlying state was already valid enough to adopt,
                        # so a failed rewrite here still means `state` is
                        # adopted below -- see _write_state's never-raises
                        # contract.
                        _write_state(validated)

        if not state.get("distinct_id"):
            candidate = (
                _fresh_candidate()
                if file_absent or raw_text is None
                else _build_repair_candidate(raw_text)
            )
            repaired = (
                _publish_new_state(path, candidate)
                if file_absent
                else _repair_state(path, candidate)
            )
            if repaired is None:
                return None  # fail closed -- see docstring; do not cache
            state = repaired

        _state_cache = state
        return state


def _publish_new_state(path: Path, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Atomically publish a freshly generated identity, then adopt the winner.

    Concurrent processes (multiple MCP sessions starting at once, the
    installer racing a first `ooo` invocation) can all observe a missing
    telemetry.json in the same instant. A plain read-then-write here would
    let each one mint its own uuid and overwrite the file, so different
    processes would end up caching different ids while only one survives on
    disk. Instead: publish the candidate via a same-directory temp file plus
    `os.link` (atomic create-if-not-exists — the content is fully written
    before the link is made, so there is no partial-write window on this
    path), falling back to O_CREAT|O_EXCL on filesystems without hard-link
    support. Either way, every process then re-reads the file and adopts
    whatever `distinct_id` actually landed there, rather than trusting its
    own candidate. This is the process-local half of the fix; the installer
    coordinates with the same create-if-not-exists protocol.

    Returns None -- never the unpersisted candidate -- if the publish AND
    every reread both failed (e.g. a read-only ~/.ouroboros): an event may
    only ever be emitted under an id that was actually durable, never a
    process-local value that a second fresh process could independently
    (and differently) mint under the same failure.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    payload = json.dumps(candidate, indent=2) + "\n"
    tmp_path = path.parent / f"telemetry.json.{os.getpid()}.tmp"
    try:
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.link(tmp_path, path)
        except FileExistsError:
            pass  # another process already published first; adopt it below
        except OSError:
            # No hard-link support (e.g. some network mounts) — fall back to
            # an atomic create-exclusive write. This has a brief
            # partial-write window between open and close, covered by the
            # bounded re-read retry below.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload.encode("utf-8"))
                finally:
                    os.close(fd)
            except FileExistsError:
                pass
            except Exception:
                pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    for attempt in range(3):
        state = _read_valid_state(path)
        if state is not None:
            return state
        if attempt < 2:
            time.sleep(0.02)
    return None


def _repair_state(path: Path, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Atomically replace a corrupt/invalid telemetry.json, then adopt the winner.

    ``_publish_new_state``'s create-if-absent primitives (``os.link`` /
    ``O_EXCL``) both refuse when the target already exists, so neither can
    fix a file that is present but unparseable or missing ``distinct_id`` --
    every process would otherwise mint its own uuid on every call, forever,
    and no id would ever persist. Exactly one process wins a repair lock,
    atomically replaces the bad file, and every process (winner and losers
    alike) adopts whatever ends up on disk afterward. Without that, retention
    and weekly-active metrics would fragment across as many ids as there were
    racing processes, every time the file happened to get corrupted.

    Returns None -- never the unpersisted candidate -- if the replace AND
    every reread both failed (e.g. a read-only ~/.ouroboros): see
    _publish_new_state's docstring for why that must fail closed rather than
    emit under a value that never actually landed on disk.
    """
    lock_path = path.with_name(path.name + ".repair.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except Exception:
        fd = None  # lock held by another repairer, or lock creation failed

    if fd is not None:
        try:
            # Re-validate under the lock: another process may have repaired
            # the file between our first read and acquiring this lock.
            state = _read_valid_state(path)
            if state is None:
                try:
                    _atomic_write(path, candidate)
                except Exception:
                    pass
                state = _read_valid_state(path)
        finally:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass
        return state

    # Someone else holds the lock: wait, bounded, for a valid file to land.
    for _ in range(10):
        time.sleep(0.02)
        state = _read_valid_state(path)
        if state is not None:
            return state

    # Lock never cleared (stale lock from a killed process) -- take over the
    # replace ourselves rather than waiting forever, accepting the tiny race.
    try:
        _atomic_write(path, candidate)
    except Exception:
        pass
    for attempt in range(3):
        state = _read_valid_state(path)
        if state is not None:
            return state
        if attempt < 2:
            time.sleep(0.02)
    return None


def _write_state(state: dict[str, Any]) -> None:
    try:
        _atomic_write(_state_path(), state)
    except Exception:
        pass


def distinct_id() -> str:
    """Stable anonymous id for this machine/user.

    Returns "" when no identity is available this attempt (see
    _load_state) -- never a process-local, unpersisted UUID. Callers must
    treat an empty string as "no identity", not as a literal id; capture()
    does exactly that, dropping the event rather than emitting it under a
    value that never actually landed on disk.
    """
    state = _load_state()
    if state is None:
        return ""
    return str(state["distinct_id"])


def _is_allowed_scalar(value: Any) -> bool:
    """Whether a property value is a plain scalar within the size bound.

    Dicts, lists, and other structured values are rejected outright: the
    whitelist covers keys, but an allowed key with an unbounded/nested value
    would still be a hole for the same reason unlisted keys are.
    """
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_PROPERTY_STR_LEN
    return False


def _sanitize_properties(props: dict[str, Any], allowed_keys: frozenset[str]) -> dict[str, Any]:
    """Keep only allowlisted keys with plain, bounded scalar values."""
    return {k: v for k, v in props.items() if k in allowed_keys and _is_allowed_scalar(v)}


def set_context(**props: Any) -> None:
    """Merge allowlisted runtime context into subsequent events."""
    with _lock:
        for key, value in props.items():
            if key in _CONTEXT_ALLOWLIST and _is_allowed_scalar(value):
                _context[key] = value


def _base_properties() -> dict[str, Any]:
    props: dict[str, Any] = {
        "app_version": __version__,
        "os": platform.system().lower(),
    }
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        props["ci"] = True
    with _lock:
        props.update(_context)
    return props


def _post(events: list[dict[str, Any]]) -> None:
    api_key = _api_key()
    if not api_key or not events:
        return
    payload = json.dumps({"api_key": api_key, "batch": events}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https scheme
        f"{_host()}/batch/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(  # noqa: S310 - fixed https scheme
        request,
        timeout=_HTTP_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    ):
        pass


def _settle_deliveries(batch: list[dict[str, Any]], delivered: bool) -> None:
    """Tell each queued rollup event's owner whether its batch was posted."""
    for event in batch:
        event_uuid = event.get("uuid")
        if not isinstance(event_uuid, str):
            continue
        with _lock:
            callback = _delivery_callbacks.pop(event_uuid, None)
        if callback is not None:
            try:
                callback(delivered)
            except Exception:
                pass


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        batch = [item]
        try:
            while len(batch) < _BATCH_MAX:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        delivered = False
        try:
            _post(batch)
            delivered = True
        except Exception:
            pass
        finally:
            # Settle before task_done so flush() returns only after a delivered
            # rollup day has been removed from disk.
            _settle_deliveries(batch, delivered)
            for _ in batch:
                _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="ouroboros-telemetry", daemon=True)
            _worker.start()


def flush(timeout: float = 1.5) -> None:
    """Best-effort wait for queued events to be sent (bounded, never raises).

    Ordinary capture is asynchronous. This explicit call may delay command
    return or process exit while waiting up to ``timeout`` seconds for the
    queue. CLI run exit paths use the default 1.5 seconds after user-facing
    output; ``mcp/detached_worker.py``'s ``main()`` uses 5 seconds before exit.

    There is no automatic exit flush: the worker thread is a daemon. A
    timeout, offline transport or process termination can leave queued or
    in-flight events undelivered. This wait does not guarantee delivery.
    """
    try:
        deadline = time.monotonic() + timeout
        with _queue.all_tasks_done:
            while _queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                _queue.all_tasks_done.wait(remaining)
    except Exception:
        pass


def _enqueue(
    payload: dict[str, Any],
    on_result: Callable[[bool], None] | None = None,
) -> None:
    """Queue one serialized event; ``on_result`` learns whether it was posted."""
    event_uuid = payload.get("uuid")
    if on_result is not None and isinstance(event_uuid, str):
        with _lock:
            _delivery_callbacks[event_uuid] = on_result
    try:
        _queue.put_nowait(payload)
    except queue.Full:
        if on_result is not None:
            with _lock:
                _delivery_callbacks.pop(str(event_uuid), None)
            on_result(False)
        return
    _ensure_worker()


# Namespace for the deterministic event uuid of a job-derived workflow_outcome.
# PostHog deduplicates on (uuid, event, distinct_id, day); it ignores $insert_id.
_OUTCOME_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/Q00/ouroboros#telemetry"
)


def capture(event: str, properties: dict[str, Any] | None = None) -> None:
    """Queue one ``workflow_outcome``. Non-blocking, never raises.

    Enforces the event/property whitelist: any other event name is dropped
    entirely (``usage_daily`` is sent only by the daily rollup), and any
    property not on ``_WORKFLOW_OUTCOME_KEYS`` is dropped before the
    properties dict is built.

    Also enforces identity fail-closed: the id is resolved via distinct_id()
    up front, before anything is built or queued, and a "" result (no
    durable identity available -- see _load_state) drops the event entirely
    rather than emitting it under a process-local candidate that never
    landed on disk.
    """
    try:
        if not is_enabled():
            return
        if event != "workflow_outcome":
            return  # not on the disclosed event table -- drop, don't guess
        resolved_id = distinct_id()
        if not resolved_id:
            return  # no durable identity this attempt -- drop, don't emit
        _send_due_usage()
        props = _base_properties()
        if properties:
            props.update({k: v for k, v in properties.items() if v is not None})
        props = _sanitize_properties(props, _WORKFLOW_OUTCOME_KEYS)
        payload: dict[str, Any] = {
            "event": event,
            "distinct_id": resolved_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "properties": props,
        }
        insert_id = props.get("$insert_id")
        if isinstance(insert_id, str):
            payload["uuid"] = str(uuid.uuid5(_OUTCOME_UUID_NAMESPACE, insert_id))
        _enqueue(payload)
    except Exception:
        pass


# --- usage_daily: the local daily rollup ------------------------------------


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _usage_dims() -> dict[str, str | bool]:
    """The dimensions stored with today's counts: version, OS, backend, CI."""
    return {
        key: value
        for key, value in _base_properties().items()
        if key in _BASE_KEYS and isinstance(value, (str, bool))
    }


def _queue_usage_daily(bucket: Mapping[str, Any], resolved_id: str) -> None:
    """Queue one claimed day; delivery deletes it, failure releases it for retry."""
    bucket_id = str(bucket["id"])
    day = str(bucket["day"])
    props = _sanitize_properties(
        {**bucket["dims"], **bucket["counts"], "day": day}, _USAGE_DAILY_KEYS
    )
    state_dir = _state_dir()

    def settle(delivered: bool) -> None:
        try:
            telemetry_rollup.settle(state_dir, bucket_id, delivered=delivered)
        except Exception:
            pass  # still leased: a later process retries it under the same uuid

    _enqueue(
        {
            "event": "usage_daily",
            "distinct_id": resolved_id,
            # The uuid is fixed per install and day and is the PostHog dedupe
            # key, so no resend can count the day twice; the timestamp places
            # the row on the day it describes, not the day it was sent.
            "uuid": telemetry_rollup.event_uuid(resolved_id, day),
            "timestamp": f"{day}T12:00:00Z",
            "properties": props,
        },
        settle,
    )


def _send_due_usage(today: str | None = None) -> None:
    """Send every rollup day that is over, checking at most once per process per day.

    Callers have already checked ``is_enabled()``. When the identity or the
    rollup lock is unavailable nothing is claimed, and the next capture in
    this process tries again.
    """
    global _rollup_checked_day
    day = today or _utc_day()
    with _lock:
        if _rollup_checked_day == day:
            return
        _rollup_checked_day = day
    resolved_id = ""
    buckets: list[dict[str, Any]] | None = None
    try:
        resolved_id = distinct_id()
        if resolved_id:
            buckets = telemetry_rollup.claim_due(_state_dir(), day, time.time())
    except Exception:
        buckets = None
    if buckets is None:
        with _lock:
            if _rollup_checked_day == day:
                _rollup_checked_day = None
        return
    for bucket in buckets:
        _queue_usage_daily(bucket, resolved_id)


def note_activity() -> None:
    """Send finished rollup days on process activity that adds no count.

    Every capture entry point that adds no count calls this (a successful
    status poll, an internal CLI command, a console request, a run
    observation), and every one that does goes through ``_record_usage``, so
    any Ouroboros activity on a later day sends the days that are over. Once
    this process has checked the rollup today it returns before reading the
    configuration, so a hot polling path stays cheap. Respects every opt-out
    (``is_enabled``), queues nothing when no day is due, and never raises.
    """
    try:
        day = _utc_day()
        with _lock:
            if _rollup_checked_day == day:
                return
        if is_enabled():
            _send_due_usage(day)
    except Exception:
        pass


def _record_usage(counts: Mapping[str, int]) -> None:
    """Add closed-vocabulary counts to today's local rollup. Never raises.

    Nothing is sent per call: the day is sent as one ``usage_daily`` row once
    it is over. Keys outside ``_USAGE_COUNT_KEYS`` and non-positive values are
    dropped. If the rollup lock is busy past its bound or the state directory
    is unwritable, this count is skipped rather than blocking the caller. A
    count for a day whose row was already claimed is dropped
    (``telemetry_rollup.DayClosed``), so a day is never sent twice.
    """
    try:
        if not is_enabled():
            return
        clean = {
            key: value
            for key, value in counts.items()
            if key in _USAGE_COUNT_KEYS and type(value) is int and value > 0
        }
        day = _utc_day()
        if clean:
            try:
                telemetry_rollup.record(_state_dir(), day, clean, _usage_dims())
            except Exception:
                pass
        _send_due_usage(day)
    except Exception:
        pass


def capture_tool_call(
    name: str,
    *,
    ok: bool,
    duration_ms: float | None = None,
    error_type: str | None = None,
    blocked: bool = False,
    registered: bool = True,
    origin: str | None = None,
) -> None:
    """Count one MCP request in today's rollup; nothing is sent per call.

    A call to a tool in ``_USAGE_TOOLS`` adds ``cmd_<command>`` and
    ``surface_mcp``; polling, status and other ``_UNCOUNTED_TOOLS`` add
    nothing. Any request that failed with an error (not a logical block such
    as ``input_required``) adds ``mcp_tool_errors``. ``origin`` is counted
    only for ``command=seed`` and only from ``_SEED_CONTEXT_ORIGINS``. A call
    that adds nothing still sends finished days (``note_activity``).
    """
    del duration_ms, error_type
    try:
        command = _USAGE_TOOLS.get(name) if registered else None
        counts: dict[str, int] = {}
        if command is not None:
            counts[_usage_key(command)] = 1
            counts["surface_mcp"] = 1
            if command == "seed" and origin in _SEED_CONTEXT_ORIGINS:
                counts[f"seed_{origin}"] = 1
        if not ok and not blocked:
            counts["mcp_tool_errors"] = 1
        if counts:
            _record_usage(counts)
        else:
            note_activity()
    except Exception:
        pass


def capture_cli_command(subcommand: str | None) -> None:
    """Count a direct ``ooo <subcommand>`` invocation in today's rollup.

    ``subcommand`` is caller-controlled the same way an MCP tool name or a
    job_type is: ``_PluginAwareGroup`` resolves any name not in the static
    command table as a dynamically-installed plugin command, so an
    unaudited value is counted as ``cmd_extension_command`` and never
    becomes a key -- see ``_CANONICAL_CLI_COMMANDS``. ``ouroboros gui`` and
    commands the console starts (``_SURFACE_ENV``) count as
    ``surface_console``; everything else as ``surface_cli``. The internal
    ``mcp``, ``job`` and ``dispatch`` commands count nothing but still send
    finished days (``note_activity``).
    """
    try:
        if not subcommand or subcommand in _CLI_SKIP:
            note_activity()
            return
        command = (
            _CLI_FUNNEL.get(subcommand, subcommand)
            if subcommand in _CANONICAL_CLI_COMMANDS
            else _EXTENSION_CLI_COMMAND
        )
        console = subcommand == "gui" or os.environ.get(_SURFACE_ENV) == "console"
        _record_usage({_usage_key(command): 1, f"surface_{'console' if console else 'cli'}": 1})
    except Exception:
        pass


def capture_mcp_serve_started(transport: str | None) -> None:
    """Count one MCP server that reached its serve loop (``mcp_serve_starts``)."""
    del transport
    _record_usage({"mcp_serve_starts": 1})


def capture_subagent_dispatch(properties: Mapping[str, Any]) -> None:
    """Count one emitted fan-out contract (``subagent_dispatches``)."""
    try:
        if properties.get("phase") == "emitted":
            _record_usage({"subagent_dispatches": 1})
        else:
            note_activity()
    except Exception:
        pass


# --- workflow_outcome folding ------------------------------------------------


def _fold(value: Any, vocabulary: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in vocabulary else _UNKNOWN_VALUE


def _top(counter: Counter[str]) -> str:
    """Most frequent label; ties go to the alphabetically first."""
    return max(sorted(counter), key=counter.__getitem__)


def _bounded_count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, _AC_TALLY_CAP)


class OutcomeTally:
    """What one run's verify gate, acceptance checks and runtime observed.

    Each observation used to be its own event. Now ``capture_*`` below adds
    it to the tally bound to the current context (``outcome_scope``), and
    ``capture_job_outcome`` drains the tally into that run's single
    ``workflow_outcome``. Observations made outside any scope are dropped.
    Thread-safe: a run's tasks and worker threads share one tally.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._verify_causes: Counter[str] = Counter()
        self._drift_kinds: Counter[str] = Counter()
        self._no_evidence_reasons: Counter[str] = Counter()
        self._coverage: str | None = None
        self._artifact_checks: tuple[str, str] | None = None
        self._artifact_failed = 0

    def _clear(self) -> None:
        self._verify_causes.clear()
        self._drift_kinds.clear()
        self._no_evidence_reasons.clear()
        self._coverage = None
        self._artifact_checks = None
        self._artifact_failed = 0

    def add_verify_rejection(self, cause: str) -> None:
        with self._lock:
            self._verify_causes[cause] += 1

    def add_runtime_drift(self, kind: str) -> None:
        with self._lock:
            self._drift_kinds[kind] += 1

    def add_no_evidence(self, reasons: Counter[str], coverage: str) -> None:
        with self._lock:
            self._no_evidence_reasons.update(reasons)
            self._coverage = coverage

    def set_artifact_checks(self, base_regression: str, worker_tests: str, failed: int) -> None:
        with self._lock:
            self._artifact_checks = (base_regression, worker_tests)
            self._artifact_failed = min(self._artifact_failed + failed, _AC_TALLY_CAP)

    def drain(self) -> dict[str, Any]:
        """The outcome properties observed so far; the tally starts over."""
        props: dict[str, Any] = {}
        with self._lock:
            if self._verify_causes:
                props["verify_rejections"] = min(self._verify_causes.total(), _AC_TALLY_CAP)
                props["verify_top_cause"] = _top(self._verify_causes)
            if self._drift_kinds:
                props["runtime_drifts"] = min(self._drift_kinds.total(), _AC_TALLY_CAP)
                props["runtime_drift_kind"] = _top(self._drift_kinds)
            if self._no_evidence_reasons:
                props["no_evidence_criteria"] = min(
                    self._no_evidence_reasons.total(), _AC_TALLY_CAP
                )
                props["no_evidence_top_reason"] = _top(self._no_evidence_reasons)
            if self._coverage is not None:
                props["verification_coverage"] = self._coverage
            if self._artifact_checks is not None:
                props["base_regression"], props["worker_tests"] = self._artifact_checks
                props["artifact_failed_criteria"] = self._artifact_failed
            self._clear()
        return props


_OUTCOME_TALLY: ContextVar[OutcomeTally | None] = ContextVar(
    "ouroboros_outcome_tally", default=None
)


def new_outcome_tally() -> OutcomeTally:
    return OutcomeTally()


@contextmanager
def outcome_scope(tally: OutcomeTally | None) -> Iterator[None]:
    """Bind ``tally`` as the current run's tally (``None`` hides an outer one).

    Tasks created inside the block copy the binding, so a job runner task
    created here keeps reporting into ``tally`` after the block exits.
    """
    token = _OUTCOME_TALLY.set(tally)
    try:
        yield
    finally:
        _OUTCOME_TALLY.reset(token)


def capture_ac_verify_failed(cause: str | None) -> None:
    """Count one verify-gate rejection, with its closed cause, for this run.

    Anything outside the audited ``_AC_VERIFY_CAUSES`` vocabulary folds to a
    fixed ``unknown`` literal -- a future gate branch (or a replayed/spoofed
    value) is counted, never forwarded verbatim.
    """
    note_activity()
    try:
        tally = _OUTCOME_TALLY.get()
        if tally is not None:
            tally.add_verify_rejection(_fold(cause, _AC_VERIFY_CAUSES))
    except Exception:
        pass


def capture_runtime_drift(kind: str | None) -> None:
    """Count one mid-run change of a frozen runtime authority input for this run."""
    note_activity()
    try:
        tally = _OUTCOME_TALLY.get()
        if tally is not None:
            tally.add_runtime_drift(_fold(kind, _RUNTIME_DRIFT_KINDS))
    except Exception:
        pass


def capture_acceptance_no_evidence(
    pairs: Iterable[tuple[str | None, str | None]],
    *,
    criterion_count: int,
    verification_coverage: str | None,
    surface: str | None,
    check_package: str | None,
    check_package_status: str | None,
    runtime_backend: str | None,
    check_package_mode: str | None = None,
) -> None:
    """Count this run's criteria accepted without evidence from any verifier.

    ``pairs`` holds one ``(package_reason, replay_reason)`` per such
    criterion. Only the count, the most common package reason (folded to
    ``_NO_EVIDENCE_PACKAGE_REASONS`` or ``unknown``) and the coverage label
    reach the run's ``workflow_outcome``; the other arguments are accepted
    for the call site's sake and dropped. Never raises.
    """
    del criterion_count, surface, check_package, check_package_status
    del runtime_backend, check_package_mode
    note_activity()
    try:
        reasons = Counter(
            _fold(package_reason, _NO_EVIDENCE_PACKAGE_REASONS) for package_reason, _ in pairs
        )
        tally = _OUTCOME_TALLY.get()
        if reasons and tally is not None:
            tally.add_no_evidence(reasons, _fold(verification_coverage, _NO_EVIDENCE_COVERAGES))
    except Exception:
        pass


def capture_acceptance_artifact_checks(
    *,
    base_regression: str | None,
    worker_tests: str | None,
    failed_criteria: int,
    criterion_count: int,
    repairs: int,
    surface: str | None,
    runtime_backend: str | None,
    exemption: str | None = None,
    exempted_tests: int = 0,
    base_regression_mode: str | None = None,
    worker_test_gate_mode: str | None = None,
    base_regression_effect: str | None = None,
    worker_tests_effect: str | None = None,
    would_fail_criteria: int = 0,
) -> None:
    """Record what this run's controller-run artifact checks observed.

    Only each check's outcome (``base_regression``, ``worker_tests``; folded
    to ``_ARTIFACT_CHECK_OUTCOMES`` or ``unknown``) and ``failed_criteria``
    reach the run's ``workflow_outcome``; the other arguments are accepted
    for the call site's sake and dropped. Never raises.
    """
    del criterion_count, repairs, surface, runtime_backend, exemption, exempted_tests
    del base_regression_mode, worker_test_gate_mode, base_regression_effect
    del worker_tests_effect, would_fail_criteria
    note_activity()
    try:
        tally = _OUTCOME_TALLY.get()
        if tally is not None:
            tally.set_artifact_checks(
                _fold(base_regression, _ARTIFACT_CHECK_OUTCOMES),
                _fold(worker_tests, _ARTIFACT_CHECK_OUTCOMES),
                _bounded_count(failed_criteria),
            )
    except Exception:
        pass


def _run_ac_tally_properties(meta: Mapping[str, Any]) -> dict[str, int]:
    """Forward ``ac_passed``/``ac_total`` only as a consistent pair of bounded ints.

    Producer: ``mcp/tools/run_ac_tally.py``. Anything malformed (non-int,
    bool, negative, passed > total, absurd size) drops both keys rather than
    forwarding a partial or spoofed tally.
    """
    passed = meta.get("ac_passed")
    total = meta.get("ac_total")
    for value in (passed, total):
        if not isinstance(value, int) or isinstance(value, bool):
            return {}
    if not (0 <= passed <= total <= _AC_TALLY_CAP):
        return {}
    return {"ac_passed": passed, "ac_total": total}


def capture_job_outcome(
    job_id: str,
    job_type: str,
    *,
    terminal_status: str,
    result_meta: dict[str, Any] | None = None,
) -> None:
    """Capture a durable background-job terminal transition.

    Evaluation completion is different from verified success: only an
    explicit ``final_approved is True`` earns ``verified``. The insert id is
    a one-way digest of the job id, and the event uuid is derived from it, so
    PostHog can de-duplicate a retry without seeing the internal identifier.
    The run's ``OutcomeTally`` bound to the current context (if any) is
    drained into the same event.

    ``job_type`` is caller-controlled the same way an MCP tool name is:
    ``JobManager.start_job()`` accepts an arbitrary string, so ``command``
    below is only ever an audited value (a real _JOB_FUNNEL stage, an
    internal shipped probe type, or the fixed ``extension_job`` literal) --
    see ``_CANONICAL_JOB_TYPES``.
    """
    try:
        normalized_status = terminal_status.strip().lower()
        meta = result_meta if isinstance(result_meta, dict) else {}
        final_approved = meta.get("final_approved")
        verified = (
            normalized_status == "completed" and job_type == "evaluate" and final_approved is True
        )
        resolution = classify_failure(normalized_status, meta)
        command = (
            _JOB_FUNNEL.get(job_type, job_type)
            if job_type in _CANONICAL_JOB_TYPES
            else _EXTENSION_JOB_COMMAND
        )
        properties: dict[str, Any] = {
            "command": command,
            "terminal_status": normalized_status,
            "verified": verified,
            "$insert_id": hashlib.sha256(f"ouroboros-job-outcome\0{job_id}".encode()).hexdigest(),
        }
        if command == "run":
            properties.update(_run_ac_tally_properties(meta))
        if resolution is not None:
            properties["failure_reason_code"] = resolution.reason_code.value
            # The fine-grained run cause (SSOT: orchestrator/run_failure_cause.py)
            # is producer-owned but folded to the closed vocabulary here anyway
            # so a replayed or spoofed value is counted, never forwarded.
            raw_cause = meta.get("failure_cause")
            if isinstance(raw_cause, str):
                properties["failure_cause"] = (
                    raw_cause if raw_cause in RUN_FAILURE_CAUSES else UNKNOWN_RUN_FAILURE_CAUSE
                )
        tally = _OUTCOME_TALLY.get()
        if tally is not None:
            properties.update(tally.drain())
        capture("workflow_outcome", properties)
    except Exception:
        pass


_NOTICE = (
    "Ouroboros collects anonymous usage data (commands, versions, success rates - "
    "never code, prompts, or file contents) to guide improvements and to publish "
    "aggregate adoption stats.\n"
    "Opt out anytime: export OUROBOROS_TELEMETRY=0  |  details: "
    "https://github.com/Q00/ouroboros/blob/main/TELEMETRY.md"
)


_NOTICE_MARKER_STALE_SECONDS = 10.0


def _claim_notice_marker(marker_path: Path) -> bool:
    """Try to become the one process that prints the first-run notice.

    True means this process won the ``O_EXCL`` create and should print and
    persist ``notice_shown``. False means someone else already holds it.

    A process can crash between creating the marker and finishing the print
    + persist step in ``show_first_run_notice``, leaving a marker behind
    with ``notice_shown`` still false forever -- every later process would
    then see the marker, assume a live claimer, and silently never disclose.
    That's distinguished from an actually-live claimer by marker age: a
    marker younger than ``_NOTICE_MARKER_STALE_SECONDS`` is plausibly still
    mid-print (return False, don't duplicate); one older than that is crash
    residue, so reclaim it once. A benign duplicate print is preferable to
    silently violating the disclosure contract.
    """
    try:
        fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        pass
    except Exception:
        return True  # best-effort marker; don't let creation failure silence the notice forever

    try:
        age = time.time() - marker_path.stat().st_mtime
    except Exception:
        return False  # can't tell; assume a live claimer rather than risk a duplicate flood

    if age < _NOTICE_MARKER_STALE_SECONDS:
        return False  # a concurrent claimer is plausibly still mid-print

    # Stale marker from a crashed claimer -- reclaim once. If someone else
    # reclaims it first, they own the print and we return quietly.
    try:
        marker_path.unlink(missing_ok=True)
        fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def show_first_run_notice() -> None:
    """Print the one-time telemetry notice to stderr (safe for MCP stdio).

    Concurrent first-use processes (multiple MCP sessions starting together)
    can all read ``notice_shown=False`` before any of them has written the
    file, so the json check alone isn't enough to keep the print to once.
    Claimed via ``_claim_notice_marker`` instead. ``notice_shown`` is
    persisted into telemetry.json as before (the installer reads that
    field), via the now-atomic ``_write_state`` -- but only *after* printing:
    a crash between the print and the persist leaves the flag false, so the
    notice may print again next time (benign duplicate) rather than the flag
    getting set with nothing ever having been shown (silent, permanent
    non-disclosure).

    If no identity is available at all (e.g. a read-only ~/.ouroboros --
    see _load_state), does nothing: no collection can happen without a
    durable id anyway, and claiming notice_shown against a state that was
    never actually persisted would be the same silent-non-disclosure
    failure mode this function exists to avoid.

    ``state.get("notice_shown")`` below is a plain truthiness check, which
    is safe because _validate_state (and every candidate constructor --
    _fresh_candidate, _build_repair_candidate) guarantees the field is
    always a literal bool by the time state reaches here: a structurally
    corrupted value fails toward disclosure (coerced to False), never
    toward silent suppression.
    """
    try:
        if not is_enabled():
            return
        state = _load_state()
        if state is None:
            return
        if state.get("notice_shown"):
            return
        marker_path = _state_path().with_name("telemetry.notice")
        if not _claim_notice_marker(marker_path):
            return

        import sys

        print(f"\n{_NOTICE}\n", file=sys.stderr)
        state["notice_shown"] = True
        _write_state(state)
    except Exception:
        pass


def _reset_for_tests() -> None:
    """Clear module caches so tests can run isolated (not public API)."""
    global _state_cache, _rollup_checked_day
    with _lock:
        _state_cache = None
        _rollup_checked_day = None
        _context.clear()
        _delivery_callbacks.clear()


__all__ = [
    "OutcomeTally",
    "capture",
    "capture_ac_verify_failed",
    "capture_acceptance_artifact_checks",
    "capture_acceptance_no_evidence",
    "capture_cli_command",
    "capture_job_outcome",
    "capture_mcp_serve_started",
    "capture_runtime_drift",
    "capture_subagent_dispatch",
    "capture_tool_call",
    "distinct_id",
    "flush",
    "is_enabled",
    "new_outcome_tally",
    "note_activity",
    "outcome_scope",
    "set_context",
    "show_first_run_notice",
]
