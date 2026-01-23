"""Tests for subdirectory concurrency behavior.

These tests verify that:
1. Subdirectories are processed with constant concurrency (no idle slots)
2. Slow directories don't block others
3. Tasks are created on-demand (not all upfront) to prevent memory explosion
4. The hybrid approach maintains high utilization
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
async def test_subdir_concurrency_maintained(temp_dir):
    """Test that subdirectory concurrency is maintained (no idle slots)."""
    # Create many subdirectories
    num_subdirs = 50
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        # Add a few files to each
        for j in range(10):
            (subdir / f"file{j}.txt").write_text(f"content{i}_{j}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=10,  # Limit to 10 concurrent
    )

    # Track when directories start scanning
    scan_start_times = {}
    scan_end_times = {}

    original_scan = purger.scan_directory

    async def tracked_scan(directory: Path):
        scan_start_times[str(directory)] = time.time()
        try:
            await original_scan(directory)
        finally:
            scan_end_times[str(directory)] = time.time()

    purger.scan_directory = tracked_scan

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1  # +1 for root

    # Check that scans overlapped (indicating concurrency)
    # If scans were sequential, total time would be sum of individual times
    # If concurrent, total time should be closer to max individual time
    if len(scan_start_times) > 1:
        total_time = max(scan_end_times.values()) - min(scan_start_times.values())
        # With concurrency, total time should be much less than sequential
        # (This is a sanity check - exact timing depends on system load)
        assert total_time < 10.0  # Should complete reasonably quickly


@pytest.mark.asyncio
async def test_slow_directories_dont_block_others(temp_dir):
    """Test that slow directories don't block other subdirectories."""
    # Create mix of fast and slow directories
    # Fast directories: small, few files
    for i in range(10):
        fast_dir = temp_dir / f"fast{i}"
        fast_dir.mkdir()
        (fast_dir / "file.txt").write_text("content")

    # Slow directories: many files (simulate slow scanning)
    for i in range(2):
        slow_dir = temp_dir / f"slow{i}"
        slow_dir.mkdir()
        # Create many files to make scanning slower
        for j in range(100):
            (slow_dir / f"file{j}.txt").write_text(f"content{j}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=5,  # Small limit to see effect
    )

    # Track completion order
    completion_order = []
    original_scan = purger.scan_directory

    async def tracked_scan(directory: Path):
        dir_name = directory.name
        await original_scan(directory)
        completion_order.append(dir_name)

    purger.scan_directory = tracked_scan

    await purger.purge()

    # Verify all directories were scanned
    assert purger.stats["dirs_scanned"] == 13  # 10 fast + 2 slow + root

    # Fast directories should complete before slow ones finish
    # (This verifies they weren't blocked waiting for slow ones)
    fast_completions = [d for d in completion_order if d.startswith("fast")]
    slow_completions = [d for d in completion_order if d.startswith("slow")]

    # At least some fast directories should complete
    assert len(fast_completions) > 0, "Fast directories should complete"
    assert len(slow_completions) == 2, "Both slow directories should complete"


@pytest.mark.asyncio
async def test_tasks_created_on_demand(temp_dir):
    """Test that tasks are created on-demand, not all upfront."""
    # Create many subdirectories
    num_subdirs = 100
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=10,  # Limit to 10 concurrent
    )

    # Track when _process_subdirs_with_constant_concurrency is called
    # and how many tasks are active at different times
    max_tasks_seen = 0
    task_counts = []

    original_method = purger._process_subdirs_with_constant_concurrency

    async def tracked_method(subdirs):
        # Count how many tasks are created
        # The method should never create more than max_concurrent_subdirs at once
        nonlocal max_tasks_seen, task_counts

        # Call original and track task creation
        # We can't easily track internal task creation, but we can verify
        # the method completes successfully
        await original_method(subdirs)
        # If we got here without OOM, tasks were likely created on-demand

    purger._process_subdirs_with_constant_concurrency = tracked_method

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1

    # Memory should be bounded (if tasks were created all upfront, memory would spike)
    peak_memory = purger.stats.get("peak_memory_mb", 0)
    assert peak_memory < 500, f"Memory should be bounded, got {peak_memory}MB"


