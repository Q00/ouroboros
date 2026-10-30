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
2. **A catalog of known loader / executable-selector keys** is denied by
   ``config/untrusted_env.py``. A new report of this class is answered by
   adding the key to both places; forgetting either one fails CI.

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

import ouroboros
from ouroboros.config.untrusted_env import UNTRUSTED_ENV_DENYLIST, is_untrusted_env_denied_key
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


def _qualified_definition(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    parts = []
    current = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(parts))


def _binding_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current
        current = parents.get(current)
    return None


@cache
def _local_nodes(scope: ast.AST) -> tuple[ast.AST, ...]:
    result = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        item = pending.pop()
        result.append(item)
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            pending.extend(ast.iter_child_nodes(item))
    return tuple(result)


def _resolve_symbol(
    dotted: str, node: ast.AST, parents: dict[ast.AST, ast.AST], module: str, depth: int = 0
) -> str:
    if depth > 8:
        return "<unresolved>"
    first, _, tail = dotted.partition(".")
    scope = _binding_scope(node, parents)
    if first in {"self", "cls"}:
        current = scope
        while current is not None:
            if isinstance(current, ast.ClassDef):
                return f"{module}.{_qualified_definition(current, parents)}.{tail}"
            current = _binding_scope(current, parents)
        return "<unresolved>"
    while scope is not None:
        matches = []
        for item in _local_nodes(scope):
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                for alias in item.names:
                    local = alias.asname or alias.name.split(".")[0]
                    if local != first:
                        continue
                    if isinstance(item, ast.Import):
                        target = alias.name if alias.asname else local
                    else:
                        prefix = item.module or ""
                        if item.level:
                            prefix = ".".join(module.split(".")[: -item.level] + [prefix])
                        target = f"{prefix}.{alias.name}"
                    matches.append((item.lineno, target))
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if item.name == first:
                    matches.append(
                        (item.lineno, f"{module}.{_qualified_definition(item, parents)}")
                    )
            elif isinstance(item, (ast.Assign, ast.AnnAssign)) and item.lineno < node.lineno:
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                if any(isinstance(target, ast.Name) and target.id == first for target in targets):
                    value = _dotted(item.value) if item.value is not None else None
                    resolved = (
                        _resolve_symbol(value, item, parents, module, depth + 1)
                        if value
                        else "<unresolved>"
                    )
                    matches.append((item.lineno, resolved))
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = scope.args
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            parameters += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
            if any(arg.arg == first for arg in parameters):
                matches.append((-1, "<parameter>"))
        if matches:
            target = max(matches)[1]
            return target + (f".{tail}" if tail else "")
        scope = _binding_scope(scope, parents)
    return dotted if first == "dict" else f"<unresolved>.{dotted}"


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
#: Matched by source-qualified definition, including its enclosing class.
#: Adding a name here is a security review, not a fix:
#: the helper must start from a copy and pop / filter keys, never hand back
#: ``os.environ`` itself. :func:`test_named_env_builders_exist` keeps the roster
#: honest.
NAMED_ENV_BUILDERS: frozenset[str] = frozenset(
    {
        "ouroboros.cli.commands.plugin_cache.git_noninteractive_env",
        "ouroboros.codex.cli_policy.build_codex_child_env",
        "ouroboros.copilot.cli_policy.build_copilot_child_env",
        "ouroboros.core.project_identity._git_environment",
        "ouroboros.kiro.cli_policy.build_kiro_child_env",
        "ouroboros.orchestrator.claude_worker_runtime.ClaudeWorkerTransport._child_env",
        "ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime._build_child_env",
        "ouroboros.orchestrator.copilot_cli_runtime.CopilotCliRuntime._build_child_env",
        "ouroboros.orchestrator.gemini_cli_runtime.GeminiCLIRuntime._build_child_env",
        "ouroboros.orchestrator.goose_runtime.GooseCliRuntime._build_child_env",
        "ouroboros.orchestrator.grok_cli_runtime.GrokCliRuntime._build_child_env",
        "ouroboros.orchestrator.hermes_runtime.HermesCliRuntime._build_child_env",
        "ouroboros.orchestrator.kiro_adapter.KiroAgentAdapter._build_child_env",
        "ouroboros.orchestrator.omp_runtime.OmpRuntime._build_child_env",
        "ouroboros.orchestrator.opencode_runtime.OpenCodeRuntime._build_child_env",
        "ouroboros.orchestrator.pi_runtime.PiRuntime._build_child_env",
        "ouroboros.orchestrator.verify_shell.sanitized_verify_environment",
        "ouroboros.orchestrator.zcode_cli_runtime.ZcodeCLIRuntime._build_child_env",
        "ouroboros.providers.claude_code_adapter.ClaudeCodeAdapter._build_cli_child_env",
        "ouroboros.providers.codex_cli_adapter.CodexCliLLMAdapter._build_child_env",
        "ouroboros.providers.copilot_cli_adapter.CopilotCliLLMAdapter._build_child_env",
        "ouroboros.providers.dsh_acp_client.DshAcpClient._build_env",
        "ouroboros.providers.gemini_cli_adapter.GeminiCLIAdapter._build_env",
        "ouroboros.providers.goose_cli_adapter.GooseCliLLMAdapter._build_child_env",
        "ouroboros.providers.goose_cli_adapter.GooseCliLLMAdapter._build_env_for_instance",
        "ouroboros.providers.hermes_cli_adapter.HermesCliLLMAdapter._build_env",
        "ouroboros.providers.kiro_adapter.KiroCodeAdapter._build_child_env",
        "ouroboros.providers.opencode_adapter.OpenCodeLLMAdapter._build_child_env",
        "ouroboros.providers.ourocode_acp_client.OurocodeAcpClient._build_env",
        "ouroboros.providers.zcode_cli_adapter.ZcodeCliLLMAdapter._build_child_env",
        "ouroboros.runtime.child_env.build_child_env",
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
    column: int = 0

    @property
    def key(self) -> tuple[str, str, str, int, int]:
        return (self.file, self.function, self.callee, self.line, self.column)

    @property
    def builder(self) -> str:
        return self.env_source

    @property
    def uses_named_builder(self) -> bool:
        if self.env_source.startswith("either(") and self.env_source.endswith(")"):
            alternatives = self.env_source[len("either(") : -1].split("|")
            return all(alt in NAMED_ENV_BUILDERS for alt in alternatives)
        return self.builder in NAMED_ENV_BUILDERS


def _last_assignment_before(name: str, function: ast.AST | None, line: int) -> ast.AST | str | None:
    """Return the value last assigned to ``name`` in ``function`` before ``line``.

    Returns the string ``"parameter"`` when the name is a parameter of the
    enclosing function and no assignment precedes the call.
    """
    if function is None:
        return None
    candidate: ast.AST | None = None
    for node in _local_nodes(function):
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
    expr: ast.AST | str | None,
    function: ast.AST | None,
    line: int,
    depth: int = 0,
    resolve: Callable[[str, ast.AST], str] | None = None,
) -> str:
    if expr is None:
        return "inherit"
    if isinstance(expr, str):
        return expr
    if isinstance(expr, ast.Call):
        callee = _dotted(expr.func) or "<call>"
        if resolve is not None:
            callee = resolve(callee, expr)
        if callee == "dict" and expr.args and not expr.keywords:
            inner = _describe_env_expr(expr.args[0], function, line, depth + 1, resolve)
            return f"dict({inner})"
        return callee
    if isinstance(expr, ast.Name):
        if depth > 4:
            return "unresolved"
        resolved = _last_assignment_before(expr.id, function, line)
        if resolved is None:
            return "unresolved"
        return _describe_env_expr(resolved, function, line, depth + 1, resolve)
    if isinstance(expr, ast.Dict):
        if any(key is None for key in expr.keys):
            spread = [
                _describe_env_expr(value, function, line, depth + 1, resolve)
                for key, value in zip(expr.keys, expr.values, strict=True)
                if key is None
            ]
            return "merge(" + ",".join(spread) + ")"
        return "dict-literal"
    if isinstance(expr, ast.DictComp):
        return "dict-comprehension"
    if isinstance(expr, ast.IfExp):
        body = _describe_env_expr(expr.body, function, line, depth + 1, resolve)
        orelse = _describe_env_expr(expr.orelse, function, line, depth + 1, resolve)
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
        spawn_names = {"subprocess", "asyncio", "os"}
        for imported in ast.walk(tree):
            if isinstance(imported, ast.Import):
                spawn_names.update(
                    alias.asname or alias.name
                    for alias in imported.names
                    if alias.name in {"subprocess", "asyncio", "os"}
                )
            elif isinstance(imported, ast.ImportFrom) and imported.module in {
                "subprocess",
                "asyncio",
                "os",
            }:
                spawn_names.update(alias.asname or alias.name for alias in imported.names)
        module = ".".join(source.relative_to(root).with_suffix("").parts)
        if root == _PACKAGE_ROOT:
            module = f"ouroboros.{module}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted(node.func)
            if not dotted or dotted.split(".")[0] not in spawn_names:
                continue
            callee = _resolve_symbol(dotted, node, parents, module) if dotted else None
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
            env_source = _describe_env_expr(
                env_expr,
                function,
                node.lineno,
                resolve=lambda name, expr: _resolve_symbol(name, expr, parents, module),
            )
            if root == _PACKAGE_ROOT:
                file = _rel(source)
            else:
                file = source.relative_to(root).as_posix()
            sites.append(
                SpawnSite(
                    file=file,
                    line=node.lineno,
                    function=_qualified_definition(function, parents) if function else "<module>",
                    callee=callee,
                    env_source=env_source,
                    column=node.col_offset,
                )
            )
    return tuple(sites)


#: Spawn sites that do NOT pass a named-builder environment, each with the
#: reason a reviewer accepted it. File, qualified scope, callee, line and column
#: identify one call; source movement intentionally requires revalidation.
#: An entry that no longer matches any site fails
#: :func:`test_spawn_site_allowlist_has_no_stale_entries`.
#:
#: "inherit" = no ``env=`` at all (child gets ``os.environ``);
#: "os.environ.copy" / "merge(os.environ)" = copy plus edits;
#: "parameter" = env passed in by the caller. A justification must name why
#: inheriting is acceptable so the allowlist never silently absorbs a finding.
SPAWN_SITE_ALLOWLIST: dict[tuple[str, str, str, int, int], str] = {
    (
        "src/ouroboros/copilot/acp_client.py",
        "CopilotAcpClient.__aenter__",
        "asyncio.create_subprocess_exec",
        99,
        12,
    ): "Required explicit env parameter; production CopilotAcpRuntime supplies inherited _build_child_env -> build_copilot_child_env, stripping runtime selectors and enforcing recursion depth. Client never implicitly inherits os.environ.",
    (
        "src/ouroboros/auto/checkpoint_commits.py",
        "_staged_changes",
        "subprocess.run",
        154,
        13,
    ): "implicit inherit; `git diff --cached`. GIT_* is denied from the untrusted .env by the loader.",
    (
        "src/ouroboros/auto/checkpoint_commits.py",
        "_git",
        "subprocess.run",
        173,
        13,
    ): "implicit inherit; `git add/commit`. GIT_* is denied from the untrusted .env by the loader.",
    (
        "src/ouroboros/auto/investigation_workspace.py",
        "_git",
        "subprocess.run",
        41,
        11,
    ): "Fixed git workspace snapshot commands; inherits operator environment after project .env loader denial of GIT_* and PATH.",
    (
        "src/ouroboros/auto/requester.py",
        "CommandRequester.ask",
        "asyncio.create_subprocess_exec",
        127,
        28,
    ): "Requester argv is explicitly operator-selected; OUROBOROS_AUTO_REQUESTER_COMMAND is denied from project .env; no shell.",
    (
        "src/ouroboros/auto/seed_preflight_repair.py",
        "_git_listed_files",
        "subprocess.run",
        108,
        20,
    ): "Fixed git ls-files roster; GIT_* and PATH denied from project .env.",
    (
        "src/ouroboros/backends/model_catalog.py",
        "refresh_models",
        "subprocess.run",
        357,
        21,
    ): "implicit inherit; `<cli> <list-args>` model listing with table-driven argv.",
    (
        "src/ouroboros/bigbang/brownfield.py",
        "_origin_remote_url",
        "subprocess.run",
        102,
        17,
    ): "implicit inherit; `git -C <path> remote get-url origin`.",
    (
        "src/ouroboros/bigbang/brownfield.py",
        "_is_git_worktree",
        "subprocess.run",
        140,
        17,
    ): "implicit inherit; `git -C <path> rev-parse HEAD`.",
    (
        "src/ouroboros/boundary/_pinned_exec.py",
        "main",
        "os.execve",
        76,
        4,
    ): "Descriptor/hash-pinned interpreter launcher; passes controller-supplied environment to the same verified interpreter, with explicit POSIX fallback.",
    (
        "src/ouroboros/boundary/_pinned_exec.py",
        "main",
        "os.execve",
        74,
        8,
    ): "Descriptor/hash-pinned interpreter launcher; passes controller-supplied environment to the same verified interpreter, with explicit POSIX fallback.",
    (
        "src/ouroboros/cli/commands/codex.py",
        "_list_stdio_mcp_tool_names_with_framing",
        "asyncio.create_subprocess_exec",
        962,
        17,
    ): "os.environ.copy()+server env from $CODEX_HOME/config.toml (denied root); doctor probe.",
    (
        "src/ouroboros/cli/commands/megacode_setup.py",
        "resolve_harness_config_dir",
        "subprocess.run",
        80,
        20,
    ): "Operator-selected harness with literal paths argv, no shell or prompt; executable and loader controls are denied from project .env, bounded by 15s.",
    (
        "src/ouroboros/cli/commands/mcp.py",
        "_ps_value",
        "subprocess.run",
        458,
        17,
    ): "implicit inherit; `ps -p <pid> -o <literal column>=` (start time / ppid).",
    (
        "src/ouroboros/cli/commands/mcp.py",
        "_ensure_shell_env",
        "subprocess.run",
        398,
        21,
    ): "implicit inherit; spawns `$SHELL -l -c` to dump the login environment. SHELL and ZDOTDIR are denied from the untrusted .env (PR #2404), so argv[0] and the zsh startup file come from the real environment.",
    (
        "src/ouroboros/cli/commands/mcp_doctor.py",
        "_pid_is_alive",
        "subprocess.run",
        697,
        25,
    ): "implicit inherit; Windows `tasklist /FI` liveness probe.",
    (
        "src/ouroboros/cli/commands/tui.py",
        "open_command",
        "subprocess.Popen",
        136,
        8,
    ): "implicit inherit; opens a terminal emulator with a shlex-quoted argv built from operator flags.",
    (
        "src/ouroboros/cli/commands/tui.py",
        "_run_slt_backend",
        "os.execv",
        332,
        8,
    ): "implicit inherit (POSIX exec of the Rust TUI); same resolution as above.",
    (
        "src/ouroboros/cli/commands/tui.py",
        "_run_slt_backend",
        "subprocess.call",
        330,
        17,
    ): "implicit inherit (Windows); argv[0] resolved by shutil.which on the denied PATH.",
    (
        "src/ouroboros/cli/commands/update.py",
        "_run_step",
        "subprocess.run",
        661,
        17,
    ): "Operator-triggered install/plugin refresh uses code-owned argv without shell; inherits operator env or merges installer/topology overrides (UV_TOOL_DIR/PIPX_HOME/UV_TOOL_BIN_DIR). Project .env denies PATH and package-manager/config-loader controls; timeout defaults to 600s and errors/nonzero return False.",
    (
        "src/ouroboros/cli/commands/update.py",
        "_installed_version",
        "subprocess.run",
        921,
        17,
    ): "Operator installation identity selects the console path with literal --version, no shell; inherits operator env after project .env executable/loader controls are denied. 30s timeout; spawn/nonzero/parse failure returns None.",
    (
        "src/ouroboros/cli/omp_config.py",
        "configure_omp_tool_call_timeout",
        "subprocess.run",
        23,
        18,
    ): "implicit inherit; `omp config get|set <literal>` (two spawns in one function).",
    (
        "src/ouroboros/cli/omp_config.py",
        "configure_omp_tool_call_timeout",
        "subprocess.run",
        42,
        17,
    ): "implicit inherit; `omp config get|set <literal>` (two spawns in one function).",
    (
        "src/ouroboros/cli/opencode_config.py",
        "_debug_paths_config_dir",
        "subprocess.run",
        91,
        17,
    ): "Operator configuration resolves the OpenCode executable; literal debug paths argv and no shell. Inherits operator profile/config after project .env PATH, OPENCODE_CONFIG(_DIR), HOME and loader controls are denied. 5s timeout; unavailable/nonzero/unparseable config returns None for deterministic fallback.",
    (
        "src/ouroboros/cli/windows_codex_mcp.py",
        "_launcher_is_usable",
        "subprocess.run",
        35,
        17,
    ): "implicit inherit; `<launcher> --help` probe with a code-owned argv.",
    (
        "src/ouroboros/config_tui/launcher.py",
        "_relaunch_with_tui_profile",
        "os.execvpe",
        70,
        4,
    ): "os.environ.copy()+bootstrap marker; `uvx --isolated` relaunch of ourselves. XDG_DATA_HOME (uv resolution) is an open question in THREAT_MODEL.md.",
    (
        "src/ouroboros/copilot/model_discovery.py",
        "_resolve_token",
        "subprocess.run",
        140,
        20,
    ): "implicit inherit; `gh auth token`.",
    (
        "src/ouroboros/core/git_workflow.py",
        "is_on_protected_branch",
        "subprocess.run",
        153,
        17,
    ): "implicit inherit; `git rev-parse --abbrev-ref HEAD`.",
    (
        "src/ouroboros/core/pm_snapshot.py",
        "_run_git",
        "subprocess.run",
        63,
        17,
    ): "implicit inherit; read-only git snapshot commands.",
    (
        "src/ouroboros/core/worktree.py",
        "_run_git_process",
        "subprocess.run",
        133,
        15,
    ): "implicit inherit; `git worktree add/remove/status`. GIT_* is denied from the untrusted .env by the loader.",
    (
        "src/ouroboros/core/worktree.py",
        "_run_git_bytes",
        "subprocess.run",
        162,
        17,
    ): "implicit inherit; git plumbing with bytes output. GIT_* is denied from the untrusted .env by the loader.",
    (
        "src/ouroboros/dashboard_web/daemon.py",
        "_spawn_detached",
        "subprocess.Popen",
        175,
        4,
    ): "implicit inherit; fixed `sys.executable -m` argv, detached daemon. PYTHON* keys are denied from the untrusted .env by the loader.",
    (
        "src/ouroboros/evaluation/mechanical.py",
        "run_command",
        "asyncio.create_subprocess_exec",
        130,
        24,
    ): "os.environ.copy() minus _OUROBOROS_NESTED; runs .ouroboros/mechanical.toml commands; the repo already controls the command text, so env from the same repo adds no capability. Not routed through sanitized_verify_environment.",
    (
        "src/ouroboros/evolution/frugality.py",
        "_git",
        "subprocess.run",
        320,
        16,
    ): "implicit inherit; `git -C <path> status --porcelain`.",
    (
        "src/ouroboros/examples/probe_wrapper.py",
        "_command",
        "subprocess.Popen",
        236,
        18,
    ): "Runs inside the confined probe process (check_env.check_command); the inherited environment is the sandbox's allowlisted check environment, never the controller's; no shell.",
    (
        "src/ouroboros/mcp/detached_jobs.py",
        "_spawn_worker",
        "subprocess.Popen",
        331,
        14,
    ): "os.environ.copy()+marker via **kwargs; fixed `sys.executable -m` argv. PYTHON* keys are denied from the untrusted .env by the loader.",
    (
        "src/ouroboros/orchestrator/attempt_evidence.py",
        "_git_bytes",
        "subprocess.run",
        161,
        20,
    ): "Fixed git metadata commands for workspace evidence; GIT_* and PATH denied from project .env; no shell.",
    (
        "src/ouroboros/orchestrator/cli_version_attestation.py",
        "probe_cli_executable_version_attestation",
        "subprocess.run",
        253,
        17,
    ): "implicit inherit; `<attested-path> --version` after filesystem identity check.",
    (
        "src/ouroboros/orchestrator/context_pack.py",
        "_git_head",
        "subprocess.run",
        490,
        17,
    ): "Literal git rev-parse HEAD at the supplied workspace, no shell; inherits operator env after project .env GIT_*/PATH/loader controls are denied. 5s timeout; spawn/nonzero/empty output returns None.",
    (
        "src/ouroboros/orchestrator/evidence/system.py",
        "_get_available_memory_gb",
        "subprocess.run",
        18,
        21,
    ): "implicit inherit; `vm_stat`.",
    (
        "src/ouroboros/orchestrator/heartbeat.py",
        "_get_process_start_time",
        "subprocess.run",
        129,
        21,
    ): "implicit inherit; `ps -p <pid> -o lstart=`.",
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "RunWorktreeManager._git",
        "subprocess.run",
        146,
        15,
    ): "implicit inherit; tournament worktree git helper.",
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "export_worktree_diff",
        "subprocess.run",
        247,
        17,
    ): "implicit inherit; `git diff --binary HEAD` / `git ls-files` / `git diff --no-index`.",
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "export_worktree_diff",
        "subprocess.run",
        264,
        27,
    ): "implicit inherit; `git diff --binary HEAD` / `git ls-files` / `git diff --no-index`.",
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "apply_diff_to_workspace",
        "subprocess.run",
        350,
        8,
    ): "implicit inherit; `git apply` of a tournament winner.",
    (
        "src/ouroboros/orchestrator/n_version_tournament.py",
        "export_worktree_diff",
        "subprocess.run",
        285,
        31,
    ): "implicit inherit; `git diff --binary HEAD` / `git ls-files` / `git diff --no-index`.",
    (
        "src/ouroboros/orchestrator/opencode_runtime.py",
        "OpenCodeRuntime._cleanup_windows_child_processes",
        "subprocess.run",
        642,
        21,
    ): "implicit inherit; Windows `wmic process ... delete` cleanup of our own child.",
    (
        "src/ouroboros/orchestrator/pi_runtime.py",
        "_probe_pi_native_param_flags",
        "subprocess.run",
        83,
        17,
    ): "Operator-selected Pi CLI with literal --help and no shell; project .env denies Pi selector/profile and PATH/loader controls. Inherits real operator profile, 10s timeout; spawn/nonzero failure returns all False, retaining conservative capability admission.",
    (
        "src/ouroboros/orchestrator/runtime_evidence.py",
        "HeadlessRunProbe.run",
        "subprocess.run",
        224,
        24,
    ): "implicit inherit; HeadlessRunProbe command from trusted config, not the repo.",
    (
        "src/ouroboros/orchestrator/verify_command_runner.py",
        "_run_process",
        "asyncio.create_subprocess_exec",
        238,
        21,
    ): "parameter passthrough `dict(env)`; every caller passes sanitized_verify_environment() (verify_shell, leaf_dispatcher, parallel_executor).",
    (
        "src/ouroboros/orchestrator/workspace_evidence_paths.py",
        "load_tracked_workspace_paths",
        "subprocess.run",
        13,
        18,
    ): "implicit inherit; `git ls-files`.",
    (
        "src/ouroboros/orchestrator/workspace_evidence_paths.py",
        "load_ignored_workspace_paths",
        "subprocess.run",
        38,
        18,
    ): "implicit inherit; `git ls-files --ignored`.",
    (
        "src/ouroboros/providers/codex_cli_adapter.py",
        "CodexCliLLMAdapter._codex_version",
        "subprocess.run",
        736,
        21,
    ): "implicit inherit; `<codex> --version` diagnostic.",
    (
        "src/ouroboros/providers/codex_cli_adapter.py",
        "CodexCliLLMAdapter._complete_once",
        "asyncio.create_subprocess_exec",
        1066,
        28,
    ): "Reviewed polymorphic dispatch: GooseCliLLMAdapter._build_env_for_instance when present, otherwise backend _build_child_env; no untrusted helper-name selection.",
    (
        "src/ouroboros/providers/litellm_adapter.py",
        "LiteLLMAdapter._run_isolated_completion",
        "asyncio.create_subprocess_exec",
        988,
        28,
    ): "explicit two-key literal env (PYTHONIOENCODING/PYTHONUTF8) and `-I`; reference pattern.",
    (
        "src/ouroboros/runtime/_confine_exec.py",
        "main",
        "os.execvpe",
        797,
        8,
    ): "Controller-serialized command environment is decoded only before executing the command after filesystem/network confinement.",
    (
        "src/ouroboros/runtime/exec_sandbox.py",
        "_probe_matrix",
        "subprocess.run",
        690,
        17,
    ): "Sandbox capability probe uses fixed _bootstrap_environment (os.defpath plus serialized command environment), not inherited loaders.",
    (
        "src/ouroboros/runtime/exec_sandbox.py",
        "_unshare_prefix",
        "subprocess.run",
        814,
        17,
    ): "Sandbox capability probe uses fixed _bootstrap_environment (os.defpath plus serialized command environment), not inherited loaders.",
    (
        "src/ouroboros/runtime/exec_sandbox.py",
        "_private_mounts_unshare",
        "subprocess.run",
        882,
        17,
    ): "Sandbox capability probe uses fixed _bootstrap_environment (os.defpath plus serialized command environment), not inherited loaders.",
    (
        "src/ouroboros/tui/screens/lineage_detail.py",
        "LineageDetailScreen._perform_rewind",
        "asyncio.create_subprocess_exec",
        547,
        25,
    ): "implicit inherit; `git status/rev-parse/checkout <tag>` from the TUI rewind flow.",
    (
        "src/ouroboros/tui/screens/lineage_detail.py",
        "LineageDetailScreen._perform_rewind",
        "asyncio.create_subprocess_exec",
        569,
        25,
    ): "implicit inherit; `git status/rev-parse/checkout <tag>` from the TUI rewind flow.",
    (
        "src/ouroboros/tui/screens/lineage_detail.py",
        "LineageDetailScreen._perform_rewind",
        "asyncio.create_subprocess_exec",
        592,
        25,
    ): "implicit inherit; `git status/rev-parse/checkout <tag>` from the TUI rewind flow.",
    (
        "src/ouroboros/cli/codex_plugin.py",
        "inspect_codex_plugin",
        "subprocess.run",
        146,
        17,
    ): "Operator-selected Codex executable with literal plugin list --marketplace ouroboros --json, no shell; inherited CODEX_HOME intentionally selects the operator profile and is denied from project .env with PATH/loader controls. 5s timeout; spawn/nonzero/invalid inventory yields advisory unknown.",
    (
        "src/ouroboros/providers/worker_process_tree.py",
        "_scan_ps",
        "subprocess.run",
        121,
        20,
    ): "Literal ps -A -o pid=,ppid=,pgid=,stat=,lstart= process-table observation, no shell; inherits operator PATH/locale after project .env PATH/loader controls are denied. 5s timeout; spawn/nonzero failure returns unknown (None).",
    (
        "src/ouroboros/providers/worker_process_tree.py",
        "_lock_holders",
        "subprocess.run",
        264,
        20,
    ): "Operator PATH-resolved lsof with literal -t -- and realpath target, no shell; project .env PATH/loader controls are denied. 5s timeout; incomplete/unavailable observation returns None, exit1 with empty output means no holders, optional PID filter only narrows observations.",
    (
        "src/ouroboros/providers/worker_process_tree.py",
        "_same_process",
        "subprocess.run",
        425,
        20,
    ): "Literal ps -o lstart= -p <numeric pid> identity probe, no shell; inherits operator PATH/locale after project .env PATH/loader controls are denied. 5s timeout; spawn/nonzero/start mismatch returns False, so unknown identity never authorizes signaling.",
    (
        "src/ouroboros/cli/opencode_config.py",
        "plugin_version_error",
        "subprocess.run",
        22,
        17,
    ): "Operator/config-selected OpenCode CLI with literal --version, no shell; inherits trusted operator profile after project .env PATH, OpenCode config and loader controls are denied. 5s timeout; unavailable/nonzero/unparseable version returns None (no compatibility diagnosis), never authorizes a security action.",
}


