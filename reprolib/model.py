"""Data model for the reproduction script execution library."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


OUTPUT_KEEP_BYTES = 64 * 1024


class Language(enum.Enum):
    SH = "sh"
    PYTHON = "python"


class Status(enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self is not Status.RUNNING


@dataclass(frozen=True)
class StreamCapture:
    """Captured prefix of one output stream."""

    data: bytes
    truncated: bool


@dataclass(frozen=True)
class ExecutionSnapshot:
    """Immutable view of one execution at a point in time."""

    execution_id: str
    defect_id: str
    language: Language
    status: Status
    exit_code: Optional[int]
    stdout: StreamCapture
    stderr: StreamCapture
    submitted_at: float
    finished_at: Optional[float]
