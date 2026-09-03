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

**Which lanes are contracted is read from the code, never from the record.** A
lane that declares an ``answer_contract`` has its submitted output checked
against what this build declares, and the record has no say in it. Persisting
the contract alongside the record was tried and removed: it turned a fact the
code guarantees into a value that could be absent, and an absent contract read
as "nothing to check" rather than as the anomaly it is. Validation is therefore
driven by what was submitted rather than by a map that could be missing an
entry. A violating lane is excluded from the aggregation and reported, so a
malformed or over-reaching answer does not reach the parent session as advice.

Extracted from ``mcp/tools/subagent.py`` (Q00/ouroboros#1754), which keeps
re-exports for its existing importers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from ouroboros.core.owner_only import secure_directory, write_owner_only
from ouroboros.orchestrator.host_dispatch import FANOUT_KIND_HOST_EXECUTION

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.mcp.tools.subagent import SubagentPayload

log = structlog.get_logger(__name__)

_DEFAULT_FANOUT_DIR = Path.home() / ".ouroboros" / "data" / "fanout"

#: A fan-out id is two things at once: a public redemption token a caller hands
#: back through ``ouroboros_submit_fanout_results``, and the name of the file
#: the record lives in. Constraining it to this alphabet is what keeps those two
#: facts from meeting. A separator, a parent segment, a drive letter and an
#: absolute form are all unspellable in it, so "the record I loaded came from
#: outside the registry" is not a condition to be detected downstream — it has
#: no way to be expressed upstream. ``Path(dir) / "/tmp/forged.json"`` is
#: ``/tmp/forged.json``, and a check placed after that join is already too late.
_FANOUT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

# Fan-out re-entry kinds — each routes to one revived synthesizer.
FANOUT_KIND_LATERAL_PERSONA_PANEL = "lateral_persona_panel"
FANOUT_KIND_CODE_INVESTIGATION = "code_investigation"
FANOUT_KIND_QUESTION_ADVISORY = "question_advisory"
# ``FANOUT_KIND_HOST_EXECUTION`` (imported above) also routes here, but is not
# synthesized: a complete submission wakes the parked HostDispatchRuntime
# waiter instead (see orchestrator.host_dispatch).


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


@dataclass(frozen=True, slots=True)
class PreparedFanoutSynthesis:
    """Validated terminal inputs whose synthesis may run behind a boundary."""

    record: FanoutRecord
    fanout_id: str
    provided: dict[str, Any]
    completion_report: dict[str, Any]


class FanoutRegistry:
    """File-backed store for pending fan-out expected-key state.

    Reuses the interview data directory as the persistence substrate (the same
    place interview state JSON is written) rather than inventing a new layer.
    Each record is a single ``{fanout_id}.json`` file. A write that fails
    issues no id: the request path still proceeds, but without re-entry, which
    is the honest degradation. Handing back an id whose record is missing looks
    like success and fails at redemption instead.

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

    def _path(self, fanout_id: str) -> Path | None:
        """Return the record path for ``fanout_id``, or ``None`` if it is not one.

        The registry directory is the whole of where a record may live, so an id
        that cannot name a file inside it names nothing. Returning ``None``
        rather than raising keeps the existing read contract: an unredeemable id
        is reported as an unknown fan-out, which is what a caller holding a
        forged one should learn.
        """
        if not _FANOUT_ID_RE.fullmatch(fanout_id):
            return None
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
    ) -> str | None:
        """Persist a fan-out record and return its ``fanout_id``, or ``None``.

        ``None`` means the record was not written, so no id is issued. An id is
        a promise that a later submission can redeem it, and returning one over
        a record that does not exist breaks that promise at the worst moment --
        after the children have run, when the host submits and is told the
        fan-out is unknown. Failing to register costs the turn its re-entry;
        issuing an unredeemable id costs the turn its results.

        A ``fanout_id`` is generated (uuid4-backed, deterministic-friendly when
        supplied by the caller) and returned once its record is on disk, so the
        producer can echo into the emitted meta only what a host can redeem.
        """
        resolved_id = fanout_id or f"fanout_{uuid4().hex}"
        # Generated ids always conform; a supplied one that does not is a
        # producer bug, and issuing it would hand out a token that can never be
        # redeemed. Refused here rather than at redemption so the defect surfaces
        # where it was written -- raised rather than returned as ``None``, which
        # says "this write did not happen" about a caller that asked for the
        # impossible.
        record_path = self._path(resolved_id)
        if record_path is None:
            raise ValueError(
                "fanout_id must be 1-128 characters of [A-Za-z0-9_-]; "
                "it names a file inside the registry directory."
            )
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
            # was derived from and takes the same writer.
            secure_directory(self._dir)
            persisted = write_owner_only(
                record_path,
                json.dumps(record.to_dict(), ensure_ascii=False),
            )
        except OSError as exc:
            log.warning(
                "fanout.registry.persist_failed",
                fanout_id=resolved_id,
                kind=kind,
                error=str(exc),
            )
            return None
        if not persisted:
            log.warning(
                "fanout.registry.durability_unconfirmed",
                fanout_id=resolved_id,
                kind=kind,
            )
            return None
        # Only now is the id public and redeemable, so only now is the directory
        # it was written to part of a promise. See ``rebase_default``.
        self._issued = True
        return resolved_id

    def load(self, fanout_id: str) -> FanoutRecord | None:
        """Load a persisted fan-out record, or ``None`` if unknown/corrupt."""
        path = self._path(fanout_id)
        if path is None:
            return None
        try:
            content = path.read_text(encoding="utf-8")
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
) -> str | None:
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
) -> str | None:
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


def _advisory_synthesizer_input(
    expected_keys: list[str],
    *,
    tool_name: str | None,
    roster_repo_ids: list[str] | None,
    phase: str | None,
) -> dict[str, Any]:
    """Return the request-side state one advisory fan-out persists."""
    data: dict[str, Any] = {"lane_ids": list(expected_keys)}
    if tool_name and tool_name != "ouroboros_interview":
        data["tool_name"] = tool_name
    if roster_repo_ids is not None:
        data["roster_repo_ids"] = list(roster_repo_ids)
    if phase:
        data["phase"] = phase
    return data


def register_question_advisory_fanout(
    registry: FanoutRegistry,
    *,
    session_id: str,
    payloads: list[SubagentPayload],
    correlation_key: str = "context.lane_id",
    fanout_id: str | None = None,
    tool_name: str | None = None,
    roster_repo_ids: list[str] | None = None,
    phase: str | None = None,
) -> str | None:
    """Register an interview question-advisory fan-out for later result re-entry.

    ``tool_name`` names the issuing MCP tool when it is not the interview, and
    re-entry reads the answer contracts from that tool's catalog: a lane id
    alone does not say whose catalog, and both tools declare a ``data_context``.
    It is written only when it differs from the default so an interview record
    is byte-identical to what earlier versions wrote.

    ``roster_repo_ids`` bounds which repositories a lane may cite. It is
    per-session data the producer knew and re-entry cannot re-derive, so unlike
    the contracts it is persisted; a record without one is simply not bounded.

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
    question it claims to be about — and only the question: it asserts no
    session, because the submission envelope already binds that and a copy the
    child asserts about itself is the weaker of the two. The contract says the
    identity field "matches the originating advisory request", a sentence
    nothing enforced until this was persisted. Advisory children run
    asynchronously and a host may have several questions in flight, so an
    unbound answer is one whose evidence can land beside a different question
    than the one it measured.

    The answer contracts are not persisted with it. Which lanes are contracted
    is a property of the code, read from the code at re-entry; a copy in the
    record would be a fact the build guarantees turned into a value that could
    go missing. See ``_canonical_lane_contracts`` for what that copy cost and
    what removing it gives up.

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
        synthesizer_input=_advisory_synthesizer_input(
            expected_keys,
            tool_name=tool_name,
            roster_repo_ids=roster_repo_ids,
            phase=phase,
        ),
        fanout_id=fanout_id,
        required_keys=required_keys,
    )


