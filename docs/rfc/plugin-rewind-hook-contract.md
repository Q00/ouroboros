# Plugin `on_rewind` Hook Contract

## 1. Status

**Implemented for schema v0.6 under #1464.** This RFC is the canonical
contract for observing a successful lineage rewind through the plugin
firewall. It does not create a generic event bus, a plugin veto path, a second
rewind implementation, or checkout authority.

Schema v0.5 remains reserved for #1462's artifact-write contract. Because that
slice was not present on the implementation baseline, #1464 introduces v0.6
without modifying schemas v0.1 through v0.4 or claiming that v0.5 is released.

## 2. Goals

1. Keep `EvolutionaryLoop.rewind_to()` as the only primitive that truncates a
   projected lineage and appends `lineage.rewound`.
2. Route MCP and TUI rewind callers through that same commit boundary.
3. Let an eligible installed plugin observe the committed rewind through a
   bounded, read-only, fail-open hook.
4. Preserve the committed result across catalog, trust, digest, subprocess,
   timeout, and audit persistence failures.

## 3. Canonical Commit Boundary

`EvolutionaryLoop.rewind_to(lineage, generation_number)` owns validation,
lineage truncation, event creation, and the single `EventStore.append()` call.
It captures an immutable `CommittedRewindResult` containing:

- `lineage`;
- `lineage_id`;
- `from_generation`;
- `to_generation`;
- `rewind_event_id`;
- `rewind_occurred_at`.

The optional observer runs only after the append succeeds. It receives a
separate immutable `RewindObservationSnapshot` containing only the five scalar
identity/generation fields. It never receives the lineage object, EventStore,
loop, checkout handle, callback, or writable service.

Invalid targets, no-op targets, and append failures return before observer
dispatch. Observer exceptions are logged and the captured committed result is
returned unchanged.

## 4. Checkout Boundary

Git checkout is caller-owned post-commit work.

- TUI may check dirty state, resolve the generation tag, and run checkout only
  after the canonical rewind succeeds.
- `scripts/ralph-rewind.py` remains an MCP client and optional local checkout
  owner.
- The generation tag remains `ooo/{lineage_id}/gen_{to_generation}`.
- Checkout failure may be reported as a partial client/UI outcome, but cannot
  roll back or reclassify the persisted rewind.

Checkout request/status, repository paths, dirty state, and workspace content
are absent from the hook payload.

## 5. Manifest Contract

Only schema v0.6 accepts `on_rewind`. Every archived schema continues to
reject it.

An `on_rewind` declaration must satisfy all of the following:

- `failure_policy = "fail_open"`;
- `hooks[].permissions` contains `plugin:rewind:observe`;
- top-level `permissions[]` contains the same scope with `required = true`;
- the hook entrypoint is a command with the existing bounded timeout field.

The runtime repeats these checks as defense in depth for programmatically
constructed manifests.

## 6. Payload Contract

The hook receives deterministic compact JSON through
`OUROBOROS_PLUGIN_REWIND_PAYLOAD`.

| Field | Type | Contract |
|---|---|---|
| `rewind_contract_version` | string | Exactly `rewind.v1`. |
| `rewind_event_id` | string | ID of the committed `lineage.rewound` event. |
| `rewind_occurred_at` | string | RFC3339 UTC timestamp of that event. |
| `lineage_id` | string | Rewound lineage aggregate ID. |
| `from_generation` | integer | Generation before rewind. |
| `to_generation` | integer | Retained generation. |
| `correlation_id` | string | Exactly equal to `rewind_event_id` in v1. |

Serialization uses sorted keys and compact separators. The final environment
variable value is encoded as UTF-8 and must be at most 2,048 bytes. A
serialization or size failure launches no hook and is isolated after commit.

The payload excludes seed JSON, generation records, discarded-generation
content, raw events, EventStore rows, checkout/workspace/repository data,
credentials, and hook stdout/stderr.

## 7. Audit Contract

Schema v0.6 audit events contain exactly one subject:

- `command`, for existing command lifecycle audit; or
- `observation`, for rewind observation.

Rewind events use:

```json
{
  "kind": "rewind",
  "id": "<rewind_event_id>",
  "aggregate_type": "lineage",
  "aggregate_id": "<lineage_id>"
}
```

