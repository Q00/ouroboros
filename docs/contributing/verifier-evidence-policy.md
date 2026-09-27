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

For test commands whose output is filtered or paged, the preferred form is
the plain pipeline, which replay supports:

```sh
<test command> 2>&1 | tail -100
```

Replay runs `<test command>` alone and judges its own exit status, so the
filter cannot mask a failure. A `set -o pipefail && ...` preamble is not
replayable (`set` is a shell builtin), so claims resting on it keep the
transcript-only rules. Those rules recognize `set -o pipefail` alone or with
`-e`/`-u` beside it (`set -euo pipefail; ...`, `set -e -o pipefail && ...`);
a later `set` that names `pipefail` without enabling it (`set +o pipefail`)
removes the protection again.

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
the default) and the leaf held Bash authority.

Every decision about a command reads one executed-command analysis
(`orchestrator/evidence/shell_parsing.py`): the simple commands of a shell
line, the runtime shell wrappers around it, and for each simple command the
chain of wrappers and launchers in front of the program that finally runs
(`program_chain`), with the assignments the chain makes
(`command_line_assignments`) and the program text of a `python -c` call
(`python_inline_program`). The allowlist, the denylist, test-target linkage
and inline-import anchoring consume that analysis; none of them reads the
command text a second way. The rules:

- **Source.** Candidates are commands from the transcript's structured Bash
  calls, never text from a claim. Runtime shell wrappers (`/bin/zsh -lc '...'`)
  are peeled. Commands linked to an unproven claim come first, then recognized
  test runs that the runner-output rules judge (for example a
  `pytest tests/test_a.py` run behind a `tests/test_a.py::test_x` claim). At
  most 3 per criterion. The most recent run of a command decides: every
  record of that run counts (the call and each correlated completion), and a
  failure in any of them (a non-zero or non-integer exit code, `is_error`, a
  `failed` or `error` status or subtype, a `.failed` runtime event) or
  records that disagree mean it is not replayed.
- **Allowlist.** Only these programs are replayed, found after peeling
  wrappers (`timeout`, `stdbuf`, `time`, `nice`, `ionice`, `env`, `nohup`,
  `command`, `exec`, `setsid`) with per-wrapper option tables; an option or
  operand the tables do not know leaves the program unknown, and nothing is
  replayed:
  - `python`/`python3`/`pythonX.Y` with `-m pytest`, `-m unittest`, `-m tox`,
    `-m nox`, `-m django test`, or a script inside the workspace (never `-c`,
    `-` or stdin);
  - `pytest`, `py.test`, `tox`, `nox`, `django-admin test`, `rspec`, `jest`,
    `vitest`, `mocha`, `ava`, `phpunit`, `ctest`;
  - `make`/`gmake`, except targets whose words include `install`, `deploy`,
    `publish`, `release`, `upload` or `push`, and except `-C`, `-f` and dry
    runs;
  - `npm`/`pnpm`/`yarn`/`bun` `test`, or `run <script>` with the same
    exceptions for the script name; `bun test`;
  - `go test|vet|build`, `cargo test|check|build|clippy|nextest`,
    `mvn`/`mvnw` with a `test` or `verify` goal, `gradle`/`gradlew` with a
    `test`, `check` or `build` task (not `install`/`publish`/`deploy` tasks,
    not with tests skipped), `dotnet test|build`, `deno test`,
    `swift test|build`, `mix test`;
  - any of these reached through `uv run`, `uvx`, `poetry run`, `pipenv run`,
    `pdm run`, `bundle exec`, `npx` or `bunx`;
  - a script inside the workspace, run directly (`./run_tests.sh`,
    `bin/test`) or by `python`, `sh` or `bash`.

  Everything else is refused: file viewers and text utilities (`cat`, `sed`,
  `head`, `tail`, `less`, `grep`, `rg`, `awk`, `wc`, `ls`, `find`, `stat`,
  `file`, `diff`, `git`, ...), package managers, `xargs`, an absolute-path
  program or argument outside the workspace, an environment assignment in
  the command (leading, or consumed by an `env` wrapper such as
  `timeout 60 env PATH=/tmp/x pytest`) naming an absolute path outside the
  workspace, `uv run --env-file`, and a runner in a mode that runs no tests
  (`--help`, `--collect-only`, `make -n`, ...). A workspace file named like an
  interpreter or a test runner (`./python`, `./pytest`) is that program only
  inside an environment's `bin` directory (`.venv/bin/pytest`,
  `node_modules/.bin/jest`); elsewhere an interpreter name is refused and a
  runner name is replayed as a plain script, whose operands are never test
  targets. Any other file in an environment's `bin` directory
  (`.venv/bin/pip`, `node_modules/.bin/tsc`) is an installed program, not a
  project script, and is refused. One exception: an absolute-path program
  whose name is an allowlisted interpreter or runner (`python3.9`, `pytest`, `make`, ...) is
  admitted when its real path, symlinks resolved, lies inside a known
  environment root: `sys.prefix` or `sys.base_prefix` of the verifying
  process, `VIRTUAL_ENV`, `CONDA_PREFIX`, or a directory on the replay
  environment's `PATH` (for example `/opt/miniconda3/envs/testbed/bin/python`
  in a SWE-bench image). The denylist below is a second layer.
