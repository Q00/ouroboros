# Native-Windows Codex Desktop HTTP task persistence

Status: **Proposed**
Related: #2024, #2056, #2026

## Summary

This documentation-only RFC proposes an experimental native-Windows persistence
path for Codex Desktop MCP. It freezes a deliberately thin design before an
implementation PR. Approval of this RFC gates that later PR; it does not
approve implementation or rollout.

Native Codex Desktop uses a loopback streamable-HTTP MCP server maintained by
one Windows Task Scheduler task, not a Codex-owned stdio child. Codex TOML is
written only after task and MCP readiness. User-managed entries are preserved,
and non-Windows behavior is unchanged.

## Scope and constants

This applies only to native Windows (not WSL), Codex Desktop, and explicit
`--mcp-mode http-task`. The v1 endpoint is exactly:

```text
http://127.0.0.1:8765/mcp
```

That exact URL is served by the direct action, used by the readiness probe, and
written to managed Codex TOML; the streamable-HTTP route MUST be `/mcp`. The
server MUST bind loopback only, never a remote interface.

The task identities are fixed:

| Field | Exact value |
| --- | --- |
| Task folder | `\Ouroboros` |
| Task name | `Codex Desktop MCP (<canonical SID>)` |
| Description | `ouroboros-codex-desktop-mcp-v1` |
| Transaction lock | `Global\Ouroboros.CodexDesktopMcp.v1.<canonical SID>` |
| Managed TOML comment | `# Managed by Ouroboros native-Windows Codex Desktop HTTP task v1` |

`<canonical SID>` is the current resolved Windows SID in its canonical string
form. Each successful forward task-definition write generates a UUIDv4 and sets
`RegistrationInfo.Source` exactly to
`ouroboros-codex-desktop-mcp-v1:<UUIDv4>`. This is a non-secret, non-ordered
write identity only: it is not a generation counter, lifecycle token, service
registry, or compatibility framework.

The exact Source grammar is
`^ouroboros-codex-desktop-mcp-v1:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`.

The one canonical action is a direct absolute resolved Ouroboros launcher
`Exec` with this fixed suffix:

```text
mcp serve --runtime codex --llm-backend codex --transport streamable-http --host 127.0.0.1 --port 8765
```

There is no generated PowerShell runner, script wrapper, alternative launcher
shape, or action alias.

For stored-task ownership proof, executable basenames are case-insensitively
limited to `uvx.exe`, `ouroboros.exe`, and `python.exe`. Their corresponding
managed argument prefixes are, respectively:

```text
--isolated --python >=3.12 --from ouroboros-ai[mcp] ouroboros
(empty)
-m ouroboros
```

Exactly one of those prefixes, followed immediately by the fixed suffix above,
is permitted. This freezes the supported launcher families and prevents
lookalikes; the absolute executable path itself is deliberately not compared
with the launcher resolved now and may have moved or disappeared.

The exact managed TOML form is the comment immediately followed by this
URL-only table:

```toml
# Managed by Ouroboros native-Windows Codex Desktop HTTP task v1
[mcp_servers.ouroboros]
url = "http://127.0.0.1:8765/mcp"
```

## Ownership, proof, and preservation

A **managed task** has the exact identity above, a Source with the exact prefix
and a valid UUIDv4, and the exact canonical projection below. A **managed HTTP
entry** is the exact marked TOML form above. A **foreign** task, listener, or
entry is anything not so proven. Cleanup proof parses the stored task XML, not
the launcher available now. Unknown or edited shapes are foreign and
preserved.

Immediately before every forward mutation, rollback mutation, `End`, `Delete`,
or restore, the implementation MUST re-query the task and compare its
canonical projection and Source to the expected value. The Source alone is
insufficient; structural proof is always required. There are no aliases.

“Preserve” for a user-managed entry means byte-for-byte preservation: no
rewrite, normalization, reordering, or lifecycle mutation. In `auto` or
`preserve` mode, an existing managed HTTP entry is usable only as part of exact
`TM + CM`; after validation, those modes make no task or configuration
lifecycle mutation.

The task ownership marker alone is insufficient; exact canonical-projection
proof is required. Likewise, URL similarity never authorizes TOML edits.

