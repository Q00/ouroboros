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
mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
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
and a valid UUIDv4, and all normalized XML fields required below. A **managed
HTTP entry** is the exact marked TOML form above. A **foreign** task, listener,
or entry is anything not so proven. Cleanup proof parses stored normalized XML,
not the launcher available now, and requires folder/name, description, Source,
principal SID/logon type/run level, the one absolute direct `Exec` in a frozen
launcher shape above, trigger types/repetition, multiple-instance policy, both
battery flags, execution limit, and on-demand setting. Unknown or edited
shapes are foreign and preserved.

Immediately before every forward mutation, rollback mutation, `End`, `Delete`,
or restore, the implementation MUST re-query the task and compare its
normalized definition and Source to the expected value. The Source alone is
insufficient; structural proof is always required. There are no aliases.

“Preserve” for a user-managed entry means byte-for-byte preservation: no
rewrite, normalization, reordering, or lifecycle mutation. For an existing
managed HTTP entry, preserve means validate that the exact entry is usable,
then make no task or configuration lifecycle mutation.

The task ownership marker alone is insufficient; exact normalized definition
proof is required. Likewise, URL similarity never authorizes TOML edits.

## Mode and state contract

| Native Windows mode/state | Required behavior |
| --- | --- |
| `auto` + absent | Do not create task/configuration; report MCP disabled and point to `--mcp-mode http-task`. |
| `auto` + user-managed entry | Preserve its bytes; report it preserved. |
| `auto` + managed HTTP entry | Preserve task and TOML; report already opted in, not disabled. |
| `auto` + any other state | Do not install, migrate, or mutate persistence/configuration; report the detected state. |
| `preserve` | Validate an existing usable command/URL entry and perform no task/configuration lifecycle mutation; absence or an unusable entry retains existing error behavior. |
| explicit `http-task` | The only consent path; create or reconcile only the exact managed task and HTTP entry. |
| explicit `stdio` | Refuse before mutation; warn that native Codex Desktop stdio is the crash-triggering topology from #2024, and direct users to explicit `http-task` or WSL. |

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
supervisor, daemon, process monitor, mutex/lease framework, PID registry,
ordered task generations, parallel replacement tasks, task-prefix cleanup,
zero-downtime handoff, or launcher-shape compatibility framework. Cooperative
setup/uninstall commands are serialized only by normal CLI usage: simultaneous
setup/uninstall is unsupported, and no mutex is added.

## Endpoint and security boundary

V1 retains the fixed endpoint to avoid a port broker, state, discovery, and
coordination complexity. It supports only one active Windows user per machine.
Port-vacancy errors MUST say another Windows user or process may own the
endpoint. Per-SID port derivation is follow-up work, not v1.

This has a local cooperative-trust limitation: another local process/account
can bind or impersonate loopback. Fresh setup requires port vacancy and MUST
NOT adopt a listener based only on self-reported identity. No bearer-token
secret lifecycle is introduced. Strong local-user isolation/authentication is
follow-up work; administrators and same-user malware are non-goals.

## Setup and rollback transaction

Setup proceeds in this order:

1. Resolve current canonical SID and the fixed task identity.
2. Preflight exact task ownership and endpoint vacancy; a same-name foreign
   task or foreign listener fails closed.
3. Record a receipt containing prior absence, or the prior exact managed XML,
   prior Source, and whether that prior task was running. Generate the new
   Source UUID as `forward_write_id`.
4. Immediately before the forward definition write, re-query and compare the
   expected prior normalized definition and Source (or absence), then create
   without `/F` unless exact managed ownership was just proven.
5. Read back the just-written task and prove its normalized definition and
   Source equal the intended definition and `forward_write_id` before start,
   readiness, or TOML.
6. Immediately before start or reconciliation, re-query and re-prove the
   written definition and `forward_write_id`.
7. Start or reconcile the exact managed task.
8. Require readiness: task Running **and** successful MCP identity probe at the
   exact endpoint.
9. Immediately before the TOML write, re-query and re-prove the written
   definition and `forward_write_id`; only then write the exact marked HTTP
   entry.

On post-mutation failure, rollback reports primary and rollback errors
together; no PID handling is allowed. A read-back mismatch is a setup failure
and MUST NOT start the task or write TOML.

For prior absence, fresh rollback re-queries the task and may `End`/`Delete`
only when its Source equals `forward_write_id` and its definition is the
written definition. It then uses `schtasks End`, proves it is not Running and
the port is no longer listening, re-queries the unchanged written definition
and `forward_write_id` immediately before delete, then deletes it and restores
absence. If the task is missing or its Source differs, it MUST preserve it and
report an ownership-conflict rollback failure.

