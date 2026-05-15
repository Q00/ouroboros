# Runtime Skill Capability Guides

Issue #1008 moves runtime-specific skill execution guidance out of individual
`SKILL.md` files and into the backend capability registry. Runtime adapters then
consume the rendered guide through the instruction surface they actually own.

## Current coverage

| Runtime | Generated artifact surface | Status |
| --- | --- | --- |
| Codex | Managed rule under `~/.codex/rules/ouroboros.md` | Installed during Codex setup/update via the generated guide renderer. |
| Hermes | `~/.hermes/skills/autonomous-ai-agents/ouroboros/SKILL_CAPABILITY_GUIDE.md` | Installed with the Hermes skill bundle. |
| Claude | `.claude-plugin/SKILL_CAPABILITY_GUIDE.md` | Shipped with the Claude plugin package and checked against the renderer. |
| OpenCode | Not yet a stable prompt/rule artifact in setup | Fallback: use the generic guide from `backends.capabilities` and keep bridge-plugin behavior unchanged until OpenCode exposes a durable instruction surface. |
| Gemini | Not yet a setup-owned prompt/rule artifact | Fallback: use the generic guide from `backends.capabilities`; setup only switches Ouroboros runtime/backend config today. |
| Kiro | Not yet a setup-owned prompt/rule artifact | Fallback: use the generic guide from `backends.capabilities`; setup currently registers MCP and backend config only. |
| Copilot | Not yet a setup-owned prompt/rule artifact | Fallback: use the generic guide from `backends.capabilities`; setup currently registers MCP and model/runtime config only. |

## Seed generation client-gate enforcement

`ouroboros_generate_seed` always reports `required_client_gates`,
`accepted_client_gates`, and `missing_client_gates` metadata so runtimes can
confirm that the Seed-ready Acceptance Guard and Restate gate ran before seed
generation. By default, missing gates remain warnings for compatibility with
existing clients. Set `OUROBOROS_REQUIRE_CLIENT_GATES=1` to promote missing
gates to a hard MCP precondition while migrating maintained clients to pass the
`client_gates` acknowledgements.

## Fallback behavior for runtimes without generated artifacts

Until a runtime has a durable instruction surface owned by `ouroboros setup`,
clients should render `render_backend_skill_capability_guide(<backend>)` when
building prompts or user-facing setup guidance, but must not copy long adapter
sections into individual `SKILL.md` files.

The fallback is intentionally conservative:

1. Keep `SKILL.md` files runtime-neutral.
2. Keep backend-specific execution wording in `src/ouroboros/backends/capabilities.py`.
3. Add an installer only when the runtime has a stable, documented artifact
   surface that setup can refresh idempotently.
4. If no such surface exists, document the gap here and rely on the generic
   backend guide until the runtime integration grows one.

## Adding a new runtime artifact

When a runtime gains a stable rule/skill/plugin instruction surface:

1. Add or refine its `SkillExecutionCapability` entries in
   `backends.capabilities`.
2. Consume `render_backend_skill_capability_guide("<backend>")` from the
   runtime installer or package artifact.
3. Add an artifact test that proves setup/package output contains the rendered
   guide and does not duplicate generated sections on refresh.
4. Update the coverage table above.
