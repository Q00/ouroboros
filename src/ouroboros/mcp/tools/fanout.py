"""Fan-out result re-entry: persisted request state + synthesizer routing.

A fan-out is one question's worth of subagent consultation. The producer (an
interview turn, a lateral panel, a code investigation) emits payloads and
registers what it expects back; the host spawns the children through its own
runtime and submits their correlated outputs to
``ouroboros_submit_fanout_results``; this module decides whether the
consultation is finished and hands the outputs on.

Everything persisted here is **request-side**: which lanes were asked, which of
them the answer cannot be completed without, what the correlation key is. No
child output is written to disk -- results are submitted, judged complete, and
returned in the same call. That boundary is why the record needs no
sanitization: there is nothing child-authored in it to sanitize.

Three properties this module holds:

**Completion reads requiredness.** A lane advertised as ``required: false`` --
in the request schema, in the child's prompt, in the payload context -- may be
absent without pinning the fan-out at ``partial`` forever. Its absence is
reported rather than dropped silently, because a lane that was asked for and did
not answer is information the host should see.

**A lane that never ran is not a lane that found nothing.** A child that runs
and has nothing to say returns its no-op answer; a child that could not be
dispatched at all returns nothing, and against a required lane that would be an
indefinite ``partial``. The host can declare the second case, which completes
the fan-out with that lane marked undispatched. Without this, the cheapest way
for a host to finish is to invent the missing output.

**Contracted lanes are validated before their output is passed on.** A lane
whose canonical definition carries an ``answer_contract`` has its submitted
output checked against it; a violating lane is excluded from the aggregation and
reported, so a malformed or over-reaching answer does not reach the parent
session as if it were advice.

Extracted from ``mcp/tools/subagent.py`` (Q00/ouroboros#1754), which keeps
re-exports for its existing importers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from ouroboros.core.owner_only import secure_directory, write_owner_only

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.mcp.tools.subagent import SubagentPayload

log = structlog.get_logger(__name__)

_DEFAULT_FANOUT_DIR = Path.home() / ".ouroboros" / "data" / "fanout"

# Fan-out re-entry kinds — each routes to one revived synthesizer.
FANOUT_KIND_LATERAL_PERSONA_PANEL = "lateral_persona_panel"
FANOUT_KIND_CODE_INVESTIGATION = "code_investigation"
FANOUT_KIND_QUESTION_ADVISORY = "question_advisory"


@dataclass(frozen=True, slots=True)
class FanoutRecord:
    """Persisted fan-out request state, keyed by ``fanout_id``.

    Survives across MCP calls so a later ``ouroboros_submit_fanout_results``
    submission can validate its expected keys and route to the right revived
    synthesizer. ``synthesizer_input`` carries exactly the non-output argument
    each synthesizer needs: the orchestration ``entries`` list for a lateral
    persona panel, or the ``request`` mapping for a code investigation.

    ``required_keys`` is the subset of ``expected_keys`` completion cannot do
    without. ``None`` means the record predates the field, and such a record
    keeps the old all-keys gate rather than silently becoming permissive: a
    record written before requiredness existed carries no evidence about which
    of its lanes were optional, and guessing would change the completion rule
    for a fan-out already in flight.
    """

    fanout_id: str
    kind: str
    session_id: str
    correlation_key: str
    expected_keys: tuple[str, ...]
    synthesizer_input: dict[str, Any]
    required_keys: tuple[str, ...] | None = None
    question_identity: str = ""

    def gating_keys(self) -> tuple[str, ...]:
        """Return the keys whose absence blocks completion."""
        if self.required_keys is None:
            return self.expected_keys
        return self.required_keys

    def optional_keys(self) -> tuple[str, ...]:
        """Return the expected keys that may be absent."""
        gating = set(self.gating_keys())
        return tuple(key for key in self.expected_keys if key not in gating)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "fanout_id": self.fanout_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "correlation_key": self.correlation_key,
            "expected_keys": list(self.expected_keys),
            "synthesizer_input": self.synthesizer_input,
        }
        # Serialize additively: a record with no requiredness is byte-identical
        # to what earlier versions wrote, so a downgrade reads it unchanged.
        if self.required_keys is not None:
            data["required_keys"] = list(self.required_keys)
        if self.question_identity:
            data["question_identity"] = self.question_identity
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FanoutRecord:
        raw_input = data.get("synthesizer_input")
        raw_required = data.get("required_keys")
        return cls(
            fanout_id=str(data["fanout_id"]),
            kind=str(data["kind"]),
            session_id=str(data.get("session_id") or ""),
            correlation_key=str(data.get("correlation_key") or ""),
            expected_keys=tuple(str(key) for key in data.get("expected_keys") or ()),
            synthesizer_input=dict(raw_input) if isinstance(raw_input, Mapping) else {},
            required_keys=(
                tuple(str(key) for key in raw_required)
                if isinstance(raw_required, (list, tuple))
                else None
            ),
            question_identity=str(data.get("question_identity") or ""),
        )


class FanoutRegistry:
    """File-backed store for pending fan-out expected-key state.

    Reuses the interview data directory as the persistence substrate (the same
    place interview state JSON is written) rather than inventing a new layer.
    Each record is a single ``{fanout_id}.json`` file. Writes are best-effort:
    a persistence failure degrades re-entry (submissions report the fan-out as
    unknown) but never breaks the fan-out request path.

    Prefer constructing with the final directory. A caller that knows the
    resolved state dir — the composition root does, long before it builds this
    — passes it here, and :meth:`rebase_default` then has nothing to do.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or _DEFAULT_FANOUT_DIR
        self._issued = False

    @property
    def directory(self) -> Path:
        return self._dir

    def rebase_default(self, directory: Path) -> None:
        """Re-root a still-unused default-located registry onto the real data dir.

        Some wiring paths build the registry before anyone knows the resolved
        interview state dir, so a handler that DOES know it (via
        ``resolved_state_dir()``) can thread it in here. A registry constructed
        with an explicit directory is never re-rooted.

        **A registry that has already issued a record is never re-rooted
        either.** A fan-out id is a promise that a later submission can redeem
        it, and moving the directory out from under an issued id silently breaks
        that promise: the record stays at the old path and its valid submission
        comes back ``unknown_fanout_id``. That is reachable whenever a producer
        registers before the first interview turn — a lateral panel, say — and
        the interview then resolves a custom ``state_dir``. Refusing the move is
        the conservative half; constructing at the final directory is the half
        that makes the move unnecessary.
        """
        if self._dir != _DEFAULT_FANOUT_DIR:
            return
        if self._issued:
            log.warning(
                "fanout.registry.rebase_refused_after_issue",
                current=str(self._dir),
                requested=str(directory),
            )
            return
        self._dir = directory

    def _path(self, fanout_id: str) -> Path:
        return self._dir / f"{fanout_id}.json"

    def register(
        self,
        *,
        kind: str,
        session_id: str,
        correlation_key: str,
        expected_keys: list[str],
        synthesizer_input: dict[str, Any],
        fanout_id: str | None = None,
        required_keys: list[str] | None = None,
        question_identity: str = "",
    ) -> str:
        """Persist a fan-out record and return its ``fanout_id``.

        A ``fanout_id`` is generated (uuid4-backed, deterministic-friendly when
        supplied by the caller) and stamped into the returned value so the
        producer can echo it into the emitted meta. Persistence is best-effort.
        """
        resolved_id = fanout_id or f"fanout_{uuid4().hex}"
        # From here the id is public and redeemable, so the directory it was
        # written to is part of the promise. See ``rebase_default``.
        self._issued = True
        record = FanoutRecord(
            fanout_id=resolved_id,
            kind=kind,
            session_id=session_id,
            correlation_key=correlation_key,
            expected_keys=tuple(expected_keys),
            synthesizer_input=synthesizer_input,
            required_keys=None if required_keys is None else tuple(required_keys),
            question_identity=question_identity,
        )
        try:
            # A fan-out record carries the producer's ``synthesizer_input``
            # verbatim — the code-investigation request, the persona panel
            # entries — so it is the same artifact class as the transcript it
            # was derived from and takes the same writer. Registration stays
            # best-effort: an unconfirmed durability flush is logged like every
            # other migrated writer, and the caller still gets its id.
            secure_directory(self._dir)
            if not write_owner_only(
                self._path(resolved_id),
                json.dumps(record.to_dict(), ensure_ascii=False),
            ):
                log.warning(
                    "fanout.registry.durability_unconfirmed",
                    fanout_id=resolved_id,
                    kind=kind,
                )
        except OSError as exc:
            log.warning(
                "fanout.registry.persist_failed",
                fanout_id=resolved_id,
                kind=kind,
                error=str(exc),
            )
        return resolved_id

    def load(self, fanout_id: str) -> FanoutRecord | None:
        """Load a persisted fan-out record, or ``None`` if unknown/corrupt."""
        try:
            content = self._path(fanout_id).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, Mapping):
            return None
        try:
            return FanoutRecord.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None