### Canonical task-definition projection

Ownership comparison is a semantic projection of namespace-aware Task Scheduler
XML, not serialized-XML equality. Parsing MUST reject duplicate singleton
elements, duplicate trigger/action kinds, malformed values, and entity
expansion. XML declaration, namespace prefixes, attribute order, sibling order,
indentation, and inter-element whitespace do not affect the projection. The
two unique trigger records project into the fixed tuple order
`(RegistrationTrigger, LogonTrigger)`. Element text is trimmed only for schema
values whose grammar excludes surrounding whitespace; command and argument
text is not otherwise normalized.

The complete v1 projection is:

| Area | Canonical value |
| --- | --- |
| Task path | Exact fixed folder and SID-derived name above |
| Registration | Exact Description and task-path URI; Source separately equals the expected valid managed UUID value; Date, Author, Version, Documentation, and SecurityDescriptor absent |
| Principal cardinality | Exactly one principal |
| Principal binding | Principal `id=OuroborosCurrentUser`; `Actions@Context=OuroborosCurrentUser` |
| Principal identity | `UserId` is the current canonical SID; `GroupId`, `DisplayName`, and `RequiredPrivileges` are absent |
| Principal mode | `LogonType=InteractiveToken`, `RunLevel` absent or `LeastPrivilege`, `ProcessTokenSidType=Default` |
| Actions | Exactly one `Exec`; no `ComHandler`, `SendEmail`, `ShowMessage`, or other action |
| Action identifier | `Exec@id` absent |
| Exec command | Absolute path; basename case-insensitively one of the three frozen launcher basenames |
| Exec arguments | `CommandLineToArgvW` token sequence equals the corresponding frozen prefix plus fixed suffix |
| Exec working directory | Absent or empty |
| Registration trigger | Exactly one; `id` absent; enabled; no delay/start/end boundary; repetition interval `PT1M`, duration absent, `StopAtDurationEnd=false` |
| Logon trigger | Exactly one; `id` absent; enabled; `UserId` is the current canonical SID; no delay/start/end boundary or repetition |
| Other triggers | Absent, including boot, idle, time, calendar, event, session-state-change, and custom triggers |
| Instance policy | `MultipleInstancesPolicy=IgnoreNew` |
| Battery | `DisallowStartIfOnBatteries=false`, `StopIfGoingOnBatteries=false` |
| Lifetime | `ExecutionTimeLimit=PT0S`, `DeleteExpiredTaskAfter` absent, `RestartOnFailure` absent (therefore its `Interval` and `Count` children are absent) |
| Availability | `Enabled=true`, `AllowStartOnDemand=true`, `StartWhenAvailable=false`, `WakeToRun=false`, `Hidden=false` |
| Idle | `RunOnlyIfIdle=false`; exactly one `IdleSettings` with `StopOnIdleEnd=true`, `RestartOnIdle=false`, and `Duration`/`WaitTimeout` absent |
| Network | `RunOnlyIfNetworkAvailable=false`; `NetworkProfileName` and `NetworkSettings` absent (therefore `NetworkSettings.Name`/`Id` absent) |
| Scheduler compatibility | `UseUnifiedSchedulingEngine=true`; `DisallowStartOnRemoteAppSession=false`; `Compatibility` absent |
| Priority | `Priority=7` (the Task Scheduler default priority) |
| Maintenance | `MaintenanceSettings` absent (therefore `Period`, `Deadline`, and `Exclusive` absent) |
| Other settings | `AllowHardTerminate=true`, `Volatile=false` |

The rows above are the complete v1 treatment of the Task Scheduler
`SettingsType` surface, not examples. In schema order, every setting is
accounted for: `AllowStartOnDemand`, `RestartOnFailure`,
`MultipleInstancesPolicy`, `DisallowStartIfOnBatteries`,
`StopIfGoingOnBatteries`, `AllowHardTerminate`, `StartWhenAvailable`,
`NetworkProfileName`, `RunOnlyIfNetworkAvailable`, `WakeToRun`, `Enabled`,
`Hidden`, `DeleteExpiredTaskAfter`, `IdleSettings`, `NetworkSettings`,
`ExecutionTimeLimit`, `Priority`, `RunOnlyIfIdle`, `Compatibility`,
`UseUnifiedSchedulingEngine`, `DisallowStartOnRemoteAppSession`,
`MaintenanceSettings`, and `Volatile`. A field introduced by a newer root
schema version is unconsumed and therefore foreign until a later RFC assigns
it canonical treatment.

