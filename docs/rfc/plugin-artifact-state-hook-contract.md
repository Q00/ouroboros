# Plugin Artifact-Write / State-Commit Hook Contract

## 1. Status

**Proposed — 2026-05-28.** Companion document to the merged tool-call
hook contract in
[`docs/rfc/plugin-tool-call-hook-contract.md`](./plugin-tool-call-hook-contract.md).
This RFC is **pure documentation**: it freezes the payload shape,
permission scopes, failure policy, audit event names, and schema-version
gating for the four deferred plugin hooks
`before_artifact_write` / `after_artifact_write` /
`before_state_commit` / `after_state_commit` so that any future
runtime-dispatch slice can be reviewed against a frozen contract.

**No runtime code, schema files, or permission scopes are introduced
by this PR.** Implementation lands in a future slice referred to here
as **PR G**.

PR G is **double-gated**:

1. The plugin-side gate is this RFC (PR D-docs) and a v0.4 manifest
   schema (introduced by the tool-call dispatch slice PR F).
2. The substrate-side gate is a write-capable Artifact/State service
   surface from the #946 / #956 lineage — currently the v1
   Run/Step/Artifact projection vocabulary is read-only. Until that
   substrate exists, PR G remains a contract-only proposal.

This contract is the **PR D-docs** deliverable from the
[#939 scope-decision comment](https://github.com/Q00/ouroboros/issues/939#issuecomment-4477726327).

## 2. Goal

Define the runtime dispatch contract for plugin hooks that observe (or
gate) an artifact write or a state commit performed by the orchestrator
on a plugin's behalf. The intent is to specify — *before code is
written* — the payload shape, allowed side effects, failure policy,
permission scopes, and audit event names that PR G MUST satisfy.

This document does **not**:

- modify `src/ouroboros/plugin/manifest.py`,
- add a `0.4` JSON Schema file under `src/ouroboros/plugin/schemas/`,
- touch `src/ouroboros/plugin/firewall.py` or any dispatcher,
- introduce a write-capable Artifact/State service surface,
- add test cases or fixtures.

The only artifact introduced is this Markdown file.

## 3. Hook payload

Artifact and state hooks observe (or, in the `commit` case, gate) a
single plugin-mediated artifact write or state commit. Payloads are
bounded, redacted by default, and correlated to the parent
`plugin.invoked` event.

### 3.1 `before_artifact_write`

| Field            | Type     | Description                                                                                                  |
|------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `artifact_ref`   | string   | Stable Artifact id from #946 projection vocabulary (e.g. `artifact:<run_id>:<step_id>:<artifact_local_id>`). Plugins MUST NOT receive a filesystem path. |
| `kind`           | string   | Artifact kind from the #946 vocabulary (`text`, `binary`, `structured`, `evidence`, etc.). Plugins read this string but cannot rewrite it. |
| `content_digest` | string   | `sha256:<hex>` digest of the bytes the orchestrator is about to commit. The hook never receives raw bytes.   |
| `content_preview`| string?  | Bounded preview when `kind = text`, **≤ 256 chars**, redacted. Truncated with a `…` sentinel when longer. For non-text kinds the field is `null`. |
| `size_bytes`     | int      | Exact byte size of the content the orchestrator is about to commit.                                          |
| `correlation_id` | string   | Identifier linking this hook callback to the parent `plugin.invoked` audit event for the enclosing plugin run. |
| `write_id`       | string   | Per-artifact-write identifier; pairs `before_artifact_write` with its `after_artifact_write` counterpart.    |
| `permissions`    | string[] | Artifact-class permissions the firewall is about to enforce. Provided for inspection only — hooks cannot grant new permissions. |

### 3.2 `after_artifact_write`

| Field            | Type     | Description                                                                                                  |
|------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `artifact_ref`   | string   | Same stable Artifact id as the matching `before_artifact_write`.                                              |
| `status`         | string   | One of `committed`, `rejected`, `blocked`, `failed`. `committed` is the only success state.                  |
| `content_digest` | string   | `sha256:<hex>` of the bytes that were actually committed. MUST equal the `content_digest` from the matching `before_artifact_write` when `status = committed`. |
| `size_bytes`     | int      | Committed byte size, matching the `before` payload when `status = committed`.                                |
| `duration_ms`    | int      | Wall-clock duration of the write operation, in milliseconds.                                                  |
| `correlation_id` | string   | Same value as the matching `before_artifact_write` event.                                                     |
| `write_id`       | string   | Same value as the matching `before_artifact_write` payload.                                                   |

### 3.3 `before_state_commit`

| Field            | Type     | Description                                                                                                  |
|------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `state_ref`      | string   | Stable identifier of the state slot being committed. Anchored to the #956 Workflow IR / #946 Run-Step lineage (e.g. `state:<run_id>:<step_id>:<slot>`). |
| `slot`           | string   | Logical state slot name (`run.status`, `step.outcome`, `seed.partial`, etc.) drawn from a closed enum exported by the orchestrator. Plugins observe the slot string but cannot extend the enum. |
| `before_digest`  | string   | `sha256:<hex>` digest of the prior state value. Always present; for first-time commits it is the sha256 of the empty value sentinel exported by the orchestrator. |
| `after_digest`   | string   | `sha256:<hex>` digest of the proposed state value.                                                            |
| `delta_preview`  | string?  | Bounded human-readable preview of the proposed change, **≤ 256 chars**, redacted. May be `null` when the state value is opaque/binary. |
| `correlation_id` | string   | Identifier linking this hook callback to the parent `plugin.invoked` audit event.                             |
| `commit_id`      | string   | Per-state-commit identifier; pairs `before_state_commit` with its `after_state_commit` counterpart.           |
| `permissions`    | string[] | State-class permissions the firewall is about to enforce. Provided for inspection only.                       |

### 3.4 `after_state_commit`

| Field            | Type     | Description                                                                                                  |
|------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `state_ref`      | string   | Same stable identifier as the matching `before_state_commit`.                                                 |
| `slot`           | string   | Same slot string as the matching `before_state_commit`.                                                       |
| `status`         | string   | One of `committed`, `rejected`, `blocked`, `failed`.                                                          |
| `before_digest`  | string   | Same value as the matching `before_state_commit`.                                                             |
| `after_digest`   | string   | `sha256:<hex>` of the value that was actually committed. Equals the `after_digest` from the matching `before_state_commit` when `status = committed`. |
| `duration_ms`    | int      | Wall-clock duration of the commit operation, in milliseconds.                                                  |
| `correlation_id` | string   | Same value as the matching `before_state_commit` event.                                                       |
| `commit_id`      | string   | Same value as the matching `before_state_commit` payload.                                                     |

Both `before_*` and `after_*` payloads are **delivered as bounded
JSON**, subject to the same size envelope used by `before_invocation`
/ `after_invocation` and the tool-call hooks. Raw artifact bytes and
raw state values are never passed to the hook process; only digests
and bounded previews cross the trust boundary.

## 4. Permission scopes

Artifact/state hooks introduce four new permission scope strings.
They are **defined here for review** but are not added to any
permission table or firewall constant by this PR.

| Scope                       | Class        | Veto allowed | Failure policy default | Notes                                                                                     |
|-----------------------------|--------------|--------------|------------------------|-------------------------------------------------------------------------------------------|
| `plugin:artifact:observe`   | Observation  | No           | `fail_open` (required) | Holds for either `before_artifact_write` or `after_artifact_write`. MUST NOT block a write. |
| `plugin:artifact:write`     | Mutating     | Yes          | `fail_closed`-eligible | Required to *block* an artifact write via `before_artifact_write`. Holders MUST also hold the artifact-kind permission declared in `artifacts.allowed` (introduced by PR G). |
| `plugin:state:observe`      | Observation  | No           | `fail_open` (required) | Holds for either `before_state_commit` or `after_state_commit`. MUST NOT block a commit. |
| `plugin:state:commit`       | Mutating     | Yes          | `fail_closed`-eligible | Required to *block* a state commit via `before_state_commit`. Holders MUST also hold the slot-specific permission declared in `state.allowed` (introduced by PR G). |

The `:write` / `:commit` scopes are strictly more powerful than their
`:observe` counterparts. Manifests that declare a mutating hook MUST
NOT also rely on the `:observe` scope being implicit; both scopes are
named, even when one is a superset of the other, to keep the audit
event taxonomy explicit.

## 5. Failure policy

Failure policy mirrors the tool-call hook contract (see
[`plugin-tool-call-hook-contract.md`](./plugin-tool-call-hook-contract.md)
§5) and the PR E (`on_error` / `on_cancel`) precedent: observation
hooks MUST NOT mask the outcome of the work they observe; mutation
hooks MAY veto, but only via the documented audit event.

| Hook class                                                          | Allowed `failure_policy` values | Required default | Rationale |
|---------------------------------------------------------------------|---------------------------------|------------------|-----------|
| `write`  (`before_artifact_write` with `plugin:artifact:write`)     | `fail_closed`, `fail_open`      | `fail_closed`    | Authoring policies want a veto path to be deterministic. Plugins may opt down to `fail_open` only when the veto is best-effort. |
| `commit` (`before_state_commit` with `plugin:state:commit`)         | `fail_closed`, `fail_open`      | `fail_closed`    | Same rationale as `write`; state commits are the higher-risk surface and `fail_closed` is the safe default. |
| `observe` (`*_observe` scope holders, including all `after_*` hooks) | `fail_open` **only**            | `fail_open`      | A v0.4 schema MUST reject `fail_closed` here, as v0.3 already does for `on_error` / `on_cancel`. |

The schema constraint pattern is the same `if` / `then` shape already
present in v0.3 for `on_error` / `on_cancel` and proposed in
PR F for `before_tool_call` / `after_tool_call`; PR G simply extends
it to the four new hook names without rewriting the constraint family.

## 6. Audit event names

Audit events mirror the existing `plugin.invoked` / `plugin.tool.*` /
`plugin.hook.failed` naming style. They are **reserved names** in this
RFC and MUST be emitted (and only emitted) by PR G's dispatcher.

| Event name                                  | When                                                                              | Notes |
|---------------------------------------------|-----------------------------------------------------------------------------------|-------|
| `plugin.artifact.write.requested`           | The firewall has selected a `write`-class hook for an artifact write, immediately before invoking the hook process. | Carries `before_artifact_write` payload fields. |
| `plugin.artifact.write.committed`           | The hook returned a non-blocking decision and the artifact was committed.         | Final decision recorded with the original `content_digest`. |
| `plugin.artifact.write.blocked`             | The `write`-class hook explicitly vetoed (or its `fail_closed` policy fired).     | The artifact MUST NOT be committed; the parent plugin sees a `write blocked` failure mode. |
| `plugin.artifact.observe.recorded`          | An `observe`-class `after_artifact_write` hook recorded a successful observation. | Never blocks; emitted at most once per artifact write per `observe` hook. |
| `plugin.state.commit.requested`             | The firewall has selected a `commit`-class hook for a state commit, immediately before invoking the hook process. | Carries `before_state_commit` payload fields. |
| `plugin.state.commit.committed`             | The hook returned a non-blocking decision and the state was committed.            | Final decision recorded with the original `after_digest`. |
| `plugin.state.commit.blocked`               | The `commit`-class hook explicitly vetoed.                                        | The state MUST NOT be committed; the parent plugin sees a `commit blocked` failure mode. |
| `plugin.state.observe.recorded`             | An `observe`-class `after_state_commit` hook recorded a successful observation.   | Never blocks. |

Failure of any artifact/state hook *process* — distinct from the
`blocked` decision — continues to flow through the existing
`plugin.hook.failed` event in PR G. PR G MUST NOT introduce a parallel
`plugin.artifact.hook.failed` or `plugin.state.hook.failed` event
family.

## 7. Service-boundary rules

These rules apply to hook handlers and are non-negotiable for PR G.
Each rule has an explicit "what PR G MUST verify" clause.

1. **No direct filesystem mutation.** Hook handlers MUST NOT open,
   write, or rename files on the local filesystem. The artifact bytes
   committed by the orchestrator are the only artifact write event the
   harness recognizes. Verification: PR G integration tests assert
   that a hook performing `open(... "w")` is rejected by the firewall
   trust boundary before its decision is observed.

2. **No direct `EventStore.append`.** Hook handlers MUST NOT call
   `EventStore.append` (or any of its variants). All audit emission
   flows through the dispatcher's reserved event names (§ 6).
   Verification: PR G integration tests assert that a hook attempting
   to append a custom event is denied and a `plugin.hook.blocked`
   audit event (the existing `HOOK_BLOCKED_EVENT` defined at
   `src/ouroboros/plugin/hooks.py:166`) is recorded citing the
   unauthorized append.

3. **No tail call into another mutating hook.** A `write`-class hook
   MUST NOT trigger another artifact write or a state commit during
   its own dispatch window. The dispatcher serializes mutating
   activity per `correlation_id`. Verification: PR G integration tests
   assert that a hook attempting a recursive write is denied with a
   `plugin.hook.blocked` event citing the reentrancy rule.

4. **Service interface, not raw projection.** Plugins reach the
   orchestrator's projection vocabulary only through a typed service
   surface introduced by PR G. The current projection layer is
   read-only at the public boundary: `ProjectionQueryHandler` in
   `src/ouroboros/mcp/tools/projection_handlers.py` (module docstring:
   `"Read-only MCP query surface for harness projections."`) is fed by
   `build_projection` in `src/ouroboros/harness/projection_builder.py`
   and exposes no write API. PR G's write-side service surface is the
   only legal write path.

5. **`fail_open` cannot promote to mutation.** A hook handler whose
   declared scope is `*:observe` MUST NOT see a write/commit decision
   field in its payload at all. Verification: PR G unit tests assert
   that the payload builder strips decision fields when the hook is
   `observe`-class.

## 8. Schema version migration

Artifact/state hooks are **not** additive to either the v0.3 or the
v0.4 manifest schema. A schema version that accepts them MUST be
introduced **by PR G itself** (which will also bring the write-side
substrate online). Until then, the rejection behavior described below
holds.

1. **v0.3 rejection (existing).** The `hooks.name` enum in
   `src/ouroboros/plugin/schemas/0.3/plugin.schema.json` is currently
   limited to `before_invocation`, `after_invocation`, `on_error`,
   `on_cancel`. Any v0.3 manifest declaring `before_artifact_write` /
   `after_artifact_write` / `before_state_commit` /
   `after_state_commit` is already rejected at validation time by
   JSON Schema's `enum` constraint with **no code change required**.

2. **v0.4 rejection (introduced by PR F).** The v0.4 schema
   introduced by PR F adds `before_tool_call` / `after_tool_call` to
   the enum but **does not** add the artifact/state names. Manifests
   that try to opt into artifact/state hooks before PR G lands MUST
   still be rejected with a clear error referencing this RFC.

3. **Future schema (PR G).** Promotion of the four artifact/state
   names into the `hooks.name` enum happens in the same PR that
   ships the write-side substrate. The schema version chosen by
   PR G (likely `0.5`) is recorded in the PR-G changelog and not
   pre-allocated here.

## 9. Non-goals

- **No plugin marketplace / remote sandbox.** PR G dispatches to
  trusted plugins only.
- **No plugin-owned permission approval prompts.** Permission HITL
  follows the contract locked by #1116.
- **No plugin-owned evidence schema.** Evidence schemas live on
  #830/#978.
- **No plugin-owned projection identity.** Artifact and state ids are
  assigned by the orchestrator (#946 vocabulary).
- **No artifact deletion or state reset hooks.** Mutation-by-deletion
  is out of scope for v1.

## 10. Acceptance criteria for PR G

When PR G is opened, this RFC functions as its acceptance contract.
PR G MUST:

1. Ship a `0.5` (or chosen) JSON Schema that adds exactly the four
   names listed in § 3 to the `hooks.name` enum and preserves the
   `failure_policy` `if`/`then` constraint family from v0.3.
2. Reject v0.3/v0.4 manifests declaring artifact/state hooks with a
   clear error message referencing this document.
3. Emit exactly the audit events listed in § 6, with payload fields
   matching § 3 verbatim.
4. Enforce the service-boundary rules in § 7 with at least one
   regression test per rule.
5. Ship at least one first-party fixture plugin demonstrating both an
   `observe`-class hook and a `write`/`commit`-class hook, including
   timeout, fail-open, and fail-closed paths.

## 11. Related

- [`plugin-tool-call-hook-contract.md`](./plugin-tool-call-hook-contract.md) — sister contract for `before_tool_call` / `after_tool_call`.
- [`userlevel-plugins.md`](./userlevel-plugins.md) — v1 hook vocabulary. `before_artifact_write` / `after_artifact_write` are listed in its Deferred hook table (line 332–333); `before_state_commit` / `after_state_commit` are currently classified as "intentionally not v1" (matching `ExcludedHookKind` at `src/ouroboros/plugin/hooks.py:140–141`, and the rationale at `userlevel-plugins.md:352–354`). **This RFC supersedes that earlier state-hook classification**: state hooks move from "intentionally not v1" into "contract frozen, runtime deferred to PR G" under the same double-gate as artifact hooks. The `ExcludedHookKind` enum and the `userlevel-plugins.md` state-hook bullet are expected to be updated in PR G (or a narrow follow-up) once the v0.5 schema lands; this RFC does not touch either surface.
- [#939](https://github.com/Q00/ouroboros/issues/939) — plugin lifecycle / permissions / audit umbrella.
- [#946](https://github.com/Q00/ouroboros/issues/946) / [#956](https://github.com/Q00/ouroboros/issues/956) — Run / Step / Artifact projection and Workflow IR vocabulary that PR G's write-side surface extends.
- [#961 SSOT](https://github.com/Q00/ouroboros/issues/961) — process rules invoked by this RFC.
