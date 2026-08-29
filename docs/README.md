# Ouroboros Documentation

> The serpent that devours itself to be reborn anew.

Ouroboros is an Agent OS for specification-first AI coding workflows. It
transforms ambiguous human requirements into clear, executable specifications
through Socratic questioning and ontological analysis, then runs them through a
replayable execution contract on your choice of runtime backend.

## Documentation Index

### Getting Started

- **[Getting Started Guide](./getting-started.md)** - **Single source of truth for onboarding**: installation, configuration, first-run flow, and troubleshooting
- [Platform Support](./platform-support.md) - Python versions, OS compatibility, and supported runtime backends

### Runtime Guides

- [Claude Code](./runtime-guides/claude-code.md) - Backend-specific configuration and CLI options (see [Getting Started](./getting-started.md) for install/onboarding)
- [Codex CLI](./runtime-guides/codex.md) - Backend-specific configuration and CLI options (see [Getting Started](./getting-started.md) for install/onboarding)
- [OpenCode](./runtime-guides/opencode.md) - Interactive plugin mode and headless subprocess runtime
- [Hermes](./runtime-guides/hermes.md) - Hermes Agent runtime setup and `ooo` dispatch
- [Zcode](./runtime-guides/zcode.md) - Z.ai desktop-agent runtime and measured CLI contract
- [Runtime Capability Matrix](./runtime-capability-matrix.md) - Feature comparison across runtime backends
- [Claude Code (한국어)](./runtime-guides/claude-code.ko.md) - 같은 문서의 한국어판
- [Codex CLI (한국어)](./runtime-guides/codex.ko.md) - 같은 문서의 한국어판
- [GitHub Copilot CLI (한국어)](./runtime-guides/copilot.ko.md) - 같은 문서의 한국어판
- [Kiro CLI (한국어)](./runtime-guides/kiro.ko.md) - 같은 문서의 한국어판
- [Goose (한국어)](./runtime-guides/goose.ko.md) - 같은 문서의 한국어판

### Architecture