- **Isolation.** Each command runs as a direct argv (no shell) in a fresh copy
  of the workspace, under the execution sandbox (`runtime/exec_sandbox.py`,
  below), with `execution.verify_command_timeout_seconds`. Its environment is
  built from scratch: `REPLAY_ENV_PASSTHROUGH` (`PATH`, the locale, `TZ`,
  Python I/O settings, `HOME`, `USER`, `LOGNAME`, and the variables that
  locate an installed toolchain: `VIRTUAL_ENV`, `CONDA_PREFIX`,
  `CONDA_DEFAULT_ENV`, `JAVA_HOME`, `GOPATH`, `GOROOT`, `GOMODCACHE`,
  `CARGO_HOME`, `RUSTUP_HOME`) is copied from the verify gate's scrubbed
  environment; `TMPDIR`, `TMP` and `TEMP` point to a per-run temp directory;
  `PYTHONDONTWRITEBYTECODE=1` is set. Nothing else is inherited. The
  narrowing variables (`NARROWING_ENVIRONMENT` and the `JEST_*` and
  `VITEST_*` families; see "Narrowing" below) are never copied, and each
  replayed run records the ones the inherited environment held in
  `scrubbed_environment`. A replay that needed an uncopied variable (a
  `manage.py test` relying on `DJANGO_SETTINGS_MODULE`, a Go build relying on
  `GOFLAGS=-mod=vendor`) can fail; that fails closed.
  Assignments the command itself makes are kept
  (recorded in `env_delta`); they disable target linkage instead (below).
  `.git` and caches are not copied; `.venv`, `venv`, `node_modules`, `.tox`
  and `.nox` are linked, not copied. A workspace over 50,000 files or 1 GiB is
  not replayed. Absolute workspace paths in the command point at the copy.
