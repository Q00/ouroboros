# RFC: UserLevel Plugin Layer

## Status

**Accepted (2026-05-07)**, supersedes the growth-oriented portions of #725.

This RFC pins the framing decisions converged on in #725 and the seven
contract decisions locked in [Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins)
issues #5–#11. Future debates about "should this go in core?" or "should we
add this to `ooo auto`?" should be answered against this document.

### Implementation Status

"Accepted" means the **design** is locked; it does **not** mean every
artifact named below already exists in the repository. This RFC is the
**target contract** — implementer-facing prose throughout this document
uses present tense for that contract, and readers SHOULD interpret
unbuilt artifacts as RFC-2119 **MUST** (the implementation must conform
when it lands), not as a description of `main` today.

The matrix below tracks where each artifact stands at the moment this RFC
is merged. Concrete paths and commands referenced later in the document
(`src/ouroboros/plugin/firewall.py:invoke_plugin`, `scripts/sync-plugin-schemas.sh`,
`ooo plugin add`, etc.) are **target paths**, not current paths, unless
this matrix marks them as shipped.

| Artifact | Tracking issue | Status at RFC merge |
|---|---|---|
| Plugin manifest schemas under upstream `schemas/0.1/` (incl. `audit-event.schema.json`) | upstream Q00/ouroboros-plugins #6, #11 | **Shipped upstream** |
| Vendored copy at `src/ouroboros/plugin/schemas/0.1/` + `_source.json` | #736 | Not yet present in core |
| `scripts/sync-plugin-schemas.sh` | #736 | Not yet present |
| `src/ouroboros/plugin/manifest.py` (loader) | #728 | Not yet present |
| `src/ouroboros/plugin/firewall.py:invoke_plugin` (invocation contract) | #729 | Not yet present |
| `ooo plugin {add,install,trust,disable,remove}` (state-mutating CLI) | #731 | Not yet present |
| `ooo plugin {discover,inspect,list}` (read-only CLI) | #731 | Not yet present |
| `~/.ouroboros/plugins.lock` + trust store | #732 | Not yet present |
| `ooo auto` domain-keyword CI lint guard | #735 | Not yet present |
| `github-pr-ops` E2E contract proof | #733 | Not yet present |

Two consequences flow from this matrix that other sections of this
document refer back to:

1. **Boundary enforcement is currently documentary, not mechanical.** The
   "ooo auto Boundary" section describes #735 as the durable, evergreen
   control. That control activates only when #735 lands. Until then, the
   boundary is held by review discipline plus the historical evidence
   captured in that section. The clause "this RFC must be revisited if
   the guard is ever removed or weakened" therefore takes effect from the
   moment #735 ships, not from the moment this RFC merges.
2. **Implementation PRs that build the unbuilt rows above MUST conform to
   this RFC.** Drift between this contract and what those PRs ship is a
   bug in the PR, not a license to amend the RFC silently — amendments
   require a follow-up RFC change against this document.

## Motivation

Ouroboros core risks expanding indefinitely as new operational workflows are
proposed. #689's GitHub-PR work was the inflection point: it crossed two
boundaries simultaneously — it was neither an OS primitive nor part of the
`ooo auto` product boundary, yet there was no third home. The defense-oriented
plugin layer described here is that third home.

The plugin layer exists to **keep core small**, not to grow ecosystem surface
area. Specifically:

- We do not pursue plugin count, marketplace dynamics, or "ecosystem health"
  as success metrics. The success metric for Ouroboros remains the strength
  of the spec-first discipline (Interview / Seed / Evolve / Provenance) and
  the quality of execution under that discipline.
- Reference plugins are deliberately few, high-quality, and maintained by
  core authors or co-maintainers — not a long-tail catalog.
- Lock-in for Ouroboros comes from the spec-first discipline and the
  durable substrate (ledger, provenance, seed history), not from how many
  plugins exist on top.
- The plugin layer is plumbing. It exists invisibly to prevent core bloat.
  It is not a product surface.

## Layer Model

```text
+-------------------------------------------------------------------+
|                Installable UserLevel Programs                      |
|                                                                   |
|  github-pr-ops   merge-assistant   jira-sync   linear-triage       |
|  slack-incident  release-coordinator  customer-debugger  ...       |
+-------------------------------+-----------------------------------+
                                |
                                | plugin contract / declared scopes
                                v
+-------------------------------------------------------------------+
|                First-party UserLevel Programs                      |
|                                                                   |
|  ooo auto     ooo run     ooo pm     ooo review?     ...           |
|                                                                   |
|  Product-level workflows maintained with Ouroboros, but still      |
|  programs above core rather than core itself.                      |
+-------------------------------+-----------------------------------+
                                |
                                | stable OS primitives
                                v
+-------------------------------------------------------------------+
|                         Ouroboros Core / OS                         |
|                                                                   |
|  Seed      Ledger      State      Runtime      MCP                 |
|  Provenance  Safety Boundaries  Progress/Status  Handoff           |
+-------------------------------+-----------------------------------+
                                |
                                | bounded adapters / external calls
                                v
+-------------------------------------------------------------------+
|                    External Systems / Runtimes                      |
|                                                                   |
|  GitHub   Jira   Linear   Slack   CI   Local repo   Agent CLIs      |
+-------------------------------------------------------------------+
```

