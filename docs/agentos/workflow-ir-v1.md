# AgentOS Workflow IR v1 Boundary

This document pins the v1 scope for #956 under the #961 AgentOS SSOT. The
Workflow IR is a harness-owned planning and validation contract. It is not a
new user-facing workflow builder, not a replacement for `Seed`, and not a live
dispatch engine yet.

## What v1 owns

- `WorkflowSpec`: a versioned graph envelope with source metadata.
- `WorkflowNode`: a typed unit of planned work owned by the harness, an agent,
  a plugin, a verifier, or a human gate.
- `WorkflowEdge`: a typed transition between nodes, including direct,
  conditional, fan-out, fan-in/barrier, and terminal edges.
- `validate_workflow`: deterministic pre-dispatch validation for graph shape,
  terminal reachability, dangling references, duplicate IDs, and missing schema
  references.
- `workflow_spec_from_seed`: a read-only adapter that maps current string
  acceptance criteria into a validated `WorkflowSpec` without mutating the Seed
  or changing runtime behavior.
- Workflow lifecycle records and conformance reports that can validate durable
  lifecycle history against the graph without dispatching work.

## Boundary with #946 projections

#956 describes what the harness plans to run. #946 describes what actually
happened after events were emitted.

| Concern | Owner |
| --- | --- |
| Nodes, edges, owners, schema refs, capability envelope | #956 Workflow IR |
| Run/Stage/Step/Artifact/Verdict read-model records | #946 projection vocabulary |
| Evidence schema semantics and verifier policy | #830 / #978 evidence spine |
| HITL WAIT/RESUME authority | #960 |
| Plugin permissions and audit contract | #939 |

Workflow IR nodes must not directly embed #946 records. Runtime or fixture code
may emit lifecycle/events whose source IDs later project into #946 records, but
that projection remains an observed read model, not the planning contract.

## Boundary with UserLevel plugins

Workflow IR is core harness substrate. UserLevel plugins may eventually provide
or consume workflow-adjacent metadata, but v1 does not make plugins a workflow
SDK and does not add plugin dispatch behavior.

- Plugin permission and lifecycle checks stay under #939.
- Plugin command execution remains outside this v1 IR surface.
- Plugin nodes can be represented as planned nodes, but v1 does not execute
  them or grant permissions.

## Non-goals for v1 follow-ups

- No Microsoft Agent Framework, Azure, DurableTask, or other workflow SDK
  dependency.
- No live `parallel_executor` dispatch source change.
- No `Seed.acceptance_criteria` migration to `PlannedAC`.
- No default-path flip, #978 P5 legacy fallback removal, or evidence-policy
  relaxation.
- No network, provider credential, cloud, or production side effects in default
  conformance fixtures.

## Review checklist for future #956 PRs

- Does the PR keep Workflow IR as a read-only planning/conformance substrate?
- Does it avoid changing existing `ooo run` behavior unless a later issue/PR
  explicitly scopes live dispatch?
- Are schema refs references to existing evidence/input contracts rather than a
  new evidence vocabulary?
- Are lifecycle/conformance fixtures deterministic and local-first?
- If it touches #946 projection code, is the boundary documented in both issue
  references and tests?
