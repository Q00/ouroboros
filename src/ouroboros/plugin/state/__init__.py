"""State persistence and recovery for Ouroboros.

This package provides:
- StateStore: JSON file-based storage with atomic writes
- StateManager: Session state persistence and recovery
- Recovery: Auto-resume hooks after interruptions
- Compression: Smart context compression when approaching limits

The state management system ensures:
1. Session state persists across /clear
2. Mode state (Autopilot/Ralph/Ultrawork) survives restarts
3. Automatic checkpoint and recovery
4. Graceful degradation on failures

Usage:
    from ouroboros.plugin.state import StateManager, StateStore

    # Initialize store
    store = StateStore(worktree="/path/to/project")
    manager = StateManager(store)

    # Save session state
    await manager.save_session(session_id, state)

    # Load session state
    state = await manager.load_session(session_id)

    # Create checkpoint
    checkpoint_id = await manager.create_checkpoint(session_id)
"""

from ouroboros.plugin.state.compression import (
    CompressionConfig,
    StateCompression,
    compress_state_dict,
)
from ouroboros.plugin.state.manager import (
    CheckpointData,
    SessionState,
    SessionStatus,
    StateManager,
)
from ouroboros.plugin.state.recovery import (
    RecoveryHook,
    RecoveryManager,
    RecoveryResult,
    RecoveryTrigger,
)
from ouroboros.plugin.state.store import (
    AtomicWriteError,
    SchemaMigration,
    StateMode,
    StateStore,
    load_state_store,
)

__all__ = [
    # State Store
    "StateStore",
    "load_state_store",
    "AtomicWriteError",
    "SchemaMigration",
    "StateMode",
    # State Manager
    "StateManager",
    "SessionState",
    "SessionStatus",
    "CheckpointData",
    # Recovery
    "RecoveryManager",
    "RecoveryHook",
    "RecoveryResult",
    "RecoveryTrigger",
    # Compression
    "StateCompression",
    "CompressionConfig",
    "compress_state_dict",
]