@cache
def _spawn_sites() -> tuple[SpawnSite, ...]:
    return _discover_spawn_sites(_PACKAGE_ROOT)


def test_spawn_site_discovery_is_non_vacuous() -> None:
    """The enumerator must keep finding the sites the invariant exists for."""
    keys = {site.key[:3] for site in _spawn_sites()}
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
                if alias.name == "*" or alias.name in _QUALIFIED_ONLY_IMPORTS[node.module]:
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
        parents = _parents(tree)
        module = "ouroboros." + ".".join(source.relative_to(_PACKAGE_ROOT).with_suffix("").parts)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(f"{module}.{_qualified_definition(node, parents)}")
    missing = sorted(NAMED_ENV_BUILDERS - defined)
    assert not missing, f"NAMED_ENV_BUILDERS names nothing defined in src: {missing}"
    assert callable(child_env.build_child_env)
    assert callable(verify_shell.sanitized_verify_environment)


def test_named_env_builders_never_return_os_environ_itself() -> None:
    """A builder must copy: handing back ``os.environ`` would let a child mutate the parent."""
    for source in _iter_package_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        parents = _parents(tree)
        module = "ouroboros." + ".".join(source.relative_to(_PACKAGE_ROOT).with_suffix("").parts)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if f"{module}.{_qualified_definition(node, parents)}" not in NAMED_ENV_BUILDERS:
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
            "ouroboros.runtime.child_env.build_child_env",
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
        column=17,
    )
    assert not site.uses_named_builder


