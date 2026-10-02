import os
import resource
import sys
import tempfile
import threading
import time

import pytest

from repro_exec import ExecutionStatus, ReproLibrary

KEEP = 64 * 1024


def wait_terminal(lib, eid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = lib.get_execution(eid)
        if view.is_terminal:
            return view
        time.sleep(0.01)
    raise AssertionError(f"execution {eid} did not finish in {timeout}s")


def test_success_and_exit_code():
    lib = ReproLibrary()
    eid = lib.submit("D-OK", "echo out; echo err 1>&2; exit 0", "sh", 5.0)
    view = wait_terminal(lib, eid)
    assert view.status is ExecutionStatus.SUCCESS
    assert view.exit_code == 0
    assert view.stdout == b"out\n"
    assert view.stderr == b"err\n"
    assert not view.stdout_truncated and not view.stderr_truncated


def test_failure_on_nonzero_exit_and_bad_language():
    lib = ReproLibrary()
    eid = lib.submit("D-F", "exit 3", "sh", 5.0)
    view = wait_terminal(lib, eid)
    assert view.status is ExecutionStatus.FAILED
    assert view.exit_code == 3

    eid2 = lib.submit("D-F", "whatever", "ruby", 5.0)
    view2 = wait_terminal(lib, eid2)
    assert view2.status is ExecutionStatus.FAILED


def test_python_language():
    lib = ReproLibrary()
    eid = lib.submit("D-PY", "print('hi')", "python", 5.0)
    view = wait_terminal(lib, eid)
    assert view.status is ExecutionStatus.SUCCESS
    assert view.stdout == b"hi\n"


def test_latest_follows_submission_order_not_completion():
    lib = ReproLibrary()
    # 旧执行：很快成功；新执行：较慢。新执行尚未结束时就必须显示新执行。
    old_eid = lib.submit("D-ORD", "exit 0", "sh", 5.0)
    new_eid = lib.submit("D-ORD", "sleep 0.6; exit 0", "sh", 5.0)

    latest = lib.get_latest("D-ORD")
    assert latest is not None and latest.execution_id == new_eid
    assert latest.status is ExecutionStatus.RUNNING

    old_view = wait_terminal(lib, old_eid)
    assert old_view.status is ExecutionStatus.SUCCESS
    # 旧执行完成后，最新仍指向后提交者。
    assert lib.get_latest("D-ORD").execution_id == new_eid

    new_view = wait_terminal(lib, new_eid)
    assert new_view.status is ExecutionStatus.SUCCESS
    assert lib.get_latest("D-ORD").execution_id == new_eid
    # 各次执行仍可按执行标识查看。
    assert lib.get_execution(old_eid).status is ExecutionStatus.SUCCESS


def test_latest_not_rolled_back_after_cancel():
    lib = ReproLibrary()
    old_eid = lib.submit("D-CXL", "exit 0", "sh", 5.0)
    new_eid = lib.submit("D-CXL", "sleep 30", "sh", 30.0)
    wait_terminal(lib, old_eid)

    assert lib.cancel(new_eid) is True
    new_view = wait_terminal(lib, new_eid)
    assert new_view.status is ExecutionStatus.CANCELLED

    latest = lib.get_latest("D-CXL")
    assert latest.execution_id == new_eid
    assert latest.status is ExecutionStatus.CANCELLED
    # 重复取消不改变结果。
    assert lib.cancel(new_eid) is False
    assert lib.get_latest("D-CXL").status is ExecutionStatus.CANCELLED


def test_cancel_natural_race_single_immutable_terminal_state():
    lib = ReproLibrary()
    for _ in range(50):
        eid = lib.submit("D-RACE", "exit 0", "sh", 5.0)
        time.sleep(0.001)
        accepted = lib.cancel(eid)
        view = wait_terminal(lib, eid)
        assert view.status in (ExecutionStatus.SUCCESS, ExecutionStatus.CANCELLED)
        if accepted:
            # 取消一旦被确认接受，最终必须是取消。
            assert view.status is ExecutionStatus.CANCELLED
        # 终态公布后不再变化。
        again = lib.get_execution(eid)
        time.sleep(0.05)
        third = lib.get_execution(eid)
        assert again == view == third
        assert lib.cancel(eid) is False


def test_descendant_killed_when_main_exits_early():
    lib = ReproLibrary()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        pid_file = tmp.name
    script = f"sleep 30 & echo $! > {pid_file}; exit 0"
    eid = lib.submit("D-ORPHAN", script, "sh", 10.0)
    view = wait_terminal(lib, eid)
    assert view.status is ExecutionStatus.SUCCESS

    with open(pid_file) as fh:
        child_pid = int(fh.read().strip())
    os.unlink(pid_file)

    # 公布终态时后代必须已停止（允许极短的回收延迟）。
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"descendant {child_pid} still running after terminal state")

    # 其他执行不受影响。
    other = lib.submit("D-OTHER", "echo fine", "sh", 5.0)
    assert wait_terminal(lib, other).status is ExecutionStatus.SUCCESS


def test_descendant_ignoring_sigterm_is_killed():
    lib = ReproLibrary()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        pid_file = tmp.name
    script = (
        f"sh -c 'trap \"\" TERM; sleep 30' & echo $! > {pid_file}; exit 0"
    )
    eid = lib.submit("D-STUBBORN", script, "sh", 10.0)
    view = wait_terminal(lib, eid)
    assert view.status is ExecutionStatus.SUCCESS

    with open(pid_file) as fh:
        child_pid = int(fh.read().strip())
    os.unlink(pid_file)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"stubborn descendant {child_pid} still running")


