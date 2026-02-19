"""Context Compression - Smart state compression when approaching limits.

This module provides compression for state data:
- Smart compression when approaching token limits
- Summary generation for history
- Critical state preservation

Complements the existing context.py compression by providing
state-specific compression for persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import json
from typing import Any

import structlog

from ouroboros.core.types import Result

log = structlog.get_logger()


class CompressionStrategy(Enum):
    """Compression strategy types."""

    LLM_SUMMARY = "llm_summary"  # Use LLM to summarize
    TRUNCATE = "truncate"  # Simple truncation
    AGGRESSIVE = "aggressive"  # Keep only critical data
    NONE = "none"  # No compression


@dataclass
class CompressionConfig:
    """Configuration for state compression.

    Attributes:
        max_tokens: Maximum tokens before compression triggers.
        max_age_hours: Maximum age in hours before compression.
        preserve_recent_count: Number of recent items to preserve.
        strategy: Compression strategy to use.
    """

    max_tokens: int = 50000
    max_age_hours: float = 6.0
    preserve_recent_count: int = 5
    strategy: CompressionStrategy = CompressionStrategy.LLM_SUMMARY


@dataclass
class CompressionMetrics:
    """Metrics from a compression operation.

    Attributes:
        before_size: Size before compression (bytes).
        after_size: Size after compression (bytes).
        compression_ratio: Ratio of after/before size.
        strategy_used: Strategy that was applied.
        timestamp: When compression occurred.
    """

    before_size: int
    after_size: int
    compression_ratio: float
    strategy_used: CompressionStrategy
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class StateCompression:
    """State data compression utility.

    Compresses state dictionaries for efficient storage and recovery.

    Example:
        compressor = StateCompression(config=CompressionConfig())

        # Compress state
        result = await compressor.compress_state_dict(state_data)

        if result.is_ok:
            compressed, metrics = result.value
            print(f"Compressed by {metrics.compression_ratio:.1%}")
    """

    def __init__(self, config: CompressionConfig | None = None) -> None:
        """Initialize state compression.

        Args:
            config: Compression configuration. Defaults to CompressionConfig().
        """
        self._config = config or CompressionConfig()

    def _estimate_size(self, data: dict[str, Any]) -> int:
        """Estimate size of dictionary in bytes.

        Args:
            data: Dictionary to measure.

        Returns:
            Estimated size in bytes.
        """
        return len(json.dumps(data, ensure_ascii=False))

    def _needs_compression(
        self,
        data: dict[str, Any],
        created_at: datetime | None = None,
    ) -> bool:
        """Check if data needs compression.

        Args:
            data: State data to check.
            created_at: When the data was created.

        Returns:
            True if compression should be applied.
        """
        size = self._estimate_size(data)

        # Check size threshold
        if size > self._config.max_tokens * 4:  # Rough estimate: 4 chars per token
            return True

        # Check age threshold
        if created_at:
            age = datetime.now(UTC) - created_at
            age_hours = age.total_seconds() / 3600
            if age_hours > self._config.max_age_hours:
                return True

        return False

    async def compress_state_dict(
        self,
        data: dict[str, Any],
    ) -> Result[tuple[dict[str, Any], CompressionMetrics], str]:
        """Compress a state dictionary.

        Args:
            data: State data to compress.

        Returns:
            Result containing (compressed_data, metrics) or error message.
        """
        before_size = self._estimate_size(data)

        # Check if compression is needed
        created_at_str = data.get("created_at")
        created_at = datetime.fromisoformat(created_at_str) if created_at_str else None

        if not self._needs_compression(data, created_at):
            log.debug("state.compression.skipped", reason="no_compression_needed")
            return Result.ok(
                (
                    data,
                    CompressionMetrics(
                        before_size=before_size,
                        after_size=before_size,
                        compression_ratio=1.0,
                        strategy_used=CompressionStrategy.NONE,
                    ),
                )
            )

        # Apply compression strategy
        compressed, strategy = await self._apply_strategy(data)

        after_size = self._estimate_size(compressed)
        compression_ratio = after_size / before_size if before_size > 0 else 1.0

        metrics = CompressionMetrics(
            before_size=before_size,
            after_size=after_size,
            compression_ratio=compression_ratio,
            strategy_used=strategy,
        )

        log.info(
            "state.compression.completed",
            strategy=strategy.value,
            before_size=before_size,
            after_size=after_size,
            compression_ratio=compression_ratio,
            reduction_percent=int((1 - compression_ratio) * 100),
        )

        return Result.ok((compressed, metrics))

    async def _apply_strategy(
        self,
        data: dict[str, Any],
    ) -> tuple[dict[str, Any], CompressionStrategy]:
        """Apply compression strategy to data.

        Args:
            data: State data to compress.

        Returns:
            Tuple of (compressed_data, strategy_used).
        """
        strategy = self._config.strategy

        # Try LLM summary first if configured
        if strategy == CompressionStrategy.LLM_SUMMARY:
            try:
                compressed = await self._llm_summary_compress(data)
                return compressed, CompressionStrategy.LLM_SUMMARY
            except Exception as e:
                log.warning("state.compression.llm_failed", error=str(e))
                # Fall back to truncate
                strategy = CompressionStrategy.TRUNCATE

        # Fall back to truncate or aggressive
        if strategy == CompressionStrategy.TRUNCATE:
            compressed = self._truncate_compress(data)
            return compressed, CompressionStrategy.TRUNCATE
        else:  # AGGRESSIVE
            compressed = self._aggressive_compress(data)
            return compressed, CompressionStrategy.AGGRESSIVE

    async def _llm_summary_compress(self, data: dict[str, Any]) -> dict[str, Any]:
        """Compress using LLM summarization.

        For state persistence, we create summaries of historical data
        while preserving critical information.

        Args:
            data: State data to compress.

        Returns:
            Compressed state dictionary.
        """
        # Preserve critical fields
        preserved = {
            "session_id": data.get("session_id"),
            "execution_id": data.get("execution_id"),
            "seed_id": data.get("seed_id"),
            "seed_goal": data.get("seed_goal"),
            "mode": data.get("mode"),
            "status": data.get("status"),
            "version": data.get("version"),
        }

        # Compress workflow state if present
        workflow_state = data.get("workflow_state", {})
        if workflow_state:
            preserved["workflow_state"] = self._compress_workflow_state(workflow_state)

        # Compress acceptance criteria
        acceptance_criteria = data.get("acceptance_criteria", [])
        if acceptance_criteria:
            preserved["acceptance_criteria"] = self._compress_acceptance_criteria(
                acceptance_criteria,
                workflow_state,
            )

        # Add compression metadata
        preserved["compression"] = {
            "method": "llm_summary",
            "timestamp": datetime.now(UTC).isoformat(),
            "original_size": self._estimate_size(data),
        }

        return preserved

    def _truncate_compress(self, data: dict[str, Any]) -> dict[str, Any]:
        """Compress by truncating history and preserving recent items.

        Args:
            data: State data to compress.

        Returns:
            Compressed state dictionary.
        """
        preserved = {
            "session_id": data.get("session_id"),
            "execution_id": data.get("execution_id"),
            "seed_id": data.get("seed_id"),
            "seed_goal": data.get("seed_goal"),
            "mode": data.get("mode"),
            "status": data.get("status"),
            "version": data.get("version"),
        }

        # Preserve full workflow state but truncate history
        workflow_state = data.get("workflow_state", {})
        if workflow_state:
            # Keep acceptance criteria, truncate history
            preserved_wfs: dict[str, Any] = {
                k: v
                for k, v in workflow_state.items()
                if k != "activity_log" and k != "recent_outputs"
            }
            preserved["workflow_state"] = preserved_wfs
            # Keep recent items
            if "activity_log" in workflow_state:
                activity_log = workflow_state["activity_log"]
                if isinstance(activity_log, list):
                    preserved_wfs["activity_log"] = activity_log[
                        -self._config.preserve_recent_count :
                    ]
                else:
                    preserved_wfs["activity_log"] = activity_log

        # Preserve acceptance criteria (critical for recovery)
        preserved["acceptance_criteria"] = data.get("acceptance_criteria", [])

        # Add compression metadata
        preserved["compression"] = {
            "method": "truncate",
            "timestamp": datetime.now(UTC).isoformat(),
            "original_size": self._estimate_size(data),
        }

        return preserved

    def _aggressive_compress(self, data: dict[str, Any]) -> dict[str, Any]:
        """Aggressive compression - keep only minimal critical data.

        Args:
            data: State data to compress.

        Returns:
            Minimally compressed state dictionary.
        """
        # Keep only essential fields for recovery
        return {
            "session_id": data.get("session_id"),
            "execution_id": data.get("execution_id"),
            "seed_id": data.get("seed_id"),
            "seed_goal": data.get("seed_goal"),
            "mode": data.get("mode"),
            "status": data.get("status"),
            "acceptance_criteria": data.get("acceptance_criteria", []),
            "compression": {
                "method": "aggressive",
                "timestamp": datetime.now(UTC).isoformat(),
                "original_size": self._estimate_size(data),
                "note": "Minimal preservation - full recovery may not be possible",
            },
        }

    def _compress_workflow_state(self, workflow_state: dict[str, Any]) -> dict[str, Any]:
        """Compress workflow state while preserving critical info.

        Args:
            workflow_state: Workflow state dictionary.

        Returns:
            Compressed workflow state.
        """
        # Preserve progress tracking
        return {
            "session_id": workflow_state.get("session_id"),
            "goal": workflow_state.get("goal"),
            "completed_acs": workflow_state.get("completed_acs", 0),
            "total_acs": workflow_state.get("total_acs", 0),
            "progress_percent": workflow_state.get("progress_percent", 0),
            "current_ac_index": workflow_state.get("current_ac_index", 0),
            "current_phase": workflow_state.get("current_phase", "Discover"),
            "activity": workflow_state.get("activity", "idle"),
        }

    def _compress_acceptance_criteria(
        self,
        acceptance_criteria: list[str],
        workflow_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Compress acceptance criteria with status tracking.

        Args:
            acceptance_criteria: List of AC strings.
            workflow_state: Optional workflow state for status info.

        Returns:
            Compressed AC list with status.
        """
        compressed = []

        for i, ac in enumerate(acceptance_criteria, 1):
            ac_data = {"index": i, "content": ac}

            # Add status if workflow state available
            if workflow_state:
                ac_list = workflow_state.get("acceptance_criteria", [])
                if ac_list and i - 1 < len(ac_list):
                    ac_data["status"] = ac_list[i - 1].get("status", "pending")

            compressed.append(ac_data)

        return compressed


async def compress_state_dict(
    data: dict[str, Any],
    config: CompressionConfig | None = None,
) -> Result[tuple[dict[str, Any], CompressionMetrics], str]:
    """Convenience function to compress state dictionary.

    Args:
        data: State data to compress.
        config: Optional compression configuration.

    Returns:
        Result containing (compressed_data, metrics) or error message.

    Example:
        result = await compress_state_dict(state_data)
        if result.is_ok:
            compressed, metrics = result.value
    """
    compressor = StateCompression(config)
    return await compressor.compress_state_dict(data)
