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


class TestSetupBypass:
    """qa skill has a no-MCP fallback, so it must bypass the setup gate."""

    def test_qa_in_bypass_list(self):
        assert "/ouroboros:qa" in SETUP_BYPASS_SKILLS

    def test_setup_and_help_in_bypass_list(self):
        assert "/ouroboros:setup" in SETUP_BYPASS_SKILLS
        assert "/ouroboros:help" in SETUP_BYPASS_SKILLS


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