def test_output_prefix_and_truncation_flags():
    lib = ReproLibrary()
    expected_out = bytes((i % 251 for i in range(KEEP + 12345)))
    expected_err = bytes(((i * 7) % 253 for i in range(KEEP + 1)))
    script = (
        "import sys;"
        f"sys.stdout.buffer.write(bytes([i % 251 for i in range({KEEP + 12345})]));"
        "sys.stdout.buffer.flush();"
        f"sys.stderr.buffer.write(bytes([(i * 7) % 253 for i in range({KEEP + 1})]));"
        "sys.stderr.buffer.flush()"
    )
    eid = lib.submit("D-OUT", script, "python", 20.0)
    view = wait_terminal(lib, eid)
    assert view.status is ExecutionStatus.SUCCESS
    # 逐字节核对保留前缀。
    assert view.stdout == expected_out[:KEEP]
    assert view.stderr == expected_err[:KEEP]
    assert view.stdout_truncated is True
    assert view.stderr_truncated is True


def test_output_exactly_at_limit_not_truncated():
    lib = ReproLibrary()
    script = (
        "import sys;"
        f"sys.stdout.buffer.write(bytes([65]) * {KEEP});"
        "sys.stdout.buffer.flush()"
    )
    eid = lib.submit("D-EXACT", script, "python", 20.0)
    view = wait_terminal(lib, eid)
    assert view.stdout == b"A" * KEEP
    assert view.stdout_truncated is False


def test_timeout_counts_from_submission():
    lib = ReproLibrary()
    started = time.monotonic()
    eid = lib.submit("D-TO", "sleep 30", "sh", 0.5)
    view = wait_terminal(lib, eid, timeout=5.0)
    elapsed = time.monotonic() - started
    assert view.status is ExecutionStatus.TIMEOUT
    # 到达超时后 1 秒内完成清理并公布终态（留 0.5s 调度余量）。
    assert elapsed < 0.5 + 1.5


def test_timeout_with_stubborn_descendant_cleanup_within_one_second():
    lib = ReproLibrary()
    script = "trap '' TERM; sleep 30 & sleep 30"
    started = time.monotonic()
    eid = lib.submit("D-TO2", script, "sh", 0.5)
    view = wait_terminal(lib, eid, timeout=5.0)
    elapsed = time.monotonic() - started
    assert view.status is ExecutionStatus.TIMEOUT
    assert elapsed < 0.5 + 1.5


def test_cancel_confirmed_within_one_second():
    lib = ReproLibrary()
    eid = lib.submit("D-CFAST", "sleep 30", "sh", 30.0)
    time.sleep(0.2)
    started = time.monotonic()
    assert lib.cancel(eid) is True
    view = wait_terminal(lib, eid, timeout=5.0)
    elapsed = time.monotonic() - started
    assert view.status is ExecutionStatus.CANCELLED
    assert elapsed < 1.5


def test_eight_parallel_one_second_jobs_finish_within_three_seconds():
    lib = ReproLibrary()
    started = time.monotonic()
    eids = [lib.submit(f"D-PAR-{i}", "sleep 1; exit 0", "sh", 10.0) for i in range(8)]
    views = [wait_terminal(lib, eid, timeout=10.0) for eid in eids]
    elapsed = time.monotonic() - started
    assert all(v.status is ExecutionStatus.SUCCESS for v in views)
    assert elapsed < 3.0


def test_noisy_execution_does_not_block_others():
    lib = ReproLibrary()
    noisy = lib.submit(
        "D-NOISY",
        "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)",
        "python",
        3.0,
    )
    quiet = lib.submit("D-QUIET", "echo done", "sh", 5.0)
    assert wait_terminal(lib, quiet).status is ExecutionStatus.SUCCESS
    noisy_view = wait_terminal(lib, noisy, timeout=10.0)
    assert noisy_view.status is ExecutionStatus.TIMEOUT
    assert noisy_view.stdout_truncated is True


def test_peak_memory_with_eight_large_output_jobs():
    lib = ReproLibrary()
    size = 64 * 1024 * 1024
    script = (
        "import sys;"
        f"d = b'x' * {size};"
        "sys.stdout.buffer.write(d); sys.stdout.buffer.flush();"
        "sys.stderr.buffer.write(d); sys.stderr.buffer.flush()"
    )
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    eids = [lib.submit(f"D-MEM-{i}", script, "python", 60.0) for i in range(8)]
    views = [wait_terminal(lib, eid, timeout=60.0) for eid in eids]
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert all(v.status is ExecutionStatus.SUCCESS for v in views)
    assert all(v.stdout_truncated and v.stderr_truncated for v in views)
    assert all(len(v.stdout) == KEEP and len(v.stderr) == KEEP for v in views)
    if sys.platform != "darwin":
        baseline *= 1024
        peak *= 1024
    extra = peak - baseline
    assert extra <= 32 * 1024 * 1024, f"extra peak memory {extra} bytes"


def test_unknown_ids():
    lib = ReproLibrary()
    assert lib.get_execution("nope") is None
    assert lib.get_latest("no-such-defect") is None
    assert lib.cancel("nope") is False
