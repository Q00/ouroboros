"""Tests for the Gemini API Logger."""

import pytest
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from ouroboros.dashboard.api_logger import GeminiAPILogger, APILogEntry


class TestAPILogEntry:
    """Tests for APILogEntry class."""

    def test_log_entry_creation(self) -> None:
        """Test creating a log entry."""
        entry = APILogEntry(
            request_id="test-123",
            timestamp=datetime.now(),
            model="gemini-2.5-pro",
            request_type="full_history_analysis",
            prompt_preview="Test prompt...",
            prompt_tokens=1000,
            response_preview="Test response...",
            completion_tokens=500,
            total_tokens=1500,
            latency_ms=2500.0,
            status="success",
        )

        assert entry.request_id == "test-123"
        assert entry.model == "gemini-2.5-pro"
        assert entry.total_tokens == 1500

    def test_log_entry_with_error(self) -> None:
        """Test creating an error log entry."""
        entry = APILogEntry(
            request_id="test-456",
            timestamp=datetime.now(),
            model="gemini-2.5-pro",
            request_type="analysis",
            prompt_preview="Test",
            prompt_tokens=100,
            response_preview="",
            completion_tokens=0,
            total_tokens=0,
            latency_ms=100.0,
            status="error",
            error_message="Rate limit exceeded",
        )

        assert entry.status == "error"
        assert entry.error_message == "Rate limit exceeded"


class TestGeminiAPILogger:
    """Tests for GeminiAPILogger class."""

    @pytest.fixture
    def temp_db_path(self) -> Path:
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test_logs.db"

    @pytest.mark.asyncio
    async def test_logger_initialization(self, temp_db_path: Path) -> None:
        """Test logger initialization creates database."""
        logger = GeminiAPILogger(db_path=temp_db_path)
        await logger.initialize()

        assert temp_db_path.exists()
        await logger.close()

    @pytest.mark.asyncio
    async def test_log_request_and_response(self, temp_db_path: Path) -> None:
        """Test logging a request and response pair."""
        logger = GeminiAPILogger(db_path=temp_db_path)
        await logger.initialize()

        # Log request
        await logger.log_request(
            request_id="req-001",
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "Test prompt"}],
            token_estimate=500,
        )

        # Log response
        await logger.log_response(
            request_id="req-001",
            response="Test response content",
            usage={
                "prompt_tokens": 500,
                "completion_tokens": 100,
                "total_tokens": 600,
            },
            elapsed_ms=1500.0,
        )

        # Retrieve logs
        logs = await logger.get_recent_logs(limit=10)

        assert len(logs) == 1
        assert logs[0].request_id == "req-001"
        assert logs[0].status == "success"
        assert logs[0].total_tokens == 600

        await logger.close()

    @pytest.mark.asyncio
    async def test_log_error(self, temp_db_path: Path) -> None:
        """Test logging an error."""
        logger = GeminiAPILogger(db_path=temp_db_path)
        await logger.initialize()

        # Log request
        await logger.log_request(
            request_id="req-error",
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "Test"}],
            token_estimate=100,
        )

        # Log error
        await logger.log_error(
            request_id="req-error",
            error="API rate limit exceeded",
            elapsed_ms=50.0,
        )

        # Retrieve logs
        logs = await logger.get_recent_logs(limit=10)

        assert len(logs) == 1
        assert logs[0].status == "error"
        assert "rate limit" in logs[0].error_message

        await logger.close()

    @pytest.mark.asyncio
    async def test_get_token_usage_summary(self, temp_db_path: Path) -> None:
        """Test getting token usage summary."""
        logger = GeminiAPILogger(db_path=temp_db_path)
        await logger.initialize()

        # Log multiple requests
        for i in range(3):
            await logger.log_request(
                request_id=f"req-{i}",
                model="gemini-2.5-pro",
                messages=[{"role": "user", "content": "Test"}],
                token_estimate=1000,
            )
            await logger.log_response(
                request_id=f"req-{i}",
                response="Response",
                usage={
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "total_tokens": 1200,
                },
                elapsed_ms=1000.0,
            )

        summary = await logger.get_token_usage_summary()

        assert summary["total_requests"] == 3
        assert summary["total_tokens"] == 3600
        assert summary["success_count"] == 3
        assert summary["success_rate"] == 100.0

        await logger.close()

    @pytest.mark.asyncio
    async def test_preview_truncation(self, temp_db_path: Path) -> None:
        """Test that long content is truncated in previews."""
        logger = GeminiAPILogger(db_path=temp_db_path, preview_length=50)
        await logger.initialize()

        long_content = "x" * 100

        await logger.log_request(
            request_id="req-long",
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": long_content}],
            token_estimate=100,
        )
        await logger.log_response(
            request_id="req-long",
            response=long_content,
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            elapsed_ms=500.0,
        )

        logs = await logger.get_recent_logs(limit=1)
        assert len(logs[0].prompt_preview) <= 53  # 50 + "..."
        assert len(logs[0].response_preview) <= 53

        await logger.close()

    @pytest.mark.asyncio
    async def test_request_type_detection(self, temp_db_path: Path) -> None:
        """Test automatic request type detection."""
        logger = GeminiAPILogger(db_path=temp_db_path)
        await logger.initialize()

        # Full history analysis
        await logger.log_request(
            request_id="req-1",
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "Analyze complete iteration history"}],
            token_estimate=100,
        )
        await logger.log_response(
            request_id="req-1",
            response="Done",
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            elapsed_ms=100.0,
        )

        # Devil's advocate
        await logger.log_request(
            request_id="req-2",
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "Be a devil's advocate"}],
            token_estimate=100,
        )
        await logger.log_response(
            request_id="req-2",
            response="Done",
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            elapsed_ms=100.0,
        )

        logs = await logger.get_recent_logs(limit=10)

        request_types = {log.request_type for log in logs}
        assert "full_history_analysis" in request_types
        assert "devil_advocate" in request_types

        await logger.close()

    @pytest.mark.asyncio
    async def test_subscribe_and_stream(self, temp_db_path: Path) -> None:
        """Test real-time subscription to log updates."""
        logger = GeminiAPILogger(db_path=temp_db_path)
        await logger.initialize()

        queue = logger.subscribe()

        # Log an entry
        await logger.log_request(
            request_id="req-stream",
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "Test"}],
            token_estimate=100,
        )
        await logger.log_response(
            request_id="req-stream",
            response="Done",
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            elapsed_ms=100.0,
        )

        # Check queue received the entry
        entry = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert entry.request_id == "req-stream"

        logger.unsubscribe(queue)
        await logger.close()
