"""Anonymous usage telemetry (PostHog).

Privacy contract — see TELEMETRY.md at the repository root:

- Never collects code, prompts, seed content, file paths, or arguments.
  Only closed command/status dimensions, runtime backend, version, OS, and
  failure reason codes are sent. Daily activity uses deterministic insert IDs.
  Country is derived by PostHog from the request, not sent by Ouroboros.
- Identity is a random UUID stored in ``~/.ouroboros/telemetry.json``.
  No PII, no machine fingerprinting.
- Opt out any time: ``DO_NOT_TRACK=1``, ``OUROBOROS_TELEMETRY=0``, or
  ``telemetry.enabled: false`` in ``~/.ouroboros/config.yaml``.
- Fire-and-forget: events go through a bounded queue drained by a daemon
  thread using stdlib urllib. Telemetry never raises, never blocks the
  caller, and silently drops events on any failure. The worker thread is a
  daemon, so process exit never waits on it either: events still queued or
  in flight when the process terminates are dropped, not delivered.
"""

from __future__ import annotations

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

from ouroboros import __version__
from ouroboros.mcp.failure_taxonomy import classify_failure

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


# Funnel step per retained MCP lifecycle command.
# but these get a stable ``command`` value so the interview -> seed -> run ->
# evaluate -> evolve funnel can be built without knowing tool names.
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
    "ouroboros_ralph": "ralph",
    "ouroboros_start_ralph": "ralph",
    "ouroboros_lateral_think": "unstuck",
}

# Internal CLI plumbing is excluded. MCP service activity is emitted separately
# as a daily-deduplicated `service_active` event.
_CLI_SKIP = frozenset({"dispatch", "job", "mcp"})
_CLI_FUNNEL = {"init": "interview"}

# The audited privacy contract for a direct CLI command_run's `command`,
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

# These handlers acknowledge durable background-job submission. Their return
# value says only whether work was accepted, never whether the workflow later
# completed or passed verification. Keeping them structurally separate avoids
# turning queue acceptance into a successful run in analytics.
_ASYNC_SUBMISSION_TOOLS = frozenset(
    {
        "ouroboros_start_execute_seed",
        "ouroboros_start_evolve_step",
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_ralph",
    }
)
_UNKNOWN_TOOL_NAME = "ouroboros_unknown_tool"

# The audited privacy contract for `tool`/`command`: every SHIPPED built-in
# ouroboros_* MCP tool name, and nothing else. Deliberately a static literal
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
# _TOOL_FUNNEL and _ASYNC_SUBMISSION_TOOLS must remain subsets of this set.
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
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
        "ouroboros_start_ralph",
        "ouroboros_submit_fanout_results",
    }
)
_EXTENSION_TOOL_NAME = "ouroboros_extension_tool"

_JOB_FUNNEL: dict[str, str] = {
    "execute_seed": "run",
    "evolve_step": "evolve",
    "auto": "auto",
    "evaluate": "evaluate",
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

# The audited privacy contract for workflow_outcome's `command`, mirroring
# _CANONICAL_TOOL_NAMES' rationale: JobManager.start_job() accepts an
# arbitrary job_type string from whatever code calls it -- a third-party
# extension using the same job manager included -- and
# JobTelemetryBoundary.observe fires for every terminal job regardless of
# who started it. A job_type outside this static set is registered but not
# ours, symmetric with an unrecognized MCP tool name (see
# _EXTENSION_TOOL_NAME), so it folds to a fixed literal command rather than
# being forwarded verbatim.
_CANONICAL_JOB_TYPES = frozenset(_JOB_FUNNEL) | _INTERNAL_SHIPPED_JOB_TYPES
_EXTENSION_JOB_COMMAND = "extension_job"

# Exact minimal contract paired with TELEMETRY.md. Anything else is dropped.
_CONTEXT_ALLOWLIST = frozenset({"runtime_backend"})
_COMMAND_RUN_MCP_KEYS = frozenset(
    {
        "command",
        "service",
        "status",
        "error_type",
        "$insert_id",
        "runtime_backend",
        "app_version",
        "os",
        "ci",
    }
)
_COMMAND_RUN_CLI_KEYS = frozenset(
    {
        "command",
        "service",
        "status",
        "$insert_id",
        "app_version",
        "os",
        "ci",
    }
)
_WORKFLOW_OUTCOME_KEYS = frozenset(
    {
        "command",
        "terminal_status",
        "verified",
        "failure_reason_code",
        "$insert_id",
        "runtime_backend",
        "app_version",
        "os",
        "ci",
    }
)
_SERVICE_ACTIVE_KEYS = frozenset(
    {
        "service",
        "$insert_id",
        "runtime_backend",
        "app_version",
        "os",
        "ci",
    }
)
# The AC verify-gate rejection-cause event. `cause` mirrors the orchestrator's
# closed _VERIFY_GATE_CAUSES vocabulary (SSOT pairing with
# orchestrator/verify_gate_outcome.py -- edit both together): which structural
# reason the deterministic verify gate rejected an AC attempt for. Never a
# command, path, artifact name, or output.
_AC_VERIFY_FAILED_KEYS = frozenset(
    {
        "cause",
        "runtime_backend",
        "app_version",
        "os",
        "ci",
    }
)
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
    }
)
_UNKNOWN_VERIFY_CAUSE = "unknown"
# Bound on any single string property. Dropped, not truncated -- a truncated
# value could still leak the start of a prompt or path.
_MAX_PROPERTY_STR_LEN = 200

