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

## Replay-first corroboration

Replay is the runner-agnostic proof the core verifier uses when the transcript
alone cannot prove a `tests_passed` or `commands_run` claim. It needs no
knowledge of the runner: the harness re-runs a command the leaf ran and judges
only the exit status.

It runs only when command verification is on (`execution.run_verify_commands`,
the default) and the leaf held Bash authority. The rules:

- **Source.** Candidates are commands from the transcript's structured Bash
  calls, never text from a claim. Runtime shell wrappers (`/bin/zsh -lc '...'`)
  are peeled. Commands linked to an unproven claim come first, then recognized
  test runs that the runner-output rules judge (for example a
  `pytest tests/test_a.py` run behind a `tests/test_a.py::test_x` claim). At
  most 3 per criterion.
- **Isolation.** Each command runs as a direct argv (no shell) in a fresh copy
  of the workspace, with the verify gate's scrubbed environment and
  `execution.verify_command_timeout_seconds`. `.git` and caches are not copied;
  `.venv`, `venv`, `node_modules`, `.tox` and `.nox` are linked, not copied. A
  workspace over 50,000 files or 1 GiB is not replayed. Absolute workspace
  paths in the command point at the copy. Network access is denied with
  `sandbox-exec` on macOS (loopback allowed) or an unprivileged network
  namespace on Linux; where neither works the run is recorded as not
  network-isolated (`network_isolated=False`, and the transcript note says
  "network not isolated").
- **Success.** Exit 0, no timeout, and no change to or deletion of a
  pre-existing file (SHA-256 of every file before and after, outside build
  outputs, caches and dependency directories). A non-zero exit, a timeout, or
  a mutation leaves the claim unsupported.
- **Linkage.** A claim is corroborated by a successful replay when the claim
  equals or contains the command (whitespace-normalized, at word boundaries),
  or when the claim, minus a trailing `(N tests)` count, is one test target
  that appears verbatim as an argument of the command. For example
  `migrations (578 tests)` is linked to `python tests/runtests.py migrations`.
  Nothing looser is matched.
- **Output filters.** `CMD | tail ...`, `CMD 2>&1 | grep ...` and chains of
  pure output filters (`tail`, `head`, `grep`, `egrep`, `fgrep`, `sed`, `cat`,
  `cut`, `sort`, `uniq`, `wc`, `tr`) replay `CMD` alone and use `CMD`'s own
  exit status. Any other shell construct (`||`, `;`, `&&` other than a leading
  `cd <relative-dir> &&`, redirection to a file, substitution, `tee`, a pipe
  into a program) is not replayed.
- **Denylist.** Commands that escalate privilege, reach the network or other
  hosts, drive containers, delete files, write to version control, or install
  packages (`sudo`, `ssh`, `curl`, `wget`, `docker`, `rm`, `git` other than
  read-only subcommands, `pip install`, `npm install`, `uv add`, `brew`, ...)
  and inline shell programs (`bash -c`) are never replayed. Their claims keep
  the transcript-only rules.

A claim that no successful replay backs falls through to the transcript-only
rules and failure classes below, unchanged.

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