- **Sandbox.** The command can write only beneath the copy and its per-run
  temp directory (plus `/dev/null` and a few other character devices); the
  live workspace, `HOME`, the system temp directory and every other path are
  read-only to it. Reading and executing are not restricted. On macOS this is
  `sandbox-exec` with a generated profile that denies `file-write*` outside
  those roots; on Linux it is Landlock (ABI 3, Linux 6.2, or newer; below
  it truncation cannot be denied), applied in the child before it execs the
  command (unprivileged, no mount or user namespace, so it works in
  containers), plus a seccomp filter for what Landlock does not mediate:
  changing metadata (the chmod, chown, utime and xattr syscall families, the
  inode-flag ioctls, io_uring). The filter cannot see paths, so on Linux a
  replay cannot change metadata inside its copy either (`touch` on an
  existing file, `shutil.copy2`, cargo's fingerprint timestamps); such a
  replay fails, which fails closed. The launchers start with a fixed bootstrap environment; the
  command's environment, including its own assignments such as
  `LD_PRELOAD=...`, takes effect only when the command itself is exec'd
  inside the sandbox. The sandbox policy is sealed in the run's
  execution-semantics contract, so a resume with the switch changed is
  refused rather than replaying under a different policy. Each backend is
  probed once per process with a mutation matrix (create, append, truncate,
  unlink, rename in and out, mkdir, rmdir, symlink, hard link, chmod, fchmod
  on a read-only descriptor, chown, utime, and where the host supports them
  xattrs, file flags and inode-flag ioctls): every class the probe can
  perform unconfined must be denied outside the root when confined, with the
  outside left unchanged, or the backend is reported unavailable. Reading
  stays allowed, so the access time the kernel records for a permitted read
  can change (under `sandbox-exec` too); only a `noatime` mount stops that.
  The replayed command gets `/dev/null` as stdin, never the controller's
  own (under an MCP host, its JSON-RPC stream); the process runner does
  this for verify commands as well. Where no backend works
  (Windows, a kernel without Landlock ABI 3, an already-sandboxed macOS
  process),
  nothing is replayed, the claims keep the transcript-only rules, and the
  observation records `replay_skipped: sandbox_unavailable`. Under Landlock
  the command also cannot read `/proc/<pid>/environ`, `mem` or `maps` of the
  controller or any other process outside its domain (Landlock denies
  ptrace-mode access across the domain boundary). `sandbox-exec` cannot deny
  the macOS equivalent (`KERN_PROCARGS2`), so on macOS a replayed command can
  read the environment of the user's other processes; the replayed command
  is one the worker already ran with the same access. Effects the command
  asks another, unconfined process to perform over IPC (a user service
  manager, a desktop automation service, a container daemon) are outside
  this boundary.
- **Network.** Network access must be denied: in the `sandbox-exec` profile
  on macOS (loopback allowed), by an unprivileged network namespace on Linux
  (with its loopback interface brought up, so loopback stays available),
  or not at all when the Linux process already has only a loopback interface
  (a container started with `--network none`). Unix-domain sockets stay
  available. Where none of these works, nothing is replayed and the
  observation records `replay_skipped: network_isolation_unavailable`.
- **Live paths.** The copy reaches live paths through its links: the linked
  dependency trees and the targets of copied symlinks. They are outside the
  writable roots, so the sandbox denies writes to them. With the sandbox
  switched off (`execution.exec_sandbox: false` or `OUROBOROS_EXEC_SANDBOX=off`,
  unsafe) nothing is confined; replay then fingerprints their metadata (type,
  size, mtime and ctime of every entry) before and after the run, any change
  marks it `mutated`, a tree over 250,000 entries is not replayed, and the
  observation says the network was not isolated.
- **Success.** Exit 0, no timeout, the transcript's recorded exit (when it
  recorded one) equal to the replay's, and no change to or deletion of a
  pre-existing file (SHA-256 of every file in the copy before and after,
  outside build outputs, caches and dependency directories) or of a live
  linked path. Anything else leaves the claim unsupported.
- **Linkage.** A claim is corroborated by a successful replay only when the
  whitespace-normalized claim:
  - equals the transcript command or its replayed core;
  - equals one of them plus one trailing parenthetical annotation
    (`make test (12 passed)`); or
  - minus a trailing `(N tests)` count, is one test target that is a
    positional operand (not an option value) of a test runner that executes
    it (pytest, unittest, Django-style runners, `bin/test`, jest, vitest,
    mocha, rspec, `go test`, phpunit), and nothing in the command narrows
    what the runner collects or selects (see "Narrowing" below). For example
    `migrations (578 tests)` is linked to `python tests/runtests.py
    migrations`, and `tests/test_x.py` is not linked to `sed -n 1,40p
    tests/test_x.py` or to `pytest --ignore tests/test_x.py`.

  A claim that merely contains a command (`make` inside `make test`) is not
  linked to it. The runner-output rules apply to a replayed run only under the
  same conditions: nothing narrows it, and a claimed file named in the
  command must be one of its executed operands.
