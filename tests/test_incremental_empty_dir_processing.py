"""
Tests for the standalone two-pass empty directory processing feature.

Phase 1 (standalone): Efficient iterative BFS + bottom-up walker that discovers
and deletes empty directories BEFORE file scanning.
Phase 2: Normal file scanning and purging.
Phase 3: Post-scan cleanup of directories that became empty after file purging.
"""

from pathlib import Path

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.mark.asyncio
async def test_standalone_purge_deletes_all_empty_dirs(tmp_path: Path) -> None:
    """Test that standalone purge deletes all empty directories."""
    num_dirs = 100
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,  # Unlimited
        memory_limit_mb=800,
    )

    stats = await purger.purge()

    # All empty dirs should be deleted
    assert stats["dirs_purged"] == num_dirs
    # No files should be processed (max_age_days=0)
    assert stats["files_scanned"] == 0


@pytest.mark.asyncio
async def test_standalone_purge_handles_nested_empty_dirs(tmp_path: Path) -> None:
    """Test that standalone purge handles nested empty directories correctly.

    Bottom-up processing means children are deleted before parents,
    allowing parent directories to become empty and be deleted too.
    """
    # Create nested structure:
    # parent/child1/ (empty)
    # parent/child2/grandchild1/ (empty)
    # parent/child2/grandchild2/ (empty)
    parent = tmp_path / "parent"
    child1 = parent / "child1"
    child2 = parent / "child2"
    grandchild1 = child2 / "grandchild1"
    grandchild2 = child2 / "grandchild2"

    child1.mkdir(parents=True)
    grandchild1.mkdir(parents=True)
    grandchild2.mkdir(parents=True)

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
        log_level="DEBUG",
    )

    stats = await purger.purge()

    # All 5 directories should be deleted (grandchild1, grandchild2, child1, child2, parent)
    assert stats["dirs_purged"] >= 3  # At least the leaf dirs

    # Leaf dirs should definitely be gone
    assert not child1.exists()
    assert not grandchild1.exists()
    assert not grandchild2.exists()


@pytest.mark.asyncio
async def test_standalone_purge_respects_rate_limit(tmp_path: Path) -> None:
    """Test that standalone purge respects max_empty_dirs_to_delete limit."""
    num_dirs = 100
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    max_to_delete = 30
    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=max_to_delete,
        memory_limit_mb=800,
    )

    stats = await purger.purge()

    # Should delete at most max_to_delete directories
    assert stats["dirs_purged"] <= max_to_delete
    assert stats["dirs_to_purge"] <= max_to_delete


@pytest.mark.asyncio
async def test_no_deletion_when_disabled(tmp_path: Path) -> None:
    """Test that no directories are deleted when remove_empty_dirs=False."""
    num_dirs = 100
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=False,
        memory_limit_mb=800,
    )

    stats = await purger.purge()

    # No directories should be deleted
    assert stats.get("dirs_purged", 0) == 0
    assert stats.get("dirs_to_purge", 0) == 0

    # All directories should still exist
    for i in range(num_dirs):
        assert (tmp_path / f"empty_{i:03d}").exists()


@pytest.mark.asyncio
async def test_standalone_purge_with_dry_run(tmp_path: Path) -> None:
    """Test that standalone purge works correctly in dry-run mode."""
    num_dirs = 60
    for i in range(num_dirs):
        (tmp_path / f"empty_{i:03d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=True,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    stats = await purger.purge()

    # In dry run, dirs should be counted but not actually deleted
    assert stats.get("dirs_to_purge", 0) == num_dirs
    assert stats.get("empty_dirs_deleted", 0) == 0 or stats.get("dirs_purged", 0) == 0

    # All directories should still exist (dry run)
    for i in range(num_dirs):
        assert (tmp_path / f"empty_{i:03d}").exists()


@pytest.mark.asyncio
async def test_standalone_purge_skips_non_empty_dirs(tmp_path: Path) -> None:
    """Test that standalone purge skips directories that contain files."""
    # Create mix of empty and non-empty directories
    num_empty = 50
    num_non_empty = 50

    for i in range(num_empty):
        (tmp_path / f"empty_{i:03d}").mkdir()

    for i in range(num_non_empty):
        d = tmp_path / f"has_file_{i:03d}"
        d.mkdir()
        (d / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    stats = await purger.purge()

    # Only empty dirs should be deleted
    assert stats["dirs_purged"] == num_empty

    # Non-empty dirs should still exist
    for i in range(num_non_empty):
        assert (tmp_path / f"has_file_{i:03d}").exists()


@pytest.mark.asyncio
async def test_standalone_purge_never_deletes_root(tmp_path: Path) -> None:
    """Test that standalone purge never deletes the root directory."""
    # Create a single empty subdir so discovery finds something
    (tmp_path / "subdir").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
    )

    await purger.purge()

    # Root should always exist
    assert tmp_path.exists()
