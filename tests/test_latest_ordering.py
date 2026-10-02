"""Latest execution per defect follows submit-accept order, never completion."""

from reprolib import Language, Status

from .conftest import wait_final


def test_latest_follows_submit_order_not_completion(executor):
    slow = executor.submit("D-1", "sleep 0.8; echo old", Language.SH, 10)
    fast = executor.submit("D-1", "echo new", Language.SH, 10)

    fast_final = wait_final(executor, fast)
    assert fast_final.status is Status.SUCCESS
    # The newer execution is latest even while the older one still runs.
    latest = executor.get_latest("D-1")
    assert latest.execution_id == fast

    slow_final = wait_final(executor, slow)
    assert slow_final.status is Status.SUCCESS
    # The older execution finishing later must not take over "latest".
    latest = executor.get_latest("D-1")
    assert latest.execution_id == fast
    assert latest.status is Status.SUCCESS
    # The old execution remains viewable by its own id.
    assert executor.get_execution(slow).status is Status.SUCCESS


def test_latest_cancelled_does_not_roll_back(executor):
    old = executor.submit("D-2", "sleep 0.8; echo old", Language.SH, 10)
    new = executor.submit("D-2", "sleep 300", Language.SH, 30)

    executor.cancel(new)
    new_final = wait_final(executor, new)
    assert new_final.status is Status.CANCELLED

    latest = executor.get_latest("D-2")
    assert latest.execution_id == new
    assert latest.status is Status.CANCELLED

    old_final = wait_final(executor, old)
    assert old_final.status is Status.SUCCESS
    # Old execution completing later must not resurrect it as latest.
    latest = executor.get_latest("D-2")
    assert latest.execution_id == new
    assert latest.status is Status.CANCELLED


def test_running_new_execution_is_shown_as_latest(executor):
    executor.submit("D-3", "echo done", Language.SH, 10)
    running = executor.submit("D-3", "sleep 5", Language.SH, 10)
    latest = executor.get_latest("D-3")
    assert latest.execution_id == running
    assert latest.status is Status.RUNNING
    executor.cancel(running)
    wait_final(executor, running)


def test_unknown_ids(executor):
    assert executor.get_execution("nope") is None
    assert executor.get_latest("nope") is None
    assert executor.cancel("nope") is None