For this table only, absent is equivalent to the documented Task Scheduler
default for `ProcessTokenSidType=Default`, `RunLevel=LeastPrivilege`, trigger `Enabled=true`,
`StopAtDurationEnd=false`, settings `Enabled=true`, `AllowStartOnDemand=true`,
`StartWhenAvailable=false`, `WakeToRun=false`, `Hidden=false`,
`AllowHardTerminate=true`, `RunOnlyIfNetworkAvailable=false`, and
`Volatile=false`, `RunOnlyIfIdle=false`,
`DisallowStartOnRemoteAppSession=false`, and `Priority=7`. Explicit and absent
values are not equivalent anywhere else;
in particular, `ExecutionTimeLimit` MUST semantically be `PT0S`.

The root Task Scheduler namespace and an OS-supported root schema-version
attribute are schema bookkeeping and are not projected. Every other XML
element or attribute not consumed by the table, including a second action,
principal, or trigger, makes the task foreign. This rule, the explicit
absent/default and absent/empty equivalences, case-insensitive executable
basename comparison, `CommandLineToArgvW` tokenization, and the
ordering/whitespace rules are the only normalization. Implementations MUST NOT
add their own ignored fields or defaults.

## Mode and state contract

Task and Codex configuration are one compound installation. The deterministic
task name is classified as `T0` (absent), `TM` (exact managed proof), or `TX`
(present but foreign/edited). The `mcp_servers.ouroboros` configuration is
classified as `C0` (absent), `CM` (the exact marked HTTP form), `CU`
(user-managed with no managed marker), or `CX` (managed marker present but
edited, malformed, duplicated, or mismatched). Classification occurs while
holding the transaction lock.

| Compound state | Classification and required behavior |
| --- | --- |
| `T0 + C0` | Absent. `auto` performs no mutation and points to explicit `http-task`; explicit `http-task` may install. |
| `T0 + CU` | User-managed configuration. Preserve its bytes in every automatic mode; explicit `http-task` refuses to overwrite it. |
| `TM + CM` | The only managed installation. `auto` may report already opted in only after proving both artifacts; explicit `http-task` may reconcile it. |
| `TM + C0` | Recoverable exact managed task-only state. `auto` and `preserve` fail closed and preserve it. Explicit `http-task` may complete it to exact `TM + CM` through the normal reconciliation transaction; uninstall may remove the exactly proven task to `T0 + C0` without creating or restoring TOML. No other operation may mutate it. |
| `T0 + CM` | Stale managed TOML only. Fail closed and preserve; do not synthesize or adopt a task. |
| `TX + CM` | Foreign/edited same-name task plus managed TOML. Fail closed and preserve both. |
| `TM + CU` or `TM + CX` | Managed task plus foreign, user-managed, or edited TOML. Fail closed and preserve both. |
| `T0 + CX`, `TX + C0`, `TX + CU`, or `TX + CX` | Foreign or mismatched installation. Fail closed and preserve every artifact. |

An occupied endpoint is a third compound-state input. Occupancy while `TM` is
Running is only a candidate prior managed action, never listener ownership
proof. Explicit reconciliation may stop that exactly proven task and continue
only if the endpoint is then vacant. Except for the receipt-proven inactive
uninstall path below, occupancy in every other case, or occupancy that remains
after a managed reconciliation stop, is a foreign/residual listener conflict
and fails closed. During receipt-proven inactive uninstall, an already-occupied
endpoint is foreign cleanup context only: it is neither adopted nor touched
and does not block removal of the exactly owned inactive task. No mode adopts
a listener from its URL or self-reported MCP identity.