def stamp_question_advisory_fanout(
    meta: dict[str, Any],
    registry: FanoutRegistry | None,
    *,
    session_id: str,
    payloads: list[SubagentPayload],
    tool_name: str | None = None,
    roster_repo_ids: list[str] | None = None,
    phase: str | None = None,
) -> None:
    """Register the advisory fan-out and stamp its id, if there is one to stamp.

    Registration and stamping are one decision, so they live in one place: an id
    reaches the host only when its record exists. With no registry there is no
    re-entry to offer; with a failed write there is an id that would answer
    ``unknown_fanout_id`` after every child had run. Both are the same absence
    from the host's side, and both leave the turn otherwise intact.
    """
    if registry is None:
        return
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id=session_id,
        payloads=payloads,
        tool_name=tool_name,
        roster_repo_ids=roster_repo_ids,
        phase=phase,
    )
    if fanout_id is not None:
        meta["question_advisory_fanout_id"] = fanout_id


def stamp_lateral_persona_fanout(
    dispatch_record: dict[str, Any],
    registry: FanoutRegistry | None,
    *,
    session_id: str,
    payloads: list[SubagentPayload],
) -> None:
    """Register the persona panel and stamp its id, on the same terms."""
    if registry is None:
        return
    fanout_id = register_lateral_persona_fanout(registry, session_id=session_id, payloads=payloads)
    if fanout_id is not None:
        dispatch_record["fanout_id"] = fanout_id


