"""CLI tests for --compounding wiring on `ouroboros run workflow`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from typer.testing import CliRunner

if TYPE_CHECKING:
    pass

from ouroboros.cli.commands.run import app as run_app

runner = CliRunner()


SEED = {
    "goal": "compounding test",
    "constraints": [],
    "acceptance_criteria": ["AC 1", "AC 2"],
    "ontology_schema": {"name": "X", "description": "x", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {
        "seed_id": "seed-compound-test",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "ambiguity_score": 0.1,
    },
}


def _write_seed(tmp_path: Path) -> Path:
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump(SEED))
    return path


class TestCompoundingFlag:
    def test_compounding_and_sequential_are_mutually_exclusive(self, tmp_path: Path) -> None:
        seed_path = _write_seed(tmp_path)
        result = runner.invoke(
            run_app,
            ["workflow", str(seed_path), "--compounding", "--sequential"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in (result.output or "").lower()

    def test_compounding_threads_mode_into_run_orchestrator(
        self, tmp_path: Path
    ) -> None:
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding"],
            )
        assert result.exit_code == 0, result.output
        assert captured.get("mode") == "compounding"

    def test_default_run_has_no_mode_override(self, tmp_path: Path) -> None:
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(run_app, ["workflow", str(seed_path)])
        assert result.exit_code == 0
        assert captured.get("mode") is None


class TestCompoundingResume:
    """Tests for --compounding --resume <session_id> CLI wiring (AC-2 / Q6.2)."""

    # ------------------------------------------------------------------
    # Core wiring: compounding_resume_session_id passed, orchestrator
    # resume NOT triggered.
    # ------------------------------------------------------------------

    def test_compounding_resume_passes_compounding_resume_session_id(
        self, tmp_path: Path
    ) -> None:
        """--compounding --resume <id> sends compounding_resume_session_id to _run_orchestrator."""
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with (
            patch(
                "ouroboros.cli.commands.run._validate_compounding_resume_not_fresh_seed",
                return_value=None,  # bypass fresh-seed guard for this wiring test
            ),
            patch(
                "ouroboros.cli.commands.run._run_orchestrator",
                new=AsyncMock(side_effect=fake_run),
            ),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )
        assert result.exit_code == 0, result.output
        assert captured.get("compounding_resume_session_id") == "orch_abc123"

    def test_compounding_resume_sets_mode_compounding(self, tmp_path: Path) -> None:
        """--compounding --resume still sets mode='compounding'."""
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with (
            patch(
                "ouroboros.cli.commands.run._validate_compounding_resume_not_fresh_seed",
                return_value=None,
            ),
            patch(
                "ouroboros.cli.commands.run._run_orchestrator",
                new=AsyncMock(side_effect=fake_run),
            ),
        ):
            runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )
        assert captured.get("mode") == "compounding"

    def test_compounding_resume_nullifies_orchestrator_resume(
        self, tmp_path: Path
    ) -> None:
        """--compounding --resume must NOT trigger orchestrator session resume path.

        ``_run_orchestrator(seed_file, resume_session, ...)`` — the second
        positional / ``resume_session`` keyword must be None so the function
        doesn't call ``runner.resume_session()``.

        Captures BOTH positional args and kwargs so a positional-style call
        site can't bypass the assertion silently. Also stubs the fresh-seed
        validator so an environment with a real ``~/.ouroboros`` directory
        doesn't reject the call before ``_run_orchestrator`` is reached
        (which would leave the captures empty and trivially pass the
        ``None == None`` check).
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}
        called = []

        async def fake_run(*args, **kwargs):
            called.append(True)
            captured["positional"] = args
            captured.update(kwargs)

        with (
            patch(
                "ouroboros.cli.commands.run._run_orchestrator",
                new=AsyncMock(side_effect=fake_run),
            ),
            # Bypass the real fresh-seed validator so the patched
            # _run_orchestrator is always reached.
            patch(
                "ouroboros.cli.commands.run._validate_compounding_resume_not_fresh_seed",
                return_value=None,
            ),
        ):
            runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )

        # Confirm _run_orchestrator was actually invoked — otherwise an
        # empty `captured` dict would make the None check trivially pass.
        assert called, "_run_orchestrator was never invoked; the test cannot verify resume_session"

        positional = captured.get("positional", ())
        # resume_session is the 2nd positional parameter; check both
        # positional and kwarg forms so a positional-style call is also caught.
        positional_resume = positional[1] if len(positional) >= 2 else None
        kwarg_resume = captured.get("resume_session")
        assert positional_resume is None, (
            f"resume_session (positional[1]) must be None for --compounding, "
            f"got {positional_resume!r}"
        )
        assert kwarg_resume is None, (
            f"resume_session kwarg must be None for --compounding, got {kwarg_resume!r}"
        )

    def test_resume_without_compounding_is_orchestrator_resume(
        self, tmp_path: Path
    ) -> None:
        """--resume without --compounding keeps the existing orchestrator session resume path.

        compounding_resume_session_id must be None; resume_session (2nd positional
        arg to _run_orchestrator) must be set to the provided ID.
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            # Capture positional args too (seed_file, resume_session, …)
            captured["positional"] = args
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--resume", "orch_xyz999"],
            )
        # resume_session is the 2nd positional argument to _run_orchestrator
        positional = captured.get("positional", ())
        assert len(positional) >= 2, f"Expected ≥2 positional args, got: {positional}"
        assert positional[1] == "orch_xyz999", (
            f"resume_session (2nd positional) should be 'orch_xyz999', got {positional[1]!r}"
        )
        assert captured.get("compounding_resume_session_id") is None

    # ------------------------------------------------------------------
    # Mutual exclusivity: compounding resume + skip-completed
    # ------------------------------------------------------------------

    def test_compounding_resume_with_skip_completed_emits_warning(
        self, tmp_path: Path
    ) -> None:
        """--compounding --resume alongside --skip-completed must produce a warning.

        The checkpoint resume already handles AC-skipping; --skip-completed
        is silently ignored and a warning is printed.

        [[INVARIANT: --compounding --resume warns and ignores --skip-completed]]
        """
        seed_path = _write_seed(tmp_path)

        # Write a minimal skip-completed YAML so the file-load step doesn't fail.
        skip_path = tmp_path / "completed.yaml"
        skip_path.write_text("completed_acs: []\n")

        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with (
            patch(
                "ouroboros.cli.commands.run._validate_compounding_resume_not_fresh_seed",
                return_value=None,  # bypass fresh-seed guard for this warning test
            ),
            patch(
                "ouroboros.cli.commands.run._run_orchestrator",
                new=AsyncMock(side_effect=fake_run),
            ),
        ):
            result = runner.invoke(
                run_app,
                [
                    "workflow",
                    str(seed_path),
                    "--compounding",
                    "--resume",
                    "orch_abc123",
                    "--skip-completed",
                    str(skip_path),
                ],
            )
        # Must not fail — just warn
        assert result.exit_code == 0, result.output
        assert "ignored" in result.output.lower() or "warning" in result.output.lower()

    # ------------------------------------------------------------------
    # Runner integration: resume_session_id forwarded to execute_seed
    # ------------------------------------------------------------------

    def test_run_orchestrator_forwards_resume_session_id_to_execute_seed(
        self, tmp_path: Path
    ) -> None:
        """_run_orchestrator passes compounding_resume_session_id as resume_session_id
        to runner.execute_seed when mode='compounding'.

        Uses the CLI shim layer to capture what execute_seed is called with, then
        asserts the compounding_resume_session_id was threaded through.

        [[INVARIANT: compounding_resume_session_id flows from CLI to execute_serial via execute_seed]]
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_execute_seed(**kwargs):
            captured.update(kwargs)
            from unittest.mock import MagicMock

            result = MagicMock()
            result.is_ok = True
            result.value = MagicMock(
                success=True,
                session_id="s1",
                messages_processed=0,
                duration_seconds=0.0,
                summary={},
                final_message="",
                execution_id="e1",
            )
            return result

        # Patch execute_seed at the runner level (where it's used via the
        # OrchestratorRunner instance created inside _run_orchestrator).
        # We spy by patching OrchestratorRunner at its definition site so the
        # instance returned inside _run_orchestrator uses our fake.
        from unittest.mock import AsyncMock, MagicMock, patch as _patch

        mock_runner_instance = MagicMock()
        mock_runner_instance.execute_seed = AsyncMock(side_effect=fake_execute_seed)

        # Build a minimal EventStore stub that passes the initialize() check.
        mock_event_store = MagicMock()
        mock_event_store.initialize = AsyncMock()
        mock_event_store.close = AsyncMock()

        # SessionRepository.create_session must return a valid tracker so that
        # prepare_session() inside execute_seed doesn't fail before reaching our spy.
        mock_tracker = MagicMock()
        mock_tracker.session_id = "s1"
        mock_tracker.execution_id = "e1"

        ok_create = MagicMock()
        ok_create.is_err = False
        ok_create.value = mock_tracker

        mock_session_repo = MagicMock()
        mock_session_repo.create_session = AsyncMock(return_value=ok_create)
        mock_session_repo.track_progress = AsyncMock()

        with (
            _patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_event_store,
            ),
            _patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_session_repo,
            ),
            _patch(
                "ouroboros.orchestrator.OrchestratorRunner",
                return_value=mock_runner_instance,
            ),
            _patch(
                "ouroboros.cli.commands.run.maybe_prepare_task_workspace",
                return_value=None,
            ),
            _patch(
                "ouroboros.orchestrator.create_agent_runtime",
                return_value=MagicMock(),
            ),
        ):
            import asyncio

            from ouroboros.cli.commands.run import _run_orchestrator

            asyncio.run(
                _run_orchestrator(
                    seed_path,
                    resume_session=None,
                    mode="compounding",
                    compounding_resume_session_id="orch_abc123",
                    no_qa=True,
                )
            )

        assert captured.get("resume_session_id") == "orch_abc123", (
            f"resume_session_id not forwarded; captured: {captured}"
        )
        assert captured.get("mode") == "compounding"

    def test_execute_parallel_signature_threads_resume_session_id(self) -> None:
        """Regression: _execute_parallel must accept resume_session_id and
        execute_precreated_session must include it in parallel_kwargs when set.

        The compounding branch of _execute_parallel references resume_session_id
        directly. Without the parameter binding (and the caller forwarding it),
        any --resume --compounding run would NameError.

        [[INVARIANT: resume_session_id flows through OrchestratorRunner._execute_parallel]]
        """
        import ast
        import inspect
        from pathlib import Path

        runner_src = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ouroboros"
            / "orchestrator"
            / "runner.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(runner_src)

        # 1. _execute_parallel must declare resume_session_id as a parameter.
        execute_parallel_def = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_execute_parallel"
            ),
            None,
        )
        assert execute_parallel_def is not None, "_execute_parallel not found"
        param_names = {
            arg.arg
            for arg in (
                *execute_parallel_def.args.args,
                *execute_parallel_def.args.kwonlyargs,
            )
        }
        assert "resume_session_id" in param_names, (
            "_execute_parallel must accept resume_session_id; otherwise the "
            "compounding branch's reference to it raises NameError at runtime."
        )

        # 2. execute_precreated_session must forward resume_session_id when set.
        eps_def = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "execute_precreated_session"
            ),
            None,
        )
        assert eps_def is not None, "execute_precreated_session not found"
        forwards_resume = False
        eps_src = ast.unparse(eps_def)
        # Cheap textual check: the body must mention parallel_kwargs and bind
        # resume_session_id into it.  ast-level Subscript matching would be more
        # rigorous but adds complexity for little additional safety.
        forwards_resume = (
            'parallel_kwargs["resume_session_id"]' in eps_src
            or "parallel_kwargs['resume_session_id']" in eps_src
        )
        assert forwards_resume, (
            "execute_precreated_session must forward resume_session_id into "
            "parallel_kwargs (only when not None)."
        )

        # 3. Sanity: the actual function object exposes the parameter at runtime.
        from ouroboros.orchestrator.runner import OrchestratorRunner

        runtime_sig = inspect.signature(OrchestratorRunner._execute_parallel)
        assert "resume_session_id" in runtime_sig.parameters


