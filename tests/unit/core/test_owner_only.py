"""Every file that holds interview or data content is owner-only.

The protection cannot be an instruction to each writer to remember: a call
site that forgets is invisible. These tests pin the property at each site that
persists this class of content, including the case that mattered — a state
directory inherited at 0755 from an earlier version.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
from typing import Any

import pytest

from ouroboros.core.owner_only import secure_directory, write_owner_only


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_write_owner_only_never_exists_at_the_umask_default(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    write_owner_only(target, '{"answer": "confirmed"}')
    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == '{"answer": "confirmed"}'


def test_secure_directory_repairs_an_inherited_open_namespace_directory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Inside ~/.ouroboros the directory is ours — an inherited 0755 is repaired."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    directory = tmp_path / ".ouroboros" / "data"
    directory.mkdir(parents=True, mode=0o755)
    os.chmod(directory, 0o755)
    secure_directory(directory)
    assert _mode(directory) == 0o700


def test_secure_directory_leaves_a_caller_supplied_directory_alone(tmp_path: Path) -> None:
    """Outside the namespace the directory is the caller's.

    An explicitly supplied 0755 state directory was being narrowed to 0700,
    revoking collaborator access this package had no business revoking.
    Ownership follows the path's provenance, not which module calls mkdir.
    """
    directory = tmp_path / "shared-state"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    secure_directory(directory)
    assert _mode(directory) == 0o755


def test_interview_state_is_owner_only(tmp_path: Path) -> None:
    """The transcript holds confirmed data answers and lives indefinitely.

    The FILE is owner-only wherever it lands; a caller-supplied state
    directory keeps its own permissions — this is the reviewer's
    probe: InterviewEngine(state_dir=<existing 0755 dir>).
    """
    from ouroboros.bigbang.interview import InterviewEngine, InterviewState

    state_dir = tmp_path / "data"
    state_dir.mkdir(mode=0o755)
    os.chmod(state_dir, 0o755)

    engine: Any = InterviewEngine.__new__(InterviewEngine)
    engine.state_dir = state_dir
    state = InterviewState(interview_id="iv_owner_only", initial_context="ctx")
    asyncio.run(engine.save_state(state))

    saved = state_dir / "interview_iv_owner_only.json"
    assert _mode(saved) == 0o600
    assert _mode(state_dir) == 0o755


def test_overwriting_an_existing_open_file_upgrades_it(tmp_path: Path) -> None:
    """The creation mode is ignored for a file that already exists.

    A Seed or transcript left at 0644 by an earlier version must not keep that
    mode just because the write reuses the inode.
    """
    target = tmp_path / "existing.yaml"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)

    write_owner_only(target, "new")

    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == "new"


def test_writing_does_not_re_permission_the_callers_directory(tmp_path: Path) -> None:
    """A Seed goes wherever the caller asks — often a shared project directory.

    Narrowing that directory would be this package changing something that is
    not its own. Only the file it writes is its business.
    """
    project = tmp_path / "project"
    project.mkdir(mode=0o755)
    os.chmod(project, 0o755)

    write_owner_only(project / "seed.yaml", "seed: {}")

    assert _mode(project) == 0o755
    assert _mode(project / "seed.yaml") == 0o600


def test_seed_save_leaves_the_target_directory_alone(tmp_path: Path) -> None:
    """The Seed writer must not chmod a caller-controlled parent."""
    from ouroboros.bigbang.seed_generator import save_seed_sync
    from ouroboros.core.seed import (
        EvaluationPrinciple,
        ExitCondition,
        OntologySchema,
        Seed,
        SeedMetadata,
    )

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    seed_path = shared / "seed.yaml"

    seed = Seed(
        metadata=SeedMetadata(),
        goal="ship the lane",
        task_type="code",
        constraints=("Python 3.14+",),
        acceptance_criteria=("The lane answers",),
        ontology_schema=OntologySchema(name="lane", description="lane domain"),
        evaluation_principles=(
            EvaluationPrinciple(name="completeness", description="all done", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(name="done", description="criteria pass", criteria="100% satisfied"),
        ),
    )
    result = save_seed_sync(seed, seed_path)
    assert result.is_ok, result.error if result.is_err else None

    assert _mode(shared) == 0o755
    assert _mode(seed_path) == 0o600


def test_the_mode_is_established_not_repaired(tmp_path: Path) -> None:
    """Content reaches disk only through a file created owner-only.

    The earlier version chmod'd an existing file and suppressed failure, so a
    filesystem or ownership that refused the repair got the new secret at the
    old mode. Establishing the mode at creation removes the failure branch
    entirely, and the replacement is atomic so no reader sees a partial file.
    """
    target = tmp_path / "seed.yaml"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)

    write_owner_only(target, "SECRET")

    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == "SECRET"
    # No temporary is left behind on success.
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".")] == []


def test_a_failed_write_leaves_nothing_behind(tmp_path: Path, monkeypatch: Any) -> None:
    """A write that cannot complete must not deposit the content anywhere."""
    import ouroboros.core.owner_only as owner_only

    target = tmp_path / "seed.yaml"
    original_write = os.fdopen

    def _explode(*args: Any, **kwargs: Any) -> Any:
        handle = original_write(*args, **kwargs)
        handle.close()
        raise OSError("disk full")

    monkeypatch.setattr(owner_only.os, "fdopen", _explode)
    try:
        owner_only.write_owner_only(target, "SECRET")
    except OSError:
        pass
    else:  # pragma: no cover - the monkeypatch always raises
        raise AssertionError("expected the write to fail")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_every_transport_that_persists_a_transcript_is_owner_only(tmp_path: Path) -> None:
    """The plugin interview path writes the same transcript the stdio one does.

    Round-62 found it still on ``write_text``: a fourth persistence site behind
    three that had been converted. The class is "files carrying interview,
    PM, Seed, or fan-out content", and every member of it goes through the
    owner-only writer.
    """
    import asyncio

    from ouroboros.bigbang.interview import InterviewState
    from ouroboros.mcp.tools.authoring_handlers import _plugin_save_state

    state_dir = tmp_path / "data"
    state_dir.mkdir(mode=0o755)
    os.chmod(state_dir, 0o755)

    state = InterviewState(interview_id="iv_plugin", initial_context="ctx")
    result = asyncio.run(_plugin_save_state(state_dir, state))
    assert result.is_ok, result.error if result.is_err else None

    saved = Path(result.value)
    assert _mode(saved) == 0o600
    # The directory is caller-supplied (outside ~/.ouroboros), so its
    # permissions are its own; the FILE carries the guarantee.
    assert _mode(saved.parent) == 0o755


def test_owner_only_write_reports_durability(tmp_path: Path) -> None:
    """The owner-only write is also the durable write.

    Round-64 merged two independently-added guarantees: main gained an atomic
    write that fsyncs and reports durability, this branch gained owner-only
    creation. Resolving the conflict either way would have dropped one of
    them silently, so the write returns the durability signal it replaced.
    """
    target = tmp_path / "state.json"
    assert write_owner_only(target, "{}") is True
    assert _mode(target) == 0o600


def test_owner_only_write_does_not_inherit_an_existing_open_mode(tmp_path: Path) -> None:
    """The mode is established at creation, never carried over from the target.

    An atomic-write helper conventionally preserves the previous mode. That is
    exactly what would keep a 0644 transcript written by an older version
    world-readable forever.
    """
    target = tmp_path / "legacy.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o644)
    assert _mode(target) == 0o644

    write_owner_only(target, '{"round": 64}')

    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == '{"round": 64}'


def test_durability_is_reported_not_swallowed(tmp_path: Path, monkeypatch: Any) -> None:
    """A directory fsync that genuinely fails is surfaced, not suppressed.

    The caller logs on an unconfirmed write; if the failure were swallowed the
    log would claim a durability the filesystem never gave.
    """
    import errno as _errno

    from ouroboros.core import owner_only

    real_fsync = os.fsync
    target = tmp_path / "state.json"

    def _fail_directory_fsync(fd: int) -> None:
        if os.path.isdir(f"/dev/fd/{fd}") or stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(_errno.EIO, "directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(owner_only.os, "fsync", _fail_directory_fsync)

    assert write_owner_only(target, "{}") is False
    # The content is still written and still owner-only — only the durability
    # claim is withheld.
    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == "{}"


def test_no_descriptor_leaks_when_the_file_object_cannot_be_created(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The raw descriptor is closed when nothing takes ownership of it.

    Between ``os.open`` and ``os.fdopen`` the descriptor belongs to no file
    object, so a failure there leaks it — a long-lived server would exhaust
    its descriptors one failed transcript write at a time.
    """
    from ouroboros.core import owner_only

    opened: list[int] = []
    real_open = os.open

    def _recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            opened.append(fd)
        return fd

    def _failing_fdopen(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fdopen failed")

    monkeypatch.setattr(owner_only.os, "open", _recording_open)
    monkeypatch.setattr(owner_only.os, "fdopen", _failing_fdopen)

    target = tmp_path / "state.json"
    try:
        write_owner_only(target, "{}")
    except RuntimeError:
        pass
    else:  # pragma: no cover - the write must not succeed here
        raise AssertionError("expected the fdopen failure to propagate")

    assert opened, "the temporary was never created"
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_auto_pipeline_state_is_owner_only(tmp_path: Path) -> None:
    """Auto state holds the confirmed interview answers, including [from-data].

    Reached through Auto rather than through the interview engine, this
    writer was one of two still landing at the umask default.
    """
    from ouroboros.auto.state import AutoPipelineState, AutoStore

    store = AutoStore(tmp_path / "data")
    state = AutoPipelineState(goal="ship the thing", cwd=str(tmp_path))

    path = store.save(state)

    assert _mode(path) == 0o600
    # Round-trips: narrowing the mode must not cost readability to its owner.
    assert store.load(state.auto_session_id).auto_session_id == state.auto_session_id


def test_auto_pipeline_state_narrows_an_inherited_open_file(tmp_path: Path) -> None:
    """A state file left at 0644 by an earlier version is narrowed on save."""
    from ouroboros.auto.state import AutoPipelineState, AutoStore

    store = AutoStore(tmp_path / "data")
    state = AutoPipelineState(goal="ship the thing", cwd=str(tmp_path))
    path = store.save(state)
    os.chmod(path, 0o644)
    assert _mode(path) == 0o644

    store.save(state)

    assert _mode(path) == 0o600


def test_auto_state_directory_is_not_re_permissioned(tmp_path: Path) -> None:
    """`root` may be a directory the caller owns — narrowing it is not ours."""
    from ouroboros.auto.state import AutoPipelineState, AutoStore

    root = tmp_path / "caller-owned"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    AutoStore(root).save(AutoPipelineState(goal="ship the thing", cwd=str(tmp_path)))

    assert _mode(root) == 0o755


def _minimal_seed() -> Any:
    from ouroboros.core.seed import (
        EvaluationPrinciple,
        ExitCondition,
        OntologyField,
        OntologySchema,
        Seed,
        SeedMetadata,
    )

    return Seed(
        goal="Build a local CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("The CLI exits non-zero on bad input",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior"),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )


