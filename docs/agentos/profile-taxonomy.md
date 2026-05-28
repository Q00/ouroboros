# Agent OS Profile Taxonomy

## 1. Status

**Terminology lock — 2026-05-28.** Closes
[#573](https://github.com/Q00/ouroboros/issues/573) as docs-only.

This document fixes the public vocabulary for the four distinct concepts
that have all been called "profile" at some point in the codebase. It
does **not** rename any code identifier or config key. The taxonomy is
recorded here so:

- New PRs can refer to the four slots unambiguously.
- Reviewers can reject any future overload of an existing key by
  pointing at this table.
- Future renames (if user pressure justifies them) have a target
  vocabulary to migrate toward.

If you are reading this because two PRs disagreed on what "profile"
means, this document is the tiebreaker.

## 2. The four slots

| Slot | Canonical name | What it answers | Concrete example |
|---|---|---|---|
| 1 | **`runtime_backend`** | Which concrete harness/process executes a stage? | `claude`, `codex`, `copilot`, `hermes`, `gemini`, `opencode` |
| 2 | **`stage_runtime`** | Which `runtime_backend` serves each stage of the pipeline? | `interview: codex`, `execute: opencode`, `evaluate: claude_code`, `reflect: hermes` |
| 3 | **`llm_profile`** (and `llm_role_profiles`) | Provider-neutral task profile applied to LLM calls (model, turn budget, reasoning effort). | `ouroboros-fast`, `ouroboros-standard`, `ouroboros-deep`, `ouroboros-frontier` |
| 4 | **`provider_profile`** | Backend-native profile anchor that the LLM profile maps to. | Codex CLI `--profile ouroboros-standard`; Copilot `--agent <name>`; other runtimes may have none |

The invariant: **one public config key has exactly one of these
meanings.** A PR that wants a key to mean two of them must split it.

## 3. Current code mapping

| Concept | Where it appears in code/config today | Notes |
|---|---|---|
| `runtime_backend` | `src/ouroboros/orchestrator/*_runtime.py` `_runtime_backend = "<name>"`, `AgentRuntimeContext.runtime_backend` | This name is correct and stable; new code should keep it. |
| `stage_runtime` | `orchestrator.runtime_profile.stages` in YAML (see `docs/runtime-guides/codex.md`, `docs/rfc/mesh.md:199`, `docs/guides/agent-process-lifecycle.md:43`) | **The YAML key uses `runtime_profile.stages`, which conflates slot 2 with slot 4.** Treat the existing key as deprecated terminology; do not add new sub-keys under it. |
| `llm_profile` / `llm_role_profiles` | Top-level `llm_profiles` and `llm_role_profiles` in `~/.ouroboros/config.yaml`; see `docs/config-reference.md:237–239` | Names already match the canonical taxonomy. |
| `provider_profile` | Codex backend: `orchestrator.runtime_profile.profile` (worker subprocess), `runtime_profile: <name>` (legacy from #505); Copilot: `runtime_profile` mapping → `--agent`. The `runtime_profile` parameter in `src/ouroboros/orchestrator/copilot_cli_runtime.py:99` is **provider_profile** semantically. | **Code/config currently overloads the `runtime_profile` name.** The semantic split is documented here; the rename is deferred (see § 5). |

The implication is that the YAML and Python identifier `runtime_profile`
is doing two unrelated jobs today: in `runtime_profile.stages` it's slot
2 (`stage_runtime`); as a bare scalar inside a Codex backend it's slot 4
(`provider_profile`).

## 4. Reconciling the original conflicting PRs

The conflict that motivated #573 was between three families of PRs that
each assigned a different meaning to `orchestrator.runtime_profile`:

| PR family | Original semantic | Canonical slot |
|---|---|---|
| #505 (Codex worker `--profile` selector) | backend-native profile | **slot 4 — `provider_profile`** |
| #519 / #538 (`runtime_profile.stages`) | stage routing | **slot 2 — `stage_runtime`** |
| #570 (provider-neutral task profiles) | LLM task profile | **slot 3 — `llm_profile`** |

All three are correct in isolation; they just need to live in
non-overlapping namespaces. The shipped surface today reflects this:

- `llm_profiles` / `llm_role_profiles` carry slot 3.
- `runtime_profile.stages` carries slot 2.
- `runtime_profile` (scalar) carries slot 4.

The remaining hygiene work is renaming the slot-2 and slot-4 keys so
the word `runtime_profile` does not stand for both. That work is
**deferred** (see § 5).

## 5. Rename — deferred

The rename of `runtime_profile.stages → stage_runtime` and
`runtime_profile → provider_profile` (Codex/Copilot scalar) is
explicitly **deferred** under #573 with these triggers for revisit:

- An incident or user-visible YAML migration confusion attributable to
  the overloaded key.
- A new runtime backend adding a third meaning under the same name.
- A `~/.ouroboros/config.yaml` schema version bump that lets us
  introduce the canonical keys with a deprecation shim.

Until then, this terminology table is sufficient to keep future PRs
from re-overloading the names.

## 6. Rules for new PRs

1. **Use the canonical names in PR titles, commit messages, and new
   docs.** Even when the code still uses the legacy name, the PR body
   must identify which slot is being touched.
2. **Do not add new sub-keys under `runtime_profile`.** Add them under
   `stage_runtime` (slot 2) or `provider_profile` (slot 4) once those
   keys exist; until then, attach them to an explicit new YAML block
   and call out the migration in the PR body.
3. **Never let one config key mean two of slots 1–4.** A reviewer
   citing this document is grounds to require the split.
4. **Setup must preserve user-explicit profile choices.** Setup may
   write defaults for any of the four slots, but it must not overwrite
   an explicit user assignment unless a documented migration applies.

## 7. Closure

#573 closes as docs-only. Future profile / runtime / LLM key proposals
that previously routed to #573 should reference this document and
identify the slot they touch. They do not need a new meta issue.

## 8. Related

- [#476](https://github.com/Q00/ouroboros/issues/476) runtime contract — `AgentRuntimeContext.runtime_backend` (defined in `src/ouroboros/orchestrator/agent_runtime_context.py:42`) is slot 1; backend identifiers are stamped in each `src/ouroboros/orchestrator/*_runtime.py` (e.g. `codex_cli_runtime.py:78`, `copilot_cli_runtime.py:77`, `opencode_runtime.py:114`, `hermes_runtime.py:156`, `gemini_cli_runtime.py:71`, `goose_runtime.py:52`).
- [#575](https://github.com/Q00/ouroboros/issues/575) ControlJournal — unrelated to this taxonomy; cross-referenced only so future readers know the two design issues do not overlap.
- [`docs/config-reference.md`](../config-reference.md) — current YAML schema reference for `llm_profiles` / `llm_role_profiles` (slot 3).
- [`docs/runtime-guides/codex.md`](../runtime-guides/codex.md) — Codex backend's slot 4 (`runtime_profile` scalar) usage (see § `Worker subprocess isolation` around line 122).
- [#961 SSOT](https://github.com/Q00/ouroboros/issues/961) — process rules for "no new `needs-design` for AgentOS substrate" that this document satisfies.

> Note: this document deliberately does **not** link to sibling
> `docs/agentos/runtime-contract.md` or `docs/agentos/control-journal.md`
> files. Those files do not exist on `main` yet (they are proposed in
> adjacent open PRs #1273 / #1274), so the GitHub issue numbers above
> are the canonical anchors until those PRs land.
