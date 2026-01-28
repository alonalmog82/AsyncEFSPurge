"""Tests for memory safety during empty directory deletion."""

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
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_large_scale_empty_dir_deletion_memory_bounded(temp_dir):
    """
    Test that memory stays bounded when deleting large numbers of empty directories.

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

    # Use high concurrency to test queue+semaphore memory bounds
    # With queue+semaphore, memory is bounded by semaphore_limit, not total dirs
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,  # High concurrency
        max_concurrent_subdirs=1000,
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
    # With queue+semaphore approach, memory is bounded by semaphore_limit * memory_per_task
    # We expect < 300MB increase even with 10k directories (much better than 1309MB)
    memory_increase = peak_memory - initial_memory
    assert memory_increase < 300, (
        f"Memory increase ({memory_increase:.1f}MB) should be bounded. "
        f"Original bug showed 1309MB increase. Peak: {peak_memory:.1f}MB, Initial: {initial_memory:.1f}MB. "
        f"With queue+semaphore, memory should be bounded by semaphore limit."
    )

    # Peak memory should not exceed memory limit significantly
    # Allow some overhead (150% of limit) for safety
    assert peak_memory < purger.memory_limit_mb * 1.5, (
        f"Peak memory ({peak_memory:.1f}MB) exceeded memory limit ({purger.memory_limit_mb}MB) by too much"
    )


@pytest.mark.asyncio
async def test_empty_dir_deletion_queue_memory_bounded(temp_dir):
    """
    Test that queue-based deletion keeps memory bounded.

    This test verifies that with high concurrency, the queue+semaphore approach
    keeps memory bounded by semaphore limit, not by total directories.
    Memory should stay bounded even with many directories.
    """
    # Create many directories to test queue-based processing
    # With queue+semaphore, memory is bounded by semaphore limit, not total dirs
    num_dirs = 5000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    # Use high concurrency - queue size = semaphore_limit + 100
    # Memory should be bounded by semaphore_limit * memory_per_task
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,  # High concurrency
        max_empty_dirs_to_delete=0,  # Unlimited for this test
        memory_limit_mb=800,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Monitor memory to verify queue approach prevents explosion
    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    start_time = time.time()
    await purger._remove_empty_directories()
    deletion_time = time.time() - start_time

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Memory increase should be small with queue+semaphore approach
    # Memory is bounded by semaphore_limit, not total directories
    assert memory_increase < 200, (
        f"Memory increase ({memory_increase:.1f}MB) suggests queue approach isn't working. "
        f"Expected small increase with queue+semaphore pattern (memory bounded by semaphore limit)."
    )

    assert deletion_time < 30, f"Deletion should complete in reasonable time, took {deletion_time:.2f}s"


@pytest.mark.asyncio
async def test_empty_dir_deletion_memory_pressure_checks(temp_dir):
    """Test that memory pressure checks are triggered during empty directory deletion.

    With queue+semaphore approach, memory checks happen in the producer before adding
    directories to the queue. This prevents memory growth by stopping queue feeding
    when memory is high.
    """
    # Create many empty directories - enough to trigger multiple memory checks
    # Producer checks memory before adding each directory to queue
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

    # Mock check_memory_pressure to track calls and return values
    check_calls = []
    check_results = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        result = await original_check()
        check_calls.append(time.time())
        check_results.append(result)
        return result

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    # Verify deletion completed successfully
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Memory checks should be called in producer before adding directories to queue
    # Producer checks memory before each directory, so we expect many checks
    # (at least one per directory, but may be fewer if rate limit or memory stops it early)
    # With 5000 dirs, we expect at least 1000+ checks (producer checks before each add)
    expected_min_calls = min(num_dirs // 5, 1000)  # Conservative estimate
    assert len(check_calls) >= expected_min_calls, (
        f"Memory checks should be called many times in producer "
        f"(before adding directories to queue), but was called {len(check_calls)} times"
    )

    # Verify check_memory_pressure returns tuple (bool, float)
    assert all(isinstance(result, tuple) and len(result) == 2 for result in check_results), (
        "check_memory_pressure should return tuple (bool, float)"
    )
    assert all(isinstance(result[0], bool) and isinstance(result[1], (int, float)) for result in check_results), (
        "check_memory_pressure should return tuple (bool, float)"
    )


@pytest.mark.asyncio
async def test_memory_checks_in_producer(temp_dir):
    """
    Test that memory checks happen in producer before adding to queue.

    With queue+semaphore approach, memory checks happen in the producer
    before adding directories to the queue. This prevents memory growth
    by stopping queue feeding when memory is high.
    """
    # Create enough directories to test producer memory checks
    num_dirs = 2000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=500,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Track memory check calls
    check_count = [0]
    check_timings = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        check_count[0] += 1
        check_timings.append(time.time())
        return await original_check()

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    # Verify deletion completed
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Verify memory checks happened in producer
    # Producer checks memory before adding each directory to queue
    # So we expect many checks (at least hundreds for 2000 dirs)
    assert check_count[0] > 100, (
        f"Memory checks should be called many times in producer "
        f"(before adding directories to queue), but was called {check_count[0]} times"
    )

    # Verify checks happen throughout the process (not just at start)
    # Check timings should span the deletion process
    if len(check_timings) > 1:
        check_duration = check_timings[-1] - check_timings[0]
        assert check_duration > 0, "Memory checks should happen throughout the deletion process"


@pytest.mark.asyncio
async def test_cascading_deletion_memory_bounded(temp_dir):
    """Test that cascading deletion doesn't cause memory explosion."""
    # Create deeply nested empty directory structure
    # This tests cascading deletion which can cause memory spikes
    depth = 5
    width = 10  # 10^5 = 100k directories (but we'll create less for CI)

    def create_nested(base, current_depth):
        if current_depth >= depth:
            return
        for i in range(width):
            subdir = base / f"dir_{i}"
            subdir.mkdir()
            create_nested(subdir, current_depth + 1)

    create_nested(temp_dir, 0)

    # Count total directories
    total_dirs = sum(1 for _ in temp_dir.rglob("*") if _.is_dir())
    print(f"Created {total_dirs} nested directories")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Monitor memory during cascading deletion
    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    await purger._remove_empty_directories()

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == total_dirs

    # Memory increase should be bounded even with cascading deletion
    assert memory_increase < 300, (
        f"Cascading deletion caused memory increase of {memory_increase:.1f}MB, which exceeds expected bound of 300MB"
    )
