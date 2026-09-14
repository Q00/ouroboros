"""CI-enforced invariants for the untrusted-``.env`` → child-process trust boundary.

Every Ouroboros advisory to date (CVE-2026-47211, CVE-2026-66065,
GHSA-wvgf-hr9x-v3g6, GHSA-7j6g-gw2r-mw48) is one class: the project-directory
``.env`` — which travels with whatever repository the operator cloned — sets an
environment key that a process Ouroboros spawns honours as an executable path,
a config/home root, a dynamic or module loader control, a shell startup hook,
or a package-manager configuration. ``config/loader.py`` filters that file with
``untrusted_env.is_untrusted_env_denied_key`` and nothing else; every spawn site
then inherits whatever survived.

Two invariants turn that class into something a PR cannot regress silently:

1. **Every child-process spawn site** in ``src/ouroboros`` either passes an
   explicit environment built by a *named* helper (``runtime.child_env.
   build_child_env`` or another reviewed builder) or is listed in
   :data:`SPAWN_SITE_ALLOWLIST` with a one-line justification. A new site that
   inherits ``os.environ`` verbatim fails CI until someone consciously
   allowlists it.
2. **A catalog of known loader / executable-selector keys** is denied. Cases
   the sibling PR ``fix/untrusted-env-runtime-loader`` fixes are marked
   ``xfail(strict=True)`` so this branch is green now and turns into a hard
   failure — "un-xfail me" — the moment that PR merges.

Discovery is AST-based (mirroring ``tests/unit/config/test_loader_env.py``) so
new call sites are found without a hand-maintained roster; the rosters that do
exist here are the *allowlists*, and each entry must still match a real site so
they cannot rot.

Simplifications, stated so nobody mistakes this for dataflow analysis:

* invariant 1 classifies the ``env=`` expression syntactically (helper call
  name, local assignment, parameter, ``**kwargs``); it does not prove what the
  helper strips. ``build_child_env`` and the per-backend ``_build_child_env`` /
  ``_build_env`` copies are *recursion-guard chokepoints* that start from
  ``os.environ``; only ``sanitized_verify_environment`` and
  ``project_identity._git_environment`` remove loader-class keys. The named
  chokepoint is what the invariant pins; sanitizing inside it is the
  denylist's job.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import ouroboros
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key
from ouroboros.orchestrator import verify_shell
from ouroboros.runtime import child_env

_PACKAGE_ROOT = Path(ouroboros.__file__).resolve().parent
_REPO_SRC_PREFIX = "src/ouroboros"


def _rel(path: Path) -> str:
    return f"{_REPO_SRC_PREFIX}/{path.relative_to(_PACKAGE_ROOT).as_posix()}"


def _iter_package_sources() -> Iterator[Path]:
    for source in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if source.is_file():
            yield source


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


# ---------------------------------------------------------------------------
# Invariant 1 — every spawn site names its environment builder or is allowlisted
# ---------------------------------------------------------------------------

#: Callees that create a child process. ``subprocess`` wrappers that only take
#: ``env=`` as a keyword are listed with ``None``; ``os.exec*e`` / ``os.spawn*e``
#: / ``os.posix_spawn*`` take the environment positionally at the given index.
SPAWN_CALLEES: dict[str, int | None] = {
    "subprocess.Popen": None,
    "subprocess.run": None,
    "subprocess.call": None,
    "subprocess.check_call": None,
    "subprocess.check_output": None,
    "asyncio.create_subprocess_exec": None,
    "asyncio.create_subprocess_shell": None,
    "os.system": None,
    "os.execv": None,
    "os.execvp": None,
    "os.execl": None,
    "os.execlp": None,
    "os.execve": 2,
    "os.execvpe": 2,
    "os.spawnv": None,
    "os.spawnvp": None,
    "os.spawnl": None,
    "os.spawnlp": None,
    "os.spawnve": 3,
    "os.spawnvpe": 3,
    "os.posix_spawn": 2,
    "os.posix_spawnp": 2,
}

#: Reviewed helpers whose *return value* may be passed as a child environment.
#: Matched on the final attribute of the call (``self._build_child_env()`` →
#: ``_build_child_env``). Adding a name here is a security review, not a fix:
#: the helper must start from a copy and pop / filter keys, never hand back
#: ``os.environ`` itself. :func:`test_named_env_builders_exist` keeps the roster
#: honest.
NAMED_ENV_BUILDERS: frozenset[str] = frozenset(
    {
        # The shared recursion-guard chokepoint (runtime/child_env.py).
        "build_child_env",
        # Per-backend wrappers over build_child_env (codex/copilot/kiro cli_policy).
        "build_codex_child_env",
        "build_copilot_child_env",
        "build_kiro_child_env",
        # Per-backend methods. Some route to build_child_env, some are
        # ``os.environ.copy()`` + pop (goose, omp, pi, dsh, ourocode) — named
        # chokepoints, not loader-key sanitizers; see the module docstring.
        "_build_child_env",
        "_build_env",
        "_child_env",
        "_build_cli_child_env",
        "_build_env_for_instance",
        # The AC verify gate: strips loader / shell-startup keys.
        "sanitized_verify_environment",
        # git helpers: non-interactive prompts / GIT_* stripped respectively.
        "git_noninteractive_env",
        "_git_environment",
    }
)


@dataclass(frozen=True, slots=True)
class SpawnSite:
    """One child-process creation site and how its environment was built."""

    file: str
    line: int
    function: str
    callee: str
    env_source: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.function, self.callee)

    @property
    def builder(self) -> str:
        return self.env_source.rsplit(".", 1)[-1]

    @property
    def uses_named_builder(self) -> bool:
        if self.env_source.startswith("either(") and self.env_source.endswith(")"):
            alternatives = self.env_source[len("either(") : -1].split("|")
            return all(alt.rsplit(".", 1)[-1] in NAMED_ENV_BUILDERS for alt in alternatives)
        return self.builder in NAMED_ENV_BUILDERS


def _last_assignment_before(name: str, function: ast.AST | None, line: int) -> ast.AST | str | None:
    """Return the value last assigned to ``name`` in ``function`` before ``line``.

    Returns the string ``"parameter"`` when the name is a parameter of the
    enclosing function and no assignment precedes the call.
    """
    if function is None:
        return None
    candidate: ast.AST | None = None
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.lineno >= line:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                if candidate is None or node.lineno > candidate.lineno:
                    candidate = node
    if candidate is not None:
        return candidate.value
    args = function.args
    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        parameters.append(args.vararg)
    if args.kwarg:
        parameters.append(args.kwarg)
    if any(parameter.arg == name for parameter in parameters):
        return "parameter"
    return None


def _describe_env_expr(
    expr: ast.AST | str | None, function: ast.AST | None, line: int, depth: int = 0
) -> str:
    if expr is None:
        return "inherit"
    if isinstance(expr, str):
        return expr
    if isinstance(expr, ast.Call):
        callee = _dotted(expr.func) or "<call>"
        if callee == "dict" and expr.args and not expr.keywords:
            inner = _describe_env_expr(expr.args[0], function, line, depth + 1)
            return f"dict({inner})"
        return callee
    if isinstance(expr, ast.Name):
        if depth > 4:
            return "unresolved"
        resolved = _last_assignment_before(expr.id, function, line)
        if resolved is None:
            return "unresolved"
        return _describe_env_expr(resolved, function, line, depth + 1)
    if isinstance(expr, ast.Dict):
        if any(key is None for key in expr.keys):
            spread = [
                _describe_env_expr(value, function, line, depth + 1)
                for key, value in zip(expr.keys, expr.values, strict=True)
                if key is None
            ]
            return "merge(" + ",".join(spread) + ")"
        return "dict-literal"
    if isinstance(expr, ast.DictComp):
        return "dict-comprehension"
    if isinstance(expr, ast.IfExp):
        body = _describe_env_expr(expr.body, function, line, depth + 1)
        orelse = _describe_env_expr(expr.orelse, function, line, depth + 1)
        return body if body == orelse else f"either({body}|{orelse})"
    if isinstance(expr, ast.Attribute):
        return _dotted(expr) or "unresolved"
    return "unresolved"


def _discover_spawn_sites(root: Path) -> tuple[SpawnSite, ...]:
    sites: list[SpawnSite] = []
    for source in sorted(root.rglob("*.py")):
        if not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _dotted(node.func)
            if callee not in SPAWN_CALLEES:
                continue
            function = _enclosing_function(node, parents)
            env_expr: ast.AST | str | None = None
            keyword = next((kw for kw in node.keywords if kw.arg == "env"), None)
            positional_index = SPAWN_CALLEES[callee]
            if keyword is not None:
                env_expr = keyword.value
            elif positional_index is not None and len(node.args) > positional_index:
                env_expr = node.args[positional_index]
            elif any(kw.arg is None for kw in node.keywords):
                env_expr = "opaque-kwargs"
            env_source = _describe_env_expr(env_expr, function, node.lineno)
            if root == _PACKAGE_ROOT:
                file = _rel(source)
            else:
                file = source.relative_to(root).as_posix()
            sites.append(
                SpawnSite(
                    file=file,
                    line=node.lineno,
                    function=getattr(function, "name", "<module>"),
                    callee=callee,
                    env_source=env_source,
                )
            )
    return tuple(sites)


#: Spawn sites that do NOT pass a named-builder environment, each with the
#: reason a reviewer accepted it. Keyed by (file, enclosing function, callee)
#: so an unrelated edit that shifts line numbers does not churn this table.
#: An entry that no longer matches any site fails
#: :func:`test_spawn_site_allowlist_has_no_stale_entries`.
#:
#: "inherit" = no ``env=`` at all (child gets ``os.environ``);
#: "os.environ.copy" / "merge(os.environ)" = copy plus edits;
#: "parameter" = env passed in by the caller. Sites whose child honours a
#: loader-class key that the denylist does not yet reject are cross-referenced
#: to docs/security/TRIAGE.md so the allowlist never silently absorbs a finding.
SPAWN_SITE_ALLOWLIST: dict[tuple[str, str, str], str] = {
    # -- Python interpreters running our own modules ------------------------
    ("src/ouroboros/mcp/detached_jobs.py", "_spawn_worker", "subprocess.Popen"): (
        "os.environ.copy()+marker via **kwargs; fixed `sys.executable -m` argv. "
        "PYTHON* inheritance is"
    ),
    ("src/ouroboros/dashboard_web/daemon.py", "_spawn_detached", "subprocess.Popen"): (
        "implicit inherit; fixed `sys.executable -m` argv, detached daemon. PYTHON* inheritance is"
    ),
    (
        "src/ouroboros/providers/litellm_adapter.py",
        "_run_isolated_completion",
        "asyncio.create_subprocess_exec",
    ): "explicit two-key literal env (PYTHONIOENCODING/PYTHONUTF8) and `-I`; reference pattern.",
    # -- mechanical evaluation of repo-declared commands ---------------------
    ("src/ouroboros/evaluation/mechanical.py", "run_command", "asyncio.create_subprocess_exec"): (
        "os.environ.copy() minus _OUROBOROS_NESTED; runs .ouroboros/mechanical.toml "
        "commands. Not routed through sanitized_verify_environment:"
    ),
    # -- verify gate plumbing (env supplied by the three sanitized callers) --
    (
        "src/ouroboros/orchestrator/verify_command_runner.py",
        "_run_process",
        "asyncio.create_subprocess_exec",
    ): (
        "parameter passthrough `dict(env)`; every caller passes "
        "sanitized_verify_environment() (verify_shell, leaf_dispatcher, parallel_executor)."
    ),
    # -- login-shell env import ---------------------------------------------
    ("src/ouroboros/cli/commands/mcp.py", "_ensure_shell_env", "subprocess.run"): (
        "implicit inherit; spawns `$SHELL -l -c` to dump the login environment. "
        "SHELL and ZDOTDIR are not denied from the untrusted .env; open question in THREAT_MODEL.md."
    ),
    # -- launchers / relaunches ---------------------------------------------
    ("src/ouroboros/config_tui/launcher.py", "_relaunch_with_tui_profile", "os.execvpe"): (
        "os.environ.copy()+bootstrap marker; `uvx --isolated` relaunch of ourselves. "
        "XDG_DATA_HOME concern is"
    ),
    ("src/ouroboros/cli/commands/tui.py", "open_command", "subprocess.Popen"): (
        "implicit inherit; opens a terminal emulator with a shlex-quoted argv built "
        "from operator flags."
    ),
    ("src/ouroboros/cli/commands/tui.py", "_run_slt_backend", "subprocess.call"): (
        "implicit inherit (Windows); argv[0] resolved by shutil.which on the denied PATH."
    ),
    ("src/ouroboros/cli/commands/tui.py", "_run_slt_backend", "os.execv"): (
        "implicit inherit (POSIX exec of the Rust TUI); same resolution as above."
    ),
    ("src/ouroboros/cli/commands/update.py", "_run_step", "subprocess.run"): (
        "{**os.environ, UV_TOOL_DIR|PIPX_HOME}; uv/pipx self-update. PIP_/PIPX_/UV_ "
        "prefixes are denied from the untrusted .env."
    ),
    ("src/ouroboros/cli/commands/update.py", "_installed_version", "subprocess.run"): (
        "implicit inherit; `<console> --version` probe of our own install."
    ),
    # -- codex doctor / MCP roster probes (roster root CODEX_HOME is denied) --
    (
        "src/ouroboros/cli/commands/codex.py",
        "_list_stdio_mcp_tool_names_with_framing",
        "asyncio.create_subprocess_exec",
    ): "os.environ.copy()+server env from $CODEX_HOME/config.toml (denied root); doctor probe.",
    ("src/ouroboros/cli/windows_codex_mcp.py", "_launcher_is_usable", "subprocess.run"): (
        "implicit inherit; `<launcher> --help` probe with a code-owned argv."
    ),
    ("src/ouroboros/cli/commands/setup.py", "_codex_release_mcp_launcher", "subprocess.run"): (
        "implicit inherit; `ouroboros mcp serve --help` probe of our own console script."
    ),
    # -- vendor CLI `--version` / config probes (argv code-owned, PATH denied) --
    ("src/ouroboros/backends/model_catalog.py", "refresh_models", "subprocess.run"): (
        "implicit inherit; `<cli> <list-args>` model listing with table-driven argv."
    ),
    ("src/ouroboros/cli/omp_config.py", "configure_omp_tool_call_timeout", "subprocess.run"): (
        "implicit inherit; `omp config get|set <literal>` (two spawns in one function)."
    ),
    ("src/ouroboros/cli/opencode_config.py", "_debug_paths_config_dir", "subprocess.run"): (
        "implicit inherit; `opencode debug paths` probe. APPDATA root gap is"
    ),
    ("src/ouroboros/copilot/model_discovery.py", "_resolve_token", "subprocess.run"): (
        "implicit inherit; `gh auth token`."
    ),
    (
        "src/ouroboros/orchestrator/cli_version_attestation.py",
        "probe_cli_executable_version_attestation",
        "subprocess.run",
    ): "implicit inherit; `<attested-path> --version` after filesystem identity check.",
    (
        "src/ouroboros/orchestrator/pi_runtime.py",
        "_probe_pi_native_param_flags",
        "subprocess.run",
    ): ("implicit inherit; `<pi> --help` one-shot capability probe."),
    ("src/ouroboros/providers/codex_cli_adapter.py", "_codex_version", "subprocess.run"): (
        "implicit inherit; `<codex> --version` diagnostic."
    ),
    (
        "src/ouroboros/orchestrator/runtime_evidence.py",
        "run",
        "subprocess.run",
    ): "implicit inherit; HeadlessRunProbe command. Command source is",
    # -- process-table / system probes (fixed argv, output never a verdict) --
    ("src/ouroboros/cli/commands/mcp.py", "_ps_value", "subprocess.run"): (
        "implicit inherit; `ps -p <pid> -o <literal column>=` (start time / ppid)."
    ),
    ("src/ouroboros/cli/commands/mcp_doctor.py", "_pid_is_alive", "subprocess.run"): (
        "implicit inherit; Windows `tasklist /FI` liveness probe."
    ),
    ("src/ouroboros/orchestrator/heartbeat.py", "_get_process_start_time", "subprocess.run"): (
        "implicit inherit; `ps -p <pid> -o lstart=`."
    ),
    (
        "src/ouroboros/orchestrator/evidence/system.py",
        "_get_available_memory_gb",
        "subprocess.run",
    ): ("implicit inherit; `vm_stat`."),
    (
        "src/ouroboros/orchestrator/opencode_runtime.py",
        "_cleanup_windows_child_processes",
        "subprocess.run",
    ): ("implicit inherit; Windows `wmic process ... delete` cleanup of our own child."),
    # -- git (implicit inherit; GIT_* denial is the sibling PR:
    ("src/ouroboros/auto/checkpoint_commits.py", "_staged_changes", "subprocess.run"): (
        "implicit inherit; `git diff --cached`. GIT_* gap:"
    ),
    ("src/ouroboros/auto/checkpoint_commits.py", "_git", "subprocess.run"): (
        "implicit inherit; `git add/commit`. GIT_* gap:"
    ),
    ("src/ouroboros/bigbang/brownfield.py", "_origin_remote_url", "subprocess.run"): (
        "implicit inherit; `git -C <path> remote get-url origin`."
    ),
    ("src/ouroboros/bigbang/brownfield.py", "_is_git_worktree", "subprocess.run"): (
        "implicit inherit; `git -C <path> rev-parse HEAD`."
    ),
    ("src/ouroboros/core/git_workflow.py", "is_on_protected_branch", "subprocess.run"): (
        "implicit inherit; `git rev-parse --abbrev-ref HEAD`."
    ),
    ("src/ouroboros/core/pm_snapshot.py", "_run_git", "subprocess.run"): (
        "implicit inherit; read-only git snapshot commands."
    ),
    ("src/ouroboros/core/worktree.py", "_run_git_process", "subprocess.run"): (
        "implicit inherit; `git worktree add/remove/status`. GIT_* gap:"
    ),
    ("src/ouroboros/core/worktree.py", "_run_git_bytes", "subprocess.run"): (
        "implicit inherit; git plumbing with bytes output. GIT_* gap:"
    ),
    ("src/ouroboros/evolution/frugality.py", "_git", "subprocess.run"): (
        "implicit inherit; `git -C <path> status --porcelain`."
    ),
    ("src/ouroboros/orchestrator/context_pack.py", "_git_head", "subprocess.run"): (
        "implicit inherit; `git rev-parse HEAD`."
    ),
    ("src/ouroboros/orchestrator/n_version_tournament.py", "_git", "subprocess.run"): (
        "implicit inherit; tournament worktree git helper."
    ),
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "export_worktree_diff",
        "subprocess.run",
    ): "implicit inherit; `git diff --binary HEAD` / `git ls-files` / `git diff --no-index`.",
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "apply_diff_to_workspace",
        "subprocess.run",
    ): ("implicit inherit; `git apply` of a tournament winner."),
    (
        "src/ouroboros/orchestrator/workspace_evidence_paths.py",
        "load_tracked_workspace_paths",
        "subprocess.run",
    ): "implicit inherit; `git ls-files`.",
    (
        "src/ouroboros/orchestrator/workspace_evidence_paths.py",
        "load_ignored_workspace_paths",
        "subprocess.run",
    ): "implicit inherit; `git ls-files --ignored`.",
    (
        "src/ouroboros/tui/screens/lineage_detail.py",
        "_perform_rewind",
        "asyncio.create_subprocess_exec",
    ): "implicit inherit; `git status/rev-parse/checkout <tag>` from the TUI rewind flow.",
}


def _spawn_sites() -> tuple[SpawnSite, ...]:
    return _discover_spawn_sites(_PACKAGE_ROOT)


def test_spawn_site_discovery_is_non_vacuous() -> None:
    """The enumerator must keep finding the sites the invariant exists for."""
    keys = {site.key for site in _spawn_sites()}
    assert ("src/ouroboros/mcp/detached_jobs.py", "_spawn_worker", "subprocess.Popen") in keys
    assert (
        "src/ouroboros/orchestrator/verify_shell.py",
        "_executes_bash_c_semantics",
        "subprocess.run",
    ) in keys
    assert any(
        site.file == "src/ouroboros/providers/claude_code_adapter.py"
        and site.callee == "asyncio.create_subprocess_exec"
        for site in _spawn_sites()
    ), "claude_code_adapter no longer spawns via asyncio.create_subprocess_exec"
    assert len(keys) >= 40, "spawn-site count collapsed — discovery contract drifted"


#: Bare names whose ``from x import name`` form would let a spawn or an env
#: read slip past the dotted-callee discovery above.
_QUALIFIED_ONLY_IMPORTS: dict[str, frozenset[str]] = {
    "subprocess": frozenset(
        callee.split(".", 1)[1] for callee in SPAWN_CALLEES if callee.startswith("subprocess.")
    ),
    "asyncio": frozenset(
        callee.split(".", 1)[1] for callee in SPAWN_CALLEES if callee.startswith("asyncio.")
    ),
    "os": frozenset(callee.split(".", 1)[1] for callee in SPAWN_CALLEES if callee.startswith("os."))
    | frozenset({"environ", "getenv", "putenv"}),
}


def _discover_bare_imports(root: Path) -> list[tuple[str, int, str]]:
    offenders: list[tuple[str, int, str]] = []
    for source in sorted(root.rglob("*.py")):
        if not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in _QUALIFIED_ONLY_IMPORTS:
                continue
            for alias in node.names:
                if alias.name in _QUALIFIED_ONLY_IMPORTS[node.module]:
                    file = (
                        _rel(source)
                        if root == _PACKAGE_ROOT
                        else source.relative_to(root).as_posix()
                    )
                    offenders.append((file, node.lineno, f"from {node.module} import {alias.name}"))
    return offenders


def test_spawn_and_environ_names_are_only_used_module_qualified() -> None:
    """Discovery keys on dotted callees; a bare import would make a site invisible.

    Write ``subprocess.run(...)`` / ``os.environ[...]``, not
    ``from subprocess import run`` / ``from os import environ``.
    """
    offenders = _discover_bare_imports(_PACKAGE_ROOT)
    assert not offenders, "\n".join(f"{f}:{line}: {stmt}" for f, line, stmt in offenders)


def test_bare_import_discovery_catches_evasion(tmp_path: Path) -> None:
    (tmp_path / "evasive.py").write_text(
        "from subprocess import run\nfrom os import environ\nrun(['x'], env=environ)\n",
        encoding="utf-8",
    )
    offenders = _discover_bare_imports(tmp_path)
    assert [stmt for _, _, stmt in offenders] == [
        "from subprocess import run",
        "from os import environ",
    ]


def test_every_spawn_site_names_its_env_builder_or_is_allowlisted() -> None:
    """A new spawn site inheriting os.environ verbatim must be consciously allowlisted."""
    offenders = [
        f"{site.file}:{site.line} {site.function}() {site.callee} env={site.env_source}"
        for site in _spawn_sites()
        if not site.uses_named_builder and site.key not in SPAWN_SITE_ALLOWLIST
    ]
    assert not offenders, (
        "child-process spawn site(s) without a named env builder "
        "(runtime.child_env.build_child_env / sanitized_verify_environment / …) and "
        "not in SPAWN_SITE_ALLOWLIST — route the env through a builder or add an "
        "allowlist entry with a justification:\n  " + "\n  ".join(offenders)
    )


def test_spawn_site_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still match a real site, so the table cannot rot."""
    live = {site.key for site in _spawn_sites()}
    stale = sorted(key for key in SPAWN_SITE_ALLOWLIST if key not in live)
    assert not stale, f"SPAWN_SITE_ALLOWLIST entries match no spawn site: {stale}"