# ---------------------------------------------------------------------------
# Invariant 2 — catalog of runtime-loader / executable-selector keys is denied
# ---------------------------------------------------------------------------

#: Keys every reviewer agrees must never come from a cloned repository's .env.
#: Adding a key here without adding it to ``untrusted_env.py`` fails CI, which
#: is the point: the catalog is the reviewer's checklist, the denylist the code.
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
    "HARNESS_HOME",
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
    # Runtime module-resolution, package-manager and git families (PR #2404).
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
    # Same-class keys folded into PR #2404 from the threat-model scan.
    "CLAUDE_CONFIG_DIR",
    "ZDOTDIR",
    "SHELL",
    "BROWSER",
    "OUROBOROS_BACKEND_LIMITS",
    "OUROBOROS_MCP_AUTH_TOKEN",
    "OUROBOROS_IO_JOURNAL_PREVIEWS",
    "BASH_COMPAT",
    "BASH_XTRACEFD",
    "CI",
    "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
    "DO_NOT_TRACK",
    "GITHUB_ACTIONS",
    "GJC_CODING_AGENT_DIR",
    "GJC_CONFIG_DIR",
    "HERMES_HOME",
    "HERMES_REAL_HOME",
    "LDR_PRELOAD",
    "LIBPATH",
    "OUROBOROS_AGENT_PERMISSION_MODE",
    "OUROBOROS_AGENT_REASONING_EFFORT",
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_ANTIGRAVITY_CLI_PATH",
    "OUROBOROS_ASSERTION_EXTRACTION_MODEL",
    "OUROBOROS_AUTO_INVESTIGATOR_CALL_TIMEOUT_SECONDS",
    "OUROBOROS_AUTO_INVESTIGATOR_MAX_CALLS",
    "OUROBOROS_AUTO_REQUESTER_COMMAND",
    "OUROBOROS_AUTO_REQUESTER_TIMEOUT_SECONDS",
    "OUROBOROS_CHECK_PACKAGE",
    "OUROBOROS_CLARIFICATION_MODEL",
    "OUROBOROS_CODEX_CLI_PATH",
    "OUROBOROS_CONSENSUS_ADVOCATE_MODEL",
    "OUROBOROS_CONSENSUS_DEVIL_MODEL",
    "OUROBOROS_CONSENSUS_JUDGE_MODEL",
    "OUROBOROS_CONSENSUS_MODELS",
    "OUROBOROS_CONTEXT_COMPRESSION_MODEL",
    "OUROBOROS_COPILOT_ACP_FALLBACK",
    "OUROBOROS_COPILOT_CLI_PATH",
    "OUROBOROS_COPILOT_TRANSPORT",
    "OUROBOROS_CROSS_HARNESS_REDISPATCH",
    "OUROBOROS_DEPENDENCY_ANALYSIS_MODEL",
    "OUROBOROS_DETECTOR_MODEL",
    "OUROBOROS_DSH_CLI_PATH",
    "OUROBOROS_DSH_CONFIG_PATH",
    "OUROBOROS_EVOLVE_STAGE1",
    "OUROBOROS_EXECUTION_MODEL",
    "OUROBOROS_EXEC_SANDBOX",
    "OUROBOROS_FIRST_COMMAND_SURFACE",
    "OUROBOROS_GEMINI_CLI_PATH",
    "OUROBOROS_GJC_CLI_PATH",
    "OUROBOROS_GOOSE_CLI_PATH",
    "OUROBOROS_GROK_CLI_PATH",
    "OUROBOROS_HERMES_CLI_PATH",
    "OUROBOROS_KIRO_CLI_PATH",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_LLM_PERMISSION_MODE",
    "OUROBOROS_MEGACODE_CLI_PATH",
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_TIER_ROUTING",
    "OUROBOROS_OMP_CLI_PATH",
    "OUROBOROS_ONTOLOGY_ANALYSIS_MODEL",
    "OUROBOROS_OPENCODE_CLI_PATH",
    "OUROBOROS_OPENCODE_PERMISSION_MODE",
    "OUROBOROS_OUROCODE_CLI_PATH",
    "OUROBOROS_PIN_MODELS",
    "OUROBOROS_PI_CLI_PATH",
    "OUROBOROS_POSTHOG_API_KEY",
    "OUROBOROS_POSTHOG_HOST",
    "OUROBOROS_QA_MODEL",
    "OUROBOROS_REFLECT_MODEL",
    "OUROBOROS_RUNTIME",
    "OUROBOROS_RUNTIME_PROFILE",
    "OUROBOROS_SEMANTIC_MODEL",
    "OUROBOROS_SHADOW_REPLAY",
    "OUROBOROS_TELEMETRY",
    "OUROBOROS_VALIDATION_MODEL",
    "OUROBOROS_WONDER_MODEL",
    "OUROBOROS_ZCODE_CLI_PATH",
    "PI_CODING_AGENT_DIR",
    "PI_CONFIG_DIR",
    "SHLIB_PATH",
)


