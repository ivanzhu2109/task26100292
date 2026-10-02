"""Cancel/natural-end races, terminal-state uniqueness, descendant cleanup."""

import time

from reprolib import Language, Status

from .conftest import pid_alive, wait_final


def test_cancel_natural_race_single_terminal_state(executor):
    for _ in range(40):
        eid = executor.submit("D-race", "sleep 0.02; exit 0", Language.SH, 10)
        at_cancel = executor.cancel(eid)
        final = wait_final(executor, eid)
        if at_cancel.status is Status.RUNNING:
            # Cancel was accepted while running: final must be CANCELLED.
            assert final.status is Status.CANCELLED
        else:
            # Cancel arrived after a terminal state: nothing may change.
            assert final.status is at_cancel.status
        # Terminal state and output are frozen.
        assert executor.get_execution(eid) == final
        repeat = executor.cancel(eid)
        assert repeat == final
        assert executor.get_execution(eid) == final


def test_descendants_killed_when_main_exits_early(executor, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = f"sleep 300 & echo $! > {pidfile}; exit 0"
    eid = executor.submit("D-orphan", script, Language.SH, 10)
    final = wait_final(executor, eid)
    assert final.status is Status.SUCCESS
    child_pid = int(pidfile.read_text().strip())
    # The terminal state was published only after the descendant stopped.
    assert not pid_alive(child_pid)


def test_descendant_holding_output_pipe_does_not_block(executor, tmp_path):
    pidfile = tmp_path / "child.pid"
    # Main exits immediately; child keeps stdout open and ignores nothing.
    script = f"sleep 300 & echo $! > {pidfile}; echo main-done; exit 0"
    eid = executor.submit("D-pipe", script, Language.SH, 10)
    final = wait_final(executor, eid)
    assert final.status is Status.SUCCESS
    assert final.stdout.data == b"main-done\n"
    assert not pid_alive(int(pidfile.read_text().strip()))


def test_unresponsive_to_sigterm_killed_within_one_second(executor):
    # Ignores SIGTERM in both the main script and its child.
    script = "trap '' TERM; sleep 300 & wait"
    eid = executor.submit("D-stubborn", script, Language.SH, 30)
    time.sleep(0.2)
    start = time.monotonic()
    executor.cancel(eid)
    final = wait_final(executor, eid)
    elapsed = time.monotonic() - start
    assert final.status is Status.CANCELLED
    assert elapsed < 1.0


def test_timeout_cleanup_within_one_second(executor):
    script = "trap '' TERM; sleep 300 & wait"
    timeout = 0.5
    submitted = time.monotonic()
    eid = executor.submit("D-timeout", script, Language.SH, timeout)
    final = wait_final(executor, eid)
    elapsed = time.monotonic() - submitted
    assert final.status is Status.TIMEOUT
    assert elapsed < timeout + 1.0


def test_stuck_execution_does_not_block_others(executor):
    stuck = executor.submit("D-iso", "sleep 300", Language.SH, 30)
    quick = executor.submit("D-iso", "echo quick", Language.SH, 10)
    final = wait_final(executor, quick, timeout=5)
    assert final.status is Status.SUCCESS
    assert final.stdout.data == b"quick\n"
    assert executor.get_execution(stuck).status is Status.RUNNING
    executor.cancel(stuck)
    assert wait_final(executor, stuck).status is Status.CANCELLED


def test_failed_exit_code_and_output(executor):
    eid = executor.submit(
        "D-fail", "echo out; echo err 1>&2; exit 3", Language.SH, 10
    )
    final = wait_final(executor, eid)
    assert final.status is Status.FAILURE
    assert final.exit_code == 3
    assert final.stdout.data == b"out\n"
    assert final.stderr.data == b"err\n"


def test_python_language_and_startup_failure(executor):
    ok = executor.submit(
        "D-py", "import sys; sys.stdout.write('py-ok')", Language.PYTHON, 10
    )
    final = wait_final(executor, ok)
    assert final.status is Status.SUCCESS
    assert final.stdout.data == b"py-ok"

    bad = executor.submit("D-py", "this is not python", Language.PYTHON, 10)
    final = wait_final(executor, bad)
    assert final.status is Status.FAILURE
    assert final.exit_code != 0
