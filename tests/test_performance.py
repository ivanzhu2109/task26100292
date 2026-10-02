"""Parallelism, termination-deadline, memory, and concurrent-API checks."""

import resource
import sys
import threading
import time

from reprolib import OUTPUT_KEEP_BYTES, Language, Status

from .conftest import wait_final


def _peak_rss_bytes():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS reports bytes.
    return rss if sys.platform == "darwin" else rss * 1024


def test_eight_one_second_runs_finish_within_three_seconds(executor):
    start = time.monotonic()
    ids = [
        executor.submit(f"D-par-{i}", "sleep 1", Language.SH, 30)
        for i in range(8)
    ]
    for eid in ids:
        final = wait_final(executor, eid)
        assert final.status is Status.SUCCESS
    elapsed = time.monotonic() - start
    assert elapsed < 3.0


def test_memory_bounded_with_large_output_batch(executor):
    mib = 1 << 20
    script = (
        "import sys\n"
        f"chunk = b'x' * {mib}\n"
        "out, err = sys.stdout.buffer, sys.stderr.buffer\n"
        "for _ in range(64):\n"
        "    out.write(chunk); err.write(chunk)\n"
        "out.flush(); err.flush()\n"
    )
    before = _peak_rss_bytes()
    ids = [
        executor.submit(f"D-mem-{i}", script, Language.PYTHON, 60)
        for i in range(8)
    ]
    for eid in ids:
        final = wait_final(executor, eid, timeout=60)
        assert final.status is Status.SUCCESS
        assert final.stdout.truncated and final.stderr.truncated
        assert len(final.stdout.data) == OUTPUT_KEEP_BYTES
        assert len(final.stderr.data) == OUTPUT_KEEP_BYTES
    extra = _peak_rss_bytes() - before
    assert extra < 32 * mib, f"parent peak RSS grew by {extra / mib:.1f} MiB"


def test_cancel_deadline_under_load(executor):
    workers = [
        executor.submit(f"D-load-{i}", "sleep 2", Language.SH, 30)
        for i in range(6)
    ]
    victim = executor.submit(
        "D-load-v", "trap '' TERM; sleep 300", Language.SH, 30
    )
    time.sleep(0.2)
    start = time.monotonic()
    executor.cancel(victim)
    final = wait_final(executor, victim)
    assert time.monotonic() - start < 1.0
    assert final.status is Status.CANCELLED
    for eid in workers:
        assert wait_final(executor, eid).status is Status.SUCCESS


def test_concurrent_submit_cancel_get_from_many_threads(executor):
    errors = []

    def work(index):
        try:
            for round_ in range(10):
                defect = f"D-thr-{round_ % 3}"
                eid = executor.submit(
                    defect, f"sleep 0.05; echo {index}", Language.SH, 10
                )
                if index % 2 == 0:
                    executor.cancel(eid)
                assert executor.get_execution(eid) is not None
                assert executor.get_latest(defect) is not None
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