def _canonical_lane_contracts(
    tool_name: str = "ouroboros_interview",
) -> dict[str, Mapping[str, Any]]:
    """Return the ``lane_id -> answer_contract`` map this build advertises.

    Which lanes are contracted is a property of the code, and it is read from
    the code every time rather than from the record.

    The map is per tool. A lane id alone does not identify a contract once a
    second tool exists: both declare a ``data_context``, and PM's code lane is
    not the interview's. The issuing tool is recorded with the fan-out, and its
    catalog is the only thing that says what its lanes promised.

    An earlier revision of this PR persisted a copy of each contract with the
    record, so that a submission arriving after a restart or an upgrade would be
    judged by the contract its child was actually given. Two rounds of findings
    came out of that copy -- a corrupt schema, then a missing entry -- and both
    had the same shape: something that must exist became something that could be
    absent, and absence read as "nothing to check" rather than as an anomaly.

    The copy is gone because what it bought does not survive being priced. A
    fan-out lives from one interview turn to its submission, so version skew
    needs an upgrade inside that window; and every skew outcome without the copy
    is already safe -- a tightened contract rejects the old answer as a violation
    (``partial``, the host retries or the turn proceeds without that lane), and a
    loosened one accepts what this build considers acceptable, which is the
    question being asked. So the copy prevented a rare, benign ``partial``, and
    in exchange made the set of lanes that must be validated into mutable data.
    """
    from ouroboros.orchestrator.capabilities import ouroboros_tool_capability_metadata

    try:
        advisory = ouroboros_tool_capability_metadata(tool_name)["orchestration"][
            "question_advisory_fanout"
        ]
    except (KeyError, TypeError):
        # A tool that declares no advisory catalog contracts nothing. This is
        # not "nothing to check" being guessed at: there is no contracted lane.
        return {}

    contracts: dict[str, Mapping[str, Any]] = {}
    for lane in advisory["lanes"]:
        if "answer_contract" not in lane:
            continue
        contract = lane["answer_contract"]
        # A lane that declares a contract stays in this map even if what it
        # declared is unusable, as an empty contract no output can satisfy.
        # Skipping it would drop the lane from the map, and a lane absent from
        # the map is a lane nobody checks -- the declaration would then have
        # disabled the very check it asked for.
        contracts[str(lane["lane_id"])] = contract if isinstance(contract, Mapping) else {}
    return contracts


