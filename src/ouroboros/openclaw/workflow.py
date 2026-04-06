"""Channel-oriented workflow state for OpenClaw integrations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import structlog

from ouroboros.openclaw.store import OpenClawStateStore

log = structlog.get_logger(__name__)


class WorkflowEntryPoint(str):
    """Detected workflow entry stage."""

    INTERVIEW = "interview"
    EXECUTION = "execution"


class WorkflowStage(str):
    """High-level lifecycle stages for a channel workflow."""

    QUEUED = "queued"
    INTERVIEWING = "interviewing"
    SEED_GENERATION = "seed_generation"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """Identity of a Discord/OpenClaw channel."""

    channel_id: str
    guild_id: str | None = None

    @property
    def key(self) -> str:
        return f"{self.guild_id}:{self.channel_id}" if self.guild_id else self.channel_id


@dataclass(frozen=True, slots=True)
class EntryPointDetection:
    """Detected routing decision for a new workflow request."""

    entry_point: WorkflowEntryPoint
    reason: str


@dataclass(frozen=True, slots=True)
class ChannelWorkflowRequest:
    """Incoming channel workflow request."""

    channel: ChannelRef
    user_id: str | None
    message: str
    repo: str
    seed_content: str | None = None
    seed_path: str | None = None
    entry_point: WorkflowEntryPoint = WorkflowEntryPoint.INTERVIEW
    message_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelWorkflowRecord:
    """Persistent state for one channel workflow instance."""

    workflow_id: str
    channel_key: str
    channel_id: str
    guild_id: str | None
    user_id: str | None
    repo: str
    request_message: str
    entry_point: WorkflowEntryPoint
    request_fingerprint: str
    stage: WorkflowStage
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    interview_session_id: str | None = None
    seed_id: str | None = None
    seed_content: str | None = None
    seed_path: str | None = None
    job_id: str | None = None
    last_job_cursor: int = 0
    session_id: str | None = None
    execution_id: str | None = None
    pr_url: str | None = None
    final_result: str | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.stage in {WorkflowStage.COMPLETED, WorkflowStage.FAILED}

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("queued_at", "started_at", "completed_at"):
            value = payload[key]
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ChannelWorkflowRecord:
        data = dict(payload)
        for key in ("queued_at", "started_at", "completed_at"):
            if isinstance(data.get(key), str):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)


_SEED_HINTS = (
    "acceptance_criteria:",
    "task_type:",
    "brownfield_context:",
    "constraints:",
    "metadata:",
    "goal:",
)


def request_fingerprint(
    *,
    user_id: str | None,
    message: str,
    repo: str,
    entry_point: WorkflowEntryPoint,
    message_id: str | None = None,
    event_id: str | None = None,
) -> str:
    """Build a stable fingerprint for duplicate-delivery protection."""
    if event_id:
        return f"event:{event_id}"
    if message_id:
        return f"message:{message_id}"
    payload = "|".join(
        [
            user_id or "",
            repo,
            entry_point,
            " ".join(message.strip().lower().split()),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_entry_point(
    message: str,
    *,
    seed_content: str | None = None,
    seed_path: str | None = None,
) -> EntryPointDetection:
    """Infer whether a request should begin at interview or execution."""
    if seed_content:
        return EntryPointDetection(
            entry_point=WorkflowEntryPoint.EXECUTION,
            reason="explicit seed_content provided",
        )
    if seed_path:
        return EntryPointDetection(
            entry_point=WorkflowEntryPoint.EXECUTION,
            reason="explicit seed_path provided",
        )

    normalized = message.strip().lower()
    if not normalized:
        return EntryPointDetection(
            entry_point=WorkflowEntryPoint.INTERVIEW,
            reason="empty message defaults to interview",
        )

    if all(hint in normalized for hint in ("goal:", "acceptance_criteria:")):
        return EntryPointDetection(
            entry_point=WorkflowEntryPoint.EXECUTION,
            reason="seed-like yaml payload detected",
        )

    if sum(1 for hint in _SEED_HINTS if hint in normalized) >= 3:
        return EntryPointDetection(
            entry_point=WorkflowEntryPoint.EXECUTION,
            reason="multiple seed/spec markers detected",
        )

    if normalized.startswith("---") and any(hint in normalized for hint in _SEED_HINTS):
        return EntryPointDetection(
            entry_point=WorkflowEntryPoint.EXECUTION,
            reason="yaml document markers suggest a seed/spec payload",
        )

    return EntryPointDetection(
        entry_point=WorkflowEntryPoint.INTERVIEW,
        reason="natural-language or issue-style request detected",
    )


class ChannelRepoRegistry:
    """Persistent default repo mapping per channel."""

    def __init__(self, path: Path | None = None, store: OpenClawStateStore | None = None) -> None:
        self._store = store or OpenClawStateStore(path)

    def get(self, channel: ChannelRef) -> str | None:
        return self._store.get_repo(channel.key)

    def set(self, channel: ChannelRef, repo: str) -> None:
        self._store.set_repo(
            channel_key=channel.key,
            channel_id=channel.channel_id,
            guild_id=channel.guild_id,
            repo=repo,
        )

    def remove(self, channel: ChannelRef) -> None:
        self._store.remove_repo(channel.key)


class ChannelWorkflowManager:
    """Manage per-channel active workflows and queued requests."""

    def __init__(
        self, state_path: Path | None = None, store: OpenClawStateStore | None = None
    ) -> None:
        self._store = store or OpenClawStateStore(state_path)

    def enqueue(self, request: ChannelWorkflowRequest) -> ChannelWorkflowRecord:
        now = datetime.now(UTC)
        is_active = self.active_for_channel(request.channel) is not None
        fingerprint = request_fingerprint(
            user_id=request.user_id,
            message=request.message,
            repo=request.repo,
            entry_point=request.entry_point,
            message_id=request.message_id,
            event_id=request.event_id,
        )
        record = ChannelWorkflowRecord(
            workflow_id=f"wf_{uuid4().hex[:12]}",
            channel_key=request.channel.key,
            channel_id=request.channel.channel_id,
            guild_id=request.channel.guild_id,
            user_id=request.user_id,
            repo=request.repo,
            request_message=request.message,
            entry_point=request.entry_point,
            request_fingerprint=fingerprint,
            stage=(
                WorkflowStage.QUEUED
                if is_active
                else (
                    WorkflowStage.INTERVIEWING
                    if request.entry_point == WorkflowEntryPoint.INTERVIEW
                    else WorkflowStage.EXECUTING
                )
            ),
            queued_at=now,
            started_at=None if is_active else now,
            seed_content=request.seed_content,
            seed_path=request.seed_path,
        )
        self._store.upsert_workflow(record.to_json())
        return record

    def get(self, workflow_id: str) -> ChannelWorkflowRecord | None:
        row = self._store.get_workflow(workflow_id)
        return ChannelWorkflowRecord.from_json(row) if row is not None else None

    def latest_for_channel(self, channel: ChannelRef) -> ChannelWorkflowRecord | None:
        row = self._store.latest_for_channel(channel.key)
        return ChannelWorkflowRecord.from_json(row) if row is not None else None

    def latest_terminal_for_channel(self, channel: ChannelRef) -> ChannelWorkflowRecord | None:
        row = self._store.latest_terminal_for_channel(channel.key)
        return ChannelWorkflowRecord.from_json(row) if row is not None else None

    def active_for_channel(self, channel: ChannelRef) -> ChannelWorkflowRecord | None:
        row = self._store.active_for_channel(channel.key)
        return ChannelWorkflowRecord.from_json(row) if row is not None else None

    def queued_for_channel(self, channel: ChannelRef) -> list[ChannelWorkflowRecord]:
        return [
            ChannelWorkflowRecord.from_json(row)
            for row in self._store.queued_for_channel(channel.key)
        ]

    def update(self, workflow_id: str, **changes: Any) -> ChannelWorkflowRecord:
        record = self.get(workflow_id)
        if record is None:
            msg = f"Unknown workflow_id: {workflow_id}"
            raise KeyError(msg)
        updated = replace(record, **changes)
        self._store.upsert_workflow(updated.to_json())
        return updated

    def set_interview_session(self, workflow_id: str, session_id: str) -> ChannelWorkflowRecord:
        return self.update(workflow_id, interview_session_id=session_id)

    def set_seed(
        self, workflow_id: str, *, seed_id: str | None, seed_content: str
    ) -> ChannelWorkflowRecord:
        return self.update(
            workflow_id,
            seed_id=seed_id,
            seed_content=seed_content,
            stage=WorkflowStage.SEED_GENERATION,
        )

    def set_executing(
        self,
        workflow_id: str,
        *,
        job_id: str | None,
        session_id: str | None,
        execution_id: str | None,
    ) -> ChannelWorkflowRecord:
        record = self.get(workflow_id)
        if record is None:
            msg = f"Unknown workflow_id: {workflow_id}"
            raise KeyError(msg)
        started_at = record.started_at or datetime.now(UTC)
        return self.update(
            workflow_id,
            stage=WorkflowStage.EXECUTING,
            started_at=started_at,
            job_id=job_id,
            last_job_cursor=0,
            session_id=session_id,
            execution_id=execution_id,
        )

    def set_job_cursor(self, workflow_id: str, cursor: int) -> ChannelWorkflowRecord:
        return self.update(workflow_id, last_job_cursor=cursor)

    def mark_completed(
        self,
        workflow_id: str,
        *,
        pr_url: str | None = None,
        final_result: str | None = None,
    ) -> ChannelWorkflowRecord:
        record = self.update(
            workflow_id,
            stage=WorkflowStage.COMPLETED,
            completed_at=datetime.now(UTC),
            pr_url=pr_url,
            final_result=final_result,
        )
        self._advance_channel(record.channel_key, workflow_id)
        return record

    def mark_failed(self, workflow_id: str, *, error: str) -> ChannelWorkflowRecord:
        record = self.update(
            workflow_id,
            stage=WorkflowStage.FAILED,
            completed_at=datetime.now(UTC),
            error=error,
        )
        self._advance_channel(record.channel_key, workflow_id)
        return record

    def _advance_channel(self, channel_key: str, completed_workflow_id: str) -> None:
        queue = self._store.queued_for_channel(channel_key)
        if not queue:
            return
        next_record = ChannelWorkflowRecord.from_json(queue[0])
        next_stage = (
            WorkflowStage.INTERVIEWING
            if next_record.entry_point == WorkflowEntryPoint.INTERVIEW
            else WorkflowStage.EXECUTING
        )
        self._store.upsert_workflow(
            replace(
                next_record,
                stage=next_stage,
                started_at=datetime.now(UTC),
            ).to_json()
        )

    def find_inflight_duplicate(
        self,
        channel: ChannelRef,
        *,
        user_id: str | None,
        message: str,
        repo: str,
        entry_point: WorkflowEntryPoint,
        message_id: str | None = None,
        event_id: str | None = None,
    ) -> ChannelWorkflowRecord | None:
        fingerprint = request_fingerprint(
            user_id=user_id,
            message=message,
            repo=repo,
            entry_point=entry_point,
            message_id=message_id,
            event_id=event_id,
        )
        row = self._store.inflight_duplicate(channel.key, fingerprint)
        return ChannelWorkflowRecord.from_json(row) if row is not None else None

    def find_by_job_id(self, job_id: str) -> ChannelWorkflowRecord | None:
        row = self._store.find_by_job_id(job_id)
        return ChannelWorkflowRecord.from_json(row) if row is not None else None

    def is_event_processed(self, event_key: str) -> bool:
        return self._store.is_event_processed(event_key)

    def mark_event_processed(self, event_key: str, workflow_id: str) -> None:
        self._store.mark_event_processed(event_key, workflow_id)


def render_stage_message(record: ChannelWorkflowRecord) -> str:
    """Render a concise channel-facing update for the workflow stage."""
    if record.stage == WorkflowStage.QUEUED:
        return (
            "⏳ Request queued\n"
            f"- Workflow: {record.workflow_id}\n"
            f"- Channel: {record.channel_id}\n"
            "- Status: waiting for the active workflow in this channel to finish"
        )
    if record.stage == WorkflowStage.INTERVIEWING:
        return (
            "💬 Interview in progress\n"
            f"- Workflow: {record.workflow_id}\n"
            f"- Repo: {record.repo}\n"
            "- Next step: answer the interview question in this channel"
        )
    if record.stage == WorkflowStage.SEED_GENERATION:
        return (
            f"🧬 Seed generated\n- Workflow: {record.workflow_id}\n- Next step: preparing execution"
        )
    if record.stage == WorkflowStage.EXECUTING:
        return (
            "🚀 Execution in progress\n"
            f"- Workflow: {record.workflow_id}\n"
            f"- Repo: {record.repo}\n"
            "- Status: running autonomous execution"
        )
    if record.stage == WorkflowStage.COMPLETED:
        return render_result_message(record)
    if record.stage == WorkflowStage.FAILED:
        error = record.error or "Unknown failure"
        return f"❌ Workflow failed\n- Workflow: {record.workflow_id}\n- Error: {error}"
    return f"Workflow {record.workflow_id} stage: {record.stage}"


def render_result_message(record: ChannelWorkflowRecord) -> str:
    """Render the terminal workflow result for channel reporting."""
    if record.stage == WorkflowStage.COMPLETED:
        if record.pr_url:
            return (
                "✅ Workflow completed\n"
                f"- Workflow: {record.workflow_id}\n"
                f"- Draft PR: {record.pr_url}"
            )
        if record.final_result:
            return (
                f"✅ Workflow completed\n- Workflow: {record.workflow_id}\n\n{record.final_result}"
            )
        return f"✅ Workflow completed\n- Workflow: {record.workflow_id}"
    error = record.error or "Unknown failure"
    return f"❌ Workflow failed\n- Workflow: {record.workflow_id}\n- Error: {error}"


def render_channel_summary(
    channel: ChannelRef,
    manager: ChannelWorkflowManager,
    repo_registry: ChannelRepoRegistry,
) -> str:
    """Summarize active and queued workflows for a channel."""
    lines = [
        f"## OpenClaw Channel Workflow Summary: {channel.key}",
        "",
        f"Default repo: {repo_registry.get(channel) or '(not configured)'}",
    ]
    active = manager.active_for_channel(channel)
    if active is None:
        lines.append("Active workflow: none")
    else:
        lines.append(f"Active workflow: {active.workflow_id} ({active.stage})")
    queued = manager.queued_for_channel(channel)
    if not queued:
        lines.append("Queued workflows: 0")
    else:
        lines.append(f"Queued workflows: {len(queued)}")
        for idx, record in enumerate(queued, 1):
            lines.append(f"  {idx}. {record.workflow_id} — {record.request_message[:80]}")
    latest = manager.latest_terminal_for_channel(channel)
    if latest is not None:
        lines.extend(
            [
                "",
                f"Latest terminal workflow: {latest.workflow_id} ({latest.stage})",
                render_result_message(latest),
            ]
        )
    return "\n".join(lines)


def extract_first_url(text: str | None) -> str | None:
    """Extract the first URL from a string."""
    if not text:
        return None
    match = re.search(r"https?://\S+", text)
    if not match:
        return None
    return match.group(0).rstrip(").,")
