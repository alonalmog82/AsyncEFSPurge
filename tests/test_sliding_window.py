"""Tests specifically for sliding window logic."""

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
async def test_exactly_batch_size_files(temp_dir):
    """Test directory with exactly batch_size files."""
    batch_size = 100

    # Create exactly batch_size files
    for i in range(batch_size):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=batch_size,
    )

    await purger.scan_directory(temp_dir)

    # All files should be processed
    assert purger.stats["files_scanned"] == batch_size


@pytest.mark.asyncio
async def test_batch_size_plus_one_files(temp_dir):
    """Test directory with batch_size + 1 files."""
    batch_size = 100

    # Create batch_size + 1 files
    for i in range(batch_size + 1):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=batch_size,
    )

    await purger.scan_directory(temp_dir)

    # All files should be processed
    assert purger.stats["files_scanned"] == batch_size + 1


@pytest.mark.asyncio
async def test_multiple_batches(temp_dir):
    """Test directory requiring multiple batches."""
    batch_size = 50
    total_files = batch_size * 3 + 25  # 3 full batches + 25 remaining

    # Create files
    for i in range(total_files):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=batch_size,
    )

    await purger.scan_directory(temp_dir)

    # All files should be processed
    assert purger.stats["files_scanned"] == total_files


@pytest.mark.asyncio
async def test_smaller_than_batch_size(temp_dir):
    """Test directory with fewer files than batch_size."""
    batch_size = 100
    file_count = 25  # Less than batch_size

    # Create files
    for i in range(file_count):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=batch_size,
    )

    await purger.scan_directory(temp_dir)

    # All files should be processed in remaining buffer
    assert purger.stats["files_scanned"] == file_count


@pytest.mark.asyncio
async def test_buffer_cleared_after_processing(temp_dir):
    """Test that buffer is cleared after processing."""
    batch_size = 10

    # Create enough files to trigger batch processing
    for i in range(batch_size * 2):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=batch_size,
    )

    # Mock _process_file_batch to verify buffer clearing
    original_process = purger._process_file_batch
    batch_sizes_seen = []

    async def mock_process(batch):
        batch_sizes_seen.append(len(batch))
        await original_process(batch)
        # Verify buffer was cleared (should be empty after processing)
        # Note: We can't directly check buffer here, but we can verify
        # that batches are the right size

    purger._process_file_batch = mock_process

    await purger.scan_directory(temp_dir)

    # Should see batches of exactly batch_size (except possibly last)
    assert len(batch_sizes_seen) >= 2  # At least 2 batches
    assert all(size == batch_size for size in batch_sizes_seen[:-1])  # All but last are batch_size
    assert batch_sizes_seen[-1] <= batch_size  # Last batch <= batch_size


@pytest.mark.asyncio
async def test_mixed_files_and_directories(temp_dir):
    """Test sliding window with mixed files and directories."""
    batch_size = 10

    # Create files and directories
    for i in range(15):  # More than batch_size
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    # Create subdirectories (should not affect file buffer)
    for i in range(5):
        subdir = temp_dir / f"dir{i}"
        subdir.mkdir()
        (subdir / f"subfile{i}.txt").write_text(f"subcontent{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=batch_size,
    )

    await purger.scan_directory(temp_dir)

    # Should process all files (15 in root + 5 in subdirs)
    assert purger.stats["files_scanned"] == 20
    assert purger.stats["dirs_scanned"] == 6  # Root + 5 subdirs
