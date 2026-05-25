# Plugin `before_tool_call` / `after_tool_call` Hook Contract

## 1. Status

**Proposed — 2026-05-20.** Companion document to PR
[#1137](https://github.com/Q00/ouroboros/pull/1137) (PR E — `on_error` /
`on_cancel` observability hooks). This RFC is **pure documentation**: it
defines the dispatch contract for the deferred `before_tool_call` and
`after_tool_call` plugin hooks so that the implementation slice can be
reviewed against a frozen contract. **No runtime code, schema files, or
permission scopes are introduced by this PR.** Implementation lands in a
future Wave 3+ slice referred to here as **PR F**.

This contract is the PR C deliverable from the
[#939 scope-decision comment](https://github.com/Q00/ouroboros/issues/939#issuecomment-4477726327)
and is tracked under umbrella [#1142](https://github.com/Q00/ouroboros/issues/1142).

## 2. Goal

Define the runtime dispatch contract for the `before_tool_call` and
`after_tool_call` plugin hooks declared as **Deferred** in
[`docs/rfc/userlevel-plugins.md`](./userlevel-plugins.md) (§ v1 hook
vocabulary). The intent is to specify — *before code is written* — the
payload shape, permission scopes, failure policy, audit event names, and
schema-version gating that PR F MUST satisfy.

This document does NOT:

- modify `src/ouroboros/plugin/manifest.py`,
- add a `0.4` JSON Schema file under `src/ouroboros/plugin/schemas/`,
- touch `src/ouroboros/plugin/firewall.py` or any dispatcher,
- add test cases or fixtures.

The only artifact introduced is this Markdown file.

## 3. Hook payload

Tool-call hooks observe (or, in the `intercept` case, gate) a single
plugin-mediated tool invocation. Payloads are bounded, redacted by
default, and correlated to the parent `plugin.invoked` event.

### 3.1 `before_tool_call`

| Field            | Type     | Description                                                                                                  |
|------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `tool`           | string   | Canonical tool name as declared by the manifest command/tool surface.                                         |
| `args_digest`    | string   | `sha256:<hex>` digest of the *full* serialized arguments. The hook never receives raw args.                   |
| `args_preview`   | string   | Bounded preview of the redacted arguments, **≤ 256 chars**. Truncated with a `…` sentinel when longer.        |
| `correlation_id` | string   | Identifier linking this hook callback to the parent `plugin.invoked` audit event for the enclosing plugin run.|
| `invocation_id`  | string   | Per-tool-call identifier; pairs `before_tool_call` with its `after_tool_call` counterpart.                    |
| `permissions`    | string[] | Tool-specific permissions the firewall is about to enforce. Provided for inspection only — hooks cannot grant new permissions. |

### 3.2 `after_tool_call`

| Field            | Type     | Description                                                                                                  |
|------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `tool`           | string   | Same canonical tool name as the matching `before_tool_call`.                                                  |
| `status`         | string   | One of `success`, `failed`, `blocked`, `cancelled`. Mirrors the firewall's tool-status taxonomy.              |
| `exit_code`      | int?     | Process-style exit/status code where applicable; `null` for non-process tools.                                |
| `output_digest`  | string   | `sha256:<hex>` digest of the combined `stdout` + `stderr` (or canonical structured-output bytes). Raw output is never delivered to the hook. |
| `duration_ms`    | int      | Wall-clock duration of the tool call, in milliseconds.                                                        |
| `correlation_id` | string   | Same value as the matching `before_tool_call` event — links to the parent `plugin.invoked` run.               |
| `invocation_id`  | string   | Same value as the matching `before_tool_call` payload.                                                        |

Both payloads are **delivered as bounded JSON**, subject to the same
size envelope used by `before_invocation` / `after_invocation` in the v1
hook vocabulary. Raw arguments and raw outputs are never passed to the
hook process; only digests and the bounded preview cross the trust
boundary.

## 4. Permission scopes

Tool-call hooks introduce two new permission scope strings. They are
**defined here for review** but are not added to any permission table or
firewall constant by this PR.

| Scope                       | Class        | Veto allowed | Failure policy default | Notes                                                                                     |
|-----------------------------|--------------|--------------|------------------------|-------------------------------------------------------------------------------------------|
| `plugin:tool:intercept`     | Mutating     | Yes          | `fail_closed`-eligible | Required for any hook that wants to *block* a tool call. Holders of this scope MUST also hold the tool-specific permission declared for the affected command/tool. |
| `plugin:tool:observe`       | Observation  | No           | `fail_open` (required) | Hooks holding only this scope MUST NOT veto a tool call. Used for telemetry, redacted logging, and external observability surfaces. |

`plugin:tool:intercept` is strictly more powerful than
`plugin:tool:observe`. Manifests that declare an `intercept` hook MUST
NOT also rely on the `observe` scope being implicit; both scopes are
named, even when one is a superset of the other, to keep the audit
event taxonomy explicit.

## 5. Failure policy

Failure policy mirrors the PR E (`on_error` / `on_cancel`) precedent:
observation hooks MUST NOT mask the outcome of the workload they
observe; mutation hooks MAY veto, but only via the documented audit
event.

| Hook class                      | Allowed `failure_policy` values | Required default | Rationale                                                                                                                                          |
|---------------------------------|---------------------------------|------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `intercept` (`before_tool_call` with `plugin:tool:intercept`) | `fail_closed`, `fail_open`      | `fail_closed`    | Authoring tools want a veto path to be deterministic. Plugins may opt down to `fail_open` ("observation-with-intent") when intercept is best-effort. |
| `observe`   (`before_tool_call` / `after_tool_call` with `plugin:tool:observe`) | `fail_open` **only**            | `fail_open`      | Mirror PR E: observation hooks must never break the underlying tool call. A v0.4 schema MUST reject `fail_closed` here, as v0.3 already does for `on_error` / `on_cancel`. |

The schema constraint pattern is the same `if` / `then` shape already
present in v0.3 for `on_error` and `on_cancel` (see
`src/ouroboros/plugin/schemas/0.3/plugin.schema.json` §
`allOf.failure_policy`); PR F simply extends it to the two new hook
names without rewriting the constraint family.

## 6. Audit event names

Audit events mirror the existing `plugin.invoked` / `plugin.hook.failed`
naming style used by `src/ouroboros/plugin/firewall.py`. They are
**reserved names** in this RFC and MUST be emitted (and only emitted)
by PR F's dispatcher.

| Event name                              | When                                                                              | Notes                                                                                              |
|-----------------------------------------|-----------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `plugin.tool.intercept.requested`       | The firewall has selected an `intercept` hook for a tool call, immediately before invoking the hook process. | Carries `before_tool_call` payload fields.                                                          |
| `plugin.tool.intercept.completed`       | The `intercept` hook returned a non-blocking decision and the tool call proceeded.| Final decision recorded with the original `args_digest`.                                            |
| `plugin.tool.intercept.blocked`         | The `intercept` hook explicitly vetoed the tool call (or its failure policy was `fail_closed` and it errored). | The tool call MUST NOT be dispatched; the parent plugin sees a `tool blocked` failure mode.        |
| `plugin.tool.observe.recorded`          | An `observe`-class `after_tool_call` hook recorded a successful observation event.| Never blocks; emitted at most once per tool invocation per `observe` hook.                          |

Failure of any tool-call hook process — distinct from the *blocked*
decision — continues to flow through the existing
`plugin.hook.failed` event in PR F, exactly as the v1 hook vocabulary
already does for `before_invocation` / `after_invocation`. PR F MUST
NOT introduce a parallel `plugin.tool.hook.failed` event.

## 7. Schema version migration

Tool-call hooks are **not** additive to the existing v0.3 manifest
schema. A v0.4 manifest schema MUST be introduced by PR F before any
runtime accepts `before_tool_call` / `after_tool_call`.

Required behavior:

1. **v0.3 rejection (existing).** The `hooks.name` enum in
   `src/ouroboros/plugin/schemas/0.3/plugin.schema.json` is currently
   limited to `before_invocation`, `after_invocation`, `on_error`,
   `on_cancel`. Any v0.3 manifest declaring `before_tool_call` or
   `after_tool_call` is already rejected at validation time by JSON
   Schema's `enum` constraint with **no code change required**. PR F
   MUST verify this with a regression test before introducing v0.4.
2. **v0.4 introduction (deferred).** PR F introduces
   `src/ouroboros/plugin/schemas/0.4/plugin.schema.json` and adds
   `before_tool_call` / `after_tool_call` to the v0.4 hook-name enum,
   along with the `failure_policy` / `permissions` conditionals
   described in §4–§5 above. v0.3 remains unchanged and continues to
   reject the new hook names.
3. **`schema_version` gating.** A manifest using these hooks
   MUST declare `schema_version: "0.4"`. The firewall MUST
   refuse to load a `0.4` manifest on a core build that does not
   advertise v0.4 support — the same dual-version negotiation pattern
   used everywhere else in the plugin contract.

No schema file is added or modified by this docs PR.

## 8. Dependencies

- **Depends on N1 — `workflow.*` lifecycle events
  ([#1134](https://github.com/Q00/ouroboros/issues/1134))** — MERGED.
  Tool-call audit events ride on the same lifecycle-event substrate.
- **Implemented by PR F (Wave 3+).** PR F introduces the v0.4 schema,
  dispatcher, and audit-event emission described here. The PR slug is
  reserved by this document; the issue number will be allocated when
  Wave 3 planning lands.
- **Does NOT depend on PR D (artifact/state hook contracts).** Tool-call
  hooks operate on tool invocations, not on artifact writes or state
  commits. The write-side substrate referenced by PR D remains
  deferred and is **not** a prerequisite for PR F.

## 9. Out of scope (for this docs PR)

- No code changes anywhere under `src/`. In particular,
  `src/ouroboros/plugin/manifest.py`,
  `src/ouroboros/plugin/firewall.py`, and
  `src/ouroboros/plugin/hooks.py` are not touched.
- No `src/ouroboros/plugin/schemas/0.4/` directory or schema file is
  added.
- No runtime dispatch, no audit-event emission code, no permission
  scope registrations.
- No test additions, fixtures, or conformance cases.
- No changes to existing v0.3 behavior, including the existing
  rejection of unknown hook names.

The schema-version migration described in §7 deliberately *describes*
the v0.4 introduction so that PR F can be reviewed against a frozen
target; it does not introduce v0.4 here.

## 10. References

- [#1137](https://github.com/Q00/ouroboros/pull/1137) — PR E:
  `on_error` / `on_cancel` observability hooks (most recent
  observability-hook precedent for failure-policy and audit-event
  naming).
- [#939](https://github.com/Q00/ouroboros/issues/939) — umbrella issue
  for plugin hook contract delivery, including the
  [scope-decision comment](https://github.com/Q00/ouroboros/issues/939#issuecomment-4477726327)
  that defines the PR C semantics this document encodes.
- [#1142](https://github.com/Q00/ouroboros/issues/1142) — Wave 2
  umbrella issue tracking this PR.
- [`docs/rfc/userlevel-plugins.md`](./userlevel-plugins.md) — existing
  plugin RFC that lists `before_tool_call` / `after_tool_call` as
  Deferred in the v1 hook vocabulary; this RFC is the deferred
  contract being filled in.
- [`docs/contributing/agent-os-kernel-terminology.md`](../contributing/agent-os-kernel-terminology.md)
  — terminology for firewall / hook / dispatch as used throughout this
  document.
