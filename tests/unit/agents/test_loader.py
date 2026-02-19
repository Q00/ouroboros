"""Unit tests for ouroboros.agents.loader module."""

from pathlib import Path

import pytest

from ouroboros.agents.loader import (
    clear_cache,
    extract_list_items,
    extract_section,
    load_agent_prompt,
    load_agent_section,
    load_persona_prompt_data,
)


@pytest.fixture(autouse=True)
def _clear_loader_cache() -> None:
    """Clear lru_cache after every test to prevent cross-test pollution."""
    yield
    clear_cache()
    # Also reset lateral.py's lazy-loaded global
    import ouroboros.resilience.lateral as _lat

    _lat._PERSONA_STRATEGIES = None


# ---------------------------------------------------------------------------
# Test data: agent names
# ---------------------------------------------------------------------------

ORIGINAL_AGENTS = [
    "socratic-interviewer",
    "seed-architect",
    "evaluator",
    "ontologist",
    "hacker",
    "researcher",
    "simplifier",
    "architect",
    "contrarian",
]

NEW_AGENTS = [
    "semantic-evaluator",
    "consensus-reviewer",
    "advocate",
    "judge",
    "ontology-analyst",
    "code-executor",
    "research-agent",
    "analysis-agent",
]

ALL_AGENTS = ORIGINAL_AGENTS + NEW_AGENTS

# Personas with structured data (YOUR APPROACH + YOUR QUESTIONS)
# Note: ontologist uses a different structure (THE FOUR FUNDAMENTAL QUESTIONS)
PERSONA_AGENTS = [
    "hacker",
    "contrarian",
    "simplifier",
    "researcher",
]


# ---------------------------------------------------------------------------
# TestLoadAgentPrompt
# ---------------------------------------------------------------------------


class TestLoadAgentPrompt:
    """Test load_agent_prompt function."""

    def test_load_existing_agent(self) -> None:
        """load_agent_prompt loads socratic-interviewer and returns content."""
        content = load_agent_prompt("socratic-interviewer")

        assert isinstance(content, str)
        assert len(content) > 0
        assert "Socratic Interviewer" in content or "socratic" in content.lower()

    def test_load_nonexistent_agent(self) -> None:
        """load_agent_prompt raises FileNotFoundError for missing agent."""
        with pytest.raises(FileNotFoundError) as exc_info:
            load_agent_prompt("nonexistent-agent-12345")

        error_message = str(exc_info.value)
        assert "nonexistent-agent-12345.md" in error_message
        assert "not found" in error_message.lower()

    @pytest.mark.parametrize("agent_name", ORIGINAL_AGENTS)
    def test_load_all_original_agents(self, agent_name: str) -> None:
        """load_agent_prompt successfully loads all original agents."""
        content = load_agent_prompt(agent_name)

        assert isinstance(content, str)
        assert len(content) > 0
        # All agent files should have markdown headers
        assert "#" in content

    @pytest.mark.parametrize("agent_name", NEW_AGENTS)
    def test_load_all_new_agents(self, agent_name: str) -> None:
        """load_agent_prompt successfully loads all new agents."""
        content = load_agent_prompt(agent_name)

        assert isinstance(content, str)
        assert len(content) > 0
        # New agents may be plain text prompts without markdown headers
        assert len(content) > 50  # Should have substantial content


# ---------------------------------------------------------------------------
# TestLoadAgentSection
# ---------------------------------------------------------------------------


class TestLoadAgentSection:
    """Test load_agent_section function."""

    def test_load_known_section(self) -> None:
        """load_agent_section extracts YOUR QUESTIONS section from hacker."""
        section_content = load_agent_section("hacker", "YOUR QUESTIONS")

        assert isinstance(section_content, str)
        assert len(section_content) > 0
        # The section should contain bullet points with questions
        assert "-" in section_content
        assert "?" in section_content

    def test_missing_section(self) -> None:
        """load_agent_section raises KeyError for missing section."""
        with pytest.raises(KeyError) as exc_info:
            load_agent_section("hacker", "NONEXISTENT SECTION")

        assert "NONEXISTENT SECTION" in str(exc_info.value)

    def test_case_insensitive(self) -> None:
        """load_agent_section section matching is case-insensitive."""
        # Load with different cases
        upper = load_agent_section("hacker", "YOUR QUESTIONS")
        lower = load_agent_section("hacker", "your questions")
        mixed = load_agent_section("hacker", "Your Questions")

        # All should return the same content
        assert upper == lower == mixed