def _fanout_identity_synthesis(aggregated_outputs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Server-side synthesizer: return the correlated outputs for the host.

    The re-entry tool does not run an LLM. Its job is to give the host the
    correlated child outputs back in dispatch order; the host performs the
    actual synthesis. This identity synthesizer preserves that contract while
    still exercising the revived synthesizer aggregation/ordering logic.
    """
    return {"aggregated_outputs": [dict(item) for item in aggregated_outputs]}


def _fanout_identity_continuation(synthesis: Any) -> dict[str, Any]:
    """Server-side interview continuation: signal readiness with the synthesis."""
    return {"ready_to_continue": True, "synthesis": synthesis}


def register_lateral_persona_fanout(
    registry: FanoutRegistry,
    *,
    session_id: str,
    payloads: list[SubagentPayload],
    correlation_key: str = "context.persona",
    fanout_id: str | None = None,
) -> str:
    """Register a lateral persona-panel fan-out for later result re-entry.

    Expected keys are the payload personas (``context.persona``); the persisted
    ``entries`` carry ``persona_id`` + ``execution_order`` so
    :func:`synthesize_lateral_persona_panel_when_complete` can order and gate
    the submitted outputs.
    """
    from ouroboros.mcp.tools.subagent import _payload_persona

    entries: list[dict[str, Any]] = []
    expected_keys: list[str] = []
    for index, payload in enumerate(payloads, start=1):
        persona = _payload_persona(payload.to_dict())
        expected_keys.append(persona)
        entries.append({"persona_id": persona, "execution_order": index})
    return registry.register(
        kind=FANOUT_KIND_LATERAL_PERSONA_PANEL,
        session_id=session_id,
        correlation_key=correlation_key,
        expected_keys=expected_keys,
        synthesizer_input={"entries": entries},
        fanout_id=fanout_id,
    )


def register_code_investigation_fanout(
    registry: FanoutRegistry,
    *,
    session_id: str,
    request: Mapping[str, Any],
    correlation_key: str = "code_facts",
    fanout_id: str | None = None,
) -> str:
    """Register a code-investigation fan-out for later result re-entry.

    Expected keys default to the request's ``required_result_ids`` (or the
    ``code_facts`` sentinel :func:`synthesize_code_investigation_when_complete`
    assumes), and the full ``request`` is persisted so the synthesizer can
    re-run its answer-contract validation on the submitted output.
    """
    required = request.get("required_result_ids")
    if isinstance(required, (list, tuple)) and required:
        expected_keys = [str(item) for item in required]
    else:
        expected_keys = ["code_facts"]
    return registry.register(
        kind=FANOUT_KIND_CODE_INVESTIGATION,
        session_id=session_id,
        correlation_key=correlation_key,
        expected_keys=expected_keys,
        synthesizer_input={"request": dict(request)},
        fanout_id=fanout_id,
    )


def register_question_advisory_fanout(
    registry: FanoutRegistry,
    *,
    session_id: str,
    payloads: list[SubagentPayload],
    correlation_key: str = "context.lane_id",
    fanout_id: str | None = None,
) -> str:
    """Register an interview question-advisory fan-out for later result re-entry.

    The advisory lanes are stamped to correlate by ``context.lane_id`` (a lane's
    persona is absent on the ``code_context`` / ``web_context`` lanes), so the
    expected keys are the lane ids carried on the emitted payloads — exactly the
    keys the stamped ``question_advisory_result_correlation_key`` tells the host
    to submit under. This is the invariant #1578 broke: the producer stamped
    ``context.lane_id`` but registered a ``code_facts`` record, so a
    contract-following host was rejected with ``correlation_mismatch``.

    Requiredness travels with the lane ids. It is read from the same payload
    context the child's prompt prints it from, so the flag the child is shown
    and the flag completion enforces cannot disagree.

    So does ``question_identity``. A contracted lane's answer carries the
    session and question it claims to be about, and the contract says that field
    "matches the originating advisory request" — a sentence nothing enforced
    until this was persisted. Advisory children run asynchronously and a host
    may have several questions in flight, so an unbound answer is one whose
    evidence can land beside a different question than the one it measured.

    Advisory lanes have no gating synthesizer (each is independent advice to make
    the human's answer easier), so submission routes to a deterministic
    aggregation that returns the correlated lane outputs in dispatch order for
    the host to synthesize.
    """
    from ouroboros.mcp.tools.subagent import _payload_lane_id

    expected_keys: list[str] = []
    required_keys: list[str] = []
    question_identity = ""
    for payload in payloads:
        data = payload.to_dict()
        lane_id = _payload_lane_id(data)
        if not lane_id or lane_id in expected_keys:
            continue
        expected_keys.append(lane_id)
        context = data.get("context")
        if isinstance(context, Mapping):
            if bool(context.get("required")):
                required_keys.append(lane_id)
            question_identity = question_identity or str(context.get("question_identity") or "")
    return registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id=session_id,
        correlation_key=correlation_key,
        expected_keys=expected_keys,
        question_identity=question_identity,
        synthesizer_input={"lane_ids": list(expected_keys)},
        fanout_id=fanout_id,
        required_keys=required_keys,
    )


def _advisory_lane_answer_contracts() -> dict[str, Mapping[str, Any]]:
    """Return the canonical ``lane_id -> answer_contract`` map.

    Looked up from the lane definitions at re-entry rather than persisted with
    the record. The contract is server-authored and versioned, so reading the
    current one keeps enforcement identical to what the server advertises today
    and keeps one more field out of durable state.
    """
    from ouroboros.orchestrator.capabilities.interview_schemas import (
        _interview_question_advisory_fanout_metadata,
    )

    contracts: dict[str, Mapping[str, Any]] = {}
    for lane in _interview_question_advisory_fanout_metadata()["lanes"]:
        contract = lane.get("answer_contract")
        if isinstance(contract, Mapping):
            contracts[str(lane["lane_id"])] = contract
    return contracts


def _validate_against_contract(
    output: Any,
    contract: Mapping[str, Any],
) -> list[str]:
    """Return contract violations for one lane's submitted output."""
    from jsonschema import Draft202012Validator

    schema = contract.get("response_model_schema")
    if not isinstance(schema, Mapping):
        return []
    if not isinstance(output, Mapping):
        return ["output is not a JSON object"]
    try:
        Draft202012Validator.check_schema(dict(schema))
    except Exception:  # noqa: BLE001 - an unenforceable contract is not a violation
        log.warning("fanout.contract.unenforceable", contract_id=contract.get("contract_id"))
        return []
    validator = Draft202012Validator(dict(schema))
    # Report the JSON path and the rule that failed, never the offending value:
    # a violation report that echoes its input turns the error channel into a
    # second copy of whatever the child produced.
    return sorted(
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.validator}"
        for error in validator.iter_errors(dict(output))
    )


