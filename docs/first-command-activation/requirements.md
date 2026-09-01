# First-command activation requirements

> Generated: 2026-08-13
> Status: Implemented, awaiting post-release measurement

> Historical note: this measurement program ended when v2 telemetry reduced
> collection to installs, daily activity, adoption dimensions, and failures.

## Original requirement

Reduce the PostHog drop-off between MCP server startup and the user's first
Ouroboros command. Compare the real onboarding surfaces, find error and
abandonment points, create a concrete product artifact, and leave the remaining
measurement work in the marketer backlog.

## Evidence baseline

The ordered recent-seven-day cohort is:

| Stage | Users | Conversion from prior stage |
|---|---:|---:|
| MCP server started | 482 | — |
| First command | 127 | 26.3% |
| Interview | 82 | 64.6% |
| Seed | 51 | 62.2% |
| Workflow completed | 17 | 33.3% |

The earlier 31.2% figure is discarded because its denominator included users
without an MCP start. The first-command surface was also missing for 296 of
482 users, so the product needs a privacy-safe, fixed-enum attribution field.

## Clarified specification

1. Persist only a fixed onboarding enum from documented installer surfaces:
   `readme_quickstart`, `getting_started`, `setup_complete`, or `unknown`.
2. Include the enum on MCP startup and MCP command events so the activation
   cohort can be grouped without recording URLs, prompts, paths, or identity.
3. Preserve an install hint after setup; otherwise a README user would be
   relabeled as `setup_complete` before the first command.
4. Make the first post-install action explicit in the English, Korean, and
   Simplified Chinese README, Getting Started, Codex, and OpenCode surfaces.
5. Keep current telemetry allowlists and project `.env` trust boundaries
   intact.

## Success criteria

- `TELEMETRY.md` and executable allowlists describe the same exact properties.
- Tests cover enum validation, invalid/missing hints, setup fallback, and hint
  preservation after setup.
- A new user can copy a setup command followed immediately by a first workflow
  command for Claude Code, Codex, and OpenCode.
- The marketer backlog retains a dated DoD for a before/after seven-day
  PostHog comparison; implementation alone does not claim conversion lift.
