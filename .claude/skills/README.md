# Vendored security skills

Vendored from Anthropic's [defending-code-reference-harness](https://github.com/anthropics/defending-code-reference-harness)
at commit `d3bea6b` (2026-09-15): `threat-model`, `vuln-scan`, `triage`, `_lib/checkpoint.py`.
Target analysis is static: it neither executes nor modifies target code. The
skills do write local checkpoints and reports. The Docker/ASAN execution pipeline, `patch`, `verify`,
`quickstart`, `customize` and the detection-and-response track were not vendored.

Initial edits adapted path/target defaults and are marked "Ouroboros adaptation"
in-file: `vuln-pipeline` pointers replaced with "/triage is the verification
step", default target `src/ouroboros`, threat model read from
`docs/security/THREAT_MODEL.md`, scanner output written under the harness
checkout's `docs/security/` for all targets, including external absolute paths
(never deriving the output path from the scanned target). Run from that checkout root, with
`CHECKPOINT_ROOT` unset or equal to it. Target selection grants read scope only;
it never changes the working directory or publication root. Local hardening binds checkpoint reads,
writes, append and reset to no-follow directory descriptors, and creates exclusive
random temporary files before atomic publication. It uses only the standard
library so scanning a repository does not import or execute its target package.
It fails closed on platforms without directory-descriptor/no-follow support.
Final report publication uses the same helper via `append --replace --from`.
Every Write-tool payload follows the trusted staging contract below; the helper
cannot secure a separate agent tool's write to a pre-planted path.

Harness maintainer ownership remains a Q00 decision. These local safety repairs
do not claim approval to expand the harness or change its advisory verdict role.

Ouroboros-specific inputs: `.claude/scan-extras.txt` (`/vuln-scan --extra`) and
`.claude/fp-rules.txt` (`/triage --fp-rules`). See `docs/security/README.md`.

## Trusted payload staging

In every skill example, `<payload-file>` is a placeholder for a new regular file
for that individual chunk, under a fresh operator-controlled staging directory
inside `CHECKPOINT_ROOT` (cwd by default). Its parent directories must be trusted
and unavailable for modification by the scanned target. Never choose a
repository-supplied checkpoint directory, reuse a fixed payload path, or follow
a symlink. If these staging conditions cannot be established, stop before using
Write. Substitute the chosen path in both the Write destination and the paired
`--from` argument; do not execute the placeholder literally. The helper secures
its own reads and publication, not the separate Write operation.