- [Native-Windows Codex Desktop HTTP task persistence](./rfc/windows-codex-desktop-http-task.md) - Proposed explicit opt-in loopback HTTP MCP task for native Codex Desktop
- [Routing B — Route Admission](./rfc/routing-b-route-admission.md) - Deterministic, provider-neutral route contract and Admission Kernel
- [Routing C — Compatibility Projection](./rfc/routing-c-route-compat.md) - Bridge existing model/effort routing into the Admission Kernel
- [Routing D — Bounded Escalation](./rfc/routing-d-bounded-escalation.md) - Route observations and finite next-route decisions
- [Project Map V1](./rfc/project-map-v1.md) - Canonical project identity and cross-run read-projection contract
- [Interview Milestone Lateral Contract](./rfc/interview-milestone-lateral-contract.md) - Proposed contract for bounded lateral review at ambiguity milestone transitions
- [CLI Reference](./cli-reference.md) - Command-line interface flags and options
- [Configuration Reference](./config-reference.md) - All `config.yaml` options and environment variables
- [Agent OS Profile Taxonomy](./agentos/profile-taxonomy.md) - Locked 4-slot vocabulary (`runtime_backend`, `stage_runtime`, `llm_profile`, `provider_profile`) for the historically-overloaded "profile" concepts; tiebreaker for #573
- [AgentOS Sequencing SSOT](https://github.com/Q00/ouroboros/issues/961) - Living authority for AgentOS ownership, approval gates, and bounded-slice sequencing

### API Reference

- [API Reference Index](./api/README.md) - Complete API documentation
  - [Core Module](./api/core.md) - Result type, Seed, and error handling
  - [MCP Module](./api/mcp.md) - Model Context Protocol integration

### Guides

- [Seed Authoring Guide](./guides/seed-authoring.md) - YAML structure, field reference, examples
- [Evolutionary Loop & Ralph](./guides/evolution-loop.md) - Wonder/Reflect cycle, convergence detection, persistent evolution
- [Evaluation Pipeline Guide](./guides/evaluation-pipeline.md) - Three-stage evaluation, failure modes, and configuration
- [Evaluation Pipeline Guide (简体中文)](./guides/evaluation-pipeline.zh-CN.md) - 同一份指南的中文版
- [Execution vs. Evaluation Contract](./guides/execution-vs-evaluation.md) - Task completion, AC verdict, and drift terminology boundaries
- [Hidden-Checklist Convergence](./hidden-checklist-convergence/architecture.md) - Run → evaluation → bounded Ralph chaining with hidden harness grading inputs
- [Hidden-Checklist Convergence (简体中文)](./hidden-checklist-convergence/README.zh-CN.md) - 同一设计的中文说明
- [Shared `ooo` Skill Dispatch Router](./guides/ooo-skill-dispatch-router.md) - Runtime setup boundary for Codex CLI, Hermes, and OpenCode skill dispatch
- [DeepSeek Harness Integration](./guides/deepseek-harness.md) - Both directions: the installable `dsh` plugin, and the `dsh` LLM backend
- [MCP Best Practices](./guides/mcp-best-practices.md) - Upstream MCP server configuration, security, and workflow mapping
- [QA Backends](./guides/qa-backends.md) - External QA backend patterns, including OpenCron-style synthetic checks
- [TUI Usage Guide](./guides/tui-usage.md) - Dashboard, screens, keyboard shortcuts
- [TUI Usage Guide (한국어)](./guides/tui-usage.ko.md) - 같은 문서의 한국어판

### Contributing

- [Contributing Guide](../CONTRIBUTING.md) - How to set up, code, test, and submit PRs
- [The Development Loop](./contributing/developing.md) - Run your own code, not the published package: local MCP, config, state, fastest verification per change type
- [Review Conventions](./contributing/review-conventions.md) - What the review bot demands, with evidence from real reviews
- [CI Gates and Branch Protection](./contributing/ci-gates.md) - Required checks, local reproduction, release order
- [Architecture for Contributors](./contributing/architecture-overview.md) - Current code ownership and authority boundaries
- [Source Tour](./contributing/source-tour.md) - Three core mechanisms mapped to file:line coordinates, for teardown writers and reviewers
- [Agent OS Kernel Terminology](./contributing/agent-os-kernel-terminology.md) - Locked vocabulary for `AgentRuntimeContext`, `ControlPlane`, `ControlContract`, `Directive`, `ControlBus`, and `IOJournal`
- [ControlContract](./contributing/control-contract.md) - Control-plane schema, terminality, replay, and idempotency invariants
- [Testing Guide](./contributing/testing-guide.md) - Hermetic homes, xdist isolation, CI commands, and real-surface verification
- [Verifier Evidence Policy](./contributing/verifier-evidence-policy.md) - Core verifier boundary: avoid runner-specific parsers, classify evidence-form mismatches, and preserve anti-fabrication semantics
- [Key Patterns](./contributing/key-patterns.md) - Result type, immutability, event sourcing, protocols
- [Findings Registry](./contributing/findings-registry.md) - Documentation audit findings registry
- [Issue Quality Policy](./contributing/issue-quality-policy.md) - Quality bar for actionable issues and PRD-lite feature requests

### Historical Planning Snapshots

These documents preserve point-in-time evidence and work orders. They are
immutable, non-normative history; use [#961](https://github.com/Q00/ouroboros/issues/961)
for current AgentOS sequencing.

- [AgentOS Release Readiness (2026-05-29)](./history/agentos/release-readiness.md) - Historical release triage and verification snapshot
- [AgentOS Issue Sequencing Graph (2026-05-29)](./history/agentos/issue-sequencing-graph.md) - Historical issue-state and work-order snapshot
- [Master Roadmap 2026-07](./history/master-roadmap-2026-07.md) - Superseded PR-A through PR-K execution plan with final dispositions
## Quick Links

- [GitHub Repository](https://github.com/Q00/ouroboros)
- [PyPI Package](https://pypi.org/project/ouroboros-ai/)

## License

MIT License
