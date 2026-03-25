"""Regression tests for the four-bug chain that prevented checkpoint saves in prod.

Background
----------
In production (EFS/NFS under saturation), ``save_checkpoint`` was never written
despite the rescue logic appearing to run.  Investigation revealed four layered bugs
— each one masked the next:

Bug 1  ``await gather(*still_stuck)``
       Hung indefinitely because ``run_in_executor`` threads blocked on NFS
       ``getdents()`` cannot acknowledge asyncio cancellation.

Bug 2  ``asyncio.wait_for`` in the finally block
       In Python ≥ 3.12, ``wait_for`` awaits cancellation acknowledgment after
       its timeout fires.  Stuck EFS threads never acknowledge → infinite hang.

Bug 3  Checkpoint write started *after* the finally-block cleanup
       The finally block waits up to 5 s for workers + 5 s for the loader before
       returning, leaving only a tiny window before OOMKill.  The fix starts the
       write concurrently with the finally block, gaining ~10 s.

Bug 4  ``open(checkpoint_file, "w")`` issued NFS ``open()`` with ``O_TRUNC``
       on the *existing* checkpoint inode.  On a saturated NFS server the
       ``O_TRUNC`` call hangs — the file is never truncated, the old checkpoint
       survives intact but no new one is written.  The fix uses
       ``tempfile.mkstemp()`` (new inode) + ``os.replace()`` (atomic rename).

Each test below targets one bug.  Without the corresponding fix the test would
either hang (Bugs 1 & 2), fail a timing assertion (Bug 3), or fail because
``save_checkpoint`` never returns (Bug 4).
"""

import asyncio
import builtins
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import efspurge.checkpoint as checkpoint_mod
from efspurge.checkpoint import load_checkpoint, save_checkpoint
from efspurge.purger import AsyncEFSPurger, CheckpointExit, async_scandir_batched


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_old_file(path: Path, days_old: int = 40) -> None:
    path.write_text("x")
    old_ts = time.time() - days_old * 86400
    os.utime(path, (old_ts, old_ts))


def _make_purger(tmp_path: Path, root: Path, stuck_worker_cancel_timeout: int = 1) -> AsyncEFSPurger:
    return AsyncEFSPurger(
        root_path=str(root),
        max_age_days=30,
        checkpoint_file=str(tmp_path / "cp.json"),
        stuck_worker_cancel_timeout=stuck_worker_cancel_timeout,
    )


def _hanging_scandir_factory(stuck_paths: set[Path]):
    """Scandir that blocks in a thread for stuck_paths (faithful EFS hang simulation).

    The blocking happens in a thread pool — asyncio task.cancel() cannot unblock it,
    matching the prod behaviour where os._exit is the only way out.
    """
    unblock_event = threading.Event()

    async def _patched(path, executor):
        if Path(path) in stuck_paths:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(executor, lambda: unblock_event.wait(timeout=30))
            return
        async for batch in async_scandir_batched(path, executor):
            yield batch

    return _patched, unblock_event


# ---------------------------------------------------------------------------
# Bug 1 & 2  — no-hang guarantee
#
# The function must raise CheckpointExit within a bounded wall-clock time.
# If Bug 1 (await gather on stuck tasks) or Bug 2 (asyncio.wait_for in finally)
# were present, the function would block indefinitely and asyncio.wait_for here
# would raise TimeoutError — making the test fail with a clear message.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_exit_completes_within_deadline_with_stuck_workers(tmp_path):
    """_scan_and_purge_files must raise CheckpointExit, not hang, when workers are stuck.

    Deadline = stuck_worker_cancel_timeout + 5 s (finally worker-wait) + 5 s (loader-wait)
             + 5 s slack  = 16 s total.

    Bug 1 regression: ``await asyncio.gather(*still_stuck)`` would hang indefinitely.
    Bug 2 regression: ``asyncio.wait_for(..., timeout=5)`` in finally would hang on
                      Python ≥ 3.12 because stuck threads never acknowledge cancellation.
    """
    STUCK_CANCEL_TIMEOUT = 1
    DEADLINE = STUCK_CANCEL_TIMEOUT + 5 + 5 + 5  # generous but bounded

    root = tmp_path / "root"
    root.mkdir()
    stuck_dir = root / "stuck"
    stuck_dir.mkdir()
    _make_old_file(stuck_dir / "old.txt")

    purger = _make_purger(tmp_path, root, stuck_worker_cancel_timeout=STUCK_CANCEL_TIMEOUT)

    async def _trigger():
        await asyncio.sleep(0.2)
        purger._checkpoint_requested = True

    patched, unblock = _hanging_scandir_factory({stuck_dir})
    try:
        with patch("efspurge.purger.async_scandir_batched", new=patched):
            asyncio.create_task(_trigger())
            with pytest.raises(CheckpointExit):
                # If the function hangs, this raises asyncio.TimeoutError and the
                # test fails with a message that makes the regression obvious.
                await asyncio.wait_for(
                    purger._scan_and_purge_files(),
                    timeout=DEADLINE,
                )
    finally:
        unblock.set()