`auto` and `preserve` never repair a compound state. `preserve` validates an
existing usable command/URL entry and performs no task/configuration lifecycle
mutation; absence or unusable input retains existing error behavior. Explicit
`http-task` is the only setup consent path and may mutate only `T0 + C0`,
exact `TM + C0`, or exact `TM + CM`. For `TM + C0`, the explicit operation
selects the terminal state without inferring whether setup, uninstall, or a
manual TOML removal produced it: `http-task` completes installation, while
explicit uninstall removes it. Explicit `stdio` refuses before mutation, warns
that native Codex Desktop stdio is the crash-triggering topology from #2024,
and directs users to explicit `http-task` or WSL.

Existing user-managed entries, including stdio, remain byte-for-byte unchanged.
A fresh native `auto` setup MUST NOT silently write stdio or install
persistence. WSL guidance remains for stdio.

## Scheduled-task contract

`http-task` creates at most one deterministic task for the current SID, at the
fixed folder and name. It MUST be visible and discoverable in Task Scheduler.

| Property | Required value |
| --- | --- |
| Principal | Current SID, medium integrity, least privilege |
| Logon type | `InteractiveToken` |
| Action | Exact canonical direct absolute `Exec` above |
| Ownership | Exact description above |
| Write identity | `RegistrationInfo.Source` format above, newly generated per successful forward definition write |
| Multiple instances | `IgnoreNew` |
| Battery start | `DisallowStartIfOnBatteries=false` |
| Battery transition | `StopIfGoingOnBatteries=false` |
| Execution limit | No execution time limit |
| Manual action | Allow on-demand start |

The task has (1) a `RegistrationTrigger` with indefinite one-minute repetition
for current-session launch attempts and (2) a per-SID `LogonTrigger` for future
logons. This is periodic 0–60 second relaunch eligibility, not failure-aware
restart. `IgnoreNew` suppresses a new action while one runs. It does not recover
hung processes.

The design MUST NOT configure, use, or claim `RestartOnFailure`, a service,
supervisor, daemon, process monitor, server-lifetime mutex/lease framework, PID
registry, ordered task generations, parallel replacement tasks, task-prefix
cleanup, zero-downtime handoff, or launcher-shape compatibility framework. The
single bounded CLI transaction lock below is required only to serialize
ownership proof and lifecycle mutation; it is not held by the MCP server and is
not a liveness or restart mechanism.

## Endpoint and security boundary

V1 retains the fixed endpoint to avoid a port broker, state, discovery, and
coordination complexity. It supports only one active Windows user per machine.
Port-vacancy errors MUST say another Windows user or process may own the
endpoint. Per-SID port derivation is follow-up work, not v1.

This has a local cooperative-trust limitation: another local process/account
can bind or impersonate loopback. Setup requires vacancy immediately before a
forward start. Reconciliation obtains vacancy only through the exact managed
stop procedure below and MUST NOT adopt a listener based only on self-reported
identity. No bearer-token secret lifecycle is introduced. Strong local-user
isolation/authentication is follow-up work; administrators and same-user
malware are non-goals.

## Transaction serialization

Setup, reconciliation, rollback, uninstall, and compound-state
classification use the exact SID-scoped named Windows mutex above, restricted
to the current SID and `SYSTEM`. Acquire it before the first task, endpoint, or
TOML read and hold it through success or complete rollback; nested rollback
reuses it. Creation, ACL verification, or bounded-acquisition failure stops
before mutation. After an abandoned mutex, reclassify all durable state.
Immediate re-proof remains mandatory against non-cooperating external edits.

Every receipt in this RFC is an in-memory, process-local transaction record
discarded when the command exits. V1 MUST NOT add a durable receipt, journal,
registry, generation store, recovery command, service, supervisor, PID record,
or migration artifact. `RegistrationInfo.Source` remains only the task-local
ABA write identity defined above. After an abandoned mutex, a new explicit
operation may classify exact `TM + C0` and snapshot its current exact
projection, Source, running InstanceGuids, and `C0` as that operation's prior
state; automatic modes still preserve it without mutation.

## Setup and rollback transaction

Setup proceeds in this order:

1. Acquire the lock, classify state, and continue only from `T0 + C0`, exact
   `TM + C0`, or exact `TM + CM`. Record prior task
   XML/projection/Source/running InstanceGuids and the existing Codex
   `_PathSnapshot` for TOML, including absence or exact whole-file bytes;
   generate `forward_write_id`.