@pytest.mark.parametrize("key", LOADER_KEY_CATALOG_DENIED)
def test_loader_and_executable_selector_catalog_is_denied(key: str) -> None:
    assert is_untrusted_env_denied_key(key), f"{key} must be denied from the untrusted .env"


@pytest.mark.parametrize("key", LOADER_KEY_CATALOG_DENIED)
def test_loader_catalog_denial_is_case_insensitive(key: str) -> None:
    """The loader upper-cases before matching; a lowercase spelling must not slip through."""
    assert is_untrusted_env_denied_key(key.lower())


def _discover_routing_keys(root: Path) -> set[str]:
    return {
        node.value
        for source in root.rglob("*.py")
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.isidentifier()
        and node.value.isupper()
        and node.value.endswith("_CLI_PATH")
    }


def _catalog_gaps(
    catalog: set[str],
    explicit_policy: set[str],
    routing: set[str],
    denies: Callable[[str], bool],
) -> tuple[set[str], set[str]]:
    return (explicit_policy | routing) - catalog, {
        key for key in catalog | routing if not denies(key)
    }


def test_catalog_reconciles_policy_and_source_in_both_directions() -> None:
    gaps = _catalog_gaps(
        set(LOADER_KEY_CATALOG_DENIED),
        set(UNTRUSTED_ENV_DENYLIST),
        _discover_routing_keys(_PACKAGE_ROOT),
        is_untrusted_env_denied_key,
    )
    assert gaps == (set(), set()), f"missing catalog entries / missing denials: {gaps}"


