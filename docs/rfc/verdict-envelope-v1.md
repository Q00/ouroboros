# Verdict Envelope v1

Issue: #814

Status: schema-first design RFC. This document fixes the multi-persona verdict
output shape before subsystem adoption.

## Problem

Ouroboros already has a dispatch-side `_subagent` / `_subagents` envelope, but
the result side is fragmented:

- `ConsensusResult` in `src/ouroboros/evaluation/models.py` stores votes,
  approval, majority ratio, and disagreements.
- Stage 3 events in `src/ouroboros/events/evaluation.py` emit `approved`,
  `votes`, `majority_ratio`, `disagreements`, and count fields.
- MCP-facing tools in `src/ouroboros/mcp/tools/definitions.py` return
  tool-specific text/meta shapes.

That fragmentation makes callers infer verdict semantics from prose or
subsystem-specific fields. The output contract should instead expose one typed
envelope.

## Schema

The canonical envelope is JSON-serializable and may be represented by Pydantic
or dataclass models in implementation PRs.

```json
{
  "schema_version": "verdict_envelope.v1",
  "verdict": "string or null",
  "status": "PASS | FAIL | BLOCKED | DEFERRED",
  "members": ["hacker", "architect"],
  "dissent": [
    {
      "persona_a": "hacker",
      "persona_b": "architect",
      "topic": "scope risk",
      "summary": "short disagreement summary"
    }
  ],
  "evidence_used": ["evt_123", "artifact_456"],
  "transcript_ref": "evt_transcript_789",
  "follow_up_issue_refs": ["#1306"],
  "metadata": {
    "source": "evaluation.stage3"
  }
}
```

Field rules:

- `schema_version` is required and must be `verdict_envelope.v1`.
- `verdict` is a one-line synthesized conclusion, or `null` when synthesis is
  deliberately deferred to the user.
- `status` is required. `DEFERRED` is the default for user-owned decisions such
  as lateral multi-persona debate.
- `members` is the ordered set of participating personas/models/roles.
- `dissent` is optional and contains pairwise or topic-level disagreement.
- `evidence_used` contains stable event IDs, artifact IDs, fact IDs, or handles
  that substantiate the verdict.
- `transcript_ref` is optional until #819 provides durable transcript storage.
- `follow_up_issue_refs` lists implementation issues opened from this design.
- `metadata` is for subsystem-specific values that must not define verdict
  semantics.

## Migration Map

| Existing shape | Envelope mapping | Gap |
| --- | --- | --- |
| `ConsensusResult.approved` | `status=PASS` when true, `status=FAIL` when false | No explicit deferred state. |
| `ConsensusResult.disagreements` | `dissent[].summary` | Lacks structured persona pairs/topics. |
| `ConsensusResult.votes[].model` | `members[]` | Models are not always personas. |
| Stage 3 `approved` event field | `status` | Event has no `schema_version`. |
| Stage 3 `votes` event field | `members` plus `metadata.votes` | Vote details remain subsystem metadata. |
| Stage 3 `disagreements` event field | `dissent` | Needs structured dissent records. |
| MCP tool text outputs | `verdict` plus `metadata.rendered_text` | Text must stop being the semantic source of truth. |
| MCP tool meta outputs | `metadata` | Existing meta can be preserved under namespaced keys. |

## Follow-up Implementation Issues

- #814 follow-up A: add a shared `VerdictEnvelopeV1` model and JSON schema.
- #814 follow-up B: adapt `ConsensusResult` / deliberative evaluation outputs
  to expose `VerdictEnvelopeV1`.
- #814 follow-up C: emit `verdict_envelope` from Stage 3 evaluation events.
- #814 follow-up D: add MCP tool result metadata for `verdict_envelope` while
  preserving current rendered text for compatibility.
- #1306: use typed verifier output and retry admission for TraceGuard-backed H1
  deliver verdicts.

## Non-goals

- Do not require every user-facing response to auto-synthesize a verdict.
- Do not replace subsystem-specific detailed result models.
- Do not make transcript storage mandatory before #819.
