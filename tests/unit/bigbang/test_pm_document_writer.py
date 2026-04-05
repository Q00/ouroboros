"""Unit tests for pm.md file writing — save_pm_document and generate_pm_markdown.

Tests the template-based PM document generation and file-writing logic
that takes markdown output and writes it to disk as pm.md.
"""

from pathlib import Path

from ouroboros.bigbang.pm_document import (
    generate_pm_markdown,
    save_pm_document,
)
from ouroboros.bigbang.pm_seed import PMSeed, UserStory

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_seed(**overrides) -> PMSeed:
    """Create a PMSeed with sensible defaults for testing."""
    defaults = {
        "pm_id": "pm_seed_test123",
        "product_name": "Task Manager",
        "goal": "Build a task management tool for small teams.",
        "user_stories": (
            UserStory(
                persona="team lead",
                action="assign tasks",
                benefit="work is distributed",
            ),
        ),
        "constraints": ("Must launch within 3 months",),
        "success_criteria": ("80% user adoption in first quarter",),
        "decide_later_items": ("API rate limiting", "Which database to use?"),
        "assumptions": ("Team has access to cloud infra",),
        "interview_id": "int_abc",
    }
    defaults.update(overrides)
    return PMSeed(**defaults)


# ──────────────────────────────────────────────────────────────────
# generate_pm_markdown tests
# ──────────────────────────────────────────────────────────────────


class TestGeneratePrdMarkdown:
    """Tests for template-based PM markdown generation."""

    def test_includes_title(self):
        """Generated markdown starts with product name as H1."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert md.startswith("# Task Manager\n")

    def test_includes_goal_section(self):
        """Generated markdown includes the Goal section."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "## Goal" in md
        assert "Build a task management tool" in md

    def test_includes_user_stories(self):
        """Generated markdown includes user stories in correct format."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "## User Stories" in md
        assert "**As a** team lead" in md
        assert "**I want to** assign tasks" in md
        assert "**so that** work is distributed" in md

    def test_includes_constraints(self):
        """Generated markdown includes constraints."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "## Constraints" in md
        assert "- Must launch within 3 months" in md

    def test_includes_success_criteria(self):
        """Generated markdown includes numbered success criteria."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "## Success Criteria" in md
        assert "1. 80% user adoption in first quarter" in md

    def test_includes_assumptions(self):
        """Generated markdown includes assumptions."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "## Assumptions" in md
        assert "- Team has access to cloud infra" in md

    def test_includes_decide_later_merged(self):
        """Generated markdown merges deferred and decide-later items into Decide Later."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "## Decide Later" in md
        assert "- API rate limiting" in md
        assert "- Which database to use?" in md
        assert "## Deferred Items" not in md

    def test_includes_interview_id_footer(self):
        """Generated markdown has footer with interview ID."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "*Interview ID: int_abc*" in md

    def test_includes_pm_id(self):
        """Generated markdown includes the PM ID."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        assert "*PM ID: pm_seed_test123*" in md

    def test_includes_created_at(self):
        """Generated markdown includes the seed created_at timestamp."""
        seed = _make_seed(created_at="2026-03-30T11:33:57+00:00")
        md = generate_pm_markdown(seed)
        assert "*Created At: 2026-03-30T11:33:57+00:00*" in md

    def test_omits_empty_sections(self):
        """Sections with no data are omitted from the output."""
        seed = PMSeed(
            pm_id="pm_minimal",
            product_name="Minimal",
            goal="Just a goal.",
            interview_id="int_min",
        )
        md = generate_pm_markdown(seed)
        assert "## Goal" in md
        assert "## User Stories" not in md
        assert "## Constraints" not in md
        assert "## Success Criteria" not in md
        assert "## Deferred Items" not in md
        assert "## Decide Later" not in md
        assert "## Assumptions" not in md

    def test_default_title_when_no_product_name(self):
        """Uses fallback title when product_name is empty."""
        seed = PMSeed(product_name="", goal="A goal")
        md = generate_pm_markdown(seed)
        assert "# Product Requirements Document\n" in md

    def test_no_goal_placeholder(self):
        """Shows placeholder when goal is empty."""
        seed = PMSeed(product_name="Test")
        md = generate_pm_markdown(seed)
        assert "*No goal specified.*" in md

    def test_brownfield_repos_section(self):
        """Includes brownfield repo context when present."""
        seed = _make_seed(
            brownfield_repos=({"path": "/code/myapp", "name": "MyApp", "desc": "Main app"},),
        )
        md = generate_pm_markdown(seed)
        assert "## Existing Codebase Context" in md
        assert "**MyApp**" in md
        assert "`/code/myapp`" in md
        assert "Main app" in md

    def test_codebase_context_excluded(self):
        """Codebase analysis is excluded from PM document."""
        seed = _make_seed(codebase_context="Python FastAPI with PostgreSQL.")
        md = generate_pm_markdown(seed)
        assert "### Codebase Analysis" not in md

    def test_multiple_user_stories_numbered(self):
        """Multiple user stories are numbered sequentially."""
        seed = _make_seed(
            user_stories=(
                UserStory(persona="admin", action="manage users", benefit="control access"),
                UserStory(persona="dev", action="deploy code", benefit="ship faster"),
            ),
        )
        md = generate_pm_markdown(seed)
        assert "1. **As a** admin" in md
        assert "2. **As a** dev" in md

    def test_omits_empty_created_at(self):
        """Created At is omitted when empty or None."""
        seed = PMSeed(pm_id="pm_1", product_name="Test", goal="A goal", created_at="")
        md = generate_pm_markdown(seed)
        assert "Created At" not in md

    def test_omits_empty_interview_id(self):
        """Interview ID is omitted when empty or None."""
        seed = PMSeed(pm_id="pm_1", product_name="Test", goal="A goal", interview_id="")
        md = generate_pm_markdown(seed)
        assert "Interview ID" not in md

    def test_footer_contains_both_pm_id_and_interview_id(self):
        """Footer contains both PM ID and Interview ID when present."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)
        footer_start = md.index("---")
        footer = md[footer_start:]
        assert "*PM ID: pm_seed_test123*" in footer
        assert "*Interview ID: int_abc*" in footer

    def test_brownfield_repo_empty_name_falls_back_to_path(self):
        """Brownfield repo with empty name falls back to path."""
        seed = _make_seed(
            brownfield_repos=({"name": "", "path": "/repo/path", "desc": "desc"},),
        )
        md = generate_pm_markdown(seed)
        assert "**/repo/path**" in md
        assert "(`/repo/path`)" in md

    def test_brownfield_repo_missing_path_renders_cleanly(self):
        """Brownfield repo with missing path renders without empty backticks."""
        seed = _make_seed(
            brownfield_repos=({"name": "MyApp"},),
        )
        md = generate_pm_markdown(seed)
        assert "**MyApp**" in md
        assert "(``)" not in md

    def test_omits_empty_pm_id(self):
        """PM ID is omitted when empty."""
        seed = PMSeed(pm_id="", product_name="Test", goal="A goal", interview_id="int_1")
        md = generate_pm_markdown(seed)
        assert "PM ID" not in md
        assert "*Interview ID: int_1*" in md

    def test_omits_footer_when_both_ids_empty(self):
        """Footer separator is omitted when both PM ID and Interview ID are empty."""
        seed = PMSeed(pm_id="", product_name="Test", goal="A goal", interview_id="")
        md = generate_pm_markdown(seed)
        assert "---" not in md
        assert "PM ID" not in md
        assert "Interview ID" not in md


