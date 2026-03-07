# OpenClaw Integration

This directory contains the OpenClaw gateway integration for Ouroboros.

## Contents

- `skills/socratic-spec/` — OpenClaw skill file for running Socratic interviews via chat platforms (Telegram, WhatsApp, Discord, etc.)

## Setup

See [docs/integrations/openclaw.md](../../docs/integrations/openclaw.md) for full installation and usage instructions.

## Bridge Module

The CLI bridge lives at `src/ouroboros/integrations/openclaw_bridge.py` and is installed as part of the `ouroboros` package. Run it with:

```bash
python -m ouroboros.integrations.openclaw_bridge <command> [args]
```
