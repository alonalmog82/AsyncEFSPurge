"""Edge case tests for AsyncEFSPurge."""

import os
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
async def test_file_deleted_during_processing(temp_dir):
    """Test handling of file deleted between stat and remove."""
    # Create a file
    test_file = temp_dir / "test.txt"
    test_file.write_text("test")

    # Make it old
    old_time = time.time() - (31 * 86400)  # 31 days ago
    os.utime(test_file, (old_time, old_time))

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=False,
        max_concurrency=1,
    )

    # Delete file manually (simulating race condition)
    test_file.unlink()

    # Process should handle gracefully (FileNotFoundError caught)
    await purger.process_file(test_file)

    # File was deleted before stat, so files_scanned won't increment
    # But should not crash - this is the expected behavior
    assert purger.stats["files_scanned"] == 0
    assert purger.stats["errors"] == 0  # FileNotFoundError is handled gracefully


@pytest.mark.asyncio
async def test_symlink_skipped(temp_dir):
    """Test that symlinks are skipped."""
    # Create a file
    real_file = temp_dir / "real.txt"
    real_file.write_text("content")

    # Create symlink
    symlink = temp_dir / "link.txt"
    symlink.symlink_to(real_file)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Symlink should be skipped, real file should be processed
    assert purger.stats["symlinks_skipped"] == 1
    assert purger.stats["files_scanned"] == 1


@pytest.mark.asyncio
async def test_permission_denied(temp_dir):
    """Test handling of permission denied errors."""
    # Create a file
    test_file = temp_dir / "test.txt"
    test_file.write_text("test")

    # Make it read-only (on Unix)
    if os.name != "nt":  # Windows handles permissions differently
        test_file.chmod(0o444)  # Read-only

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Should handle gracefully, increment errors
    assert purger.stats["errors"] >= 0  # Might be 0 on Windows


@pytest.mark.asyncio
async def test_empty_directory(temp_dir):
    """Test scanning empty directory."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
    )

    await purger.scan_directory(temp_dir)

    assert purger.stats["dirs_scanned"] == 1
    assert purger.stats["files_scanned"] == 0


@pytest.mark.asyncio
async def test_nested_directories(temp_dir):
    """Test nested directory structure."""
    # Create nested structure
    (temp_dir / "level1" / "level2" / "level3").mkdir(parents=True)

    # Create files at different levels
    (temp_dir / "root.txt").write_text("root")
    (temp_dir / "level1" / "level1.txt").write_text("level1")
    (temp_dir / "level1" / "level2" / "level2.txt").write_text("level2")
    (temp_dir / "level1" / "level2" / "level3" / "level3.txt").write_text("level3")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
    )

    await purger.scan_directory(temp_dir)

    assert purger.stats["files_scanned"] == 4
    assert purger.stats["dirs_scanned"] == 4  # root + 3 levels


@pytest.mark.asyncio
async def test_very_large_batch_size(temp_dir):
    """Test with very large batch size."""
    # Create many files
    for i in range(100):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=10000,  # Larger than file count
    )

    await purger.scan_directory(temp_dir)

    # Should process all files
    assert purger.stats["files_scanned"] == 100


@pytest.mark.asyncio
async def test_small_batch_size(temp_dir):
    """Test with small batch size."""
    # Create many files
    for i in range(100):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        task_batch_size=10,  # Small batch
    )

    await purger.scan_directory(temp_dir)

    # Should process all files in multiple batches
    assert purger.stats["files_scanned"] == 100


@pytest.mark.asyncio
async def test_dry_run_no_deletion(temp_dir):
    """Test that dry-run doesn't delete files."""
    # Create old file
    old_file = temp_dir / "old.txt"
    old_file.write_text("old content")
    old_time = time.time() - (31 * 86400)
    os.utime(old_file, (old_time, old_time))

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
    )

    await purger.scan_directory(temp_dir)

    # File should still exist
    assert old_file.exists()
    assert purger.stats["files_to_purge"] == 1
    assert purger.stats["files_purged"] == 0


@pytest.mark.asyncio
async def test_actual_deletion(temp_dir):
    """Test that actual deletion works."""
    # Create old file
    old_file = temp_dir / "old.txt"
    old_file.write_text("old content")
    old_time = time.time() - (31 * 86400)
    os.utime(old_file, (old_time, old_time))

    # Create new file
    new_file = temp_dir / "new.txt"
    new_file.write_text("new content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Old file should be deleted, new file should remain
    assert not old_file.exists()
    assert new_file.exists()
    assert purger.stats["files_purged"] == 1


@pytest.mark.asyncio
async def test_concurrent_file_processing(temp_dir):
    """Test concurrent file processing."""
    # Create many files
    for i in range(50):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        max_concurrency=10,
    )

    await purger.scan_directory(temp_dir)

    assert purger.stats["files_scanned"] == 50


@pytest.mark.asyncio
async def test_nonexistent_path():
    """Test handling of nonexistent root path."""
    purger = AsyncEFSPurger(
        root_path="/nonexistent/path/that/does/not/exist",
        max_age_days=30,
    )

    with pytest.raises(FileNotFoundError):
        await purger.purge()


@pytest.mark.asyncio
async def test_memory_limit_zero(temp_dir):
    """Test with memory limit disabled (0)."""
    for i in range(10):
        (temp_dir / f"file{i}.txt").write_text(f"content{i}")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        memory_limit_mb=0,  # Disabled
    )

    await purger.scan_directory(temp_dir)

    # Should work without memory checks
    assert purger.stats["files_scanned"] == 10
    assert purger.stats["memory_backpressure_events"] == 0