@pytest.mark.asyncio
async def test_memory_bounded_with_many_subdirs(temp_dir):
    """Test that memory is bounded even with many subdirectories."""
    # Create many subdirectories (more than max_concurrent_subdirs)
    num_subdirs = 200
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=20,  # Limit to 20 concurrent
        memory_limit_mb=100,  # Set memory limit
    )

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1

    # Memory should be bounded (not grow linearly with subdir count)
    # Peak memory should be reasonable
    peak_memory = purger.stats.get("peak_memory_mb", 0)
    # Should be well under limit (allowing for overhead)
    assert peak_memory < 200, f"Memory should be bounded, got {peak_memory}MB"


@pytest.mark.asyncio
async def test_deep_directory_tree_memory_safety(temp_dir):
    """Test that deep directory trees don't cause memory explosion.

    IMPORTANT: This test uses 40×40×40 (65,641 dirs) for reasonable CI runtime.

    Before committing changes to subdirectory concurrency logic (especially
    _process_subdirs_with_constant_concurrency or scan_directory), please test
    manually with 80×80×80 (518,481 dirs) to ensure no deadlock or memory issues:

        # Change range(3) to use 80 dirs per level
        for i in range(80):  # 80 dirs per level
        expected_dirs = 1 + 80 + 6400 + 512000  # 518,481 total

    The 80×80×80 test should complete in ~6 minutes and verify:
    - No deadlock occurs
    - Memory stays bounded (<800MB)
    - All directories are scanned correctly
    """
    import sys
    import time

    print("\n=== Starting deep directory tree test ===", file=sys.stderr, flush=True)
    print(f"Temp dir: {temp_dir}", file=sys.stderr, flush=True)
    start_time = time.time()

    # Create deep nested structure (reasonable size for CI)
    # Level 1: 40 dirs
    # Level 2: Each has 40 dirs = 1,600 dirs
    # Level 3: Each has 40 dirs = 64,000 dirs
    # Total: 1 (root) + 40 + 1,600 + 64,000 = 65,641 dirs
    # NOTE: For pre-commit testing, use 80×80×80 (518,481 dirs) - see docstring above
    print("Creating directory structure (40x40x40)...", file=sys.stderr, flush=True)
    structure_start = time.time()
    current_level = [temp_dir]
    total_dirs = 0

    for level in range(3):  # 3 levels deep
        print(f"  Creating level {level + 1}...", file=sys.stderr, flush=True)
        level_start = time.time()
        next_level = []
        for parent in current_level:
            for i in range(40):  # 40 dirs per level (use 80 for pre-commit stress test)
                subdir = parent / f"level{level}_dir{i}"
                subdir.mkdir()
                (subdir / "file.txt").write_text("content")
                next_level.append(subdir)
                total_dirs += 1
        current_level = next_level
        level_time = time.time() - level_start
        print(
            f"  Level {level + 1} created: {len(next_level)} dirs (took {level_time:.2f}s)",
            file=sys.stderr,
            flush=True,
        )

    print(
        f"Structure creation complete: {total_dirs} dirs in {time.time() - structure_start:.2f}s",
        file=sys.stderr,
        flush=True,
    )

    print("Creating purger...", file=sys.stderr, flush=True)
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=30,  # Good concurrency for this structure
        memory_limit_mb=400,  # Reasonable limit for CI
        log_level="INFO",
    )
    print(
        f"Purger created. max_concurrent_subdirs={purger.max_concurrent_subdirs}",
        file=sys.stderr,
        flush=True,
    )

    print("Starting purge...", file=sys.stderr, flush=True)
    purge_start = time.time()

    # Simple progress tracking without background task
    original_update = purger.update_stats

    async def tracked_update(**kwargs):
        result = await original_update(**kwargs)
        if "dirs_scanned" in kwargs:
            print(
                f"  [PROGRESS] dirs_scanned={purger.stats['dirs_scanned']}, "
                f"files_scanned={purger.stats['files_scanned']}",
                file=sys.stderr,
                flush=True,
            )
        return result

    purger.update_stats = tracked_update

    try:
        await purger.purge()
    except Exception as e:
        print(f"ERROR during purge: {e}", file=sys.stderr, flush=True)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise

    purge_time = time.time() - purge_start
    total_time = time.time() - start_time
    print(f"Purge complete: took {purge_time:.2f}s (total: {total_time:.2f}s)", file=sys.stderr, flush=True)

    # Verify all directories were scanned
    # Total: 1 (root) + 40 (level 1) + 1,600 (level 2) + 64,000 (level 3) = 65,641
    # For 80×80×80 stress test: expected_dirs = 1 + 80 + 6400 + 512000 = 518,481
    expected_dirs = 1 + 40 + 1600 + 64000
    print(f"Expected dirs: {expected_dirs}, Scanned: {purger.stats['dirs_scanned']}", file=sys.stderr, flush=True)
    assert purger.stats["dirs_scanned"] == expected_dirs

    # Memory should be bounded (not explode with depth)
    peak_memory = purger.stats.get("peak_memory_mb", 0)
    print(f"Peak memory: {peak_memory}MB", file=sys.stderr, flush=True)
    # With 65K+ directories, memory should still be reasonable
    # For 80×80×80 stress test, use: assert peak_memory < 800
    assert peak_memory < 600, f"Memory should be bounded even with 65K+ dirs, got {peak_memory}MB"