# ---------------------------------------------------------------------------
# Bug 3  — early write (concurrent with finally-block cleanup)
#
# When stuck workers are rescued, the checkpoint write must be started BEFORE
# the finally block runs, so the write runs concurrently with the ~10 s cleanup.
#
# Observable signature:
#   Fixed  → save_checkpoint() called ~1 s after rescue; finally runs for ~5 s
#             concurrently; CheckpointExit raised ~5 s after save started.
#             Gap: CheckpointExit_time − write_start_time  ≈  5 s
#   Buggy  → save_checkpoint() called only after finally completes (~5 s);
#             write is instant on local fs; CheckpointExit raised immediately.
#             Gap: ≈  0 s
#
# We assert the gap > 2 s, which is impossible if the write happened after finally.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_write_starts_before_finally_overhead(tmp_path):
    """save_checkpoint must be called before the finally-block worker cleanup, not after.

    Bug 3 regression: the write was started only after the finally block, which waits up
    to 5 s for workers + 5 s for the loader.  In prod that used up the entire OOMKill
    window before the write even started.

    How the test works
    ------------------
    In a pure-asyncio test, cancelled tasks complete instantly (asyncio marks them done
    even if their executor thread is still running), so the finally block takes ~0 s.
    To create a measurable gap we patch asyncio.wait to add a 2 s sleep on the two
    finally-block calls (timeout=5.0), simulating the prod delay.

    Observable signature:
      Fixed  → save_checkpoint called ~(trigger_delay + stuck_timeout) s before
               finally starts; finally takes 2×2 = 4 s concurrently; gap ≥ 3 s.
      Buggy  → save_checkpoint called only after finally; gap ≈ 0 s.
    """
    STUCK_CANCEL_TIMEOUT = 1
    FINALLY_SIMULATED_DELAY_S = 3.0  # injected into each finally-block wait (timeout=5.0)
    MIN_EXPECTED_GAP_SECONDS = 2.5   # write started ≥ 2.5 s before CheckpointExit

    root = tmp_path / "root"
    root.mkdir()
    stuck_dir = root / "stuck"
    stuck_dir.mkdir()
    _make_old_file(stuck_dir / "old.txt")

    purger = _make_purger(tmp_path, root, stuck_worker_cancel_timeout=STUCK_CANCEL_TIMEOUT)

    write_start_time: list[float] = []
    _real_save = save_checkpoint

    def _recording_save(*args, **kwargs):
        write_start_time.append(time.monotonic())
        return _real_save(*args, **kwargs)

    # Patch asyncio.wait to add a delay on timeout=5.0 calls only.
    # Those are the two finally-block calls: asyncio.wait(workers, 5) and
    # asyncio.wait({loader}, 5).  Other calls (timeout=1.0, 50.0, etc.) pass through.
    _real_asyncio_wait = asyncio.wait
    finally_call_count = [0]

    async def _slow_finally_wait(aws, *, timeout=None, **kwargs):
        if timeout == 5.0 and finally_call_count[0] < 2:
            finally_call_count[0] += 1
            await asyncio.sleep(FINALLY_SIMULATED_DELAY_S)
        return await _real_asyncio_wait(aws, timeout=timeout, **kwargs)

    async def _trigger():
        await asyncio.sleep(0.2)
        purger._checkpoint_requested = True

    patched, unblock = _hanging_scandir_factory({stuck_dir})
    try:
        with patch("efspurge.purger.async_scandir_batched", new=patched):
            with patch("efspurge.purger.save_checkpoint", side_effect=_recording_save):
                with patch("asyncio.wait", new=_slow_finally_wait):
                    asyncio.create_task(_trigger())
                    with pytest.raises(CheckpointExit):
                        await asyncio.wait_for(
                            purger._scan_and_purge_files(),
                            timeout=30,
                        )
                    t_done = time.monotonic()
    finally:
        unblock.set()

    assert write_start_time, "save_checkpoint was never called — checkpoint not written at all"

    gap = t_done - write_start_time[0]
    assert gap >= MIN_EXPECTED_GAP_SECONDS, (
        f"Checkpoint write started only {gap:.2f} s before CheckpointExit was raised "
        f"(expected ≥ {MIN_EXPECTED_GAP_SECONDS} s with {FINALLY_SIMULATED_DELAY_S:.0f} s "
        f"of simulated finally overhead). "
        "This indicates the write happened AFTER the finally block instead of concurrently — "
        "Bug 3 regression: write must start before the finally-block cleanup overhead."
    )


