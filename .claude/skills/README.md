# Vendored security skills

Vendored from Anthropic's [defending-code-reference-harness](https://github.com/anthropics/defending-code-reference-harness)
at commit `d3bea6b` (2026-09-15): `threat-model`, `vuln-scan`, `triage`, `_lib/checkpoint.py`.
All are static and read-only; the Docker/ASAN execution pipeline, `patch`, `verify`,
`quickstart`, `customize` and the detection-and-response track were not vendored.

Edits are limited to path/target defaults and are marked "Ouroboros adaptation"
in-file: `vuln-pipeline` pointers replaced with "/triage is the verification
step", default target `src/ouroboros`, threat model read from
`docs/security/THREAT_MODEL.md`, scanner output written under `docs/security/`
(never inside the shipped package). No prompt text or verdict contract changed.

Ouroboros-specific inputs: `.claude/scan-extras.txt` (`/vuln-scan --extra`) and
`.claude/fp-rules.txt` (`/triage --fp-rules`). See `docs/security/README.md`.
