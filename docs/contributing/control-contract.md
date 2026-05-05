# ControlContract

`ControlContract` is the stable schema and invariant boundary for Agent OS
control-plane decisions. It sits between local decision sites and durable
`control.directive.emitted` events.

The contract exists so projectors, replay, future mesh delivery, and in-process
`ControlBus` subscribers consume the same decision shape instead of ad-hoc event
payloads.

## Layering

```text
Decision site
  -> ControlContract validates schema + invariants
  -> control.directive.emitted persists the durable decision
  -> ControlBus may deliver the same decision in-process
  -> projectors replay the persisted contract, not transport side effects
```

`ControlBus` is transport. The EventStore is the durable source of truth.

## Schema version 1

| Field | Required | Meaning |
|---|---:|---|
| `schema_version` | yes | Additive ControlContract schema version. Current value: `1`. |
| `directive` | yes | A `Directive` member serialized by value. |
| `target_type` | yes | The aggregate type the decision is about. |
| `target_id` | yes | The aggregate id the decision is about. |
| `emitted_by` | yes | Logical producer, e.g. `evolver`, `evaluator`, `job_manager`. |
| `reason` | yes | Short audit rationale for why the decision was emitted. |
| `is_terminal` | derived | Computed from `directive`; callers must not override it. |
| `phase` | no | Local phase name when known. |
| `session_id` | no | Owning session correlation when known. |
| `execution_id` | no | Execution/run correlation when known. |
| `lineage_id` | no | Lineage correlation when known. |
| `generation_number` | no | Lineage generation number when known. |
| `context_snapshot_id` | no | Context snapshot captured at emission time when known. |
| `parent_directive_id` | no | Causal parent directive event id when a directive follows another directive. |
| `idempotency_key` | no | Effective-decision dedupe key for replay/backfill/mesh consumers. |
| `extra` | no | Forward-compatible supplemental metadata. Prefer stable fields when possible. |

Canonical `target_type` values are `session`, `execution`, `lineage`,
`agent_process`, `contract`, and `execution_node`. The field remains a string so
new target kinds can land additively.

## Invariants

- Required string fields must be non-empty.
- `schema_version` must be greater than or equal to `1`.
- `generation_number`, when present, is 1-based.
- Terminality is global: only `Directive.CANCEL` and `Directive.CONVERGE` are
  terminal. Every other directive is non-terminal.
- `is_terminal` is derived from the directive vocabulary and must never be a
  caller-provided payload flag.
- Schema evolution is additive unless a future PR explicitly declares a breaking
  version boundary.

## Idempotency

Raw event UUIDs identify persisted rows. They do not identify whether two rows
represent the same effective control decision.

When a producer can compute stable decision identity, it should set
`idempotency_key`. Consumers that need effective dedupe should use:

```text
(target_type, target_id, directive, idempotency_key)
```

Rows without `idempotency_key` are valid legacy-compatible rows. They provide raw
replay but no projection-level dedupe guarantee beyond the event id.

Backfill, mesh delivery, and contract-targeted emitters should prefer deterministic
idempotency keys before consumers rely on at-most-once effective application.

## Local enum mapping

Local workflow enums may remain in their owning subsystem, but any control-plane
emission must map them into the global `Directive` vocabulary without semantic
loss. The current evolution emission site uses
`ouroboros.evolution.directive_mapping.step_action_to_directive()` as the single
authority for `StepAction` outcomes:

| Local action | Directive | Notes |
|---|---|---|
| `StepAction.CONTINUE` | no directive emission | No-op continuation; `lineage.generation.completed` already records progress. |
| `StepAction.CONVERGED` | `CONVERGE` | Terminal success. |
| `StepAction.STAGNATED` | `UNSTUCK` | Non-terminal recovery path; never terminal success. |
| `StepAction.EXHAUSTED` | `CANCEL` | Terminal stop without claiming convergence. |
| `StepAction.INTERRUPTED` | `CANCEL` | Current runtime stops the interrupted step and relies on resume state for continuation. |
| `StepAction.FAILED` | `RETRY` or `CANCEL` | Depends on remaining retry budget; default live behavior emits `RETRY` until the resilience layer owns the budget. |

## Projector expectations

Projectors consume the ControlContract fields from `control.directive.emitted`
payloads. They should not infer terminality from local enum names, transport
state, or `ControlBus` delivery.

A projector may use `idempotency_key` to collapse duplicate effective decisions,
but it must keep raw events available for audit. If causal ordering matters, use
`parent_directive_id` when present and fall back to timestamp/event-id ordering
only as a weaker legacy signal.

## Mesh delivery minimum

A cross-runtime mesh delivery envelope must preserve at least:

- `schema_version`
- `directive`
- `target_type`
- `target_id`
- `emitted_by`
- `reason`
- `is_terminal`
- every known replay correlation field (`session_id`, `execution_id`,
  `lineage_id`, `generation_number`, `phase`)
- `parent_directive_id` and `idempotency_key` when known

Dropping these fields turns a control decision into a local notification, not a
replayable Agent OS contract.
