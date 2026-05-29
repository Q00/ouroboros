# Verifier Evidence Policy

This policy governs fixes to the atomic verifier and fat-harness evidence
boundary.

The core verifier must stay domain- and language-agnostic. It decides whether
typed evidence is backed by the runtime transcript. It must not grow bespoke
parsers for each test runner, language ecosystem, framework, or report format.

## Core rule

When valid work appears to be rejected, first classify the failure shape:

| Shape | Meaning | Correct direction |
| --- | --- | --- |
| No runtime transcript evidence exists for the claim | The claim may be fabricated. | Keep or use `FABRICATION_SUSPECTED`. |
| Related runtime work exists, but the evidence form cannot prove the claim | The evidence contract is mismatched. | Use `EVIDENCE_FORM_MISMATCH` with retry guidance. |
| The evidence needs runner-specific interpretation | The core verifier is the wrong layer. | Add a profile/adapter-level contract or require a runner-agnostic proof form. |

## Do not add runner-specific parsers to core

Avoid fixes that teach `parallel_executor.py` to understand one ecosystem's
result format, such as JUnit XML, pytest JUnit XML, TAP, Go test JSON, Vitest
JSON, Maven Surefire XML, or Gradle-specific report layouts.

Those fixes make one stack pass while creating an implicit obligation to support
every other stack in the same core path. They also make anti-fabrication
semantics depend on language-specific parsing details that the core verifier
cannot own consistently.

## Preferred evidence forms

For test commands whose output is filtered or paged, the preferred proof is a
runner-agnostic command contract:

```sh
set -o pipefail && <test command> 2>&1 | tail -100
```

`pipefail` preserves the failing status of the left-hand test command even when
the right-hand output filter succeeds. Without it, a filtered pipeline is a real
transcript event but not a clean command proof.

For richer result formats, prefer one of these approaches:

- Emit a typed, runner-neutral proof field from a profile or adapter that owns
  that runner.
- Keep the core verdict as `EVIDENCE_FORM_MISMATCH` and retry with clearer
  evidence.
- Promote a new cross-runner evidence contract only after it has an explicit
  design issue and acceptance criteria across multiple ecosystems.

## Failure class semantics

`FABRICATION_SUSPECTED` is reserved for claims with no supporting runtime event
or artifact reference. It should not be used when the transcript clearly shows
related work but the evidence shape is contract-incompatible.

`EVIDENCE_FORM_MISMATCH` means:

- related runtime work is visible;
- the current evidence form cannot prove the typed claim;
- retrying with a contract-compliant proof form is reasonable;
- the verifier must still reject the claim until that proof form exists.

This distinction keeps issue triage honest: the implementer did not necessarily
invent work, but the harness still cannot accept the evidence.

## Review checklist

Before accepting a verifier-evidence fix, ask:

1. Does this add language-, framework-, or runner-specific parsing to core?
2. Could the same pattern appear in another ecosystem tomorrow?
3. Is the fix preserving anti-fabrication semantics, or merely making one report
   format pass?
4. Would `EVIDENCE_FORM_MISMATCH` plus retry guidance be the correct smaller
   response?
5. If structured parsing is required, is it owned by a profile/adapter or a
   cross-runner evidence contract rather than by the core verifier?