def test_spawn_site_allowlist_does_not_cover_named_builder_sites() -> None:
    """Allowlisting a site that already uses a builder hides a future regression."""
    covered_builder_sites = sorted(
        f"{site.file}:{site.line}"
        for site in _spawn_sites()
        if site.uses_named_builder and site.key in SPAWN_SITE_ALLOWLIST
    )
    assert not covered_builder_sites, covered_builder_sites


def test_named_env_builders_exist() -> None:
    """Every roster name resolves to a real definition, so the roster cannot go stale."""
    defined: set[str] = set()
    for source in _iter_package_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
    missing = sorted(NAMED_ENV_BUILDERS - defined)
    assert not missing, f"NAMED_ENV_BUILDERS names nothing defined in src: {missing}"
    assert callable(child_env.build_child_env)
    assert callable(verify_shell.sanitized_verify_environment)


def test_named_env_builders_never_return_os_environ_itself() -> None:
    """A builder must copy: handing back ``os.environ`` would let a child mutate the parent."""
    for source in _iter_package_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in NAMED_ENV_BUILDERS:
                continue
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    assert _dotted(ret.value) != "os.environ", (
                        f"{_rel(source)}:{ret.lineno} {node.name} returns os.environ uncopied"
                    )


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "import subprocess, os\ndef f():\n    subprocess.run(['x'])\n",
            "inherit",
        ),
        (
            "import subprocess, os\ndef f():\n    subprocess.run(['x'], env=os.environ.copy())\n",
            "os.environ.copy",
        ),
        (
            "import subprocess, os\n"
            "def f():\n"
            "    env = os.environ.copy()\n"
            "    env['A'] = '1'\n"
            "    subprocess.run(['x'], env=env)\n",
            "os.environ.copy",
        ),
        (
            "import subprocess\n"
            "from ouroboros.runtime.child_env import build_child_env\n"
            "def f():\n"
            "    env = build_child_env(depth_error_factory=RuntimeError)\n"
            "    subprocess.run(['x'], env=env)\n",
            "build_child_env",
        ),
        (
            "import subprocess\ndef f(env):\n    subprocess.run(['x'], env=dict(env))\n",
            "dict(parameter)",
        ),
        (
            "import os\n"
            "def f(uvx, args):\n"
            "    env = os.environ.copy()\n"
            "    os.execvpe(uvx, args, env)\n",
            "os.environ.copy",
        ),
        (
            "import subprocess\ndef f(kwargs):\n    subprocess.Popen(['x'], **kwargs)\n",
            "opaque-kwargs",
        ),
        (
            "import subprocess, os\n"
            "def f(o):\n"
            "    subprocess.run(['x'], env={**os.environ, **o})\n",
            "merge(os.environ,parameter)",
        ),
    ),
)
def test_spawn_env_classification_covers_supported_shapes(
    tmp_path: Path, source: str, expected: str
) -> None:
    """The classifier recognises each env-passing shape the package uses."""
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    (site,) = _discover_spawn_sites(tmp_path)
    assert site.env_source == expected


