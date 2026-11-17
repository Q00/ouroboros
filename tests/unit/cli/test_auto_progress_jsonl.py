"""``ouroboros auto --progress-jsonl``: every progress event as one JSON line.

No agent runs: the pipeline is a stand-in that emits events through the
callback the CLI wires, as ``AutoPipeline`` does.
"""

from __future__ import annotations

from dataclasses import asdict, fields
import json
from pathlib import Path
import re
from typing import Any

import pytest
from typer.testing import CliRunner

from ouroboros.auto.handoff_contract import RUN_HANDOFF_STARTED_STATUS
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.progress import AutoProgressEvent
from ouroboros.cli.commands import auto as auto_command
from ouroboros.cli.main import app


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _events() -> list[AutoProgressEvent]:
    """One event of each kind, as the pipeline and the interview driver build them."""
    session = "auto_0123456789ab"
    return [
        AutoProgressEvent(session, "interview", "phase", "interview round 1/50"),
        AutoProgressEvent(
            session,
            "interview",
            "question",
            "question round 1/50",
            round=1,
            question="회문 판정에서 구두점도 무시할까요?",
        ),
        AutoProgressEvent(
            session,
            "interview",
            "answer",
            "answered round 1/50 from assumption",
            round=1,
            question="회문 판정에서 구두점도 무시할까요?",
            answer="Ignore punctuation as well.",
            answer_source="assumption",
        ),
        AutoProgressEvent(session, "review", "grade", "Seed grade B", grade="B"),
        AutoProgressEvent(session, "repair", "repair", "repair round 1", round=1),
    ]


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_each_event_is_one_flushed_json_line_with_every_field(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    write = auto_command._make_progress_jsonl_writer(path)
    names = [field.name for field in fields(AutoProgressEvent)]

    for count, event in enumerate(_events(), start=1):
        write(event)
        # Readable as soon as the call returns: a follower sees whole events.
        rows = _lines(path)
        assert len(rows) == count
        assert list(rows[-1]) == names
        assert rows[-1] == asdict(event)

    assert {row["kind"] for row in _lines(path)} == {
        "phase",
        "question",
        "answer",
        "grade",
        "repair",
    }
    assert path.read_text(encoding="utf-8").endswith("\n")
    # Non-ASCII stays readable text, not escapes.
    assert "구두점" in path.read_text(encoding="utf-8")


def test_the_file_is_appended_to_never_truncated(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    path.write_text('{"earlier": true}\n', encoding="utf-8")
    auto_command._make_progress_jsonl_writer(path)(_events()[0])
    assert _lines(path)[0] == {"earlier": True}
    assert _lines(path)[1]["kind"] == "phase"


def test_a_failed_write_warns_once_and_later_events_are_still_attempted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "later"
    path = folder / "progress.jsonl"
    write = auto_command._make_progress_jsonl_writer(path)
    events = _events()

    write(events[0])  # the folder does not exist yet
    write(events[1])
    folder.mkdir()
    write(events[2])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("could not write auto progress") == 1
    assert str(path) in captured.err
    assert [row["kind"] for row in _lines(path)] == ["answer"]


def test_composed_callbacks_run_in_order_and_none_means_no_callback() -> None:
    calls: list[str] = []
    assert auto_command._compose_progress_callbacks(None, None) is None
    only = calls.append
    assert auto_command._compose_progress_callbacks(None, only) is only  # type: ignore[arg-type]
    both = auto_command._compose_progress_callbacks(
        lambda _e: calls.append("first"), lambda _e: calls.append("second")
    )
    assert both is not None
    both(_events()[0])
    assert calls == ["first", "second"]


class _Pipeline:
    """Stands in for ``AutoPipeline``: emits every kind of event, then completes."""

    seen: list[Any] = []
    emitted: list[AutoProgressEvent] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.callback = kwargs.get("progress_callback")
        _Pipeline.seen.append(self.callback)

    async def run(self, run_state: Any) -> AutoPipelineResult:
        if self.callback is not None:
            for event in _events():
                _Pipeline.emitted.append(event)
                self.callback(event)
        return AutoPipelineResult(
            status="complete", auto_session_id=run_state.auto_session_id, phase="complete"
        )


class _Handler:
    def __init__(self, **_kwargs: Any) -> None:
        pass


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> type[_Pipeline]:
    from ouroboros.auto.state import AutoStore

    _Pipeline.seen = []
    _Pipeline.emitted = []
    monkeypatch.setattr(auto_command, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(
        auto_command, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )
    monkeypatch.setattr(auto_command, "AutoPipeline", _Pipeline)
    for name in (
        "InterviewHandler",
        "GenerateSeedHandler",
        "ExecuteSeedHandler",
        "StartExecuteSeedHandler",
    ):
        monkeypatch.setattr(auto_command, name, _Handler)
    return _Pipeline


@pytest.mark.usefixtures("auto_project_cwd")
@pytest.mark.parametrize("quiet", [False, True])
def test_cli_writes_every_event_and_keeps_the_text_trace_as_it_was(
    fake_pipeline: type[_Pipeline], tmp_path: Path, quiet: bool
) -> None:
    path = tmp_path / "out" / "progress.jsonl"
    path.parent.mkdir()
    args = ["auto", "Build a CLI", "--progress-jsonl", str(path)] + (["--quiet"] if quiet else [])

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert [row["kind"] for row in _lines(path)] == [e.kind for e in _events()]
    assert _lines(path) == [asdict(e) for e in fake_pipeline.emitted]
    output = _strip_ansi(result.output)
    if quiet:
        assert "[auto]" not in output  # --quiet silences the text, not the file
    else:
        assert "[auto] interview — interview round 1/50" in output
        assert "[auto] grade B — Seed grade B" in output


@pytest.mark.usefixtures("auto_project_cwd")
def test_without_the_option_nothing_changes(fake_pipeline: type[_Pipeline], tmp_path: Path) -> None:
    quiet = CliRunner().invoke(app, ["auto", "Build a CLI", "--quiet"])
    assert quiet.exit_code == 0
    assert fake_pipeline.seen[-1] is None  # --quiet alone still wires no callback
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.usefixtures("auto_project_cwd")
def test_an_unwritable_file_never_breaks_the_pipeline(
    fake_pipeline: type[_Pipeline], tmp_path: Path
) -> None:
    path = tmp_path / "missing-folder" / "progress.jsonl"

    result = CliRunner().invoke(app, ["auto", "Build a CLI", "--progress-jsonl", str(path)])

    assert result.exit_code == 0, result.output
    assert "Auto pipeline completed" in _strip_ansi(result.stdout)
    assert result.stderr.count("could not write auto progress") == 1
    assert "[auto] grade B — Seed grade B" in _strip_ansi(result.stdout)
    assert not path.exists()


@pytest.mark.usefixtures("auto_project_cwd")
@pytest.mark.parametrize("value", ["", "   ", "{dir}"])
def test_a_path_that_is_not_a_file_is_refused_before_anything_runs(
    fake_pipeline: type[_Pipeline], tmp_path: Path, value: str
) -> None:
    result = CliRunner().invoke(
        app, ["auto", "Build a CLI", "--progress-jsonl", value.format(dir=tmp_path)]
    )

    assert result.exit_code == 1
    assert "--progress-jsonl" in _strip_ansi(result.output)
    assert fake_pipeline.seen == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_project_cwd")
@pytest.mark.parametrize(
    ("quiet", "callback", "expected"),
    [(True, True, True), (False, True, False), (None, True, False), (None, False, True)],
)
async def test_the_run_wait_follows_quiet_not_whether_a_callback_is_wired(
    fake_pipeline: type[_Pipeline],
    monkeypatch: pytest.MonkeyPatch,
    quiet: bool | None,
    callback: bool,
    expected: bool,
) -> None:
    """A JSON-lines writer is a callback; it must not turn the run wait's text on."""
    waited: dict[str, Any] = {}

    async def run(self: _Pipeline, run_state: Any) -> AutoPipelineResult:
        return AutoPipelineResult(
            status="complete",
            auto_session_id=run_state.auto_session_id,
            phase="complete",
            run_handoff_status=RUN_HANDOFF_STARTED_STATUS,
            job_id="job_0123456789",
        )

    async def wait(result: AutoPipelineResult, **kwargs: Any) -> AutoPipelineResult:
        waited.update(kwargs)
        return result

    monkeypatch.setattr(_Pipeline, "run", run)
    monkeypatch.setattr(auto_command, "_await_run_handoff_terminal", wait)

    await auto_command._run_auto(
        goal="Build a CLI",
        resume=None,
        runtime=None,
        max_interview_rounds=1,
        max_repair_rounds=1,
        skip_run=False,
        progress_callback=(lambda _event: None) if callback else None,
        quiet=quiet,
    )

    assert waited["quiet"] is expected


def test_help_documents_the_option() -> None:
    result = CliRunner().invoke(app, ["auto", "--help"])
    assert result.exit_code == 0
    assert "--progress-jsonl" in _strip_ansi(result.output)