2. For `TM + C0` or `TM + CM`, re-prove and `End` every running prior instance.
   Prove all ceased, task not Running, and port vacant. `T0 + C0` also requires
   vacancy. A pre-write failure restarts a receipt-authorized prior run only
   through projection/Source, vacancy, new InstanceGuid, and readiness proof;
   an inactive prior task remains inactive.
3. Prove no running instance, re-prove prior ownership or absence, write the
   definition, then read back its exact projection and `forward_write_id`.
4. Start through Task Scheduler COM and capture the unique new
   `IRunningTask.InstanceGuid` as `forward_run_id`; if RegistrationTrigger wins,
   accept only the one post-write GUID absent from the pre-write snapshot.
   Zero, multiple, pre-existing, or unprovable instances fail.
5. Before and after MCP identity readiness, re-prove the projection, Source,
   and exact Running `forward_run_id`. Preserve existing `TM + CM` TOML; for
   `T0 + C0` or `TM + C0`, atomically write the marked entry only after those
   proofs.

On post-mutation failure, rollback reports primary and rollback errors
together; no PID handling is allowed. A read-back mismatch is a setup failure
and MUST NOT start the task or write TOML.

TOML mutation MUST narrowly extend, not duplicate, the existing Codex setup
snapshot transaction. It constructs complete intended bytes without touching
the source and preserves unrelated bytes.
`_atomic_write_text_if_current_matches` MUST compare the current link-aware
snapshot immediately before replacement, atomically replace complete bytes
through a flushed and closed same-directory temporary file, and return an
immediate post-replace `_snapshot_path(path)` read-back rather than a synthetic
regular-file snapshot. That read-back MUST show the intended complete bytes and
the expected regular-file or unchanged-symlink topology; otherwise the write
fails.

Rollback first compares an immediate link-aware snapshot with the recorded
forward read-back. For a prior regular file, it restores complete prior bytes
by same-directory temporary write and atomic replace. For a prior symlink, it
requires the same link and exact recorded forward target snapshot, atomically
replaces the target through a temporary file in the target directory, and MUST
NOT unlink or recreate the symlink. For prior absence, it may unlink only a
regular file whose immediate snapshot exactly equals the recorded forward
regular-file snapshot. Every restoration is followed immediately by an exact
`_snapshot_path(path)` read-back; an observed pre-mutation or read-back mismatch
is an ownership conflict.

These are compare-immediately-before-mutate checks, not a race-free
operating-system compare-and-swap. A non-cooperating mutation after the final
comparison remains the bounded TOCTOU risk stated below. V1 MUST NOT add a
generalized file transaction, file-locking, retry, or journaling subsystem.

If failure occurs after TOML replacement begins, rollback restores TOML before
the task through the existing compare-before-restore contract. Exact prior
snapshot or already-restored state is success; any current-snapshot mismatch
preserves file and serving task as an ownership conflict. Task rollback
requires proven prior TOML state, so it cannot leave a managed entry pointing
to a deleted task.

For prior absence, fresh rollback re-queries the task and may `End`/`Delete`
only when its Source equals `forward_write_id` and its definition is the
written projection. If started, `forward_run_id` must be the only running
instance before `End`; otherwise prove no instance runs. Prove cessation and
port release, re-prove projection/Source, then delete. Missing or changed
ownership is preserved as rollback conflict.

For prior managed XML, replacement rollback re-queries before every mutation
and requires matching written projection/`forward_write_id`, plus the same
started-or-not cessation proof. Restore exact prior XML/Source, re-prove it,
and restart only when the receipt says it ran, through new InstanceGuid and MCP
readiness proof. Any mismatch is preserved; definition equality alone never
authorizes rollback. Failed readiness never authorizes TOML.

When the prior compound state was `TM + C0`, rollback restores the exact prior
task XML, Source, and running-or-inactive state while the existing TOML
snapshot/CAS contract restores or retains exact `C0`; it MUST NOT synthesize a
managed entry. Interruption of this recovery can leave only `TM + C0`,
`TM + CM`, or `T0 + C0`, each of which a later explicit operation reclassifies
under the state table rather than resuming a durable receipt.

