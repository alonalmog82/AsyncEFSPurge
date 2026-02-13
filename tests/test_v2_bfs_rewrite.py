"""Tests for v2.0 BFS rewrite: worker cleanup, Phase 1b timeout, and removed APIs."""

import asyncio
import os
import tempfile
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# test_worker_cleanup_on_exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_cleanup_on_exception(temp_dir):
    """Force an exception during file scanning; verify no RuntimeWarning and graceful cleanup."""
    # Create directory structure: root with a subdir that will raise when scanned
    (temp_dir / "normal").mkdir()
    (temp_dir / "normal" / "file.txt").write_text("content")
    (temp_dir / "raise_here").mkdir()
    (temp_dir / "raise_here" / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=False,
    )

    async def _scandir_wrapper(path, executor=None, batch_size=5000):
        """Raise OSError when scanning 'raise_here'; delegate to real impl otherwise."""
        if path == temp_dir / "raise_here":
            raise OSError("Simulated scan failure")
        from efspurge.purger import async_scandir_batched as real_scandir

        async for batch in real_scandir(path, executor, batch_size):
            yield batch

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with patch("efspurge.purger.async_scandir_batched", _scandir_wrapper):
            await purger._scan_and_purge_files()

    # No RuntimeWarning about unawaited coroutines
    unawaited_warnings = [x for x in w if "was never awaited" in str(x.message)]
    assert len(unawaited_warnings) == 0, f"Unexpected RuntimeWarnings: {unawaited_warnings}"

    # Errors counter incremented
    assert purger.stats["errors"] >= 1

    # active_directories is empty after completion
    assert len(purger.active_directories) == 0


# ---------------------------------------------------------------------------
# test_phase1b_timeout_skips_stuck_dirs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1b_timeout_skips_stuck_dirs(temp_dir):
    """Test Phase 1b timeout: stuck rmdir operations are skipped, non-stuck are deleted."""
    # Create 5 empty directories
    for i in range(5):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
    )

    # Track which dirs we make "stuck"
    stuck_dirs = {temp_dir / "empty_1", temp_dir / "empty_3"}
    original_rmdir = None

    async def mock_rmdir(path):
        if Path(path) in stuck_dirs:
            await asyncio.sleep(999)  # Hang indefinitely
        else:
            await original_rmdir(path)

    # Patch aiofiles.os.rmdir and asyncio.wait timeout
    import aiofiles.os as aiofiles_os

    original_rmdir = aiofiles_os.rmdir

    _real_wait = asyncio.wait

    async def short_timeout_wait(fs, *, timeout=None):
        return await _real_wait(fs, timeout=0.5)

    with (
        patch.object(aiofiles_os, "rmdir", side_effect=mock_rmdir),
        patch("asyncio.wait", side_effect=short_timeout_wait),
    ):
        deleted = await purger._purge_empty_directories_standalone()

    # Non-stuck directories (empty_0, empty_2, empty_4) should be deleted
    assert deleted >= 3
    assert not (temp_dir / "empty_0").exists()
    assert not (temp_dir / "empty_2").exists()
    assert not (temp_dir / "empty_4").exists()

    # Errors logged for timed-out operations
    assert purger.stats["errors"] >= 2


# ---------------------------------------------------------------------------
# test_permission_denied_on_directory_during_scan
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="chmod not applicable on Windows")
@pytest.mark.asyncio
async def test_permission_denied_on_directory_during_scan(temp_dir):
    """Test BFS handles unreadable directories during scan."""
    (temp_dir / "accessible").mkdir()
    (temp_dir / "accessible" / "file.txt").write_text("content")
    (temp_dir / "restricted").mkdir()
    (temp_dir / "restricted" / "file.txt").write_text("content")

    # Make restricted directory unreadable
    os.chmod(temp_dir / "restricted", 0o000)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=False,
    )

    try:
        await purger._scan_and_purge_files()

        # Errors counter incremented for unreadable dir
        assert purger.stats["errors"] >= 1

        # Accessible directory was still scanned
        assert purger.stats["files_scanned"] >= 1
    finally:
        # Restore permissions for cleanup
        os.chmod(temp_dir / "restricted", 0o700)


# ---------------------------------------------------------------------------
# test_max_concurrent_subdirs_rejected
# ---------------------------------------------------------------------------


def test_max_concurrent_subdirs_rejected():
    """Test that removed max_concurrent_subdirs parameter raises TypeError."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        AsyncEFSPurger(
            root_path="/tmp",
            max_age_days=30,
            max_concurrent_subdirs=100,
        )


# ---------------------------------------------------------------------------
# test_gc_collect_called_during_stuck_detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gc_collect_called_during_stuck_detection(temp_dir):
    """Test that gc.collect() is triggered during stuck detection in progress reporter."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
    )
    purger.current_phase = "scanning"
    purger.progress_interval = 0.05  # Short interval for fast test

    with patch("efspurge.purger.gc.collect") as mock_gc:
        reporter_task = asyncio.create_task(purger._background_progress_reporter())
        try:
            # Run long enough for 2+ progress checks with no progress (stats stay at 0)
            await asyncio.sleep(0.25)
        finally:
            reporter_task.cancel()
            try:
                await reporter_task
            except asyncio.CancelledError:
                pass

        assert mock_gc.called, "gc.collect should be called when stuck detection triggers (stuck_detection_count >= 2)"