# ---------------------------------------------------------------------------
# Bug 4  — atomic write avoids NFS O_TRUNC hang
#
# save_checkpoint must complete even when open(checkpoint_path, "w") hangs.
# The fix writes to a tempfile (new inode, no O_TRUNC on existing file) then
# renames atomically.  The buggy version called open(path, "w") directly.
#
# We simulate the NFS O_TRUNC hang by patching builtins.open to block when
# called with the checkpoint file path in write mode.  os.fdopen(fd, "w")
# (used for the mkstemp fd) receives an integer, not a string path, so it is
# NOT intercepted — the patched code mimics exactly what NFS saturation does.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_write_completes_despite_direct_open_hang(tmp_path):
    """save_checkpoint must complete even when open(checkpoint_path, 'w') hangs.

    Bug 4 regression: the original code did ``open(self.checkpoint_file, "w")``
    which issues NFS open() with O_TRUNC on the existing inode.  On a saturated
    NFS server this hangs indefinitely — the file is never truncated, no new
    checkpoint is written.

    We simulate the hang by patching builtins.open to block on the target path
    (string path + write mode only).  The fix uses tempfile.mkstemp() which
    opens a new inode by fd, bypassing the hung path.
    """
    cp_path = tmp_path / "checkpoint.json"

    # Write a pre-existing checkpoint so the file exists (just like prod).
    save_checkpoint(
        cp_path,
        root_path="/data",
        pending_dirs=["/data/dir1"],
        stats={"files_scanned": 0},
        config={"max_age_days": 30, "root_path": "/data"},
    )
    assert cp_path.exists()

    _real_open = builtins.open
    hang_event = threading.Event()

    def _trunc_hanging_open(path_or_fd, mode="r", *args, **kwargs):
        # Block only on direct string-path writes to the checkpoint file — same
        # as what NFS does when an O_TRUNC open hangs on the existing inode.
        if isinstance(path_or_fd, (str, Path)) and Path(path_or_fd) == cp_path and "w" in str(mode):
            hang_event.wait(timeout=30)
            return _real_open(path_or_fd, mode, *args, **kwargs)
        return _real_open(path_or_fd, mode, *args, **kwargs)

    try:
        with patch("builtins.open", side_effect=_trunc_hanging_open):
            # Must complete within 5 s — if the buggy open(path,'w') path is taken it hangs.
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: save_checkpoint(
                        cp_path,
                        root_path="/data",
                        pending_dirs=["/data/dir2"],
                        stats={"files_scanned": 100},
                        config={"max_age_days": 30, "root_path": "/data"},
                    ),
                ),
                timeout=5.0,
            )
    except asyncio.TimeoutError:
        pytest.fail(
            "save_checkpoint hung for > 5 s while builtins.open was patched to hang on "
            f"the checkpoint path {cp_path}.  This is Bug 4: the code is calling "
            "open(path, 'w') directly instead of using tempfile.mkstemp() + os.replace()."
        )
    finally:
        hang_event.set()

    # Verify the new checkpoint was written correctly.
    cp = load_checkpoint(cp_path)
    assert cp is not None, "Checkpoint file not loadable after atomic write"
    assert "/data/dir2" in cp["pending_dirs"], "New checkpoint content not written"


@pytest.mark.asyncio
async def test_checkpoint_old_content_preserved_until_new_write_complete(tmp_path):
    """Old checkpoint must remain intact and readable while new one is being written.

    This is the atomicity guarantee: a reader that loads the checkpoint file during
    a concurrent write must never see a truncated or partially-written file.
    Bug 4 regression: the original ``open(path, 'w')`` truncated the file immediately,
    leaving a window where a concurrent reader would see an empty file.
    """
    cp_path = tmp_path / "checkpoint.json"

    save_checkpoint(
        cp_path,
        root_path="/data",
        pending_dirs=[f"/data/dir{i}" for i in range(100)],
        stats={"files_scanned": 0},
        config={"max_age_days": 30, "root_path": "/data"},
    )

    read_errors: list[str] = []
    stop_reading = threading.Event()

    def _concurrent_reader():
        """Keep reading the checkpoint file while save_checkpoint is running."""
        while not stop_reading.is_set():
            try:
                data = cp_path.read_text()
                if not data:
                    read_errors.append("checkpoint file was empty during concurrent write")
                else:
                    import json as _json
                    _json.loads(data)  # must be valid JSON at all times
            except (OSError, ValueError) as e:
                read_errors.append(f"checkpoint unreadable during write: {e}")
            time.sleep(0.001)

    reader_thread = threading.Thread(target=_concurrent_reader, daemon=True)
    reader_thread.start()

    try:
        # Run 10 concurrent saves to maximise the chance of observing a window.
        for i in range(10):
            save_checkpoint(
                cp_path,
                root_path="/data",
                pending_dirs=[f"/data/batch{i}/dir{j}" for j in range(50)],
                stats={"files_scanned": i * 50},
                config={"max_age_days": 30, "root_path": "/data"},
            )
    finally:
        stop_reading.set()
        reader_thread.join(timeout=2.0)

    assert not read_errors, (
        "Checkpoint file was observed in an invalid state during a concurrent write — "
        "the write is not atomic.  Bug 4 regression: "
        + "; ".join(read_errors[:3])
    )
