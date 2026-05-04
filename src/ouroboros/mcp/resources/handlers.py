"""Ouroboros resource handlers for MCP server.

This module defines resource handlers for exposing Ouroboros data:
- seeds: Access to seed definitions
- sessions: Access to session data
- events: Access to event history
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.seed_generator import load_seed
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPServerError
from ouroboros.mcp.types import MCPResourceContent, MCPResourceDefinition
from ouroboros.orchestrator.session import SessionRepository, SessionTracker
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)


@dataclass
class SeedsResourceHandler:
    """Handler for seed resources.

    Provides access to seed definitions and content.
    URI patterns:
    - ouroboros://seeds - List all seeds
    - ouroboros://seeds/{seed_id} - Get specific seed
    """

    seed_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.seed_dir is None:
            self.seed_dir = Path.home() / ".ouroboros" / "seeds"

    @property
    def definitions(self) -> Sequence[MCPResourceDefinition]:
        """Return the resource definitions."""
        return (
            MCPResourceDefinition(
                uri="ouroboros://seeds",
                name="Seeds List",
                description="List of all available seeds in the system",
                mime_type="application/json",
            ),
        )

    async def handle(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Handle a seed resource request.

        Args:
            uri: The resource URI.

        Returns:
            Result containing resource content or error.
        """
        log.info("mcp.resource.seeds", uri=uri)

        try:
            if uri == "ouroboros://seeds":
                seeds = await self._list_seeds()
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps(
                            {"count": len(seeds), "seeds": seeds},
                            sort_keys=True,
                        ),
                        mime_type="application/json",
                    )
                )

            # Handle specific seed ID
            if uri.startswith("ouroboros://seeds/"):
                seed_id = uri.replace("ouroboros://seeds/", "")
                seed = await self._load_seed_by_id(seed_id)
                if seed is None:
                    return Result.err(
                        MCPResourceNotFoundError(
                            f"Seed not found: {seed_id}",
                            resource_type="seed",
                            resource_id=seed_id,
                        )
                    )
                payload = {"id": seed.metadata.seed_id, "seed": seed.to_dict()}
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps(payload, sort_keys=True),
                        mime_type="application/json",
                    )
                )

            return Result.err(
                MCPResourceNotFoundError(
                    f"Unknown seed resource: {uri}",
                    resource_type="seed",
                    resource_id=uri,
                )
            )
        except Exception as e:
            log.error("mcp.resource.seeds.error", uri=uri, error=str(e))
            return Result.err(MCPServerError(f"Failed to read seed resource: {e}"))

    def _seed_paths(self) -> list[Path]:
        seed_dir = self.seed_dir
        if seed_dir is None or not seed_dir.exists():
            return []
        paths = [
            *seed_dir.glob("*.yaml"),
            *seed_dir.glob("*.yml"),
            *seed_dir.glob("*.json"),
        ]
        return sorted({path.resolve() for path in paths}, key=lambda path: path.name)

    async def _list_seeds(self) -> list[dict[str, Any]]:
        seeds: list[dict[str, Any]] = []
        for path in self._seed_paths():
            result = await load_seed(path)
            if result.is_err:
                log.warning("mcp.resource.seeds.skip_invalid", path=str(path))
                continue
            seed = result.value
            seeds.append(
                {
                    "id": seed.metadata.seed_id,
                    "goal": seed.goal,
                    "task_type": seed.task_type,
                    "created_at": seed.metadata.created_at.isoformat(),
                    "ambiguity_score": seed.metadata.ambiguity_score,
                }
            )
        return seeds

    async def _load_seed_by_id(self, seed_id: str):
        for path in self._seed_paths():
            name_matches = path.stem == seed_id or path.name == seed_id
            result = await load_seed(path)
            if result.is_err:
                log.warning("mcp.resource.seeds.skip_invalid", path=str(path))
                continue

            seed = result.value
            if seed.metadata.seed_id == seed_id:
                return seed

            if name_matches:
                log.warning(
                    "mcp.resource.seeds.metadata_mismatch",
                    path=str(path),
                    requested_seed_id=seed_id,
                    actual_seed_id=seed.metadata.seed_id,
                )
        return None


