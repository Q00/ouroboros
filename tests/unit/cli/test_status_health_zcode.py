"""Unit tests for the zcode model-config wiring in ``status health``."""

from __future__ import annotations

from pathlib import Path

from ouroboros.cli.commands import status as status_module
from ouroboros.config.zcode_model_config import ZcodeModelConfigStatus


def _probe_result(ok: bool, detail: str) -> ZcodeModelConfigStatus:
    return ZcodeModelConfigStatus(
        ok=ok, detail=detail, config_path=Path("/fake/.zcode/cli/config.json")
    )


def test_zcode_credentials_ok_includes_model_config_detail(monkeypatch):
    monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("OUROBOROS_RUNTIME", raising=False)
    monkeypatch.setattr(
        status_module,
        "inspect_zcode_model_config",
        lambda: _probe_result(True, "model.main -> p/m (provider entry present)"),
    )

    row = status_module._check_credentials({"llm": {"backend": "zcode"}}, Path("/fake/config.yaml"))

    assert row["status"] == "ok"
    assert "model.main -> p/m" in row["detail"]


def test_zcode_credentials_warning_when_model_config_missing(monkeypatch):
    monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("OUROBOROS_RUNTIME", raising=False)
    monkeypatch.setattr(
        status_module,
        "inspect_zcode_model_config",
        lambda: _probe_result(False, "config.json not found"),
    )

    row = status_module._check_credentials({"llm": {"backend": "zcode"}}, Path("/fake/config.yaml"))

    assert row["status"] == "warning"
    assert "config.json not found" in row["detail"]


def test_non_zcode_local_auth_backend_stays_ok_without_probe(monkeypatch):
    monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("OUROBOROS_RUNTIME", raising=False)

    def _fail_probe():
        raise AssertionError("probe must not run for non-zcode backends")

    monkeypatch.setattr(status_module, "inspect_zcode_model_config", _fail_probe)

    row = status_module._check_credentials({"llm": {"backend": "gjc"}}, Path("/fake/config.yaml"))

    assert row["status"] == "ok"


def test_runtime_backend_row_warns_for_zcode_without_model_config(monkeypatch):
    monkeypatch.setattr(
        status_module,
        "inspect_zcode_model_config",
        lambda: _probe_result(False, "config.json not found"),
    )

    row = status_module._runtime_backend_health_row("zcode", "zcode: /bin/zcode.cjs")

    assert row["status"] == "warning"
    assert "model config: config.json not found" in row["detail"]


def test_runtime_backend_row_ok_for_zcode_with_model_config(monkeypatch):
    monkeypatch.setattr(
        status_module,
        "inspect_zcode_model_config",
        lambda: _probe_result(True, "model.main -> p/m"),
    )

    row = status_module._runtime_backend_health_row("zcode", "zcode: /bin/zcode.cjs")

    assert row["status"] == "ok"


def test_runtime_backend_row_unchanged_for_other_backends(monkeypatch):
    def _fail_probe():
        raise AssertionError("probe must not run for non-zcode backends")

    monkeypatch.setattr(status_module, "inspect_zcode_model_config", _fail_probe)

    row = status_module._runtime_backend_health_row("codex", "codex: /bin/codex")

    assert row["status"] == "ok"
    assert row["detail"] == "codex: /bin/codex"


def test_zcode_candidate_uses_runtime_environment_path(monkeypatch, tmp_path):
    script = tmp_path / "custom-zcode.cjs"
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_ZCODE_CLI_PATH", str(script))

    candidates = status_module._candidate_cli_paths("zcode", {})

    assert candidates[0] == str(script)


def test_zcode_health_accepts_readable_script_path(monkeypatch, tmp_path):
    script = tmp_path / "custom-zcode.cjs"
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_ZCODE_CLI_PATH", str(script))
    monkeypatch.setattr(
        status_module,
        "inspect_zcode_model_config",
        lambda: _probe_result(True, "model.main -> p/m"),
    )

    row = status_module._check_runtime_backend(
        {"orchestrator": {"runtime_backend": "zcode"}, "llm": {"backend": "zcode"}}
    )

    assert row["status"] == "ok"
    assert str(script) in row["detail"]


def test_zcode_health_rejects_malformed_app_bundle(monkeypatch, tmp_path):
    contents = tmp_path / "ZCode.app" / "Contents"
    script = contents / "Resources" / "glm" / "zcode.cjs"
    script.parent.mkdir(parents=True)
    script.write_text("// zcode", encoding="utf-8")
    script.with_name(".node-bundle-meta.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_ZCODE_CLI_PATH", str(script))

    row = status_module._check_runtime_backend(
        {"orchestrator": {"runtime_backend": "zcode"}, "llm": {"backend": "zcode"}}
    )

    assert row["status"] == "error"
    assert "invalid JSON" in row["detail"]


def test_zcode_health_rejects_standalone_script_without_node(monkeypatch, tmp_path):
    script = tmp_path / "custom-zcode.cjs"
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_ZCODE_CLI_PATH", str(script))
    monkeypatch.setattr("ouroboros.zcode_cli_launcher.shutil.which", lambda _name: None)

    row = status_module._check_runtime_backend(
        {"orchestrator": {"runtime_backend": "zcode"}, "llm": {"backend": "zcode"}}
    )

    assert row["status"] == "error"
    assert "requires Node on PATH" in row["detail"]


def test_zcode_health_does_not_fallback_after_selected_override_is_unready(monkeypatch, tmp_path):
    override = tmp_path / "override-zcode.cjs"
    override.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    configured = tmp_path / "configured-zcode"
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)
    monkeypatch.setenv("OUROBOROS_ZCODE_CLI_PATH", str(override))
    monkeypatch.setattr("ouroboros.zcode_cli_launcher.shutil.which", lambda _name: None)

    row = status_module._check_runtime_backend(
        {
            "orchestrator": {
                "runtime_backend": "zcode",
                "zcode_cli_path": str(configured),
            },
            "llm": {"backend": "zcode"},
        }
    )

    assert row["status"] == "error"
    assert "requires Node on PATH" in row["detail"]
    assert str(override) in row["detail"]
    assert str(configured) not in row["detail"]
