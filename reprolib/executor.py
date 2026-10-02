"""Public entry point: the reproduction script executor."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, Optional

from .model import ExecutionSnapshot, Language
from .process import ProcessRunner


class ReproExecutor:
    """In-memory library used directly by the defect-management layer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: Dict[str, ProcessRunner] = {}
        self._latest_by_defect: Dict[str, str] = {}

    def submit(
        self,
        defect_id: str,
        script: str,
        language: Language,
        timeout_seconds: float,
    ) -> str:
        """Accept a reproduction run; returns its execution id immediately."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        execution_id = uuid.uuid4().hex
        runner = ProcessRunner(
            execution_id=execution_id,
            defect_id=defect_id,
            language=language,
            script=script,
            timeout_seconds=timeout_seconds,
            submitted_at=time.time(),
        )
        with self._lock:
            self._executions[execution_id] = runner
            # Latest is decided by submit-accept order, not completion order.
            self._latest_by_defect[defect_id] = execution_id
        runner.start()
        return execution_id

    def cancel(self, execution_id: str) -> Optional[ExecutionSnapshot]:
        """Request cancellation; returns the snapshot, or None if unknown id."""
        runner = self._get_runner(execution_id)
        if runner is None:
            return None
        runner.request_cancel()
        return runner.snapshot()

    def get_execution(self, execution_id: str) -> Optional[ExecutionSnapshot]:
        runner = self._get_runner(execution_id)
        return runner.snapshot() if runner is not None else None

    def get_latest(self, defect_id: str) -> Optional[ExecutionSnapshot]:
        with self._lock:
            execution_id = self._latest_by_defect.get(defect_id)
        if execution_id is None:
            return None
        return self.get_execution(execution_id)

    def _get_runner(self, execution_id: str) -> Optional[ProcessRunner]:
        with self._lock:
            return self._executions.get(execution_id)