A foreign task is never overwritten, adopted, ended, or deleted. Replacement is
allowed only after exact managed proof. `/F` is prohibited for new or unproven
ownership.

## Uninstall

Uninstall holds the same lock, records the same process-local task/TOML
receipt, and proves stored ownership without resolving the current launcher.
It accepts only exact `TM + CM` or exact `TM + C0`. For `TM + C0`, the explicit
invocation is fresh consent to finish removal of the exactly proven task; exact
`C0` is recorded as prior state and remains a no-op throughout. Every other
incomplete, foreign, or mismatched state fails closed.

Immediately before each `End`, TOML cleanup or absence proof, and `Delete`,
uninstall MUST re-query and re-prove projection, Source, task state, and current
InstanceGuids. If the receipt recorded a running instance, or if any new
InstanceGuid is observed before a mutation, uninstall switches to the bounded
`End`/cessation branch: it re-proves ownership, ends every observed instance,
and proves each InstanceGuid ceased plus task non-Running before continuing.
Failure of that proof preserves the task and configuration.

A receipt-observed running task additionally requires port release before
cleanup. A receipt-observed inactive task never requires vacancy during that
uninstall; any listener remains foreign and untouched even if a newly observed
managed InstanceGuid must be ended and proven ceased.

Uninstall then atomically removes and verifies only the exact managed TOML
entry for `TM + CM`; for `TM + C0`, it instead re-proves exact absence without
writing it. It performs the required immediate ownership/state/InstanceGuid
re-query before deleting the task. Delete alone never counts as process
cessation.

After any failure following `End` or TOML cleanup while the task remains
present, rollback restores TOML first when `TM + CM` changed it; `TM + C0`
retains exact absence. It re-proves the stopped task and restarts only when the
receipt recorded a prior running instance, using vacancy, a new InstanceGuid,
and MCP readiness proof. A receipt-observed inactive task remains inactive and
requires no vacancy for rollback. An ambiguous delete is re-queried: proven
absence with exact `C0` is the intended `T0 + C0` terminal state, while proven
presence uses the rollback path above. Ownership conflict preserves observed
artifacts and reports primary-first.

Interruption before proven deletion leaves exact `TM + CM` or `TM + C0`;
interruption after proven deletion leaves `T0 + C0`. A later explicit
operation reclassifies those states and repeats the same bounded path.

If End, InstanceGuid cessation, or a required port-release proof fails,
preserve task and configuration and report the failure. Native evidence proves
End changes a test task from Running to Ready/`267014` and terminates its
action; real MCP End/port-release behavior remains required validation before
implementation merge. If it fails, stop for RFC review rather than adding a
PID framework.

V1 does not migrate or clean up stdio installations. Existing stdio and other
user-managed TOML entries remain byte-for-byte preserved. No broad scan,
implicit stdio-to-HTTP migration, or task-prefix cleanup is allowed.

## Protocol readiness probe

Following Q00’s non-blocking direction, this is a narrow probe, not an
expansive SDK-client lifecycle proposal. It sends JSON-RPC `initialize` using
the implementation-selected request version and retains exact envelope and
response structural validation. It requires:

- `serverInfo.name == "ouroboros-mcp"`;
- a non-empty `serverInfo.version`; and
- a negotiated `protocolVersion` that is a real Gregorian calendar date in
  `YYYY-MM-DD` form, not merely a lexically well-formed string or exact version
  equality.

Codex negotiates independently. The implementation PR MUST test acceptance of
a valid future protocol-version date.

## Diagnostics

For every setup, readiness, rollback, or uninstall failure, report
in this normative order: (1) primary error; (2) bounded Task Scheduler API or
`schtasks` error/return code, stdout, and stderr; (3) best-effort lock, task,
InstanceGuid, endpoint, and TOML rollback state plus `LastTaskResult`; then (4)
`~/.ouroboros/logs/ouroboros.log`. Rollback errors follow the primary error in
the same order. Diagnostic collection failure MUST NOT mask the primary error.
Scheduler Operational history may be disabled and cannot be required.

## Native evidence appendix (2026-08-12)