def submit_fanout_results(
    registry: FanoutRegistry,
    *,
    session_id: str,
    correlation_key: str,
    results: list[Mapping[str, Any]],
    fanout_id: str,
) -> dict[str, Any]:
    """Validate + route a batch of correlated fan-out results back to synthesis.

    Contract:

    * Unknown ``fanout_id`` → ``status="unknown_fanout_id"`` (clean error).
    * A ``session_id`` / ``correlation_key`` that disagrees with the persisted
      record → ``status="correlation_mismatch"`` (clean error).
    * Missing required keys → ``status="partial"`` + ``missing_required_keys``
      (the host may resubmit with the remaining lanes).
    * Otherwise → route to the revived synthesizer for the record ``kind`` and
      return its structured outcome under ``status="complete"``, reporting any
      ``missing_optional_keys``, ``undispatched_keys`` and ``contract_violations``.

    A result entry of ``{"key": ..., "undispatched": true}`` declares a lane the
    host could not spawn at all. It is excluded from the completion gate but
    reported, which is the difference between a consultation that concluded with
    nothing to say and one that never happened. It travels in ``results`` rather
    than a separate argument so the host reports every lane it was asked to
    spawn through one list, whatever became of it.
    """
    record = registry.load(fanout_id)
    if record is None:
        return {
            "status": "unknown_fanout_id",
            "fanout_id": fanout_id,
            "error": f"No pending fan-out is registered for fanout_id={fanout_id!r}.",
        }
    if record.session_id and session_id and record.session_id != session_id:
        return {
            "status": "correlation_mismatch",
            "fanout_id": fanout_id,
            "error": "session_id does not match the registered fan-out.",
            "expected_session_id": record.session_id,
        }
    if record.correlation_key and correlation_key and record.correlation_key != correlation_key:
        return {
            "status": "correlation_mismatch",
            "fanout_id": fanout_id,
            "error": "correlation_key does not match the registered fan-out.",
            "expected_correlation_key": record.correlation_key,
        }

    provided: dict[str, Any] = {}
    declared: set[str] = set()
    for result in results:
        key = result.get("key")
        if key is None:
            continue
        # A child that never ran is declared in the same list its siblings
        # report through: one entry per lane the host was asked to spawn,
        # carrying either what it said or the fact that it could not be asked.
        if result.get("undispatched"):
            declared.add(str(key))
            continue
        provided[str(key)] = result.get("content")

    # A declaration only counts for a lane this fan-out actually asked for, and
    # never for one whose output arrived anyway.
    undispatched = tuple(
        key for key in record.expected_keys if key in declared and key not in provided
    )

    contract_violations = _contract_violations(record, provided)
    for key in contract_violations:
        provided.pop(key, None)

    missing_required = [
        key for key in record.gating_keys() if key not in provided and key not in undispatched
    ]
    if missing_required:
        return {
            "status": "partial",
            "fanout_id": fanout_id,
            "kind": record.kind,
            # ``missing_keys`` stays for hosts written against the previous
            # shape; it now names the same set as ``missing_required_keys``.
            "missing_keys": missing_required,
            "missing_required_keys": missing_required,
            "received_keys": sorted(provided),
            "expected_keys": list(record.expected_keys),
            "contract_violations": contract_violations,
        }

    completion_report = {
        "missing_optional_keys": [
            key for key in record.optional_keys() if key not in provided and key not in undispatched
        ],
        "undispatched_keys": list(undispatched),
        "contract_violations": contract_violations,
    }

    if record.kind == FANOUT_KIND_LATERAL_PERSONA_PANEL:
        from ouroboros.mcp.tools.subagent import (
            continue_interview_after_lateral_persona_synthesis,
        )

        entries = record.synthesizer_input.get("entries") or []
        outcome = continue_interview_after_lateral_persona_synthesis(
            entries,
            provided,
            _fanout_identity_synthesis,
            _fanout_identity_continuation,
        )
        return {
            "status": "complete",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "correlation_key": record.correlation_key,
            "result": outcome,
            **completion_report,
        }

    if record.kind == FANOUT_KIND_CODE_INVESTIGATION:
        from ouroboros.mcp.tools.subagent import synthesize_code_investigation_when_complete

        request = record.synthesizer_input.get("request") or {}
        outcome = synthesize_code_investigation_when_complete(
            request,
            provided,
            _fanout_identity_synthesis,
        )
        return {
            "status": "complete",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "correlation_key": record.correlation_key,
            "result": outcome,
            **completion_report,
        }

    if record.kind == FANOUT_KIND_QUESTION_ADVISORY:
        # Advisory lanes are independent advice with no gating synthesizer, so
        # aggregate the correlated outputs deterministically in dispatch (lane)
        # order and hand them back for the host to synthesize.
        lane_ids = record.synthesizer_input.get("lane_ids") or list(record.expected_keys)
        aggregated = [
            {"lane_id": lane_id, "output": provided[lane_id]}
            for lane_id in lane_ids
            if lane_id in provided
        ]
        outcome = _fanout_identity_synthesis(aggregated)
        return {
            "status": "complete",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "correlation_key": record.correlation_key,
            "result": outcome,
            **completion_report,
        }

    return {
        "status": "unknown_kind",
        "fanout_id": fanout_id,
        "kind": record.kind,
        "error": f"No synthesizer is registered for fan-out kind={record.kind!r}.",
    }


