# Security loop

Every Ouroboros advisory to date is one class: an untrusted source (the
project-directory `.env`, the cloned repository) reaches a process boundary — a
spawned executable, a config root, a loader control, the AC verify verdict —
without crossing a trust check. Two things keep that class from recurring:

1. **`tests/unit/security/test_trust_boundary_invariants.py`** (runs in the
   normal test job). AST-discovers supported, statically named process calls in
   `src/ouroboros` and fails unless it passes an env from a named builder or is
   individually allowlisted with a one-line reason. Import aliases resolve to
   their modules; builder identities include source module and class. Call
   identities include line and column, so moved calls require revalidation.
   The key catalog is reconciled against every explicit production denial in
   both directions. Literal `*_CLI_PATH` selectors discovered in Python source
   must also appear in the catalog and be denied. Prefix-family samples remain
   checked. This is a syntactic guard, not arbitrary dataflow or dynamic-key
   discovery; new selector naming schemes still need security review.
2. **`THREAT_MODEL.md`**, the scope for the vendored skills in
   `.claude/skills/` (see its README):

   ```
   /vuln-scan src/ouroboros --extra .claude/scan-extras.txt
   /triage docs/security/VULN-FINDINGS.json --repo . --fp-rules .claude/fp-rules.txt
   ```

   Scan and triage output contain exploit detail for unfixed paths; keep them
   out of public commits until the fix lands (`SECURITY.md` asks the same of
   reporters). Refresh the threat model only when a trust boundary is added.

Reports publish through the confined checkpoint helper to `docs/security/`;
legacy root TRIAGE/VULN-FINDINGS outputs are also ignored. The helper requires
POSIX no-follow directory-descriptor support and otherwise refuses writes.
Target code remains static/read-only; local checkpoints and reports are writes.
Q00 still needs to name the vendored harness maintainer. Scan verdicts remain
advisory and do not authorize execution, release, or successful completion.
