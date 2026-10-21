"""Which transcript commands the harness may replay, and what each one runs.

Replay executes commands the leaf ran, so the default is to refuse. The
program a command really runs is found by peeling known wrappers (``timeout``,
``env``, ``nice``, ...) and launchers (``uv run``, ``npx``, ``bundle exec``,
...) with per-program option tables; when an option or operand cannot be
classified with certainty, the program is unknown and nothing is replayed.

A command is replayed only when that program is one of:

(a) a test or build runner or an interpreter on the allowlist below, with a
    subcommand, target or script name that does not install, deploy or
    publish:

    - ``python``/``python3``/``pythonX.Y`` with ``-m pytest``, ``-m unittest``,
      ``-m tox``, ``-m nox``, ``-m django test``, or a script inside the
      workspace (never ``-c``, ``-`` or stdin);
    - ``pytest``, ``py.test``, ``tox``, ``nox``, ``django-admin test``;
    - ``make``/``gmake`` (targets not ``install``, ``uninstall``, ``deploy``,
      ``publish``, ``release``, ``upload`` or ``push``; no ``-C``/``-f``, no
      dry run);
    - ``npm``/``pnpm``/``yarn``/``bun`` ``test`` or ``run <script>`` (not an
      install, publish or deploy script); ``bun test``;
    - ``go test|vet|build``, ``cargo test|check|build|clippy|nextest``,
      ``mvn``/``mvnw`` with a ``test`` or ``verify`` goal, ``gradle``/
      ``gradlew`` with a ``test``, ``check`` or ``build`` task,
      ``dotnet test|build``, ``deno test``, ``swift test|build``, ``mix test``;
    - ``rspec``, ``jest``, ``vitest``, ``mocha``, ``ava``, ``phpunit``,
      ``ctest``;
    - the same runners reached through ``uv run``, ``uvx``, ``poetry run``,
      ``pipenv run``, ``pdm run``, ``bundle exec``, ``npx`` or ``bunx``;

(b) a script inside the workspace, run directly (``./run_tests.sh``,
    ``bin/test``) or by ``python``/``sh``/``bash`` (``tests/runtests.py``).
    A file in an environment's ``bin`` directory (``.venv/bin/pip``,
    ``node_modules/.bin/tsc``) is an installed program, not such a script: it
    is admitted only as a runner of (a).

An absolute-path program outside the workspace is admitted only when its
name is an allowlisted interpreter or runner (``python3.9``, ``pytest``,
``make``, ...) and its resolved real path lies inside a known environment
root (``environment_roots``: the verifying interpreter's ``sys.prefix`` and
``sys.base_prefix``, ``VIRTUAL_ENV``, ``CONDA_PREFIX``, and the directories
on the replay environment's ``PATH``), as with
``/opt/miniconda3/envs/testbed/bin/python -m pytest`` in a SWE-bench image.

Refused: every other program, including the file viewers and text utilities
in ``VIEWER_PROGRAMS``, version control, package managers, any other
absolute-path program outside the workspace, an absolute-path argument
outside the workspace (other than such an environment program), ``xargs``
(its argv comes from stdin), and a runner in a mode that runs no tests
(``--help``, ``--collect-only``, ``make -n``, ...).

The denylist (``replay_denied``: privilege, network, containers, deletion,
version-control writes, package installs, inline shell programs) is a second
layer. ``authorize_replay`` applies it to every program of the chain the
allowlist admitted (``shell_parsing.program_chain``: each launcher and the
program it launches), never to a different reading of the command.

The same resolution decides the test-target linkage rule
(``claim_target_operands``): a claim naming a test file or label is linked to
a command only when it is a positional operand (not an option value) of a
test runner that executes it, and the command neither excludes or narrows the
tests it runs nor changes what the runner collects, loads or imports through
configuration. Each of these disables the target rule and the runner-output
rules (``run_may_back_test_claim``); where a runner's semantics are unclear,
an option is treated as narrowing:

- an option that excludes or selects tests: ``--deselect``, ``--ignore``,
  ``-k`` and ``-m`` (pytest), ``-k`` (unittest, Django, project runner
  scripts), ``--tag``, ``--exclude-tag`` and ``--start-at``/``--start-after``
  (Django), and the entries of ``_TARGET_EXCLUDING_OPTIONS`` and
  ``_SHORT_EXCLUDING_OPTIONS``, also inside an argparse short-option cluster
  (``-qk expr`` is ``-q -k expr``; ``_expand_short_clusters``);
- pytest ``-o``/``--override-ini`` setting ``addopts``, ``python_files``,
  ``python_classes``, ``python_functions``, ``testpaths`` or
  ``norecursedirs`` (``addopts`` covers ``--deselect``, ``-k`` and ``-m``
  inside it);
- pytest ``-c``/``--config-file``, ``--rootdir`` and ``--confcutdir``, in any
  form: whether a named file or directory is the one pytest would use by
  default cannot be decided from the command, so any use counts;
- pytest ``-p`` unless the plugin is in ``NO_OP_PYTEST_PLUGINS``
  (``no:cacheprovider``); ``-p``/``--pattern`` for unittest, Django and
  project runner scripts, where it is a discovery pattern;
- an assignment on the command line (leading, or consumed by an ``env``
  wrapper) of a variable in ``NARROWING_ENVIRONMENT`` (the pytest variables,
  ``PYTHONPATH``, ``PYTHONHOME``, ``PYTHONSTARTUP``, ``PYTHONSAFEPATH``,
  ``DJANGO_SETTINGS_MODULE``, ``NODE_OPTIONS``, ``NODE_PATH``, ``RUBYOPT``,
  ``RUBYLIB``, ``BUNDLE_GEMFILE``, ``GOFLAGS``, ``CGO_ENABLED``) or with a
  prefix in ``NARROWING_ENVIRONMENT_PREFIXES`` (``JEST_``, ``VITEST_``),
  whatever the runner (``narrowing_variable``);
- the Python interpreter flags ``-P`` and ``-I``;
- an entry of ``_RUNNER_CONFIG_OPTIONS`` for the runner: Django ``--settings``
  (except ``runtests.py``'s documented default ``test_sqlite``,
  ``_DEFAULT_OPTION_VALUES``), ``--pythonpath`` and ``--testrunner``; jest and vitest
  configuration, selection, sharding and module-mapping options; mocha,
  phpunit and rspec configuration, filter, group and load-path options;
  ``go test`` ``-run``, ``-skip``, ``-tags``, ``-short``, ``-list``,
  ``-exec``, ``-mod``, ``-overlay``; Maven ``-P``, ``-pl``, ``-s``, ``-f``
  and test-selecting ``-D`` properties (``-Dtest=``); Gradle ``--tests``,
  ``-P``, ``-x`` and build-file options; ``cargo test`` and ``cargo nextest``
  target and feature options, and any positional filter or option outside
  ``_CARGO_FLAG_OPTIONS`` and ``_CARGO_VALUE_OPTIONS``.

An option narrows only when it changes which tests run or how modules and
settings resolve relative to the runner's documented default: a process
count (Django ``--parallel N``, pytest-xdist ``-n``, ``cargo -j``) never
does, and an option set to a documented default does not either.

Narrowing has two classes. ``SELECTION`` options only choose which tests run
(``-k``, ``--tests``, ``-Dtest=``, ``-run``, ``_RUNNER_SELECTION_OPTIONS``);
``CONFIGURATION`` (every other entry above: the environment, interpreter
flags, configuration files, settings, module resolution, plugin loading)
can make a named test pass against code other than the workspace's, so the
transcript-only rules do not accept even output that names the claimed test
(``alters_configuration``).

Replay also removes every narrowing variable from the environment the replay
inherits (``command_replay``). ``uv run --env-file`` is refused, since the
file may set those variables. Configuration the workspace itself carries
(``pytest.ini`` ``addopts``, ``conftest.py``, ``jest.config.js``) is part of
the work under review and out of scope here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import os
from pathlib import PurePosixPath
import re
import sys

from ouroboros.orchestrator.evidence.shell_parsing import (
    _PYTEST_NON_EXECUTING_OPTIONS,
    _PYTHON_FLAGS,
    _PYTHON_VALUE_OPTIONS,
    REFUSED_WRAPPERS,
    _has_gradle_or_maven_test_skip,
    _is_python_executable,
    _program_name,
    _project_test_runner_script,
    command_line_assignments,
    program_chain,
    python_inline_program,
)

# Narrowing classes (``_selection``). ``SELECTION``: an option selects, skips
# or excludes tests by name, path or group. ``CONFIGURATION``: the runner's
# configuration, module resolution or environment is replaced.
SELECTION = "selection"
CONFIGURATION = "configuration"

# File viewers and text utilities: they read or list files, they never
# execute a test. Never replayed, and never a test-target runner.
VIEWER_PROGRAMS = frozenset(
    {
        "cat",
        "bat",
        "tac",
        "nl",
        "sed",
        "head",
        "tail",
        "less",
        "more",
        "most",
        "view",
        "vi",
        "vim",
        "nvim",
        "nano",
        "emacs",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "awk",
        "gawk",
        "mawk",
        "wc",
        "ls",
        "tree",
        "find",
        "fd",
        "stat",
        "file",
        "du",
        "diff",
        "cmp",
        "comm",
        "git",
        "hg",
        "svn",
        "sort",
        "uniq",
        "cut",
        "tr",
        "paste",
        "column",
        "od",
        "xxd",
        "hexdump",
        "strings",
        "jq",
        "yq",
        "echo",
        "printf",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "pwd",
        "which",
        "type",
        "true",
        "false",
        "test",
    }
)

# Programs that are never replayed, whatever their arguments: privilege,
# remote access and transfer, containers, deletion, process control, system
# package managers, and programs that open other applications.
_DENIED_PROGRAMS = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "curl",
        "wget",
        "nc",
        "ncat",
        "telnet",
        "ftp",
        "docker",
        "docker-compose",
        "podman",
        "kubectl",
        "rm",
        "rmdir",
        "dd",
        "shred",
        "kill",
        "pkill",
        "killall",
        "shutdown",
        "reboot",
        "launchctl",
        "systemctl",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "apk",
        "pacman",
        "port",
        "open",
        "xdg-open",
        "osascript",
        "busybox",
        "truncate",
        "chmod",
        "chown",
        "chgrp",
        "twine",
        "gh",
    }
)
# Language package managers: the subcommands that install, remove or publish.
_DENIED_SUBCOMMANDS: Mapping[str, frozenset[str]] = {
    "pip": frozenset({"install", "uninstall", "download", "wheel"}),
    "pip3": frozenset({"install", "uninstall", "download", "wheel"}),
    "pipx": frozenset({"install", "uninstall", "inject", "upgrade", "reinstall", "run"}),
    "uv": frozenset({"pip", "add", "remove", "sync", "lock", "tool", "python", "publish"}),
    "poetry": frozenset({"add", "install", "remove", "update", "lock", "publish"}),
    "npm": frozenset(
        {"install", "i", "ci", "add", "uninstall", "remove", "rm", "update", "publish", "link"}
    ),
    "yarn": frozenset({"add", "install", "remove", "upgrade", "publish", "link"}),
    "pnpm": frozenset({"add", "install", "i", "remove", "rm", "update", "publish", "link"}),
    "cargo": frozenset({"install", "uninstall", "publish"}),
    "go": frozenset({"install", "get"}),
    "gem": frozenset({"install", "uninstall", "update"}),
    "bundle": frozenset({"install", "update", "add"}),
    "composer": frozenset({"install", "require", "update", "remove"}),
    "conda": frozenset({"install", "create", "remove", "update"}),
    "mamba": frozenset({"install", "create", "remove", "update"}),
    "pipenv": frozenset({"install", "uninstall", "update", "sync", "lock", "upgrade"}),
    "pdm": frozenset({"add", "install", "remove", "update", "sync", "lock", "publish"}),
    "bun": frozenset({"install", "i", "add", "remove", "rm", "update", "publish", "link"}),
}
_VERSIONED_PIP_RE = re.compile(r"pip\d+(?:\.\d+)*")
# Package managers whose bare invocation installs.
_BARE_INSTALLERS = frozenset({"yarn", "bundle", "composer"})
# Git is replayed only for subcommands that read.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "ls-files", "rev-parse", "grep", "blame", "describe"}
)
_DENIED_PYTHON_MODULES = frozenset({"pip", "ensurepip"})
_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

# Target, script and task names that install, deploy or publish. A name is
# refused when any of its words (split on ``-``, ``_``, ``:`` and ``.``) is one
# of these.
_DENIED_TASK_WORDS = frozenset(
    {
        "install",
        "uninstall",
        "preinstall",
        "postinstall",
        "deploy",
        "publish",
        "prepublish",
        "prepublishonly",
        "release",
        "upload",
        "push",
    }
)
_TASK_WORD_SPLIT_RE = re.compile(r"[-_:.]+")
_GENERIC_NON_EXECUTING_OPTIONS = frozenset({"-h", "--help", "--version", "--dry-run"})


_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_DIRECT_RUNNERS = frozenset(
    {"pytest", "py.test", "tox", "nox", "rspec", "jest", "vitest", "mocha", "ava", "phpunit"}
    | {"ctest"}
)
# Runners with an allowed first subcommand; ``cargo`` also takes a
# ``+toolchain`` token before it.
_SUBCOMMAND_RUNNERS: Mapping[str, frozenset[str]] = {
    "go": frozenset({"test", "vet", "build"}),
    "cargo": frozenset({"test", "check", "build", "clippy", "nextest"}),
    "dotnet": frozenset({"test", "build"}),
    "deno": frozenset({"test"}),
    "swift": frozenset({"test", "build"}),
    "mix": frozenset({"test"}),
    "django-admin": frozenset({"test"}),
}
_JS_PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_JS_TEST_SUBCOMMANDS = frozenset({"test", "t", "tst"})
_JS_RUN_SUBCOMMANDS = frozenset({"run", "run-script"})
_JS_GLOBAL_FLAGS = frozenset({"-s", "--silent", "-q", "--quiet"})
_MAKE_REFUSED_OPTIONS = frozenset(
    {
        "-C",
        "--directory",
        "-f",
        "--file",
        "--makefile",
        "-n",
        "--just-print",
        "--dry-run",
        "--recon",
        "-q",
        "--question",
        "-t",
        "--touch",
        "-p",
        "--print-data-base",
    }
)
_MAVEN_TEST_GOALS = frozenset({"test", "verify"})
_GRADLE_TEST_TASKS = frozenset({"test", "check", "build"})

# Kinds whose positional operands select the tests that run.
TARGET_RUNNER_KINDS = frozenset(
    {"pytest", "unittest", "django", "test-script", "jest", "vitest", "mocha", "rspec"}
    | {"go-test", "phpunit"}
)
# Options that exclude or narrow the tests a runner executes. Any of them in
# the command disables the target rule and the runner-output rules: the named
# test may not have run.
_TARGET_EXCLUDING_OPTIONS = frozenset(
    {
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--exclude",
        "--exclude-tag",
        "--exclude-dir",
        "--exclude-pattern",
        "--exclude-group",
        "--skip",
        "--tag",
        "--start-at",
        "--start-after",
        "--testNamePattern",
        "--testPathIgnorePatterns",
        "--grep",
        "--invert",
        "--filter",
        "--group",
        "--example",
    }
)
# Short selection options, by runner (``-k`` is a keyword filter for pytest,
# unittest and Django; ``-m`` a pytest marker filter; ``-run``/``-skip`` for
# ``go test``; ``-t`` a name pattern for jest and vitest; ``-g`` for mocha;
# ``-e`` an example filter for rspec).
_SHORT_EXCLUDING_OPTIONS: Mapping[str, frozenset[str]] = {
    "pytest": frozenset({"-k", "-m"}),
    "unittest": frozenset({"-k"}),
    "django": frozenset({"-k"}),
    "test-script": frozenset({"-k"}),
    "go-test": frozenset({"-run", "-skip"}),
    "jest": frozenset({"-t"}),
    "vitest": frozenset({"-t"}),
    "mocha": frozenset({"-g"}),
    "rspec": frozenset({"-e"}),
}
# Options of the target runners that take a separate value.
_TARGET_VALUE_OPTIONS = frozenset(
    {
        "-m",
        "-p",
        "-c",
        "-o",
        "-W",
        "-n",
        "-r",
        "--rootdir",
        "--basetemp",
        "--junitxml",
        "--junit-xml",
        "--cov",
        "--cov-report",
        "--tb",
        "--maxfail",
        "--durations",
        "--confcutdir",
        "--log-level",
        "--color",
        "--import-mode",
        "--capture",
        "--dist",
        "--numprocesses",
        "--settings",
        "--parallel",
        "--timeout",
        "--config-file",
        "--override-ini",
        "--pattern",
    }
)
# Options of the target runners that take no value.
_TARGET_FLAG_OPTIONS = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-vvv",
        "-x",
        "-s",
        "-l",
        "-b",
        "-f",
        "-ra",
        "-rA",
        "-rf",
        "-rs",
        "-rx",
        "-rX",
        "-rE",
        "-rP",
        "--lf",
        "--ff",
        "--nf",
        "--sw",
        "--last-failed",
        "--failed-first",
        "--new-first",
        "--stepwise",
        "--exitfirst",
        "--quiet",
        "--verbose",
        "--no-header",
        "--no-summary",
        "--disable-warnings",
        "--showlocals",
        "--strict-markers",
        "--strict",
        "--failfast",
        "--buffer",
        "--noinput",
        "--no-input",
        "--keepdb",
        "--debug-sql",
        "--reverse",
        "--timing",
        "--runxfail",
        "--no-cov",
        "-race",
    }
)
# Django-style runners read ``-v`` as a verbosity level with a value;
# ``python -m unittest`` reads ``-v`` as a flag.
_LABEL_RUNNER_VALUE_OPTIONS = frozenset({"-v", "--verbosity"})
_LABEL_RUNNER_KINDS = frozenset({"django", "test-script"})
# Runners whose ``-p``/``--pattern`` is a test discovery pattern.
_PATTERN_RUNNER_KINDS = frozenset({"unittest", "django", "test-script"})

# Per runner, options that replace its configuration, change where it imports
# modules from, or select, shard or skip tests. Any of them disables test-target
# linkage and the runner-output rules. Where a runner's semantics are unclear
# the option is listed (conservative). jest and vitest long options are
# compared case-insensitively without dashes (``--testNamePattern`` and
# ``--test-name-pattern`` alike); ``go test`` accepts ``--name`` for ``-name``.
_JS_CONFIG_OPTIONS = frozenset(
    {"-c", "--config", "-t", "--testnamepattern", "--testpathignorepatterns"}
    | {"--selectprojects", "--ignoreprojects", "--shard", "-o", "--onlychanged"}
    | {"--changedsince", "--changed", "--lastcommit", "--findrelatedtests", "--related"}
    | {"--passwithnotests", "--testpathpattern", "--testpathpatterns", "--testmatch"}
    | {"--testregex", "--roots", "--rootdir", "-r", "--root", "--dir", "--project"}
    | {"--exclude", "--setupfiles", "--setupfilesafterenv", "--modulepaths"}
    | {"--moduledirectories", "--modulenamemapper", "--testrunner", "--testsequencer"}
)
_DJANGO_CONFIG_OPTIONS = frozenset({"--settings", "--pythonpath", "--testrunner"})
# Option values equal to a runner's documented default: they neither select
# tests nor change how modules and settings resolve, so they do not narrow.
# Django's ``tests/runtests.py`` sets ``DJANGO_SETTINGS_MODULE`` to
# ``test_sqlite`` when neither ``--settings`` nor the variable is given
# (``os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_sqlite")``); a
# command-line ``DJANGO_SETTINGS_MODULE`` narrows on its own, and replay scrubs
# an inherited one. ``manage.py`` and ``django-admin`` have no such default.
_DEFAULT_OPTION_VALUES: Mapping[tuple[str, str], frozenset[str]] = {
    ("runtests.py", "--settings"): frozenset({"test_sqlite"}),
}
_CARGO_CONFIG_OPTIONS = frozenset(
    {"--skip", "--exact", "--ignored", "--features", "-F", "--no-default-features"}
    | {"--all-features", "--lib", "--bins", "--bin", "--tests", "--test", "--examples"}
    | {"--example", "--benches", "--bench", "--doc", "-p", "--package", "--exclude"}
    | {"--manifest-path", "--config", "--no-run", "--list", "-Z"}
    | {"-E", "--filter-expr", "--filterset", "--partition", "-P", "--profile", "--run-ignored"}
)
_RUNNER_CONFIG_OPTIONS: Mapping[str, frozenset[str]] = {
    "jest": _JS_CONFIG_OPTIONS,
    "vitest": _JS_CONFIG_OPTIONS,
    "mocha": frozenset(
        {"--config", "--package", "--opts", "--grep", "-g", "--fgrep", "-f", "--invert"}
        | {"-i", "--ignore", "--exclude", "--file", "--require", "-r", "--extension"}
    ),
    "phpunit": frozenset(
        {"-c", "--configuration", "--no-configuration", "--filter", "--exclude-filter"}
        | {"--group", "--exclude-group", "--testsuite", "--exclude-testsuite", "--covers"}
        | {"--uses", "--test-suffix", "--bootstrap", "--include-path", "-d"}
    ),
    "rspec": frozenset(
        {"-O", "--options", "-e", "--example", "-E", "--example-matches", "-t", "--tag"}
        | {"-P", "--pattern", "--exclude-pattern", "-I", "-r", "--require"}
        | {"--default-path", "--only-failures", "-n", "--next-failure"}
    ),
    "go-test": frozenset(
        {"-run", "-skip", "-tags", "-short", "-list", "-exec", "-toolexec", "-overlay"}
        | {"-mod", "-modfile", "-C"}
    ),
    "cargo-test": _CARGO_CONFIG_OPTIONS,
    "cargo-nextest": _CARGO_CONFIG_OPTIONS,
    "mvn": frozenset(
        {"-P", "--activate-profiles", "-pl", "--projects", "-s", "--settings", "-gs"}
        | {"--global-settings", "-f", "--file"}
    ),
    "gradle": frozenset(
        {"--tests", "-P", "--project-prop", "-p", "--project-dir", "-b", "--build-file"}
        | {"-c", "--settings-file", "-I", "--init-script", "-x", "--exclude-task"}
    ),
    "django": _DJANGO_CONFIG_OPTIONS,
    "test-script": _DJANGO_CONFIG_OPTIONS,
}
# The entries of ``_RUNNER_CONFIG_OPTIONS`` that only select or skip tests by
# name, path or group; the others replace configuration or module resolution
# (see ``SELECTION`` and ``CONFIGURATION``).
_JS_SELECTION_OPTIONS = frozenset(
    {"-t", "--testnamepattern", "--testpathignorepatterns", "--testpathpattern"}
    | {"--testpathpatterns", "--selectprojects", "--ignoreprojects", "--shard", "-o"}
    | {"--onlychanged", "--changedsince", "--changed", "--lastcommit", "--findrelatedtests"}
    | {"--related", "--passwithnotests", "--exclude", "--project"}
)
_RUNNER_SELECTION_OPTIONS: Mapping[str, frozenset[str]] = {
    "jest": _JS_SELECTION_OPTIONS,
    "vitest": _JS_SELECTION_OPTIONS,
    "mocha": frozenset(
        {"--grep", "-g", "--fgrep", "-f", "--invert", "-i", "--ignore"} | {"--exclude"}
    ),
    "phpunit": frozenset(
        {"--filter", "--exclude-filter", "--group", "--exclude-group", "--testsuite"}
        | {"--exclude-testsuite", "--covers", "--uses", "--test-suffix"}
    ),
    "rspec": frozenset(
        {"-e", "--example", "-E", "--example-matches", "-t", "--tag", "-P", "--pattern"}
        | {"--exclude-pattern", "--only-failures", "-n", "--next-failure"}
    ),
    "go-test": frozenset({"-run", "-skip"}),
    "cargo-test": frozenset({"--skip", "--exact", "--ignored"}),
    "cargo-nextest": frozenset({"--skip", "--exact", "--ignored", "-E", "--filter-expr"}),
    "mvn": frozenset({"-pl", "--projects"}),
    "gradle": frozenset({"--tests", "-x", "--exclude-task"}),
}
_CAMEL_OPTION_KINDS = frozenset({"jest", "vitest"})
# Runners whose short options take an attached value (``-cjest.config.js``,
# ``-Ilib``): a table entry of two characters also matches as a prefix.
_ATTACHED_SHORT_KINDS = frozenset({"jest", "vitest", "mocha", "phpunit", "rspec"})
# Runners whose single-dash options are whole words (``go test -mod=vendor``,
# ``mvn -pl core``): the option is matched as written, up to any ``=``.
_WORD_OPTION_KINDS = frozenset({"go-test", "mvn", "gradle"})
# Kinds whose positional arguments are test-name filters (``cargo test foo``,
# ``cargo test -- foo``), not targets: any positional narrows, and an option
# outside the two tables below narrows too, since its value may be a filter.
_FILTER_OPERAND_KINDS = frozenset({"cargo-test", "cargo-nextest"})
_CARGO_FLAG_OPTIONS = frozenset(
    {"-q", "--quiet", "-v", "-vv", "--verbose", "-r", "--release", "--workspace", "--all"}
    | {"--all-targets", "--no-fail-fast", "--frozen", "--locked", "--offline", "--timings"}
    | {"--nocapture", "--no-capture", "--show-output", "--include-ignored"}
)
_CARGO_VALUE_OPTIONS = frozenset(
    {"-j", "--jobs", "--target", "--target-dir", "--color", "--message-format"} | {"--test-threads"}
)
# Maven and Gradle ``-D`` properties that select tests.
_BUILD_SELECTION_PROPERTIES = frozenset({"test", "it.test", "groups", "excludedgroups"})
_BUILD_SELECTION_PROPERTY_PREFIXES = ("test.", "surefire.", "failsafe.", "maven.test.")
# Interpreter flags that change where Python imports modules from (``-P``
# and ``-I`` stop prepending the script or working directory to ``sys.path``).
_NARROWING_PYTHON_FLAGS = frozenset({"-P", "-I"})

# Environment variables that change what a test runner collects, selects or
# loads, or where the code under test is imported from. Assigned on the
# command line they disable test-target linkage; replay also removes them from
# the environment it inherits. ``NARROWING_ENVIRONMENT_PREFIXES`` extends the
# set to every variable with one of those prefixes.
NARROWING_ENVIRONMENT = frozenset(
    {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}
    | {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH"}
    | {"DJANGO_SETTINGS_MODULE", "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "RUBYLIB"}
    | {"BUNDLE_GEMFILE", "GOFLAGS", "CGO_ENABLED"}
)
NARROWING_ENVIRONMENT_PREFIXES = ("JEST_", "VITEST_")
# An assignment to one of those variables anywhere in a command's text: a
# prefix, an ``env`` or ``export`` form, a preamble segment, or inside a shell
# wrapper's quoted body.
_NARROWING_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(sorted(NARROWING_ENVIRONMENT))
    + "".join(f"|{prefix}[A-Za-z0-9_]*" for prefix in NARROWING_ENVIRONMENT_PREFIXES)
    + r")="
)


def narrowing_variable(name: str) -> bool:
    """Return True when environment variable ``name`` narrows a test run."""
    upper = name.upper()
    return upper in NARROWING_ENVIRONMENT or upper.startswith(NARROWING_ENVIRONMENT_PREFIXES)


def narrowing_assignments(command: str) -> tuple[str, ...]:
    """Return the narrowing variables ``command``'s text assigns anywhere, sorted.

    A deliberate textual over-approximation: every ``NAME=`` of a narrowing
    variable counts, wherever the shell would put it (a prefix, ``export``,
    an earlier segment, a wrapper's quoted body). Every consumer uses the
    result only to withhold linkage, never to admit or link a command, so
    reading more text than the shell would can only refuse more.
    """
    return tuple(sorted(set(_NARROWING_ASSIGNMENT_RE.findall(command))))


# pytest ini keys that decide which tests are collected or selected; setting
# one through ``-o``/``--override-ini`` disables test-target linkage.
_PYTEST_SELECTION_INI_KEYS = frozenset(
    {"addopts", "python_files", "python_classes", "python_functions", "testpaths"}
    | {"norecursedirs"}
)
# pytest options that replace the configuration or where it is looked up.
_PYTEST_CONFIG_OPTIONS = frozenset({"-c", "--config-file", "--rootdir", "--confcutdir"})
_PYTEST_OVERRIDE_OPTIONS = frozenset({"-o", "--override-ini"})
# ``-p`` plugins that cannot change which tests run or pass: disabling the
# cache provider only removes ``--lf``/``--ff`` and the ``cache`` fixture.
NO_OP_PYTEST_PLUGINS = frozenset({"no:cacheprovider"})


# Programs admitted by absolute path when they resolve inside an environment
# root (see ``environment_roots``), besides the Python interpreters.
_ENVIRONMENT_PROGRAMS = (
    _DIRECT_RUNNERS
    | _JS_PACKAGE_RUNNERS
    | frozenset(_SUBCOMMAND_RUNNERS)
    | {"py.test", "make", "gmake", "mvn", "gradle"}
)


def environment_roots(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the real paths of the known environment roots.

    The verifying interpreter's ``sys.prefix`` and ``sys.base_prefix``,
    ``VIRTUAL_ENV``, ``CONDA_PREFIX`` and every absolute ``PATH`` directory of
    ``environment`` (the process environment when None). The filesystem root
    itself is never a root.
    """
    source = os.environ if environment is None else environment
    candidates = [
        sys.prefix,
        sys.base_prefix,
        source.get("VIRTUAL_ENV", ""),
        source.get("CONDA_PREFIX", ""),
        *source.get("PATH", "").split(os.pathsep),
    ]
    roots = {
        os.path.realpath(candidate)
        for candidate in candidates
        if candidate and os.path.isabs(candidate)
    }
    roots.discard(os.sep)
    return tuple(sorted(roots))


def _environment_program(program: str, roots: Sequence[str] | None) -> bool:
    """Return True for an absolute allowlisted program inside an environment root.

    With ``roots`` None (lexical linkage on an admitted command) the name
    alone decides; otherwise the program's real path, symlinks resolved,
    must be an executable file inside one of ``roots``.
    """
    if not os.path.isabs(program):
        return False
    name = _program_name(program)
    if not (_is_python_executable(name) or name in _ENVIRONMENT_PROGRAMS):
        return False
    if roots is None:
        return True
    real = os.path.realpath(program)
    return os.path.isfile(real) and os.access(real, os.X_OK) and _inside(real, roots)


@dataclass(frozen=True, slots=True)
class ResolvedRunner:
    """The program a command really runs and the arguments it receives.

    ``kind`` names the runner (``pytest``, ``make``, ``script``, ...);
    ``arguments`` are the runner's own arguments, after the module, script or
    subcommand that selected it. ``narrowing_interpreter`` is True when the
    Python interpreter ran with a flag in ``_NARROWING_PYTHON_FLAGS``.
    ``script`` is the file name of a ``test-script`` runner (``runtests.py``).
    ``programs`` is the argv of every program the command runs, from
    ``shell_parsing.program_chain``: each launcher, then the runner itself.
    The denylist is applied to each of them, so it judges the same programs
    the allowlist admitted.
    """

    kind: str
    arguments: tuple[str, ...]
    narrowing_interpreter: bool = False
    script: str = ""
    programs: tuple[tuple[str, ...], ...] = ()


def _environment_bin_path(program: str) -> bool:
    """Return True when ``program``'s parent directory is an environment's
    ``bin`` (``Scripts`` on Windows) or ``node_modules/.bin``."""
    parts = PurePosixPath(program.replace("\\", "/")).parts
    if len(parts) < 3:
        return False
    return parts[-2] in {"bin", "Scripts"} or parts[-3:-1] == ("node_modules", ".bin")


def _denied_task_name(name: str) -> bool:
    return any(word in _DENIED_TASK_WORDS for word in _TASK_WORD_SPLIT_RE.split(name.lower()))


def _inside(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _workspace_roots(workspace: str) -> tuple[str, ...]:
    return tuple({os.path.normpath(os.path.abspath(workspace)), os.path.realpath(workspace)} - {""})


def _workspace_file(token: str, workspace: str | None, cwd_relative: str) -> bool:
    """Return True when ``token`` names a regular file inside the workspace.

    The path is normalized lexically (``..`` cannot climb out). With no
    workspace (linkage on an already admitted command), only a relative path
    that does not climb out is accepted.
    """
    if not token or "\\" in token:
        return False
    if workspace is None:
        path = PurePosixPath(token)
        return not path.is_absolute() and ".." not in path.parts
    roots = _workspace_roots(workspace)
    base = os.path.normpath(os.path.join(os.path.realpath(workspace), cwd_relative))
    candidate = os.path.normpath(token if os.path.isabs(token) else os.path.join(base, token))
    return _inside(candidate, roots) and os.path.isfile(candidate)


def _python_runner(
    parts: Sequence[str], workspace: str | None, cwd_relative: str
) -> ResolvedRunner | None:
    index = 1
    narrowing = False
    while index < len(parts):
        token = parts[index]
        if token == "-m":
            if index + 1 >= len(parts):
                return None
            module, arguments = parts[index + 1], tuple(parts[index + 2 :])
            if module in {"pytest", "unittest", "tox", "nox"}:
                return ResolvedRunner(module, arguments, narrowing)
            if module == "django" and arguments[:1] == ("test",):
                return ResolvedRunner("django", arguments[1:], narrowing)
            return None
        narrowing = narrowing or token in _NARROWING_PYTHON_FLAGS
        if token in _PYTHON_FLAGS:
            index += 1
            continue
        if token in _PYTHON_VALUE_OPTIONS:
            index += 2
            continue
        if token[:2] in _PYTHON_VALUE_OPTIONS and len(token) > 2:
            index += 1
            continue
        if token.startswith("-"):
            # -c, -, -i and unknown options: an inline or interactive program.
            return None
        if not _workspace_file(token, workspace, cwd_relative):
            return None
        script = _project_test_runner_script(["python", *parts[index:]])
        if script is None:
            return ResolvedRunner("script", tuple(parts[index + 1 :]), narrowing)
        rest = tuple(parts[index + 1 :])
        if PurePosixPath(script).name == "manage.py":
            rest = rest[1:]
        return ResolvedRunner("test-script", rest, narrowing, PurePosixPath(script).name)
    return None


def _make_runner(arguments: Sequence[str]) -> ResolvedRunner | None:
    for token in arguments:
        name = token.partition("=")[0]
        if name in _MAKE_REFUSED_OPTIONS or (
            not token.startswith("--") and token[:2] in {"-C", "-f"}
        ):
            return None
        if token in _GENERIC_NON_EXECUTING_OPTIONS:
            return None
    targets = [
        token
        for token in arguments
        if not token.startswith("-") and "=" not in token and not token.isdigit()
    ]
    if any(_denied_task_name(target) for target in targets):
        return None
    return ResolvedRunner("make", tuple(arguments))


def _js_runner(name: str, arguments: Sequence[str]) -> ResolvedRunner | None:
    index = 0
    while index < len(arguments) and arguments[index] in _JS_GLOBAL_FLAGS:
        index += 1
    if index >= len(arguments):
        return None
    subcommand = arguments[index]
    rest = tuple(arguments[index + 1 :])
    if subcommand in _JS_TEST_SUBCOMMANDS:
        return ResolvedRunner("bun-test" if name == "bun" else "js-test", rest)
    if subcommand in _JS_RUN_SUBCOMMANDS:
        if not rest or rest[0].startswith("-") or _denied_task_name(rest[0]):
            return None
        return ResolvedRunner("js-run", rest)
    return None


def _build_tool_runner(name: str, arguments: Sequence[str]) -> ResolvedRunner | None:
    parts = list(arguments)
    if _has_gradle_or_maven_test_skip(parts) or any(
        token in _GENERIC_NON_EXECUTING_OPTIONS or token in {"-m", "--dry-run"} for token in parts
    ):
        return None
    tasks = [token for token in parts if not token.startswith("-") and "=" not in token]
    if any(_denied_task_name(task) for task in tasks):
        return None
    wanted = _MAVEN_TEST_GOALS if name.startswith("mvn") else _GRADLE_TEST_TASKS
    if not any(task.rsplit(":", 1)[-1] in wanted for task in tasks):
        return None
    return ResolvedRunner(name.removesuffix("w"), tuple(arguments))


def _subcommand_runner(name: str, arguments: Sequence[str]) -> ResolvedRunner | None:
    index = 0
    if name == "cargo" and arguments[:1] and arguments[0].startswith("+"):
        index = 1
    if index >= len(arguments) or arguments[index] not in _SUBCOMMAND_RUNNERS[name]:
        return None
    subcommand = arguments[index]
    kind = "django" if name == "django-admin" else f"{name}-{subcommand}"
    return ResolvedRunner(kind, tuple(arguments[index + 1 :]))


def _runner(
    name: str, parts: Sequence[str], workspace: str | None, cwd_relative: str
) -> ResolvedRunner | None:
    arguments = parts[1:]
    if _is_python_executable(name):
        return _python_runner(parts, workspace, cwd_relative)
    if name in _SHELL_INTERPRETERS:
        # Only ``sh <workspace script> [args]``: no options, no inline program.
        if not arguments or not _workspace_file(arguments[0], workspace, cwd_relative):
            return None
        return ResolvedRunner("script", tuple(arguments[1:]))
    if name in {"py.test", "pytest"}:
        return ResolvedRunner("pytest", tuple(arguments))
    if name in _DIRECT_RUNNERS:
        return ResolvedRunner(name, tuple(arguments))
    if name in {"make", "gmake"}:
        return _make_runner(arguments)
    if name in _JS_PACKAGE_RUNNERS:
        return _js_runner(name, arguments)
    if name in {"mvn", "mvnw", "gradle", "gradlew"}:
        return _build_tool_runner(name, arguments)
    if name in _SUBCOMMAND_RUNNERS:
        return _subcommand_runner(name, arguments)
    return None


def _resolve(
    peeled: tuple[str, ...],
    workspace: str | None,
    cwd_relative: str,
    roots: Sequence[str] | None,
) -> ResolvedRunner | None:
    """Return the runner the final program of a ``program_chain`` is, or None."""
    program = peeled[0]
    name = _program_name(program)
    if "/" in program or "\\" in program:
        # A path program must be a file inside the workspace: a runner the
        # project ships (``./gradlew``, ``.venv/bin/pytest``) or its script;
        # or an allowlisted interpreter or runner inside an environment root.
        if not _workspace_file(program, workspace, cwd_relative):
            if _environment_program(program, roots):
                return _runner(name, peeled, workspace, cwd_relative)
            return None
        interpreter = _is_python_executable(name) or name in _SHELL_INTERPRETERS
        if (interpreter or name in _DIRECT_RUNNERS) and not _environment_bin_path(program):
            # A workspace file named like an interpreter or a test runner
            # (``./pytest``) is that program only inside an environment's
            # ``bin`` directory (``.venv/bin/pytest``, ``node_modules/.bin/jest``);
            # elsewhere an interpreter name is refused and a runner name is a
            # plain script, whose operands are never test targets.
            return None if interpreter else ResolvedRunner("script", peeled[1:])
        resolved = _runner(name, peeled, workspace, cwd_relative)
        if resolved is not None:
            return resolved
        if interpreter or _environment_bin_path(program):
            # A file in an environment's ``bin`` (``.venv/bin/pip``,
            # ``node_modules/.bin/tsc``) is an installed program, not a script
            # the project ships: it is replayed only as an allowlisted runner.
            return None
        script = _project_test_runner_script(list(peeled))
        if script is None:
            return ResolvedRunner("script", peeled[1:])
        rest = peeled[1:]
        if PurePosixPath(script).name == "manage.py":
            rest = rest[1:]
        return ResolvedRunner("test-script", rest, script=PurePosixPath(script).name)
    if name in VIEWER_PROGRAMS or name in REFUSED_WRAPPERS:
        return None
    return _runner(name, peeled, workspace, cwd_relative)


def _non_executing(runner: ResolvedRunner) -> bool:
    if any(argument in _GENERIC_NON_EXECUTING_OPTIONS for argument in runner.arguments):
        return True
    if runner.kind == "pytest":
        return any(argument in _PYTEST_NON_EXECUTING_OPTIONS for argument in runner.arguments)
    if runner.kind == "tox":
        return any(a in {"-l", "--listenvs", "--showconfig"} for a in runner.arguments)
    if runner.kind == "nox":
        return any(argument in {"-l", "--list"} for argument in runner.arguments)
    return False


def resolve_replay_program(
    argv: Sequence[str],
    *,
    workspace: str | None,
    cwd_relative: str = ".",
    environment: Mapping[str, str] | None = None,
) -> ResolvedRunner | None:
    """Return the allowlisted runner ``argv`` really runs, or None.

    ``workspace`` None resolves lexically (no file checks); replay admission
    always passes the workspace, and ``environment`` (the replay environment,
    the process environment when None) supplies the environment roots.
    """
    programs = program_chain(argv)
    if not programs:
        return None
    # Only the final program is resolved: every earlier entry is a launcher,
    # and the launched program passes the same allowlist (``uv run pytest``).
    roots = None if workspace is None else environment_roots(environment)
    resolved = _resolve(programs[-1], workspace, cwd_relative, roots)
    if resolved is None or _non_executing(resolved):
        return None
    return replace(resolved, programs=programs)


def _absolute_argument_outside(token: str, roots: Sequence[str]) -> bool:
    value = token.partition("=")[2] if token.startswith("-") and "=" in token else token
    if not value.startswith("/") or value == "/dev/null":
        return False
    return not _inside(os.path.normpath(value), roots)


def outside_known_roots(
    value: str, *, workspace: str, environment: Mapping[str, str] | None = None
) -> bool:
    """Return True when an absolute ``value`` lies outside the workspace.

    ``/dev/null`` and an admitted environment program are not outside.
    """
    return _absolute_argument_outside(value, _workspace_roots(workspace)) and not (
        _environment_program(value, environment_roots(environment))
    )


def admitted_runner(
    argv: Sequence[str],
    *,
    workspace: str,
    cwd_relative: str = ".",
    environment: Mapping[str, str] | None = None,
) -> ResolvedRunner | None:
    """Return the runner ``argv`` runs when the allowlist admits it, or None.

    The runner is ``resolve_replay_program``'s; an absolute-path argument
    outside the workspace and the environment roots refuses the command.
    """
    runner = resolve_replay_program(
        argv, workspace=workspace, cwd_relative=cwd_relative, environment=environment
    )
    if runner is None or any(
        outside_known_roots(token, workspace=workspace, environment=environment) for token in argv
    ):
        return None
    return runner


def replay_allowed(
    argv: Sequence[str],
    *,
    workspace: str,
    cwd_relative: str = ".",
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return True when ``argv`` may be replayed (see module docstring)."""
    return (
        admitted_runner(
            argv, workspace=workspace, cwd_relative=cwd_relative, environment=environment
        )
        is not None
    )


def replay_denied(argv: Sequence[str]) -> bool:
    """Return True when ``argv`` must never be replayed (see module docstring).

    Every program ``argv`` runs is judged (``shell_parsing.program_chain``):
    each launcher and the program it launches, after wrappers are peeled, so
    ``uv run .venv/bin/pip install x`` is judged as ``pip install``. A chain
    whose programs cannot be identified with certainty is denied.
    """
    programs = program_chain(argv)
    if not programs:
        return True
    return any(program_denied(program) for program in programs)


def program_denied(parts: Sequence[str]) -> bool:
    """Return True when one program's argv, wrappers already peeled, is denied."""
    if not parts:
        return True
    name = _program_name(parts[0])
    if _VERSIONED_PIP_RE.fullmatch(name):
        name = "pip"
    arguments = list(parts[1:])
    if name in _DENIED_PROGRAMS:
        return True
    if name in _SHELL_PROGRAMS and any(
        argument == "-c" or (argument[:1] == "-" and argument[1:2] != "-" and "c" in argument[1:])
        for argument in arguments
    ):
        # An inline shell program cannot be inspected; it is never replayed.
        return True
    if name == "git":
        return not arguments or arguments[0] not in _READ_ONLY_GIT_SUBCOMMANDS
    if _is_python_executable(name) and "-m" in arguments:
        module_index = arguments.index("-m") + 1
        if module_index < len(arguments) and arguments[module_index] in _DENIED_PYTHON_MODULES:
            return True
    denied = _DENIED_SUBCOMMANDS.get(name)
    if denied is None:
        return False
    subcommand = next((argument for argument in arguments if not argument.startswith("-")), None)
    if subcommand is None:
        return name in _BARE_INSTALLERS
    return subcommand in denied


def authorize_replay(
    argv: Sequence[str],
    *,
    workspace: str,
    cwd_relative: str = ".",
    environment: Mapping[str, str] | None = None,
) -> ResolvedRunner | None:
    """Return the runner ``argv`` runs when replay is authorized, or None.

    The allowlist (``admitted_runner``) and the denylist (``program_denied``)
    judge the same resolution: the denylist is applied to every program of
    the admitted runner's chain, each launcher and the program it launches.
    """
    runner = admitted_runner(
        argv, workspace=workspace, cwd_relative=cwd_relative, environment=environment
    )
    if runner is None or any(program_denied(program) for program in runner.programs):
        return None
    return runner


def _excluding_option(token: str, kind: str) -> bool:
    if not token.startswith("-") or token == "-":
        return False
    name = token.partition("=")[0]
    if name in _TARGET_EXCLUDING_OPTIONS:
        return True
    short = _SHORT_EXCLUDING_OPTIONS.get(kind, frozenset())
    if name in short:
        return True
    return not token.startswith("--") and any(
        len(option) == 2 and token.startswith(option) for option in short
    )


def _option_key(name: str, kind: str) -> str:
    """Return ``name`` as ``_RUNNER_CONFIG_OPTIONS`` spells it for ``kind``."""
    if kind in _CAMEL_OPTION_KINDS and name.startswith("--"):
        return "--" + name[2:].replace("-", "").lower()
    if kind == "go-test" and name.startswith("--"):
        return name[1:]
    return name


def _build_property_narrows(name: str, value: str | None) -> bool:
    """Return True for a Maven or Gradle ``-D`` property that selects tests."""
    if not name.startswith("-D"):
        return False
    key = name[2:] if len(name) > 2 else (value or "").partition("=")[0]
    key = key.lower()
    return key in _BUILD_SELECTION_PROPERTIES or key.startswith(_BUILD_SELECTION_PROPERTY_PREFIXES)


def _runner_config_option(token: str, name: str, value: str | None, kind: str) -> str | None:
    """Return the narrowing class of ``token`` among ``kind``'s
    ``_RUNNER_CONFIG_OPTIONS``, or None when it is not one of them."""
    options = _RUNNER_CONFIG_OPTIONS.get(kind)
    if options is None:
        return None
    selection = _RUNNER_SELECTION_OPTIONS.get(kind, frozenset())
    if kind in _WORD_OPTION_KINDS:
        name = token.partition("=")[0]
    if kind in {"mvn", "gradle"}:
        if _build_property_narrows(name, value):
            return SELECTION
        if name.startswith("-P"):
            return CONFIGURATION
    key = _option_key(name, kind)
    if key not in options and kind in _ATTACHED_SHORT_KINDS and not token.startswith("--"):
        key = next((o for o in options if len(o) == 2 and token.startswith(o)), key)
    if key in options:
        return SELECTION if key in selection else CONFIGURATION
    if kind in _FILTER_OPERAND_KINDS and name not in (_CARGO_FLAG_OPTIONS | _CARGO_VALUE_OPTIONS):
        # An option this table does not know: its value may be a filter, and
        # the option may change the build.
        return CONFIGURATION
    return None


def _narrowing_option(name: str, value: str | None, kind: str) -> str | None:
    """Return the narrowing class of option ``name`` (with ``value``, if it took
    one) for pytest and the pattern runners, or None."""
    if kind == "pytest":
        if name in _PYTEST_CONFIG_OPTIONS:
            return CONFIGURATION
        if name in _PYTEST_OVERRIDE_OPTIONS:
            if value is None or "=" not in value:
                return CONFIGURATION
            key = value.partition("=")[0].strip().lower()
            return CONFIGURATION if not key or key in _PYTEST_SELECTION_INI_KEYS else None
        if name == "-p" and value not in NO_OP_PYTEST_PLUGINS:
            return CONFIGURATION
    elif kind in _PATTERN_RUNNER_KINDS and name in {"-p", "--pattern"}:
        return SELECTION
    return None


def _narrowing_environment(argv: Sequence[str], environment: Sequence[str]) -> bool:
    names = set(environment)
    names.update(token.partition("=")[0] for token in command_line_assignments(argv))
    return any(narrowing_variable(name) for name in names)


# Runners whose short options follow argparse: a cluster such as ``-qk expr``
# is ``-q`` followed by ``-k expr``.
_CLUSTERED_SHORT_KINDS = frozenset({"pytest", "unittest", "django", "test-script", "tox", "nox"})


def _expand_short_clusters(
    arguments: Sequence[str], flag_options: set[str], value_options: set[str]
) -> tuple[str, ...]:
    """Split argparse short-option clusters into one option per token.

    ``-qk expr`` becomes ``-q -k expr`` and ``-sv`` becomes ``-s -v``. A
    letter that is not a known flag takes the rest of the cluster as its
    value (``-qkexpr`` becomes ``-q -kexpr``), as argparse reads it. A token
    that is itself a known option (``-vv``, ``-ra``) or a known value option
    with its value attached (``-n4``, ``-rfE``) is kept, and nothing after
    ``--`` is touched.
    """
    expanded: list[str] = []
    for position, token in enumerate(arguments):
        if token == "--":
            expanded.extend(arguments[position:])
            break
        if (
            token.startswith("--")
            or not token.startswith("-")
            or len(token) <= 2
            or token in flag_options
            or token[:2] in value_options
            or "=" in token
        ):
            expanded.append(token)
            continue
        cluster = token[1:]
        for letter_index, letter in enumerate(cluster):
            option = f"-{letter}"
            if option in flag_options:
                expanded.append(option)
                continue
            expanded.append(option + cluster[letter_index + 1 :])
            break
    return tuple(expanded)


def _selection(
    argv: Sequence[str], environment: Sequence[str]
) -> tuple[ResolvedRunner | None, str | None, frozenset[str]]:
    """Return ``(runner, narrowing, operands)`` for ``argv``.

    ``narrowing`` is ``CONFIGURATION`` when configuration, the environment or
    an interpreter flag changes what the runner collects, loads or imports;
    otherwise ``SELECTION`` when an option excludes or narrows the tests it
    executes; otherwise None (see the module docstring). ``operands`` are
    empty unless ``narrowing`` is None. ``environment``
    names the variables the command line assigns outside ``argv`` (a replay
    candidate's ``env_delta``). Option values are never operands; a token after
    an option the tables do not know is not an operand either, since it may be
    that option's value. Tokens after ``--`` are checked for narrowing but are
    never operands.
    """
    runner = resolve_replay_program(argv, workspace=None)
    if runner is None:
        return None, None, frozenset()
    if runner.narrowing_interpreter or _narrowing_environment(argv, environment):
        return runner, CONFIGURATION, frozenset()
    narrowing: str | None = None
    value_options = set(_TARGET_VALUE_OPTIONS)
    flag_options = set(_TARGET_FLAG_OPTIONS)
    if runner.kind in _LABEL_RUNNER_KINDS:
        value_options |= _LABEL_RUNNER_VALUE_OPTIONS
        flag_options -= _LABEL_RUNNER_VALUE_OPTIONS
    if runner.kind in _FILTER_OPERAND_KINDS:
        value_options |= _CARGO_VALUE_OPTIONS
        flag_options |= _CARGO_FLAG_OPTIONS
    operands: set[str] = set()
    after_separator = False
    arguments = runner.arguments
    if runner.kind in _CLUSTERED_SHORT_KINDS:
        arguments = _expand_short_clusters(arguments, flag_options, value_options)
    index = 0
    while index < len(arguments):
        token = arguments[index]
        index += 1
        if token == "--":
            after_separator = True
            continue
        if not token.startswith("-") or token == "-":
            if runner.kind in _FILTER_OPERAND_KINDS:
                # ``cargo test foo`` or ``cargo test -- foo``: a name filter.
                narrowing = SELECTION
            elif not after_separator:
                operands.add(token)
            continue
        if _excluding_option(token, runner.kind):
            narrowing = SELECTION
            continue
        name, separator, inline = token.partition("=")
        value: str | None = None
        consumes_next = False
        if token.startswith("--"):
            if separator:
                value = inline
            elif name not in flag_options:
                # A known value option, or an unknown one whose value may follow.
                consumes_next = True
        elif token[:2] in value_options and len(token) > 2:
            name, value = token[:2], token[2:]
        elif separator:
            value = inline
        elif name not in flag_options:
            consumes_next = True
        if consumes_next and index < len(arguments):
            following = arguments[index]
            if not following.startswith("-") or following == "-":
                # An option-like token is never taken as a value: it is
                # examined as an option of its own (``--x --ignore t.py``).
                value = following
                index += 1
        if value in _DEFAULT_OPTION_VALUES.get((runner.script, name), frozenset()):
            continue
        found = _narrowing_option(name, value, runner.kind) or _runner_config_option(
            token, name, value, runner.kind
        )
        if found == CONFIGURATION:
            return runner, CONFIGURATION, frozenset()
        narrowing = narrowing or found
    if narrowing is not None:
        return runner, narrowing, frozenset()
    return runner, None, frozenset(operands)


def inline_python_alters_imports(argv: Sequence[str], environment: Sequence[str] = ()) -> bool:
    """Return True when a ``python -c`` call may import modules from outside the default path.

    The same configuration decision ``_selection`` makes for a runner: an
    interpreter flag in ``_NARROWING_PYTHON_FLAGS`` (``-P``, ``-I``: the
    working directory is not on ``sys.path``) or a narrowing variable
    (``PYTHONPATH``, ...) assigned on the command line or named in
    ``environment``. False when ``argv`` makes no ``python -c`` call.
    """
    inline = python_inline_program(argv)
    if inline is None:
        return False
    return bool(inline.options & _NARROWING_PYTHON_FLAGS) or _narrowing_environment(
        argv, environment
    )


def excludes_tests(argv: Sequence[str], environment: Sequence[str] = ()) -> bool:
    """Return True when ``argv`` resolves to a runner that an option or the
    command-line configuration narrows (``--ignore``, ``-k``, ``-o addopts=``,
    ``PYTEST_ADDOPTS=``, ...). ``environment`` names variables assigned
    outside ``argv``."""
    runner, narrowing, _ = _selection(argv, environment)
    return runner is not None and narrowing is not None


def alters_configuration(argv: Sequence[str], environment: Sequence[str] = ()) -> bool:
    """Return True when ``argv`` resolves to a runner whose configuration, module
    resolution or environment the command changes (``CONFIGURATION``): its
    output cannot show that a named test passed against the workspace's code,
    even when it names that test. ``environment`` names variables assigned
    outside ``argv``."""
    runner, narrowing, _ = _selection(argv, environment)
    return runner is not None and narrowing == CONFIGURATION


def run_may_back_test_claim(
    argv: Sequence[str], claim_file: str | None, environment: Sequence[str] = ()
) -> bool:
    """Gate the runner-output rules for a replayed test run.

    False when the program cannot be resolved, when an option or the
    command-line configuration excludes or narrows the tests it runs, or when
    ``claim_file`` (the claimed test file, if any) appears in the command
    without being an executed operand, as in ``pytest --rootdir
    tests/test_x.py``. ``environment`` names variables assigned outside
    ``argv`` (a replayed run's ``env_delta``).
    """
    runner, narrowing, operands = _selection(argv, environment)
    if runner is None or narrowing is not None:
        return False
    if claim_file is None:
        return True
    needle = claim_file.lower()
    if not any(needle in token.lower() for token in argv):
        return True
    return runner.kind in TARGET_RUNNER_KINDS and any(
        operand.split("::", 1)[0].lower() == needle for operand in operands
    )


def claim_target_operands(argv: Sequence[str], environment: Sequence[str] = ()) -> frozenset[str]:
    """Return the tests ``argv`` names as positional operands of a test runner.

    Empty unless the program is a test runner whose operands select tests
    (``TARGET_RUNNER_KINDS``) and neither an option nor the command-line
    configuration narrows them (see the module docstring). ``environment``
    names variables assigned outside ``argv``.
    """
    runner, narrowing, operands = _selection(argv, environment)
    if runner is None or narrowing is not None or runner.kind not in TARGET_RUNNER_KINDS:
        return frozenset()
    return operands


__all__ = [
    "TARGET_RUNNER_KINDS",
    "VIEWER_PROGRAMS",
    "ResolvedRunner",
    "admitted_runner",
    "authorize_replay",
    "alters_configuration",
    "NARROWING_ENVIRONMENT",
    "NARROWING_ENVIRONMENT_PREFIXES",
    "NO_OP_PYTEST_PLUGINS",
    "claim_target_operands",
    "command_line_assignments",
    "environment_roots",
    "excludes_tests",
    "inline_python_alters_imports",
    "narrowing_assignments",
    "narrowing_variable",
    "outside_known_roots",
    "program_denied",
    "replay_allowed",
    "replay_denied",
    "resolve_replay_program",
    "run_may_back_test_claim",
]