For prior managed XML, replacement rollback re-queries before every mutation
and may End or restore the prior XML only while the current Source equals
`forward_write_id` and the written definition matches. The restored XML
retains its prior Source; before a receipt-authorized restart, rollback
re-queries and proves that restored XML and prior Source. A missing or
different Source is an ownership-conflict rollback failure and is preserved;
definition equality alone never authorizes rollback. When authorized, rollback
proves cessation, restarts only when the receipt says it was running, and
verifies prior readiness. It MUST NOT write a new TOML entry after failed
readiness.

A foreign task is never overwritten, adopted, ended, or deleted. Replacement is
allowed only after exact managed proof. `/F` is prohibited for new or unproven
ownership.

## Uninstall and migration

Uninstall/migration first revalidates exact managed ownership and normalized
XML, including any valid managed Source UUID and the stored structural action
shape, independently of whether the current launcher can be resolved. It
re-queries immediately before `End` and `Delete`, requiring unchanged
normalized XML and Source. It then uses `schtasks End`, proves task Ready/not
Running and endpoint unavailable, deletes the task, and only then performs
exact managed TOML cleanup. `schtasks Delete` alone does not stop a running
action.

If End or cessation proof fails, preserve task and configuration and report the
failure. Native evidence proves End changes a test task from Running to
Ready/`267014` and terminates its action; real MCP End/port-release behavior
remains required validation before implementation merge. If it fails, stop for
RFC review rather than adding a PID framework.

Exact managed stdio cleanup/migration is allowed only for exact ownership.
Foreign TOML entries and tasks are preserved. No broad scan, implicit
stdio-to-HTTP migration, or task-prefix cleanup is allowed.

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

For every setup, readiness, rollback, uninstall, or migration failure, report
in this normative order: (1) primary error; (2) bounded `schtasks` return code,
stdout, and stderr; (3) best-effort task state and `LastTaskResult`; then (4)
`~/.ouroboros/logs/ouroboros.log`. Diagnostic collection failure MUST NOT mask
the primary error. Scheduler Operational history may be disabled and cannot be
required.

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

Logoff/logon, reboot, and real MCP End/port-release boundaries were not
exercised. They are required native validation before implementation merge.
The evidence does not establish failure-aware restart or hung-process recovery.

## Normative acceptance criteria

A later implementation PR is acceptable only if it proves all of these:

1. Every mode/state-table case: absent, exact managed, user-managed, and
   same-name foreign/edited-managed behavior.
2. Native `auto` creates no persistence/configuration; managed HTTP reports
   already opted in; user entries remain byte-for-byte unchanged.
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
7. Fresh setup fails closed for foreign task/listener and identifies another
   Windows user or process as a potential endpoint owner.
8. TOML writes occur only after task-Running plus MCP identity readiness;
   protocol probing accepts a valid future real Gregorian date version.
9. Cleanup/uninstall recognizes a structurally managed stored task when its
   launcher path has moved or disappeared, but preserves edited or lookalike
   actions even when a launcher with a similar name resolves now.
10. A forward write read-back Source/definition mismatch fails before start,
    readiness, or TOML; exact UUID-owned fresh rollback restores absence only
    after End, non-Running, and port-closed proof before delete.
11. An identical ABA recreation with a different Source is preserved and
    reported as an ownership-conflict rollback failure; replacement rollback
    restores exact prior XML/state and its prior Source only when the current
    Source is the receipt's `forward_write_id`.
12. Uninstall/migration stops before delete, proves Ready/not Running and port
    release, then cleans only exact managed TOML; failure preserves both.
13. Diagnostics retain primary-error-first ordering even when diagnostics fail.
14. Native logoff/logon, reboot, and real-MCP End/port-release validation pass.

## Size guard, rollout, risks, and non-goals

Implementation plus uninstall helpers SHOULD remain roughly at or below 300
product lines. Exceeding it MUST stop for RFC review; tests/fixtures do not
justify hiding a lifecycle framework.

Rollout is explicit opt-in and experimental: no automatic migration or silent
`auto` enablement. Rollback is only the receipt procedure above or exact
managed uninstall; it never alters foreign tasks, listeners, or entries.

Risks are fixed-port contention, one-minute launch delay, cooperative-trust
loopback impersonation, unsupported concurrent commands, and unverified session
boundaries. This RFC does not provide multi-user support, remote access, strong
local authentication, administrator/malware protection, hung recovery,
zero-downtime replacement, generalized task management, or non-Windows change.

Alternatives rejected: native Codex-owned stdio child; `RestartOnFailure`;
service/supervisor; dynamic/per-SID port in v1; bearer-token lifecycle; and an
SDK-managed persistent client. Each either contradicts native evidence,
expands lifecycle scope, or exceeds Q00’s probe direction.
