---
doc_id: contributing/findings-registry
title: Documentation Findings Registry
schema_version: "1.5"
generated: "2026-03-15"
severity_audit: "2026-03-15"
gap_type_schema_updated: "2026-03-15"
gap_type_migration_completed: "2026-03-15"
claim_id_schema_added: "2026-03-15"
fnd_migration_completed: "2026-03-15"
status: legacy-frozen
successor_registry: docs/entity-registry.yaml
successor_spec: docs/entity-registry-spec.yaml
migration_guide: docs/entity-registry-migration-guide.md
description: >-
  LEGACY ARCHIVE (schema v1.5, frozen 2026-03-15): All 50 FIND-NNN entries in
  this file have been migrated to FND-NNN records in docs/entity-registry.yaml
  (record_type: finding). This file is preserved for backward-compatibility and
  historical reference. Do NOT add new findings here; use docs/entity-registry.yaml
  instead. All entries implicitly carry record_type: finding per the multi-entity
  registry backward-compat contract (docs/entity-registry-migration-guide.md Rule 1).
  Original description: Canonical, deduplicated registry of every documentation
  finding produced by all previous-generation static audits. Each entry carries
  a normalized id, a concise claim statement, severity, gap_type (and optional
  sub_qualifier), resolution status, the set of affected documents, and a pointer
  to the fix or recommendation.
schema_changelog:
  "1.5": >-
    2026-03-15 (Sub-AC 3 of AC 1): FREEZE migration. All 50 FIND-NNN entries
    migrated to FND-NNN records in docs/entity-registry.yaml with record_type:
    finding discriminator. New fields in FND-NNN schema: correction (replaces
    resolution_ref prose), implicated_claim_ids (list; promotes single claim_id
    to multi-claim forward-compat), legacy_id (FIND-NNN preserved for
    backward-compat). This file is now a legacy-frozen archive; the authoritative
    finding registry is docs/entity-registry.yaml. All FIND-NNN entries implicitly
    have record_type: finding per entity-registry-migration-guide.md Rule 1.
    Schema bumped 1.4→1.5; no entries modified (backward-compatible).
    Multi-entity spec: docs/entity-registry-spec.yaml v1.0.
  "1.4": >-
    2026-03-15 (Sub-AC 2-1): Added claim_id (format: CLM-NNN, pattern ^CLM-[0-9]{3,}$)
    as a required field on every finding entry, making claims independently referenceable
    entities separate from their FIND-NNN finding identifier. Added code_deps[] (required;
    empty list [] for doc-only or cross-doc findings) to each entry, linking each claim to
    the source code files that establish its truth value. Schema bumped 1.3→1.4;
    all 50 FIND-NNN entries migrated. No existing fields removed or renamed (backward-compatible).
  "1.3": >-
    2026-03-15 (Sub-AC 6c): Added inaccuracy gap_type with sub_qualifier support;
    deprecated contradiction as alias for inaccuracy+sub_qualifier:cross-doc;
    migrated FIND-014, FIND-022, FIND-040 from gap_type:contradiction to
    gap_type:inaccuracy + sub_qualifier:cross-doc; field renamed gap_type_qualifier
    → sub_qualifier for consistency with claim-registry-spec.yaml;
    no dangling references to removed inconsistency enum value remain.
  "1.2": >-
    2026-03-15 (AC-06): gap_type enum refactor announced; gap_type_qualifier
    (now sub_qualifier) introduced in description; severity_audit applied.
  "1.1": "2026-03-15 — schema fields extended"
  "1.0": "2026-03-15 — initial version"
sources:
  - docs/cli-audit-findings.md
  - docs/contributing/config-doc-findings.md
  - docs/cross-document-contradiction-findings.md
  - docs/contributing/skill-cli-mapping-findings.md
  - docs/semantic-link-rot-report.md
  - docs/runtime-capability-crosscheck.md
  - docs/doc-issues-register.md
depends_on:
  - docs/cli-audit-findings.md
  - docs/contributing/config-doc-findings.md
  - docs/cross-document-contradiction-findings.md
  - docs/contributing/skill-cli-mapping-findings.md
  - docs/semantic-link-rot-report.md
  - docs/runtime-capability-crosscheck.md
  - docs/doc-issues-register.md
affects:
  - docs/cli-reference.md
  - docs/guides/cli-usage.md
  - docs/getting-started.md
  - README.md
  - docs/architecture.md
  - docs/config-reference.md
  - docs/runtime-guides/codex.md
  - docs/runtime-guides/claude-code.md
  - docs/guides/common-workflows.md
  - docs/guides/execution-failure-modes.md
  - docs/runtime-capability-matrix.md
  - docs/README.md
  - docs/config-inventory.md
stats:
  total_findings: 50
  open: 5
  resolved: 45
  by_severity:
    critical: 4
    high: 26
    medium: 20
    low: 0
  severity_audit: "2026-03-15 (AC-06): 11 findings upgraded high, 4 findings low→medium, 0 low remain; rubric aligned with CONTRIBUTING.md"
---

# Documentation Findings Registry

> **Purpose:** Single authoritative record of all documentation audit findings.
> All findings from the per-topic audit reports have been merged here, duplicates
> eliminated, and each entry assigned a normalized `FIND-NNN` identifier.
>
> **Schema version:** 1.5 | **Last updated:** 2026-03-15 (Sub-AC 3 of AC 1: multi-entity migration; schema 1.4→1.5)
>
> **⚠️ LEGACY ARCHIVE:** This file is frozen as of 2026-03-15. All 50 FIND-NNN entries have
> been migrated to FND-NNN records in [`docs/entity-registry.yaml`](../entity-registry.yaml)
> with `record_type: finding`. Do NOT add new findings here.
> New findings → [`docs/entity-registry.yaml`](../entity-registry.yaml).
> Migration details → [`docs/entity-registry-migration-guide.md`](../entity-registry-migration-guide.md).
>
> **Backward-compat rule:** All entries in this file implicitly carry `record_type: finding`
> (docs/entity-registry-migration-guide.md Rule 1). FIND-NNN IDs map 1:1 to FND-NNN in
> `entity-registry.yaml` (same numeric suffix; `legacy_id` field preserved).
>
> **Source audits merged:** CLI command audit · Config doc audit ·
> Cross-document contradiction scan · Skill-CLI mapping audit ·
> Semantic link-rot report · Runtime capability crosscheck

---

## Schema Reference

> **v1.5 NOTE:** In the new multi-entity registry (`docs/entity-registry.yaml`), these
> FIND-NNN fields map to FND-NNN fields as follows: `id`→`finding_id`/`legacy_id`;
> `claim_id`→`legacy_claim_ref`+`implicated_claim_ids[0]`; `claim`→claim record;
> `resolution_ref`→`correction`; `code_deps`→claim record. A new `record_type: finding`
> discriminator field is added (implicit for all entries in this legacy file).
> Full field mapping: `docs/entity-registry-migration-guide.md`.

Each finding record carries these **ten** fields (v1.5 adds `record_type`):