def test_auto_generated_seed_is_owner_only(tmp_path: Path) -> None:
    """The Auto Seed writer was the one Seed path still at the umask default.

    An auto-generated Seed carries the same requirement content as one written
    through the interview path, including answers confirmed from a data
    lookup.
    """
    from ouroboros.auto.adapters import save_seed

    written = Path(save_seed(_minimal_seed(), seeds_dir=tmp_path / "seeds"))

    assert _mode(written) == 0o600
    assert written.read_text(encoding="utf-8")


def test_auto_generated_seed_narrows_an_inherited_open_file(tmp_path: Path) -> None:
    from ouroboros.auto.adapters import save_seed

    seeds_dir = tmp_path / "seeds"
    # The SAME Seed, so the rewrite lands on the same path — a fresh Seed
    # would get a fresh seed_id and write a different file.
    seed = _minimal_seed()
    written = Path(save_seed(seed, seeds_dir=seeds_dir))
    os.chmod(written, 0o644)
    assert _mode(written) == 0o644

    assert Path(save_seed(seed, seeds_dir=seeds_dir)) == written

    assert _mode(written) == 0o600


def test_auto_seed_directory_keeps_its_own_permissions(tmp_path: Path) -> None:
    """`seeds_dir` may be a shared project directory the caller chose."""
    from ouroboros.auto.adapters import save_seed

    seeds_dir = tmp_path / "project-seeds"
    seeds_dir.mkdir(mode=0o755)
    os.chmod(seeds_dir, 0o755)

    save_seed(_minimal_seed(), seeds_dir=seeds_dir)

    assert _mode(seeds_dir) == 0o755


