"""Single-execution process lifecycle management."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import IO, List, Optional

from .model import (
    OUTPUT_KEEP_BYTES,
    ExecutionSnapshot,
    Language,
    Status,
    StreamCapture,
)

_READ_CHUNK = 64 * 1024
_TERM_GRACE_SECONDS = 0.4
_KILL_WAIT_SECONDS = 0.5


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_for(language: Language, script: str) -> List[str]:
    if language is Language.SH:
        return ["/bin/sh", "-c", script]
    if language is Language.PYTHON:
        return [sys.executable, "-c", script]
    raise ValueError(f"unsupported language: {language!r}")


class ProcessRunner:
    """Runs one submitted script to a single terminal state."""

    def __init__(
        self,
        execution_id: str,
        defect_id: str,
        language: Language,
        script: str,
        timeout_seconds: float,
        submitted_at: float,
    ) -> None:
        self._execution_id = execution_id
        self._defect_id = defect_id
        self._language = language
        self._script = script
        self._timeout_seconds = timeout_seconds
        self._submitted_at = submitted_at
        self._cond = threading.Condition()
        self._status = Status.RUNNING
        self._exit_code: Optional[int] = None
        self._finished_at: Optional[float] = None
        self._stdout = StreamCapture(b"", False)
        self._stderr = StreamCapture(b"", False)
        self._cancel_requested = False
        self._timed_out = False
        self._group_stopped = False
        self._cleanup_lock = threading.Lock()
        self._pgid: Optional[int] = None
        self._submitted_monotonic = time.monotonic()
        self._worker: Optional[threading.Thread] = None

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._run,
            name=f"repro-worker-{self._execution_id}",
            daemon=True,
        )
        self._worker.start()

    def request_cancel(self) -> bool:
        """Accept cancellation if still running; return True if accepted."""
        with self._cond:
            if self._status.is_final:
                return False
            self._cancel_requested = True
            self._cond.notify_all()
            return True

    def wait_finished(self, timeout: Optional[float] = None) -> bool:
        with self._cond:
            if not self._status.is_final:
                self._cond.wait_for(lambda: self._status.is_final, timeout)
            return self._status.is_final

    def _run(self) -> None:
        try:
            proc = subprocess.Popen(
                _command_for(self._language, self._script),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError):
            self._finalize(Status.FAILURE, None)
            return

        self._pgid = proc.pid
        assert proc.stdout is not None and proc.stderr is not None
        readers = [
            self._start_reader(proc.stdout, "stdout"),
            self._start_reader(proc.stderr, "stderr"),
        ]
        monitor = threading.Thread(
            target=self._monitor,
            name=f"repro-monitor-{self._execution_id}",
            daemon=True,
        )
        monitor.start()

        exit_code = proc.wait()
        # The main script is gone; make sure no descendant survives it.
        self._stop_group()
        for reader in readers:
            reader.join()
        monitor.join()

        with self._cond:
            if self._cancel_requested:
                status = Status.CANCELLED
            elif self._timed_out:
                status = Status.TIMEOUT
            elif exit_code == 0:
                status = Status.SUCCESS
            else:
                status = Status.FAILURE
        self._finalize(status, exit_code)

    def _monitor(self) -> None:
        deadline = self._submitted_monotonic + self._timeout_seconds
        with self._cond:
            while True:
                if self._status.is_final or self._group_stopped:
                    return
                if self._cancel_requested or self._timed_out:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._timed_out = True
                    break
                self._cond.wait(remaining)
        self._stop_group()

    def _stop_group(self) -> None:
        """SIGTERM then SIGKILL the process group; block until it is gone."""
        with self._cleanup_lock:
            if self._group_stopped:
                return
            pgid = self._pgid
            if pgid is not None and _group_alive(pgid):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                deadline = time.monotonic() + _TERM_GRACE_SECONDS
                while _group_alive(pgid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                if _group_alive(pgid):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    deadline = time.monotonic() + _KILL_WAIT_SECONDS
                    while _group_alive(pgid) and time.monotonic() < deadline:
                        time.sleep(0.01)
            with self._cond:
                self._group_stopped = True
                self._cond.notify_all()

    def _start_reader(self, stream: IO[bytes], which: str) -> threading.Thread:
        reader = threading.Thread(
            target=self._read_stream,
            args=(stream, which),
            name=f"repro-reader-{which}-{self._execution_id}",
            daemon=True,
        )
        reader.start()
        return reader

    def _read_stream(self, stream: IO[bytes], which: str) -> None:
        kept = bytearray()
        truncated = False
        try:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                room = OUTPUT_KEEP_BYTES - len(kept)
                if room > 0:
                    kept += chunk[:room]
                    if len(chunk) > room:
                        truncated = True
                else:
                    truncated = True
        finally:
            stream.close()
        capture = StreamCapture(bytes(kept), truncated)
        with self._cond:
            if which == "stdout":
                self._stdout = capture
            else:
                self._stderr = capture

    def _finalize(self, status: Status, exit_code: Optional[int]) -> None:
        with self._cond:
            if self._status.is_final:
                return
            self._status = status
            self._exit_code = exit_code
            self._finished_at = time.time()
            self._cond.notify_all()

    def snapshot(self) -> ExecutionSnapshot:
        with self._cond:
            return ExecutionSnapshot(
                execution_id=self._execution_id,
                defect_id=self._defect_id,
                language=self._language,
                status=self._status,
                exit_code=self._exit_code,
                stdout=self._stdout,
                stderr=self._stderr,
                submitted_at=self._submitted_at,
                finished_at=self._finished_at,
            )