_lock = threading.Lock()
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_context: dict[str, Any] = {}
_state_cache: dict[str, Any] | None = None
_activity_cache: set[str] = set()


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


def _state_path() -> Path:
    return Path.home() / ".ouroboros" / "telemetry.json"


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


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        batch = [item]
        try:
            while len(batch) < _BATCH_MAX:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            _post(batch)
        except Exception:
            pass
        finally:
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

    Not called automatically at process exit — the worker thread is a daemon,
    so the process can terminate with events still queued or in flight, and
    those are silently dropped. Callers that need delivery before exiting
    (e.g. a short-lived script that wants its one event to land) may call
    this explicitly.

    The one production caller is ``mcp/detached_worker.py``'s ``main()``: a
    detached job worker is a short-lived background process, not an
    interactive command, and its terminal ``workflow_outcome`` event is the
    only event the published counting rule (TELEMETRY.md) reads. Without an
    explicit flush before that process exits, the queued event would be
    dropped along with everything else still in flight, silently
    undercounting every detached run. A bounded exit delay there does not
    violate the "never blocks a command" contract above -- there is no
    interactive command left to block by the time this runs.
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


def _resolve_allowed_keys(event: str, properties: dict[str, Any] | None) -> frozenset[str] | None:
    if event == "command_run":
        service = (properties or {}).get("service")
        return _COMMAND_RUN_CLI_KEYS if service == "cli" else _COMMAND_RUN_MCP_KEYS
    if event == "workflow_outcome":
        return _WORKFLOW_OUTCOME_KEYS
    if event == "service_active":
        return _SERVICE_ACTIVE_KEYS
    if event == "ac_verify_failed":
        return _AC_VERIFY_FAILED_KEYS
    return None


