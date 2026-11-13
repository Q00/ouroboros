"""DEFAULT_SEMANTIC_MODEL is resolved on first access, not when the package is imported."""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
from typing import Any

import pytest

from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
import ouroboros.evaluation as evaluation
from ouroboros.evaluation import semantic

_NAME = "DEFAULT_SEMANTIC_MODEL"
_MISSING = object()
_PINNED_CONFIG = "models:\n  pin: true\nevaluation:\n  semantic_model: custom-semantic-model\n"


def _reference_default_semantic_model() -> str:
    """The expression the constant evaluated at import before this change."""
    return get_llm_model_for_role(
        "semantic_evaluation",
        backend=get_llm_backend_for_role("semantic_evaluation"),
    )


def _write_home(home: Path, config: str | None) -> None:
    (home / ".ouroboros").mkdir(parents=True)
    if config is not None:
        (home / ".ouroboros" / "config.yaml").write_text(config, encoding="utf-8")


def _run_python(home: Path, script: str) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OUROBOROS_")}
    env["HOME"] = str(home)
    env["OUROBOROS_TELEMETRY"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return json.loads(result.stdout.strip().splitlines()[-1])


_COUNT_CONFIG_PARSES = """
    import json, os
    import yaml

    parses = [0]
    _safe_load = yaml.safe_load

    def counting_safe_load(stream, *args, **kwargs):
        if os.path.basename(str(getattr(stream, "name", ""))) == "config.yaml":
            parses[0] += 1
        return _safe_load(stream, *args, **kwargs)

    yaml.safe_load = counting_safe_load
"""


class TestImportReadsNoConfig:
    def test_import_parses_no_config_and_first_access_resolves_once(self, tmp_path: Path) -> None:
        _write_home(tmp_path, _PINNED_CONFIG)
        out = _run_python(
            tmp_path,
            _COUNT_CONFIG_PARSES
            + """
    import ouroboros.evaluation
    import ouroboros.evaluation.semantic
    at_import = parses[0]

    package_value = ouroboros.evaluation.DEFAULT_SEMANTIC_MODEL
    first_access = parses[0] - at_import

    from ouroboros.evaluation import DEFAULT_SEMANTIC_MODEL as from_package
    from ouroboros.evaluation.semantic import DEFAULT_SEMANTIC_MODEL as from_module
    module_value = ouroboros.evaluation.semantic.DEFAULT_SEMANTIC_MODEL
    later_accesses = parses[0] - at_import - first_access

    from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
    reference = get_llm_model_for_role(
        "semantic_evaluation", backend=get_llm_backend_for_role("semantic_evaluation")
    )
    print(json.dumps({
        "at_import": at_import,
        "first_access": first_access,
        "later_accesses": later_accesses,
        "values": [package_value, from_package, from_module, module_value],
        "reference": reference,
    }))
    """,
        )

        assert out["at_import"] == 0
        assert out["first_access"] > 0  # resolved from config.yaml, on demand
        assert out["later_accesses"] == 0  # computed once per process
        assert out["values"] == [out["reference"]] * 4
        assert out["reference"] == "custom-semantic-model"

    @pytest.mark.parametrize("unopenable", ["directory", "mode_000"])
    def test_observable_difference_unopenable_config_no_longer_breaks_import(
        self, tmp_path: Path, unopenable: str
    ) -> None:
        """Pins the one deliberate difference from import-time resolution.

        ``main`` ran the reference expression while importing ``semantic``, so an
        existing but unopenable config.yaml made ``import ouroboros.evaluation``
        raise. Now the import succeeds and the first access raises that same
        exception type and message (and is retried, not cached).
        """
        _write_home(tmp_path, None)
        config = tmp_path / ".ouroboros" / "config.yaml"
        if unopenable == "directory":
            config.mkdir()
        else:
            if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
                pytest.skip("needs POSIX permissions enforced for the current user")
            config.write_text("llm:\n  backend: claude\n", encoding="utf-8")
            config.chmod(0)
        try:
            out = _run_python(
                tmp_path,
                """
    import json

    def failure(read):
        try:
            read()
        except Exception as e:
            return [type(e).__name__, str(e)]
        return None

    import ouroboros.evaluation  # raised here on main
    from ouroboros.evaluation import semantic
    imported = True

    accesses = [
        failure(lambda: ouroboros.evaluation.DEFAULT_SEMANTIC_MODEL),
        failure(lambda: semantic.DEFAULT_SEMANTIC_MODEL),
    ]

    # The statement main executed at module level in semantic.py.
    from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
    main_import_error = failure(lambda: get_llm_model_for_role(
        "semantic_evaluation", backend=get_llm_backend_for_role("semantic_evaluation")
    ))
    print(json.dumps({
        "imported": imported,
        "accesses": accesses,
        "main_import_error": main_import_error,
        "cached": "DEFAULT_SEMANTIC_MODEL" in vars(semantic),
    }))
    """,
            )
        finally:
            if unopenable == "mode_000":
                config.chmod(0o600)

        expected_type = "IsADirectoryError" if unopenable == "directory" else "PermissionError"
        assert out["imported"] is True
        assert out["main_import_error"] is not None
        assert out["main_import_error"][0] == expected_type
        assert str(config) in out["main_import_error"][1]
        assert out["accesses"] == [out["main_import_error"]] * 2
        assert out["cached"] is False


@pytest.fixture
def unresolved() -> Iterator[None]:
    """Run with the constant not yet resolved; restore the module afterwards."""
    saved = vars(semantic).pop(_NAME, _MISSING)
    yield
    vars(semantic).pop(_NAME, None)
    if saved is not _MISSING:
        vars(semantic)[_NAME] = saved


@pytest.fixture
def resolver_calls(unresolved: None, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def backend_for_role(role: str) -> str:
        return "codex"

    def model_for_role(role: str, *, backend: str) -> str:
        calls.append((role, backend))
        return f"model-for-{backend}"

    monkeypatch.setattr(semantic, "get_llm_backend_for_role", backend_for_role)
    monkeypatch.setattr(semantic, "get_llm_model_for_role", model_for_role)
    return calls


class TestFirstAccess:
    def test_value_matches_the_role_model_lookup(
        self, unresolved: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_home(tmp_path, _PINNED_CONFIG)
        monkeypatch.setenv("HOME", str(tmp_path))

        expected = _reference_default_semantic_model()
        assert expected == evaluation.DEFAULT_SEMANTIC_MODEL
        assert semantic.DEFAULT_SEMANTIC_MODEL == "custom-semantic-model"

    def test_resolved_once_and_shared_by_both_import_paths(
        self, resolver_calls: list[tuple[str, str]]
    ) -> None:
        from ouroboros.evaluation import DEFAULT_SEMANTIC_MODEL as from_package
        from ouroboros.evaluation.semantic import DEFAULT_SEMANTIC_MODEL as from_module

        values = [from_package, from_module]
        values += [evaluation.DEFAULT_SEMANTIC_MODEL, semantic.DEFAULT_SEMANTIC_MODEL]

        assert resolver_calls == [("semantic_evaluation", "codex")]
        assert values == ["model-for-codex"] * 4
        assert vars(semantic)[_NAME] == "model-for-codex"
        assert _NAME not in vars(evaluation)  # the package forwards; one owner

    def test_failed_resolution_propagates_and_is_retried(
        self, resolver_calls: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        error = PermissionError(13, "Permission denied", "config.yaml")

        def failing_backend(role: str) -> str:
            raise error

        monkeypatch.setattr(semantic, "get_llm_backend_for_role", failing_backend)
        for _ in range(2):
            with pytest.raises(PermissionError) as exc_info:
                evaluation.DEFAULT_SEMANTIC_MODEL  # noqa: B018
            assert exc_info.value is error
        assert _NAME not in vars(semantic)

        monkeypatch.setattr(semantic, "get_llm_backend_for_role", lambda _role: "claude")
        assert semantic.DEFAULT_SEMANTIC_MODEL == "model-for-claude"

    def test_concurrent_first_access_resolves_once(
        self, unresolved: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()
        calls: list[str] = []

        def slow_model_for_role(role: str, *, backend: str) -> str:
            calls.append(role)
            assert release.wait(timeout=10)
            return "model-x"

        monkeypatch.setattr(semantic, "get_llm_backend_for_role", lambda _role: "claude")
        monkeypatch.setattr(semantic, "get_llm_model_for_role", slow_model_for_role)
        results: list[str] = []
        threads = [
            threading.Thread(target=lambda: results.append(evaluation.DEFAULT_SEMANTIC_MODEL))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        release.set()
        for thread in threads:
            thread.join(timeout=10)

        assert calls == ["semantic_evaluation"]
        assert results == ["model-x"] * 8


class TestAttributeSurface:
    def test_name_stays_public_without_resolving(
        self, resolver_calls: list[tuple[str, str]]
    ) -> None:
        assert _NAME in evaluation.__all__
        assert _NAME in dir(evaluation)
        assert _NAME in dir(semantic)
        assert resolver_calls == []

        namespace: dict[str, Any] = {}
        exec("from ouroboros.evaluation import *", namespace)
        assert namespace[_NAME] == "model-for-codex"

    @pytest.mark.parametrize("module", [evaluation, semantic], ids=["package", "semantic"])
    def test_unknown_names_fail_as_before(self, module: Any) -> None:
        with pytest.raises(AttributeError) as exc_info:
            module.NO_SUCH_NAME  # noqa: B018
        assert str(exc_info.value) == f"module {module.__name__!r} has no attribute 'NO_SUCH_NAME'"
        assert not hasattr(module, "NO_SUCH_NAME")
        assert getattr(module, "NO_SUCH_NAME", "fallback") == "fallback"
        with pytest.raises(ImportError, match="cannot import name 'NO_SUCH_NAME'"):
            exec(f"from {module.__name__} import NO_SUCH_NAME", {})