| Field | Type | Description |
|-------|------|-------------|
| `record_type` | `finding` | *(v1.5, implicit for all entries in this file)* Multi-entity discriminator. All FIND-NNN entries in this legacy file are implicitly `record_type: finding`. Explicit in `docs/entity-registry.yaml` FND-NNN records. |
| `id` | `FIND-NNN` | Normalized, stable finding identifier |
| `claim_id` | `CLM-NNN` | *(v1.4, required)* Stable claim identifier — independently referenceable entity separate from the finding ID. Format: `CLM-NNN` (three or more digits, zero-padded). Allows claim cross-referencing without coupling to the finding sequence. |
| `claim` | string | Concise statement of the erroneous or missing claim |
| `severity` | `critical` \| `high` \| `medium` \| `low` | Impact per [CONTRIBUTING.md rubric](../../CONTRIBUTING.md#documentation-issue-severity-rubric) |
| `gap_type` | enum (see below) | Nature of the documentation gap |
| `sub_qualifier` | string \| null | *(v1.3, optional — renamed from `gap_type_qualifier`)* Narrows `gap_type: inaccuracy`; see qualifier table below |
| `status` | `resolved` \| `open` \| `tracked` | Current resolution state |
| `affected_documents` | list of paths | Documents that contain or must receive the fix |
| `code_deps` | list of paths | *(v1.4, required)* Source code files that establish the claim's truth value. Empty list `[]` for purely cross-doc or documentation-only findings. Paths relative to repository root. |
| `resolution_ref` | string | Source-audit ID(s) and/or fix description |

### `gap_type` Values

> **v1.3 ENUM CONTRACT (Sub-AC 6c, 2026-03-15)**
> - `inconsistency` is **NOT** a valid `gap_type` value and never was.  Use
>   `gap_type: inaccuracy` + `sub_qualifier: cross-doc` to express cross-document
>   inconsistencies.  All downstream consumers have been audited: zero dangling
>   references to `inconsistency` exist in any registry entry.
> - `contradiction` is **DEPRECATED** and **fully migrated** as of schema v1.3.
>   All FIND-NNN entries that previously carried `gap_type: contradiction` have been
>   updated to `gap_type: inaccuracy` + `sub_qualifier: cross-doc` (see FIND-014,
>   FIND-022, FIND-040).  New findings MUST NOT use `contradiction`; use
>   `gap_type: inaccuracy` + `sub_qualifier: cross-doc` instead.
> - `gap_type_qualifier` (v1.2 field name) is renamed to `sub_qualifier` in v1.3.
>   The old field name is accepted as a backward-compatible alias by any tool that
>   reads this registry, but all new entries MUST use `sub_qualifier`.

| Value | Meaning | Qualifier applicable? |
|-------|---------|----------------------|
| `wrong-value` | Doc states a factually incorrect value (wrong path, flag name, key, count) | No |
| `missing-content` | Content that exists in code/runtime but is absent from docs | No |
| `misleading` | Correct information absent or framing creates false expectations | No |
| `inaccuracy` | Doc makes a factual claim that is incorrect or inconsistent with the source of truth; use `sub_qualifier` to sub-classify (e.g., `cross-doc` for cross-document conflicts) | **Yes** (required when qualifier applies) |
| `staleness` | Once-correct content that no longer matches current implementation; only set when a staleness signal has fired (a file in `code_deps[]` modified since `last_verified`) and the claim has not been re-verified. Set by SEC-010 in `staleness-enforcement-spec.yaml`. **Replaces former `stale` value (renamed v1.7).** | No |
| `stale` | *(DEPRECATED — renamed to `staleness` in schema v1.7, Sub-AC 3c)* Legacy alias; migrate all existing entries to `staleness`. | *(legacy)* |
| `link-rot` | A link exists but its target cannot fulfill what the source context promises | No |
| `contradiction` | *(DEPRECATED — use `inaccuracy + qualifier: cross-doc`)* Two documents state mutually exclusive values for the same claim | *(legacy)* |

### `sub_qualifier` Values

Applies only when `gap_type: inaccuracy`.  Absent or `null` when `gap_type` is any
other value.  *(Field was named `gap_type_qualifier` in schema v1.2; renamed to
`sub_qualifier` in v1.3 for alignment with claim-registry-spec.yaml conventions.)*

| Qualifier | Meaning |
|-----------|---------|
| `cross-doc` | The inaccuracy is a cross-document inconsistency: two or more docs make mutually exclusive claims about the same fact.  One (or more) of them is wrong relative to the source of truth.  **This qualifier replaces the deprecated `contradiction` gap_type.** Previously informal descriptions of these findings as "inconsistencies" must use this canonical form. |
| `stale-value` | The inaccuracy arises from a value that was once correct but diverged after a code change.  Use `inaccuracy + sub_qualifier: stale-value` when the claim is directly contradicted by current source code (not merely likely outdated). NOTE (v1.7 Sub-AC 3a): In the **claim registry** (claim-registry.yaml), staleness is now expressed via `staleness_signal.cause: code_dep_changed` (not via `gap_type`). In the **findings registry** (this file), `gap_type: staleness` remains valid for findings about once-correct-now-drifted values. Use `staleness` (not `inaccuracy`) when the claim may still be correct but has not been re-verified after a code change. |
| `aspirational` | The inaccuracy is a forward-looking or placeholder claim presented as current fact.  Use this sub_qualifier instead of leaving the claim unclassified when the mismatch is intentional-but-misleading (e.g., docs describe a planned feature as if already shipped). |

### `severity` Definitions (per CONTRIBUTING.md)

| Level | Definition |
|-------|-----------|
| `critical` | User follows docs and **fails** (command error, wrong path, flag rejected) |
| `high` | User proceeds **incorrectly** or holds a false expectation. Includes: nonexistent env vars that silently have no effect; major config sections absent from all docs (user cannot configure production behavior). |
| `medium` | User is mildly confused but can still succeed. Includes: option missing from one reference doc but present in another; minor behavior notes absent; optional/minor config sections undocumented. |
| `low` | Minor gap; cosmetic; an alternative form is undocumented but the canonical form works; edge case covered elsewhere. No confusion or incorrect outcome results. |

> **Rubric alignment note (AC-06, 2026-03-15):** The `low` level was not in CONTRIBUTING.md's
> original rubric (which defined only Critical/High/Medium). The rubric has been updated to include
> `low` as a formal fourth level. Findings previously classified as `low` have been re-evaluated:
> nonexistent env vars upgraded to `high`; undocumented-but-harmless gaps upgraded to `medium`;
> the `low` bucket is now empty across all 50 registry entries.

---

## Status Summary

| Severity | Total | Resolved | Open |
|----------|-------|----------|------|
| critical | 4 | 4 | 0 |
| high | 26 | 24 | 2 |
| medium | 20 | 17 | 3 |
| low | 0 | 0 | 0 |
| **Total** | **50** | **45** | **5** |

> **Note (AC-06 severity audit, 2026-03-15):** 11 findings reclassified upward and 4 reclassified
> from `low` to `medium` to align with the CONTRIBUTING.md severity rubric. `low` is now 0 — all
> findings at or above `medium`. The `medium open` count corrects a pre-existing table error (was
> stated as 1; actual count was 2 before FIND-050 moved from `low` to `medium`).

Open findings: [FIND-018](#find-018) *(high)*, [FIND-019](#find-019) *(high)*, [FIND-044](#find-044) *(medium)*, [FIND-045](#find-045) *(medium)*, [FIND-050](#find-050) *(medium)*

---

## Findings Data (Machine-Parseable)

```yaml
findings:

  # ── CRITICAL ──────────────────────────────────────────────────────────────

  - id: FIND-001
    claim_id: CLM-001
    claim: >-
      README.md Commands table presented interview, seed, evaluate, evolve,
      unstuck, ralph, tutorial, and help as ouroboros CLI commands; none of
      these exist in the CLI (they are ooo Claude Code skills only).
    severity: critical
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - README.md
    code_deps:
      - src/ouroboros/cli/main.py
      - src/ouroboros/cli/commands/__init__.py
    resolution_ref: "cli-audit-findings.md#F-01; fixed by prior generation"

  - id: FIND-002
    claim_id: CLM-002
    claim: >-
      architecture.md "Configuration Files" example block showed five config
      keys (event_store_path, max_concurrent_agents, checkpoint_interval,
      theme, log_level) that do not exist in OuroborosConfig.
    severity: critical
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/architecture.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-001; fixed in config-doc pass"

  - id: FIND-003
    claim_id: CLM-003
    claim: >-
      architecture.md "Environment Variables" block listed three variables
      (OUROBOROS_TUI_THEME, OUROBOROS_MAX_AGENTS, OUROBOROS_EVENT_CACHE_SIZE)
      that are not read by any Ouroboros source file.
    severity: critical
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/architecture.md
    code_deps:
      - src/ouroboros/config/loader.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-002; fixed in config-doc pass"

  - id: FIND-004
    claim_id: CLM-004
    claim: >-
      cli-reference.md and execution-failure-modes.md stated the SQLite event
      store path as ~/.ouroboros/data/ouroboros.db; the actual runtime path
      (hardcoded in event_store.py and tui.py) is ~/.ouroboros/ouroboros.db.
    severity: critical
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/execution-failure-modes.md
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/persistence/event_store.py
      - src/ouroboros/cli/commands/tui.py
    resolution_ref: >-
      cross-document-contradiction-findings.md#CONTRADICTION-001;
      runtime-capability-crosscheck.md#Sec11;
      doc-issues-register.md#ISSUE-R01; fixed 2026-03-15

  # ── HIGH ──────────────────────────────────────────────────────────────────

  - id: FIND-005
    claim_id: CLM-005
    claim: >-
      cli-usage.md ouroboros init start options table omitted four implemented
      options: --orchestrator/-o, --runtime, --llm-backend, --debug/-d.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/init.py
    resolution_ref: "cli-audit-findings.md#F-02; fixed by prior generation"

  - id: FIND-006
    claim_id: CLM-006
    claim: >-
      cli-usage.md ouroboros run workflow options table omitted two implemented
      options: --runtime and --no-qa.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/run.py
    resolution_ref: "cli-audit-findings.md#F-03; fixed by prior generation"

  - id: FIND-007
    claim_id: CLM-007
    claim: >-
      cli-usage.md ouroboros mcp serve options table omitted three implemented
      options: --db, --runtime, --llm-backend.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/mcp.py
    resolution_ref: "cli-audit-findings.md#F-04; fixed by prior generation"

  - id: FIND-008
    claim_id: CLM-008
    claim: >-
      Both cli-reference.md and cli-usage.md showed no options for ouroboros
      mcp info, omitting the --runtime and --llm-backend options that are
      implemented in mcp.py lines 316-337.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/mcp.py
    resolution_ref: "cli-audit-findings.md#F-05; fixed by prior generation"

  - id: FIND-009
    claim_id: CLM-009
    claim: >-
      cli-usage.md Commands Overview table omitted ouroboros setup and
      ouroboros cancel, both of which are fully implemented commands
      registered in main.py.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/main.py
      - src/ouroboros/cli/commands/setup.py
      - src/ouroboros/cli/commands/cancel.py
    resolution_ref: >-
      cli-audit-findings.md#F-06;
      runtime-capability-crosscheck.md#Sec5; fixed by prior generation

  - id: FIND-010
    claim_id: CLM-010
    claim: >-
      Both cli-reference.md and cli-usage.md described --dry-run as "validate
      seed without executing." In the default orchestrator mode the flag is
      accepted by Typer but never passed to _run_orchestrator(), so the full
      workflow executes silently.
    severity: high
    gap_type: misleading
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/run.py
      - src/ouroboros/orchestrator/
    resolution_ref: "cli-audit-findings.md#F-11; doc-issues-register.md#ISSUE-R09; fixed in this generation"

  - id: FIND-011
    claim_id: CLM-011
    claim: >-
      Twenty OUROBOROS_* environment variables recognized by
      src/ouroboros/config/loader.py were absent from all public
      documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/loader.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-015; fixed: env vars added to config-reference.md"

  - id: FIND-012
    claim_id: CLM-012
    claim: >-
      Seven additional env vars active in source code
      (OUROBOROS_LOG_MODE, OUROBOROS_AGENTS_DIR, OUROBOROS_WEB_SEARCH_TOOL,
      OUROBOROS_EXECUTION_MODEL, OUROBOROS_VALIDATION_MODEL,
      OUROBOROS_EVOLVE_STAGE1, OUROBOROS_GENERATION_TIMEOUT) were absent
      from the user-facing config-reference.md despite being documented in
      config-inventory.md.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/loader.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-018; fixed in second-pass config audit"

  - id: FIND-013
    claim_id: CLM-013
    claim: >-
      README.md ooo Skills table listed only 11 skills, omitting ooo cancel,
      ooo update, and ooo welcome which all exist in the skills/ directory.
    severity: high
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - README.md
    code_deps:
      - skills/
    resolution_ref: >-
      cross-document-contradiction-findings.md#CONTRADICTION-002;
      doc-issues-register.md#ISSUE-R02; fixed 2026-03-15

  - id: FIND-014
    claim_id: CLM-014
    claim: >-
      cli-usage.md TUI Keyboard Shortcuts table omitted the p (Pause
      execution) key that is documented in cli-reference.md, getting-started.md,
      and tui-usage.md.
    severity: high
    gap_type: inaccuracy
    sub_qualifier: cross-doc
    status: resolved
    affected_documents:
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/tui.py
    resolution_ref: >-
      cross-document-contradiction-findings.md#CONTRADICTION-003;
      doc-issues-register.md#ISSUE-R03; fixed 2026-03-15
      [gap_type migrated: contradiction → inaccuracy/cross-doc in Sub-AC 6c]

  - id: FIND-015
    claim_id: CLM-015
    claim: >-
      Three documentation files described ouroboros config init as
      "informational only / placeholder that does not write files." The command
      actually creates ~/.ouroboros/config.yaml and ~/.ouroboros/credentials.yaml
      with default templates and sets chmod 600 on credentials.yaml.
    severity: high
    gap_type: misleading
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
      - docs/config-inventory.md
    code_deps:
      - src/ouroboros/cli/commands/config.py
    resolution_ref: >-
      cross-document-contradiction-findings.md#CONTRADICTION-004;
      contributing/config-doc-findings.md#OPEN-003 (RESOLVED-003);
      doc-issues-register.md#ISSUE-R04; fixed 2026-03-15

  - id: FIND-016
    claim_id: CLM-016
    claim: >-
      cli-reference.md in multiple locations claimed that scripts/install.sh
      bootstraps Codex ooo skill artifacts into ~/.codex/. This is not
      implemented; every ooo skill is "Not yet" on Codex.
    severity: high
    gap_type: misleading
    status: resolved
    affected_documents:
      - docs/cli-reference.md
    code_deps:
      - src/ouroboros/cli/commands/setup.py
      - scripts/install.sh
    resolution_ref: >-
      contributing/config-doc-findings.md#FINDING-005 (RESOLVED-001);
      runtime-capability-crosscheck.md#Sec4 and #4c-2;
      doc-issues-register.md#ISSUE-R06; fixed 2026-03-15

  - id: FIND-017
    claim_id: CLM-017
    claim: >-
      architecture.md Plugin Layer section stated "9 core workflow skills"
      when the actual count is 14 (confirmed by skills/ directory enumeration).
    severity: high
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/architecture.md
    code_deps:
      - skills/
    resolution_ref: "runtime-capability-crosscheck.md#4c-1; fixed 2026-03-15"

  - id: FIND-018
    claim_id: CLM-018
    claim: >-
      README.md Quick Start for Claude Code shows claude plugin marketplace add
      + ooo skill commands, then links to docs/runtime-guides/claude-code.md
      for "full details." The linked doc covers a different install path
      (pip install) and different commands (uv run ouroboros run workflow
      --orchestrator) and does not document the claude plugin / ooo workflow.
    severity: high
    gap_type: link-rot
    status: open
    affected_documents:
      - README.md
      - docs/runtime-guides/claude-code.md
    code_deps: []
    resolution_ref: >-
      semantic-link-rot-report.md#MISMATCH-1;
      recommended fix: expand claude-code.md to cover the claude plugin
      marketplace add + ooo workflow, or update README Quick Start to match
      what claude-code.md documents

  - id: FIND-019
    claim_id: CLM-019
    claim: >-
      docs/architecture.md Deployment section shows claude plugin marketplace
      add + ooo interview as the Claude Code deployment path, then links to
      runtime-guides/claude-code.md for "full details." The target covers only
      pip install + uv run ouroboros run workflow --orchestrator, not the
      plugin/ooo workflow shown in the source context.
    severity: high
    gap_type: link-rot
    status: open
    affected_documents:
      - docs/architecture.md
      - docs/runtime-guides/claude-code.md
    code_deps: []
    resolution_ref: >-
      semantic-link-rot-report.md#MISMATCH-2;
      same root cause as FIND-018; recommended fix: consolidate
      claude-code.md install path or split into clearly-labeled sections

  # ── MEDIUM ────────────────────────────────────────────────────────────────

  - id: FIND-020
    claim_id: CLM-020
    claim: >-
      getting-started.md "Performance Issues" troubleshooting section
      recommended: export OUROBOROS_MAX_PARALLEL=2. This env var does not
      exist and has no effect; the correct mechanism is the --sequential flag.
    severity: high
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/getting-started.md
    code_deps:
      - src/ouroboros/config/loader.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-003; fixed in config-doc pass"

  - id: FIND-021
    claim_id: CLM-021
    claim: >-
      Both cli-reference.md and cli-usage.md showed ~/.config/claude/config.json
      as the Claude Desktop MCP registration path. The actual path written by
      ouroboros setup (setup.py line 74) is ~/.claude/mcp.json.
    severity: medium
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/setup.py
    resolution_ref: >-
      cli-audit-findings.md#F-07;
      doc-issues-register.md#ISSUE-R08; fixed by prior generation

  - id: FIND-022
    claim_id: CLM-022
    claim: >-
      getting-started.md TUI "Interactive Features" section listed Space,
      D, and C as keyboard shortcuts that do not appear in the authoritative
      TUI reference docs (cli-reference.md, tui-usage.md); the documented
      shortcuts p, r, q were absent.
    severity: medium
    gap_type: inaccuracy
    sub_qualifier: cross-doc
    status: resolved
    affected_documents:
      - docs/getting-started.md
    code_deps:
      - src/ouroboros/cli/commands/tui.py
    resolution_ref: >-
      cli-audit-findings.md#F-08; fixed by prior generation
      [gap_type migrated: contradiction → inaccuracy/cross-doc in Sub-AC 6c]

  - id: FIND-023
    claim_id: CLM-023
    claim: >-
      Both cli-reference.md and cli-usage.md omitted the -o (enable
      orchestrator) and -O (disable orchestrator) short flags for
      ouroboros run workflow --orchestrator/--no-orchestrator, which are
      registered in run.py lines 292-299.
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/run.py
    resolution_ref: "cli-audit-findings.md#F-10; fixed in this generation"

  - id: FIND-024
    claim_id: CLM-024
    claim: >-
      Neither cli-reference.md nor cli-usage.md documented the behavior when
      opencode is the only runtime detected by ouroboros setup: setup.py scans
      for opencode but the configuration handler only supports claude/codex,
      so setup exits with "Unsupported runtime: opencode."
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/setup.py
    resolution_ref: "cli-audit-findings.md#F-12; fixed in this generation"

  - id: FIND-025
    claim_id: CLM-025
    claim: >-
      The entire EconomicsConfig section (economics: in config.yaml, which
      configures the PAL Router including tier definitions and escalation
      thresholds) was absent from all public documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-006; fixed: full section added to config-reference.md"

  - id: FIND-026
    claim_id: CLM-026
    claim: >-
      The entire ClarificationConfig section (clarification: in config.yaml,
      covering Phase 0 / Big Bang settings including ambiguity_threshold,
      max_interview_rounds, model_tier, default_model) was absent from all
      public documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-007; fixed: full section added to config-reference.md"

  - id: FIND-027
    claim_id: CLM-027
    claim: >-
      The entire ExecutionConfig section (execution: in config.yaml, covering
      Phase 2 / Double Diamond settings) was absent from all public documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-008; fixed: full section added to config-reference.md"

  - id: FIND-028
    claim_id: CLM-028
    claim: >-
      The entire ResilienceConfig section (resilience: in config.yaml, covering
      Phase 3 / stagnation and lateral thinking settings) was absent from all
      public documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-009; fixed: full section added to config-reference.md"

  - id: FIND-029
    claim_id: CLM-029
    claim: >-
      The entire EvaluationConfig section (evaluation: in config.yaml, covering
      Phase 4 / 3-stage pipeline settings) was absent from all public documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-010; fixed: full section added to config-reference.md"

  - id: FIND-030
    claim_id: CLM-030
    claim: >-
      The entire ConsensusConfig section (consensus: in config.yaml, covering
      Phase 5 / multi-model voting settings) was absent from all public documentation.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-011; fixed: full section added to config-reference.md"

  - id: FIND-031
    claim_id: CLM-031
    claim: >-
      The entire PersistenceConfig section (persistence: in config.yaml) was
      absent from all public documentation; users had no way to learn that
      persistence can be disabled or that database_path is configurable.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-012; fixed: full section added to config-reference.md"

  - id: FIND-032
    claim_id: CLM-032
    claim: >-
      The entire DriftConfig section (drift: in config.yaml, covering
      warning_threshold and critical_threshold) was absent from all public
      documentation despite drift monitoring being discussed conceptually in
      architecture docs.
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-013; fixed: full section added to config-reference.md"

  - id: FIND-033
    claim_id: CLM-033
    claim: >-
      logging.log_path and logging.include_reasoning were never documented
      anywhere; only logging.level appeared in existing config examples.
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-014; fixed: full logging section added"

  - id: FIND-034
    claim_id: CLM-034
    claim: >-
      Five orchestrator config options were undocumented: permission_mode,
      opencode_permission_mode, cli_path, opencode_cli_path,
      default_max_turns. Only runtime_backend and codex_cli_path appeared
      in existing examples.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-016; fixed: full orchestrator section added"

  - id: FIND-035
    claim_id: CLM-035
    claim: >-
      Five llm config options were undocumented: permission_mode,
      opencode_permission_mode, qa_model, dependency_analysis_model,
      ontology_analysis_model, context_compression_model. Only llm.backend
      appeared in existing config examples.
    severity: high
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-017; fixed: full llm section added"

  - id: FIND-036
    claim_id: CLM-036
    claim: >-
      OUROBOROS_GENERATION_TIMEOUT is read in two source files with different
      hardcoded defaults (0 in evolution/loop.py meaning no timeout; 7200 in
      mcp/tools/definitions.py as MCP protocol-level timeout); documentation
      treated it as a single-default variable, hiding the dual-usage behavior.
    severity: medium
    gap_type: misleading
    status: resolved
    affected_documents:
      - docs/config-reference.md
      - docs/config-inventory.md
    code_deps:
      - src/ouroboros/orchestrator/evolution/loop.py
      - src/ouroboros/mcp/tools/definitions.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-019; fixed: dual-usage note added to config-reference.md"

  - id: FIND-037
    claim_id: CLM-037
    claim: >-
      The "API Keys" table in config-reference.md listed only three provider
      keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY) and omitted
      GOOGLE_API_KEY, which is required for Gemini models used in the frugal
      and standard tier defaults.
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/config-reference.md
    code_deps:
      - src/ouroboros/config/loader.py
      - src/ouroboros/config/models.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-020; fixed: GOOGLE_API_KEY row added"

  - id: FIND-038
    claim_id: CLM-038
    claim: >-
      Four documentation files (cli-reference.md, cli-usage.md, getting-started.md,
      README.md) listed only 4 TUI screens (keys 1-4: Dashboard, Execution, Logs,
      Debug). Two additional views documented in tui-usage.md were missing: Session
      Selector (s) and Lineage (e).
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
      - docs/getting-started.md
      - README.md
    code_deps:
      - src/ouroboros/cli/commands/tui.py
    resolution_ref: >-
      cross-document-contradiction-findings.md#CONTRADICTION-005;
      doc-issues-register.md#ISSUE-R05; fixed 2026-03-15

  - id: FIND-039
    claim_id: CLM-039
    claim: >-
      cli-reference.md listed opencode as a valid --runtime and --llm-backend
      enum value alongside claude and codex, with no disclaimer that opencode
      is explicitly marked out of scope in cli-inventory.yaml.
    severity: medium
    gap_type: misleading
    status: resolved
    affected_documents:
      - docs/cli-reference.md
    code_deps:
      - src/ouroboros/cli/commands/run.py
      - src/ouroboros/cli/commands/mcp.py
    resolution_ref: >-
      runtime-capability-crosscheck.md#Sec2;
      fix: "(opencode is in the CLI enum but out of scope)" note added to all
      --runtime/--llm-backend option descriptions in cli-reference.md

  - id: FIND-040
    claim_id: CLM-040
    claim: >-
      runtime-capability-matrix.md used language implying ooo skills were
      available or near-available in Codex sessions ("route through the in-process
      MCP server inside Codex sessions"), contradicting codex.md which marks
      every skill as "Not yet."
    severity: medium
    gap_type: inaccuracy
    sub_qualifier: cross-doc
    status: resolved
    affected_documents:
      - docs/runtime-capability-matrix.md
    code_deps:
      - skills/
    resolution_ref: >-
      runtime-capability-crosscheck.md#Sec3;
      fix: matrix updated to "Not yet available. Codex skill artifacts exist
      in the repository but automatic installation into ~/.codex/ is not yet
      implemented."
      [gap_type migrated: contradiction → inaccuracy/cross-doc in Sub-AC 6c]

  - id: FIND-041
    claim_id: CLM-041
    claim: >-
      codex.md skill-to-CLI mapping table was missing ooo ralph, ooo tutorial,
      and ooo welcome — three skills that exist in the skills/ directory but had
      no documented Codex equivalent.
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/runtime-guides/codex.md
    code_deps:
      - skills/
    resolution_ref: "runtime-capability-crosscheck.md#4c-3; fixed 2026-03-15"

  - id: FIND-042
    claim_id: CLM-042
    claim: >-
      docs/README.md PyPI badge and link pointed to https://pypi.org/project/ouroboros/
      (wrong package name); the published package name is ouroboros-ai.
    severity: medium
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/README.md
    code_deps:
      - pyproject.toml
    resolution_ref: "runtime-capability-crosscheck.md#Sec12; fix: link corrected to ouroboros-ai"

  - id: FIND-043
    claim_id: CLM-043
    claim: >-
      docs/guides/common-workflows.md section 9 showed ~/.config/claude/config.json
      as the Claude Desktop MCP registration path. The actual path written by
      ouroboros setup is ~/.claude/mcp.json.
    severity: medium
    gap_type: staleness    # findings-registry schema: staleness = once-correct value drifted
    # [v1.7 Sub-AC 3a NOTE]: In claim-registry.yaml, this finding maps to CR-NNN with
    #   staleness_signal.cause=code_dep_changed (code_dep: setup.py changed the path).
    #   The claim-registry gap_type='stale'/'staleness' is RETIRED; use staleness_signal.cause.
    #   In THIS findings-registry, gap_type=staleness is still a valid classification.
    status: resolved
    affected_documents:
      - docs/guides/common-workflows.md
    code_deps:
      - src/ouroboros/cli/commands/setup.py
    resolution_ref: "runtime-capability-crosscheck.md#Sec14; fixed 2026-03-15"

  - id: FIND-044
    claim_id: CLM-044
    claim: >-
      codex.md skill-to-CLI mapping table shows ooo status CLI equivalent as
      uv run ouroboros status executions (plural, list command). The correct
      equivalent for the skill's primary operation (inspecting a specific session)
      is uv run ouroboros status execution <session_id> (singular).
    severity: medium
    gap_type: wrong-value
    status: open
    affected_documents:
      - docs/runtime-guides/codex.md
    code_deps:
      - src/ouroboros/cli/commands/status.py
    resolution_ref: >-
      contributing/skill-cli-mapping-findings.md#MISMATCH-1;
      recommended fix: update row to show status execution <session_id> as
      primary form; also note that drift-measurement capability has no CLI
      equivalent at all

  - id: FIND-045
    claim_id: CLM-045
    claim: >-
      docs/runtime-guides/claude-code.md and docs/runtime-guides/codex.md
      describe API key requirements but neither links to the credentials.yaml
      schema in config-reference.md, causing friction for users following
      runtime guides to configure credentials.
    severity: medium
    gap_type: missing-content
    status: open
    affected_documents:
      - docs/runtime-guides/claude-code.md
      - docs/runtime-guides/codex.md
    code_deps:
      - src/ouroboros/config/models.py
    resolution_ref: >-
      contributing/config-doc-findings.md#OPEN-002;
      doc-issues-register.md#ISSUE-001;
      recommended fix: add "For the full credentials.yaml schema see
      [Config Reference — Credentials](../config-reference.md#credentials)"
      to each runtime guide's credentials section

  # ── LOW ───────────────────────────────────────────────────────────────────

  - id: FIND-046
    claim_id: CLM-046
    claim: >-
      cli-usage.md CI/CD Usage section showed OUROBOROS_LOG_LEVEL=DEBUG as a
      valid env var. OUROBOROS_LOG_LEVEL is not recognized; log level is
      controlled via logging.level in config.yaml or the --debug CLI flag.
      User sets the env var in CI/CD expecting debug output; it silently has no effect.
    severity: high
    gap_type: wrong-value
    status: resolved
    affected_documents:
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/config/loader.py
    resolution_ref: "contributing/config-doc-findings.md#FINDING-004; fixed in config-doc pass"

  - id: FIND-047
    claim_id: CLM-047
    claim: >-
      cli-reference.md init list section showed no options table; the
      --state-dir option implemented in init.py lines 664-675 was absent.
      The same option was correctly documented in cli-usage.md.
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
    code_deps:
      - src/ouroboros/cli/commands/init.py
    resolution_ref: "cli-audit-findings.md#F-13; fixed in this generation"

  - id: FIND-048
    claim_id: CLM-048
    claim: >-
      Both cli-reference.md and cli-usage.md omitted the mcp serve startup
      behavior: on each start it auto-cancels sessions left in RUNNING or
      PAUSED state for more than 1 hour (mcp.py lines 139-149).
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/mcp.py
    resolution_ref: "cli-audit-findings.md#F-14; fixed in this generation"

  - id: FIND-049
    claim_id: CLM-049
    claim: >-
      Both cli-reference.md and cli-usage.md only documented ouroboros tui
      monitor (explicit subcommand) and ouroboros monitor (top-level alias)
      without noting that ouroboros tui (bare, no subcommand) is also
      equivalent (tui.py callback invoke_without_command=True).
    severity: medium
    gap_type: missing-content
    status: resolved
    affected_documents:
      - docs/cli-reference.md
      - docs/guides/cli-usage.md
    code_deps:
      - src/ouroboros/cli/commands/tui.py
    resolution_ref: "cli-audit-findings.md#F-15; fixed in this generation"

  - id: FIND-050
    claim_id: CLM-050
    claim: >-
      codex.md maps ooo update CLI equivalent as pip install --upgrade
      ouroboros-ai, which is directionally correct but understates what the
      skill does: the skill first checks current version against PyPI,
      prompts for confirmation, then upgrades, with an optional Claude Code
      plugin update step.
    severity: medium
    gap_type: misleading
    status: open
    affected_documents:
      - docs/runtime-guides/codex.md
    code_deps:
      - skills/update/SKILL.md
    resolution_ref: >-
      contributing/skill-cli-mapping-findings.md#MISMATCH-2;
      recommended fix: add footnote clarifying that the CLI command upgrades
      directly without the version-check wrapper the skill provides
```

---

## Human-Readable Summary Table

| ID | Severity | Gap Type | Status | Claim (short) | Affected Documents |
|----|----------|----------|--------|---------------|-------------------|
| FIND-001 | critical | wrong-value | resolved | README ghost CLI commands (interview, seed, etc.) | `README.md` |
| FIND-002 | critical | wrong-value | resolved | architecture.md shows 5 nonexistent config keys | `docs/architecture.md` |
| FIND-003 | critical | wrong-value | resolved | architecture.md lists 3 nonexistent env vars | `docs/architecture.md` |
| FIND-004 | critical | wrong-value | resolved | SQLite path shown as `~/.ouroboros/data/ouroboros.db` | `docs/cli-reference.md`, `docs/guides/execution-failure-modes.md` |
| FIND-005 | high | missing-content | resolved | `init start` options table missing 4 options | `docs/guides/cli-usage.md` |
| FIND-006 | high | missing-content | resolved | `run workflow` options table missing `--runtime`/`--no-qa` | `docs/guides/cli-usage.md` |
| FIND-007 | high | missing-content | resolved | `mcp serve` options table missing `--db`/`--runtime`/`--llm-backend` | `docs/guides/cli-usage.md` |
| FIND-008 | high | missing-content | resolved | `mcp info` options completely undocumented | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| FIND-009 | high | missing-content | resolved | Commands Overview missing `cancel` and `setup` | `docs/guides/cli-usage.md` |
| FIND-010 | high | misleading | resolved | `--dry-run` silently ignored in default orchestrator mode | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| FIND-011 | high | missing-content | resolved | 20 `OUROBOROS_*` env vars absent from all docs | `docs/config-reference.md` |
| FIND-012 | high | missing-content | resolved | 7 env vars absent from config-reference.md (second-pass) | `docs/config-reference.md` |
| FIND-013 | high | wrong-value | resolved | README lists 11 `ooo` skills; actual count is 14 | `README.md` |
| FIND-014 | high | inaccuracy/cross-doc | resolved | TUI `p` (pause) key missing from cli-usage.md | `docs/guides/cli-usage.md` |
| FIND-015 | high | misleading | resolved | `config init` described as placeholder; actually creates files | `docs/cli-reference.md`, `docs/guides/cli-usage.md`, `docs/config-inventory.md` |
| FIND-016 | high | misleading | resolved | `cli-reference.md` falsely claims `install.sh` bootstraps Codex `ooo` artifacts | `docs/cli-reference.md` |
| FIND-017 | high | wrong-value | resolved | `architecture.md` states "9 core workflow skills"; actual count is 14 | `docs/architecture.md` |
| **FIND-018** | **high** | **link-rot** | **open** | `README.md` → `claude-code.md` workflow mismatch | `README.md`, `docs/runtime-guides/claude-code.md` |
| **FIND-019** | **high** | **link-rot** | **open** | `architecture.md` → `claude-code.md` workflow mismatch | `docs/architecture.md`, `docs/runtime-guides/claude-code.md` |
| FIND-020 | **high** | wrong-value | resolved | `OUROBOROS_MAX_PARALLEL` recommended but nonexistent — silently no effect | `docs/getting-started.md` |
| FIND-021 | medium | wrong-value | resolved | Claude Desktop MCP path wrong (`~/.config/claude/config.json`) | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| FIND-022 | medium | inaccuracy/cross-doc | resolved | TUI shortcuts `Space`/`D`/`C` in getting-started.md not in reference | `docs/getting-started.md` |
| FIND-023 | medium | missing-content | resolved | `run workflow` `-o`/`-O` short flags not documented | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| FIND-024 | medium | missing-content | resolved | `setup` opencode-only failure mode not documented | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| FIND-025 | **high** | missing-content | resolved | `economics` config section entirely undocumented — PAL Router un-configurable | `docs/config-reference.md` |
| FIND-026 | **high** | missing-content | resolved | `clarification` config section entirely undocumented — Phase 0 un-configurable | `docs/config-reference.md` |
| FIND-027 | **high** | missing-content | resolved | `execution` config section entirely undocumented — Phase 2 un-configurable | `docs/config-reference.md` |
| FIND-028 | **high** | missing-content | resolved | `resilience` config section entirely undocumented — Phase 3 un-configurable | `docs/config-reference.md` |
| FIND-029 | **high** | missing-content | resolved | `evaluation` config section entirely undocumented — Phase 4 un-configurable | `docs/config-reference.md` |
| FIND-030 | **high** | missing-content | resolved | `consensus` config section entirely undocumented — Phase 5 un-configurable | `docs/config-reference.md` |
| FIND-031 | **high** | missing-content | resolved | `persistence` config section entirely undocumented — db path un-configurable | `docs/config-reference.md` |
| FIND-032 | medium | missing-content | resolved | `drift` config section entirely undocumented | `docs/config-reference.md` |
| FIND-033 | medium | missing-content | resolved | `logging.log_path` and `logging.include_reasoning` undocumented | `docs/config-reference.md` |
| FIND-034 | **high** | missing-content | resolved | 5 `orchestrator` config options undocumented (permission_mode, cli_path, etc.) | `docs/config-reference.md` |
| FIND-035 | **high** | missing-content | resolved | 5+ `llm` config options undocumented (qa_model, dependency_analysis_model, etc.) | `docs/config-reference.md` |
| FIND-036 | medium | misleading | resolved | `OUROBOROS_GENERATION_TIMEOUT` conflicting defaults not documented | `docs/config-reference.md`, `docs/config-inventory.md` |
| FIND-037 | medium | missing-content | resolved | `GOOGLE_API_KEY` absent from API Keys env var table | `docs/config-reference.md` |
| FIND-038 | medium | missing-content | resolved | TUI Session Selector (`s`) and Lineage (`e`) views omitted from 4 docs | `docs/cli-reference.md`, `docs/guides/cli-usage.md`, `docs/getting-started.md`, `README.md` |
| FIND-039 | medium | misleading | resolved | `opencode` in `--runtime` options without out-of-scope disclaimer | `docs/cli-reference.md` |
| FIND-040 | medium | inaccuracy/cross-doc | resolved | `runtime-capability-matrix.md` implied `ooo` skills available on Codex | `docs/runtime-capability-matrix.md` |
| FIND-041 | medium | missing-content | resolved | `codex.md` mapping table missing `ooo ralph`, `ooo tutorial`, `ooo welcome` | `docs/runtime-guides/codex.md` |
| FIND-042 | medium | wrong-value | resolved | `docs/README.md` PyPI link pointed to wrong package name | `docs/README.md` |
| FIND-043 | medium | staleness | resolved | `common-workflows.md` MCP path stale (`~/.config/claude/config.json`) | `docs/guides/common-workflows.md` |
| **FIND-044** | **medium** | **wrong-value** | **open** | `ooo status` CLI equivalent in `codex.md` uses `executions` (list) not `execution <id>` | `docs/runtime-guides/codex.md` |
| **FIND-045** | **medium** | **missing-content** | **open** | Runtime guides lack cross-link to `credentials.yaml` schema | `docs/runtime-guides/claude-code.md`, `docs/runtime-guides/codex.md` |
| FIND-046 | **high** | wrong-value | resolved | `OUROBOROS_LOG_LEVEL` in `cli-usage.md` CI/CD example; does not exist — silently no effect | `docs/guides/cli-usage.md` |
| FIND-047 | **medium** | missing-content | resolved | `init list --state-dir` option absent from `cli-reference.md` (present in `cli-usage.md`) | `docs/cli-reference.md` |
| FIND-048 | **medium** | missing-content | resolved | `mcp serve` orphaned-session auto-cancel at startup not documented | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| FIND-049 | **medium** | missing-content | resolved | `ouroboros tui` bare invocation launches monitor; not documented | `docs/cli-reference.md`, `docs/guides/cli-usage.md` |
| **FIND-050** | **medium** | **misleading** | **open** | `ooo update` CLI equivalent in `codex.md` omits version-check wrapper | `docs/runtime-guides/codex.md` |

---

## Open Findings Detail

### FIND-018 — `README.md` → `claude-code.md` workflow mismatch (high)

The README Quick Start for Claude Code presents `claude plugin marketplace add` +
`ooo skill` commands as the primary workflow and links to `docs/runtime-guides/claude-code.md`
for "full details." The linked document covers a completely different install path
(`pip install ouroboros-ai[claude]`) and different commands
(`uv run ouroboros run workflow --orchestrator`). A user following the README Quick Start
will find the "full details" link goes to documentation for an entirely different workflow.

**Recommended fix (one of):**
1. Expand `claude-code.md` to cover the `claude plugin marketplace add` + `ooo` workflow as the primary Claude Code path.
2. Update `README.md` Quick Start to use the `pip install` + orchestrator path that `claude-code.md` documents.
3. If `claude plugin marketplace add` is not yet live, mark it `[NOT YET AVAILABLE]` and remove the cross-link promise.

---

### FIND-019 — `architecture.md` → `claude-code.md` workflow mismatch (high)

Same root cause as FIND-018. `docs/architecture.md` Deployment section shows
`claude plugin marketplace add` + `ooo interview` as the Claude Code path and links
to `claude-code.md` for "full details." The target documents only the `pip install` +
orchestrator CLI path. Fix is the same as FIND-018.

---

### FIND-044 — `ooo status` CLI equivalent wrong in `codex.md` (medium)

`codex.md` maps `ooo status` to `uv run ouroboros status executions` (plural — lists
all executions). The skill's primary operation is inspecting a specific session, for
which the correct CLI equivalent is `uv run ouroboros status execution <session_id>`
(singular). Additionally, the drift-measurement capability (`ouroboros_measure_drift`)
has no CLI equivalent at all; neither the list nor single-session CLI subcommands implement it.

**Recommended fix:** Update the codex.md row to:

```
| `ooo status` | Not yet | `uv run ouroboros status execution <session_id>` — or `uv run ouroboros status executions` to list all. Note: drift-measurement via ouroboros_measure_drift has no CLI equivalent. |
```

---

### FIND-045 — Runtime guides lack `credentials.yaml` cross-link (medium)

`docs/runtime-guides/claude-code.md` and `docs/runtime-guides/codex.md` both describe
API key requirements in their Prerequisites sections but neither links to the
`credentials.yaml` schema in `docs/config-reference.md`.

**Recommended fix:** Add to each runtime guide's credentials/API key section:

> For the full `credentials.yaml` schema and all supported keys, see
> [Config Reference — Credentials](../config-reference.md#credentials).

---

### FIND-050 — `ooo update` CLI equivalent understates skill behavior (low)

`codex.md` maps `ooo update` to `pip install --upgrade ouroboros-ai`, which performs
the upgrade correctly but omits the version-check wrapper the skill provides (check
current version → query PyPI for latest → prompt user → upgrade → optional Claude Code
plugin update → verify).

**Recommended fix:** Add a parenthetical clarification noting the CLI upgrades directly
without the version-check flow.

---

## Deduplication Notes

The following source-audit IDs were merged into single registry entries to eliminate
duplicate tracking:

| Registry Entry | Merged Source IDs |
|----------------|------------------|
| FIND-004 | `CONTRADICTION-001` + `runtime-capability-crosscheck.md#Sec11` |
| FIND-009 | `cli-audit-findings.md#F-06` + `runtime-capability-crosscheck.md#Sec5` |
| FIND-015 | `CONTRADICTION-004` + `config-doc-findings.md#OPEN-003` |
| FIND-016 | `config-doc-findings.md#FINDING-005` + `runtime-capability-crosscheck.md#Sec4` + `runtime-capability-crosscheck.md#4c-2` + `doc-issues-register.md#ISSUE-R06` |

The `doc-issues-register.md` entries ISSUE-R01 through ISSUE-R09 and ISSUE-001 are
cross-references to primary findings already captured above; they are not recorded as
separate entries.

Source audit `cli-audit-findings.md#F-09` (commands/__init__.py internal docstring
missing cancel/setup/tui) was intentionally excluded: it is a source code change, which
is out of scope for documentation-only findings.

---

## Filing a New Finding

Use the template below when adding a new `FIND-NNN` entry to the YAML findings block.
Assign the next sequential ID and choose `gap_type` from the valid enum; add
`sub_qualifier` only when `gap_type: inaccuracy`.

```yaml
- id: FIND-NNN                      # next available sequential ID
  claim: >-
    <one-paragraph factual statement of what is wrong or missing>
  severity: critical | high | medium | low
  gap_type: wrong-value | missing-content | misleading | inaccuracy | staleness | link-rot
  # sub_qualifier: cross-doc | stale-value | aspirational
  #   ^ include ONLY when gap_type: inaccuracy; omit entirely otherwise
  #   ^ cross-doc: inaccuracy is a conflict between two or more docs (replaces deprecated 'contradiction')
  #   ^ stale-value: inaccuracy is a diverged value after a code change
  #   ^ aspirational: inaccuracy is a planned/placeholder claim presented as current fact
  status: open | resolved | tracked
  affected_documents:
    - docs/<path/to/doc.md>
  resolution_ref: "<source-audit-id>; <fix description or 'pending'>"
```

**gap_type selection guide:**
- A doc says `~/.ouroboros/data/ouroboros.db` but source says `~/.ouroboros/ouroboros.db` → `wrong-value`
- A flag exists in code but no doc mentions it → `missing-content`
- A doc is technically correct but creates false expectations → `misleading`
- Doc A and Doc B say different things about the same fact → `inaccuracy` + `sub_qualifier: cross-doc`
- A link target doesn't match what the source context promises → `link-rot`
- A value was correct six months ago but code changed, and not yet re-verified → `staleness`
- A value that was once correct but is now directly contradicted by code → `inaccuracy + sub_qualifier: stale-value`
- **NEVER use** `stale` (renamed to `staleness` in schema v1.7, Sub-AC 3c; see migration note below)
- **NEVER use** `decay` (was never a valid enum value; see v1.7 migration below)
- **NEVER use** `inconsistency` (not a valid enum value; see v1.2/v1.3 migration below)
- **NEVER use** `contradiction` for new entries (deprecated in v1.2, fully retired in v1.3; use `inaccuracy + sub_qualifier: cross-doc`)
- **NEVER use** `gap_type_qualifier` in new entries (renamed to `sub_qualifier` in v1.3)

---

## Schema Changelog

### v1.4 — 2026-03-15 (Sub-AC 3c: gap_type 'stale' renamed to 'staleness'; 'decay' banned)

**Changes:**

1. **`stale` renamed to `staleness`.**  The `gap_type: stale` enum value has been renamed
   to `gap_type: staleness` in `claim-registry-spec.yaml` v1.7 (Sub-AC 3c).  The rename
   introduces an explicit trigger condition: `staleness` MUST only be set when a staleness
   signal has fired (a file in `code_deps[]` was modified after `last_verified`) AND the
   claim has not been re-verified.  This operationalises staleness as an executable
   mechanism (SEC-010) rather than a passive annotation.
2. **`decay` explicitly banned.**  `gap_type: decay` was never a valid enum value.
   The `no_decay_gap_type` validation rule (ERROR) in `claim-registry-spec.yaml` v1.7
   formally prohibits both `decay` and the legacy `stale` spelling.
3. **`doc-decay` terminology removed.**  Generated docs (`link-index.md`,
   `section-content-index.md`) that referred to "doc-decay detection" have been updated to
   use "staleness detection".
4. **FIND-043 migrated.**  The single live entry carrying `gap_type: stale` (FIND-043) has
   been updated to `gap_type: staleness`.

**Migration notes for tooling consumers (v1.4 update):**

- Parsers MUST reject `gap_type: decay` (was never valid).
- Parsers MUST reject `gap_type: stale` on entries added AFTER schema_version 1.4
  (rename to `staleness`).  Parsers MAY emit a deprecation WARNING on pre-v1.4 entries.
- Parsers MUST only set `gap_type: staleness` when a staleness signal is active per
  SEC-010 (staleness-enforcement-spec.yaml v1.4).
- The relationship between `staleness` and `inaccuracy + sub_qualifier: stale-value`:
  - Use `staleness` when the claim may still be correct but is unverified after a code change.
  - Use `inaccuracy + sub_qualifier: stale-value` when the claim is directly contradicted
    by the current code value (the value changed; the doc states the old value).

**[Sub-AC 3a NOTE — v1.7 claim-registry schema]:** In `claim-registry.yaml`, `gap_type:
staleness` has been RETIRED from the claim registry enum (Sub-AC 3a, 2026-03-15).
Staleness in claims is now expressed via `staleness_signal.cause` (enum:
`code_dep_changed | time_elapsed | upstream_changed`).  In THIS findings-registry,
`gap_type: staleness` REMAINS VALID — these are separate schemas.  Parsers consuming
findings-registry data MUST NOT apply the claim-registry no_stale_gap_type rule here.

**Entries migrated in this pass:**

| Finding | Old gap_type | New gap_type | Migration note |
|---------|-------------|--------------|----------------|
| FIND-043 | `stale` | `staleness` | Renamed per schema v1.7 / Sub-AC 3c |

**Audit result:** One live entry (`FIND-043`) carried `gap_type: stale` — migrated above.
Zero entries carried `gap_type: decay` (was never valid, confirmed absent).

---

### v1.3 — 2026-03-15 (Sub-AC 6c: gap_type migration completed)

**Changes:**

1. **`gap_type_qualifier` renamed to `sub_qualifier`.**  The field was introduced
   as `gap_type_qualifier` in v1.2 but renamed to the shorter `sub_qualifier` in v1.3
   for consistency with `claim-registry-spec.yaml` conventions.
   Tooling MUST accept `gap_type_qualifier` as a backward-compatible alias.
2. **`contradiction` fully retired.**  All three entries that carried `gap_type: contradiction`
   (FIND-014, FIND-022, FIND-040) have been migrated to `gap_type: inaccuracy` +
   `sub_qualifier: cross-doc`.  The `contradiction` value remains in the enum table
   as a marked-deprecated entry for backward compatibility, but no live entry uses it.
3. **`inconsistency` confirmed absent.**  A full audit of all 50 FIND-NNN entries
   confirms zero instances of `gap_type: inconsistency`.  The term was used informally
   in prose narrative (runtime-capability-crosscheck.md) but was never a registry
   field value.  The prohibition is now documented in both the enum table and the
   selection guide.

**Migration notes for tooling consumers (v1.3 update):**

- Parsers MAY reject `gap_type: contradiction` on entries added AFTER schema_version 1.3
  (new entries must use `inaccuracy + sub_qualifier: cross-doc`).
- Parsers MUST accept both `sub_qualifier` and `gap_type_qualifier` as field names
  for the sub-qualifier value (backward-compatible alias support).
- Parsers MUST emit a validation ERROR when `gap_type` is `inconsistency`.
- Parsers MUST emit a validation ERROR when `sub_qualifier` (or `gap_type_qualifier`)
  is present on an entry whose `gap_type` is not `inaccuracy`.
- The canonical field name is `sub_qualifier`; `gap_type_qualifier` is legacy.

**Entries migrated in this pass:**

| Finding | Old gap_type | New gap_type | sub_qualifier |
|---------|-------------|--------------|---------------|
| FIND-014 | `contradiction` | `inaccuracy` | `cross-doc` |
| FIND-022 | `contradiction` | `inaccuracy` | `cross-doc` |
| FIND-040 | `contradiction` | `inaccuracy` | `cross-doc` |

**Audit result:** Zero dangling `gap_type: inconsistency` references found across all
50 FIND-NNN entries, all narrative prose in this registry, all related docs
(runtime-capability-crosscheck.md uses the term in narrative but not as a field value),
and CONTRIBUTING.md severity rubric (uses "inconsistency" to describe style issues,
not as a gap_type enum value).

---

### v1.2 — 2026-03-15 (gap_type enum refactor)

**Changes:**

1. **Added `gap_type_qualifier` field** (optional; applies only to `gap_type: inaccuracy`).
   Carries sub-classification values: `cross-doc`, `stale-value`, `aspirational`.
2. **Added `inaccuracy` to `gap_type` enum.**  This is the preferred value when a doc
   makes a factual claim that is wrong or inconsistent with the source of truth.
3. **Deprecated `contradiction`.**  Existing entries using `gap_type: contradiction`
   remained valid (backward-compatible) pending the v1.3 data migration.
4. **Explicitly excluded `inconsistency`.**  `inconsistency` was never a valid
   enum value but appeared in informal usage.  Formally documented as invalid.

**Note:** The v1.2 deprecation of `contradiction` was completed by the v1.3 data
migration (Sub-AC 6c).  The three remaining `contradiction` entries that were deferred
in v1.2 have now been migrated.

### v1.1 — 2026-03-15 (severity audit)

Severity labels normalized; LOW level formally defined; 15 findings reclassified.
See frontmatter `severity_audit` field for details.

### v1.0 — 2026-03-15 (initial registry)

Canonical, deduplicated registry created from five per-topic audit report files.

---

*Registry generated 2026-03-15. Schema v1.3. Update this file when new audit
findings are produced or when open findings are resolved. Do not scatter findings
across ad-hoc finding docs — this file is the single source of truth.*
