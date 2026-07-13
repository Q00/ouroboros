# RFC: Symposium Deliberation Contract

Issue: #813

Status: Proposed closeout contract. This RFC resolves the design questions in
#813 without changing runtime behavior.

## Decision

`Symposium` names a deliberation semantics layer. It does not rename or replace
the generic subagent fan-out transport.

The repository already has a transport contract in
`src/ouroboros/mcp/tools/subagent.py`:

- `build_fanout_subagents` builds independent payloads;
- `stamp_fanout_meta` describes plugin-passive, host-driven, and sequential
  dispatch;
- `FanoutRegistry` records correlation state for result re-entry; and
- `ouroboros_submit_fanout_results` returns correlated child outputs to the
  caller.

Those facilities answer how work is dispatched and correlated. Symposium
answers a different set of questions: who participates, what they deliberate,
how many rounds run, who synthesizes, and who owns the final decision.

## Vocabulary

| Term | Meaning | Current examples |
| --- | --- | --- |
| Fan-out transport | Dispatch and correlate N independent payloads. It defines no debate or verdict semantics. | `_subagents`, host-driven payloads, sequential payload processing |
| Typed panel | N lanes with panel-specific outputs and deterministic aggregation or selection. Lanes do not become a general debate pool. | Interview question advisory, ambiguity scoring, question candidates, seed-closer tri-panel |
| Symposium deliberation | Two or more members produce positions that may conflict, followed by an explicit synthesis and decision-ownership policy. | `ooo lateral` debate, Seed Reflect/user-adoption flow |
| Evaluation consensus | Evaluation-specific voting or judgment over an artifact and acceptance contract. | `ConsensusEvaluator`, `DeliberativeConsensus` |

These categories may share the fan-out transport without sharing semantics.
Parallel execution alone does not make a surface a Symposium deliberation.

## Canonical Request

The semantic request is JSON-serializable:

```json
{
  "schema_version": "symposium_request.v1",
  "members": ["hacker", "architect"],
  "topic": {
    "problem_context": "The design has stalled.",
    "current_approach": "Keep extending the current abstraction.",
    "failed_attempts": ["Added another adapter layer"]
  },
  "rounds": 1,
  "synthesis_mode": "host_recommendation",
  "decision_owner": "user"
}
```

### Field rules

- `schema_version` is required and must be `symposium_request.v1`.
- `members` is an ordered list of at least two unique member identifiers.
  A single persona remains a solo lateral pass, not Symposium deliberation.
- `topic.problem_context` and `topic.current_approach` are required non-empty
  strings.
- `topic.failed_attempts` is an optional ordered string list.
- `rounds` is `1` or `2`; the default is `1`.
- `rounds=2` runs one cross-attack round after the initial positions. It runs
  only when the user or caller explicitly requests two rounds before dispatch.
  V1 does not infer round escalation from free-text disagreement.
- `synthesis_mode` is `host_recommendation` or `automatic_judge`.
- `decision_owner` is `user` or `system`.
- `synthesis_mode` and `decision_owner` are policy labels. They are not
  transport keys and do not change `_subagents`, `dispatch_mode`,
  `host_action`, or `result_correlation_key`.

V1 supports these policy pairs:

| Synthesis mode | Decision owner | Meaning |
| --- | --- | --- |
| `host_recommendation` | `user` | The host summarizes options, dissent, and a recommendation. The user owns the verdict. |
| `automatic_judge` | `system` | A designated judge produces the terminal verdict under a subsystem-specific contract. |

Other pairs require a later RFC revision.

## Current Lateral Mapping

`ouroboros_lateral_think` remains the current lateral prompt and dispatch
surface. A future adoption slice maps its existing arguments without loss:

| Current lateral input | Symposium request |
| --- | --- |
| `personas` | `members` |
| `problem_context` | `topic.problem_context` |
| `current_approach` | `topic.current_approach` |
| `failed_attempts` | `topic.failed_attempts` |
| `persona` | Solo lateral behavior; outside Symposium unless promoted to a request with at least two `members` |

The current SKILL guidance may start a second cross-attack round when responses
"diverge meaningfully." That heuristic is legacy, non-v1 behavior. This RFC
does not change the SKILL. A fresh lateral-adoption issue must align the SKILL
with the explicit `rounds` contract.

## Canonical Result

Symposium uses `VerdictEnvelopeV1` from
`docs/rfc/verdict-envelope-v1.md`. It does not introduce another result
envelope.

For `host_recommendation + user`:

- `status` is `DEFERRED`;
- `verdict` is `null`;
- `members` preserves request order;
- `dissent` records material disagreements; and
- a host recommendation, when present, belongs in
  `metadata.recommendation`, not in `verdict`.

For `automatic_judge + system`:

- `status` is `PASS`, `FAIL`, or `BLOCKED` under the owning subsystem's
  contract;
- `verdict` contains the judge's synthesized conclusion; and
- detailed votes or positions remain namespaced subsystem metadata.

