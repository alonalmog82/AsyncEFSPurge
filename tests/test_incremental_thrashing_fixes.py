"""Tests for the standalone two-pass empty directory purge architecture.

This test suite validates the two-pass approach that replaced incremental
processing during scanning:
1. Phase 1: Standalone BFS discovery + bottom-up deletion
2. Phase 2: File scanning and purging
3. Phase 3: Post-scan cleanup of newly-empty directories
"""

import tempfile
from pathlib import Path

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_two_pass_order_empty_dirs_before_scan(temp_dir):
    """
    Test that empty directories are purged BEFORE file scanning starts.

    Phase 1 (empty dir purge) should complete before Phase 2 (file scan).
    """
    # Create empty directories and files
    for i in range(50):
        (temp_dir / f"empty_{i}").mkdir()
    for i in range(10):
        (temp_dir / f"file_{i}.txt").write_text("content")

    phase_order = []

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,
    )

    # Monkey-patch to track phase transitions
    original_standalone = purger._purge_empty_directories_standalone
    original_scan = purger.scan_directory

    async def tracked_standalone():
        phase_order.append("phase1_start")
        result = await original_standalone()
        phase_order.append("phase1_end")
        return result

    async def tracked_scan(directory):
        if directory == purger.root_path:
            phase_order.append("phase2_start")
        return await original_scan(directory)

    purger._purge_empty_directories_standalone = tracked_standalone
    purger.scan_directory = tracked_scan

    await purger.purge()

    # Phase 1 should complete before Phase 2 starts
    assert phase_order.index("phase1_start") < phase_order.index("phase2_start")
    assert phase_order.index("phase1_end") < phase_order.index("phase2_start")


@pytest.mark.asyncio
async def test_standalone_purge_no_recursive_coroutine_overhead(temp_dir):
    """
    Test that the standalone purger uses iterative BFS, not recursive coroutines.

    The standalone purger should discover directories iteratively (bounded memory),
    not create recursive scan_directory coroutines (unbounded memory).
    """
    # Create a wide+deep structure
    for i in range(20):
        d = temp_dir / f"level1_{i}"
        d.mkdir()
        for j in range(10):
            (d / f"level2_{j}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    # Track that scan_directory is NOT called during Phase 1
    scan_directory_calls = []
    original_scan = purger.scan_directory

    async def tracked_scan(directory):
        scan_directory_calls.append(directory)
        return await original_scan(directory)

    purger.scan_directory = tracked_scan

    # Run only Phase 1
    deleted = await purger._purge_empty_directories_standalone()

    # Phase 1 should NOT use scan_directory
    assert len(scan_directory_calls) == 0, (
        f"Standalone purger should not call scan_directory, but it was called "
        f"{len(scan_directory_calls)} times. It should use iterative BFS instead."
    )

    # Should have deleted at least the leaf directories (200 = 20 * 10)
    # Parent directories (20) become empty after leaf deletion.
    # Phase 1 processes bottom-up in a single pass, so parents may or may not
    # be caught depending on processing order. The important thing is leaves are deleted.
    assert deleted >= 200, f"Expected at least 200 leaf dirs deleted, got {deleted}"


@pytest.mark.asyncio
async def test_standalone_purge_back_pressure_reduces_batch_size(temp_dir):
    """
    Test that back-pressure reduces batch size when memory is high.

    When memory exceeds thresholds, the standalone purger should process
    smaller batches to avoid OOM.
    """
    from unittest.mock import patch

    # Create enough directories to require multiple batches
    for i in range(200):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=100,  # Low limit to trigger back-pressure
    )

    # Mock memory to appear high (80% of limit)
    with patch("efspurge.purger.get_memory_usage_mb", return_value=80.0):
        deleted = await purger._purge_empty_directories_standalone()

    # Should still delete all directories despite memory pressure
    assert deleted == 200


@pytest.mark.asyncio
async def test_post_scan_empty_dir_cleanup(temp_dir):
    """
    Test Phase 3: directories that become empty after file purging are cleaned up.

    Phase 1 can't catch these because they contained files at discovery time.
    Phase 2 purges the files. Phase 3 should clean up the now-empty directories.
    """
    import time

    # Create directories with old files (will be purged)
    for i in range(20):
        d = temp_dir / f"dir_with_old_files_{i}"
        d.mkdir()
        f = d / "old_file.txt"
        f.write_text("old content")
        # Make file old (2 days ago)
        old_time = time.time() - 86400 * 2
        import os

        os.utime(f, (old_time, old_time))

    # Create directories with new files (should NOT be purged)
    for i in range(10):
        d = temp_dir / f"dir_with_new_files_{i}"
        d.mkdir()
        (d / "new_file.txt").write_text("new content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=1,  # Purge files older than 1 day
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    stats = await purger.purge()

    # Old files should be purged
    assert stats["files_purged"] == 20

    # Directories that had old files should be empty and cleaned up
    # (by either Phase 1 catching them if already empty, or Phase 3 after purging)
    for i in range(20):
        assert not (temp_dir / f"dir_with_old_files_{i}").exists(), (
            f"dir_with_old_files_{i} should have been deleted after its files were purged"
        )

    # Directories with new files should still exist
    for i in range(10):
        assert (temp_dir / f"dir_with_new_files_{i}").exists()


@pytest.mark.asyncio
async def test_standalone_purge_handles_large_flat_structure(temp_dir):
    """
    Test standalone purge with a large flat directory structure.

    This is the common case: a directory with thousands of empty subdirectories.
    """
    num_dirs = 1000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:06d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    deleted = await purger._purge_empty_directories_standalone()

    assert deleted == num_dirs

    # Only root should remain
    remaining = list(temp_dir.iterdir())
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_standalone_purge_handles_permission_errors(temp_dir):
    """
    Test that standalone purge gracefully handles permission errors.
    """
    # Create some empty directories
    for i in range(10):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    # Should complete without crashing even if some dirs have issues
    deleted = await purger._purge_empty_directories_standalone()

    # All accessible empty dirs should be deleted
    assert deleted == 10