class TestCompoundingResumeFreshSeedValidation:
    """Sub-AC 3 (b): Mutual exclusivity of --resume with fresh seed path.

    Tests that --compounding --resume raises an appropriate error when the seed
    has no prior compounding checkpoint ("fresh seed path" scenario).

    Compounding context from prior ACs:
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — chain serialization round-trip works.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — checkpoint payloads include sub-postmortems.
    - AC-3 established [[INVARIANT: Haiku verifier runs inline per AC before
      chain advances]] — invariants in checkpoints are verified.
    - Sub-ACs 1+2 established [[INVARIANT: checkpoints are only written after
      AC success, never on failure]] — a missing checkpoint means the seed was
      never successfully run in compounding mode.

    [[INVARIANT: _validate_compounding_resume_not_fresh_seed returns None when checkpoint dir absent]]
    [[INVARIANT: --compounding --resume with no prior checkpoint raises an error not a silent fresh run]]
    """

    # ------------------------------------------------------------------
    # Unit tests: _validate_compounding_resume_not_fresh_seed function
    # ------------------------------------------------------------------

    def test_validate_returns_none_when_not_resuming(self) -> None:
        """Validation always passes when compounding_resume_session_id is None.

        If the user doesn't request --resume, the function must be a no-op
        regardless of whether a checkpoint exists.
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed

        # None = not resuming → always valid
        assert _validate_compounding_resume_not_fresh_seed("any-seed-id", None) is None

    def test_validate_returns_none_when_checkpoint_dir_absent(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """When the default checkpoint dir doesn't exist, validation is skipped.

        This guards fresh installs and CI environments where no prior runs
        have occurred.  Failing-open is safer than blocking new users.

        [[INVARIANT: _validate_compounding_resume_not_fresh_seed returns None when checkpoint dir absent]]
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed

        # Point HOME to tmp_path which has no .ouroboros directory.
        monkeypatch.setenv("HOME", str(tmp_path))

        result = _validate_compounding_resume_not_fresh_seed(
            "any-seed-id", "orch_abc123"
        )
        assert result is None, (
            "Validation must skip (return None) when checkpoint dir is absent"
        )

    def test_validate_returns_error_when_checkpoint_dir_exists_but_empty(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """When the checkpoint dir exists but has no checkpoint for the seed → error.

        This is the 'fresh seed path' scenario: the user is trying to --resume
        a seed that has never been run in compounding mode.

        [[INVARIANT: --compounding --resume with no prior checkpoint raises an error not a silent fresh run]]
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed

        monkeypatch.setenv("HOME", str(tmp_path))
        ckpt_dir = tmp_path / ".ouroboros" / "data" / "checkpoints"
        ckpt_dir.mkdir(parents=True)

        result = _validate_compounding_resume_not_fresh_seed(
            "unknown-seed-id", "orch_abc123"
        )
        assert result is not None, (
            "Validation must return an error message for a fresh seed"
        )
        assert "cannot resume" in result.lower(), (
            f"Error message should mention 'cannot resume'; got: {result!r}"
        )

    def test_validate_returns_none_when_checkpoint_found(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """When a valid checkpoint exists for the seed, validation passes.

        This is the happy path: the user is resuming a real prior run.
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed
        from ouroboros.orchestrator.level_context import PostmortemChain
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        monkeypatch.setenv("HOME", str(tmp_path))
        ckpt_dir = tmp_path / ".ouroboros" / "data" / "checkpoints"
        store = CheckpointStore(base_path=ckpt_dir)
        store.initialize()

        # Pre-write a checkpoint for the seed (simulating a successful prior run).
        _write_compounding_checkpoint(
            store=store,
            seed_id="known-seed-id",
            session_id="prior-session",
            ac_index=0,
            chain=PostmortemChain(),
        )

        result = _validate_compounding_resume_not_fresh_seed(
            "known-seed-id", "orch_abc123"
        )
        assert result is None, (
            "Validation must pass (return None) when a checkpoint exists"
        )

    def test_validate_with_injectable_store_returns_error_on_missing(self) -> None:
        """Injectable store returning error → validation returns error message.

        Tests the function's injectable-store path (used in unit tests that
        don't touch the real filesystem at all).
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed
        from ouroboros.core.types import Result
        from unittest.mock import MagicMock

        mock_store = MagicMock()
        mock_store.load.return_value = Result.err("no checkpoint found")

        result = _validate_compounding_resume_not_fresh_seed(
            "any-seed", "orch_123", mock_store
        )
        assert result is not None
        assert "cannot resume" in result.lower()

    def test_validate_with_injectable_store_returns_none_on_found(self) -> None:
        """Injectable store returning success → validation passes.

        Tests the function's happy path using an injectable store mock.
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed
        from ouroboros.core.types import Result
        from unittest.mock import MagicMock

        mock_checkpoint = MagicMock()
        mock_checkpoint.state = {
            "mode": "compounding",
            "last_completed_ac_index": 1,
            "postmortem_chain": [],
        }
        mock_store = MagicMock()
        mock_store.load.return_value = Result.ok(mock_checkpoint)

        result = _validate_compounding_resume_not_fresh_seed(
            "any-seed", "orch_123", mock_store
        )
        assert result is None

    def test_validate_rejects_non_compounding_checkpoint(self) -> None:
        """A checkpoint exists for the seed but isn't a compounding checkpoint.

        Without inspecting state.mode, --resume would silently rehydrate a
        non-compounding checkpoint (e.g. left by a different mode/version)
        and produce nonsense.  The guard now requires
        state["mode"] == "compounding".
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed
        from ouroboros.core.types import Result
        from unittest.mock import MagicMock

        mock_checkpoint = MagicMock()
        mock_checkpoint.state = {"mode": "parallel", "some_other": "data"}
        mock_store = MagicMock()
        mock_store.load.return_value = Result.ok(mock_checkpoint)

        result = _validate_compounding_resume_not_fresh_seed(
            "any-seed", "orch_123", mock_store
        )
        assert result is not None
        assert "no compounding checkpoint" in result.lower()

    def test_validate_rejects_checkpoint_with_missing_mode(self) -> None:
        """Edge case: checkpoint state has no ``mode`` key at all → reject.

        Defensive coverage in case a partially-written or corrupted state
        dict lands in the store.
        """
        from ouroboros.cli.commands.run import _validate_compounding_resume_not_fresh_seed
        from ouroboros.core.types import Result
        from unittest.mock import MagicMock

        mock_checkpoint = MagicMock()
        mock_checkpoint.state = {"last_completed_ac_index": 0}  # mode key absent
        mock_store = MagicMock()
        mock_store.load.return_value = Result.ok(mock_checkpoint)

        result = _validate_compounding_resume_not_fresh_seed(
            "any-seed", "orch_123", mock_store
        )
        assert result is not None
        assert "no compounding checkpoint" in result.lower()


class TestLoadSeedIdFromYamlSizeGuard:
    """The early-probe ``_load_seed_id_from_yaml`` must apply the same DoS
    file-size guard as ``_load_seed_from_yaml``.  Otherwise an oversized seed
    file could still be parsed via this code path during early validation,
    bypassing the protection added to the main loader.
    """

    def test_oversized_seed_returns_none(self, tmp_path: Path) -> None:
        from unittest.mock import patch as _patch

        from ouroboros.cli.commands.run import _load_seed_id_from_yaml

        seed_path = tmp_path / "fake.yaml"
        seed_path.write_text("metadata:\n  seed_id: would-be-loaded\n")

        # Patch the validator to report the file as oversize regardless of
        # actual content, so the test doesn't have to write a real megabyte.
        with _patch(
            "ouroboros.cli.commands.run.InputValidator.validate_seed_file_size",
            return_value=(False, "too big"),
        ):
            result = _load_seed_id_from_yaml(seed_path)

        assert result is None, (
            "Oversize seed must NOT yield a seed_id from the early probe; "
            "the guard's DoS protection would otherwise be bypassable."
        )

    def test_size_valid_yaml_yields_seed_id(self, tmp_path: Path) -> None:
        from ouroboros.cli.commands.run import _load_seed_id_from_yaml

        seed_path = tmp_path / "ok.yaml"
        seed_path.write_text("metadata:\n  seed_id: my-seed\n")
        assert _load_seed_id_from_yaml(seed_path) == "my-seed"

    # ------------------------------------------------------------------
    # CLI integration tests: wiring validation into the workflow command
    # ------------------------------------------------------------------

    def test_cli_exits_with_error_when_validation_returns_error(
        self, tmp_path: Path
    ) -> None:
        """CLI exits non-zero when _validate_compounding_resume_not_fresh_seed returns an error.

        Tests that the workflow command correctly propagates a validation error
        to the process exit code and output.

        [[INVARIANT: --compounding --resume with no prior checkpoint raises an error not a silent fresh run]]
        """
        seed_path = _write_seed(tmp_path)

        with patch(
            "ouroboros.cli.commands.run._validate_compounding_resume_not_fresh_seed",
            return_value=(
                "Cannot resume: no compounding checkpoint found for seed "
                "'seed-compound-test'. The seed has not been run in compounding "
                "mode before. To start a fresh run, omit --resume."
            ),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )

        assert result.exit_code != 0, (
            f"CLI should exit non-zero for fresh-seed resume; "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert "cannot resume" in result.output.lower(), (
            f"Output must contain the error message; got: {result.output!r}"
        )

    def test_cli_proceeds_when_validation_returns_none(
        self, tmp_path: Path
    ) -> None:
        """CLI proceeds to _run_orchestrator when validation returns None (valid).

        When the seed has a valid checkpoint, the workflow continues normally.
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        with (
            patch(
                "ouroboros.cli.commands.run._validate_compounding_resume_not_fresh_seed",
                return_value=None,
            ),
            patch(
                "ouroboros.cli.commands.run._run_orchestrator",
                new=AsyncMock(side_effect=fake_run),
            ),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )

        assert result.exit_code == 0, (
            f"CLI should succeed when validation passes; output={result.output!r}"
        )
        # Compounding resume session ID must be forwarded to _run_orchestrator.
        assert captured.get("compounding_resume_session_id") == "orch_abc123"
