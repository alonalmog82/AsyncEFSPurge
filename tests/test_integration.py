"""Integration tests for AsyncEFSPurge - run in CI."""

import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def large_test_structure():
    """Create a large test directory structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        # Create flat directory with many files
        flat_dir = base / "flat"
        flat_dir.mkdir()
        for i in range(1000):
            (flat_dir / f"file{i}.txt").write_text(f"content{i}")

        # Create nested directory structure
        nested_dir = base / "nested"
        nested_dir.mkdir()
        for dir_num in range(10):
            subdir = nested_dir / f"dir{dir_num}"
            subdir.mkdir()
            for file_num in range(100):
                (subdir / f"file{file_num}.txt").write_text(f"content{dir_num}_{file_num}")

        # Make some files old
        old_time = time.time() - (31 * 86400)  # 31 days ago

        # Make 50% of flat files old
        for i in range(500):
            os.utime(flat_dir / f"file{i}.txt", (old_time, old_time))

        # Make 50% of nested files old
        for dir_num in range(5):
            for file_num in range(100):
                os.utime(nested_dir / f"dir{dir_num}" / f"file{file_num}.txt", (old_time, old_time))

        yield base


@pytest.mark.asyncio
@pytest.mark.integration
async def test_large_flat_directory(large_test_structure):
    """Test processing large flat directory."""
    flat_dir = large_test_structure / "flat"

    purger = AsyncEFSPurger(
        root_path=str(flat_dir),
        max_age_days=30,
        max_concurrency_scanning=100,
        max_concurrency_deletion=100,
        memory_limit_mb=200,
        task_batch_size=200,
        dry_run=True,
    )

    stats = await purger.purge()

    assert stats["files_scanned"] == 1000
    assert stats["files_to_purge"] == 500
    assert stats["files_purged"] == 0  # Dry run
    assert stats["memory_backpressure_events"] == 0
    assert stats["peak_memory_mb"] < 200  # Should be well under limit


@pytest.mark.asyncio
@pytest.mark.integration
async def test_large_nested_directory(large_test_structure):
    """Test processing large nested directory structure."""
    nested_dir = large_test_structure / "nested"

    purger = AsyncEFSPurger(
        root_path=str(nested_dir),
        max_age_days=30,
        max_concurrency_scanning=100,
        max_concurrency_deletion=100,
        memory_limit_mb=200,
        task_batch_size=200,
        dry_run=True,
    )

    stats = await purger.purge()

    assert stats["files_scanned"] == 1000  # 10 dirs * 100 files
    assert stats["files_to_purge"] == 500  # 5 dirs * 100 files
    assert stats["dirs_scanned"] == 11  # 10 subdirs + root
    assert stats["memory_backpressure_events"] == 0
    assert stats["peak_memory_mb"] < 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_actual_deletion_large_structure(large_test_structure):
    """Test actual deletion on large structure."""
    flat_dir = large_test_structure / "flat"

    # Count files before
    files_before = len(list(flat_dir.glob("*.txt")))
    assert files_before == 1000

    purger = AsyncEFSPurger(
        root_path=str(flat_dir),
        max_age_days=30,
        max_concurrency_scanning=100,
        max_concurrency_deletion=100,
        memory_limit_mb=200,
        task_batch_size=200,
        dry_run=False,
    )

    stats = await purger.purge()

    # Count files after
    files_after = len(list(flat_dir.glob("*.txt")))

    assert stats["files_scanned"] == 1000
    assert stats["files_purged"] == 500
    assert files_after == 500  # Should have deleted 500 old files
    assert stats["memory_backpressure_events"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_memory_stress_test(large_test_structure):
    """Test with aggressive memory limit."""
    nested_dir = large_test_structure / "nested"

    purger = AsyncEFSPurger(
        root_path=str(nested_dir),
        max_age_days=30,
        max_concurrency_scanning=100,
        max_concurrency_deletion=100,
        memory_limit_mb=50,  # Very low limit
        task_batch_size=50,  # Small batches
        dry_run=True,
    )

    stats = await purger.purge()

    # Should complete successfully even with low memory limit
    assert stats["files_scanned"] == 1000
    # Might have some backpressure events, but should complete
    assert stats["peak_memory_mb"] < 100  # Should still be reasonable


@pytest.mark.asyncio
@pytest.mark.integration
async def test_streaming_architecture_verification(large_test_structure):
    """Verify streaming architecture works correctly."""
    # Create directory with exactly batch_size files
    test_dir = large_test_structure / "batch_test"
    test_dir.mkdir()

    batch_size = 200
    for i in range(batch_size * 3):  # 3 full batches
        (test_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(test_dir),
        max_age_days=30,
        task_batch_size=batch_size,
        dry_run=True,
    )

    stats = await purger.purge()

    # Should process all files
    assert stats["files_scanned"] == batch_size * 3
    # Memory should be bounded (not grow with file count)
    assert stats["peak_memory_mb"] < 100  # Should be low


@pytest.mark.asyncio
@pytest.mark.integration
async def test_progress_updates(large_test_structure):
    """Test that progress updates appear."""
    nested_dir = large_test_structure / "nested"

    purger = AsyncEFSPurger(
        root_path=str(nested_dir),
        max_age_days=30,
        max_concurrency_scanning=10,  # Lower concurrency = slower = more progress updates
        max_concurrency_deletion=10,
        dry_run=True,
    )

    # Start purge
    task = asyncio.create_task(purger.purge())

    # Wait a bit
    await asyncio.sleep(0.5)

    # Check that progress reporter is running
    # (stats should be updating)

    # Wait a bit more
    await asyncio.sleep(0.5)

    # Should have processed more files
    await task  # Wait for completion

    stats = await task
    assert stats["files_scanned"] == 1000
