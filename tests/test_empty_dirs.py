"""Tests for empty directory removal feature."""

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
async def test_empty_dir_not_removed_by_default(temp_dir):
    """Test that empty directories are not removed by default."""
    # Create empty directory
    empty_dir = temp_dir / "empty"
    empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=False,  # Default
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Empty directory should still exist
    assert empty_dir.exists()
    assert purger.stats["empty_dirs_deleted"] == 0


@pytest.mark.asyncio
async def test_empty_dir_removed_when_enabled(temp_dir):
    """Test that empty directories are removed when flag is enabled."""
    # Create empty directory
    empty_dir = temp_dir / "empty"
    empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Empty directory should be deleted
    assert not empty_dir.exists()
    assert purger.stats["empty_dirs_deleted"] == 1


@pytest.mark.asyncio
async def test_root_dir_never_removed(temp_dir):
    """Test that root directory is never removed, even if empty."""
    # Create empty root (only for this test)
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Root should still exist
    assert temp_dir.exists()
    assert purger.stats["empty_dirs_deleted"] == 0


@pytest.mark.asyncio
async def test_nested_empty_dirs_post_order(temp_dir):
    """Test that nested empty directories are deleted in post-order."""
    # Create nested structure: /a/b/c (all empty)
    dir_a = temp_dir / "a"
    dir_b = dir_a / "b"
    dir_c = dir_b / "c"
    dir_c.mkdir(parents=True)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # All nested empty directories should be deleted
    assert not dir_c.exists()
    assert not dir_b.exists()
    assert not dir_a.exists()
    assert purger.stats["empty_dirs_deleted"] == 3


@pytest.mark.asyncio
async def test_dir_with_files_not_removed(temp_dir):
    """Test that directories with files are not removed."""
    # Create directory with a file
    dir_with_file = temp_dir / "has_file"
    dir_with_file.mkdir()
    (dir_with_file / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Directory with file should still exist
    assert dir_with_file.exists()
    assert purger.stats["empty_dirs_deleted"] == 0


@pytest.mark.asyncio
async def test_dir_with_subdirs_not_removed(temp_dir):
    """Test that directories with non-empty subdirectories are not removed."""
    # Create directory with subdirectory that has a file
    dir_with_subdir = temp_dir / "has_subdir"
    subdir = dir_with_subdir / "subdir"
    subdir.mkdir(parents=True)
    (subdir / "file.txt").write_text("content")  # Subdir has a file, so not empty

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Directory with non-empty subdir should still exist
    assert dir_with_subdir.exists()
    assert subdir.exists()
    # No empty dirs should be deleted
    assert purger.stats["empty_dirs_deleted"] == 0


@pytest.mark.asyncio
async def test_dir_with_empty_subdirs_removed(temp_dir):
    """Test that directories containing only empty subdirectories are removed."""
    # Create directory with empty subdirectory
    dir_with_subdir = temp_dir / "has_subdir"
    subdir = dir_with_subdir / "subdir"
    subdir.mkdir(parents=True)  # Both are empty

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Both should be deleted (subdir first, then parent)
    assert not subdir.exists()
    assert not dir_with_subdir.exists()
    assert purger.stats["empty_dirs_deleted"] == 2


@pytest.mark.asyncio
async def test_dry_run_reports_empty_dirs(temp_dir):
    """Test that dry-run reports empty directories but doesn't delete them."""
    # Create empty directory
    empty_dir = temp_dir / "empty"
    empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,  # Dry run
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Directory should still exist (dry run)
    assert empty_dir.exists()
    # But should be counted
    assert purger.stats["empty_dirs_deleted"] == 1


@pytest.mark.asyncio
async def test_multiple_empty_dirs(temp_dir):
    """Test removal of multiple empty directories."""
    # Create multiple empty directories
    for i in range(5):
        (temp_dir / f"empty{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # All empty directories should be deleted
    assert purger.stats["empty_dirs_deleted"] == 5
    for i in range(5):
        assert not (temp_dir / f"empty{i}").exists()
