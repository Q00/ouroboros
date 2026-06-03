# Runtime Skill Capability Guides

Issue #1008 moves runtime-specific skill execution guidance out of individual
`SKILL.md` files and into the backend capability registry. Runtime adapters then
consume the rendered guide through the instruction surface they actually own.

## Capability graph contract

The capability graph is the source of truth that maps abstract skill needs to
runtime-specific tool names, prompt instructions, and unsupported behavior.

```
SKILL.md required capabilities
    -> src/ouroboros/backends/capabilities.py
    -> render_backend_skill_capability_guide(<backend>)
    -> runtime-owned instruction artifact
    -> runtime session behavior
```

`SKILL.md` files should describe what a skill needs in runtime-neutral terms
such as `run_lateral_review`. They should not name a specific runtime's prompt
file, CLI flag, or tool spelling unless the skill is explicitly runtime-scoped.
Backend-specific wording belongs in `SkillExecutionCapability` entries in
`src/ouroboros/backends/capabilities.py`.

When the graph changes, contributors must update every generated or
setup-owned surface that exposes the graph. A capability added only to a
`SKILL.md` is not shipped until a maintained runtime can discover it through
its rendered guide or a documented fallback.

## Current coverage

| Runtime | Generated artifact surface | Status |
| --- | --- | --- |
| Codex | Managed rule under `~/.codex/rules/ouroboros.md` | Installed during Codex setup/update via the generated guide renderer. |
| Hermes | `~/.hermes/skills/autonomous-ai-agents/ouroboros/SKILL_CAPABILITY_GUIDE.md` | Installed with the Hermes skill bundle. |
| Claude | `.claude-plugin/SKILL_CAPABILITY_GUIDE.md` | Shipped with the Claude plugin package and checked against the renderer. |
| OpenCode | Global `AGENTS.md` in the active OpenCode config directory | Installed by OpenCode setup for plugin and subprocess modes. |
| Gemini | `~/.gemini/GEMINI.md` | Installed by Gemini setup as a managed section in the global Gemini memory file. |
| Kiro | `~/.kiro/steering/ouroboros-skill-capability-guide.md` | Installed by Kiro setup as a global steering file. |
| Copilot | `~/.copilot/ouroboros-instructions/AGENTS.md` | Installed by Copilot setup; Ouroboros Copilot runtime also injects that directory through `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`. |

## Seed generation client-gate enforcement

`ouroboros_generate_seed` always reports `required_client_gates`,
`accepted_client_gates`, and `missing_client_gates` metadata so runtimes can
confirm that the Seed-ready Acceptance Guard and Restate gate ran before seed
generation. By default, missing gates remain warnings for compatibility with
existing clients. Set `OUROBOROS_REQUIRE_CLIENT_GATES=1` to promote missing
gates to a hard MCP precondition while migrating maintained clients to pass the
`client_gates` acknowledgements.

## Fallback behavior for new runtimes without generated artifacts

When adding a new runtime that does not yet have a durable instruction surface
owned by `ouroboros setup`, clients should render
`render_backend_skill_capability_guide(<backend>)` when building prompts or
user-facing setup guidance, but must not copy long adapter sections into
individual `SKILL.md` files.

The fallback for new runtimes should stay conservative:

1. Keep `SKILL.md` files runtime-neutral.
2. Keep backend-specific execution wording in `src/ouroboros/backends/capabilities.py`.
3. Add an installer only when the runtime has a stable, documented artifact
   surface that setup can refresh idempotently.
4. If no such surface exists, document the gap here and rely on the generic
   backend guide until the runtime integration grows one.

## Contributor checklist for capability changes

Use this checklist when a PR adds, removes, renames, or materially changes an
abstract capability used by any skill.

1. Update the relevant `SKILL.md` `required_capabilities` or instructions using
   runtime-neutral capability names.
2. Update `src/ouroboros/backends/capabilities.py` with the matching
   `SkillExecutionCapability` entries for every maintained backend.
3. Update any setup-owned or packaged instruction artifacts that are checked in,
   such as `.claude-plugin/SKILL_CAPABILITY_GUIDE.md`, when renderer output
   changes.
4. Update this document when a runtime's generated artifact surface changes or a
   backend has a known gap.
5. Add or update tests that prove the rendered guide and installed artifacts
   contain the capability.

Recommended targeted checks:

```bash
uv run pytest -q \
  tests/unit/backends/test_capabilities.py \
  tests/unit/test_runtime_skill_capability_docs.py \
  tests/unit/test_runtime_instruction_artifacts.py \
  tests/unit/test_claude_plugin_skill_guide.py \
  tests/unit/test_codex_artifacts.py::TestInstallCodexRules::test_packaged_rules_include_rendered_skill_capability_guide \
  tests/unit/hermes/test_artifacts.py
```

## Adding a new runtime artifact

When a runtime gains a stable rule/skill/plugin instruction surface:

1. Add or refine its `SkillExecutionCapability` entries in
   `backends.capabilities`.
2. Consume `render_backend_skill_capability_guide("<backend>")` from the
   runtime installer or package artifact.
3. Add an artifact test that proves setup/package output contains the rendered
   guide and does not duplicate generated sections on refresh.
4. Update the coverage table above.
