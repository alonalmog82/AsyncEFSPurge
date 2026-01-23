"""Tests for memory safety during large-scale empty directory deletion.

These tests verify that empty directory deletion doesn't cause memory explosions
even with very large numbers of empty directories (100k+).
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path

import psutil
import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_large_scale_empty_dir_deletion_memory_bounded(temp_dir):
    """Test that deleting many empty directories doesn't cause memory explosion.

    This test verifies the fix for memory explosion when deleting 100k+ empty directories.
    Before the fix, memory could grow from ~250MB to 1500MB+.
    After the fix, memory should stay bounded even with large numbers of empty dirs.
    """
    # Create a large number of empty directories (10k for CI, but tests the same code path)
    # For production testing, use 100k+ empty directories
    num_dirs = 10000  # 10k dirs - enough to test batching and memory safety

    print(f"\nCreating {num_dirs} empty directories...")
    start_create = time.time()
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:06d}").mkdir()
    create_time = time.time() - start_create
    print(f"Created {num_dirs} directories in {create_time:.2f}s")

    # Use high concurrency to test batch size limits
    # With max_concurrency_deletion=4000, batch size should be capped at 200-400
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=4000,  # High concurrency
        max_concurrent_subdirs=4000,
        memory_limit_mb=800,  # Set memory limit
        max_empty_dirs_to_delete=0,  # Unlimited for this test
        dry_run=False,
    )

    # Get initial memory
    process = psutil.Process(os.getpid())
    initial_memory = process.memory_info().rss / 1024 / 1024  # MB

    print(f"Initial memory: {initial_memory:.1f}MB")

    # Scan directories
    await purger.scan_directory(temp_dir)

    # Get memory after scanning
    memory_after_scan = process.memory_info().rss / 1024 / 1024
    print(f"Memory after scan: {memory_after_scan:.1f}MB")

    # Delete empty directories and monitor memory
    deletion_start = time.time()
    peak_memory = memory_after_scan
    memory_samples = []

    # Monitor memory during deletion
    async def monitor_memory():
        nonlocal peak_memory
        while True:
            current_memory = process.memory_info().rss / 1024 / 1024
            peak_memory = max(peak_memory, current_memory)
            memory_samples.append(current_memory)
            await asyncio.sleep(0.1)  # Sample every 100ms

    monitor_task = asyncio.create_task(monitor_memory())

    try:
        await purger._remove_empty_directories()
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    deletion_time = time.time() - deletion_start
    final_memory = process.memory_info().rss / 1024 / 1024

    print(f"Memory after deletion: {final_memory:.1f}MB")
    print(f"Peak memory during deletion: {peak_memory:.1f}MB")
    print(f"Deletion took: {deletion_time:.2f}s")
    print(f"Memory increase: {peak_memory - initial_memory:.1f}MB")

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Memory should stay bounded - the original bug showed 250MB -> 1559MB (1309MB increase)
    # With the fix (smaller batches, incremental processing), increase should be much smaller
    # We expect < 300MB increase even with 10k directories (much better than 1309MB)
    memory_increase = peak_memory - initial_memory
    assert memory_increase < 300, (
        f"Memory increase ({memory_increase:.1f}MB) should be bounded. "
        f"Original bug showed 1309MB increase. Peak: {peak_memory:.1f}MB, Initial: {initial_memory:.1f}MB"
    )

    # Peak memory should not exceed memory limit significantly
    # Allow some overhead (150% of limit) for safety
    assert peak_memory < purger.memory_limit_mb * 1.5, (
        f"Peak memory ({peak_memory:.1f}MB) exceeded memory limit ({purger.memory_limit_mb}MB) by too much"
    )


@pytest.mark.asyncio
async def test_empty_dir_deletion_batch_sizes(temp_dir):
    """Test that batch sizes are properly limited during empty directory deletion.

    This test verifies that with high concurrency (4000), batch sizes are capped
    to prevent memory explosion. We verify this by checking memory stays bounded
    even with many directories.
    """
    # Create enough directories to require multiple batches
    # With max_concurrency_deletion=4000, batch size should be capped at 200
    # So 5000 dirs should require at least 25 batches (5000 / 200)
    num_dirs = 5000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    # Use high concurrency to test batch size capping
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=4000,  # High concurrency - should cap batch at 200
        max_empty_dirs_to_delete=0,  # Unlimited for this test
        memory_limit_mb=800,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Monitor memory to verify batching prevents explosion
    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    start_time = time.time()
    await purger._remove_empty_directories()
    deletion_time = time.time() - start_time

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Memory increase should be small if batching is working correctly
    # Without batching, we'd see a large spike. With proper batching, memory should stay bounded
    assert memory_increase < 200, (
        f"Memory increase ({memory_increase:.1f}MB) suggests batching isn't working. "
        f"Expected small increase with proper batch size limits."
    )

    # Should complete in reasonable time
    assert deletion_time < 30, f"Deletion should complete in reasonable time, took {deletion_time:.2f}s"


@pytest.mark.asyncio
async def test_empty_dir_deletion_memory_pressure_checks(temp_dir):
    """Test that memory pressure checks are triggered during empty directory deletion.

    Memory checks are called every 1000 directories, so with 5000 directories,
    check_memory_pressure() should be called at least 5 times.
    """
    # Create many empty directories - enough to trigger multiple memory checks
    # Memory checks happen every 1000 directories, so 5000 dirs = 5 checks
    num_dirs = 5000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    # Set a low memory limit to increase chance of back-pressure
    # But we're mainly testing that checks are called, not that back-pressure triggers
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=200,  # Low limit
        max_empty_dirs_to_delete=0,  # Unlimited for this test
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Mock check_memory_pressure to track calls
    check_calls = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        check_calls.append(time.time())
        return await original_check()

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    # Verify deletion completed successfully
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Memory checks should have been called at least once per 1000 directories
    # With 5000 dirs, we expect at least 5 calls (every 1000 dirs)
    expected_min_calls = num_dirs // 1000
    assert len(check_calls) >= expected_min_calls, (
        f"Memory checks should be called at least {expected_min_calls} times "
        f"(once per 1000 directories), but was called {len(check_calls)} times"
    )


@pytest.mark.asyncio
async def test_cascading_deletion_memory_bounded(temp_dir):
    """Test that cascading deletion doesn't cause memory explosion."""
    # Create deeply nested empty directory structure
    # This creates many parents that need cascading deletion
    depth = 10
    num_branches = 100  # 100 branches at each level

    print(f"\nCreating nested structure: {depth} levels, {num_branches} branches...")
    for branch in range(num_branches):
        current_path = temp_dir
        for level in range(depth):
            current_path = current_path / f"branch_{branch:03d}" / f"level_{level:02d}"
            current_path.mkdir(parents=True)

    # Count actual directories created (including all parent directories)
    created = sum(1 for _ in temp_dir.rglob("*") if _.is_dir())
    print(f"Created {created} directories")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,  # Unlimited for this test
        dry_run=False,
    )

    process = psutil.Process(os.getpid())
    initial_memory = process.memory_info().rss / 1024 / 1024

    await purger.scan_directory(temp_dir)
    memory_after_scan = process.memory_info().rss / 1024 / 1024

    # Delete empty directories
    await purger._remove_empty_directories()

    final_memory = process.memory_info().rss / 1024 / 1024
    peak_memory = max(initial_memory, memory_after_scan, final_memory)

    print(
        f"Memory: Initial={initial_memory:.1f}MB, After scan={memory_after_scan:.1f}MB, "
        f"Final={final_memory:.1f}MB, Peak={peak_memory:.1f}MB"
    )

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == created, (
        f"Expected {created} directories deleted, got {purger.stats['empty_dirs_deleted']}"
    )

    # Memory should stay bounded during cascading deletion
    # Cascading deletion processes parents in batches of max 10k per iteration
    # Memory increase should be reasonable even with deeply nested structures
    memory_increase = peak_memory - initial_memory
    assert memory_increase < 300, (
        f"Memory increase ({memory_increase:.1f}MB) should be bounded during cascading deletion. "
        f"Peak: {peak_memory:.1f}MB, Initial: {initial_memory:.1f}MB"
    )
