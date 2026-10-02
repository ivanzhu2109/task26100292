"""数据模型：执行状态与对外只读视图。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class ExecutionStatus(enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCESS,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.CANCELLED,
    }
)


@dataclass(frozen=True)
class ExecutionView:
    """单次执行的对外快照。终态发布后内容不可再变。"""

    execution_id: str
    defect_id: str
    language: str
    status: ExecutionStatus
    exit_code: Optional[int]
    stdout: bytes
    stdout_truncated: bool
    stderr: bytes
    stderr_truncated: bool
    submitted_at: float
    finished_at: Optional[float]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