No synthetic command name is emitted. The dispatcher reuses
`plugin.hook.invoked`, `plugin.hook.completed`, `plugin.hook.blocked`, and
`plugin.hook.failed`.

Rewind provenance is limited to string values for:

- `correlation_id`;
- `hook_name`;
- `failure_policy`;
- optional `reason`, `returncode`, `timeout_seconds`, `stdout_sha256`,
  `stderr_sha256`, and `skipped_count`.

Output digests are lowercase 64-character SHA-256 hex. Raw output is never
persisted. Audit events flow through the shared typed `PluginLedgerAdapter`.
Audit buffering or flush failure is fail-open and falls back to core logging
without recursively calling the failed sink.

## 8. Installed-Plugin Catalog

Each committed rewind reads exactly one `Lockfile.read()` snapshot.

- A missing lockfile is the normal no-observer case.
- A corrupt or unreadable lockfile aborts only the observer phase.
- Entries are visited by plugin name ascending.
- Each manifest is loaded from its installed plugin home.
- A corrupt manifest skips that entry and later entries continue.
- Only schema v0.6 `on_rewind` declarations are selected.
- Multiple hooks in one manifest preserve declaration order.

The total order is plugin name then manifest hook index. Candidate values are
immutable and contain only the validated manifest, plugin home, source subject,
artifact digest, and declaration index needed by the firewall.

There is no persistent subscriber list, lockfile polling, process-global
registry, replay queue, or generic subscription API.

## 9. Firewall And Trust

The rewind dispatcher requires complete lockfile `source_type`,
`source_identity`, and `artifact_digest` metadata. It does not use the legacy
version-only fallback.

Before launch, the firewall verifies:

1. schema/hook/failure-policy/permission contract;
2. eligible non-first-party lockfile source type;
3. source type equality with the manifest;
4. readable plugin home and current canonical tree digest;
5. non-disabled install subject;
6. exact trust record equality for version, source type, source identity, and
   artifact digest;
7. an exact trusted `plugin:rewind:observe` scope.

Any failure records a bounded blocked/failed outcome when possible and launches
no hook. Hook return code and output never become a rewind decision.

## 10. Dispatch Budget

One rewind observation phase has a fixed 5.0-second monotonic deadline. The
deadline starts before payload/catalog work and also bounds digest/trust
preflight plus audit flush. Each selected hook timeout is:

```text
min(manifest timeout, remaining global budget)
```

Quick failures continue to later candidates while time remains. At or after the
deadline, no further candidate starts. Remaining candidates are skipped in
deterministic order and the first skipped candidate records the total
`skipped_count` with reason `dispatch_budget_exhausted`.

Blocking filesystem work is isolated from the caller-facing async path. A
timed-out worker may finish its current read in the background, but deadline
checks after catalog/digest/trust work prevent a late hook launch, and its late
audit writes are discarded.

This is a bounded best-effort dispatch contract, not durable delivery or an
at-most-once guarantee.

## 11. Non-Goals And Claim Boundary

- No generic `on_event` bus or subscriber registry.
- No plugin-triggered rewind, retry, veto, cleanup, checkpoint, or checkout.
- No raw EventStore or mutable lineage access.
- No first-party/non-lockfile rewind observers.
- No OS sandbox or malicious local-process containment claim.
- No expected-head/CAS, idempotency key, cross-process duplicate prevention,
  or at-most-once guarantee.

`EventStore.append()` remains atomic for one insert, but concurrent callers may
still commit semantically duplicate rewind events. Solving that requires a
separate EventStore concurrency contract.

## 12. Traceability

| #1464 acceptance text | Contract section | Primary proof |
|---|---|---|
| Bounded plugin-visible payload | §6 | `test_rewind_dispatch.py`, golden payload fixture |
| Hook cannot trigger rewind | §3, §11 | scalar-only observer API tests |
| Permission and failure policy defined | §5 | v0.6 manifest schema matrix |
| Hook failure cannot corrupt/mask rewind | §3, §7, §10 | loop failure injection and E2E tests |
| Rewind works without plugin | §8 | no-plugin E2E baseline |

## 13. Related Work

- #1462: artifact/state hook dispatch; reserves schema v0.5.
- #1463: typed read-only generic event observation; remains separate.
- #1464: this harness-level rewind observation contract.
- [`userlevel-plugins.md`](./userlevel-plugins.md): umbrella plugin contract.
