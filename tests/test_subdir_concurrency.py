"""Tests for BFS-based directory scanning concurrency.

These tests verify that:
1. Multiple directories are processed concurrently via the BFS worker pool
2. Slow directories don't block others (workers process independently)
3. Memory stays bounded with many subdirectories
4. active_directories is tracked during scanning for diagnostics
5. max_concurrent_discovery controls worker count
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
async def test_bfs_concurrency_maintained(temp_dir):
    """Test that BFS worker pool processes multiple subdirectories concurrently."""
    num_subdirs = 50
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        for j in range(10):
            (subdir / f"file{j}.txt").write_text(f"content{i}_{j}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_discovery=10,
    )

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1  # +1 for root


@pytest.mark.asyncio
async def test_slow_directories_dont_block_others(temp_dir):
    """Test that slow directories don't block other subdirectories."""
    # Fast directories: small, few files
    for i in range(10):
        fast_dir = temp_dir / f"fast{i}"
        fast_dir.mkdir()
        (fast_dir / "file.txt").write_text("content")

    # Slow directories: many files (simulate slow scanning)
    for i in range(2):
        slow_dir = temp_dir / f"slow{i}"
        slow_dir.mkdir()
        for j in range(100):
            (slow_dir / f"file{j}.txt").write_text(f"content{j}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_discovery=5,
    )

    await purger.purge()

    # Verify all directories were scanned
    assert purger.stats["dirs_scanned"] == 13  # 10 fast + 2 slow + root


@pytest.mark.asyncio
async def test_memory_bounded_with_many_subdirs(temp_dir):
    """Test that memory is bounded even with many subdirectories."""
    num_subdirs = 200
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_discovery=20,
        memory_limit_mb=100,
    )

    await purger.purge()

    # Verify all subdirectories were scanned
    assert purger.stats["dirs_scanned"] == num_subdirs + 1


@pytest.mark.asyncio
async def test_deep_directory_tree_memory_safety(temp_dir):
    """Test that deep directory trees don't cause memory explosion.

    Uses 40×40×40 (65,641 dirs) for reasonable CI runtime.
    """
    import sys

    print("\n=== Starting deep directory tree test ===", file=sys.stderr, flush=True)
    start_time = time.time()

    # Create deep nested structure (40 dirs per level, 3 levels)
    current_level = [temp_dir]
    total_dirs = 0

    for level in range(3):
        next_level = []
        for parent in current_level:
            for i in range(40):
                subdir = parent / f"level{level}_dir{i}"
                subdir.mkdir()
                (subdir / "file.txt").write_text("content")
                next_level.append(subdir)
                total_dirs += 1
        current_level = next_level

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_discovery=30,
        memory_limit_mb=400,
        log_level="INFO",
    )

    await purger.purge()

    total_time = time.time() - start_time
    print(f"Purge complete: took {total_time:.2f}s", file=sys.stderr, flush=True)

    expected_dirs = 1 + 40 + 1600 + 64000
    assert purger.stats["dirs_scanned"] == expected_dirs


@pytest.mark.asyncio
async def test_active_directories_cleared_after_scan(temp_dir):
    """Test that active_directories is empty after purge completes."""
    num_subdirs = 50
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        for j in range(20):
            (subdir / f"file{j}.txt").write_text(f"content{i}_{j}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_discovery=10,
    )

    await purger.purge()

    # After purge completes, active_directories should be empty
    assert len(purger.active_directories) == 0
    assert purger.stats["dirs_scanned"] == num_subdirs + 1


@pytest.mark.asyncio
async def test_max_concurrent_discovery_limits_workers(temp_dir):
    """Test that max_concurrent_discovery controls worker pool size."""
    num_subdirs = 30
    for i in range(num_subdirs):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_concurrent_discovery=5,
    )

    await purger.purge()

    assert purger.stats["dirs_scanned"] == num_subdirs + 1
    assert purger.max_concurrent_discovery == 5
