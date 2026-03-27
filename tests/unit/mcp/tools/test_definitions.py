"""Tests for Ouroboros tool definitions."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import _is_interview_completion_signal
from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    CancelExecutionHandler,
    CancelJobHandler,
    EvaluateHandler,
    EvolveRewindHandler,
    EvolveStepHandler,
    ExecuteSeedHandler,
    GenerateSeedHandler,
    InterviewHandler,
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
    LateralThinkHandler,
    LineageStatusHandler,
    MeasureDriftHandler,
    QueryEventsHandler,
    SessionStatusHandler,
    StartEvolveStepHandler,
    StartExecuteSeedHandler,
    evaluate_handler,
    execute_seed_handler,
    generate_seed_handler,
    get_ouroboros_tools,
    interview_handler,
    start_execute_seed_handler,
)
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.types import ToolInputType
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
    DELEGATED_PARENT_SESSION_ID_ARG,
)
from ouroboros.orchestrator.session import SessionStatus, SessionTracker