def _validate_against_contract(
    output: Any,
    contract: Mapping[str, Any],
) -> list[str]:
    """Return contract violations for one lane's submitted output.

    **An unenforceable contract fails closed.** A persisted contract with no
    schema, or one no validator will accept, used to return "no violations" —
    the child had done nothing wrong, so nothing was reported. But the caller
    reads an empty list as *validated*, and the lane then reaches the host
    carrying whatever it liked. The two conditions are opposite: "this output
    satisfies its contract" and "this contract cannot say" must not share an
    answer. Not being able to check is reported as the violation it is, which
    excludes the lane from aggregation through the channel that already exists.
    """
    from jsonschema import Draft202012Validator

    schema = contract.get("response_model_schema")
    if not isinstance(schema, Mapping):
        return ["<contract>: response_model_schema is missing or is not an object"]
    if not isinstance(output, Mapping):
        return ["output is not a JSON object"]
    try:
        Draft202012Validator.check_schema(dict(schema))
    except Exception:  # noqa: BLE001 - any rejection makes the contract unenforceable
        log.warning("fanout.contract.unenforceable", contract_id=contract.get("contract_id"))
        return ["<contract>: response_model_schema is not an enforceable schema"]
    validator = Draft202012Validator(dict(schema))
    # Report the JSON path and the rule that failed, never the offending value:
    # a violation report that echoes its input turns the error channel into a
    # second copy of whatever the child produced.
    return sorted(
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.validator}"
        for error in _reportable_errors(validator.iter_errors(dict(output)))
    )


def _reportable_errors(errors: Any) -> list[Any]:
    """Flatten a ``oneOf`` failure to the branch the answer was trying to be.

    A contract written as alternative states reports one error at the root --
    ``oneOf`` -- with each branch's reasons underneath in ``error.context``.
    Reporting the root alone says only "wrong shape"; reporting every branch's
    reasons says contradictory things, because the branch the answer was *not*
    trying to satisfy fails for reasons that would be wrong advice ("this
    should have been a no-op" to a measurement that merely carried a stray
    field).

    So the branch with the fewest complaints wins: the one the answer came
    closest to being is the one it meant, and its complaints are the ones worth
    reporting. This keeps the report as specific as it was when the contract
    was a single object -- a path and a failed rule, never a value.

    Applied recursively, because alternatives nest: the answer is one of two
    states, and a measured state's read is in turn one of two shapes (grouped or
    not, Q00/ouroboros#1825). Flattening one level turned the outer ``oneOf``
    into an inner ``oneOf`` and reported *that*, which tells the host "wrong
    shape" about a value it already knew was the wrong shape. Depth is a
    property of the contract, not of this function, so it is not counted.
    """
    flattened: list[Any] = []
    for error in errors:
        context = list(getattr(error, "context", None) or ())
        if error.validator != "oneOf" or not context:
            flattened.append(error)
            continue
        by_branch: dict[int, list[Any]] = {}
        for sub in context:
            by_branch.setdefault(sub.schema_path[0] if sub.schema_path else 0, []).append(sub)
        flattened.extend(_reportable_errors(min(by_branch.values(), key=_branch_distance)))
    return flattened


def _branch_distance(branch_errors: list[Any]) -> tuple[int, int]:
    """Rank a branch by how far the answer was from being it.

    Complaint count alone ties whenever both branches object once, and the tie
    was broken by declaration order -- which picked wrong. An answer that
    declared ``group_by`` and omitted a label got "group_by is not an allowed
    field" from the ungrouped branch, when the advice it needed was "label your
    values". That is exactly the wrong-branch advice this flattening exists to
    avoid, arriving through the tie instead of through the count.

    So a complaint about a field the answer *wrote* breaks the tie against its
    branch. Refusing what the author put there is a branch saying "you did not
    mean me"; asking for something they left out is a branch saying "you meant
    me and are not finished". The second is the advice worth giving, and the
    ordering generalises: it reads only which kind of rule failed, not which
    contract this happens to be.
    """
    refusals = sum(1 for error in branch_errors if error.validator == "additionalProperties")
    return len(branch_errors), refusals