# ---------------------------------------------------------------------------
# TestExtractSection
# ---------------------------------------------------------------------------


class TestExtractSection:
    """Test extract_section utility function."""

    def test_extract_from_content(self) -> None:
        """extract_section extracts content between markdown headers."""
        content = """
# Main Title

Some intro text.

## First Section

This is the first section content.
It has multiple lines.

## Second Section

This is the second section.

## Third Section

Third content here.
"""
        section = extract_section(content, "First Section")

        assert "This is the first section content" in section
        assert "multiple lines" in section
        # Should not include the next section
        assert "Second Section" not in section
        assert (
            "second section" not in section.lower()
            or "second section content" not in section.lower()
        )

    def test_section_stops_at_next_heading(self) -> None:
        """extract_section stops at the next ## heading."""
        content = """
## Section A

Content A line 1.
Content A line 2.

## Section B

Content B here.
"""
        section = extract_section(content, "Section A")

        assert "Content A line 1" in section
        assert "Content A line 2" in section
        # Should NOT bleed into Section B
        assert "Section B" not in section
        assert "Content B" not in section


# ---------------------------------------------------------------------------
# TestExtractListItems
# ---------------------------------------------------------------------------


class TestExtractListItems:
    """Test extract_list_items utility function."""

    def test_bullet_items(self) -> None:
        """extract_list_items extracts - item bullet points."""
        section_content = """
Here are some items:

- First item
- Second item with more text
- Third item

And some non-bullet text.
"""
        items = extract_list_items(section_content)

        assert len(items) == 3
        assert "First item" in items
        assert "Second item with more text" in items
        assert "Third item" in items

    def test_ignores_non_bullet_lines(self) -> None:
        """extract_list_items skips non-bullet content."""
        section_content = """
Introduction paragraph.

- Bullet one
- Bullet two

Closing paragraph.
Not a bullet.
"""
        items = extract_list_items(section_content)

        assert len(items) == 2
        assert "Bullet one" in items
        assert "Bullet two" in items
        # Should not include non-bullet lines
        assert "Introduction" not in items
        assert "Closing" not in items
        assert "Not a bullet" not in items


# ---------------------------------------------------------------------------
# TestLoadPersonaPromptData
# ---------------------------------------------------------------------------


class TestLoadPersonaPromptData:
    """Test load_persona_prompt_data function."""

    @pytest.mark.parametrize("agent_name", PERSONA_AGENTS)
    def test_all_personas_have_data(self, agent_name: str) -> None:
        """load_persona_prompt_data returns data for all persona agents."""
        data = load_persona_prompt_data(agent_name)

        assert data is not None
        assert data.system_prompt is not None
        assert data.approach_instructions is not None
        assert data.question_templates is not None

    @pytest.mark.parametrize("agent_name", PERSONA_AGENTS)
    def test_system_prompt_not_empty(self, agent_name: str) -> None:
        """load_persona_prompt_data system_prompt is not empty."""
        data = load_persona_prompt_data(agent_name)

        assert len(data.system_prompt) > 0

    @pytest.mark.parametrize("agent_name", PERSONA_AGENTS)
    def test_approach_instructions_count(self, agent_name: str) -> None:
        """load_persona_prompt_data approach_instructions has at least 3 items."""
        data = load_persona_prompt_data(agent_name)

        assert len(data.approach_instructions) >= 3

    @pytest.mark.parametrize("agent_name", PERSONA_AGENTS)
    def test_question_templates_count(self, agent_name: str) -> None:
        """load_persona_prompt_data question_templates has at least 3 items."""
        data = load_persona_prompt_data(agent_name)

        assert len(data.question_templates) >= 3

    @pytest.mark.parametrize("agent_name", PERSONA_AGENTS)
    def test_question_templates_end_with_question_mark(self, agent_name: str) -> None:
        """load_persona_prompt_data all question templates end with ?."""
        data = load_persona_prompt_data(agent_name)

        for question in data.question_templates:
            assert question.strip().endswith("?"), f"Question doesn't end with ?: {question}"

    def test_hacker_keywords(self) -> None:
        """load_persona_prompt_data hacker has 'unconventional' in system_prompt."""
        data = load_persona_prompt_data("hacker")

        assert "unconventional" in data.system_prompt.lower()

    def test_contrarian_keywords(self) -> None:
        """load_persona_prompt_data contrarian has 'question' in system_prompt."""
        data = load_persona_prompt_data("contrarian")

        assert "question" in data.system_prompt.lower()