The same diagram lives in
[Q00/ouroboros-plugins/docs/architecture.md](https://github.com/Q00/ouroboros-plugins/blob/main/docs/architecture.md)
and is the canonical reference for both repos.

> **Note.** Ouroboros-core's own `docs/architecture.md` still presents
> the older `PLUGIN LAYER` framing at the time this RFC merges. That
> divergence is intentional and tracked in #727; the diagram above is
> the authoritative one for new work, and the core architecture doc
> will be updated to match as part of #727. Until that update lands,
> any apparent disagreement between the two diagrams MUST be resolved
> in favor of this RFC.

## Why Defense-Oriented

Three reasons the plugin layer is plumbing, not a product:

1. **Lock-in for Ouroboros comes from the spec-first discipline, not from
   plugin count.** Interview → Seed → Evolve → Provenance is the unique
   value. GitHub PR ops, Jira sync, Slack incident response — every adjacent
   tool has those. Plugins are commodity; spec-first discipline is not.
2. **Ecosystem-driven lock-in is fragile at this scale.** A healthy
   ecosystem (governance, security review, breaking-change discipline,
   support) is operationally expensive. Maintainer cost grows with the
   ecosystem and the unique value gets diluted.
3. **A user-facing "AI workflow App Store" is not what `ooo` should sell.**
   The promise of `ooo auto "do X"` is "spec-first agent does the right
   thing." Adding "...but first, browse the marketplace and install the
   right plugin" makes the entry experience worse, not better.

The success metric is therefore **"the boundary holds"** — measurable, not
"plugin count" — vague.

## Manifest Schema

Authoritative v0.1 source:
[Q00/ouroboros-plugins/schemas/0.1/plugin.schema.json](https://github.com/Q00/ouroboros-plugins/blob/main/schemas/0.1/plugin.schema.json).

Per the locked decision in
[Q00/ouroboros-plugins#6](https://github.com/Q00/ouroboros-plugins/issues/6),
the manifest carries **8 required + 2 optional** top-level fields:

- **Required (8)**: `schema_version`, `name`, `version`, `source`,
  `commands`, `capabilities`, `permissions`, `entrypoint`.
- **Optional (2)**: `description` (default `""`), `audit` (default
  `{events: [plugin.invoked, plugin.permission_used, plugin.completed,
  plugin.failed]}`).

Each required field is load-bearing for some part of the lifecycle, lockfile,
or firewall; each optional field has a sensible default the firewall provides
unconditionally.

Core supports v0.1 and a local v0.2 extension. Archived v0.1 manifests do
not accept top-level `hooks`; v0.2 adds optional hook declarations while
preserving the v0.1 fields.

The `source.type` enum is `local_path | plugin_home | first_party`. Per
[Q00/ouroboros-plugins#8](https://github.com/Q00/ouroboros-plugins/issues/8),
first-party programs share the manifest format and are registered at core
boot, bypassing the user-facing `discovered → installed → trusted` flow.

**First-party trust semantics.** Because first-party programs are shipped
inside the same release artifact as core (i.e. their manifests are not
attacker-controlled), all permissions they declare — including
`required: true` — are treated as **implicitly trusted at boot** by the
firewall (see Invocation Contract below). The boot-time registration step
populates the trust store with these grants in-process; there is no
user-visible "trust" prompt for first-party programs by default. This is
the deliberate contract: first-party programs MAY declare `required: true`
permissions, and conforming firewalls MUST NOT block them on the trust
check. Plugins that are not first-party never receive this treatment
regardless of `source.type`.

**First-party persistence model — one rule, three references aligned.**
The user lockfile (`~/.ouroboros/plugins.lock`) holds **zero** first-
party records, ever, in any state (trusted or disabled). All durable
state for first-party programs lives in a sibling override file
`~/.ouroboros/first-party-overrides.json`, owned by core. The override
file contains one entry per first-party program **name** that the user
has explicitly disabled; nothing else is persisted there.

The override is keyed by `name` only — **not** by
`(name, artifact_digest)`. This is the deliberate contract: an explicit
user `disable` MUST persist across ordinary core upgrades, even though
upgrades change `artifact_digest`. Pinning the override to a particular
digest would silently re-enable the program on the next release that
touches its bytes, which would be a surprising and security-relevant
regression of the user's last explicit decision. The override is
released only by an explicit `ooo plugin trust <name>` (the user
re-decides), or by removing the program entirely from a future release
(at which point the orphaned entry simply does not apply to anything
and may be GC'd).

The boot/disable/re-enable cycle works against that single file:

1. **Boot.** For every first-party program present in the release
   artifact, the firewall looks up `name` in the override file. If
   absent, the program is implicitly trusted in-process (no lockfile
   write, no override write — the trust is fully derived from "release
   artifact present" + "no override saying otherwise"). If present,
   the program starts disabled: required permissions are stripped
   from the in-process trust table and invocation is refused. The
   override carries forward across upgrades unchanged.
2. **`ooo plugin disable <name>` for a first-party program.** Writes
   the `name` entry into the override file (the `artifact_digest` at
   the time of disable MAY be recorded as audit metadata, but it is
   **not** part of the key). No lockfile changes. Effective
   immediately and on every subsequent boot, including across
   upgrades.
3. **`ooo plugin trust …` for a disabled first-party program.**
   Removes the matching entry from the override file. No lockfile
   write occurs — first-party trust grants are never persisted in
   the lockfile and the in-process implicit grant re-attaches at the
   next boot (or immediately, in the same process). The CLI presents
   this re-trust as an explicit confirmation prompt for audit
   symmetry, but the persistence target is the override file, not
   the lockfile.

The follow-on consequence this model bakes in: the lockfile remains a
clean, third-party-only artifact, so any tooling that audits "what
third-party plugins do I trust?" never has to filter first-party rows
out.

Disabled first-party programs remain installed (they're part of the
core release artifact, so they cannot be uninstalled separately) but
are not invocable until the override entry is cleared.

The manifest schema versions per
[Q00/ouroboros-plugins#11](https://github.com/Q00/ouroboros-plugins/issues/11):
SemVer-style `MAJOR.MINOR`. Each released `MAJOR.MINOR` lives in its own
directory under upstream `schemas/<MAJOR.MINOR>/` (e.g. `schemas/0.1/`,
`schemas/0.2/`, `schemas/1.0/`). The support window is *current MAJOR +
previous MAJOR*; older MAJORs may be retained for archival reading but are
out-of-window for compatibility guarantees.

### Vendoring strategy in core (resolves #736)

Ouroboros core vendors the schemas at
`src/ouroboros/plugin/schemas/<MAJOR.MINOR>/`, **mirroring the upstream
directory layout one-for-one** (so the URL `schemas/0.1/plugin.schema.json`
maps to vendored `src/ouroboros/plugin/schemas/0.1/plugin.schema.json`).
Each vendored directory contains a `_source.json` recording the upstream
git SHA at the time of the copy. The `scripts/sync-plugin-schemas.sh`
script copies all in-window MAJOR.MINOR directories from a pinned upstream
SHA. CI may surface drift as a warning until the schemas stabilize at v1;
this is intentionally less strict than a hard error to keep bring-up
smooth.

## Invocation Contract

Every UserLevel plugin command flows through one wrapper —
`src/ouroboros/plugin/firewall.py:invoke_plugin` (#729).

The wrapper's responsibilities, in order:

1. **Disable and install-subject gates.** If the plugin is disabled, or if
   the installed `plugin_home` digest cannot be verified against the trusted
   subject, emit only `plugin.failed` and refuse to run plugin-controlled
   code.
2. **Pre-invocation trust check.** If any `required: true` permission is not
   trusted, emit only `plugin.failed` with `result.status="blocked"` and a
   message naming the missing scope and the exact `ooo plugin trust ...`
   command to run (the canonical CLI entrypoint for the lifecycle commands;
   `ouroboros` is not a separate user-facing command). **No `plugin.invoked`
   is emitted** — the plugin never started.
3. **Caller-supplied cancellation check.** If `invoke_plugin` is called with
   `cancellation_requested=True`, emit terminal `plugin.failed` with reason
   `cancelled`, then run `on_cancel` observability hooks if declared. This is
   the current bounded API surface; wiring a production cancellation source
   into that parameter is separate integration work.
4. **Confirmation gate.** If the resolved command has
   `requires_confirmation: true`, show a single confirmation prompt. Per
   [Q00/ouroboros-plugins#9 Q2](https://github.com/Q00/ouroboros-plugins/issues/9),
   this is the only confirmation; permission risk is handled at trust grant
   time.
5. **Emit `plugin.invoked`** before launching the entrypoint subprocess.
6. **Emit `plugin.permission_used`** for each `required: true` permission
   declared in the manifest. Optional permissions (`required: false`) are
   not emitted by default in v0; this is the deliberately coarse Option (a)
   from #729's spec. The path to graduate to per-call granular emission
   (stderr-line or sidecar file) is open but not implemented.
7. **Run entrypoint** out-of-process (subprocess via the manifest's declared
   command).
8. **Emit `plugin.completed` or `plugin.failed`** with `result.status` and
   the subprocess exit code.

Audit events conform to
[Q00/ouroboros-plugins/schemas/0.1/audit-event.schema.json](https://github.com/Q00/ouroboros-plugins/blob/main/schemas/0.1/audit-event.schema.json).
The compatibility surface between this schema and the existing core ledger
writer is tracked in #737.

## Lifecycle Hook Contract

This section is the locked design target for #939 PR 1. It defines the
vocabulary and safety policy for plugin lifecycle hooks, but it does **not**
make hook execution part of the v0.1 manifest/runtime contract yet. Until a
later PR updates the manifest schema and firewall implementation, conforming
core releases MUST continue to treat plugin entrypoints exactly as described
in the Invocation Contract above.

The intent is to keep the plugin layer extensible without letting plugins
reach around the harness. Hooks are harness callbacks, not a second runtime:
they run only when the firewall/orchestrator invokes them, with bounded input,
bounded output, explicit permissions, and audit events.

### v1 hook vocabulary

The first implementation should start with the smallest hook set that proves
the contract and keeps review scope small.

| Hook | Phase | Side-effect class | Default failure policy | Required permission class | v1 status |
|---|---|---|---|---|---|
| `before_invocation` | After trust/confirmation, before `plugin.invoked` | Read-only inspection / policy | `fail_closed` for policy hooks, `fail_open` for observability-only hooks | `plugin:lifecycle:read` for read-only, `plugin:lifecycle:policy` for policy/veto decisions | **Included** |
| `after_invocation` | After `plugin.completed` / `plugin.failed` is known, before the wrapper returns to the caller | Observability / summary emission | `fail_open` | `plugin:lifecycle:read` | **Included** |
| `before_tool_call` | Before a plugin-mediated tool call is allowed to execute | Policy / possible mutation gate | `fail_closed` | tool-specific permission plus `plugin:tool:intercept` | Deferred |
| `after_tool_call` | After a plugin-mediated tool call result is available | Observability or result annotation | `fail_open` unless it mutates returned evidence | `plugin:tool:observe` | Deferred |
| `before_artifact_write` | Before artifact service writes plugin-provided output | Policy / mutation gate | `fail_closed` | artifact-specific write permission | Deferred |
| `after_artifact_write` | After artifact write completes | Observability | `fail_open` | `plugin:artifact:observe` | Deferred |
| `on_error` | When the wrapper sees a plugin/runtime error | Observability / recovery hint | `fail_open`; MUST NOT mask the original error | `plugin:lifecycle:read` | **Included** |
| `on_cancel` | When a plugin invocation is cancelled | Observability / cancellation summary | `fail_open`; MUST NOT perform cleanup side effects | `plugin:lifecycle:read` | **Included** |

`on_error` and `on_cancel` are the terminal-outcome subset of the v1 hook
vocabulary, promoted out of the deferred bucket by Wave 1 PR E (#1131, refs
#939 scope decision). They run only after the firewall has emitted the
terminal `plugin.failed` event for the corresponding command lifecycle, are
gated by the read-only `plugin:lifecycle:read` permission, and must declare
`fail_open` — a hook failure cannot mask the original error/cancel cause that
already reached the caller through the `InvocationResult` and the terminal
audit event. Cleanup side effects remain explicitly out of scope for this
v0.3 terminal observability surface.

The following candidate hooks are intentionally **not** in the v1 hook
vocabulary:

- `before_runtime_start` / `after_runtime_start`: runtime adapters and
  capability policy are still Track C Tier 2 work; plugins MUST NOT receive a
  runtime-start interception point before that substrate is stable.
- `before_state_commit` / `after_state_commit`: state/replay projections are
  owned by #946; adding state hooks first would create a second state mutation
  path.
- `on_event`: too broad for v1. It risks turning the audit/event stream into a
  plugin message bus.
- `on_rewind`: rewind semantics depend on replay/projection contracts that are
  not yet stable.

### Hook ordering

For the included v1 hooks, the intended happy-path order is:

```text
trust check
confirmation gate
before_invocation hook(s)
plugin.invoked
plugin.permission_used*
entrypoint subprocess
plugin.completed | plugin.failed
after_invocation hook(s)
on_error hook(s), only for failed launched commands
return InvocationResult
```

`plugin.permission_used*` keeps the current v0 behavior: one event for each
declared `required: true` permission. Hook-specific permission emission is a
future extension; v1 hook PRs MUST NOT silently change the coarse permission
emission model.

The placement of `before_invocation` is deterministic: it runs only after the
pre-invocation trust check and any command confirmation have succeeded, and it
runs before `plugin.invoked` is emitted. If a required permission is not trusted
or the confirmation gate is rejected, `before_invocation` MUST NOT run. If a
`fail_closed` `before_invocation` hook blocks the call, the wrapper emits the
hook audit event(s) and the blocked invocation result; it MUST NOT emit
`plugin.invoked`, `plugin.permission_used`, `plugin.completed`, or
`plugin.failed` for the command entrypoint because the plugin command never
started.

`after_invocation` is scoped to started command entrypoint invocations only:
it runs only after that entrypoint reaches `plugin.completed` or
`plugin.failed`. It MUST NOT run for pre-start terminal outcomes such as trust
denial, confirmation rejection, or a `fail_closed` `before_invocation` block.
Those outcomes are represented by `plugin.failed` for trust/confirmation
denials or by `plugin.hook.blocked` / `plugin.hook.failed` for hook failures,
as applicable.

`on_error` is scoped to failed launched commands and command-launch/runtime
failures. For a launched command that exits non-zero and declares both
`after_invocation` and `on_error`, the order is strictly `plugin.failed`,
then `after_invocation` hook events, then `on_error` hook events.

`on_cancel` runs only when the `invoke_plugin` caller supplies
`cancellation_requested=True` and only after the disable gate,
`plugin_home` digest/tamper verification, and required-permission trust check
have passed. It runs after the terminal cancelled `plugin.failed` event and
before confirmation, `before_invocation`, `plugin.invoked`, permission
emission, or command launch. It MUST NOT run for disabled, untrusted, or
tampered plugins.

### Failure and timeout policy

Every hook declaration must resolve to one of these failure policies:

| Policy | Meaning | Allowed for |
|---|---|---|
| `fail_open` | Record hook failure and continue the original invocation. | Observability-only hooks whose output cannot authorize or mutate work. |
| `fail_closed` | Stop the original invocation and emit a failed/blocked audit result. | Policy, security, mutating, or authority-bearing hooks. |

Timeouts are failures for policy purposes. A hook that times out under
`fail_open` produces an audit event and the original invocation continues. A
hook that times out under `fail_closed` blocks the original invocation. The
default timeout is intentionally short and implementation-defined in v1; the
manifest/schema PR must document the exact default and any maximum override.

Hooks MUST NOT retry indefinitely. Any retry policy must be bounded and must
preserve the original invocation's idempotency and audit ordering.

### Permission and mutation boundaries

Hooks inherit the same trust model as commands: a required permission that is
not trusted blocks before plugin-controlled code runs. Additional rules:

1. Read-only lifecycle hooks require at least a read lifecycle scope such as
   `plugin:lifecycle:read`.
   Lifecycle permission scopes use the same colon-delimited grammar as manifest
   `permissions[].scope`; dot-delimited forms such as `plugin.lifecycle.read` are
   invalid.
2. Hooks that can block, authorize, rewrite, or mutate work require an explicit
   policy/mutation permission. Lifecycle policy/veto hooks require
   `plugin:lifecycle:policy` and default to `fail_closed`.
3. Hooks MUST NOT directly edit `.omx`, EventStore rows, artifacts, or user
   files. Mutations must go through the same harness service boundary that the
   underlying command would use.
4. Hook output is advisory unless the hook is declared as a policy hook and the
   caller explicitly consumes the decision.
5. Plugin permission approval is specified by the typed HITL contract below.
   This RFC still does not implement a plugin prompt renderer; until a caller
   wires a #960 HITL surface, required permission denial remains fail-closed at
   the firewall rather than falling back to a hook-local prompt.


### Plugin permission HITL contract (#960)

When the plugin firewall needs human permission approval, it must express that
wait through the shared `hitl.*` event contract instead of prompting from plugin
code. This section is a contract/specification slice only; it does not add a
plugin prompt runtime, scheduler, or UI renderer.

Required request fields:

| HITL field | Required value for plugin permission waits |
|---|---|
| `type` | `hitl.requested` |
| `kind` | `approval` for ordinary permission grants; `destructive_confirmation` when the requested scope can delete, overwrite, deploy to production, or otherwise cause irreversible side effects |
| `source` | `plugin_firewall` |
| `required_permission` | the exact manifest/firewall scope being requested, for example `plugin:lifecycle:read` or `external:production:deploy` |
| `risk_class` | `low` for read-only local inspection; `material_branch` for local mutation or policy-changing scopes; `credential_gated` for secret/credential authority; `external_production` for non-destructive production/external effects; `destructive` for irreversible deletion, overwrite, or production deployment |
| `resume_target` | a firewall-owned target such as `plugin-firewall:permission:<session-or-invocation-id>` |
| `surface` | the renderer that owns the question, for example `plugin.firewall.permission`, `cli.plugin.permission`, or a future MCP/TUI permission surface |
| `payload` | bounded, non-secret metadata such as `plugin_id`, `permission_scope`, `permission_reason`, and `invocation_id` |

Responses must be recorded as `hitl.answered` with `response_kind=approval`.
`approval_decision=true` resumes the firewall path that originally requested the
permission; `approval_decision=false` is a denial and must remain fail-closed.
Aborted renderers must persist `hitl.cancelled`; expired permission waits must
persist `hitl.timed_out` and use the contract's timeout action rather than
letting plugin code continue silently.

Secret material must not be stored in HITL payloads or answers. If the user must
provide credentials, the permission request should point at a credential-gated
external surface and persist only a non-secret reference or denial reason.

Non-goals for this slice:

1. no plugin-controlled `input()`/prompt fallback;
2. no bypass of manifest permission validation;
3. no persistence of tokens, API keys, or credential values;
4. no scheduler or UI implementation for pending plugin waits; and
5. no conversion of plugin permission waits into JobManager jobs.

### Hook audit events

Hook execution must be observable without storing unbounded output. The v1
lifecycle dispatch slice uses these event names for the minimal
`before_invocation` / `after_invocation` wrapper:

- `plugin.hook.invoked`
- `plugin.hook.completed`
- `plugin.hook.failed`
- `plugin.hook.blocked`

The current v0.3 schema slice vendors the complete v1 hook event vocabulary
needed by this minimal wrapper: `plugin.hook.invoked`,
`plugin.hook.completed`, `plugin.hook.blocked`, and `plugin.hook.failed`.
In code, `HOOK_EVENT_TYPES` is the canonical runtime set,
`HOOK_OUTCOME_AUDIT_EVENTS` names the blocked/failed subset, and
`HOOK_AUDIT_EVENTS` remains only a backward-compatible alias for the original
#984 outcome-event export.

Hook event payloads follow the same bounded-payload rules as command audit
events:

- include plugin identity, command name when applicable, hook name,
  invocation/session correlation, failure policy, timeout, and status;
- store stdout/stderr only as sha256 hashes; raw stdout/stderr and bounded
  previews MUST NOT be copied into the ledger;
- apply the argv/secret redaction rules above;
- do not add fields outside the vendored audit schema until the schema is
  updated in `ouroboros-plugins` and re-vendored into core.

Core MUST NOT emit any new `plugin.hook.*` event until the upstream
audit-event schema and vendored core copy both include that event name.

### Plugin descriptor/action projection

The plugin manifest also projects into a harness-readable descriptor without
executing plugin code. This read model is the bridge between manifest validation
and future conformance checks: it exposes `plugin_id`, schema version, source,
entrypoint, declared capabilities, declared permissions, lifecycle hooks, audit
events, and command-level action descriptors.

Descriptor projection is intentionally not a dispatch surface. It MUST NOT import
plugin modules, invoke entrypoint commands, grant permissions, or widen the v0.3
hook vocabulary. Runtime permission checks, hook execution, and audit emission
remain owned by the firewall.

### v0.3 lifecycle conformance baseline

The v0.3 lifecycle wrapper has a compatibility matrix that future plugin work
MUST keep green before promoting more hook kinds. The baseline covers:

- manifests with no hooks, which keep the standard command audit sequence;
- `before_invocation` fail-open observation, which records hook failure and
  continues the original invocation;
- `before_invocation` fail-closed policy, which blocks before `plugin.invoked`;
- `after_invocation` fail-open observation after terminal command events;
- explicit `audit.events` lists that include every runtime-emitted command and
  hook event;
- lifecycle permission trust-gate failures before any hook dispatch; and
- timeout, malformed-entrypoint, non-zero-exit, and startup-error paths that
  keep hook output bounded to hashes when output exists.

This matrix is a regression baseline, not a new feature surface. It MUST NOT
promote deferred hook names or change v0.3 schema compatibility by itself.

### Review boundary for follow-up PRs

The hook rollout should remain reviewable:

1. **Contract/docs PR** (this section): vocabulary and policy only.
2. **Manifest/schema PR**: optional hook declarations with backward
   compatibility for existing v0.1 manifests.
3. **Policy validator PR**: hook permission, timeout, and failure-policy
   validation without executing hooks.
4. **Audit-event schema/vendoring PR**: add `plugin.hook.*` event support
   upstream and re-vendor it into core before any runtime emits hook events.
5. **Minimal invocation PR**: `before_invocation` / `after_invocation` only,
   with fixture tests.
6. **Deferred hook PRs**: tool, artifact, state, or rewind hooks only after the
   corresponding harness substrate is stable.

This sequencing is intentional: it prevents hook support from becoming a
second unreviewable plugin runtime.

### Audit-event compatibility (resolves #737)

The audit-event schema is the canonical shape for plugin-emitted events. The
core ledger writer accepts these events as-is, with any core-level envelope
(e.g. ledger-internal sequence numbers) added at a layer **above** the
schema's `additionalProperties: false` boundary. No silent field truncation
or expansion is permitted; mismatches produce errors, not warnings.

**Bounded payloads — argv handling.** The "tokens, channel IDs, and
free-form user messages are forbidden" rule applies to **plugin-defined
audit fields** (fields the plugin populates inside `plugin.invoked` /
`plugin.permission_used` / `plugin.completed` / `plugin.failed` event
payloads). For `argv` specifically the contract is **defense in depth**;
treating argv as either fully trusted or fully redacted is unsafe.

1. **Plugins MUST NOT accept secrets via argv.** Plugin authors MUST
   document a secure path (env var, file, OS keychain) for any
   credential a command needs and MUST reject argv-supplied secrets at
   parse time when feasible. This is the primary control.
2. **The firewall MUST apply a built-in argv redaction policy before
   ledger write**, as a safety net for the case where rule (1) is
   violated by accident. The minimum policy redacts:
   - Values of well-known secret flags by name match
     (e.g. `--token=…`, `--password=…`, `--api-key=…`, `--secret=…`,
     and the value position immediately following those flags),
   - Tokens with high-confidence formats (`Bearer …`, `gh[oprsu]_…`,
     `sk-…`, AWS-style `AKIA…`, JWT-shaped strings with three
     dot-separated base64url segments).
   Redacted positions are replaced with the literal string `[redacted]`
   in the ledger record. The hash of the original argv (sha256 over the
   un-redacted form) MAY be recorded alongside for forensic
   reconciliation, but the original value MUST NOT.
3. **Plugins MAY tighten the policy per command.** A plugin MAY declare
   additional flags or positional indexes to redact via a future
   manifest extension; the v0 manifest does not yet expose this, so v0
   redaction is exactly the built-in policy in (2). Adding the
   per-command redaction list is tracked alongside the granular
   permission emission work in the Deferred Decisions section.
4. **Caller responsibility persists.** The firewall's safety net does
   not absolve callers (`ooo` CLI, first-party programs, scripts/CI) of
   the obligation to keep secrets out of argv in the first place; the
   safety net exists to limit blast radius, not to make argv a
   sanctioned secret channel.

Provenance fields in audit events are string-only per the
[`audit-event.schema.json`](https://github.com/Q00/ouroboros-plugins/blob/main/schemas/0.1/audit-event.schema.json)
constraint set (the schema is the canonical source; this RFC does not
introduce a separate `docs/audit.md` contract). Raw stdout/stderr is
**not** copied into the ledger; only a sha256 hash is recorded for
forensic comparison.

## UX

The user-facing install path is `ooo plugin add <repo-url>`. The repository
URL is the unit of distribution; the catalog inside the repository is the
unit of selection. Full UX details:
[Q00/ouroboros-plugins/docs/lifecycle.md](https://github.com/Q00/ouroboros-plugins/blob/main/docs/lifecycle.md).

**`add` vs `install`.** `add` is the **interactive entry point** intended
for humans: it accepts a repo URL, fetches the catalog, presents the
selection prompt, and then internally invokes `install` for each selected
plugin. `install` is the **non-interactive primitive** used when scripts
or CI need to bypass the selection prompt. The two commands are layered,
not redundant: `add` calls `install`; `install` never calls `add`.

`install` MUST be unambiguous about which `(source_identity, digest)`
it is targeting:

- **Default form** — `ooo plugin install <name>` — succeeds **only if
  exactly one** known catalog (a previously `add`-ed `plugin_home`
  repository or a previously registered `local_path` source — see
  below) exposes a plugin with that `name`. If two or more sources
  expose the same `name`, the command MUST exit with an "ambiguous
  plugin name" error listing the candidate sources; it MUST NOT pick
  one heuristically.
- **Qualified form** — `ooo plugin install <name> --from <repo-url>`
  (or `--from <local-path>`) — selects an explicit source and is
  required whenever the default form would be ambiguous. CI / scripts
  SHOULD prefer this form unconditionally, because catalog membership
  can change over time and the qualified form is stable across that
  drift.
- **No catalog match** — `ooo plugin install <name>` with no known
  source providing `<name>` MUST instruct the user to either
  `ooo plugin add <repo-url>` first (for a `plugin_home` source) **or**
  re-run `install` in the qualified form
  `ooo plugin install <name> --from <local-path>` (for a `local_path`
  source — that form is itself the registration verb, per the catalog-
  registration rules above). The error message MUST mention both
  paths so users with a local checkout are not misdirected to `add`.
  In no case may `install` silently search the network.

**How sources enter the known catalog.** v0 has exactly two registration
paths, one per `source.type` that can produce a user trust record:

- `plugin_home` sources are registered by `ooo plugin add <repo-url>`
  (the repo URL becomes a known catalog at that moment, regardless of
  whether the user proceeds to install anything from the selection
  prompt). Subsequent `install`s can address that `name` without
  re-fetching.
- `local_path` sources are registered the first time the user runs
  `ooo plugin install <name> --from <local-path>` against an absolute
  path. The qualified form is therefore both a register-on-first-use
  and an install in the same command; there is no separate
  `register-local` verb in v0. The path is recorded as a known catalog
  in the trust store; later `install <name>` calls can address it via
  the default form if it is unambiguous.

`first_party` sources do not go through registration at all — they are
populated at boot from the core release artifact, as documented under
the Manifest Schema section.

**Plugin name → command-namespace mapping.** Every installed plugin's
manifest `name` field IS the user-facing command namespace, with no
aliasing: a plugin named `github-pr-ops` is invoked as
`ooo github-pr-ops <command> [args...]`, where `<command>` is one of
the entries declared in the manifest's `commands` array (each `commands`
entry's own `name` is the subcommand). Aliases and short names are
explicitly out of scope for v0.

`ooo plugin install` MUST refuse a new install whose manifest `name`
collides with **any** name already occupying the top-level `ooo`
command namespace, not just other installed third-party plugins. The
reserved set at the moment of the check is the union of:

- every first-party UserLevel program currently registered at boot
  (`auto`, `run`, `pm`, `plugin` itself, and any other first-party
  program shipped in the same release artifact);
- every built-in `ooo` subcommand or top-level option that is not a
  first-party program (e.g. `help`, `version`, `--version`); and
- every other third-party plugin currently installed.

A name collision MUST produce an explicit error naming the conflicting
occupant — never silently shadow it. This prevents a third-party plugin
from hijacking dispatch for a name like `auto` or `run`. Renaming on
the plugin side (manifest `name` change ⇒ new `artifact_digest` ⇒ fresh
trust subject) is the only path to install such a plugin.

**Collisions detected at boot** (the upgrade case). The same uniqueness
invariant MUST be enforced at boot, because a new core release can
introduce a first-party program whose name collides with an
already-installed third-party plugin from a previous release. When the
firewall detects such a collision at boot:

1. **First-party wins** for command dispatch — the new first-party
   program is registered under that name. This preserves the release
   contract: a core release is allowed to ship new commands.
2. The conflicting third-party plugin is **auto-disabled** (its
   `disabled: true` flag is set) and its required permissions are
   stripped from the in-process trust table. It remains installed on
   disk so the user does not lose data.
3. The firewall MUST emit an explicit `plugin.failed` event with
   `result.status="name_collision_with_first_party"` for the disabled
   plugin, and `ooo plugin list` MUST surface the disabled state with
   the reason. The plugin is not invocable until the user resolves the
   conflict by `ooo plugin remove` (and, if desired, reinstalling
   under a different name from a re-published catalog).

There is no silent shadowing in either direction — install-time and
boot-time both refuse the ambiguous state explicitly.

**Trust identity is NOT the manifest `name`.** Manifest `name` controls
the CLI namespace and the install-time uniqueness check; it does **not**
identify the trust subject. Trust records — and the lockfile entries in
`~/.ouroboros/plugins.lock` — MUST be keyed by the tuple

```text
( source.type , source_identity , artifact_digest )
```

where `source_identity` is dispatched on `source.type` so the key works
across all enum values (this matters because `source.type` is
`local_path | plugin_home | first_party`, none of which is necessarily
URL-shaped):

| `source.type` | `source_identity` | Notes |
|---|---|---|
| `plugin_home` | normalized repo URL the plugin was added from | Normalization is **strict and conservative**: it strips a trailing `.git`, the URL fragment, and any embedded `userinfo` (`user:pass@`), but **preserves the scheme exactly** (so `http://…` and `https://…` are distinct trust subjects) and preserves the host case-insensitively. Aliasing across schemes, hosts, or paths produces a different `source_identity` and forces fresh trust |
| `local_path` | the absolute, resolved filesystem path of the plugin directory at install time | Symlinks are resolved; relative paths are rejected. Two installs of the same path resolve to the same `source_identity` |
| `first_party` | the manifest `name` (the program is shipped inside the core release artifact) | First-party programs do not produce trust records in the user lockfile; their required permissions are populated as implicitly trusted at boot per the Manifest Schema section. The triple is recorded in core's in-process trust table for audit symmetry, not in `~/.ouroboros/plugins.lock` |

`artifact_digest` is the sha256 of the **complete installed artifact**,
not just the manifest, computed at install time and re-verified before
each invocation. The hashing input is dispatched per source type so it
covers all executable bytes the plugin will run:

| `source.type` | `artifact_digest` input | Re-verification rule |
|---|---|---|
| `plugin_home` | sha256 of the **canonical tree hash** of the installed plugin subtree under `~/.ouroboros/plugins/<...>/`, computed independently of any tar dialect (see "Canonical tree hash" below) so arbitrary path lengths and link targets are covered without relying on `ustar`/`pax` quirks | Recorded at `install` / `add` time AND **recomputed before every invocation** against the on-disk subtree. If the recomputed digest does not match the trusted record (e.g. a user or another process edited the installed bytes), the firewall MUST emit `plugin.failed` with `result.status="trust_subject_changed"` and refuse to run; the user must re-issue `ooo plugin trust ...` to re-confirm the new artifact. There is no "recompute only at install" shortcut |
| `local_path` | the same canonical tree hash, computed against the absolute path on disk | Same per-invocation re-verification rule as `plugin_home`. The path is mutable in place, so the firewall MUST recompute the digest before each invocation and fail closed on drift, as above |
| `first_party` | the same canonical tree hash applied to the entrypoint file (or the program's subtree if it ships as a directory), computed at boot | Rolls with each core release; restart picks up the new digest and the boot-time grant attaches to the new triple. Per-invocation re-verification is not required because the core release artifact is the unit of trust here — tampering with it is out of scope for the plugin firewall |

**Canonical tree hash.** To avoid `ustar`/`pax` lossiness for long paths
or extended attributes, the digest is computed without going through a
tarball at all:

1. Walk the subtree depth-first, collecting an entry for each regular
   file and each symlink (directories are implicit). Reject other file
   types (devices, FIFOs, sockets) at install time.
2. For each entry, build a record `<mode>\0<path>\0<sha256-of-content
   or link-target>\0`, where:
   - `<mode>` is the octal POSIX mode masked to the executable bit
     (`0o755` or `0o644` for files; `0o777` for symlinks);
   - `<path>` is the path relative to the subtree root, with `/` as
     separator and **no length cap** (this is what `ustar` couldn't do);
   - `<sha256-of-content>` is the hex sha256 of the file's bytes; for
     symlinks it's the hex sha256 of the link target string.
3. Sort the records lexicographically by `<path>` (NUL is sorted as
   raw byte 0x00).
4. The canonical tree hash is `sha256(concat(sorted records))`.

This serialization is deterministic, covers the entire executable
surface (including symlinks), and has no implementation-defined
truncation. Implementations MAY cache the hash but MUST recompute it
on the cadence specified in the table above.

Manifest-only binding is **insufficient** and explicitly rejected: an
attacker (or a careless edit) that swaps the entrypoint while leaving
the manifest untouched would otherwise inherit the prior trust. Binding
to the artifact digest closes that code-substitution path.

This collectively closes the two permission-escalation paths the trust
subject is designed to defeat:

1. **Same-name reinstall under a different source** — `remove` + `add`
   from a different repository produces a new `source_identity`, hence
   a new triple, hence un-trusted required scopes.
2. **Code substitution under the same source** — modifying the
   entrypoint without touching the manifest produces a new
   `artifact_digest`, hence a new triple, hence un-trusted required
   scopes. For `plugin_home` and `local_path` this is checked on
   **every invocation** (the firewall recomputes the canonical tree
   hash defined above against the on-disk subtree before each call
   and fails closed on drift, exactly as the per-source-type rule
   above specifies — note that the digest is **not** a tarball hash;
   the tar-independent serialization is what makes long paths and
   symlinks safe). For `first_party` it is checked at **boot only**,
   on the explicit grounds that the core release artifact is the unit
   of trust there and tampering with it is out of scope for the
   plugin firewall.

Concrete obligations on the lifecycle commands:

- `ooo plugin remove <name>` MUST delete **every trust record for the
  install subject — past and present**, not only records bound to the
  currently-active triple. The lockfile entry shape includes the
  plugin's manifest `name` alongside its `(source.type,
  source_identity, artifact_digest)`, so the deletion scope is records
  matching `(name, source.type, source_identity)` for any historical
  `artifact_digest` — explicitly **scoped to this plugin name**, never
  to other sibling plugins installed from the same `plugin_home` repo
  URL or the same `local_path` directory. (A catalog repo can host
  multiple plugins; removing one MUST NOT de-trust its siblings.)
  After clearing those records, `remove` also removes the installed
  snapshot for the `plugin_home` plugin (its own subdirectory under
  `~/.ouroboros/plugins/<...>/`, not the parent catalog) or, for
  `local_path`, removes only this plugin's catalog registration while
  leaving the on-disk path untouched. `remove` ALSO deletes any
  disable record for the plugin's install subject — once the user has
  uninstalled it, the disable signal no longer applies and a future
  fresh install starts un-trusted-but-enabled (the standard new-trust
  prompt path), not silently disabled. This closes the otherwise-silent
  regrant path where a user could downgrade or reinstall back to an
  old digest and inherit prior trust, while preserving siblings. No
  "tombstone with implicit re-grant" behavior is permitted — uninstall
  fully revokes, including for any earlier version of the same install
  subject.
- `ooo plugin install` MUST compute the new triple (recomputing
  `artifact_digest` from the just-installed bytes, not copying it from
  upstream metadata) and, if any field differs from a previously-trusted
  record for the same `name`, MUST treat the install as a fresh trust
  subject (all `required: true` permissions begin un-trusted).
- `ooo plugin trust ...` writes a trust record bound to the current
  triple of the named plugin. Trust does NOT carry across triple
  changes, ever, including version bumps from the same source (digest
  changes ⇒ new trust subject). `trust` also clears any disable record
  for the plugin's install subject (see `disable` below) — that is the
  re-enable path.
- `ooo plugin disable <name>` is the **revocation primitive** for both
  third-party plugins and first-party programs. The third-party
  lockfile carries two kinds of records, each with its own key shape,
  precisely so the disable signal cannot be lost by a digest rotation:
  - **Trust records** — keyed by `(name, source.type, source_identity,
    artifact_digest)` — express "the user has granted these scopes to
    *this exact artifact*". They are wiped whenever any field of the
    triple changes.
  - **Disable records** — keyed by `(name, source.type,
    source_identity)` (no `artifact_digest`) — express "the user has
    disabled this plugin, regardless of which version is installed".
    They survive every digest change, including upgrades and any
    `remove + add` cycle that lands the same `(source.type,
    source_identity)` again. (For first-party programs the disable
    records live in `~/.ouroboros/first-party-overrides.json` keyed
    by `name` only, per the Manifest Schema section above; the lock-
    file holds zero first-party rows.)

  `disable` MUST therefore:
  - delete every trust record bound to the plugin's install subject
    (every `artifact_digest` for the matching
    `(name, source.type, source_identity)`), so the firewall's
    pre-invocation trust check refuses on the next invocation as if
    the user had never run `trust`;
  - write a disable record keyed by `(name, source.type,
    source_identity)` (or, for first-party, by `name`) — this record
    is the binding signal across upgrades; it is **not** stored on a
    trust-record row and is **not** lost when the trust row's digest
    changes;
  - leave the installed bytes and manifest in place, so re-enabling
    is cheap and identity-preserving.

  The firewall MUST consult the disable record before any invocation,
  independently of whether trust records exist. A plugin with no
  `required: true` permissions therefore cannot bypass `disable` by
  having an empty trust subject — the disable check fires first and
  fails closed regardless of permission shape.

  Re-enabling is performed by re-running `ooo plugin trust …` on the
  same plugin: that deletes the disable record AND writes fresh trust
  records bound to the *current* triple. There is no separate `enable`
  verb in v0; the trust prompt is the only re-grant entrypoint, which
  keeps every grant decision explicit and recorded.
- The deferred `update` flow, when it lands, MUST surface the digest
  change to the user and require explicit re-confirmation of any
  permission whose risk class changed; until `update` exists, the
  documented upgrade path is `remove` + `add`, which by construction
  forces fresh trust grants.

```bash
$ ooo plugin add https://github.com/Q00/ouroboros-plugins
Repository: Q00/ouroboros-plugins (b3a91f2)

Select plugins to install:

  [x] github-pr-ops      0.1.0   review and prepare PR merges

Press space to toggle, enter to confirm, esc to cancel.

$ ooo plugin trust github-pr-ops --scope github:read
$ ooo github-pr-ops review https://github.com/Q00/ouroboros/pull/725
```

Anti-pattern install strings (e.g.
`git+https://github.com/Q00/ouroboros-plugins.git#plugins/github-pr-ops`)
are explicitly rejected because they leak repository layout into the
user-visible URL. Plugin authors must be free to refactor their repos
without breaking installs.

## Reference Plugin

[Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins) is the
**curated** reference repo, not a marketplace. It hosts the contract
artifacts (schemas, validator) and one v0 reference plugin —
`github-pr-ops` — whose purpose is to **prove the contract**, not populate
an ecosystem. Other plugins live in their authors' own repositories and
install via `ooo plugin add <author-repo-url>`.

`github-pr-ops` ships with one command: `review` (read-only). The
destructive `merge` command is intentionally absent from v0 per
[Q00/ouroboros-plugins#7](https://github.com/Q00/ouroboros-plugins/issues/7);
it returns when the destructive trust UX (#9) is in place.

## ooo auto Boundary

`ooo auto` is a **first-party UserLevel program**, not core. Its product
boundary is permanent:

```text
goal → clarification/interview → Seed → validation → execution handoff
```

Domain-specific operational workflows do not live here. They live in
plugins.

**Historical rationale (not an evergreen claim).** When this RFC was
drafted, `grep -nE 'github|pull_request' src/ouroboros/cli/commands/auto.py src/ouroboros/auto/pipeline.py`
returned empty, and the closed status of the #689 PR stack
(`#697`, `#707`, `#712`, `#715`, `#721`) showed the project had already
been rejecting domain-specific intrusions into `ooo auto` on a per-PR
basis. This RFC promotes that de facto rejection to a de jure boundary.

The **future enforcement** of the boundary is mechanical, not
documentary: #735 will add a CI lint guard that fails any PR
re-introducing domain-specific keywords into the `ooo auto` code path.
Once that guard ships (status tracked in the Implementation Status
matrix), it — not the historical snapshot above — becomes the evergreen
control, and the "must be revisited if weakened" clause takes effect from
that point. Until #735 lands, the boundary is held by review discipline
plus the evidence captured here.

## Deferred Decisions

These are intentionally postponed until a real plugin demonstrates the
need. Adding any of them speculatively violates the
"contract emerges from what the plugin actually exercises" principle.

- **Granular `plugin.permission_used` emission.** v0 emits one event per
  declared `required: true` permission at invocation start. Per-call
  emission via stderr-line or sidecar (Options (b)/(c) in #729) is open
  but unimplemented.
- **Per-repo trust grants.** v0 stores trust per-user. A future opt-in
  per-repo policy file is possible but not designed.
- **MCP-tool publication via plugins.** Partly resolved by the firewall
  (#729); remaining MCP-specific concerns to file separately if surfaced.
- **Plugin-update flow (`ooo plugin update`).** v0 ships the eight plugin
  commands locked in #731, split as follows:
  - **State-mutating** (write the trust store / lockfile / installed set):
    `add`, `install`, `trust`, `disable`, `remove`.
  - **Read-only** (no persistent state change): `discover`, `inspect`,
    `list`.
  The single deferred verb is the `update` *transition* — a separate
  in-place upgrade command. It lands when a real upgrade need surfaces;
  until then, `remove` + `add` is the documented upgrade path.
- **Automated migration scripts** for MAJOR-version manifest schema bumps.
  v0 → v1 (whenever it happens) ships with a manual migration guide.
- **Hosted catalog / index server.** Permanent non-goal: marketplace as a
  product surface is a non-goal of #725.

## Related Work

Sub-issues of #725, organized by phase:

| Phase | Issue | Title |
|---|---|---|
| 0 | #726 | Pin self-restraint in #725 body and draft this RFC |
| 0 | #727 | Resolve `PLUGIN LAYER` terminology collision in `docs/architecture.md` |
| 1 | #728 | Plugin manifest loader (`src/ouroboros/plugin/manifest.py`) |
| 1 | #729 | Plugin invocation firewall + audit-event emitter |
| 1 | #730 | Extend `src/ouroboros/plugin/skills/registry.py` for UserLevel programs |
| 2 | #731 | `ooo plugin {add,discover,inspect,install,trust,disable,remove,list}` CLI |
| 2 | #732 | Trust store + `~/.ouroboros/plugins.lock` |
| 3 | #733 | E2E contract proof with `github-pr-ops` (read-only path only) |
| 4 | #734 | Excise / verify-absent GitHub-PR domain branching from `ooo auto` |
| 4 | #735 | CI lint guard preventing domain keywords from leaking into `ooo auto` |
| Cross-repo | #736 | Schema vendoring strategy (vendor / submodule / PyPI) |
| Cross-repo | #737 | `audit-event.schema.json` compatibility with core ledger writer |

Contract repo issues at
[Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins):

| # | Title |
|---|---|
| #1 | `validate_contract.py` enforce JSON Schema (validator correctness) |
| #2 | CI workflow for the validator |
| #3 | LICENSE / CONTRIBUTING / CODEOWNERS (repo-metadata signals) |
| #4 | `ooo plugin add <repo-url>` UX docs in lifecycle.md |
| #5 | Rename `registry/` → `catalog/` |
| #6 | Manifest minimum schema (locked: 8 required + 2 optional) |
| #7 | `merge` removed from v0 reference plugin |
| #8 | first-party programs share the manifest format (locked: yes) |
| #9 | Destructive permission trust UX (locked: 6 answers) |
| #10 | `command.risk` and `permission.risk` enum alignment (locked: 3-value) |
| #11 | Schema versioning policy (locked: SemVer + dual-major + archived) |