def test_spawn_site_discovery_finds_new_modules(tmp_path: Path) -> None:
    """A new module with a bare spawn is found without touching any roster."""
    module = tmp_path / "future" / "runner.py"
    module.parent.mkdir()
    module.write_text(
        "import asyncio\n"
        "async def go(cmd):\n"
        "    return await asyncio.create_subprocess_exec(*cmd)\n",
        encoding="utf-8",
    )
    (site,) = _discover_spawn_sites(tmp_path)
    assert site == SpawnSite(
        file="future/runner.py",
        line=3,
        function="go",
        callee="asyncio.create_subprocess_exec",
        env_source="inherit",
    )
    assert not site.uses_named_builder


# ---------------------------------------------------------------------------
# Invariant 2 — catalog of runtime-loader / executable-selector keys is denied
# ---------------------------------------------------------------------------

#: Keys every reviewer agrees must never come from a cloned repository's .env.
#: Denied on this branch today.
LOADER_KEY_CATALOG_DENIED: tuple[str, ...] = (
    "PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "NODE_OPTIONS",
    "BASH_ENV",
    "ENV",
    "BASH_FUNC_pytest%%",
    "BASH_FUNC_git%%",
    "SHELLOPTS",
    "BASHOPTS",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_CONFIG_FILE",
    "PIPX_HOME",
    "PIPX_BIN_DIR",
    "UV_INDEX_URL",
    "UV_TOOL_DIR",
    "UV_PYTHON",
    "UV_CONFIG_FILE",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "XDG_CONFIG_HOME",
    "CODEX_HOME",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OUROBOROS_VERIFY_BASH",
    "OUROBOROS_MCP_CONFIG",
    "OUROBOROS_PLUGIN_LOCKFILE",
    "OUROBOROS_PLUGIN_TRUST_ROOT",
    "OUROBOROS_AGENTS_DIR",
    "OUROBOROS_TOOL_CAPABILITIES",
    "OUROBOROS_ALLOW_LOCAL_TRANSPORT",
    "OUROBOROS_CLI",
    "OUROBOROS_CLI_PATH",
    "OPENCODE_CLI_PATH",
)

