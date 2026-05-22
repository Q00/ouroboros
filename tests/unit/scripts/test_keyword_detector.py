"""Tests for keyword-detector.py — setup gate and routing behavior."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

# Load keyword-detector.py as a module (it uses hyphens in filename)
_script_path = Path(__file__).resolve().parents[3] / "scripts" / "keyword-detector.py"
_spec = importlib.util.spec_from_file_location("keyword_detector", _script_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

detect_keywords = _mod.detect_keywords
SETUP_BYPASS_SKILLS = _mod.SETUP_BYPASS_SKILLS
main = _mod.main
is_first_time = _mod.is_first_time


class TestFirstTimePrefs:
    def test_missing_prefs_file_is_first_time(self, tmp_path):
        home = tmp_path
        with patch.object(_mod.Path, "home", return_value=home):
            assert is_first_time() is True

    def test_welcome_completed_marks_not_first_time(self, tmp_path):
        prefs_dir = tmp_path / ".ouroboros"
        prefs_dir.mkdir()
        (prefs_dir / "prefs.json").write_text('{"welcomeCompleted": "2026-05-06T00:00:00Z"}')

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is False

    def test_legacy_welcome_shown_marks_not_first_time(self, tmp_path):
        prefs_dir = tmp_path / ".ouroboros"
        prefs_dir.mkdir()
        (prefs_dir / "prefs.json").write_text('{"welcomeShown": true}')

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is False

    def test_existing_star_prompt_pref_marks_not_first_time(self, tmp_path):
        prefs_dir = tmp_path / ".ouroboros"
        prefs_dir.mkdir()
        (prefs_dir / "prefs.json").write_text('{"star_asked": true}')

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is False

    def test_missing_prefs_but_config_yaml_marks_not_first_time(self, tmp_path):
        # ``ooo setup`` writes config.yaml, but only the (skippable) welcome
        # flow writes welcomeCompleted. A configured user without prefs.json
        # must not be treated as first-time, otherwise the welcome suggestion
        # is re-injected on every keyword-less prompt forever.
        ouro_dir = tmp_path / ".ouroboros"
        ouro_dir.mkdir()
        (ouro_dir / "config.yaml").write_text("version: 1\n")

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is False

    def test_missing_prefs_but_db_marks_not_first_time(self, tmp_path):
        # The event-sourcing DB only exists once the MCP runtime has done real
        # work — an unambiguous "already used Ouroboros" signal.
        ouro_dir = tmp_path / ".ouroboros"
        ouro_dir.mkdir()
        (ouro_dir / "ouroboros.db").write_bytes(b"")

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is False

    def test_missing_prefs_but_mcp_registered_marks_not_first_time(self, tmp_path):
        # MCP registered in ~/.claude/mcp.json means setup has already run.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "mcp.json").write_text('{"mcpServers": {"ouroboros": {}}}')

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is False

    def test_pristine_home_is_still_first_time(self, tmp_path):
        # Regression guard for the discriminator: a truly empty home (no prefs,
        # no install artifacts, no MCP registration) is still a genuine first
        # run and SHOULD surface the welcome experience.
        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is True

    def test_malformed_prefs_stays_first_time_despite_install_markers(self, tmp_path):
        # Intentional asymmetry guard: a malformed prefs.json (valid JSON that
        # parses to a non-dict) stays first-time even when install markers are
        # present, because re-running welcome rewrites the corrupt file cleanly.
        # Pins the contract so the malformed branch is not "fixed" by routing
        # it through _has_prior_install_state().
        ouro_dir = tmp_path / ".ouroboros"
        ouro_dir.mkdir()
        (ouro_dir / "config.yaml").write_text("version: 1\n")
        (ouro_dir / "prefs.json").write_text('["not", "a", "dict"]')

        with patch.object(_mod.Path, "home", return_value=tmp_path):
            assert is_first_time() is True


class TestDetectKeywords:
    def test_ooo_qa_detected(self):
        result = detect_keywords("ooo qa src/main.py")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:qa"

    def test_ooo_qa_bare(self):
        result = detect_keywords("ooo qa")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:qa"

    def test_bare_ooo_maps_to_welcome(self):
        result = detect_keywords("ooo")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:welcome"

    def test_qa_check_trigger(self):
        result = detect_keywords("qa check this code")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:qa"

    def test_quality_check_trigger(self):
        result = detect_keywords("quality check please")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:qa"

    def test_qa_check_no_false_positive_on_checklist(self):
        result = detect_keywords("make a QA checklist")
        assert result["detected"] is False

    def test_quality_check_no_false_positive_on_checklist(self):
        result = detect_keywords("quality checklist for release")
        assert result["detected"] is False

    def test_no_match(self):
        result = detect_keywords("hello world")
        assert result["detected"] is False

    def test_ooo_auto_with_goal(self):
        result = detect_keywords('ooo auto "Add /healthz endpoint"')
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:auto"
        assert result["keyword"] == "ooo auto"

    def test_ooo_auto_bare(self):
        result = detect_keywords("ooo auto")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:auto"

    def test_ooo_auto_with_resume_flag(self):
        result = detect_keywords("ooo auto --resume auto_abc123")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:auto"

    def test_ooo_auto_does_not_collide_with_autopilot_natural_language(self):
        # Make sure the `auto` pattern does not over-match unrelated phrases.
        result = detect_keywords("turn on autopilot")
        assert result["suggested_skill"] != "/ouroboros:auto", (
            "bare 'autopilot' should not route to ooo auto"
        )

    def test_ooo_publish_detected(self):
        result = detect_keywords("ooo publish")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:publish"

    def test_ooo_publish_with_args(self):
        result = detect_keywords("ooo publish --dry-run")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:publish"

    def test_ooo_resume_session_detected(self):
        result = detect_keywords("ooo resume-session")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:resume-session"

    def test_ooo_resume_session_with_args(self):
        result = detect_keywords("ooo resume-session --all")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:resume-session"

    def test_ooo_resume_prose_does_not_route(self):
        # Guards the canonical-form-only decision: a prose mention of "ooo
        # resume" inside a sentence must NOT route to resume-session, because
        # word-boundary matching would otherwise mis-suggest session recovery
        # for ordinary text like "please ooo resume work on this".
        result = detect_keywords("please ooo resume work on this")
        assert result["suggested_skill"] != "/ouroboros:resume-session"

    def test_ooo_resume_bare_does_not_route(self):
        # The bare short form is intentionally unsupported — users must type
        # the unambiguous canonical "ooo resume-session".
        result = detect_keywords("ooo resume")
        assert result["suggested_skill"] != "/ouroboros:resume-session"

    def test_ooo_resume_session_does_not_collide_with_hyphen_extension(self):
        """Regression: ``ooo resume-session-extra`` (a future hypothetical
        sibling command, or a typo) used to falsely route to
        ``/ouroboros:resume-session`` because ``\\b`` triggers on every
        word/non-word transition — including the boundary between
        ``session`` and the trailing ``-``. The tightened end-boundary
        rejects a hyphen as a valid token terminator, so longer
        hyphenated identifiers cannot silently masquerade as the
        canonical command.
        """
        result = detect_keywords("ooo resume-session-extra --all")
        assert result["suggested_skill"] != "/ouroboros:resume-session", (
            "ooo resume-session-extra must not collapse onto resume-session"
        )

    def test_ooo_resume_session_with_trailing_punctuation_still_matches(self):
        """Sanity: trailing non-hyphen punctuation (space, ``.``, ``:``,
        newline, end-of-string) still terminates the match cleanly."""
        for text in (
            "ooo resume-session",
            "ooo resume-session ",
            "ooo resume-session.",
            "ooo resume-session: please",
            "ooo resume-session\nfollow-up",
        ):
            result = detect_keywords(text)
            assert result["suggested_skill"] == "/ouroboros:resume-session", (
                f"resume-session should still match for {text!r}"
            )


class TestSetupBypass:
    """qa skill has a no-MCP fallback, so it must bypass the setup gate."""

    def test_qa_in_bypass_list(self):
        assert "/ouroboros:qa" in SETUP_BYPASS_SKILLS

    def test_setup_and_help_in_bypass_list(self):
        assert "/ouroboros:setup" in SETUP_BYPASS_SKILLS
        assert "/ouroboros:help" in SETUP_BYPASS_SKILLS

    def test_resume_session_in_bypass_list(self):
        # resume-session reads the EventStore directly and is meant to be used
        # exactly when the MCP server is unreachable. It must bypass the gate.
        assert "/ouroboros:resume-session" in SETUP_BYPASS_SKILLS


class TestMainGate:
    """When MCP is not configured, bypass skills should NOT redirect to setup."""

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_qa_bypasses_setup_gate(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "ooo qa"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" not in out
        assert "/ouroboros:qa" in out

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_qa_check_alias_bypasses_setup_gate(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "qa check my code"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" not in out
        assert "/ouroboros:qa" in out

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_quality_check_alias_bypasses_setup_gate(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "quality check please"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" not in out
        assert "/ouroboros:qa" in out

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_non_bypass_skill_redirects_to_setup(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "ooo run"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" in out

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_ooo_auto_unconfigured_redirects_to_setup(self, _first, _mcp, capsys):
        # ooo auto is not in SETUP_BYPASS_SKILLS, so an unconfigured environment
        # must steer the user to /ouroboros:setup before running the skill.
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = 'ooo auto "Add /healthz endpoint"'
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" in out

    @patch.object(_mod, "is_mcp_configured", return_value=True)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_ooo_auto_configured_routes_to_auto_skill(self, _first, _mcp, capsys):
        # When MCP is configured, ooo auto must surface /ouroboros:auto rather
        # than the setup redirect.
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = 'ooo auto "Add /healthz endpoint"'
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:auto" in out
        assert "/ouroboros:setup" not in out

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_resume_session_bypasses_setup_gate(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "ooo resume-session"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" not in out
        assert "/ouroboros:resume-session" in out

    @patch.object(_mod, "is_mcp_configured", return_value=False)
    @patch.object(_mod, "is_first_time", return_value=False)
    def test_resume_session_with_args_bypasses_setup_gate(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "ooo resume-session --all"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" not in out
        assert "/ouroboros:resume-session" in out
