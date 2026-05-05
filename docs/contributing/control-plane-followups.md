# Control-plane follow-up map

Issue [#574](https://github.com/Q00/ouroboros/issues/574) established the
versioned `ControlContract` boundary for `control.directive.emitted` events.
This document records which follow-up pull requests are valid Agent OS work,
which issue lane owns them, and what risk each lane carries.

The map is intentionally scoped by Agent OS layers instead of treating every
item as a direct child of #574. `ControlContract` is the schema/invariant
boundary; delivery, process lifecycle, policy, and mesh work are adjacent lanes
under the broader Agent OS runtime contract.

## Lane summary

| Lane | Owning issue | Status | Why it fits Agent OS | Primary risk |
|---|---:|---|---|---|
| ControlContract closure | #574 | Ready now | Turns the contract from implemented code into a reviewable, closed acceptance record. | Over-scoping #574 into delivery or policy work. |
| Projector conformance | #574 | Ready now | Ensures replay/projectors consume the contract fields rather than ad-hoc payload fragments. | Changing audit projections in a way that drops raw history. |
| Effective idempotency | #574 / #575 | Ready after conformance | Makes replay/backfill/mesh consumers able to dedupe effective decisions while preserving raw events. | Collapsing distinct decisions that share weak keys. |
| ControlJournal delivery | #575 | Needs design first | Defines the source-of-truth handoff from durable journal to local `ControlBus` and future transports. | Introducing an outbox before delivery guarantees and failure modes are explicit. |
| Producer migration | #472 / #574 | Incremental | Moves evaluator, resilience, watchdog, and lifecycle decisions onto the shared `Directive` vocabulary. | Mapping local states to terminal directives too aggressively. |
| AgentProcess lifecycle | #518 / #528 | Incremental | Makes long-running workflows pause/resume/cancel/replay through one observable process contract. | Treating lifecycle transport as equivalent to durable replay. |
| Capability and policy | #576 | Separate epic lane | Explains why a directive was allowed by making tool visibility/execution policy explicit and journaled. | Mixing authorization policy with directive intent. |
| External guidance | #614 | Later trust-boundary lane | Makes prompt guidance explicit, hashed, scoped, and replayable instead of ambient runtime inheritance. | Treating guidance text as harmless when it can alter planning and evaluation. |
| Mesh delivery | #476 / future mesh issue | Last | Preserves `ControlContract` fields across runtimes after local delivery/idempotency semantics exist. | Standardizing cross-runtime delivery before local outbox semantics are stable. |

## Recommended pull-request sequence

1. **Close #574 acceptance evidence.**
   - Record the implemented schema, invariant enforcement, docs, and tests.
   - Do not add new runtime behavior in this PR.
   - Risk: low; documentation can drift if later PRs do not update it.

2. **Projector contract conformance.**
   - Preserve `schema_version`, target identity, `parent_directive_id`, and
     `idempotency_key` in projected directive summaries.
   - Keep raw directive events available for audit.
   - Risk: medium; projection models are user-visible API and should evolve
     additively.

3. **Effective idempotency projection.**
   - Add an explicit effective-decision view keyed by
     `(target_type, target_id, directive, idempotency_key)` when the key exists.
   - Keep the raw audit timeline unchanged.
   - Risk: medium; dedupe must not hide distinct decisions without stable keys.

4. **ControlJournal delivery design (#575).**
   - Decide between journal-backed outbox and best-effort bus delivery.
   - Prefer journal-backed outbox if delivery must survive process crashes.
   - Risk: high; this sets durability expectations for every later transport.

5. **Journal-backed directive emitter and dispatcher.**
   - Centralize append/outbox/publish behavior so producers do not hand-roll
     `append -> publish` sequences.
   - Risk: high; failure handling must be deterministic and idempotent.

6. **Producer migrations.**
   - Migrate evaluator, resilience/unstuck, runtime watchdog, and lifecycle
     decisions one site at a time.
   - Risk: medium-high; local outcomes must map to `Directive` without semantic
     loss, especially terminal decisions.

7. **Replay and UI consumers.**
   - Define which directives are reapplied, which are audit-only, and how humans
     inspect the directive timeline.
   - Risk: medium; replay rules must not create side effects from historical
     audit rows accidentally.

8. **Broader Agent OS lanes.**
   - Policy/capability enforcement, external guidance contracts, and mesh
     delivery should proceed after the local control journal semantics are clear.
   - Risk: high; these lanes cross trust boundaries and runtime boundaries.

## Non-goals for #574 follow-ups

- Do not make `ControlBus` the durable source of truth. It remains transport.
- Do not infer policy authorization from a directive value.
- Do not treat `Contract Ledger` as the same artifact as `ControlContract`.
- Do not backfill ambiguous history into effective decisions without marking the
  synthetic provenance and preserving raw events.