def _daily_insert_id(
    event: str,
    distinct_id_value: str,
    properties: dict[str, Any],
) -> str:
    """Deduplicate DAU rows to one user/day/dimension tuple."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    dimensions = (
        properties.get("service"),
        properties.get("command"),
        properties.get("status"),
        properties.get("error_type"),
        properties.get("runtime_backend"),
    )
    material = "\0".join(str(value or "") for value in (distinct_id_value, day, event, *dimensions))
    return hashlib.sha256(material.encode()).hexdigest()


def capture(event: str, properties: dict[str, Any] | None = None) -> None:
    """Queue one event. Non-blocking, never raises, drops when queue is full.

    Enforces the event/property whitelist: an event (or command_run source
    variant) not on the disclosed table is dropped entirely, and any
    property not on that exact variant's set is dropped before the
    properties dict is built. See _resolve_allowed_keys.

    Also enforces identity fail-closed: the id is resolved via distinct_id()
    up front, before anything is built or queued, and a "" result (no
    durable identity available -- see _load_state) drops the event entirely
    rather than emitting it under a process-local candidate that never
    landed on disk.
    """
    try:
        if not is_enabled():
            return
        allowed_keys = _resolve_allowed_keys(event, properties)
        if allowed_keys is None:
            return  # not on the disclosed event table -- drop, don't guess
        resolved_id = distinct_id()
        if not resolved_id:
            return  # no durable identity this attempt -- drop, don't emit
        props = _base_properties()
        if properties:
            props.update({k: v for k, v in properties.items() if v is not None})
        if event in {"command_run", "service_active"} and "$insert_id" not in props:
            props["$insert_id"] = _daily_insert_id(event, resolved_id, props)
        props = _sanitize_properties(props, allowed_keys)
        event_payload = {
            "event": event,
            "distinct_id": resolved_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "properties": props,
        }
        if event == "service_active":
            insert_id = str(props["$insert_id"])
            with _lock:
                if insert_id in _activity_cache:
                    return
                _queue.put_nowait(event_payload)
                _activity_cache.add(insert_id)
        else:
            _queue.put_nowait(event_payload)
        _ensure_worker()
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
) -> None:
    """Capture service activity plus retained lifecycle/failure commands."""
    del duration_ms
    try:
        if not registered:
            name = _UNKNOWN_TOOL_NAME
        elif name not in _CANONICAL_TOOL_NAMES:
            name = _EXTENSION_TOOL_NAME
        command = _TOOL_FUNNEL.get(name)
        capture_service_active()
        if ok and not blocked and command is None:
            return
        if blocked:
            status = "blocked"
        elif name in _ASYNC_SUBMISSION_TOOLS:
            status = "accepted" if ok else "rejected"
        else:
            status = "succeeded" if ok else "failed"
        capture(
            "command_run",
            {
                "command": command or name.removeprefix("ouroboros_"),
                "service": "mcp",
                "status": status,
                "error_type": error_type,
            },
        )
    except Exception:
        pass


def capture_ac_verify_failed(cause: str | None) -> None:
    """Capture one deterministic verify-gate rejection with its closed cause.

    ``cause`` is produced by the orchestrator's own gate branches, but this
    boundary still folds anything outside the audited ``_AC_VERIFY_CAUSES``
    vocabulary to a fixed ``unknown`` literal -- a future gate branch (or a
    replayed/spoofed value) is counted, never forwarded verbatim.
    """
    try:
        capture(
            "ac_verify_failed",
            {"cause": cause if cause in _AC_VERIFY_CAUSES else _UNKNOWN_VERIFY_CAUSE},
        )
    except Exception:
        pass


def capture_service_active() -> None:
    """Record at most one service-active row per user/day/backend."""
    capture("service_active", {"service": "mcp"})


def capture_subagent_dispatch(properties: dict[str, Any]) -> None:
    """Compatibility no-op: subagent dispatch telemetry is no longer collected."""
    del properties


def capture_job_outcome(
    job_id: str,
    job_type: str,
    *,
    terminal_status: str,
    result_meta: dict[str, Any] | None = None,
) -> None:
    """Capture a durable background-job terminal transition.

    ``command_run`` submission receipts and durable outcomes are deliberately
    different events. Evaluation completion is also different from verified
    success: only an explicit ``final_approved is True`` earns ``verified``.
    The PostHog insert id is a one-way digest, so retries can be de-duplicated
    without disclosing the internal job identifier.

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
        if resolution is not None:
            properties["failure_reason_code"] = resolution.reason_code.value
        capture("workflow_outcome", properties)
    except Exception:
        pass


def capture_cli_command(subcommand: str | None) -> None:
    """Capture a direct ``ooo <subcommand>`` invocation.

    ``subcommand`` is caller-controlled the same way an MCP tool name or a
    job_type is: ``_PluginAwareGroup`` resolves any name not in the static
    command table as a dynamically-installed plugin command, so an
    unaudited value must never carry an identifying name through
    ``command`` -- see ``_CANONICAL_CLI_COMMANDS``.
    """
    try:
        if not subcommand or subcommand in _CLI_SKIP:
            return
        command = (
            _CLI_FUNNEL.get(subcommand, subcommand)
            if subcommand in _CANONICAL_CLI_COMMANDS
            else _EXTENSION_CLI_COMMAND
        )
        capture(
            "command_run",
            {
                "command": command,
                "service": "cli",
                "status": "invoked",
            },
        )
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
    global _state_cache
    with _lock:
        _state_cache = None
        _context.clear()
        _activity_cache.clear()


__all__ = [
    "capture",
    "capture_ac_verify_failed",
    "capture_cli_command",
    "capture_job_outcome",
    "capture_service_active",
    "capture_tool_call",
    "distinct_id",
    "flush",
    "is_enabled",
    "set_context",
    "capture_subagent_dispatch",
    "show_first_run_notice",
]
