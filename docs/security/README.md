# Security loop

Every Ouroboros advisory to date is one class: an untrusted source (the
project-directory `.env`, the cloned repository) reaches a process boundary — a
spawned executable, a config root, a loader control, the AC verify verdict —
without crossing a trust check. Two things keep that class from recurring:

1. **`tests/unit/security/test_trust_boundary_invariants.py`** (runs in the
   normal test job). AST-discovers every child-process spawn site in
   `src/ouroboros` and fails unless it passes an env from a named builder or is
   allowlisted with a one-line reason; and asserts a catalog of loader /
   executable-selector keys is denied by `config/untrusted_env.py`. Cases
   marked `xfail(strict=True)` are known gaps: they fail loudly once fixed and
   must then be moved to the denied roster.
2. **`THREAT_MODEL.md`**, the scope for the vendored skills in
   `.claude/skills/` (see its README):

   ```
   /vuln-scan src/ouroboros --extra .claude/scan-extras.txt
   /triage docs/security/VULN-FINDINGS.json --repo . --fp-rules .claude/fp-rules.txt
   ```

   Scan and triage output contain exploit detail for unfixed paths; keep them
   out of public commits until the fix lands (`SECURITY.md` asks the same of
   reporters). Refresh the threat model only when a trust boundary is added.