Environment: Windows 11 Pro `10.0.26200`, medium integrity. The following are
native observations, not guarantees beyond their stated conditions. Private
probe artifacts are local evidence, not shipped files.

| Probe/settings/action | Observation |
| --- | --- |
| Least-privilege XML task create/query/run/end/delete | All operations succeeded without elevation. |
| `RestartOnFailure` `PT1M`, count `3`; action exits `7` | No relaunch observed. |
| Same recovery setting; action terminated `0xC0000005` | No relaunch observed. |
| Registration trigger + indefinite one-minute repetition; `0xC0000005` action | Relaunched at minute ticks and recovered on third start. |
| `IgnoreNew`; 130-second action | One instance; duplicate suppressed. |
| `schtasks End` on running test action | State changed to Ready/`267014`; action terminated. |
| Canonical XML create/query round-trip | Scheduler persisted task URI and `Actions@Context`, omitted explicit `LeastPrivilege`, and materialized the listed idle/unified-engine defaults. |

Logoff/logon, reboot, and real MCP End/port-release boundaries were not
exercised. They are required native validation before implementation merge.
The evidence does not establish failure-aware restart or hung-process recovery.

## Normative acceptance criteria

A later implementation PR is acceptable only if it proves all of these:

1. Every compound-state-table case, including task-only, TOML-only,
   foreign-task/managed-TOML, managed-task/foreign-TOML, edited/mismatched, and
   foreign-listener states, fails closed or proceeds exactly as specified.
   Exact `TM + C0` alone is operation-selectable: `auto`/`preserve` make no
   mutation, explicit `http-task` converges to `TM + CM`, and uninstall
   converges to `T0 + C0`; no other state gains repair authority.
2. Native `auto` creates no persistence/configuration and reports already
   opted in only for exact `TM + CM`; user entries remain byte-for-byte
   unchanged.
3. `preserve` validates a usable command/URL entry without task/configuration
   lifecycle mutation; absence or unusable input retains existing error behavior.
4. Native explicit `stdio` is refused before mutation with the #2024
   crash-topology warning and `http-task`/WSL direction.
5. `http-task` creates the exact folder/name/description/action/endpoint,
   current-SID medium-integrity `InteractiveToken` task with `IgnoreNew`, both
   specified battery flags, no execution limit, on-demand start, and a fresh
   valid managed Source UUID on each successful forward definition write.
6. Trigger behavior is registration repetition plus per-SID logon trigger, with
   no `RestartOnFailure` configuration or claim.
7. The SID-scoped named mutex serializes setup, reconciliation, rollback,
   uninstall, and state classification across independent CLI
   processes, is held through rollback, and fails before mutation on lock
   errors/timeouts.
8. Active reconciliation from exact `TM + CM` or recoverable `TM + C0` proves
   exact prior ownership, ends all prior instances, proves port release, writes
   the new definition, and accepts readiness only while the post-write
   `forward_run_id` is Running; an old action cannot satisfy the gate despite
   `IgnoreNew`.
9. Fresh setup and inactive reconciliation fail closed for a foreign listener
   and identify another Windows user or process as a potential endpoint owner.
10. TOML writes occur only after forward execution identity plus MCP readiness;
    protocol probing accepts a valid future real Gregorian date version.
11. Cleanup/uninstall recognizes a structurally managed stored task when its
    launcher path has moved or disappeared, but preserves edited or lookalike
    actions even when a launcher with a similar name resolves now.
12. Canonical-projection tests cover every listed principal, action,
    working-directory, trigger, repetition, instance, battery, lifetime,
    on-demand, hidden, start-when-available, wake, and every field in the
    complete `SettingsType` inventory above. Table-driven mutations exercise
    each accepted value, each required absence, and each explicitly allowed
    absent/default pair (including `Priority=7`, `RunOnlyIfIdle=false`, and
    `DisallowStartOnRemoteAppSession=false`);
    unknown fields, duplicates, and non-equivalent defaults are foreign while
    only the closed normalization rules are equal. Native create/query
    round-trip tests assert the entire projected settings record before any
    lifecycle operation, and exact `CommandLineToArgvW` token tests pass.
    The accepted action projection includes the explicit
    `--runtime codex --llm-backend codex` selectors for every permitted
    executable prefix; omitting either selector is foreign and a fresh
    scheduled action must start successfully without relying on global runtime
    configuration.