def test_owner_only_write_survives_a_target_at_the_filename_limit(tmp_path: Path) -> None:
    """A caller that bounded its filename must not be broken by the temporary.

    The temporary used to embed the whole target name, adding a fixed 38
    characters, so a name sized to the 255-byte limit — which the Auto Seed
    writer produces deliberately — failed with ENAMETOOLONG before anything
    was written.
    """
    name = "s" * (255 - len(".yaml")) + ".yaml"
    target = tmp_path / name

    assert write_owner_only(target, "goal: ship\n") is True

    assert _mode(target) == 0o600
    assert target.read_text(encoding="utf-8") == "goal: ship\n"
    # And nothing was left behind.
    assert [path.name for path in tmp_path.iterdir()] == [name]


def test_no_bytes_are_written_when_the_filesystem_widens_the_mode(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The widened-mode check must run BEFORE the content exists.

    This check exists for the filesystem that ignores or widens the requested
    0600. Verifying it after the write meant that on exactly that filesystem
    the content had already existed group- or world-readable — the window the
    check was added to close.
    """
    from ouroboros.core import owner_only

    real_open = os.open
    widened: list[Path] = []

    def _widening_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            os.fchmod(fd, 0o644)  # the filesystem "ignores" the requested mode
            widened.append(Path(path))
        return fd

    written: list[str] = []
    real_fdopen = os.fdopen

    def _recording_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        written.append("fdopen")
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(owner_only.os, "open", _widening_open)
    monkeypatch.setattr(owner_only.os, "fdopen", _recording_fdopen)

    target = tmp_path / "secret.json"
    with pytest.raises(OSError, match="owner-only"):
        write_owner_only(target, '{"answer": "confirmed"}')

    # The failure happened before anything could be wrapped for writing.
    assert written == [], "content was written into a widened temporary"
    assert not target.exists()
    assert widened, "the probe never created a temporary"
    assert list(tmp_path.iterdir()) == [], "a widened temporary was left behind"


def test_native_windows_degrades_loudly_instead_of_refusing(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    """The guarantee is scoped to POSIX; Windows still writes, and says so.

    Round 71 made this path refuse outright; round 72 pointed out native
    Windows is an advertised (experimental) platform and refusing every
    Interview/Seed/PM/Auto write makes it unusable. The write proceeds
    atomically under the inherited ACL, the degradation is stated once per
    process rather than discovered, and the vacuous mode check does not run —
    a check that cannot fail must not stand in for a guarantee.
    """
    import logging

    from ouroboros.core import owner_only

    monkeypatch.setattr(owner_only, "_posix", lambda: False)
    monkeypatch.setattr(owner_only, "_degradation_warned", False)

    target = tmp_path / "secret.json"
    with caplog.at_level(logging.WARNING, logger="ouroboros.core.owner_only"):
        assert write_owner_only(target, '{"answer": "confirmed"}') is True
        write_owner_only(target, '{"answer": "again"}')

    assert target.read_text(encoding="utf-8") == '{"answer": "again"}'
    degradations = [r for r in caplog.records if "inherited ACL" in r.getMessage()]
    assert len(degradations) == 1, "the degradation must be stated exactly once per process"
    # Atomic on that platform too: no temporary survives.
    assert [path.name for path in tmp_path.iterdir()] == ["secret.json"]


def test_fanout_registry_register_is_owner_only(tmp_path: Path) -> None:
    """A fan-out record carries the producer's request verbatim.

    ``synthesizer_input`` is the code-investigation request or the persona
    panel entries the transcript produced, so the record is the same artifact
    class and takes the same writer. This site was still on ``write_text`` and
    produced 0644 under a 022 umask.
    """
    from ouroboros.mcp.tools.subagent import FANOUT_KIND_CODE_INVESTIGATION, FanoutRegistry

    registry = FanoutRegistry(tmp_path / "fanout")
    fanout_id = registry.register(
        kind=FANOUT_KIND_CODE_INVESTIGATION,
        session_id="sess-mode",
        correlation_key="context.lane_id",
        expected_keys=["code_facts"],
        synthesizer_input={"request": {"question": "which module owns auth?"}},
    )
    saved = tmp_path / "fanout" / f"{fanout_id}.json"

    assert _mode(saved) == 0o600
    assert registry.load(fanout_id) is not None


def test_fanout_registry_narrows_a_record_left_at_0644(tmp_path: Path) -> None:
    """A record written before this change is narrowed on the next write."""
    from ouroboros.mcp.tools.subagent import FANOUT_KIND_CODE_INVESTIGATION, FanoutRegistry

    registry = FanoutRegistry(tmp_path / "fanout")
    fanout_id = registry.register(
        kind=FANOUT_KIND_CODE_INVESTIGATION,
        session_id="sess-narrow",
        correlation_key="context.lane_id",
        expected_keys=["code_facts"],
        synthesizer_input={},
    )
    saved = tmp_path / "fanout" / f"{fanout_id}.json"
    saved.chmod(0o644)

    registry.register(
        kind=FANOUT_KIND_CODE_INVESTIGATION,
        session_id="sess-narrow",
        correlation_key="context.lane_id",
        expected_keys=["code_facts"],
        synthesizer_input={},
        fanout_id=fanout_id,
    )

    assert _mode(saved) == 0o600
