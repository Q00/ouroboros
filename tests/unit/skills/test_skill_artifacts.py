"""Unit tests for shared packaged skill resolution helpers."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from ouroboros.skills.artifacts import resolve_packaged_skills_dir


def test_codex_plugin_manifest_exposes_the_shared_skills_and_mcp_server() -> None:
    """The marketplace install must carry the first-use skills before setup runs."""
    repo_root = Path(__file__).resolve().parents[3]
    marketplace_path = repo_root / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    manifest_path = repo_root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert marketplace["name"] == "ouroboros"
    assert marketplace["plugins"] == [
        {
            "name": "ouroboros",
            "source": {"source": "local", "path": "."},
            "policy": {"installation": "AVAILABLE"},
            "category": "Developer Tools",
        }
    ]
    assert manifest["name"] == "ouroboros"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["interface"]["displayName"] == "Ouroboros"
    assert (repo_root / ".mcp.json").is_file()
    assert (repo_root / "skills" / "config" / "SKILL.md").is_file()
    assert (repo_root / "skills" / "ooo" / "SKILL.md").is_file()
    assert (repo_root / ".claude-plugin" / "skills" / "config" / "SKILL.md").is_file()


def test_first_use_onboarding_has_host_specific_model_settings_handoffs() -> None:
    """Codex and Claude package only the first-use wording for their own host."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_required_phrases = (
        "Setup Gate: First Use",
        "CODEX_SETUP_REQUIRED",
        "mcp_servers\\.ouroboros",
        "ouroboros setup --runtime codex",
        "uvx --from 'ouroboros-ai[mcp]' ouroboros setup --runtime codex",
        "설정하고 시작하기",
        "직접 모델 설정하기",
        "모델은 언제든 나중에 바꿀 수 있어요",
        "Use Codex default model",
        "Enter another model ID",
        "../config/SKILL.md",
        "temporary `localhost` address",
        "A previously completed welcome must never hide the setup gate",
    )
    codex_welcome = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in codex_required_phrases:
        assert phrase in codex_welcome, f"Codex skills: missing first-use handoff `{phrase}`"

    codex_entry = (repo_root / "skills" / "ooo" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: ooo" in codex_entry
    assert "../welcome/SKILL.md" in codex_entry

    claude_welcome = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Setup Gate: First Use" in claude_welcome
    assert "../setup/SKILL.md" in claude_welcome
    assert "previously completed welcome must never hide the setup gate" in claude_welcome
    assert "runtime_backend: claude" in claude_welcome
    assert '"ouroboros"' in claude_welcome
    assert "Codex" not in claude_welcome

    claude_setup = (repo_root / ".claude-plugin" / "skills" / "setup" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    claude_config = (repo_root / ".claude-plugin" / "skills" / "config" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Codex" not in claude_setup
    assert "Codex" not in claude_config
    assert "execution.default_model" in claude_config

    codex_rules = (repo_root / "src" / "ouroboros" / "codex" / "ouroboros.md").read_text(
        encoding="utf-8"
    )
    assert "### First use in Codex" in codex_rules
    assert "직접 모델 설정하기" in codex_rules
    assert "mere existence" in codex_rules

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        setup = (root / "setup" / "SKILL.md").read_text(encoding="utf-8")
        assert "### Step 5.1: Model Choice (Claude Code)" in setup
        assert "직접 모델 설정하기" in setup
        assert "ooo config" in setup

    assert "execution.default_model" in (repo_root / "skills" / "config" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _run_setup_gate(script: str, *, home: Path, codex_home: Path | None = None) -> str:
    """Run the packaged setup gate exactly as a host executes its Markdown snippet."""
    env = {"HOME": str(home)}
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        ["bash", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def test_codex_setup_gate_accepts_reordered_yaml_and_quoted_toml_mcp_key(tmp_path: Path) -> None:
    """The first-use gate reads structures, not nearby lines or one TOML spelling."""
    repo_root = Path(__file__).resolve().parents[3]
    codex_home = tmp_path / "alternate-codex-home"
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    config_path.parent.mkdir()
    codex_home.mkdir()
    config_path.write_text(
        """orchestrator:\n  retries: 3\n  timeout: 20\n  runtime_backend: codex\nllm:\n  qa_model: gpt-5\n  backend: codex\n""",
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        """[mcp_servers]\n\"ouroboros\" = { command = \"ouroboros\" }\n""",
        encoding="utf-8",
    )
    skill = (repo_root / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index('CODEX_HOME_DIR=', setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path, codex_home=codex_home) == "CODEX_READY"


def test_claude_setup_gate_accepts_reordered_yaml_and_json_mcp_key(tmp_path: Path) -> None:
    """Claude's mirrored first-use gate uses the same structural YAML check."""
    repo_root = Path(__file__).resolve().parents[3]
    config_path = tmp_path / ".ouroboros" / "config.yaml"
    mcp_path = tmp_path / ".claude" / "mcp.json"
    config_path.parent.mkdir()
    mcp_path.parent.mkdir()
    config_path.write_text(
        """llm:\n  qa_model: claude\n  backend: claude\norchestrator:\n  retries: 3\n  timeout: 20\n  runtime_backend: claude\n""",
        encoding="utf-8",
    )
    mcp_path.write_text('{"mcpServers": {"ouroboros": {"command": "ouroboros"}}}', encoding="utf-8")
    skill = (repo_root / ".claude-plugin" / "skills" / "welcome" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    setup_gate_start = skill.index("### Setup Gate: First Use")
    start = skill.index('if python3 - "$HOME/.ouroboros/config.yaml"', setup_gate_start)
    gate = skill[start : skill.index("\n```", start)]

    assert _run_setup_gate(gate, home=tmp_path) == "SETUP_READY"


def test_resolve_packaged_skills_dir_falls_back_to_repo_root_bundle_when_package_is_stub(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Editable installs should skip package stubs that do not contain skill bundles."""
    package_stub_dir = tmp_path / "repo" / "src" / "ouroboros" / "skills"
    package_stub_dir.mkdir(parents=True)
    package_stub_dir.joinpath("__init__.py").write_text("# package stub\n", encoding="utf-8")

    repo_skills_dir = tmp_path / "repo" / "skills"
    run_skill_dir = repo_skills_dir / "run"
    run_skill_dir.mkdir(parents=True)
    run_skill_dir.joinpath("SKILL.md").write_text("---\nname: run\n---\n", encoding="utf-8")

    anchor_file = tmp_path / "repo" / "src" / "ouroboros" / "codex" / "artifacts.py"
    anchor_file.parent.mkdir(parents=True)
    anchor_file.write_text("# anchor\n", encoding="utf-8")

    monkeypatch.setattr(
        "ouroboros.skills.artifacts.importlib.resources.files",
        lambda _package: package_stub_dir,
    )

    with resolve_packaged_skills_dir(anchor_file=anchor_file) as resolved_dir:
        assert resolved_dir == repo_skills_dir


def test_multitool_deferred_schema_guards_name_each_discovery_query() -> None:
    """Multi-tool skill guards must not reuse the wrong deferred schema query."""
    repo_root = Path(__file__).resolve().parents[3]
    expected = {
        "brownfield": [
            ('"+ouroboros brownfield"', "ouroboros_brownfield"),
        ],
        "setup": [
            ('"+ouroboros brownfield"', "ouroboros_brownfield"),
        ],
        "seed": [
            ('"+ouroboros seed"', "ouroboros_generate_seed"),
            ('"+ouroboros qa"', "ouroboros_qa"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "interview": [
            ('"+ouroboros interview"', "ouroboros_interview"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "evaluate": [
            ('"+ouroboros evaluate"', "ouroboros_evaluate"),
        ],
        "evolve": [
            ('"+ouroboros evolve"', "ouroboros_evolve_step"),
            ('"+ouroboros interview"', "ouroboros_interview"),
            ('"+ouroboros seed"', "ouroboros_generate_seed"),
            ('"+ouroboros lateral"', "ouroboros_lateral_think"),
        ],
        "pm": [
            ('"+ouroboros pm_interview"', "ouroboros_pm_interview"),
        ],
        "run": [
            ('"+ouroboros execute"', "ouroboros_start_execute_seed"),
            ('"+ouroboros execute"', "ouroboros_job_wait"),
            ('"+ouroboros execute"', "ouroboros_job_result"),
            ('"+ouroboros execute"', "ouroboros_ac_tree_hud"),
            ('"+ouroboros session signal"', "ouroboros_session_signal_targets"),
            ('"+ouroboros session signal"', "ouroboros_session_signal"),
        ],
    }

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        assert "the same tool-discovery load query you used above" not in "\n".join(
            skill_path.read_text(encoding="utf-8") for skill_path in root.glob("*/SKILL.md")
        )
        for skill, pairs in expected.items():
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            assert "deferred-schema guard" in text
            for query, tool in pairs:
                assert query in text
                assert tool in text


def test_packaged_skills_gate_fallback_on_callability_not_empty_discovery() -> None:
    """No packaged skill may route to fallback/Path B on empty discovery alone.

    ``render_mcp_server_instructions()`` declares that an empty discovery result
    for an already-exposed tool is a no-op (not unavailability). Skill bodies
    must therefore gate fallback on whether the MCP tool is *callable*, not on
    whether discovery returned a match — otherwise direct-exposure or
    already-loaded runtimes skip a callable tool. This guards that contract for
    every packaged skill in both trees.
    """
    repo_root = Path(__file__).resolve().parents[3]
    # Bare "empty discovery -> fallback" routing that predated the server contract.
    forbidden = (
        "If not → proceed to **Path B**",
        "If not → skip to **Fallback**",
        "returns no matching tools → proceed to **Path B**",
    )
    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill_path in root.glob("*/SKILL.md"):
            text = skill_path.read_text(encoding="utf-8")
            for phrase in forbidden:
                assert phrase not in text, (
                    f"{skill_path}: bare empty-discovery fallback `{phrase}` — "
                    "gate on tool callability instead"
                )
            # Every deferred-schema-guard "no matching tool" fallback must carry
            # a callability qualifier (an empty load for an exposed tool is fine).
            if "no matching tool" in text:
                assert "not already callable" in text, (
                    f"{skill_path}: `no matching tool` fallback not gated on callability"
                )


def test_brownfield_default_selection_ends_turn_in_every_skill_bundle() -> None:
    """All shipped host bundles must render the repo list before selection."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("brownfield", "setup"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            compact = " ".join(text.split())
            assert "Do NOT use `AskUserQuestion` for this selection" in text
            assert "end the turn with the repo grid as the final message" in compact
            assert '"keep" to keep the current defaults' in compact
            assert "IMMEDIATELY after showing the list" not in text
            assert "Default repo selection — IMMEDIATELY" not in text


def test_brownfield_scan_contract_matches_runtime_in_every_skill_bundle() -> None:
    """All shipped host bundles must describe the depth-bounded, self-only scan.

    ``scan_home_for_repos()`` walks ``scan_root`` at most two levels deep and
    registers each candidate self-only — it never expands Git worktree families
    via ``git worktree list``. Skill instructions in both ``skills/`` and
    ``.claude-plugin/skills/`` must not advertise the retired outside-root
    worktree-expansion behavior.
    """
    repo_root = Path(__file__).resolve().parents[3]
    stale_phrases = (
        "even outside the scan root directory",
        "even when they live outside `scan_root`",
        "even if their paths are outside `scan_root`",
        "git worktree list --porcelain",
    )

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("brownfield", "setup"):
            skill_path = root / skill / "SKILL.md"
            text = skill_path.read_text(encoding="utf-8")
            compact = " ".join(text.split())
            for phrase in stale_phrases:
                assert phrase not in compact, (
                    f"{skill_path}: stale worktree-expansion scan contract `{phrase}` — "
                    "the runtime scan is depth-bounded and registers repos self-only"
                )
            assert "Git worktree families are not expanded" in compact.replace(
                "families are NOT expanded", "families are not expanded"
            ), f"{skill_path}: missing self-only scan contract statement"
            assert "two" in compact and "level" in compact, (
                f"{skill_path}: missing depth-bounded (two-level) scan description"
            )


def test_background_skills_delegate_one_exclusive_job_observer() -> None:
    """Background skills delegate polling while the parent relays child events."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto", "ralph"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            compact = " ".join(text.split())
            compact = " ".join(normalized.split())
            assert "response.meta.job_observer" in normalized
            assert "exactly one" in normalized
            assert "read-only" in normalized
            assert "main session must not poll the same job" in compact
            assert "attention_required" in normalized
            assert "isolated worktree" in compact
            assert "tui open" in normalized
            assert "conversation" in normalized and "available" in normalized
            assert "wait_agent" in text
            assert "cannot revive" in compact or "cannot wake" in compact
            assert "must not poll" in normalized
            assert "stop live observation" in compact
            assert "keep the durable job running" in compact
            assert "instead of waiting indefinitely" in compact

    for skill in ("run", "auto", "ralph"):
        text = (repo_root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        normalized = text.lower()
        assert "spawn_agent" in text
        assert "run_observer" in text
        assert "wait" in normalized and "not a spawn" in normalized
        assert "stdio" in normalized
        assert "detached worker" in normalized
        assert "survive" in normalized or "continues" in normalized

    codex_instructions = (repo_root / "src" / "ouroboros" / "codex" / "ouroboros.md").read_text(
        encoding="utf-8"
    )
    assert "wait_agent" in codex_instructions
    assert "cannot revive a parent turn" in codex_instructions
    assert "stop live observation" in codex_instructions
    assert "without cancelling the durable job" in codex_instructions


def test_run_and_auto_route_human_intent_without_exposing_internal_ids() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            compact = " ".join(text.split())
            assert "ouroboros_session_signal_targets" in text
            assert "ouroboros_session_signal" in text
            assert "+ouroboros session signal" in text
            assert "never ask" in normalized and "id" in normalized
            assert "genuine" in normalized and ("tie" in normalized or "tied" in normalized)
            assert "omit `fallback_mode`" in compact


def test_active_conductor_skill_copies_cover_start_and_progress_briefing() -> None:
    """Every supported host gets the same user-facing control-surface facts."""
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            compact = " ".join(normalized.split())
            assert "efficient execution" in compact
            assert "quality-first execution" in compact
            assert "adaptive" in normalized and "quality_first" in normalized
            assert "observe" in normalized and "off" in normalized
            assert "strict" in normalized and "explicit" in normalized
            assert "run_configuration" in normalized
            assert "execution_plan" in normalized
            assert "discovery_summary" in normalized
            assert "parallel" in normalized
            assert "first scheduled ac" in compact
            assert "runtime" in normalized and "harness" in normalized


def test_active_conductor_skill_copies_cover_synapse_and_audited_action_order() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills"):
        for skill in ("run", "auto", "ralph"):
            text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
            normalized = text.lower()
            assert "ouroboros_session_signal_targets" in text
            assert "ouroboros_session_signal" in text
            assert 'mode="inform"' in text
            assert "queued" in normalized and "applied" in normalized
            assert "verify" in normalized
            assert "decide" in normalized
            assert "log" in normalized
            assert "act" in normalized
            assert "recommended_host_actions" in text
            assert "ouroboros_record_conductor_decision" in text


def test_active_conductor_guidance_is_english_canonical_without_locale_catalogs() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    paths = [
        root / skill / "SKILL.md"
        for root in (repo_root / "skills", repo_root / ".claude-plugin" / "skills")
        for skill in ("run", "auto", "ralph")
    ] + [repo_root / "src" / "ouroboros" / "codex" / "ouroboros.md"]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        normalized = text.lower()
        assert "english" in normalized and "canonical" in normalized
        assert "conversation language" in normalized
        assert "centralized" not in normalized
        assert "locale catalog" not in normalized
        assert "translation catalog" not in normalized