@dataclass
class SessionsResourceHandler:
    """Handler for session resources.

    Provides access to session data and status.
    URI patterns:
    - ouroboros://sessions - List all sessions
    - ouroboros://sessions/current - Get current active session
    - ouroboros://sessions/{session_id} - Get specific session
    """

    event_store: EventStore | None = None

    @property
    def definitions(self) -> Sequence[MCPResourceDefinition]:
        """Return the resource definitions."""
        return (
            MCPResourceDefinition(
                uri="ouroboros://sessions",
                name="Sessions List",
                description="List of all sessions",
                mime_type="application/json",
            ),
            MCPResourceDefinition(
                uri="ouroboros://sessions/current",
                name="Current Session",
                description="The currently active session",
                mime_type="application/json",
            ),
        )

    async def handle(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Handle a session resource request.

        Args:
            uri: The resource URI.

        Returns:
            Result containing resource content or error.
        """
        log.info("mcp.resource.sessions", uri=uri)

        try:
            if uri == "ouroboros://sessions":
                sessions = await self._list_sessions()
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps(
                            {"count": len(sessions), "sessions": sessions},
                            sort_keys=True,
                        ),
                        mime_type="application/json",
                    )
                )

            if uri == "ouroboros://sessions/current":
                session = await self._current_session()
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps({"session": session}, sort_keys=True),
                        mime_type="application/json",
                    )
                )

            # Handle specific session ID
            if uri.startswith("ouroboros://sessions/"):
                session_id = uri.replace("ouroboros://sessions/", "")
                session = await self._load_session(session_id)
                if session is None:
                    return Result.err(
                        MCPResourceNotFoundError(
                            f"Session not found: {session_id}",
                            resource_type="session",
                            resource_id=session_id,
                        )
                    )
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps({"session": session}, sort_keys=True),
                        mime_type="application/json",
                    )
                )

            return Result.err(
                MCPResourceNotFoundError(
                    f"Unknown session resource: {uri}",
                    resource_type="session",
                    resource_id=uri,
                )
            )
        except Exception as e:
            log.error("mcp.resource.sessions.error", uri=uri, error=str(e))
            return Result.err(MCPServerError(f"Failed to read session resource: {e}"))

    async def _list_sessions(self) -> list[dict[str, Any]]:
        event_store = await self._ensure_event_store()
        if event_store is None:
            return []

        repo = SessionRepository(event_store)
        activity_by_session = await self._session_activity_by_id()
        start_events = await event_store.get_all_sessions()
        session_ids = list(dict.fromkeys(event.aggregate_id for event in start_events))
        sessions: list[dict[str, Any]] = []
        for session_id in session_ids:
            result = await repo.reconstruct_session(session_id)
            if result.is_ok:
                session = _session_to_dict(result.value)
                last_activity = activity_by_session.get(session_id)
                if last_activity is not None:
                    session["last_activity"] = _timestamp_to_string(last_activity)
                sessions.append(session)
        return sessions

    async def _load_session(self, session_id: str) -> dict[str, Any] | None:
        event_store = await self._ensure_event_store()
        if event_store is None:
            return None

        result = await SessionRepository(event_store).reconstruct_session(session_id)
        if result.is_err:
            return None
        return _session_to_dict(result.value)

    async def _current_session(self) -> dict[str, Any] | None:
        sessions = await self._list_sessions()
        active_sessions = [
            session for session in sessions if session.get("status") in {"running", "paused"}
        ]
        if not active_sessions:
            return None
        return max(active_sessions, key=_session_activity_key)

    async def _session_activity_by_id(self) -> dict[str, object]:
        event_store = await self._ensure_event_store()
        if event_store is None:
            return {}

        snapshots = await event_store.get_session_activity_snapshots()
        activity_by_session: dict[str, object] = {}
        for snapshot in snapshots:
            activity = snapshot.last_activity or snapshot.start_time
            if activity is not None:
                activity_by_session[snapshot.session_id] = activity
            related_events = await event_store.query_session_related_events(
                session_id=snapshot.session_id,
                execution_id=snapshot.execution_id,
                limit=1,
            )
            if related_events:
                related_activity = related_events[0].timestamp
                current_activity = activity_by_session.get(snapshot.session_id)
                activity_by_session[snapshot.session_id] = _latest_timestamp(
                    current_activity,
                    related_activity,
                )
        return activity_by_session

    async def _ensure_event_store(self) -> EventStore | None:
        if self.event_store is None:
            self.event_store = EventStore()
        if getattr(self.event_store, "_engine", None) is None:
            await self.event_store.initialize()
        return self.event_store


@dataclass
class EventsResourceHandler:
    """Handler for event resources.

    Provides access to event history.
    URI patterns:
    - ouroboros://events - List recent events
    - ouroboros://events/{session_id} - Events for a specific session
    """

    event_store: EventStore | None = None

    @property
    def definitions(self) -> Sequence[MCPResourceDefinition]:
        """Return the resource definitions."""
        return (
            MCPResourceDefinition(
                uri="ouroboros://events",
                name="Events",
                description="Recent event history",
                mime_type="application/json",
            ),
        )

    async def handle(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Handle an events resource request.

        Args:
            uri: The resource URI.

        Returns:
            Result containing resource content or error.
        """
        log.info("mcp.resource.events", uri=uri)

        try:
            if uri == "ouroboros://events":
                events = await self._recent_events()
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps(
                            {"count": len(events), "events": events},
                            sort_keys=True,
                        ),
                        mime_type="application/json",
                    )
                )

            # Handle session-specific events
            if uri.startswith("ouroboros://events/"):
                session_id = uri.replace("ouroboros://events/", "")
                events = await self._session_events(session_id)
                if events is None:
                    return Result.err(
                        MCPResourceNotFoundError(
                            f"Session events not found: {session_id}",
                            resource_type="events",
                            resource_id=session_id,
                        )
                    )
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        text=json.dumps(
                            {"session_id": session_id, "count": len(events), "events": events},
                            sort_keys=True,
                        ),
                        mime_type="application/json",
                    )
                )

            return Result.err(
                MCPResourceNotFoundError(
                    f"Unknown events resource: {uri}",
                    resource_type="events",
                    resource_id=uri,
                )
            )
        except Exception as e:
            log.error("mcp.resource.events.error", uri=uri, error=str(e))
            return Result.err(MCPServerError(f"Failed to read events resource: {e}"))

    async def _recent_events(self) -> list[dict[str, Any]]:
        event_store = await self._ensure_event_store()
        if event_store is None:
            return []
        events = await event_store.get_recent_events(limit=100)
        return [_event_to_dict(event) for event in events]

    async def _session_events(self, session_id: str) -> list[dict[str, Any]] | None:
        event_store = await self._ensure_event_store()
        if event_store is None:
            return None
        events = await event_store.query_session_related_events(
            session_id=session_id,
            limit=100,
        )
        if not events:
            return None
        return [_event_to_dict(event) for event in events]

    async def _ensure_event_store(self) -> EventStore | None:
        if self.event_store is None:
            self.event_store = EventStore()
        if getattr(self.event_store, "_engine", None) is None:
            await self.event_store.initialize()
        return self.event_store


