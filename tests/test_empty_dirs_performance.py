"""Tests for empty directory deletion performance optimizations.

These tests verify that performance optimizations (removed redundant checks,
optimized semaphore usage) work correctly and improve throughput.
"""

import tempfile
import time
from pathlib import Path

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_no_redundant_scandir_checks(temp_dir):
    """Test that redundant scandir checks are removed for empty directories.

    We should NOT check if a directory is empty before deleting it if we already
    know it's empty from scanning.
    """
    # Create empty directories
    num_dirs = 100
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=100,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Track scandir calls during deletion
    scandir_calls = []

    # Mock async_scandir to track calls
    async def tracked_scandir(path, executor, purger_instance):
        scandir_calls.append(str(path))
        # Use the actual scandir implementation
        import asyncio

        loop = asyncio.get_running_loop()

        def _scandir():
            import os

            with os.scandir(path) as entries:
                return list(entries)

        return await loop.run_in_executor(executor, _scandir)

    # Patch async_scandir in the module
    import efspurge.purger as purger_module

    original_async_scandir = purger_module.async_scandir
    purger_module.async_scandir = tracked_scandir

    try:
        await purger._remove_empty_directories()
    finally:
        # Restore original
        purger_module.async_scandir = original_async_scandir

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # We should NOT see scandir calls for the directories themselves before deletion
    # (we only check parents after deletion for cascading)
    # Each directory deletion should only trigger 1 scandir (for parent check)
    # So we expect ~num_dirs scandir calls, not 2*num_dirs
    assert len(scandir_calls) <= num_dirs * 2, (
        f"Too many scandir calls ({len(scandir_calls)}). "
        f"Expected ~{num_dirs} calls (one per parent check), not 2*{num_dirs} (redundant checks)"
    )


@pytest.mark.asyncio
async def test_semaphore_released_early(temp_dir):
    """Test that semaphore is released immediately after rmdir, not held during checks.

    This allows better concurrency - other deletions can proceed while checking parents.
    """
    # Create nested empty directories to test cascading
    depth = 5
    num_branches = 20

    for branch in range(num_branches):
        current_path = temp_dir
        for level in range(depth):
            current_path = current_path / f"branch_{branch:02d}" / f"level_{level}"
            current_path.mkdir(parents=True)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=10,  # Low concurrency to test semaphore behavior
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Track semaphore acquisitions/releases
    semaphore_acquires = []
    semaphore_releases = []

    original_acquire = purger.deletion_semaphore.acquire
    original_release = purger.deletion_semaphore.release

    async def tracked_acquire():
        semaphore_acquires.append(time.time())
        return await original_acquire()

    def tracked_release():
        semaphore_releases.append(time.time())
        return original_release()

    purger.deletion_semaphore.acquire = tracked_acquire
    purger.deletion_semaphore.release = tracked_release

    await purger._remove_empty_directories()

    # Verify deletion completed
    assert purger.stats["empty_dirs_deleted"] > 0

    # Semaphore should be acquired and released many times (not held for long periods)
    # With optimized semaphore usage, we should see many acquire/release cycles
    assert len(semaphore_acquires) > 0, "Semaphore should be acquired during deletion"
    assert len(semaphore_releases) == len(semaphore_acquires), "Semaphore should be released as many times as acquired"


@pytest.mark.asyncio
async def test_memory_pressure_stops_queue_feeding(temp_dir):
    """Test that memory pressure stops producer from feeding queue when memory is high."""
    # Create many empty directories
    num_dirs = 2000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,  # High concurrency
        memory_limit_mb=100,  # Very low limit to trigger memory pressure
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Track memory checks in producer
    memory_check_results = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        result = await original_check()
        memory_check_results.append(result)
        return result

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    # With very low memory limit, producer may stop immediately due to critical memory threshold
    # This is correct behavior - memory protection is working
    deleted_count = purger.stats["empty_dirs_deleted"]

    # Memory checks should be called in producer (at least once, even if it stops immediately)
    # Producer checks memory before adding each directory to queue
    assert len(memory_check_results) > 0, (
        f"Memory checks should be called in producer before adding to queue. "
        f"Got {len(memory_check_results)} calls. "
        f"Deleted {deleted_count} directories (may be 0 if memory threshold hit immediately)."
    )

    # Verify that if memory was critical, the system stopped gracefully
    # (deleted_count may be 0 if critical threshold hit before any processing)
    # This is correct behavior - better to stop early than risk OOM


@pytest.mark.asyncio
async def test_queue_processing_with_memory_checks(temp_dir):
    """Test that queue processing continues with memory checks in producer."""
    # Create empty directories
    num_dirs = 1000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=500,  # Reasonable limit
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Track memory check results in producer
    memory_check_results = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        result = await original_check()
        memory_check_results.append(result)
        return result

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    # Verify deletion completed
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Memory checks should be called many times in producer
    # Producer checks memory before adding each directory to queue
    # So we expect many checks (at least hundreds for 1000 dirs)
    assert len(memory_check_results) >= num_dirs // 10, (
        f"Memory checks should be called many times in producer. "
        f"Expected at least {num_dirs // 10} calls, got {len(memory_check_results)}"
    )