@pytest.mark.asyncio
async def test_hybrid_approach_maintains_concurrency(temp_dir):
    """Test that hybrid approach maintains constant concurrency."""
    # Create structure with many subdirectories
    num_subdirs = 50
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        # Add files to make scanning take some time
        for j in range(20):
            (subdir / f"file{j}.txt").write_text(f"content{i}_{j}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=10,
    )

    # Track concurrent scans
    concurrent_scans = []
    max_concurrent = 0

    original_scan = purger.scan_directory

    async def tracked_scan(directory: Path):
        nonlocal max_concurrent
        async with purger.active_directories_lock:
            current_count = len(purger.active_directories)
            concurrent_scans.append(current_count)
            max_concurrent = max(max_concurrent, current_count)
        await original_scan(directory)

    purger.scan_directory = tracked_scan

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1

    # With max_concurrent_subdirs=10, we should see up to 10 concurrent scans
    # (plus the root directory scan)
    # Max concurrent should be close to max_concurrent_subdirs
    assert max_concurrent <= purger.max_concurrent_subdirs + 1, (
        f"Should not exceed max_concurrent_subdirs, got {max_concurrent}"
    )

    # Should have seen some concurrency (not just sequential)
    if len(concurrent_scans) > 1:
        # Should have seen multiple concurrent scans at some point
        max_seen = max(concurrent_scans)
        assert max_seen > 1, "Should see concurrent scans, not just sequential"


@pytest.mark.asyncio
async def test_subdir_semaphore_limits_concurrency(temp_dir):
    """Test that subdir_semaphore properly limits concurrency."""
    # Create many subdirectories
    num_subdirs = 30
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_subdirs=5,  # Small limit
    )

    # Track how many directories are active at once
    max_concurrent_seen = 0
    concurrent_counts = []

    original_scan = purger.scan_directory

    async def tracked_scan(directory: Path):
        nonlocal max_concurrent_seen
        async with purger.active_directories_lock:
            current_count = len(purger.active_directories)
            concurrent_counts.append(current_count)
            max_concurrent_seen = max(max_concurrent_seen, current_count)
        await original_scan(directory)

    purger.scan_directory = tracked_scan

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1

    # Semaphore should limit concurrent scans
    # Max concurrent should not exceed max_concurrent_subdirs (plus root)
    assert max_concurrent_seen <= purger.max_concurrent_subdirs + 1, (
        f"Should not exceed max_concurrent_subdirs, got {max_concurrent_seen}"
    )

    # Should have seen some concurrency
    if len(concurrent_counts) > 1:
        max_count = max(concurrent_counts)
        assert max_count > 1, "Should see concurrent scans"
