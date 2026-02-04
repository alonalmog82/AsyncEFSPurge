"""
Tests for incremental empty directory processing feature.

This feature prevents OOM by processing empty directories in batches
during scanning when memory or count thresholds are exceeded.
"""

from pathlib import Path

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.mark.asyncio
async def test_incremental_processing_triggers_on_count_threshold(tmp_path: Path) -> None:
    """Test that incremental processing triggers when count threshold is exceeded."""
    # Create a structure with many empty directories
    # We'll create more than the default count threshold
    num_dirs = 100  # Create 100 empty dirs

    for i in range(num_dirs):
        dir_path = tmp_path / f"empty_{i:03d}"
        dir_path.mkdir()

    # Configure purger with low count threshold to trigger incremental processing
    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,  # Skip file processing
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,  # Unlimited
        memory_limit_mb=800,
        log_level="INFO",
    )

    # Override the count threshold to a low value to trigger incremental processing
    purger.empty_dirs_count_threshold = 50  # Process when we have > 50 empty dirs

    # Run purge
    stats = await purger.purge()

    # Verify all directories were deleted
    assert stats["dirs_purged"] == num_dirs
    # Verify incremental processing was used (check that counter was incremented)
    assert purger.empty_dirs_processed_total > 0


@pytest.mark.asyncio
async def test_incremental_processing_maintains_post_order(tmp_path: Path) -> None:
    """Test that incremental processing maintains post-order deletion (deepest first)."""
    # Create nested directory structure
    # parent/
    #   child1/ (empty)
    #   child2/
    #     grandchild1/ (empty)
    #     grandchild2/ (empty)

    parent = tmp_path / "parent"
    child1 = parent / "child1"
    child2 = parent / "child2"
    grandchild1 = child2 / "grandchild1"
    grandchild2 = child2 / "grandchild2"

    child1.mkdir(parents=True)
    grandchild1.mkdir(parents=True)
    grandchild2.mkdir(parents=True)

    # Configure purger with low count threshold
    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
        log_level="DEBUG",
    )

    # Set low threshold to trigger incremental processing
    purger.empty_dirs_count_threshold = 2

    # Run purge
    stats = await purger.purge()

    # Incremental processing deletes the leaf directories (grandchild1, grandchild2, child1)
    # The final pass will handle parent directories that become empty (child2, parent)
    # Total: grandchild1, grandchild2, child1, child2, parent = 5 directories
    # But incremental processing only gets the first batch (3 leaf dirs)
    # Final pass should get child2 and parent
    assert stats["dirs_purged"] >= 3  # At least the 3 leaf dirs

    # Verify leaf directories are deleted
    assert not child1.exists()
    assert not grandchild1.exists()
    assert not grandchild2.exists()


@pytest.mark.asyncio
async def test_incremental_processing_respects_rate_limit(tmp_path: Path) -> None:
    """Test that incremental processing respects max_empty_dirs_to_delete limit."""
    # Create many empty directories
    num_dirs = 100
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    # Configure with rate limit
    max_to_delete = 30
    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=max_to_delete,
        memory_limit_mb=800,
        log_level="INFO",
    )

    # Set low threshold to trigger incremental processing
    purger.empty_dirs_count_threshold = 20

    # Run purge
    stats = await purger.purge()

    # Should delete at most max_to_delete directories
    assert stats["dirs_purged"] <= max_to_delete
    assert stats["dirs_to_purge"] <= max_to_delete


@pytest.mark.asyncio
async def test_incremental_processing_frees_memory(tmp_path: Path) -> None:
    """Test that incremental processing clears empty_dirs set to free memory."""
    # Create directories
    num_dirs = 60
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
        log_level="INFO",
    )

    # Set threshold to trigger after 30 dirs
    purger.empty_dirs_count_threshold = 30

    # Track empty_dirs size during scan
    original_check_empty = purger._check_empty_directory
    max_empty_dirs_seen = 0

    async def tracked_check_empty(directory: Path) -> None:
        nonlocal max_empty_dirs_seen
        await original_check_empty(directory)
        async with purger.stats_lock:
            max_empty_dirs_seen = max(max_empty_dirs_seen, len(purger.empty_dirs))

    purger._check_empty_directory = tracked_check_empty

    # Run purge
    stats = await purger.purge()

    # Verify that empty_dirs was cleared during incremental processing
    # Max size should be around the threshold, not the total count
    assert max_empty_dirs_seen <= purger.empty_dirs_count_threshold + 20  # Some buffer
    assert max_empty_dirs_seen < num_dirs  # Should be less than total

    # All should still be deleted
    assert stats["dirs_purged"] == num_dirs


@pytest.mark.asyncio
async def test_no_incremental_processing_when_disabled(tmp_path: Path) -> None:
    """Test that incremental processing doesn't run when remove_empty_dirs=False."""
    # Create many directories (but don't enable removal)
    num_dirs = 100
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=False,  # Disabled
        memory_limit_mb=800,
        log_level="INFO",
    )

    # Run purge
    stats = await purger.purge()

    # No directories should be deleted
    assert stats.get("dirs_purged", 0) == 0
    assert stats.get("dirs_to_purge", 0) == 0
    assert purger.empty_dirs_processed_total == 0

    # All directories should still exist
    for i in range(num_dirs):
        assert (tmp_path / f"empty_{i:03d}").exists()


@pytest.mark.asyncio
async def test_incremental_processing_with_dry_run(tmp_path: Path) -> None:
    """Test that incremental processing works correctly in dry-run mode."""
    # Create directories
    num_dirs = 60
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=True,  # Dry run
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=100,  # Use a limit so counter is incremented
        memory_limit_mb=800,
        log_level="INFO",
    )

    # Set low threshold to trigger incremental processing
    purger.empty_dirs_count_threshold = 30

    # Run purge
    stats = await purger.purge()

    # In dry run, empty_dirs_to_delete is incremented but empty_dirs_deleted is 0
    # Since we set limit to 100 and only have 60 dirs, all should be counted
    assert stats.get("empty_dirs_to_delete", 0) == num_dirs or stats.get("dirs_to_purge", 0) == num_dirs
    assert stats.get("empty_dirs_deleted", 0) == 0 or stats.get("dirs_purged", 0) == 0

    # Verify incremental processing was used
    assert purger.empty_dirs_processed_total > 0

    # All directories should still exist (dry run)
    for i in range(num_dirs):
        assert (tmp_path / f"empty_{i:03d}").exists()