def _session_to_dict(session: SessionTracker) -> dict[str, Any]:
    data = session.to_dict()
    data.update(session.progress)
    return data


def _event_to_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "data": event.data,
        "consensus_id": event.consensus_id,
        "event_version": event.event_version,
    }


def _timestamp_to_string(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _latest_timestamp(current: object | None, candidate: object | None) -> object:
    if current is None:
        return candidate or ""
    if candidate is None:
        return current
    if _timestamp_sort_key(candidate) > _timestamp_sort_key(current):
        return candidate
    return current


def _timestamp_sort_key(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return datetime.min.replace(tzinfo=UTC)


def _session_activity_key(session: dict[str, Any]) -> datetime:
    activity = (
        session.get("last_activity")
        or session.get("last_message_time")
        or session.get("start_time")
    )
    return _timestamp_sort_key(activity)


# Convenience functions for handler access
def seeds_handler() -> SeedsResourceHandler:
    """Create a SeedsResourceHandler instance."""
    return SeedsResourceHandler()


def sessions_handler(event_store: EventStore | None = None) -> SessionsResourceHandler:
    """Create a SessionsResourceHandler instance."""
    return SessionsResourceHandler(event_store=event_store or EventStore())


def events_handler(event_store: EventStore | None = None) -> EventsResourceHandler:
    """Create an EventsResourceHandler instance."""
    return EventsResourceHandler(event_store=event_store or EventStore())


# List of all Ouroboros resources for registration
OUROBOROS_RESOURCES: tuple[
    SeedsResourceHandler | SessionsResourceHandler | EventsResourceHandler, ...
] = (
    seeds_handler(),
    sessions_handler(),
    events_handler(),
)