def _contract_violations(
    record: FanoutRecord,
    provided: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Return ``lane_id -> violations`` for contracted lanes that broke theirs."""
    if record.kind != FANOUT_KIND_QUESTION_ADVISORY:
        return {}
    violations: dict[str, list[str]] = {}
    for lane_id, contract in _advisory_lane_answer_contracts().items():
        if lane_id not in provided:
            continue
        output = provided[lane_id]
        errors = _validate_against_contract(output, contract)
        errors.extend(_provenance_violations(record, output))
        if errors:
            violations[lane_id] = errors
    return violations


def _provenance_violations(record: FanoutRecord, output: Any) -> list[str]:
    """Return violations for an answer that claims a different question.

    The schema can only check that ``question_identity`` is *shaped* like one.
    Shape is not provenance: ``interview-question:ffffffffffffffff`` validates
    perfectly and belongs to nothing. Advisory children run asynchronously and a
    host may hold several questions open, so an answer that is never bound to
    the request it came from is one whose numbers can be rendered beside a
    question they did not measure — which is the single thing this lane exists
    to prevent.

    Compared against the record rather than against the submission's arguments,
    because both are attacker-supplied in the same call; only the record was
    written by the producer.
    """
    if not isinstance(output, Mapping):
        return []
    problems: list[str] = []
    claimed_identity = str(output.get("question_identity") or "")
    if record.question_identity and claimed_identity != record.question_identity:
        problems.append("question_identity: does not belong to this fan-out")
    claimed_session = str(output.get("session_id") or "")
    if record.session_id and claimed_session and claimed_session != record.session_id:
        problems.append("session_id: does not belong to this fan-out")
    return problems


__all__ = [
    "FANOUT_KIND_CODE_INVESTIGATION",
    "FANOUT_KIND_LATERAL_PERSONA_PANEL",
    "FANOUT_KIND_QUESTION_ADVISORY",
    "FanoutRecord",
    "FanoutRegistry",
    "register_code_investigation_fanout",
    "register_lateral_persona_fanout",
    "register_question_advisory_fanout",
    "submit_fanout_results",
]
