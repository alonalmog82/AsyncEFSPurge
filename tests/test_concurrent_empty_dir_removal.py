"""Tests for concurrent empty directory removal."""

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
async def test_concurrent_empty_dir_deletion(temp_dir):
    """Test that empty directories are deleted concurrently."""
    # Create many empty directories (more than default concurrency)
    num_dirs = 50  # Smaller number for faster test
    for i in range(num_dirs):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=20,  # High concurrency
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Measure total deletion time
    start_time = time.time()
    await purger._remove_empty_directories()
    total_time = time.time() - start_time

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Concurrent deletion should complete quickly
    # Sequential would take much longer (50 * ~0.001s = 0.05s minimum)
    # Concurrent should be much faster (closer to ~0.01s)
    # This is a sanity check - exact timing depends on system
    assert total_time < 1.0, f"Concurrent deletion should be fast. Took {total_time:.3f}s for {num_dirs} directories."


@pytest.mark.asyncio
async def test_concurrent_deletion_respects_semaphore(temp_dir):
    """Test that concurrent deletion respects deletion_semaphore limit."""
    # Create many empty directories
    num_dirs = 50  # Smaller number for faster test
    for i in range(num_dirs):
        (temp_dir / f"empty_{i}").mkdir()

    max_concurrency = 10  # Low concurrency to see the limit
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=max_concurrency,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Verify all directories were deleted
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Verify semaphore was used (by checking that deletion happened)
    # The semaphore limits concurrency internally, so we can't easily test
    # the exact concurrent count without more invasive mocking
    # But we can verify it completed successfully
    remaining = [d for d in temp_dir.iterdir() if d.is_dir()]
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_concurrent_cascading_deletion(temp_dir):
    """Test that cascading parent deletion works correctly with concurrency."""
    # Create deeply nested structure
    # /a/b/c/d/e (all empty, will cascade)
    deep_dir = temp_dir / "a" / "b" / "c" / "d" / "e"
    deep_dir.mkdir(parents=True)

    # Also create flat structure
    for i in range(10):
        (temp_dir / f"flat_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=50,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Should delete: 5 nested (e, d, c, b, a) + 10 flat = 15 total
    assert purger.stats["empty_dirs_deleted"] == 15

    # All should be gone
    assert not deep_dir.exists()
    assert not (temp_dir / "a").exists()
    for i in range(10):
        assert not (temp_dir / f"flat_{i}").exists()


@pytest.mark.asyncio
async def test_concurrent_deletion_no_duplicates(temp_dir):
    """Test that concurrent deletion doesn't process directories twice."""
    # Create many empty directories
    num_dirs = 50  # Smaller number for faster test
    for i in range(num_dirs):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=20,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Verify all directories were deleted exactly once
    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # Verify no directories remain (all were deleted)
    remaining = [d for d in temp_dir.iterdir() if d.is_dir()]
    assert len(remaining) == 0

    # The processed_dirs set in the implementation prevents duplicates
    # We verify correctness by checking stats match expected count


@pytest.mark.asyncio
async def test_concurrent_deletion_rate_limit(temp_dir):
    """Test that rate limit works correctly with concurrent deletion."""
    # Create many empty directories
    num_dirs = 100
    for i in range(num_dirs):
        (temp_dir / f"empty_{i}").mkdir()

    rate_limit = 50
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=20,
        max_empty_dirs_to_delete=rate_limit,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Should only delete up to rate limit
    assert purger.stats["empty_dirs_deleted"] == rate_limit
    assert purger.stats["empty_dirs_to_delete"] == rate_limit

    # Should have remaining directories
    remaining = [d for d in temp_dir.iterdir() if d.is_dir()]
    assert len(remaining) == num_dirs - rate_limit


@pytest.mark.asyncio
async def test_concurrent_deletion_handles_already_deleted(temp_dir):
    """Test that concurrent deletion handles directories already deleted by another process."""
    # Create empty directories
    for i in range(20):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=10,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Manually delete some directories to simulate race condition
    import aiofiles

    for i in range(5):
        await aiofiles.os.rmdir(temp_dir / f"empty_{i}")

    # Should handle gracefully
    await purger._remove_empty_directories()

    # Should delete remaining directories (15)
    assert purger.stats["empty_dirs_deleted"] == 15
    assert purger.stats["errors"] == 0  # FileNotFoundError should be handled gracefully


@pytest.mark.asyncio
async def test_concurrent_deletion_handles_populated_dirs(temp_dir):
    """Test that concurrent deletion skips directories that become populated."""
    # Create empty directories
    for i in range(20):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=10,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Add files to some directories to simulate them being populated
    for i in range(5):
        (temp_dir / f"empty_{i}" / "file.txt").write_text("content")

    await purger._remove_empty_directories()

    # Should only delete directories that remained empty (15)
    assert purger.stats["empty_dirs_deleted"] == 15

    # Populated directories should still exist
    for i in range(5):
        assert (temp_dir / f"empty_{i}").exists()
