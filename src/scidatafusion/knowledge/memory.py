"""Task-memory admission and revocation rules."""

from __future__ import annotations

from datetime import datetime

from scidatafusion.contracts.knowledge import MemoryStatus, TaskMemoryEntry
from scidatafusion.knowledge.integrity import calculate_task_memory_hash


class MemoryCurator:
    """Create immutable revocation successors without mutating prior memory entries."""

    @staticmethod
    def revoke(entry: TaskMemoryEntry, *, revoked_at: datetime, reason: str) -> TaskMemoryEntry:
        """Return a content-addressed revoked successor of one existing memory entry."""

        draft = entry.model_copy(
            update={
                "memory_id": "tme_" + "0" * 32,
                "status": MemoryStatus.REVOKED,
                "reusable": False,
                "revoked_at": revoked_at,
                "revocation_reason": reason,
                "supersedes_memory_hash": entry.memory_hash,
                "memory_hash": "0" * 64,
            }
        )
        validated = TaskMemoryEntry.model_validate(draft.model_dump(mode="python"))
        value = calculate_task_memory_hash(validated)
        return validated.model_copy(update={"memory_id": f"tme_{value[:32]}", "memory_hash": value})