Current rendered text, `ConsensusResult`, `DeliberationResult`, Stage 3 events,
and MCP metadata remain backward compatible until fresh adapter issues are
implemented.

## Membership Policy

The `ooo lateral` debate pool remains the five stateless mindset personas:
`hacker`, `researcher`, `simplifier`, `architect`, and `contrarian`.

Stateful or workflow-owned roles do not join an ad hoc global debate pool in
V1. They may participate in named typed panels only when that panel defines:

- the role and required setup state;
- the input and output schema;
- whether the lane can vote or only advise;
- side-effect and tool-call boundaries; and
- deterministic aggregation or gating semantics.

Allowing stateful roles to vote as generic Symposium members requires a later
policy RFC.

## Syntax Ownership

Member and preset syntax stays at the SKILL/parser boundary:

- `ooo lateral debate <p1> <p2> ...` remains the explicit member syntax;
- `@all` remains an `unstuck` SKILL preset;
- V1 does not add `+` member-join syntax because `+ouroboros lateral` is already
  used as a tool-discovery cue; and
- a shared parser is justified only after a second real grammar consumer needs
  the same syntax.

The transport layer receives normalized members and does not parse user-facing
syntax.

## Transcript Policy

Transcript persistence is off by default in V1.

- `transcript_ref` is `null` or absent when no transcript is persisted.
- `FanoutRegistry` is correlation state, not transcript storage. It does not
  satisfy replay, audit, privacy, or retrieval requirements.
- A future opt-in implementation requires a fresh issue that defines project
  locality, privacy/redaction, retention or TTL, purge behavior, and a retrieval
  surface before storage code is added.

## Adoption Matrix

| Surface | Classification | Symposium action |
| --- | --- | --- |
| `ooo lateral` debate | Deliberation | V1 adoption target in a fresh issue |
| Seed Reflect/user-adoption flow | Deliberation | Map to `host_recommendation + user`; no automatic verdict |
| Interview milestone lateral review | Inherits lateral | Uses the lateral contract when it dispatches multiple personas |
| Interview question advisory | Typed panel/fan-out | No Symposium adoption |
| Ambiguity dimension panel | Typed panel/fan-out | No Symposium adoption |
| Question candidate panel | Typed panel/fan-out | No Symposium adoption |
| Seed-closer tri-panel | Typed panel/fan-out | No Symposium adoption |
| `ConsensusEvaluator` | Voting consensus | Non-Symposium unless a later wrapper is justified |
| `DeliberativeConsensus` | Existing standalone deliberation | Candidate for a result adapter only; no execution migration |
| Auto single-persona lateral recovery | Solo lateral | Not Symposium |

## Compatibility And Migration

This RFC changes no runtime behavior.

- Generic fan-out builders, dispatch metadata, and result re-entry remain the
  transport source of truth.
- `_subagent`, `_subagents`, host-driven payloads, sequential payloads, and the
  lateral inline dispatch sentinel remain compatible.
- `ConsensusResult`, `DeliberationResult`, Stage 3 events, and MCP rendered text
  remain current public shapes until adapters are implemented.
- Evaluation execution does not migrate to lateral debate. Any such proposal
  must identify a reproducible failure in the current evaluator and a measurable
  success criterion that justifies latency, cost, and reproducibility changes.

## Fresh Follow-up Issues

After this RFC is accepted, implementation work should be opened as new issues,
not by reviving the folded issue numbers:

1. Add the `VerdictEnvelopeV1` model/schema and compatibility adapters.
2. Adopt the Symposium request/result contract in lateral debate, including
   explicit round selection and synthesis emission.
3. Add optional transcript storage and retrieval only after privacy, retention,
   and purge policy is accepted.
4. Add a `DeliberativeConsensus` output adapter if consumers need the standard
   envelope.

## Folded History

Issues #814, #815, #816, and #819 were closed on 2026-06-07 and folded into
#813. Issues #817 and #818 were also closed. They are design history and must
not be treated as active implementation children.

## Acceptance Criteria

This RFC closes #813 when:

1. `Symposium` is documented as deliberation semantics, not fan-out transport.
2. The request schema, valid policy pairs, and current lateral mapping are
   explicit.
3. Round 2 is explicit-request-only in V1.
4. `VerdictEnvelopeV1` is the only standard result envelope.
5. The adoption matrix classifies current fan-out, panel, voting, and
   deliberation surfaces.
6. Membership, syntax, and transcript policies are explicit.
7. Folded issues are historical and future implementation uses fresh issues.
8. No runtime behavior or evaluation execution path is changed by this RFC.

## Non-goals

- Renaming or rewriting the generic fan-out transport.
- Adding a global pool of stateful debate agents.
- Migrating `evaluate` execution to Symposium without independent evidence.
- Persisting transcripts in this RFC slice.
- Replacing subsystem-specific detailed result models.