13. A forward write read-back Source/definition mismatch fails before start,
    readiness, or TOML; exact UUID-owned fresh rollback restores absence only
    after End, exact InstanceGuid cessation, and port-closed proof before
    delete.
14. An identical ABA recreation with a different Source is preserved and
    reported as an ownership-conflict rollback failure; replacement rollback
    restores exact prior XML/state and its prior Source only when the current
    Source is the receipt's `forward_write_id`.
15. The existing Codex snapshot transaction captures exact prior whole-TOML
    bytes or absence. A forward snapshot-checked write returns an immediate
    truthful link-aware `_snapshot_path` read-back. Rollback atomically replaces
    complete regular-file or symlink-target bytes without replacing the symlink
    itself; prior absence unlinks only a regular file matching the recorded
    forward snapshot. Tests cover regular files, symlinks, prior absence,
    mismatch before the final comparison, failure before replacement, and
    mismatch in the immediate post-replace read-back. The implementation does
    not claim race-free filesystem compare-and-swap.
16. Rollback restores exact prior TOML before task rollback; for prior
    `TM + C0`, it instead restores or retains exact absence and never
    synthesizes TOML. TOML ownership conflict preserves the serving task, and
    rollback diagnostics never mask the primary error.
17. Uninstall immediately re-proves ownership, state, and InstanceGuids before
    every `End`, TOML mutation or absence proof, and `Delete`. A
    receipt-observed running instance, or any newly observed InstanceGuid,
    enters the bounded `End`/cessation branch before cleanup. Receipt-observed
    running state requires port release. Receipt-observed inactive state never
    requires vacancy during that uninstall; any foreign listener remains
    untouched and cannot block cleanup. Failure after either `End` or
    inactive-path TOML cleanup follows the same receipt-conditional rollback
    rules. The exact managed task alone is deleted, and `TM + C0` never
    synthesizes TOML.
18. Fault injection after every durable setup, `TM + C0` recovery, and
    uninstall boundary, followed by abandoned-mutex reclassification, proves
    repeated explicit `http-task` converges to `TM + CM` and repeated uninstall
    converges to `T0 + C0`. Interruption produces no durable state outside
    `T0 + C0`, `TM + C0`, and `TM + CM`, and requires no durable receipt.
19. Diagnostics retain primary-error-first ordering even when rollback or
    diagnostics fail.
20. Native projection round-trip, InstanceGuid capture, logoff/logon, reboot,
    concurrent-CLI serialization, and real-MCP End/port-release validation pass.

## Size guard, rollout, risks, and non-goals

Implementation plus uninstall helpers SHOULD remain roughly at or below 300
product lines. Exceeding it MUST stop for RFC review; tests/fixtures do not
justify hiding a lifecycle framework.

Rollout is explicit opt-in and experimental: v1 provides no migration or silent
`auto` enablement. Rollback is only the process-local receipt procedure above
or exact managed uninstall; it never alters foreign tasks, listeners, or
entries. Explicit `http-task` may complete exact `TM + C0`, and explicit
uninstall may remove it; both may stop a proven running task and may fail back
to the exact prior partial state without automatic repair.

Risks are fixed-port contention, one-minute launch delay, cooperative-trust
loopback impersonation, non-cooperating external mutation, and unverified
session boundaries. Non-cooperating TOML mutation between the final snapshot
comparison and filesystem replacement or unlink is a residual TOCTOU risk.
Snapshot equality also cannot distinguish an identical ABA recreation; v1
introduces no generalized filesystem transaction or durable file identity.
This RFC does not provide multi-user support, remote access, strong local
authentication, administrator/malware protection, hung recovery, zero-downtime
replacement, generalized task management, or non-Windows change.

Alternatives rejected: native Codex-owned stdio child; `RestartOnFailure`;
service/supervisor; dynamic/per-SID port in v1; bearer-token lifecycle; and an
SDK-managed persistent client. Each either contradicts native evidence,
expands lifecycle scope, or exceeds Q00’s probe direction.
