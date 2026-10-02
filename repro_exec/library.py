"""对外入口：提交复现 / 取消执行 / 查看单次执行 / 查看某缺陷最新执行。

线程安全：提交、取消、查看可由不同线程并发调用。
某缺陷的“最新执行”按提交被接受的先后确定，登记后永不回退。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, Optional

from .models import ExecutionStatus, ExecutionView
from .runner import _Execution, run_execution


class ReproLibrary:
    """本地复现脚本执行库，由缺陷管理上层直接调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: Dict[str, _Execution] = {}
        self._latest_by_defect: Dict[str, str] = {}

    def submit(
        self,
        defect_id: str,
        script_text: str,
        language: str,
        timeout_seconds: float,
    ) -> str:
        """接受一次复现提交，立即返回执行标识，不等待脚本结束。"""
        submitted_at = time.time()
        deadline_mono = time.monotonic() + timeout_seconds
        execution_id = uuid.uuid4().hex
        execution = _Execution(
            execution_id=execution_id,
            defect_id=defect_id,
            script_text=script_text,
            language=language,
            timeout_seconds=timeout_seconds,
            submitted_at=submitted_at,
            deadline_mono=deadline_mono,
        )
        with self._lock:
            self._executions[execution_id] = execution
            self._latest_by_defect[defect_id] = execution_id
        worker = threading.Thread(
            target=run_execution, args=(execution,), daemon=True
        )
        worker.start()
        return execution_id

    def cancel(self, execution_id: str) -> bool:
        """请求取消。返回是否被接受；已结束的执行返回 False 且结果不变。"""
        execution = self._get(execution_id)
        if execution is None:
            return False
        with execution.lock:
            if execution.status is not ExecutionStatus.RUNNING:
                return False
            execution.cancel_requested = True
            execution.cancel_event.set()
            return True

    def get_execution(self, execution_id: str) -> Optional[ExecutionView]:
        execution = self._get(execution_id)
        return execution.to_view() if execution is not None else None

    def get_latest(self, defect_id: str) -> Optional[ExecutionView]:
        with self._lock:
            execution_id = self._latest_by_defect.get(defect_id)
        if execution_id is None:
            return None
        return self.get_execution(execution_id)

    def _get(self, execution_id: str) -> Optional[_Execution]:
        with self._lock:
            return self._executions.get(execution_id)