# ──────────────────────────────────────────────────────────────────
# save_pm_document tests
# ──────────────────────────────────────────────────────────────────


class TestSavePrdDocument:
    """Tests for the save_pm_document file writing function."""

    def test_writes_to_default_ouroboros_dir(self, tmp_path: Path, monkeypatch):
        """Writes pm.md to .ouroboros/ in cwd when no output_dir specified."""
        monkeypatch.chdir(tmp_path)
        seed = _make_seed()

        path = save_pm_document(seed)

        assert path.exists()
        assert path.name == "pm.md"
        assert path.parent.name == ".ouroboros"
        assert path.parent.parent == tmp_path

    def test_writes_to_custom_output_dir(self, tmp_path: Path):
        """Writes pm.md to a custom output directory."""
        custom_dir = tmp_path / "output"
        seed = _make_seed()

        path = save_pm_document(seed, output_dir=custom_dir)

        assert path.exists()
        assert path == custom_dir / "pm.md"

    def test_creates_parent_directories(self, tmp_path: Path):
        """Creates parent directories if they don't exist."""
        nested = tmp_path / "a" / "b" / "c"
        seed = _make_seed()

        path = save_pm_document(seed, output_dir=nested)

        assert path.exists()
        assert nested.is_dir()

    def test_file_content_matches_generated_markdown(self, tmp_path: Path):
        """Written file content matches generate_pm_markdown output."""
        seed = _make_seed()
        expected = generate_pm_markdown(seed)

        path = save_pm_document(seed, output_dir=tmp_path)

        content = path.read_text(encoding="utf-8")
        assert content == expected

    def test_file_is_utf8_encoded(self, tmp_path: Path):
        """File is written with UTF-8 encoding."""
        seed = _make_seed(goal="Support für internationale Nutzer — ñ, ü, é")

        path = save_pm_document(seed, output_dir=tmp_path)

        content = path.read_text(encoding="utf-8")
        assert "für internationale Nutzer" in content
        assert "ñ, ü, é" in content

    def test_overwrites_existing_file(self, tmp_path: Path):
        """Overwrites an existing pm.md file."""
        seed1 = _make_seed(product_name="First Version")
        seed2 = _make_seed(product_name="Second Version")

        save_pm_document(seed1, output_dir=tmp_path)
        path = save_pm_document(seed2, output_dir=tmp_path)

        content = path.read_text()
        assert "Second Version" in content
        assert "First Version" not in content

    def test_accepts_string_output_dir(self, tmp_path: Path):
        """Accepts output_dir as a string path."""
        seed = _make_seed()

        path = save_pm_document(seed, output_dir=str(tmp_path))

        assert path.exists()
        assert path.name == "pm.md"

    def test_returns_path_object(self, tmp_path: Path):
        """Returns a Path object pointing to the saved file."""
        seed = _make_seed()

        result = save_pm_document(seed, output_dir=tmp_path)

        assert isinstance(result, Path)
        assert result.is_file()

    def test_contains_all_seed_sections(self, tmp_path: Path):
        """Saved file contains all expected PM sections from seed."""
        seed = _make_seed()

        path = save_pm_document(seed, output_dir=tmp_path)
        content = path.read_text()

        assert "# Task Manager" in content
        assert "## Goal" in content
        assert "## User Stories" in content
        assert "## Constraints" in content
        assert "## Success Criteria" in content
        assert "## Assumptions" in content
        assert "## Decide Later" in content

    def test_minimal_seed_produces_valid_file(self, tmp_path: Path):
        """A minimal seed still produces a valid pm.md file."""
        seed = PMSeed(pm_id="min", product_name="Min", goal="Minimal goal")

        path = save_pm_document(seed, output_dir=tmp_path)

        content = path.read_text()
        assert "# Min" in content
        assert "Minimal goal" in content
        assert len(content) > 50  # Non-trivial output