def prepare_fanout_results(
    registry: FanoutRegistry,
    *,
    session_id: str,
    correlation_key: str,
    results: list[Mapping[str, Any]],
    fanout_id: str,
) -> dict[str, Any] | PreparedFanoutSynthesis:
    """Validate a batch and return either a reply or terminal synthesis inputs.

    Contract:

    * Unknown ``fanout_id`` → ``status="unknown_fanout_id"`` (clean error).
    * A ``session_id`` / ``correlation_key`` that disagrees with the persisted
      record → ``status="correlation_mismatch"`` (clean error).
    * Missing required keys → ``status="partial"`` + ``missing_required_keys``
      (the host may retry with the complete set — see below).

    **Every call is judged whole.** ``provided`` is built from this call's
    ``results`` alone; nothing a previous call carried is retained, because
    retaining it would make the record hold child-authored output. That is the
    durable result state RFC #1754 defers to a later slice along with the
    sanitization obligation it brings -- a submitted lane can carry a name or an
    address inside its content, and the record on ``main`` has never held
    anything a child wrote.

    So a retry after ``partial`` resubmits every lane the host holds, not only
    the ones just reported missing. The host is the one place all of them exist
    at once, and asking it to send what it already has costs nothing; the
    alternative buys convenience with a durable copy of unsanitized child text.
    This is stated in the tool description, the ``results`` parameter, and
    ``skills/interview/SKILL.md`` in the same words, because a partial reply
    that reads as "send the rest" traps a sequential host in a loop that never
    completes.
    * Otherwise → route to the revived synthesizer for the record ``kind`` and
      return its structured outcome under ``status="complete"``, reporting any
      ``missing_optional_keys``, ``undispatched_keys`` and ``contract_violations``.

    A result entry of ``{"key": ..., "undispatched": true}`` declares a lane the
    host could not spawn at all. It is excluded from the completion gate but
    reported, which is the difference between a consultation that concluded with
    nothing to say and one that never happened. It travels in ``results`` rather
    than a separate argument so the host reports every lane it was asked to
    spawn through one list, whatever became of it. Exactly that shape, though:
    an ``undispatched`` that is not the literal ``true``, or one arriving with
    ``content``, returns ``status="invalid_result_entry"`` rather than being
    read for what it might have meant. So does a lane reported twice: two
    entries for one lane are two statements about it, and choosing between
    them by list position is not a reading this can defend.
    """
    record = registry.load(fanout_id)
    if record is None:
        return {
            "status": "unknown_fanout_id",
            "fanout_id": fanout_id,
            "error": f"No pending fan-out is registered for fanout_id={fanout_id!r}.",
        }
    # The record decides what must be proven, and an omitted value is a mismatch
    # rather than a waiver. Both checks used to require the *submitted* value to
    # be truthy too, so a caller who simply left the argument out skipped them --
    # and the public handler turns every omission into ``""``, which made "left
    # it out" the cheapest way past the envelope rather than the hardest.
    #
    # This is the envelope contracted lanes lean on: their answers assert no
    # session of their own precisely because it is settled here, so a binding
    # that any caller can decline by silence is not a binding at all.
    #
    # A record holding neither value -- a producer that ran without a session,
    # written before the field, or keyed nothing -- has nothing to bind against
    # and keeps today's behavior. What the producer recorded is what is demanded,
    # so tightening cannot reject a submission whose contract was never made.
    if record.session_id and record.session_id != session_id:
        return {
            "status": "correlation_mismatch",
            "fanout_id": fanout_id,
            "error": "session_id does not match the registered fan-out.",
            "expected_session_id": record.session_id,
        }
    if record.correlation_key and record.correlation_key != correlation_key:
        return {
            "status": "correlation_mismatch",
            "fanout_id": fanout_id,
            "error": "correlation_key does not match the registered fan-out.",
            "expected_correlation_key": record.correlation_key,
        }

    provided: dict[str, Any] = {}
    declared: set[str] = set()
    invalid: list[str] = []
    for index, result in enumerate(results):
        # An entry that is not an object, or that names no lane, is reported by
        # position rather than dropped: it has no key to be reported under, and
        # silently discarding it let an otherwise complete submission come back
        # `complete` while one lane had been mis-serialised into nothing.
        if not isinstance(result, Mapping):
            invalid.append(f"<results[{index}]>")
            continue
        key = result.get("key")
        if key is None:
            invalid.append(f"<results[{index}]>")
            continue
        # One lane, one entry. Two measurements for one lane used to be a plain
        # dict assignment, so the later entry won and the earlier one vanished
        # unreported — list order chose the number the interview saw. Answered
        # *and* declared undispatched resolved in content's favour whichever
        # order it arrived in, which was deterministic but undeclared: a
        # precedence rule no contract states, applied to a host that has told
        # us two opposite things about one lane.
        #
        # Neither is a report this can rank. Refused rather than ranked, with
        # the malformed entries below, so the host is told which lane it
        # double-reported instead of being given a number chosen for it.
        if str(key) in declared or str(key) in provided:
            invalid.append(str(key))
            continue
        # A child that never ran is declared in the same list its siblings
        # report through: one entry per lane the host was asked to spawn,
        # carrying either what it said or the fact that it could not be asked.
        #
        # The declaration is read as the JSON literal it is documented to be,
        # not for truthiness. `"undispatched": "false"` is a non-empty string
        # and would otherwise have declared the lane undispatched -- a required
        # lane excused by a value that says the opposite of what it does. A key
        # whose whole job is to excuse a lane cannot be satisfied by accident,
        # so anything other than `true` is refused rather than interpreted, and
        # an entry claiming both that its child never ran and what it returned
        # is refused with it: those are opposite reports of the same lane.
        if "undispatched" in result:
            if result["undispatched"] is not True or "content" in result:
                invalid.append(str(key))
                continue
            declared.add(str(key))
            continue
        # An entry that reports neither is a lane satisfied by nothing: no
        # output, and no statement that there was none to have. That is cheaper
        # than inventing the missing output, which is the incentive the
        # undispatched declaration exists to remove.
        if "content" not in result:
            invalid.append(str(key))
            continue
        provided[str(key)] = result["content"]

    # Every bad entry at once. Reporting the first would make a host with three
    # malformed entries send three submissions to learn three facts, which is
    # the loop the cumulative-retry contract above exists to avoid.
    if invalid:
        return {
            "status": "invalid_result_entry",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "error": (
                'each result must be either {"key": <lane>, "content": ...} '
                'or exactly {"key": <lane>, "undispatched": true}, '
                "and each lane may appear once."
            ),
            "invalid_keys": invalid,
        }

    # A declaration only counts for a lane this fan-out actually asked for.
    #
    # ``not in provided`` is kept but no longer filters anything: refusing a
    # repeated key above leaves these two sets disjoint, so a lane cannot be
    # both declared and answered by the time this runs. It used to be the rule
    # that settled that collision silently. Left in place as a guard rather
    # than deleted on the strength of an argument made one screen away — but a
    # guard, not a check: if the invariant broke, this would go back to
    # dropping the lane quietly instead of saying so.
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
            # A list of what is missing reads as a list of what to send next.
            # It is not: this call retained nothing, so a retry carrying only
            # these keys reports the ones just received as missing instead --
            # a loop that never completes. Said here as a constant because the
            # reply is the one place a host is certainly reading.
            "retry_contract": (
                "Resubmit every lane you hold, not only the missing ones. "
                "No submitted output is retained between calls."
            ),
        }

    completion_report = {
        "missing_optional_keys": [
            key for key in record.optional_keys() if key not in provided and key not in undispatched
        ],
        "undispatched_keys": list(undispatched),
        "contract_violations": contract_violations,
    }

    if record.kind not in {
        FANOUT_KIND_LATERAL_PERSONA_PANEL,
        FANOUT_KIND_CODE_INVESTIGATION,
        FANOUT_KIND_QUESTION_ADVISORY,
        FANOUT_KIND_HOST_EXECUTION,
    }:
        return {
            "status": "unknown_kind",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "error": f"No synthesizer is registered for fan-out kind={record.kind!r}.",
        }
    return PreparedFanoutSynthesis(
        record=record,
        fanout_id=fanout_id,
        provided=provided,
        completion_report=completion_report,
    )