#: Keys the sibling PR `fix/untrusted-env-runtime-loader` adds. Each case is
#: ``xfail(strict=True)``: green now, and a hard failure ("un-xfail me: move the
#: key to LOADER_KEY_CATALOG_DENIED") as soon as that PR merges into this
#: branch's base. Do NOT fix these here — the sibling PR owns the change.
LOADER_KEY_CATALOG_PENDING_SIBLING_PR: tuple[str, ...] = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "NODE_PATH",
    "NPM_CONFIG_SCRIPT_SHELL",
    "NPM_CONFIG_REGISTRY",
    "YARN_REGISTRY",
    "PNPM_HOME",
    "COREPACK_HOME",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_GLOBAL",
    "GIT_DIR",
    "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF",
)

_SIBLING_PR_REASON = "fixed by fix/untrusted-env-runtime-loader PR"


def _catalog_cases() -> list[object]:
    cases: list[object] = [pytest.param(key, id=key) for key in LOADER_KEY_CATALOG_DENIED]
    cases.extend(
        pytest.param(key, id=key, marks=pytest.mark.xfail(strict=True, reason=_SIBLING_PR_REASON))
        for key in LOADER_KEY_CATALOG_PENDING_SIBLING_PR
    )
    return cases


@pytest.mark.parametrize("key", _catalog_cases())
def test_loader_and_executable_selector_catalog_is_denied(key: str) -> None:
    assert is_untrusted_env_denied_key(key), f"{key} must be denied from the untrusted .env"


@pytest.mark.parametrize("key", LOADER_KEY_CATALOG_DENIED)
def test_loader_catalog_denial_is_case_insensitive(key: str) -> None:
    """The loader upper-cases before matching; a lowercase spelling must not slip through."""
    assert is_untrusted_env_denied_key(key.lower())


def test_catalog_rosters_are_disjoint() -> None:
    overlap = sorted(set(LOADER_KEY_CATALOG_DENIED) & set(LOADER_KEY_CATALOG_PENDING_SIBLING_PR))
    assert not overlap, overlap