# ---------------------------------------------------------------------------
# TestResolutionOrder
# ---------------------------------------------------------------------------


class TestResolutionOrder:
    """Test the 3-tier resolution order for agent files."""

    def test_env_var_takes_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """OUROBOROS_AGENTS_DIR env var takes priority over bundled agents."""
        # Create a custom agent directory with a modified agent file
        custom_agents_dir = tmp_path / "custom_agents"
        custom_agents_dir.mkdir()

        custom_agent_file = custom_agents_dir / "hacker.md"
        custom_content = (
            "# Custom Hacker Agent\n\nThis is a custom version with UNIQUE_MARKER_12345."
        )
        custom_agent_file.write_text(custom_content, encoding="utf-8")

        # Set the env var to point to our custom directory
        monkeypatch.setenv("OUROBOROS_AGENTS_DIR", str(custom_agents_dir))

        # Clear the cache to ensure fresh load
        clear_cache()

        # Load the agent
        content = load_agent_prompt("hacker")

        # Should load the custom version
        assert "UNIQUE_MARKER_12345" in content
        assert "Custom Hacker Agent" in content

    def test_fallback_to_bundle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Loader falls back to bundled agents when env var and CWD don't match."""
        # Ensure no env var is set
        monkeypatch.delenv("OUROBOROS_AGENTS_DIR", raising=False)

        # Change to a directory that doesn't have .claude-plugin/agents/
        monkeypatch.chdir(tmp_path)

        # Clear the cache
        clear_cache()

        # Load an agent - should fall back to bundled version
        content = load_agent_prompt("hacker")

        # Should successfully load the bundled version
        assert isinstance(content, str)
        assert len(content) > 0
        assert "hacker" in content.lower() or "Hacker" in content

    def test_cwd_relative_takes_priority_over_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CWD .claude-plugin/agents/ takes priority over bundled agents."""
        # Create .claude-plugin/agents/ in tmp directory
        cwd_agents_dir = tmp_path / ".claude-plugin" / "agents"
        cwd_agents_dir.mkdir(parents=True)

        custom_agent_file = cwd_agents_dir / "hacker.md"
        custom_content = "# CWD Hacker Agent\n\nThis is from CWD with MARKER_CWD_67890."
        custom_agent_file.write_text(custom_content, encoding="utf-8")

        # Ensure no env var
        monkeypatch.delenv("OUROBOROS_AGENTS_DIR", raising=False)

        # Change to the tmp directory
        monkeypatch.chdir(tmp_path)

        # Clear the cache
        clear_cache()

        # Load the agent
        content = load_agent_prompt("hacker")

        # Should load the CWD version
        assert "MARKER_CWD_67890" in content
        assert "CWD Hacker Agent" in content

    def test_env_var_takes_priority_over_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OUROBOROS_AGENTS_DIR env var takes priority over CWD .claude-plugin/agents/."""
        # Create both directories
        env_agents_dir = tmp_path / "env_agents"
        env_agents_dir.mkdir()

        cwd_agents_dir = tmp_path / ".claude-plugin" / "agents"
        cwd_agents_dir.mkdir(parents=True)

        # Create agent files in both locations with different content
        env_agent_file = env_agents_dir / "hacker.md"
        env_content = "# ENV Hacker\n\nENV_MARKER_11111"
        env_agent_file.write_text(env_content, encoding="utf-8")

        cwd_agent_file = cwd_agents_dir / "hacker.md"
        cwd_content = "# CWD Hacker\n\nCWD_MARKER_22222"
        cwd_agent_file.write_text(cwd_content, encoding="utf-8")

        # Set env var and change directory
        monkeypatch.setenv("OUROBOROS_AGENTS_DIR", str(env_agents_dir))
        monkeypatch.chdir(tmp_path)

        # Clear the cache
        clear_cache()

        # Load the agent
        content = load_agent_prompt("hacker")

        # Should load the ENV version (higher priority)
        assert "ENV_MARKER_11111" in content
        assert "CWD_MARKER_22222" not in content