def synthesize_fanout_results(prepared: PreparedFanoutSynthesis) -> dict[str, Any]:
    """Run terminal synthesis for an already validated fan-out submission."""

    record = prepared.record
    fanout_id = prepared.fanout_id
    provided = prepared.provided
    completion_report = prepared.completion_report
    if record.kind == FANOUT_KIND_LATERAL_PERSONA_PANEL:
        from ouroboros.mcp.tools.citation_check import audit_citations
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
        # Deep-tier citation gate (grounded-lateral RFC D4): audit the URLs
        # personas cited in their fenced evidence blocks. Withhold-only — a
        # panel with no evidence blocks touches no network and gains no key,
        # and an unreachable citation is marked for the synthesizer to demote,
        # never a synthesis failure.
        try:
            audit = audit_citations(
                output if isinstance(output, str) else json.dumps(output, default=str)
                for output in provided.values()
            )
        except Exception:
            audit = None
        response = {
            "status": "complete",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "correlation_key": record.correlation_key,
            "result": outcome,
            **completion_report,
        }
        if audit is not None:
            response["citation_audit"] = audit
        return response

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
        # Request provenance lets later interview turns identify the same-session
        # start snapshot without trusting a child's free-form output to assert it.
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
            "provenance": {
                "session_id": record.session_id,
                "phase": str(record.synthesizer_input.get("phase") or ""),
                "question_identity": record.question_identity,
            },
            "result": outcome,
            **completion_report,
        }

    if record.kind == FANOUT_KIND_HOST_EXECUTION:
        # Defensive identity path only: the production handler routes execution
        # submissions to the HostDispatchBridge *before* synthesis, so this
        # branch is reached only by direct callers of the plain function.
        return {
            "status": "complete",
            "fanout_id": fanout_id,
            "kind": record.kind,
            "correlation_key": record.correlation_key,
            "result": _fanout_identity_synthesis(
                [{"lane_id": key, "output": provided[key]} for key in sorted(provided)]
            ),
            **completion_report,
        }

    raise RuntimeError(f"prepared fan-out kind has no synthesizer: {record.kind!r}")