def test_uncatalogued_selector_fails_even_with_reviewed_builder(tmp_path: Path) -> None:
    (tmp_path / "future.py").write_text(
        "import os, subprocess\nfrom ouroboros.runtime.child_env import build_child_env\n"
        "subprocess.run([os.getenv('FIFTH_CLI_PATH')], env=build_child_env())\n"
    )
    (site,) = _discover_spawn_sites(tmp_path)
    assert site.uses_named_builder
    discovered = _discover_routing_keys(tmp_path)
    assert discovered == {"FIFTH_CLI_PATH"}
    assert _catalog_gaps(set(), set(), discovered, lambda _key: False) == (
        {"FIFTH_CLI_PATH"},
        {"FIFTH_CLI_PATH"},
    )


def test_catalog_and_policy_cannot_drift_independently() -> None:
    assert _catalog_gaps(set(), {"SELECTOR"}, set(), lambda _key: True) == ({"SELECTOR"}, set())
    assert _catalog_gaps({"SELECTOR"}, set(), set(), lambda _key: False) == (set(), {"SELECTOR"})


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess as sp\nsp.run(['x'])\n",
        "import asyncio as aio\nasync def f():\n await aio.create_subprocess_exec('x')\n",
        "from subprocess import run as launch\nlaunch(['x'])\n",
        "import os as operating\noperating.execvp('x', ['x'])\n",
    ],
)
def test_spawn_discovery_resolves_import_aliases(tmp_path: Path, source: str) -> None:
    (tmp_path / "runner.py").write_text(source)
    (site,) = _discover_spawn_sites(tmp_path)
    assert site.callee in SPAWN_CALLEES
    assert not site.uses_named_builder


