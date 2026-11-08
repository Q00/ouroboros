"""Docs and skills must name only registered MCP tools and existing agents or skills.

Item D4 of #2921; policy in docs/contributing/docs-freshness.md.

MCP tools
---------
Registered names are ``ouroboros.mcp.tool_manifest.BUILTIN_TOOL_NAMES``. It is
the production server's tool list:
``tests/unit/cli/test_mcp_doctor_probe_lifecycle.py::
test_authoritative_manifest_matches_real_production_composition`` asserts it
equals what ``create_ouroboros_server`` registers. Importing it constructs
nothing (about 0.03 s). ``mcp.tools.definitions.OUROBOROS_TOOLS`` builds every
handler at import (0.9 to 5 s measured) and lacks four tools the server adds,
so it is not used.

Checked references:

* ``mcp__ouroboros__<tool>`` and ``mcp__plugin_ouroboros_ouroboros__<tool>``,
  the host-qualified names for the standalone and the plugin server;
* every other ``ouroboros_<name>`` token, in prose and in code blocks, backticked
  or not. Real references include ``mcp_tool: ouroboros_start_auto`` in skill
  frontmatter and ``ouroboros_job_result(job_id=...)`` call syntax, so neither
  missing backticks nor a following ``(`` exempts a token.

Not tool references, by rule rather than by a list of names:

* a token right after ``.`` is a key path (``metadata.ouroboros_dispatch`` in
  the OpenCode bridge guide);
* a name the corpus defines as a function in a code block (``ouroboros_cli() {``
  in skills/setup/SKILL.md), collected from the corpus itself;
* the distribution's normalized name (``ouroboros_ai`` in wheel and dist-info
  names), read from pyproject.toml.

``*`` or a trailing ``_`` makes a token a pattern (``ouroboros_*``,
``ouroboros_start_``), valid when a registered tool matches it.

Agents and skills
-----------------
``ouroboros:<name>`` must name a bundled agent (``src/ouroboros/agents/<name>.md``)
or a skill identifier; agents and skills share the plugin namespace. Skill
identifiers are the ones ``ouroboros.router.registry.SkillDispatchRegistry``
resolves for ``/ouroboros:<identifier>`` from ``skills/``: the directory name,
the frontmatter ``name`` (``ouroboros-run`` for skills/run), and the alias
fields. Both plugin manifests (.claude-plugin and .codex-plugin) point
``skills`` at ``./skills/`` without renaming anything.
``<name>``, ``{persona}``, and ``*`` are placeholders, valid when some agent or
skill matches. Not references: a bare ``/ouroboros:`` namespace prefix, and
``ouroboros:`` right after ``:`` (``plugin:ouroboros:ouroboros``, the host's
MCP server id).
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
import re
import tomllib

from ouroboros.mcp.tool_manifest import BUILTIN_TOOL_NAMES
from ouroboros.router.registry import SkillDispatchRegistry
from tests.unit.docs._corpus import REPO_ROOT, DocLine, corpus_files, iter_lines

AGENTS_DIR = REPO_ROOT / "src" / "ouroboros" / "agents"
SKILLS_DIR = REPO_ROOT / "skills"

_QUALIFIED_TOOL = re.compile(
    r"(?<![A-Za-z0-9_])mcp__(?:ouroboros|plugin_ouroboros_ouroboros)__([A-Za-z0-9_*]+)"
)
# ``.`` in the lookbehind skips key paths; ``_`` skips the tail of a qualified name.
_BARE_TOOL = re.compile(r"(?<![A-Za-z0-9_.])ouroboros_[A-Za-z0-9_*]*")
# ``name() {`` or ``function name`` (shell), ``def name(`` (Python).
_DEFINED_FUNCTION = re.compile(
    r"^\s*(?:function\s+(ouroboros_[A-Za-z0-9_]+)\b"
    r"|(ouroboros_[A-Za-z0-9_]+)\s*\(\)\s*\{"
    r"|def\s+(ouroboros_[A-Za-z0-9_]+)\s*\()"
)
_NAMESPACED = re.compile(r"(?<![A-Za-z0-9_:-])ouroboros:([A-Za-z0-9_<>{}*-]+)")
_NAME_PLACEHOLDER = re.compile(r"\*|<[^<>]+>|\{[^{}]+\}")

_DRIFT_HINT = "or add <!-- docs-drift: ignore --> if the line names it on purpose"


def tool_names() -> frozenset[str]:
    return BUILTIN_TOOL_NAMES


@cache
def agent_names() -> frozenset[str]:
    return frozenset(path.stem for path in AGENTS_DIR.glob("*.md"))


@cache
def skill_names() -> frozenset[str]:
    return frozenset(SkillDispatchRegistry.from_skills_dir(SKILLS_DIR).mapping)


@cache
def distribution_name() -> str:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return re.sub(r"[-.]+", "_", project["project"]["name"]).lower()


def defined_functions(lines: Iterable[DocLine]) -> frozenset[str]:
    """``ouroboros_*`` names the given lines define as functions in code blocks."""
    names: set[str] = set()
    for line in lines:
        if not line.in_code_block:
            continue
        match = _DEFINED_FUNCTION.match(line.text)
        if match:
            names.add(next(group for group in match.groups() if group))
    return frozenset(names)


def _matches_any(token: str, placeholder: re.Pattern[str], fill: str, known: Iterable[str]) -> bool:
    parts: list[str] = []
    last = 0
    for match in placeholder.finditer(token):
        parts.append(re.escape(token[last : match.start()]))
        parts.append(fill)
        last = match.end()
    parts.append(re.escape(token[last:]))
    if token.endswith("_"):
        parts.append("[A-Za-z0-9_]*")
    pattern = re.compile("".join(parts))
    return any(pattern.fullmatch(name) for name in known)


def tool_is_known(token: str, tools: frozenset[str]) -> bool:
    if token in tools:
        return True
    if "*" in token or token.endswith("_"):
        return _matches_any(token, re.compile(r"\*"), "[A-Za-z0-9_]*", tools)
    return False


def find_tool_violations(
    lines: Iterable[DocLine], tools: frozenset[str], not_tools: frozenset[str]
) -> list[str]:
    violations: list[str] = []
    for line in lines:
        tokens = [m.group(1) for m in _QUALIFIED_TOOL.finditer(line.text)]
        tokens += [m.group(0) for m in _BARE_TOOL.finditer(line.text)]
        for token in tokens:
            if token in not_tools or tool_is_known(token, tools):
                continue
            violations.append(
                f"{line.where()}: {token} — not a registered MCP tool "
                "(ouroboros.mcp.tool_manifest.BUILTIN_TOOL_NAMES); "
                f"use the registered name, {_DRIFT_HINT}"
            )
    return violations


def namespaced_is_known(name: str, known: frozenset[str]) -> bool:
    if name in known:
        return True
    if _NAME_PLACEHOLDER.search(name):
        return _matches_any(name, _NAME_PLACEHOLDER, "[A-Za-z0-9_-]+", known)
    return False


def find_namespace_violations(lines: Iterable[DocLine], known: frozenset[str]) -> list[str]:
    violations: list[str] = []
    for line in lines:
        for match in _NAMESPACED.finditer(line.text):
            name = match.group(1)
            if namespaced_is_known(name, known):
                continue
            violations.append(
                f"{line.where()}: ouroboros:{name} — no bundled agent "
                f"src/ouroboros/agents/{name}.md and no skill named {name}; "
                f"use an existing agent or skill name, {_DRIFT_HINT}"
            )
    return violations


@cache
def _corpus_lines() -> tuple[DocLine, ...]:
    return tuple(iter_lines())


def _not_tools() -> frozenset[str]:
    return defined_functions(_corpus_lines()) | {distribution_name()}


def test_corpus_and_registries_are_not_empty() -> None:
    assert corpus_files(), "the maintained-docs corpus matched no files"
    assert "ouroboros_interview" in tool_names()
    assert "socratic-interviewer" in agent_names()
    assert {"interview", "run", "ouroboros-run", "execute"} <= skill_names()


def test_docs_name_only_registered_mcp_tools() -> None:
    violations = find_tool_violations(_corpus_lines(), tool_names(), _not_tools())
    assert not violations, (
        f"{len(violations)} doc reference(s) to unregistered MCP tools:\n" + "\n".join(violations)
    )


def test_docs_name_only_existing_agents_and_skills() -> None:
    known = agent_names() | skill_names()
    violations = find_namespace_violations(_corpus_lines(), known)
    assert not violations, (
        f"{len(violations)} doc reference(s) to missing agents or skills:\n" + "\n".join(violations)
    )


def test_tool_checker_reports_unregistered_names_and_accepts_registered_ones() -> None:
    tools = frozenset({"ouroboros_interview", "ouroboros_start_auto"})
    lines = [
        DocLine("docs/t.md", 1, "Call `ouroboros_interveiw` first.", False),
        DocLine("docs/t.md", 2, "mcp__plugin_ouroboros_ouroboros__ouroboros_old_name", True),
        DocLine("docs/t.md", 3, "mcp_tool: ouroboros_start_auto", False),
        DocLine(
            "docs/t.md", 4, "`mcp__ouroboros__ouroboros_interview` and ouroboros_start_*", False
        ),
        DocLine("docs/t.md", 5, "ouroboros_interview(session_id=...) or `ouroboros_`", True),
        DocLine(
            "docs/t.md", 6, "`metadata.ouroboros_dispatch_errors` and `ouroboros_nope_*`", False
        ),
    ]
    violations = find_tool_violations(lines, tools, frozenset())
    assert [v.split(" — ")[0] for v in violations] == [
        "docs/t.md:1: ouroboros_interveiw",
        "docs/t.md:2: ouroboros_old_name",
        "docs/t.md:6: ouroboros_nope_*",
    ]
    assert violations[0].endswith(_DRIFT_HINT)


def test_names_the_corpus_defines_as_functions_are_not_tools() -> None:
    lines = [
        DocLine("skills/s.md", 10, "ouroboros_cli() {", True),
        DocLine("skills/s.md", 11, "function ouroboros_helper {", True),
        DocLine("skills/s.md", 12, "def ouroboros_probe(path):", True),
        DocLine("skills/s.md", 13, "ouroboros_prose() {", False),
        DocLine("skills/s.md", 20, "Define `ouroboros_cli` first.", False),
    ]
    defined = defined_functions(lines)
    assert defined == {"ouroboros_cli", "ouroboros_helper", "ouroboros_probe"}
    assert find_tool_violations(lines[4:], frozenset(), defined) == []
    assert distribution_name() == "ouroboros_ai"


def test_namespace_checker_reports_missing_names_and_accepts_existing_ones() -> None:
    known = frozenset({"socratic-interviewer", "interview", "ouroboros-run", "evaluator"})
    lines = [
        DocLine("skills/x.md", 1, "Load `ouroboros:socratic-interviwer`.", False),
        DocLine("skills/x.md", 2, "`/ouroboros:interview` and `/ouroboros:ouroboros-run`", False),
        DocLine(
            "skills/x.md", 3, "`ouroboros:{persona}`, `/ouroboros:<name>`, `/ouroboros:`", False
        ),
        DocLine("README.md", 4, "Failed to reconnect to plugin:ouroboros:ouroboros: -32000", False),
        DocLine("skills/x.md", 5, "`/ouroboros:retired-skill` and `/ouroboros:nope-<name>`", True),
    ]
    violations = find_namespace_violations(lines, known)
    assert [v.split(" — ")[0] for v in violations] == [
        "skills/x.md:1: ouroboros:socratic-interviwer",
        "skills/x.md:5: ouroboros:retired-skill",
        "skills/x.md:5: ouroboros:nope-<name>",
    ]


def test_renaming_a_registered_tool_or_agent_is_caught_in_the_real_corpus() -> None:
    lines = _corpus_lines()
    renamed_tool = find_tool_violations(
        lines, tool_names() - {"ouroboros_start_auto"}, _not_tools()
    )
    assert any(
        v.startswith("skills/auto/SKILL.md:") and ": ouroboros_start_auto — " in v
        for v in renamed_tool
    )
    renamed_agent = find_namespace_violations(
        lines, (agent_names() - {"socratic-interviewer"}) | skill_names()
    )
    assert any(": ouroboros:socratic-interviewer — " in v for v in renamed_agent)
    renamed_skill = find_namespace_violations(
        lines, agent_names() | (skill_names() - {"ouroboros-run"})
    )
    assert any(": ouroboros:ouroboros-run — " in v for v in renamed_skill)
