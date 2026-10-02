import os
import time

import pytest

from reprolib import ReproExecutor


@pytest.fixture()
def executor():
    return ReproExecutor()


def wait_final(ex, execution_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = ex.get_execution(execution_id)
        if snap is not None and snap.status.is_final:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"execution {execution_id} did not finish in {timeout}s")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
