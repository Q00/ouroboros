# First-command activation architecture

> Generated: 2026-08-13
> Approach: install hint plus setup fallback

> Historical note: the v2 lean telemetry contract stopped collecting this
> attribution. This document is retained only as implementation history.

## Overview

The installer translates a documented `OUROBOROS_INSTALL_REF` token into a
coarse enum and stores it at `~/.ouroboros/first_command_surface` with a
restrictive umask. Telemetry reads the enum for MCP events. If no hint exists,
an existing `~/.ouroboros/config.yaml` indicates `setup_complete`; otherwise it
uses `unknown`.

## Data flow

```text
README / Getting Started
        │  OUROBOROS_INSTALL_REF
        ▼
scripts/install.sh ──► ~/.ouroboros/first_command_surface
        │                         │
        └──────────────┐          ▼
                       │   telemetry detector
                       ▼          │
                 MCP startup ─────┴──► first_command_surface
                                      │
                                      └──► MCP command_run
```

## Components

| Component | Responsibility | Location |
|---|---|---|
| Installer mapping | Convert README/guide tokens to fixed enum and persist a local hint | `scripts/install.sh` |
| Detector | Resolve explicit trusted process value, hint, setup fallback, or unknown | `src/ouroboros/telemetry.py` |
| Trust boundary | Prevent a cloned project's `.env` from overriding attribution | `src/ouroboros/config/untrusted_env.py` |
| Event contract | Allow the enum only on MCP command and server-start events | `TELEMETRY.md`, `src/ouroboros/telemetry.py` |
| Onboarding surfaces | Show the same setup → first-command sequence | README files, `docs/getting-started.md`, runtime guides |

## Privacy and failure behavior

- Values outside the four enum members are discarded as `unknown`.
- A missing or unreadable hint never blocks installation or telemetry.
- The hint contains no URL, prompt, filesystem path, machine data, or user ID.
- Direct and marketplace installs without a known documented surface remain
  `unknown` unless setup fallback is available.
- Telemetry continues to fail closed when disabled or when no durable identity
  is available.

## Measurement query contract

The next PostHog comparison should cohort on `mcp_serve_started`, order by the
same distinct ID, and group by `first_command_surface` plus OS and frontdoor.
Use the existing `./ops/posthog.sh activation` report as the baseline, then
repeat after seven full days of production exposure. Do not mix MCP-started and
non-MCP users in the denominator.
