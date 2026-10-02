"""Per-stream 64 KiB prefix retention with truncation flags, byte-exact."""

from reprolib import OUTPUT_KEEP_BYTES, Language, Status

from .conftest import wait_final


def _pattern(seed, length):
    return bytes(((i + seed) % 256 for i in range(length)))


def test_both_streams_prefix_and_truncation(executor):
    out_len = OUTPUT_KEEP_BYTES * 3 + 12345
    err_len = OUTPUT_KEEP_BYTES * 2 + 777
    script = (
        "import sys\n"
        f"sys.stdout.buffer.write(bytes((i % 256 for i in range({out_len}))))\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write(bytes(((i + 7) % 256 for i in range({err_len}))))\n"
        "sys.stderr.buffer.flush()\n"
    )
    eid = executor.submit("D-cap", script, Language.PYTHON, 30)
    final = wait_final(executor, eid)
    assert final.status is Status.SUCCESS

    assert final.stdout.truncated is True
    assert len(final.stdout.data) == OUTPUT_KEEP_BYTES
    assert final.stdout.data == _pattern(0, out_len)[:OUTPUT_KEEP_BYTES]

    assert final.stderr.truncated is True
    assert len(final.stderr.data) == OUTPUT_KEEP_BYTES
    assert final.stderr.data == _pattern(7, err_len)[:OUTPUT_KEEP_BYTES]


def test_exactly_at_limit_not_truncated(executor):
    script = (
        "import sys\n"
        f"sys.stdout.buffer.write(bytes((i % 256 for i in range({OUTPUT_KEEP_BYTES}))))\n"
        "sys.stdout.buffer.flush()\n"
    )
    eid = executor.submit("D-cap", script, Language.PYTHON, 30)
    final = wait_final(executor, eid)
    assert final.status is Status.SUCCESS
    assert final.stdout.truncated is False
    assert final.stdout.data == _pattern(0, OUTPUT_KEEP_BYTES)
    assert final.stderr.data == b""
    assert final.stderr.truncated is False


def test_one_byte_over_limit_truncated(executor):
    script = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'x' * {OUTPUT_KEEP_BYTES + 1})\n"
        "sys.stdout.buffer.flush()\n"
    )
    eid = executor.submit("D-cap", script, Language.PYTHON, 30)
    final = wait_final(executor, eid)
    assert final.stdout.truncated is True
    assert final.stdout.data == b"x" * OUTPUT_KEEP_BYTES


def test_small_output_not_truncated(executor):
    eid = executor.submit(
        "D-cap", "echo hello; echo world 1>&2", Language.SH, 10
    )
    final = wait_final(executor, eid)
    assert final.stdout.data == b"hello\n"
    assert final.stdout.truncated is False
    assert final.stderr.data == b"world\n"
    assert final.stderr.truncated is False


def test_flooding_execution_still_cancellable(executor):
    # Endless output must not block cancellation or other bookkeeping.
    script = "yes flood | head -c 20000000; sleep 300"
    eid = executor.submit("D-flood", script, Language.SH, 30)
    executor.cancel(eid)
    final = wait_final(executor, eid)
    assert final.status is Status.CANCELLED
    assert len(final.stdout.data) <= OUTPUT_KEEP_BYTES
