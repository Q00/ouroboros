"""Recovery Hooks - Auto-resume after interruptions.

This module provides recovery mechanisms for:
- Auto-resume after interruptions (e.g., /clear, crashes)
- Session restoration
- Graceful degradation on failures

Hooks are registered with the system and triggered on specific events.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from ouroboros.plugin.state.manager import SessionStatus, StateManager

log = structlog.get_logger()


class RecoveryTrigger(Enum):
    """Events that trigger recovery."""

    STARTUP = "startup"  # On system startup
    CLEAR = "clear"  # After /clear command
    CRASH = "crash"  # After detected crash
    INTERRUPTION = "interruption"  # After user interruption
    MODE_SWITCH = "mode_switch"  # When switching execution modes


@dataclass
class RecoveryResult:
    """Result of a recovery operation.

    Attributes:
        success: Whether recovery succeeded.
        session_restored: Whether a session was restored.
        checkpoint_restored: Whether a checkpoint was restored.
        message: Human-readable result message.
        metadata: Additional recovery metadata.
    """

    success: bool
    session_restored: bool
    checkpoint_restored: bool
    message: str
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "success": self.success,
            "session_restored": self.session_restored,
            "checkpoint_restored": self.checkpoint_restored,
            "message": self.message,
            "metadata": self.metadata or {},
        }


class RecoveryHook(ABC):
    """Base class for recovery hooks.

    Recovery hooks are called when specific triggers occur.
    They can implement custom recovery logic.

    Example:
        class CustomRecoveryHook(RecoveryHook):
            async def on_trigger(self, trigger, manager):
                # Custom recovery logic
                return RecoveryResult(
                    success=True,
                    session_restored=True,
                    checkpoint_restored=False,
                    message="Custom recovery completed",
                )
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Get hook name."""
        ...

    @property
    def priority(self) -> int:
        """Hook priority (higher = executed first)."""
        return 0

    @abstractmethod
    async def on_trigger(
        self,
        trigger: RecoveryTrigger,
        manager: StateManager,
    ) -> RecoveryResult:
        """Handle recovery trigger.

        Args:
            trigger: The recovery trigger event.
            manager: StateManager for recovery operations.

        Returns:
            RecoveryResult with outcome.
        """
        ...

    async def should_trigger(self, trigger: RecoveryTrigger) -> bool:
        """Check if hook should handle this trigger.

        Args:
            trigger: The recovery trigger event.

        Returns:
            True if hook should handle this trigger.
        """
        _ = trigger  # Mark as intentionally unused for base implementation
        return True
        return True


class DefaultSessionRecoveryHook(RecoveryHook):
    """Default recovery hook for session restoration.

    Restores the last active session for each mode on startup.
    """

    @property
    def name(self) -> str:
        return "default_session_recovery"

    @property
    def priority(self) -> int:
        return 100  # High priority, runs first

    async def on_trigger(
        self,
        trigger: RecoveryTrigger,
        manager: StateManager,
    ) -> RecoveryResult:
        """Handle recovery trigger.

        Args:
            trigger: The recovery trigger event.
            manager: StateManager for recovery operations.

        Returns:
            RecoveryResult with outcome.
        """
        if trigger not in (RecoveryTrigger.STARTUP, RecoveryTrigger.CLEAR):
            return RecoveryResult(
                success=True,
                session_restored=False,
                checkpoint_restored=False,
                message=f"Hook skipped for trigger: {trigger.value}",
            )

        restored_count = 0

        # Try to restore active sessions
        from ouroboros.plugin.state.store import StateMode

        for mode in StateMode:
            session = await manager.get_active_session(mode)
            if session and session.status == SessionStatus.ACTIVE:
                log.info(
                    "recovery.session.found",
                    session_id=session.session_id,
                    mode=mode.value,
                )
                restored_count += 1

        if restored_count > 0:
            return RecoveryResult(
                success=True,
                session_restored=True,
                checkpoint_restored=False,
                message=f"Restored {restored_count} active session(s)",
                metadata={"restored_count": restored_count},
            )

        return RecoveryResult(
            success=True,
            session_restored=False,
            checkpoint_restored=False,
            message="No active sessions to restore",
        )


class CheckpointRecoveryHook(RecoveryHook):
    """Recovery hook for checkpoint restoration.

    Attempts to restore from the most recent checkpoint
    when session recovery is not possible.
    """

    @property
    def name(self) -> str:
        return "checkpoint_recovery"

    @property
    def priority(self) -> int:
        return 50  # Lower priority, runs after session recovery

    async def on_trigger(
        self,
        trigger: RecoveryTrigger,
        manager: StateManager,
    ) -> RecoveryResult:
        """Handle recovery trigger.

        Args:
            trigger: The recovery trigger event.
            manager: StateManager for recovery operations.

        Returns:
            RecoveryResult with outcome.
        """
        if trigger not in (RecoveryTrigger.STARTUP, RecoveryTrigger.CRASH):
            return RecoveryResult(
                success=True,
                session_restored=False,
                checkpoint_restored=False,
                message=f"Hook skipped for trigger: {trigger.value}",
            )

        # List available checkpoints
        checkpoints = await manager.list_checkpoints()

        if not checkpoints:
            return RecoveryResult(
                success=True,
                session_restored=False,
                checkpoint_restored=False,
                message="No checkpoints available",
            )

        # Try to restore the most recent checkpoint
        latest_checkpoint = checkpoints[0]
        checkpoint_id = latest_checkpoint.get("checkpoint_id")

        if checkpoint_id:
            state = await manager.restore_checkpoint(checkpoint_id)

            if state:
                log.info(
                    "recovery.checkpoint.restored",
                    checkpoint_id=checkpoint_id,
                )
                return RecoveryResult(
                    success=True,
                    session_restored=False,
                    checkpoint_restored=True,
                    message=f"Restored from checkpoint: {checkpoint_id}",
                    metadata={"checkpoint_id": checkpoint_id},
                )

        return RecoveryResult(
            success=True,
            session_restored=False,
            checkpoint_restored=False,
            message="Failed to restore checkpoint",
        )


class GracefulDegradationHook(RecoveryHook):
    """Hook for graceful degradation when recovery fails.

    Ensures the system can continue operating even when
    full recovery is not possible.
    """

    @property
    def name(self) -> str:
        return "graceful_degradation"

    @property
    def priority(self) -> int:
        return 10  # Lowest priority, runs last

    async def on_trigger(
        self,
        trigger: RecoveryTrigger,
        manager: StateManager,
    ) -> RecoveryResult:
        """Handle recovery trigger.

        Args:
            trigger: The recovery trigger event.
            manager: StateManager for recovery operations.

        Returns:
            RecoveryResult with outcome.
        """
        # Always succeeds - provides graceful degradation
        _ = manager  # Mark as intentionally unused
        log.info(
            "recovery.graceful_degradation",
            trigger=trigger.value,
        )

        return RecoveryResult(
            success=True,
            session_restored=False,
            checkpoint_restored=False,
            message="System ready for new session",
            metadata={"degraded": True},
        )


class RecoveryManager:
    """Manages recovery hooks and coordinates recovery operations.

    Example:
        manager = StateManager(store)
        recovery = RecoveryManager(manager)

        # Register hooks
        recovery.register_hook(DefaultSessionRecoveryHook())
        recovery.register_hook(CheckpointRecoveryHook())
        recovery.register_hook(GracefulDegradationHook())

        # Trigger recovery
        result = await self.recover(RecoveryTrigger.STARTUP)
    """

    def __init__(self, state_manager: StateManager) -> None:
        """Initialize recovery manager.

        Args:
            state_manager: StateManager for recovery operations.
        """
        self._state_manager = state_manager
        self._hooks: list[RecoveryHook] = []

        # Register default hooks
        self._register_default_hooks()

    def _register_default_hooks(self) -> None:
        """Register default recovery hooks."""
        self._hooks = [
            DefaultSessionRecoveryHook(),
            CheckpointRecoveryHook(),
            GracefulDegradationHook(),
        ]

    def register_hook(self, hook: RecoveryHook) -> None:
        """Register a custom recovery hook.

        Args:
            hook: RecoveryHook to register.
        """
        self._hooks.append(hook)
        # Sort by priority (descending)
        self._hooks.sort(key=lambda h: h.priority, reverse=True)

        log.info("recovery.hook.registered", hook=hook.name, priority=hook.priority)

    def unregister_hook(self, hook_name: str) -> bool:
        """Unregister a recovery hook by name.

        Args:
            hook_name: Name of hook to unregister.

        Returns:
            True if hook was found and removed.
        """
        for i, hook in enumerate(self._hooks):
            if hook.name == hook_name:
                self._hooks.pop(i)
                log.info("recovery.hook.unregistered", hook=hook_name)
                return True
        return False

    async def recover(
        self,
        trigger: RecoveryTrigger,
        context: dict[str, Any] | None = None,
    ) -> RecoveryResult:
        """Execute recovery for a trigger.

        Runs all applicable hooks in priority order until one succeeds.

        Args:
            trigger: The recovery trigger event.
            context: Optional context data for recovery.

        Returns:
            RecoveryResult with outcome.
        """
        log.info("recovery.started", trigger=trigger.value)

        context = context or {}
        results = []

        for hook in self._hooks:
            try:
                if not await hook.should_trigger(trigger):
                    continue

                result = await hook.on_trigger(trigger, self._state_manager)
                results.append(result)

                # Stop on first successful session/checkpoint restoration
                if result.session_restored or result.checkpoint_restored:
                    log.info(
                        "recovery.completed",
                        trigger=trigger.value,
                        hook=hook.name,
                        message=result.message,
                    )
                    return result

            except Exception as e:
                log.error(
                    "recovery.hook.error",
                    hook=hook.name,
                    trigger=trigger.value,
                    error=str(e),
                )
                results.append(
                    RecoveryResult(
                        success=False,
                        session_restored=False,
                        checkpoint_restored=False,
                        message=f"Hook {hook.name} failed: {e}",
                    )
                )

        # If all hooks ran but none restored session, use the last result
        # or create a default degraded result
        if results:
            last_result = results[-1]
            if last_result.success:
                return last_result

        # Fallback to graceful degradation
        return RecoveryResult(
            success=True,
            session_restored=False,
            checkpoint_restored=False,
            message="No session restored, system ready",
            metadata={"degraded": True},
        )

    async def recover_on_startup(self) -> RecoveryResult:
        """Recover state on system startup.

        Returns:
            RecoveryResult with outcome.
        """
        return await self.recover(RecoveryTrigger.STARTUP)

    async def recover_after_clear(self) -> RecoveryResult:
        """Recover state after /clear command.

        Returns:
            RecoveryResult with outcome.
        """
        return await self.recover(RecoveryTrigger.CLEAR)

    async def recover_from_crash(self) -> RecoveryResult:
        """Recover state after detected crash.

        Returns:
            RecoveryResult with outcome.
        """
        return await self.recover(RecoveryTrigger.CRASH)

    def list_hooks(self) -> list[dict[str, Any]]:
        """List registered hooks.

        Returns:
            List of hook metadata.
        """
        return [
            {
                "name": hook.name,
                "priority": hook.priority,
            }
            for hook in self._hooks
        ]