- **Narrowing.** Any of these in a command disables the target rule and the
  runner-output rules for it, on replay and on the transcript-only path.
  Where a runner's semantics are unclear, the option is treated as narrowing.
  - an option that excludes or selects tests, in any spelling (`--opt value`,
    `--opt=value`, `-kvalue`, or inside a short-option cluster such as
    `-qk expr` for pytest, unittest, Django and project runner scripts): pytest `--deselect`, `--ignore`,
    `--ignore-glob`, `-k`, `-m`; unittest `-k`; Django and project runner
    scripts `-k`, `--tag`, `--exclude-tag`, `--start-at`, `--start-after`;
    and `--exclude*`, `--skip`, `--filter`, `--grep`, `-t`, `-g`, `-e`,
    `-run`, `-skip` for the other runners;
  - pytest `-o`/`--override-ini` setting `addopts`, `python_files`,
    `python_classes`, `python_functions`, `testpaths` or `norecursedirs`
    (`addopts` covers a `--deselect`, `-k` or `-m` inside it); other keys,
    such as `cache_dir`, do not narrow;
  - pytest `-c`/`--config-file`, `--rootdir` and `--confcutdir`, whatever
    they name: whether it is the file or directory pytest would pick by
    default cannot be decided from the command;
  - pytest `-p` loading or disabling a plugin, except the no-op
    `no:cacheprovider`; `-p`/`--pattern` for unittest, Django and project
    runner scripts (a discovery pattern);
  - an assignment on the command line of a variable that changes what a
    runner collects or loads or where the code under test is imported from:
    `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`,
    `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONSAFEPATH`,
    `DJANGO_SETTINGS_MODULE`, `NODE_OPTIONS`, `NODE_PATH`, `RUBYOPT`,
    `RUBYLIB`, `BUNDLE_GEMFILE`, `GOFLAGS`, `CGO_ENABLED`, and any `JEST_*` or
    `VITEST_*` variable. A leading assignment, one consumed by an `env`
    wrapper, and one anywhere in the transcript command's text
    (`export PYTHONPATH=stubs && ...`) all count; on the transcript-only
    path, so does an export of one in an earlier Bash call of the same leaf
    (a runtime's shell may keep it);
  - the Python interpreter flags `-P` and `-I` (they change `sys.path`);
  - a runner's configuration, module-resolution or selection options:
    Django `runtests.py`, `manage.py test` and `django-admin test`
    `--settings`, `--pythonpath` and `--testrunner`; jest and
    vitest `-c`/`--config`, `-t`/`--testNamePattern`,
    `--testPathIgnorePatterns`, `--testPathPattern`, `--selectProjects`,
    `--shard`, `-o`/`--onlyChanged`, `--changedSince`, `--passWithNoTests`,
    `--root`/`--rootDir`, module-mapping and setup-file options (camelCase and
    kebab-case alike); mocha `--config`, `--grep`/`-g`, `--fgrep`,
    `--invert`, `--ignore`/`--exclude`, `--file`, `-r`/`--require`; phpunit
    `-c`/`--configuration`, `--filter`, `--group`, `--exclude-group`,
    `--testsuite`, `--bootstrap`; rspec `-O`/`--options`, `-e`/`--example`,
    `-t`/`--tag`, `--pattern`, `--exclude-pattern`, `-I`, `-r`/`--require`;
    `go test` `-run`, `-skip`, `-tags`, `-short`, `-list`, `-exec`, `-mod`,
    `-modfile`, `-overlay`; `cargo test` and `cargo nextest` with a
    positional filter (before or after `--`), `--skip`, `--exact`,
    `--features`, `--no-default-features`, `--lib`, `--bins`, `--tests`,
    `--test`, `-p`/`--package`, or any option outside a short list of
    harmless ones (`--release`, `-j`, `--workspace`, `--nocapture`, ...);
    Maven `-Dtest=` (and `-Dit.test`, `-Dgroups`, `surefire.*`,
    `failsafe.*`), `-P`, `-pl`, `-s`, `-f`; Gradle `--tests`, `-P`, `-x`,
    `-b`, `-c`, `-I`, `-p`.

  An option narrows only when it changes which tests run, or how modules and
  settings resolve, relative to the runner's documented default. A process
  count never narrows: Django `--parallel N` (any `N`, `auto` included),
  pytest-xdist `-n`, `cargo test -j`. An option set to its documented default
  does not narrow either. The one such default recognized is Django's
  `tests/runtests.py --settings test_sqlite` (also `--settings=test_sqlite`,
  and the script reached as `./tests/runtests.py`, `python tests/runtests.py`
  or `cd tests && python runtests.py`): `runtests.py` runs
  `os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_sqlite")` when no
  `--settings` is given, and its `--settings` help says "either the
  DJANGO_SETTINGS_MODULE environment variable or "test_sqlite" will be used"
  ([django/tests/runtests.py](https://github.com/django/django/blob/main/tests/runtests.py)).
  A command-line `DJANGO_SETTINGS_MODULE` narrows on its own and replay
  scrubs an inherited one, so the default holds on replay. So the SWE-bench
  form `./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1
  <labels>` links its labels. Any other settings value, `manage.py test
  --settings=test_sqlite` (whose default is the project's own settings), and
  every other runner's configuration options stay narrowing: no other option
  value is known to equal its runner's default.

  Two classes are told apart. Selection options (`-k`, `--tests`, `-Dtest=`,
  `-run`, `-t`, `--filter`, ...) choose which tests run; on the
  transcript-only path, output that names the whole claim still backs it
  (`gradle test --tests Foo` with `Foo > t PASSED`). Configuration (the
  variables above, `-P`/`-I`, config files, settings, module resolution,
  plugin loading, `go test -list`) can make a named test pass against other
  code, so no output backs a claim on such a run.

  An option the tables do not know never takes an option-like token as its
  value, so `pytest --x --ignore tests/t.py tests/t.py` is still narrowed.

  Scope: configuration the workspace itself carries (a `pytest.ini` with
  `addopts = --deselect ...`, a `conftest.py` that deselects or skips, a
  `jest.config.js`, Django's default settings module) is part of the work
  under review, not of the command, so it does not disable linkage. Detecting
  it would need the runner's own report of deselected or skipped tests; the
  legacy verifier does not detect such test tampering either.
- **Output filters.** `CMD | tail ...`, `CMD 2>&1 | grep ...` and chains of
  output filters (`tail`, `head`, `grep`, `egrep`, `fgrep`, `sed`, `cat`,
  `cut`, `sort`, `uniq`, `wc`, `tr`) replay `CMD` alone and use `CMD`'s own
  exit status. The filters are never run. Any other shell construct (`||`,
  `;`, `&&` other than a leading `cd <relative-dir> &&`, redirection to a
  file, substitution, `tee`, a pipe into a program) is not replayed.
- **Denylist.** Commands that escalate privilege, reach the network or other
  hosts, drive containers, delete files, write to version control, or install
  packages (`sudo`, `ssh`, `curl`, `wget`, `docker`, `rm`, `git` other than
  read-only subcommands, `pip install`, `npm install`, `uv add`, `brew`,
  `twine`, `gh`, ...) and inline shell programs (`bash -c`) are never
  replayed, whatever the allowlist says. The denylist judges the same
  resolution the allowlist admitted (`shell_parsing.program_chain`): every
  launcher in the command and the program it launches, so
  `uv run .venv/bin/pip install x` is judged as `pip install`.

A claim that no successful replay backs falls through to the transcript-only
rules and failure classes below. Two of those rules follow the same
principle: a claimed test file backed by a transcript run links only as an
executed operand of a runner that nothing narrows (not
`pytest --ignore tests/x.py`), a bare-word claim (`test_add`) links only as a
whole word of the command (not inside `tests/test_address.py`), the
functional tier does not accept a
recorded exit that belongs to a pipeline without `pipefail`
(`./run_tests.sh | tail -5`), since it is the last stage's status, and an
inline Python program (`python3 -c "from mathutils import clamp; ..."`)
anchors a workspace module only through the import that is certain to run:
the first module of the parsed `-c` program's first statement, when that
statement is an `import` or an absolute `from ... import`. Text that mentions
an import (`python -c "print('import app')"`) and a later import that may
never run (`raise SystemExit(0); import app`) anchor nothing, and so does a
`python -c` whose success the line's zero exit does not imply
(`python3 -c "import app"; true`, `... || true`, a pipeline stage before the
last), one whose import path is not the default (the same configuration
decision as for runners: `-P`, `-I`, or a narrowing variable such as
`PYTHONPATH` assigned in the command or exported by an earlier call), and one
run after a change of directory other than a single leading
`cd <workspace-relative dir> &&`, whose directory then prefixes the module
path.

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
