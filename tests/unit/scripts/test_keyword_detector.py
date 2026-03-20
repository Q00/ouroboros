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
    def test_non_bypass_skill_redirects_to_setup(self, _first, _mcp, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "ooo run"
            main()
        out = capsys.readouterr().out
        assert "/ouroboros:setup" in out
