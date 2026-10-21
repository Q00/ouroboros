"""Mechanical command configuration built from ``.ouroboros/mechanical.toml``.

Ouroboros no longer ships hardcoded per-language presets. Instead,
``ouroboros.evaluation.detector`` runs a single AI call that inspects the
repo and writes ``.ouroboros/mechanical.toml`` with commands this project
can actually execute. This module is the deterministic reader for that
file: Stage 1 trusts the toml and nothing else.

Usage:
    config = build_mechanical_config(Path("/path/to/project"))
    verifier = MechanicalVerifier(config)

When the toml is absent, all commands resolve to ``None`` and Stage 1
skips gracefully rather than running the wrong tool.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
import re
from typing import Any

import structlog

from ouroboros.evaluation.command_parsing import split_command
from ouroboros.evaluation.mechanical import MechanicalConfig

log = structlog.get_logger()


# Executables accepted in ``.ouroboros/mechanical.toml`` (authored by the AI
# detector or by hand). Any token outside this set is dropped before reaching
# ``create_subprocess_exec``. Curated to cover the common zero-cost gate
# tools — build runners, language package managers, and well-behaved linters
# / test runners — while refusing arbitrary shell utilities (``rm``, ``curl``,
# ``bash`` …) that could turn a Stage 1 run into remote code execution in CI.
_ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        # Python
        "python",
        "python3",
        "uv",
        "poetry",
        "pdm",
        "hatch",
        "pip",
        "ruff",
        "mypy",
        "pytest",
        "pyright",
        "black",
        "isort",
        "flake8",
        "pylint",
        "bandit",
        "tox",
        "nox",
        # Zig
        "zig",
        # Rust
        "cargo",
        "rustc",
        "clippy-driver",
        # Go
        "go",
        "golangci-lint",
        "gofmt",
        # Node.js
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "node",
        "deno",
        "tsc",
        "vitest",
        "jest",
        "mocha",
        "eslint",
        "biome",
        "prettier",
        # General build tools
        "make",
        "gmake",
        "cmake",
        "ninja",
        "bazel",
        "buck",
        "gradle",
        "gradlew",
        "gradlew.bat",
        "mvn",
        "mvnw",
        "mvnw.cmd",
        "ant",
        "just",
        "task",
        # Other languages
        "cabal",
        "stack",
        "ghc",
        "dotnet",
        "mix",
        "elixir",
        "swift",
        "swiftc",
        "xcodebuild",
        "javac",
        "java",
        "kotlinc",
        "clang",
        "clang-tidy",
        "clang-format",
        "gcc",
        "g++",
        "shellcheck",
        # Ruby
        "ruby",
        "bundle",
        "rake",
        "rspec",
        "rubocop",
        # PHP
        "php",
        "composer",
        "phpunit",
    }
)


# Model judgment may construct a check; it cannot grant acceptance (#2450).
# An allowlisted executable can still be invoked in a mode whose exit status
# says nothing about the project: ``pytest --version`` exits 0 over a test
# suite whose only test is ``assert False``, and Stage 1 would record that
# as an executed pass. Every executable in ``_ALLOWED_EXECUTABLES`` therefore
# has an entry below naming the argv shapes that put it in such a mode:
#
# * it reports about the tool instead of running it (version, help, info);
# * it enumerates or plans the work without performing it (collect-only,
#   list, dry-run, no-run, touch / assume-old);
# * it forces a success exit whatever it finds (exit-zero, ignore-errors,
#   fail-never, ``-DskipTests``);
# * it swaps out what runs the check (``make SHELL=true``, ``--script-shell``,
#   ``go test -exec``, ``bazel --run_under``, ``cargo --config`` runner
#   overrides) or runs inline code instead of the project (``python -c``,
#   ``node -e``, ``php -r``).
#
# Such a command is refused at the same gate as a non-allowlisted one, so it
# is never persisted and a hand-authored ``mechanical.toml`` cannot carry it
# either. Entries are per executable because the same spelling means
# different things to different tools (``-n`` is make's dry run but
# pytest-xdist's worker count; ``-V`` is pytest's version but Maven's
# show-version-and-continue). ``test_languages`` fails when an executable is
# added to the allowlist without an entry here.


@dataclass(frozen=True, slots=True)
class _ModeSpec:
    """How one executable's argv selects a mode that verifies nothing.

    ``forbidden`` and ``prefixes`` hold option names already normalized the
    way :func:`_ModeSpec.normalize` normalizes argv tokens. Build entries with
    :func:`_spec`, never directly.
    """

    forbidden: frozenset[str]
    build_ok: frozenset[str]
    union_flags: frozenset[str]
    prefixes: tuple[str, ...]
    subcommands: frozenset[str]
    scoped: tuple[tuple[str, frozenset[str]], ...]
    clusters: bool
    abbreviates: bool
    single_dash_long: bool
    case_insensitive: bool
    wraps: bool
    module_option: str | None
    forwards: bool
    assignments: bool
    needs_positional: bool
    skip_properties: bool
    takes_value: frozenset[str]
    task_list: bool
    zero_runs: frozenset[str]

    def normalize(self, name: str) -> str:
        if self.case_insensitive:
            name = name.lower()
        if self.single_dash_long and name.startswith("-"):
            name = "-" + name.lstrip("-")
        return name

    def refuses(self, candidate: str, extra: frozenset[str] = frozenset()) -> bool:
        name = self.normalize(candidate)
        forbidden = self.forbidden | extra
        if name in forbidden or name.startswith(self.prefixes):
            return True
        if (
            self.abbreviates
            and name.startswith("--")
            and len(name) > 3
            and any(f.startswith(name) for f in forbidden if f.startswith("--"))
        ):
            # argparse (``allow_abbrev``) and getopt_long accept any unique
            # prefix: ``mypy --vers`` and ``make --dry`` are the full options.
            return True
        cluster = _SHORT_CLUSTER.match(name) if self.clusters else None
        if cluster is not None and len(cluster.group(1)) > 1:
            # ``-qV`` / ``-kn`` / ``-nj4``: clustered single-letter options.
            return any(f"-{letter}" in forbidden for letter in cluster.group(1))
        return False


_GENERIC_FLAGS: frozenset[str] = frozenset({"--version", "--help"})
# Long flags a forwarding runner (``npm test -- …``, ``tox -- …``) must not
# pass on: the script body is not visible here, so every tool's long
# non-verifying flags apply. ``--all`` is excluded because it is ``tsc``'s
# help listing but jest's legitimate "run all tests".
_UNION_EXCLUDED: frozenset[str] = frozenset({"--all"})
# ``-qV`` / ``-nj4``: a run of single-letter options, possibly ending in a value.
_SHORT_CLUSTER = re.compile(r"-([A-Za-z]+)")
# make / just / task / rake command-line variable assignments.
_ASSIGNMENT = re.compile(r"[A-Za-z_.][A-Za-z0-9_.-]*(?::{0,3}|[+?!])=")


def _spec(
    flags: str = "",
    *,
    overrides: str = "",
    build_ok: str = "",
    prefixes: str = "",
    subcommands: str = "",
    scoped: dict[str, str] | None = None,
    clusters: bool = True,
    abbreviates: bool = False,
    single_dash_long: bool = False,
    case_insensitive: bool = False,
    wraps: bool = False,
    module_option: str | None = None,
    forwards: bool = False,
    assignments: bool = False,
    needs_positional: bool = False,
    skip_properties: bool = False,
    values: str = "",
    task_list: bool = False,
    zero_runs: str = "",
) -> _ModeSpec:
    """Build a :class:`_ModeSpec` from space-separated option names.

    ``flags`` are modes that report, enumerate, plan or force success;
    ``overrides`` replace what runs the check or run inline code. Both are
    refused for every key; only long ``flags`` join the set applied to
    forwarded args. ``build_ok`` modes still compile but run nothing
    (``cargo test --no-run``): evidence for a ``build`` command, refused for
    every other key. ``values`` are options whose next token is their value
    (``pytest -k npm``), so that token is neither a positional nor a program.
    ``task_list`` marks tools that run every positional as a task or goal
    (``gradle clean test``): ``subcommands`` then refuse only a line whose
    positionals are all reporting tasks. ``zero_runs`` are repeat counts,
    refused under a ``scoped`` subcommand when set to zero (``go test
    -count=0`` runs nothing and exits 0).
    """
    probe = _ModeSpec(
        forbidden=frozenset(),
        build_ok=frozenset(),
        union_flags=frozenset(),
        prefixes=(),
        subcommands=frozenset(),
        scoped=(),
        clusters=clusters and not single_dash_long,
        abbreviates=abbreviates,
        single_dash_long=single_dash_long,
        case_insensitive=case_insensitive,
        wraps=wraps,
        module_option=module_option,
        forwards=forwards,
        assignments=assignments,
        needs_positional=needs_positional,
        skip_properties=skip_properties,
        takes_value=frozenset(),
        task_list=task_list,
        zero_runs=frozenset(),
    )
    names = flags.split()
    return replace(
        probe,
        forbidden=frozenset(map(probe.normalize, (*names, *overrides.split(), *_GENERIC_FLAGS))),
        build_ok=frozenset(map(probe.normalize, build_ok.split())),
        union_flags=frozenset(n for n in names if n.startswith("--")) - _UNION_EXCLUDED,
        prefixes=tuple(map(probe.normalize, prefixes.split())),
        subcommands=frozenset(filter(None, (s.strip() for s in subcommands.split(",")))),
        scoped=tuple(
            (sub, frozenset(map(probe.normalize, names_.split())))
            for sub, names_ in (scoped or {}).items()
        ),
        takes_value=frozenset(map(probe.normalize, values.split())),
        zero_runs=frozenset(map(probe.normalize, zero_runs.split())),
    )


_PYTHON_SPEC = _spec(
    "-V -h -? --help-env --help-xoptions --help-all",
    overrides="-c",
    wraps=True,
    module_option="-m",
    forwards=True,
    needs_positional=True,
    # ``python -m <module>`` whose exit status cannot reflect the project.
    subcommands=(
        "timeit, pydoc, json.tool, tabnanny, pickletools, zipfile, zipapp, dis, ast, this,"
        " antigravity, sysconfig, site, platform, calendar, webbrowser, base64, uuid"
    ),
)
_MAKE_SPEC = _spec(
    "-n --just-print --dry-run --recon -q --question -t --touch -i --ignore-errors -v -h"
    " -o --old-file --assume-old -W --what-if --new-file --assume-new",
    overrides="-E --eval",
    abbreviates=True,
    assignments=True,
)
_GRADLE_SPEC = _spec(
    "-m --dry-run -v -h -? --status --stop",
    clusters=False,
    # Tasks that report on the build or delete outputs, and run no check.
    subcommands=(
        "tasks, help, projects, properties, dependencies, dependencyInsight,"
        " buildEnvironment, outgoingVariants, resolvableConfigurations, javaToolchains,"
        " components, model, clean"
    ),
    values=(
        "-x --exclude-task -p --project-dir -b --build-file -c --settings-file"
        " -I --init-script -g --gradle-user-home -P -D --console --warning-mode"
    ),
    task_list=True,
)
_MAVEN_SPEC = _spec(
    "-v -h -fn --fail-never",
    clusters=False,
    skip_properties=True,
    # Goals that describe the project or delete outputs, and run no check.
    subcommands=(
        "help:help, help:describe, help:effective-pom, help:effective-settings, help:system,"
        " help:active-profiles, help:all-profiles, help:evaluate, dependency:tree,"
        " dependency:list, dependency:resolve, dependency:sources, clean"
    ),
    values=(
        "-pl --projects -f --file -P --activate-profiles -s --settings -gs"
        " --global-settings -rf --resume-from -T --threads -t --toolchains -l --log-file"
    ),
    task_list=True,
)
_NODE_PM_SPEC = _spec(
    "-v -h -? --dry-run",
    overrides="--script-shell --config.script-shell",
    wraps=True,
    forwards=True,
)
_RUSTC_SPEC = _spec("-V -h --print --explain")
_CC_SPEC = _spec(
    "-dumpversion -dumpmachine -dumpfullversion -dumpspecs -### --target-help -E -M -MM",
    prefixes="-print- --print-",
    clusters=False,
)

_MODE_SPECS: dict[str, _ModeSpec] = {
    # Python
    "python": _PYTHON_SPEC,
    "python3": _PYTHON_SPEC,
    "uv": _spec(
        "-V -h --dry-run",
        wraps=True,
        forwards=True,
        subcommands="tree, version, help, cache dir, python list, tool list, self version",
    ),
    "poetry": _spec("-V -h --dry-run", wraps=True, forwards=True),
    "pdm": _spec("-V -h -l --list --dry-run", wraps=True, forwards=True),
    "hatch": _spec("-h --dry-run", wraps=True, forwards=True),
    "pip": _spec(
        "-V -h --dry-run",
        subcommands=(
            "list, show, freeze, help, config, debug, inspect, index, cache, completion, hash,"
            " search"
        ),
        needs_positional=True,
    ),
    "ruff": _spec(
        "-V -h -e --exit-zero --show-files --show-settings --fix-only --add-noqa",
        subcommands="help, version, rule, config, linter, clean, server, analyze graph",
    ),
    "mypy": _spec("-V -h", overrides="-c --command", abbreviates=True),
    "pytest": _spec(
        "-V -h --collect-only --co --setup-plan --setup-only --fixtures --funcargs"
        " --fixtures-per-test --markers",
        abbreviates=True,
        values=(
            "-k -m -p -c -W -n --deselect --ignore --ignore-glob --rootdir --confcutdir"
            " --basetemp --junitxml --junit-xml --log-level --tb --durations --maxfail --dist"
            " --cov --cov-report --cov-fail-under -r --import-mode --capture"
        ),
    ),
    "pyright": _spec("-h --createstub"),
    "black": _spec("-h", overrides="-c --code"),
    "isort": _spec("-V -h --show-config --show-files", abbreviates=True),
    "flake8": _spec("-h --exit-zero --bug-report", abbreviates=True),
    "pylint": _spec(
        "-h --exit-zero --long-help --full-documentation --generate-rcfile"
        " --generate-toml-config --help-msg",
        prefixes="--list-",
        abbreviates=True,
    ),
    "bandit": _spec("-h --exit-zero", abbreviates=True),
    "tox": _spec(
        "-h -l --listenvs -a --listenvs-all --showconfig --notest --no-test --help-ini --hi",
        overrides="-x --override",
        subcommands="list, l, config, c, depends, de, schema, quickstart",
        abbreviates=True,
        forwards=True,
    ),
    "nox": _spec(
        "-h -l --list --list-sessions --install-only --usage", abbreviates=True, forwards=True
    ),
    # Zig
    "zig": _spec(
        "-h",
        subcommands="help, version, env, targets, zen, libc, std, init",
        scoped={"build": "-l --list-steps"},
        clusters=False,
    ),
    # Rust
    "cargo": _spec(
        "-V -h --list --unit-graph --build-plan",
        overrides="--config",
        build_ok="--no-run",
        subcommands=(
            "version, help, search, info, locate-project, metadata, tree, pkgid, read-manifest,"
            " verify-project, report"
        ),
    ),
    "rustc": _RUSTC_SPEC,
    "clippy-driver": _RUSTC_SPEC,
    # Go
    "go": _spec(
        "-n -h -help",
        overrides="-exec -toolexec",
        build_ok="-c",
        # The test binary also takes its flags as ``-test.<name>``, directly
        # or after ``-args``.
        scoped={"test": "-list -test.list"},
        single_dash_long=True,
        zero_runs="-count -test.count",
        subcommands="version, env, help, doc, list, bug, telemetry",
    ),
    "golangci-lint": _spec(
        "-h --issues-exit-code",
        subcommands=(
            "help, version, linters, completion, cache, config, formatters, migrate, custom"
        ),
        needs_positional=True,
    ),
    "gofmt": _spec("-h -help", single_dash_long=True),
    # Node.js
    "npm": _NODE_PM_SPEC,
    "pnpm": _NODE_PM_SPEC,
    "yarn": _NODE_PM_SPEC,
    "npx": _spec("-v -h", overrides="-c --call", wraps=True),
    "bun": _spec("-v -h --revision", overrides="-e --eval -p --print", wraps=True, forwards=True),
    "node": _spec(
        "-v -h --v8-options --completion-bash",
        overrides="-e --eval -p --print",
        forwards=True,
        needs_positional=True,
    ),
    "deno": _spec(
        "-V -h --rules",
        build_ok="--no-run",
        subcommands="help, info, eval, types, completions, repl, lsp, upgrade",
        needs_positional=True,
    ),
    "tsc": _spec(
        "-v -h --all --showConfig --listFilesOnly --init --noCheck --clean --dry",
        clusters=False,
        case_insensitive=True,
    ),
    "vitest": _spec("-v -h", subcommands="list, init"),
    "jest": _spec(
        "-v -h --listTests --list-tests --showConfig --show-config --init --clearCache"
        " --clear-cache"
    ),
    "mocha": _spec("-V -h --dry-run --dryRun --list-interfaces --list-reporters"),
    "eslint": _spec("-v -h --print-config --env-info --init --inspect-config"),
    "biome": _spec(
        "-h",
        subcommands="version, explain, rage, init, start, stop, clean, lsp-proxy, migrate, search",
        needs_positional=True,
    ),
    "prettier": _spec("-v -h --find-config-path --file-info --support-info"),
    # General build tools
    "make": _MAKE_SPEC,
    "gmake": _MAKE_SPEC,
    "cmake": _spec(
        "-N --system-information --find-package --list-presets",
        overrides="-E",
        prefixes="--help",
        clusters=False,
    ),
    "ninja": _spec("-n -t -h"),
    "bazel": _spec(
        "-h --nobuild --check_tests_up_to_date --check_up_to_date",
        overrides="--run_under --script_path",
        subcommands=(
            "version, help, info, query, cquery, aquery, dump, license, canonicalize-flags,"
            " print_action, analyze-profile, shutdown, clean, fetch, sync, mod, vendor, config"
        ),
        forwards=True,
        needs_positional=True,
    ),
    "buck": _spec(
        "-h",
        subcommands=(
            "targets, query, uquery, cquery, aquery, audit, help, version, root, server, kill,"
            " clean, docs, log, rage, doctor, completion"
        ),
        needs_positional=True,
    ),
    "gradle": _GRADLE_SPEC,
    "gradlew": _GRADLE_SPEC,
    "gradlew.bat": _GRADLE_SPEC,
    "mvn": _MAVEN_SPEC,
    "mvnw": _MAVEN_SPEC,
    "mvnw.cmd": _MAVEN_SPEC,
    "ant": _spec("-p -projecthelp -version -help -h -diagnostics", single_dash_long=True),
    "just": _spec(
        "-l --list --summary -n --dry-run -s --show --evaluate --dump --variables -V -h"
        " --choose --changelog --fmt --init --completions --man --groups",
        overrides="--set",
        assignments=True,
        forwards=True,
    ),
    "task": _spec(
        "-l --list -a --list-all -n --dry --summary --status -h --init",
        assignments=True,
        forwards=True,
    ),
    # Other languages
    "cabal": _spec(
        "-h --numeric-version --dry-run --only-dependencies",
        subcommands=(
            "list, info, help, path, list-bin, exec, user-config, outdated, freeze, gen-bounds,"
            " update, init, clean"
        ),
        needs_positional=True,
    ),
    "stack": _spec(
        "-h --numeric-version --dry-run --only-dependencies",
        build_ok="--no-run-tests",
        subcommands=(
            "ls, path, query, ide, exec, eval, ghci, repl, list, setup, update, upgrade,"
            " uninstall, clean, purge, config, templates, new, init, hoogle, unpack, sdist,"
            " upload, dot"
        ),
        needs_positional=True,
    ),
    "ghc": _spec(
        "-? --numeric-version --info --supported-extensions --supported-languages"
        " --show-options --show-iface",
        overrides="-e",
        prefixes="--print-",
        clusters=False,
    ),
    "dotnet": _spec(
        "-h -? --info --list-sdks --list-runtimes",
        subcommands="list, help, sdk, workload list",
        scoped={"test": "-t --list-tests"},
        clusters=False,
    ),
    "mix": _spec("-v -h", subcommands="help, deps.tree, hex.info"),
    "elixir": _spec("-v -h", overrides="-e --eval", forwards=True, needs_positional=True),
    "swift": _spec(
        "-h -l --list-tests --show-bin-path -version",
        subcommands=(
            "help, package describe, package dump-package, package dump-pif,"
            " package show-dependencies, package init, package clean, package reset,"
            " package purge-cache, package completion-tool, package plugin"
        ),
        clusters=False,
        needs_positional=True,
    ),
    "swiftc": _spec("-version -help -h -print-target-info -###", prefixes="-dump-", clusters=False),
    "xcodebuild": _spec(
        "-version -help -h -usage -list -showsdks -showdestinations -showTestPlans -dry-run"
        " -license -checkFirstLaunchStatus",
        prefixes="-showBuildSettings -find",
        single_dash_long=True,
    ),
    "javac": _spec("-version -help -X --help-extra -? -fullversion", single_dash_long=True),
    "java": _spec(
        "-version -help -h -? -X --list-modules --dry-run --describe-module -fullversion",
        single_dash_long=True,
        forwards=True,
    ),
    "kotlinc": _spec(
        "-version -help -h -X",
        overrides="-e -expression",
        single_dash_long=True,
        needs_positional=True,
    ),
    "clang": _CC_SPEC,
    "gcc": _CC_SPEC,
    "g++": _CC_SPEC,
    "clang-tidy": _spec(
        "-version -help -list-checks -dump-config -explain-config -verify-config"
        " -help-hidden -help-list",
        single_dash_long=True,
    ),
    "clang-format": _spec(
        "-version -help -dump-config -help-hidden -help-list", single_dash_long=True
    ),
    "shellcheck": _spec("-V -h --list-optional"),
    # Ruby
    "ruby": _spec("-h", overrides="-e", prefixes="--dump", forwards=True, needs_positional=True),
    "bundle": _spec("-v -h", wraps=True),
    "rake": _spec(
        "-T --tasks -D --describe -P --prereqs -W --where -n --dry-run -V -h -H",
        overrides="-e --execute -p --execute-print -E --execute-continue",
        assignments=True,
    ),
    "rspec": _spec("--dry-run -v -h --init"),
    "rubocop": _spec(
        "-v -V --verbose-version -h --show-cops --show-docs-url -L --list-target-files"
        " --auto-gen-config --regenerate-todo"
    ),
    # PHP
    "php": _spec(
        "-v -i --info -m --modules -h --ini --rf --rc --re --rz --ri",
        overrides="-r --run",
        clusters=False,
        forwards=True,
        needs_positional=True,
    ),
    "composer": _spec("-V -h --dry-run", forwards=True),
    "phpunit": _spec(
        "-h --check-version --generate-configuration --migrate-configuration --atleast-version"
        " --list-tests --list-tests-xml --list-groups --list-suites --list-test-files",
        prefixes="--list-",
    ),
}

_VALUE_SPLIT = re.compile(r"[=\s,]")
_FORWARDED_FLAGS: frozenset[str] = frozenset(
    {"-V", "-h", *(flag for spec in _MODE_SPECS.values() for flag in spec.union_flags)}
)
_MAVEN_IGNORE_MARKERS: tuple[str, ...] = (
    "failure.ignore",
    "failureignore",
    "fail.never",
    "failifno",
)


def _program_name(token: str) -> str:
    """Executable name a token refers to: ``./mvnw`` -> mvnw, ``eslint@8`` -> eslint."""
    name = Path(token).name
    return name.split("@", 1)[0] if not name.startswith("@") else name


def _flag_candidates(token: str) -> list[str]:
    """Option names a token can carry, including ones embedded in a value.

    ``--collect-only=1`` names ``--collect-only``; ``-o addopts=--co`` and
    ``--test_arg=--list`` smuggle an option inside a value, which the tool
    re-parses, so every ``=``/comma/space-separated piece that looks like an
    option is a candidate too.
    """
    pieces = _VALUE_SPLIT.split(token)
    return [piece for piece in pieces if piece.startswith("-") and piece != "-"]


@dataclass(slots=True)
class _Scope:
    tool: str
    spec: _ModeSpec
    active: bool = True
    expect_module: bool = False
    positionals: list[str] = field(default_factory=list)
    excluded_tasks: list[str] = field(default_factory=list)


def non_verifying_reason(
    parts: Sequence[str],
    key: str | None = None,
) -> str | None:
    """Return why ``parts`` cannot produce Stage 1 evidence, or ``None``.

    ``key`` is the check the command is configured for (``lint``, ``build``,
    ``test`` …); ``None`` means unknown and is treated as strictly as
    ``test``. The argv is walked once. The head and every later token naming
    a known executable (``python -m pytest``, ``uv run pytest``, ``npx
    jest``) open a scope whose spec applies to the tokens after it. A
    wrapper's or forwarder's own options stop at the program it wraps or at
    ``--``; every other tool's options apply to the rest of the line, so
    ``pytest -k go --co`` is still pytest's ``--co``. Runners that forward
    trailing arguments to a script body that is not visible here (``npm
    test -- --listTests``) additionally get every tool's long flags applied.
    ``--version`` and ``--help`` are refused wherever they appear.
    """
    if not parts:
        return None
    head = _program_name(parts[0])
    head_spec = _MODE_SPECS.get(head)
    if head_spec is None:
        # Fail closed: an executable nobody has reviewed for these modes is
        # not evidence. ``test_languages`` keeps this unreachable for the
        # allowlist; anything else is already refused by the allowlist gate.
        return f"{head}: no reviewed entry in _MODE_SPECS"
    build = key == "build"
    scopes = [_Scope(head, head_spec)]
    forwarding = head_spec.forwards
    previous = ""
    value_next = False
    for token in parts[1:]:
        candidates = _flag_candidates(token)
        for candidate in candidates:
            # Every program prints usage for these, including ones with no
            # entry (``python -m unittest --help``, an npm script body).
            if candidate.split("=", 1)[0] in _GENERIC_FLAGS:
                return f"{candidate}"
        for scope in scopes:
            if not scope.active:
                continue
            spec = scope.spec
            extra = frozenset() if build else spec.build_ok
            for candidate in candidates:
                if spec.refuses(candidate, extra):
                    return f"{scope.tool}: {candidate}"
            if spec.assignments and _ASSIGNMENT.match(token):
                return f"{scope.tool}: variable override {token}"
            if spec.skip_properties and _maven_property_skips(token, previous, build):
                return f"{scope.tool}: {token}"
        if forwarding:
            for candidate in candidates:
                if candidate in _FORWARDED_FLAGS:
                    return f"forwarded: {candidate}"
        if value_next:
            # The value of the option before it: ``pytest -k npm -v`` filters on
            # the string "npm" and does not run npm.
            for scope in scopes:
                if (
                    scope.active
                    and scope.spec is _GRADLE_SPEC
                    and previous in {"-x", "--exclude-task"}
                ):
                    scope.excluded_tasks.append(token)
            value_next = False
            previous = token
            continue
        previous = token
        for scope in scopes:
            if scope.active and scope.spec is _GRADLE_SPEC:
                option, separator, value = token.partition("=")
                if separator and option in {"-x", "--exclude-task"}:
                    scope.excluded_tasks.append(value)
                elif token.startswith("-x") and token != "-x":
                    scope.excluded_tasks.append(token[2:])
        value_next = any(
            scope.active and scope.spec.normalize(token) in scope.spec.takes_value
            for scope in scopes
        )
        if token == "--":
            # What follows belongs to the wrapped program or script; the
            # forwarded-flag check above keeps applying to it.
            for scope in scopes:
                if scope.spec.wraps or scope.spec.forwards:
                    scope.active = False
            continue
        if token.startswith("-") or "=" in token:
            for scope in scopes:
                if scope.active and scope.spec.module_option == token:
                    scope.expect_module = True
            continue
        module = False
        for scope in scopes:
            if scope.active:
                scope.positionals.append(token)
                if scope.expect_module:
                    scope.active = False
                    module = True
        # ``python -m pytest.__main__`` runs pytest.
        name = token.split(".", 1)[0] if module else _program_name(token)
        nested = _MODE_SPECS.get(name)
        if nested is not None:
            for scope in scopes:
                if scope.spec.wraps:
                    scope.active = False
            scopes.append(_Scope(name, nested))
            forwarding = forwarding or nested.forwards
    if head_spec.needs_positional and not scopes[0].positionals:
        return f"{head}: no subcommand or program (prints usage or reads stdin)"
    for scope in scopes:
        reason = _positional_reason(scope, parts)
        if reason is not None:
            return reason
    return None


def _positional_reason(scope: _Scope, parts: Sequence[str]) -> str | None:
    """Informational subcommands, and options valid only under one subcommand."""
    leading = scope.positionals[:2]
    for excluded in scope.excluded_tasks:
        for task in scope.positionals:
            if task.lstrip(":") in scope.spec.subcommands:
                continue
            if (
                task.lstrip(":") == excluded.lstrip(":")
                or (":" not in excluded and excluded == task.rsplit(":", 1)[-1])
                or (":" not in task and task == excluded.rsplit(":", 1)[-1])
            ):
                return f"{scope.tool}: excluded checking task {task} (-x {excluded})"
    if scope.spec.task_list:
        # ``gradle clean test`` runs the tests; ``gradle clean tasks`` runs none.
        tasks = scope.positionals
        if tasks and all(task in scope.spec.subcommands for task in tasks):
            return f"{scope.tool}: {' '.join(tasks)}"
    else:
        for name in (*leading, " ".join(leading)):
            if name in scope.spec.subcommands:
                return f"{scope.tool}: {name}"
    for sub, flags in scope.spec.scoped:
        if sub not in leading:
            continue
        previous = ""
        for token in parts:
            for candidate in _flag_candidates(token):
                if scope.spec.normalize(candidate) in flags:
                    return f"{scope.tool} {sub}: {candidate}"
            if _zero_run(scope.spec, previous, token):
                return f"{scope.tool} {sub}: zero repeat count {token}"
            previous = token
    return None


def _zero_run(spec: _ModeSpec, previous: str, token: str) -> bool:
    """``-count=0`` / ``-count 0``: a repeat count of zero runs no test."""
    if not spec.zero_runs:
        return False
    name, sep, value = token.partition("=")
    if not (sep and token.startswith("-")):
        name, value = previous, token
    if not name.startswith("-") or spec.normalize(name) not in spec.zero_runs:
        return False
    return value.strip().isdigit() and int(value) == 0


def _maven_property_skips(token: str, previous: str, build: bool) -> bool:
    """``-Dmaven.test.failure.ignore=true`` always; ``-DskipTests`` unless building.

    ``mvn package -DskipTests`` is a real build check, so skip switches are
    refused only for the other keys; a failure-ignore switch turns any
    failure into success and is refused everywhere.
    """
    if token.startswith("-D") and token != "-D":
        prop = token[2:]
    elif token.startswith("--define="):
        prop = token[len("--define=") :]
    elif previous in {"-D", "--define"}:
        prop = token
    else:
        return False
    name, _, value = prop.partition("=")
    name = name.lower()
    if value.lower() == "false" and "failifno" not in name:
        return False
    if any(marker in name for marker in _MAVEN_IGNORE_MARKERS):
        return True
    return not build and "skip" in name


def _load_project_overrides(working_dir: Path) -> dict[str, Any] | None:
    """Load ``.ouroboros/mechanical.toml`` if it exists.

    Returns the parsed TOML dict, or ``None`` when the file is missing or
    malformed. Never raises.
    """
    config_path = working_dir / ".ouroboros" / "mechanical.toml"

    import tomllib

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except OSError as e:
        # Probe and read through one guarded operation. A separate ``exists()``
        # preflight can itself raise for an inaccessible parent and introduces a
        # race before ``open()``.
        log.warning("mechanical.toml_read_error", path=str(config_path), error=str(e))
        return None
    except Exception as e:
        # "Never raises" is the contract, and this file is optional operator-authored
        # input, so anything that stops it becoming a dict must degrade to defaults
        # rather than abort mechanical-config resolution. Narrowing this to the known
        # exception types has repeatedly missed one: tomllib raises TOMLDecodeError for
        # bad syntax, UnicodeDecodeError for non-UTF-8 bytes, and RecursionError for
        # pathologically nested documents — and only the first two share a base class.
        log.warning("mechanical.toml_parse_error", path=str(config_path), error=str(e))
        return None


def _parse_command(
    value: str,
    *,
    working_dir: Path | None = None,
    key: str | None = None,
) -> tuple[str, ...] | None:
    """Parse a command string into a tuple, or ``None`` if empty/blocked.

    Guarantees:

    * Empty / whitespace-only inputs mean "skip this check".
    * Malformed quoting (unterminated string literals etc.) is swallowed
      rather than raised — Stage 1 must never crash on repo-authored toml.
    * Shell operators (``&&``, pipes, redirects, command substitution) are
      refused so ``mechanical.toml`` cannot smuggle shell constructs past
      ``create_subprocess_exec``.
    * The leading executable is checked against the curated allowlist.
    * When ``working_dir`` is provided, the command is additionally fed
      through the detector's on-disk entry-point validator so hand-authored
      toml cannot bypass the same repo-coupling contract the AI path
      enforces (``npm install`` / ``cargo publish`` / ``/tmp/mvnw`` etc.
      are rejected).
    * A command in a mode that cannot reflect the project (``pytest
      --version``, ``make -n test``) is refused for ``key`` on every path,
      including caller-provided overrides; see :func:`non_verifying_reason`.
    """
    value = value.strip()
    if not value:
        return None
    if any(token in value for token in ("&&", "||", "|", ";", ">", "<", "`", "$(")):
        log.warning("mechanical.toml_shell_operator_blocked", command=value)
        return None
    try:
        parts = tuple(split_command(value))
    except ValueError as exc:
        log.warning("mechanical.toml_parse_error", command=value, error=str(exc))
        return None
    if not parts:
        return None
    head = parts[0]
    # Reject absolute paths and other non-allowlisted forms. Basename-only
    # allowlisting still accepts project-local wrappers like ``./mvnw``
    # because ``Path("./mvnw").name == "mvnw"`` matches the allowlist.
    if PureWindowsPath(head).anchor or head.startswith("~"):
        log.warning("mechanical.toml_absolute_path_blocked", command=value)
        return None
    executable = Path(head).name
    if executable not in _ALLOWED_EXECUTABLES:
        log.warning(
            "mechanical.blocked_executable",
            executable=executable,
            command=value,
            hint="Add to _ALLOWED_EXECUTABLES or revise mechanical.toml",
        )
        return None
    reason = non_verifying_reason(parts, key)
    if reason is not None:
        log.warning("mechanical.toml_non_verifying_command", command=value, reason=reason)
        return None
    if working_dir is not None:
        # Lazy import to avoid a circular dep: detector imports
        # ``_ALLOWED_EXECUTABLES`` from this module.
        from ouroboros.evaluation.detector import _command_is_valid

        if not _command_is_valid(working_dir, value, key):
            log.warning(
                "mechanical.toml_entry_point_invalid",
                command=value,
                working_dir=str(working_dir),
            )
            return None
    return parts


def _apply_overrides(
    current: dict[str, Any],
    source: dict[str, Any],
    *,
    working_dir: Path | None = None,
) -> None:
    """Merge ``source`` command/threshold entries into ``current``.

    ``working_dir`` is forwarded to :func:`_parse_command` so file-loaded
    overrides (``.ouroboros/mechanical.toml``) get the same entry-point
    validation the detector applies; explicit caller-provided overrides
    pass ``None`` because they are trusted MCP inputs that have already
    been vetted upstream.
    """
    for key in ("lint", "build", "test", "static", "coverage"):
        if key in source:
            current[key] = _parse_command(str(source[key]), working_dir=working_dir, key=key)
    if "timeout" in source:
        try:
            current["timeout"] = int(source["timeout"])
        except (TypeError, ValueError):
            log.warning("mechanical.toml_bad_timeout", value=source["timeout"])
    if "coverage_threshold" in source:
        try:
            current["coverage_threshold"] = float(source["coverage_threshold"])
        except (TypeError, ValueError):
            log.warning(
                "mechanical.toml_bad_coverage_threshold",
                value=source["coverage_threshold"],
            )


def build_mechanical_config(
    working_dir: Path,
    overrides: dict[str, Any] | None = None,
) -> MechanicalConfig:
    """Build a ``MechanicalConfig`` from ``mechanical.toml`` plus overrides.

    Priority (highest first):
        1. Explicit ``overrides`` dict (MCP caller)
        2. ``.ouroboros/mechanical.toml`` authored by the detector or user
        3. All commands ``None`` → Stage 1 skips every check

    Args:
        working_dir: Project root directory.
        overrides: Optional dict of command overrides.

    Returns:
        Deterministic ``MechanicalConfig`` with ``working_dir`` set.
    """
    current: dict[str, Any] = {
        "lint": None,
        "build": None,
        "test": None,
        "static": None,
        "coverage": None,
        "timeout": 300,
        "coverage_threshold": 0.7,
    }

    file_overrides = _load_project_overrides(working_dir)
    if file_overrides:
        _apply_overrides(current, file_overrides, working_dir=working_dir)

    if overrides:
        _apply_overrides(current, overrides)

    return MechanicalConfig(
        lint_command=current["lint"],
        build_command=current["build"],
        test_command=current["test"],
        static_command=current["static"],
        coverage_command=current["coverage"],
        timeout_seconds=current["timeout"],
        coverage_threshold=current["coverage_threshold"],
        working_dir=working_dir,
    )


@dataclass(frozen=True, slots=True)
class LanguagePreset:
    """Deprecated legacy preset shape.

    Ouroboros 0.29+ stopped shipping per-language presets; Stage 1 reads
    ``.ouroboros/mechanical.toml`` directly. This dataclass remains only
    so that external callers that import ``LanguagePreset`` continue to
    load. Every field defaults to ``None`` and the type does not feed
    into Stage 1 resolution.
    """

    name: str = ""
    lint_command: tuple[str, ...] | None = None
    build_command: tuple[str, ...] | None = None
    test_command: tuple[str, ...] | None = None
    static_command: tuple[str, ...] | None = None
    coverage_command: tuple[str, ...] | None = None


def detect_language(working_dir: Path) -> LanguagePreset | None:
    """Deprecated compatibility shim that bridges callers to mechanical.toml.

    Preserves the old preset-shaped return: if a
    ``.ouroboros/mechanical.toml`` already exists, the shim reads it via
    :func:`build_mechanical_config` and packages the resolved commands into
    a :class:`LanguagePreset` so legacy callers still receive runnable
    Stage 1 commands. When the toml is absent, returns ``None`` — the
    ambiguity marker the old preset detector used.

    Emits a :class:`DeprecationWarning` on every call so third-party
    callers migrate to
    :func:`ouroboros.evaluation.detector.ensure_mechanical_toml` plus
    :func:`build_mechanical_config` for the full contract.
    """
    import warnings

    warnings.warn(
        "ouroboros.evaluation.detect_language() is deprecated; call "
        "ensure_mechanical_toml() + build_mechanical_config() instead. "
        "This shim now reads .ouroboros/mechanical.toml when present and "
        "returns None when no commands are configured.",
        DeprecationWarning,
        stacklevel=2,
    )
    config = build_mechanical_config(working_dir)
    if not any(
        (
            config.lint_command,
            config.build_command,
            config.test_command,
            config.static_command,
            config.coverage_command,
        )
    ):
        return None
    return LanguagePreset(
        name="mechanical-toml",
        lint_command=config.lint_command,
        build_command=config.build_command,
        test_command=config.test_command,
        static_command=config.static_command,
        coverage_command=config.coverage_command,
    )


__all__ = [
    "LanguagePreset",
    "build_mechanical_config",
    "detect_language",
]
