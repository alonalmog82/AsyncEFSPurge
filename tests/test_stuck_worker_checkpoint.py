"""Tests for stuck-worker checkpoint rescue mechanism.

When a worker is blocked inside an EFS syscall (run_in_executor) it cannot
check _checkpoint_requested cooperatively.  After _STUCK_WORKER_CANCEL_TIMEOUT
the gather loop cancels the stuck task, rescues its in-flight directory into
_checkpoint_pending, and saves the checkpoint so the directory is retried on
the next resume — making the scan self-resolving given sufficient runs.
"""

import asyncio
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.checkpoint import load_checkpoint
from efspurge.purger import AsyncEFSPurger, CheckpointExit, async_scandir_batched


def _make_old_file(path: Path, days_old: int = 40) -> None:
    """Create a file with mtime old enough to be eligible for purging."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hanging_scandir_factory(stuck_paths: set[Path]):
    """Return a patched async_scandir_batched that blocks in a thread for stuck_paths.

    Uses run_in_executor + threading.Event to faithfully simulate a real EFS syscall
    hang: the blocking happens in a thread, so asyncio task.cancel() cannot unblock
    it (cf_future.cancel() returns False for a running thread).  This matches the
    prod behaviour where os._exit(75) is the only thing that kills the threads.
    """
    unblock_event = threading.Event()  # set by fixture teardown / test cleanup

    async def _patched(path, executor):
        if Path(path) in stuck_paths:
            loop = asyncio.get_running_loop()
            # Block in the thread pool — cannot be cancelled by asyncio
            await loop.run_in_executor(executor, lambda: unblock_event.wait(timeout=30))
            return
        async for batch in async_scandir_batched(path, executor):
            yield batch

    return _patched, unblock_event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_stuck_worker_rescued_to_checkpoint(tmp_path):
    """A worker stuck on one dir has that dir saved to checkpoint for retry."""
    root = tmp_path / "root"
    root.mkdir()
    normal_dir = root / "normal"
    normal_dir.mkdir()
    stuck_dir = root / "stuck"
    stuck_dir.mkdir()
    _make_old_file(normal_dir / "a.txt")
    _make_old_file(stuck_dir / "b.txt")

    purger = _make_purger(tmp_path, root)

    async def _trigger():
        await asyncio.sleep(0.2)
        purger._checkpoint_requested = True

    patched, unblock = _hanging_scandir_factory({stuck_dir})
    try:
        with patch("efspurge.purger.async_scandir_batched", new=patched):
            asyncio.create_task(_trigger())
            with pytest.raises(CheckpointExit):
                await purger._scan_and_purge_files()
    finally:
        unblock.set()

    cp = load_checkpoint(tmp_path / "cp.json")
    assert cp is not None, "Checkpoint file must be written"
    pending = set(cp["pending_dirs"])
    assert str(stuck_dir) in pending, f"stuck_dir should be rescued into checkpoint; pending={pending}"


@pytest.mark.asyncio
async def test_multiple_stuck_workers_all_rescued(tmp_path):
    """All concurrently stuck workers have their dirs rescued."""
    root = tmp_path / "root"
    root.mkdir()
    stuck_dirs = {root / f"stuck{i}" for i in range(3)}
    for d in stuck_dirs:
        d.mkdir()
        _make_old_file(d / "file.txt")

    purger = _make_purger(tmp_path, root)

    async def _trigger():
        await asyncio.sleep(0.2)
        purger._checkpoint_requested = True

    patched, unblock = _hanging_scandir_factory(stuck_dirs)
    try:
        with patch("efspurge.purger.async_scandir_batched", new=patched):
            asyncio.create_task(_trigger())
            with pytest.raises(CheckpointExit):
                await purger._scan_and_purge_files()
    finally:
        unblock.set()

    cp = load_checkpoint(tmp_path / "cp.json")
    assert cp is not None
    pending = set(cp["pending_dirs"])
    for d in stuck_dirs:
        assert str(d) in pending, f"{d.name} not rescued; pending={pending}"


@pytest.mark.asyncio
async def test_cooperative_workers_exit_without_rescue(tmp_path):
    """Workers that respond to _checkpoint_requested are not force-cancelled."""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(4):
        d = root / f"dir{i}"
        d.mkdir()
        _make_old_file(d / "f.txt")

    purger = _make_purger(tmp_path, root)

    rescued_warnings: list[str] = []
    original_warning = purger.logger.warning

    def _capture_warning(msg, *args, **kwargs):
        if "stuck on EFS" in str(msg):
            rescued_warnings.append(str(msg))
        return original_warning(msg, *args, **kwargs)

    purger.logger.warning = _capture_warning  # type: ignore[method-assign]

    async def _trigger():
        await asyncio.sleep(0.2)
        purger._checkpoint_requested = True

    asyncio.create_task(_trigger())
    with pytest.raises(CheckpointExit):
        await purger._scan_and_purge_files()

    # No force-cancel warnings expected — all workers responded cooperatively
    assert rescued_warnings == [], f"Unexpected stuck-worker rescues: {rescued_warnings}"


@pytest.mark.asyncio
async def test_rescued_dir_included_in_next_resume(tmp_path):
    """Checkpoint round-trip: rescued dir appears in pending_dirs on reload."""
    root = tmp_path / "root"
    root.mkdir()
    stuck_dir = root / "stuck"
    stuck_dir.mkdir()
    _make_old_file(stuck_dir / "old.txt")

    purger = _make_purger(tmp_path, root)

    async def _trigger():
        await asyncio.sleep(0.2)
        purger._checkpoint_requested = True

    patched, unblock = _hanging_scandir_factory({stuck_dir})
    try:
        with patch("efspurge.purger.async_scandir_batched", new=patched):
            asyncio.create_task(_trigger())
            with pytest.raises(CheckpointExit):
                await purger._scan_and_purge_files()
    finally:
        unblock.set()

    cp = load_checkpoint(tmp_path / "cp.json")
    assert cp is not None
    pending = [Path(p) for p in cp["pending_dirs"]]

    # The stuck_dir must be reachable by the next run — present in pending_dirs
    assert stuck_dir in pending or any(p == stuck_dir or stuck_dir in p.parents for p in pending), (
        f"stuck_dir not recoverable from checkpoint; pending={pending}"
    )
