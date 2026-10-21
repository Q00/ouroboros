# Vendored security skills

Vendored from Anthropic's [defending-code-reference-harness](https://github.com/anthropics/defending-code-reference-harness)
at commit `d3bea6b` (2026-09-15): `threat-model`, `vuln-scan`, `triage`, `_lib/checkpoint.py`.
Target analysis is static: it neither executes nor modifies target code. The
skills do write local checkpoints and reports. The Docker/ASAN execution pipeline, `patch`, `verify`,
`quickstart`, `customize` and the detection-and-response track were not vendored.

Initial edits adapted path/target defaults and are marked "Ouroboros adaptation"
in-file: `vuln-pipeline` pointers replaced with "/triage is the verification
step", default target `src/ouroboros`, threat model read from
`docs/security/THREAT_MODEL.md`, scanner output written under `docs/security/`
(never inside the shipped package). Local hardening also binds checkpoint reads,
writes, append and reset to no-follow directory descriptors, and creates exclusive
random temporary files before atomic publication. It uses only the standard
library so scanning a repository does not import or execute its target package.
It fails closed on platforms without directory-descriptor/no-follow support.
Final report publication uses the same helper via `append --replace --from`.
The existing Write-tool payload staging step must use a new operator-controlled
file; the helper cannot secure a separate agent tool's write to a pre-planted path.

Harness maintainer ownership remains a Q00 decision. These local safety repairs
do not claim approval to expand the harness or change its advisory verdict role.

Ouroboros-specific inputs: `.claude/scan-extras.txt` (`/vuln-scan --extra`) and
`.claude/fp-rules.txt` (`/triage --fp-rules`). See `docs/security/README.md`.
