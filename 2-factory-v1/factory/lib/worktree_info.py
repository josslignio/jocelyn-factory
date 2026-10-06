"""WorktreeInfo — data record for a mission's isolated git worktree (F002)."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeInfo:
    mission_id: str
    path: Path
    branch: str
    created_at: str
    base_commit: str
