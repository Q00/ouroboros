"""Channel-oriented workflow state for OpenClaw integrations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import structlog

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


def _default_state_path() -> Path:
    return Path.home() / ".ouroboros" / "openclaw" / "channel_workflows.json"


def _default_repo_config_path() -> Path:
    return Path.home() / ".ouroboros" / "openclaw" / "channel_repos.json"


_SEED_HINTS = (
    "acceptance_criteria:",
    "task_type:",
    "brownfield_context:",
    "constraints:",
    "metadata:",
    "goal:",
)


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

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_repo_config_path()
        self._mapping: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._mapping = {}
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("openclaw.repo_registry.load_failed", path=str(self._path))
            self._mapping = {}
            return
        self._mapping = {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._mapping, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, channel: ChannelRef) -> str | None:
        return self._mapping.get(channel.key)

    def set(self, channel: ChannelRef, repo: str) -> None:
        self._mapping[channel.key] = repo
        self._save()

    def remove(self, channel: ChannelRef) -> None:
        self._mapping.pop(channel.key, None)
        self._save()


class ChannelWorkflowManager:
    """Manage per-channel active workflows and queued requests."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or _default_state_path()
        self._records: dict[str, ChannelWorkflowRecord] = {}
        self._active_by_channel: dict[str, str] = {}
        self._queues: dict[str, list[str]] = defaultdict(list)
        self._load()

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("openclaw.workflow_state.load_failed", path=str(self._state_path))
            return
        self._records = {
            workflow_id: ChannelWorkflowRecord.from_json(record)
            for workflow_id, record in payload.get("records", {}).items()
            if isinstance(record, dict)
        }
        self._active_by_channel = {
            str(k): str(v)
            for k, v in payload.get("active_by_channel", {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        self._queues = defaultdict(
            list,
            {
                str(k): [str(item) for item in values if isinstance(item, str)]
                for k, values in payload.get("queues", {}).items()
                if isinstance(k, str) and isinstance(values, list)
            },
        )

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "records": {
                workflow_id: record.to_json() for workflow_id, record in self._records.items()
            },
            "active_by_channel": self._active_by_channel,
            "queues": dict(self._queues),
        }
        self._state_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def enqueue(self, request: ChannelWorkflowRequest) -> ChannelWorkflowRecord:
        now = datetime.now(UTC)
        channel_key = request.channel.key
        is_active = channel_key in self._active_by_channel
        record = ChannelWorkflowRecord(
            workflow_id=f"wf_{uuid4().hex[:12]}",
            channel_key=channel_key,
            channel_id=request.channel.channel_id,
            guild_id=request.channel.guild_id,
            user_id=request.user_id,
            repo=request.repo,
            request_message=request.message,
            entry_point=request.entry_point,
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
        self._records[record.workflow_id] = record
        if is_active:
            self._queues[channel_key].append(record.workflow_id)
        else:
            self._active_by_channel[channel_key] = record.workflow_id
        self._save()
        return record

    def get(self, workflow_id: str) -> ChannelWorkflowRecord | None:
        return self._records.get(workflow_id)

    def latest_for_channel(self, channel: ChannelRef) -> ChannelWorkflowRecord | None:
        records = [record for record in self._records.values() if record.channel_key == channel.key]
        if not records:
            return None
        return max(records, key=lambda record: record.queued_at)

    def active_for_channel(self, channel: ChannelRef) -> ChannelWorkflowRecord | None:
        workflow_id = self._active_by_channel.get(channel.key)
        if workflow_id is None:
            return None
        return self._records.get(workflow_id)

    def queued_for_channel(self, channel: ChannelRef) -> list[ChannelWorkflowRecord]:
        return [
            self._records[workflow_id]
            for workflow_id in self._queues.get(channel.key, [])
            if workflow_id in self._records
        ]

    def update(self, workflow_id: str, **changes: Any) -> ChannelWorkflowRecord:
        record = self._records[workflow_id]
        updated = replace(record, **changes)
        self._records[workflow_id] = updated
        self._save()
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
        record = self._records[workflow_id]
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
        active_workflow_id = self._active_by_channel.get(channel_key)
        if active_workflow_id == completed_workflow_id:
            self._active_by_channel.pop(channel_key, None)
        queue = self._queues.get(channel_key, [])
        if not queue:
            self._save()
            return
        next_workflow_id = queue.pop(0)
        if queue:
            self._queues[channel_key] = queue
        else:
            self._queues.pop(channel_key, None)
        next_record = self._records[next_workflow_id]
        next_stage = (
            WorkflowStage.INTERVIEWING
            if next_record.entry_point == WorkflowEntryPoint.INTERVIEW
            else WorkflowStage.EXECUTING
        )
        self._records[next_workflow_id] = replace(
            next_record,
            stage=next_stage,
            started_at=datetime.now(UTC),
        )
        self._active_by_channel[channel_key] = next_workflow_id
        self._save()

    def find_by_job_id(self, job_id: str) -> ChannelWorkflowRecord | None:
        return next((record for record in self._records.values() if record.job_id == job_id), None)


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
            "🧬 Seed generated\n"
            f"- Workflow: {record.workflow_id}\n"
            "- Next step: preparing execution"
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
        return (
            "❌ Workflow failed\n"
            f"- Workflow: {record.workflow_id}\n"
            f"- Error: {error}"
        )
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
                "✅ Workflow completed\n"
                f"- Workflow: {record.workflow_id}\n\n"
                f"{record.final_result}"
            )
        return f"✅ Workflow completed\n- Workflow: {record.workflow_id}"
    error = record.error or "Unknown failure"
    return (
        "❌ Workflow failed\n"
        f"- Workflow: {record.workflow_id}\n"
        f"- Error: {error}"
    )


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
    latest = manager.latest_for_channel(channel)
    if latest is not None and latest.is_terminal:
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