def test_spawn_identities_do_not_share_an_allowance(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text(
        "import subprocess\ndef f():\n subprocess.run(['one']); subprocess.run(['two'])\n"
        "class A:\n def f(self):\n  subprocess.run(['three'])\n"
        "class B:\n def f(self):\n  subprocess.run(['four'])\n"
    )
    sites = _discover_spawn_sites(tmp_path)
    assert len(sites) == 4
    approved = {sites[0].key: "reviewed first call only"}
    assert len({site.key for site in sites}) == 4
    assert sum(site.key in approved for site in sites) == 1


def test_local_builder_name_cannot_impersonate_reviewed_helper(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text(
        "import subprocess, os\ndef build_child_env():\n return os.environ\n"
        "subprocess.run(['x'], env=build_child_env())\n"
    )
    (site,) = _discover_spawn_sites(tmp_path)
    assert not site.uses_named_builder


def test_nested_assignment_cannot_supply_outer_environment(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text(
        "import subprocess, os\nfrom ouroboros.runtime.child_env import build_child_env\n"
        "def run():\n env = os.environ\n def nested():\n  env = build_child_env()\n"
        " subprocess.run(['x'], env=env)\n"
    )
    (site,) = _discover_spawn_sites(tmp_path)
    assert not site.uses_named_builder


def test_wildcard_import_is_not_an_undiscovered_spawn_escape(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text("from subprocess import *\nrun(['x'])\n")
    assert _discover_bare_imports(tmp_path) == [("runner.py", 1, "from subprocess import *")]


def _checkpoint_helper() -> Path:
    return _PACKAGE_ROOT.parent.parent / ".claude/skills/_lib/checkpoint.py"


_REQUIRES_NOFOLLOW = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Checkpoint publication requires POSIX no-follow directory descriptors",
)


@_REQUIRES_NOFOLLOW
@pytest.mark.parametrize("name", ["progress.json.tmp", "progress.json"])
def test_checkpoint_done_does_not_write_through_symlink(tmp_path: Path, name: str) -> None:
    root = tmp_path / "repo"
    state = root / ".triage-state"
    state.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel")
    (state / name).symlink_to(outside)
    result = subprocess.run(
        [sys.executable, str(_checkpoint_helper()), "done", str(state), "6"],
        cwd=root,
        env={**os.environ, "CHECKPOINT_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert outside.read_text() == "sentinel", result.stderr
    assert not (state / "progress.json").is_symlink() or result.returncode != 0


@_REQUIRES_NOFOLLOW
def test_checkpoint_publish_holds_parent_authority(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("checkpoint_audit", _checkpoint_helper())
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    root = tmp_path / "repo"
    state = root / ".triage-state"
    state.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "progress.json").write_text("sentinel")
    (outside / "progress.json.tmp").write_text("attacker replacement")
    monkeypatch.setattr(helper, "_ROOT", root)
    replace = os.replace

    def swap_parent(src, dst, **kwargs):
        state.rename(root / "held-state")
        state.symlink_to(outside, target_is_directory=True)
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(helper.os, "replace", swap_parent)
    with pytest.raises(OSError):
        helper._atomic_write(state / "progress.json", '{"status":"complete"}')
    assert (outside / "progress.json").read_text() == "sentinel"


def test_security_reports_are_ignored_at_actual_output_paths() -> None:
    root = _PACKAGE_ROOT.parent.parent
    paths = ["TRIAGE.json", "TRIAGE.md", "docs/security/TRIAGE.json", "docs/security/TRIAGE.md"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert set(result.stdout.splitlines()) == set(paths)


@_REQUIRES_NOFOLLOW
@pytest.mark.parametrize(
    "operation", ["save", "shard", "done", "load", "append", "reset", "rundir"]
)
def test_checkpoint_commands_refuse_symlink_parent(tmp_path: Path, operation: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "progress.json"
    sentinel.write_text('{"status":"running"}')
    (outside / "report.md").write_text("sentinel")
    (root / ".triage-state").symlink_to(outside, target_is_directory=True)
    (root / "payload.json").write_text('{"finding":"synthetic"}')
    options = {
        "save": [".triage-state", "1", "--from", "payload.json"],
        "shard": [".triage-state", "case", "--from", "payload.json"],
        "done": [".triage-state", "6"],
        "load": [".triage-state"],
        "append": [".triage-state/report.md", "--from", "payload.json"],
        "reset": [".triage-state"],
        "rundir": [".triage-state", "--state", ".scan-state", "--fresh"],
    }
    result = subprocess.run(
        [sys.executable, str(_checkpoint_helper()), operation, *options[operation]],
        cwd=root,
        env={**os.environ, "CHECKPOINT_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert sentinel.read_text() == '{"status":"running"}'
    assert (outside / "report.md").read_text() == "sentinel"
    assert sorted(p.name for p in outside.iterdir()) == ["progress.json", "report.md"]


@_REQUIRES_NOFOLLOW
def test_checkpoint_public_lifecycle_and_report_publication(tmp_path: Path) -> None:
    (tmp_path / "payload.json").write_text('{"finding":"synthetic"}')
    (tmp_path / "chunk.txt").write_text("synthetic section")

    def invoke(*args: str) -> str:
        result = subprocess.run(
            [sys.executable, str(_checkpoint_helper()), *args],
            cwd=tmp_path,
            env={**os.environ, "CHECKPOINT_ROOT": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result.stdout

    invoke("save", ".triage-state", "1", "--from", "payload.json")
    invoke("shard", ".triage-state", "finding-1", "--from", "payload.json")
    assert '"finding-1"' in invoke("load", ".triage-state")
    invoke("append", "docs/security/TRIAGE.md", "--replace", "--from", "chunk.txt")
    invoke("append", "docs/security/TRIAGE.md", "--from", "chunk.txt")
    assert (tmp_path / "docs/security/TRIAGE.md").read_text() == "synthetic section\n" * 2
    invoke("append", "docs/security/TRIAGE.md", "--replace", "--from", "chunk.txt")
    assert (tmp_path / "docs/security/TRIAGE.md").read_text() == "synthetic section\n"
    invoke("done", ".triage-state", "6")
    assert '"status": "complete"' in invoke("load", ".triage-state")
    first = invoke("rundir", "results", "--state", ".scan-state", "--scope", ".").strip()
    assert invoke("rundir", "results", "--state", ".scan-state", "--scope", ".").strip() == first
    assert invoke("rundir", "results", "--state", ".scan-state", "--fresh").strip() != first
    invoke("reset", ".triage-state")
    assert not (tmp_path / ".triage-state").exists()


def test_checkpoint_fails_closed_without_nofollow_support(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("checkpoint_unsupported", _checkpoint_helper())
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "_ROOT", tmp_path)
    monkeypatch.delattr(helper.os, "O_NOFOLLOW", raising=False)
    assert helper.main(["done", str(tmp_path / ".triage-state"), "1"]) == 2
    assert not (tmp_path / ".triage-state").exists()
