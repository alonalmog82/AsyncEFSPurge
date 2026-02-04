"""Tests for incremental empty directory processing race condition and thrashing fixes.

This test suite validates the fixes for issues identified in production:
1. Race conditions causing multiple concurrent batch processing
2. Lack of debouncing leading to trigger spam
3. Micro-batches (1-3 dirs) causing inefficiency
4. Incorrect logging showing cumulative instead of batch-specific counts
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_only_one_batch_processes_at_a_time(temp_dir):
    """
    Test Fix #1: Verify that only one batch processing operation runs at a time.

    This prevents the race condition where multiple concurrent scan tasks
    all trigger batch processing simultaneously when hitting the memory threshold.
    """
    # Create many empty directories to trigger processing
    empty_dirs = []
    for i in range(200):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()
        empty_dirs.append(empty_dir)

    # Use low thresholds to trigger processing easily
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        memory_limit_mb=100,  # Low limit to trigger threshold easily
        max_concurrent_subdirs=20,  # High concurrency to stress test
    )

    # Lower the threshold to make it trigger more easily
    purger.empty_dirs_count_threshold = 50

    # Track concurrent batch processing calls
    processing_count = {"current": 0, "max_concurrent": 0}
    processing_lock = asyncio.Lock()

    original_process = purger._process_empty_dirs_batch

    async def monitored_process():
        """Wrap the batch processing to track concurrent calls."""
        async with processing_lock:
            processing_count["current"] += 1
            processing_count["max_concurrent"] = max(
                processing_count["max_concurrent"], processing_count["current"]
            )

        try:
            await original_process()
        finally:
            async with processing_lock:
                processing_count["current"] -= 1

    purger._process_empty_dirs_batch = monitored_process

    # Scan with high concurrency - this should trigger multiple threshold checks
    await purger.scan_directory(temp_dir)

    # Verify that we never had more than 1 concurrent batch processing
    assert processing_count["max_concurrent"] <= 1, (
        f"Expected max 1 concurrent batch processing, got {processing_count['max_concurrent']}. "
        "This indicates the lock is not preventing concurrent processing."
    )


@pytest.mark.asyncio
async def test_debouncing_reduces_trigger_spam(temp_dir):
    """
    Test Fix #2: Verify that trigger checks are debounced under high concurrency.

    This prevents the log spam where 20 concurrent tasks all log
    "Incremental empty directory processing triggered" at the same timestamp.
    """
    # Create enough empty directories to exceed threshold
    for i in range(150):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        memory_limit_mb=100,
        max_concurrent_subdirs=20,
    )

    purger.empty_dirs_count_threshold = 50
    purger.empty_dirs_min_batch_size = 40  # Low enough to trigger

    # Track how many times the trigger check runs
    trigger_check_count = {"count": 0}
    trigger_log_count = {"count": 0}

    original_should_process = purger._should_process_empty_dirs_incrementally

    async def monitored_should_process(log_trigger=False):
        """Wrap the check to count invocations."""
        trigger_check_count["count"] += 1
        if log_trigger:
            trigger_log_count["count"] += 1
        return await original_should_process(log_trigger=log_trigger)

    purger._should_process_empty_dirs_incrementally = monitored_should_process

    # Scan with high concurrency
    await purger.scan_directory(temp_dir)

    # We expect some checks (at least one per triggered processing)
    # But far fewer logs than directories
    assert trigger_check_count["count"] > 0, "Expected at least some threshold checks"
    assert trigger_log_count["count"] <= 10, (
        f"Expected very few trigger logs due to debouncing, got {trigger_log_count['count']}. "
        "This indicates excessive log spam is not being prevented."
    )


@pytest.mark.asyncio
async def test_minimum_batch_size_prevents_micro_batches(temp_dir):
    """
    Test Fix #3: Verify that tiny batches (< min_batch_size) are not processed.

    This prevents the thrashing where batches of 1-3 directories are processed,
    causing inefficient I/O operations and overhead.
    """
    # Create a small number of empty directories (less than min_batch_size)
    num_dirs = 50  # Less than default min_batch_size of 100
    for i in range(num_dirs):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        memory_limit_mb=5000,  # High limit so memory is not critical
        max_concurrent_subdirs=20,
    )

    # Set thresholds high so only small batches would trigger
    purger.empty_dirs_count_threshold = 10  # Low count threshold
    purger.empty_dirs_min_batch_size = 100  # But require 100+ to actually process

    # Track batch processing calls
    batch_process_calls = []

    original_process = purger._process_empty_dirs_batch

    async def monitored_process():
        """Track when batch processing is called."""
        # Check the batch size that would be processed
        async with purger.stats_lock:
            batch_size = len(purger.empty_dirs)
        batch_process_calls.append(batch_size)
        await original_process()

    purger._process_empty_dirs_batch = monitored_process

    # Scan the directory
    await purger.scan_directory(temp_dir)

    # Verify that no tiny batches were processed during scanning
    # (final cleanup at end may process remaining directories)
    tiny_batches = [size for size in batch_process_calls if 0 < size < purger.empty_dirs_min_batch_size]
    assert len(tiny_batches) == 0, (
        f"Expected no batches smaller than {purger.empty_dirs_min_batch_size}, "
        f"but found batches of sizes: {tiny_batches}. "
        "This indicates micro-batch thrashing is not being prevented."
    )


@pytest.mark.asyncio
async def test_logging_reports_batch_specific_counts(temp_dir):
    """
    Test Fix #4: Verify that logging reports batch-specific deletion counts, not cumulative.

    This fixes the confusing logs like:
    - batch_size: 3, deleted_in_batch: 24504
    Where 24504 was the running total instead of the actual deletions in that batch.
    """
    # Create enough directories to trigger multiple batches
    num_dirs = 300
    for i in range(num_dirs):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,  # Actually delete to track counts
        memory_limit_mb=100,
        max_concurrent_subdirs=20,
    )

    # Set low threshold to trigger multiple batches
    purger.empty_dirs_count_threshold = 100

    # Capture log messages by wrapping the logging function
    logged_batches = []

    from efspurge.logging import log_with_context
    original_log = log_with_context

    def capture_logs(logger, level, message, extra_fields=None):
        """Capture batch processing completion logs."""
        if message == "Empty directory batch processed":
            logged_batches.append({
                "batch_size": extra_fields.get("batch_size") if extra_fields else None,
                "deleted_in_batch": extra_fields.get("deleted_in_batch") if extra_fields else None,
                "total_processed": extra_fields.get("total_processed") if extra_fields else None,
            })
        original_log(logger, level, message, extra_fields)

    # Monkey patch the logging function
    import efspurge.purger
    efspurge.purger.log_with_context = capture_logs

    try:
        # Scan and process
        await purger.scan_directory(temp_dir)

        # Also run final empty dir removal to catch any remaining
        await purger._remove_empty_directories()

        # Verify that each batch's deleted_in_batch matches its batch_size (in dry_run=False)
        # or is close to it (accounting for errors, race conditions, etc.)
        for batch_info in logged_batches:
            batch_size = batch_info["batch_size"]
            deleted_in_batch = batch_info["deleted_in_batch"]

            # Skip if data is None (shouldn't happen)
            if batch_size is None or deleted_in_batch is None:
                continue

            # The deleted count should be close to batch size (within 10% tolerance for races)
            # It should NOT be a cumulative total that's much larger than batch_size
            assert deleted_in_batch <= batch_size * 1.1, (
                f"Batch size was {batch_size} but deleted_in_batch was {deleted_in_batch}. "
                "This indicates logging is reporting cumulative totals instead of batch-specific counts."
            )
    finally:
        # Restore original function
        efspurge.purger.log_with_context = original_log


@pytest.mark.asyncio
async def test_memory_critical_overrides_min_batch_size(temp_dir):
    """
    Test that when memory is critical, we process even small batches.

    The minimum batch size is meant to prevent thrashing, but when memory
    is at the limit, we should process regardless of batch size.
    """
    # Create a small number of empty directories
    num_dirs = 50
    for i in range(num_dirs):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        memory_limit_mb=100,  # Low limit
        max_concurrent_subdirs=20,
    )

    purger.empty_dirs_min_batch_size = 100  # Require 100+ normally
    purger.empty_dirs_memory_threshold = 0.01  # 1% threshold - very low

    # Track batch processing
    batch_processed = {"called": False, "batch_size": 0}

    original_process = purger._process_empty_dirs_batch

    async def monitored_process():
        """Track when processing happens."""
        async with purger.stats_lock:
            batch_processed["batch_size"] = len(purger.empty_dirs)
        batch_processed["called"] = True
        await original_process()

    purger._process_empty_dirs_batch = monitored_process

    # Mock memory to appear critical
    with patch("efspurge.purger.get_memory_usage_mb", return_value=99.0):
        await purger.scan_directory(temp_dir)

    # Verify that processing was triggered despite small batch size
    # because memory was critical
    assert batch_processed["called"], (
        "Expected batch processing to be triggered when memory is critical, "
        "even with small batch size"
    )


@pytest.mark.asyncio
async def test_lock_acquisition_pattern_is_non_blocking(temp_dir):
    """
    Test that the lock acquisition pattern doesn't block scanning tasks.

    When the lock is held, other tasks should skip processing and continue
    scanning, not wait in a queue. This prevents backpressure on the scan tasks.
    """
    # Create many empty directories
    num_dirs = 200
    for i in range(num_dirs):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        memory_limit_mb=100,
        max_concurrent_subdirs=20,
    )

    purger.empty_dirs_count_threshold = 50

    # Track waiting time on the lock
    wait_times = []

    original_check = purger._check_empty_directory

    async def monitored_check(directory):
        """Monitor how long we wait when checking directories."""
        start = asyncio.get_event_loop().time()
        await original_check(directory)
        elapsed = asyncio.get_event_loop().time() - start
        wait_times.append(elapsed)

    purger._check_empty_directory = monitored_check

    # Scan with high concurrency
    await purger.scan_directory(temp_dir)

    # Verify that individual checks don't wait long
    # Most should complete quickly (< 1 second)
    # Only the one that actually processes should take longer
    quick_checks = sum(1 for t in wait_times if t < 1.0)
    total_checks = len(wait_times)

    # At least 90% of checks should be quick (non-blocking)
    assert quick_checks >= total_checks * 0.9, (
        f"Expected most checks to be non-blocking, but only {quick_checks}/{total_checks} "
        "completed quickly. This indicates tasks are blocking on the lock."
    )


@pytest.mark.asyncio
async def test_no_duplicate_trigger_logs_at_same_timestamp(temp_dir):
    """
    Test that we significantly reduce duplicate trigger logs at the same timestamp.

    In production we saw logs like:
    {"timestamp": "2026-02-04 14:48:16,190", "message": "triggered"...}
    {"timestamp": "2026-02-04 14:48:16,190", "message": "triggered"...}
    {"timestamp": "2026-02-04 14:48:16,190", "message": "triggered"...}

    With our fix, we should have very few trigger logs (ideally one per actual batch processing).
    """
    # Create many empty directories
    num_dirs = 200
    for i in range(num_dirs):
        empty_dir = temp_dir / f"empty_{i}"
        empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        memory_limit_mb=100,
        max_concurrent_subdirs=20,
    )

    purger.empty_dirs_count_threshold = 50

    # Capture trigger logs with timestamps
    trigger_logs = []

    from efspurge.logging import log_with_context
    original_log = log_with_context

    def capture_trigger_logs(logger, level, message, extra_fields=None):
        """Capture trigger log messages."""
        if message == "Incremental empty directory processing triggered":
            trigger_logs.append({
                "timestamp": asyncio.get_event_loop().time(),
                "msg": message,
            })
        original_log(logger, level, message, extra_fields)

    # Monkey patch the logging function
    import efspurge.purger
    efspurge.purger.log_with_context = capture_trigger_logs

    try:
        # Scan with high concurrency
        await purger.scan_directory(temp_dir)

        # We should have far fewer trigger logs than directories
        # With 200 directories and threshold of 50, we expect ~4 batches max
        # Allow some tolerance for race conditions
        assert len(trigger_logs) <= 10, (
            f"Expected <= 10 trigger logs, got {len(trigger_logs)}. "
            "This indicates excessive log spam."
        )
    finally:
        # Restore original function
        efspurge.purger.log_with_context = original_log
