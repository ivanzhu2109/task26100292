"""执行引擎：负责单次执行的子进程生命周期。

要点：
- 子进程独立会话（start_new_session），整组可控；
- stdout/stderr 各由独立线程持续排空，只保留前 64 KiB；
- 终态发布前，主脚本及其普通子孙进程全部停止（SIGTERM -> SIGKILL），
  且输出管道读到 EOF；
- 终态在 execution.lock 下只写一次，取消一旦被接受则优先为 CANCELLED。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import List, Optional

from .models import ExecutionStatus, ExecutionView

OUTPUT_KEEP_BYTES = 64 * 1024
_READ_CHUNK = 64 * 1024
_TERM_GRACE_SECONDS = 0.3
_POLL_INTERVAL = 0.01


class _StreamBuffer:
    """保留前 OUTPUT_KEEP_BYTES 字节并标记截断，其余丢弃。"""

    def __init__(self) -> None:
        self._data = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        remaining = OUTPUT_KEEP_BYTES - len(self._data)
        if remaining > 0:
            self._data += chunk[:remaining]
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._data)


class _Execution:
    """单次执行的内部可变状态。"""

    def __init__(
        self,
        execution_id: str,
        defect_id: str,
        script_text: str,
        language: str,
        timeout_seconds: float,
        submitted_at: float,
        deadline_mono: float,
    ) -> None:
        self.execution_id = execution_id
        self.defect_id = defect_id
        self.script_text = script_text
        self.language = language
        self.timeout_seconds = timeout_seconds
        self.submitted_at = submitted_at
        self.deadline_mono = deadline_mono
        self.lock = threading.Lock()
        self.cancel_requested = False
        self.cancel_event = threading.Event()
        self.status = ExecutionStatus.RUNNING
        self.exit_code: Optional[int] = None
        self.finished_at: Optional[float] = None
        self.stdout = _StreamBuffer()
        self.stderr = _StreamBuffer()

    def to_view(self) -> ExecutionView:
        with self.lock:
            return ExecutionView(
                execution_id=self.execution_id,
                defect_id=self.defect_id,
                language=self.language,
                status=self.status,
                exit_code=self.exit_code,
                stdout=self.stdout.snapshot(),
                stdout_truncated=self.stdout.truncated,
                stderr=self.stderr.snapshot(),
                stderr_truncated=self.stderr.truncated,
                submitted_at=self.submitted_at,
                finished_at=self.finished_at,
            )


def _build_argv(execution: _Execution) -> List[str]:
    if execution.language == "sh":
        return ["/bin/sh", "-c", execution.script_text]
    if execution.language == "python":
        return [sys.executable, "-c", execution.script_text]
    raise ValueError(f"unsupported language: {execution.language}")


def _drain_stream(stream, buffer: _StreamBuffer) -> None:
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                return
            buffer.feed(chunk)
    finally:
        stream.close()


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_process_group(proc: subprocess.Popen, pgid: int) -> None:
    """确保主进程及同组子孙全部停止。调用时主进程可能已退出。"""
    if proc.poll() is None:
        _signal_group(pgid, signal.SIGTERM)
    elif _group_exists(pgid):
        _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None and not _group_exists(pgid):
            return
        time.sleep(_POLL_INTERVAL)
    _signal_group(pgid, signal.SIGKILL)
    while True:
        try:
            proc.wait(timeout=_POLL_INTERVAL)
        except subprocess.TimeoutExpired:
            pass
        if proc.returncode is not None and not _group_exists(pgid):
            return


def run_execution(execution: _Execution) -> None:
    """在线程中运行一次执行直到终态发布。"""
    exit_code: Optional[int] = None
    timed_out = False
    try:
        argv = _build_argv(execution)
    except ValueError:
        _finalize(execution, ExecutionStatus.FAILED, None)
        return

    if time.monotonic() >= execution.deadline_mono:
        _finalize(execution, ExecutionStatus.TIMEOUT, None)
        return

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        _finalize(execution, ExecutionStatus.FAILED, None)
        return

    pgid = proc.pid
    readers = [
        threading.Thread(
            target=_drain_stream, args=(proc.stdout, execution.stdout), daemon=True
        ),
        threading.Thread(
            target=_drain_stream, args=(proc.stderr, execution.stderr), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    while True:
        if execution.cancel_event.is_set():
            break
        if proc.poll() is not None:
            break
        if time.monotonic() >= execution.deadline_mono:
            timed_out = True
            break
        time.sleep(_POLL_INTERVAL)

    _reap_process_group(proc, pgid)
    for reader in readers:
        reader.join()

    if not timed_out and proc.returncode is not None:
        exit_code = proc.returncode

    with execution.lock:
        if execution.cancel_requested:
            status = ExecutionStatus.CANCELLED
        elif timed_out:
            status = ExecutionStatus.TIMEOUT
        elif exit_code == 0:
            status = ExecutionStatus.SUCCESS
        else:
            status = ExecutionStatus.FAILED
    _finalize(execution, status, exit_code)


def _finalize(
    execution: _Execution, status: ExecutionStatus, exit_code: Optional[int]
) -> None:
    with execution.lock:
        if execution.cancel_requested:
            status = ExecutionStatus.CANCELLED
        execution.status = status
        execution.exit_code = exit_code
        execution.finished_at = time.time()