def submit_fanout_results(
    registry: FanoutRegistry,
    *,
    session_id: str,
    correlation_key: str,
    results: list[Mapping[str, Any]],
    fanout_id: str,
) -> dict[str, Any]:
    """Validate and synchronously synthesize a complete fan-out submission."""

    prepared = prepare_fanout_results(
        registry,
        session_id=session_id,
        correlation_key=correlation_key,
        results=results,
        fanout_id=fanout_id,
    )
    if isinstance(prepared, dict):
        return prepared
    return synthesize_fanout_results(prepared)


def _contract_violations(
    record: FanoutRecord,
    provided: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Return ``lane_id -> violations`` for contracted lanes that broke theirs.

    Driven by what was submitted, not by the contract map. Iterating the map
    made a lane's absence from it into silence: the loop never visited that
    lane, so "no contract" produced the same result as "checked and fine" --
    and the provenance check below rides here too, so a lane skipped this way
    lost its question binding as well as its schema.

    Now every submitted lane is visited and asked whether it is contracted. A
    lane the code declares uncontracted (``code_context``, ``web_context``)
    passes through, which is what those lanes are; a lane the code declares
    contracted is checked, and there is no third answer for it to fall into.
    """
    if record.kind != FANOUT_KIND_QUESTION_ADVISORY:
        return {}
    contracts = _canonical_lane_contracts(
        str(record.synthesizer_input.get("tool_name") or "ouroboros_interview")
    )
    violations: dict[str, list[str]] = {}
    for lane_id, output in provided.items():
        if lane_id not in contracts:
            continue
        errors = _validate_against_contract(output, contracts[lane_id])
        errors.extend(_provenance_violations(record, output))
        errors.extend(_roster_violations(record, output))
        errors.extend(_aggregate_violations(output))
        if errors:
            violations[lane_id] = errors
    return violations


def _aggregate_violations(output: Any) -> list[str]:
    """Return violations for grouped values that are not one number per category.

    Two numbers under the same label is a row list wearing an aggregate's name --
    the shape ``group_by`` being categorical exists to prevent, reached by
    repeating a category instead of grouping by an identifier. The schema cannot
    say it: Draft 2020-12 has ``uniqueItems`` for whole items and nothing for
    uniqueness by property, so a check that claimed it there would be a rule
    that reads as enforced and is not.

    Only duplication is judged. Which categories appear, and how many, is the
    read's business.
    """
    if not isinstance(output, Mapping):
        return []
    reads = output.get("read_requests")
    if not isinstance(reads, list):
        return []
    problems: list[str] = []
    for index, read in enumerate(reads):
        if not isinstance(read, Mapping):
            continue
        values = read.get("values")
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                continue
            group = value.get("group")
            if not isinstance(group, str):
                continue
            if group in seen:
                problems.append(f"read_requests/{index}/values: repeats a group")
                break
            seen.add(group)
    return problems


def _roster_violations(record: FanoutRecord, output: Any) -> list[str]:
    """Return violations for an ``examined`` entry outside or repeating the roster.

    The schema can only check that a ``repo_id`` is *shaped* like one, and shape
    is not membership. This is what makes the roster a boundary rather than a
    suggestion: where a lane may look stays open, what it may hand back as
    evidence is closed, and the decision is made from the value alone.

    There is one list to judge rather than two. Scope and claim used to be
    ``examined_repository_ids`` and ``evidence[]``, checked separately against
    the roster and never against each other, so an answer could declare one
    repository examined while citing another's code and pass. They are folded
    into per-repository entries now, which makes that contradiction unspellable;
    what is left for this function is membership, and it is the same check it
    always was.

    Uniqueness is checked here for the reason ``_aggregate_violations`` above
    gives: Draft 2020-12 has ``uniqueItems`` for whole items and nothing for
    uniqueness by property, so two entries for one repository -- one carrying a
    claim, one saying it was read and clean -- would otherwise reintroduce the
    disagreement the fold removed, one level down.

    A record with no persisted roster is not checked -- an interview fan-out, or
    one whose producer bounded nothing. Inventing a boundary at re-entry would
    reject evidence its child was never told to avoid.
    """
    if not isinstance(output, Mapping):
        return []
    roster = record.synthesizer_input.get("roster_repo_ids")
    if not isinstance(roster, (list, tuple)):
        return []
    examined = output.get("examined")
    if not isinstance(examined, (list, tuple)):
        return []
    allowed = {str(item) for item in roster}
    problems: list[str] = []
    seen: set[str] = set()
    for entry in examined:
        if not isinstance(entry, Mapping):
            continue
        # An absent or empty identifier is the schema's to reject; saying it
        # twice would report one defect as two.
        repo_id = str(entry.get("repo_id") or "")
        if not repo_id:
            continue
        if repo_id not in allowed:
            problems.append(f"examined: {repo_id!r} is not in this session's roster")
        elif repo_id in seen:
            problems.append(f"examined: {repo_id!r} has more than one entry")
        seen.add(repo_id)
    return problems


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

    The question is the only thing checked here. A contracted answer carries no
    session field for the same reason this function exists at all: the session
    is already bound by the submission envelope above, which compares the
    caller's session against the record before any content is read, and a second
    copy the child asserts about itself is that check restated by the weaker
    party. A field checked only when the child chose to fill it is worse than no
    field -- it reads as a binding and holds as an option.
    """
    if not isinstance(output, Mapping):
        return []
    problems: list[str] = []
    claimed_identity = str(output.get("question_identity") or "")
    if record.question_identity and claimed_identity != record.question_identity:
        problems.append("question_identity: does not belong to this fan-out")
    return problems


__all__ = [
    "FANOUT_KIND_CODE_INVESTIGATION",
    "FANOUT_KIND_LATERAL_PERSONA_PANEL",
    "FANOUT_KIND_QUESTION_ADVISORY",
    "FanoutRecord",
    "FanoutRegistry",
    "PreparedFanoutSynthesis",
    "prepare_fanout_results",
    "register_code_investigation_fanout",
    "register_lateral_persona_fanout",
    "register_question_advisory_fanout",
    "stamp_lateral_persona_fanout",
    "stamp_question_advisory_fanout",
    "submit_fanout_results",
    "synthesize_fanout_results",
]
