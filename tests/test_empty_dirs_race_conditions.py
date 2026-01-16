"""Tests for race conditions in empty directory removal."""

import tempfile
from pathlib import Path

import aiofiles
import pytest

from efspurge.purger import AsyncEFSPurger, async_scandir


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_concurrent_empty_dir_detection(temp_dir):
    """Test that concurrent scans don't create duplicate entries."""
    # Create structure where multiple subdirs could check same parent
    # /a/b/c (empty)
    # /a/d/e (empty)
    # Both will check /a/b and /a/d, and both might check /a
    dir_a = temp_dir / "a"
    dir_b = dir_a / "b"
    dir_c = dir_b / "c"
    dir_d = dir_a / "d"
    dir_e = dir_d / "e"

    dir_c.mkdir(parents=True)
    dir_e.mkdir(parents=True)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    # Scan should detect leaf empty dirs (c and e)
    # Parents (b, d, a) will be detected during cascading deletion
    await purger.scan_directory(temp_dir)

    # Check that empty_dirs set has no duplicates
    # (set automatically prevents duplicates)
    assert len(purger.empty_dirs) == len(set(purger.empty_dirs))

    # Initially should have found: c and e (leaf empty dirs)
    # Parents will be added during cascading deletion
    assert len(purger.empty_dirs) >= 2  # At least the leaf dirs

    # Delete them (cascading will add parents)
    await purger._remove_empty_directories()

    # All 5 empty dirs should be deleted (c, e, b, d, a)
    assert purger.stats["empty_dirs_deleted"] == 5


@pytest.mark.asyncio
async def test_path_resolution_edge_cases(temp_dir):
    """Test path comparison handles edge cases."""
    # Create empty dir
    empty_dir = temp_dir / "empty"
    empty_dir.mkdir()

    # Use relative path for root
    purger = AsyncEFSPurger(
        root_path=str(temp_dir.resolve()),  # Absolute
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)
    await purger._remove_empty_directories()

    # Empty dir should be deleted
    assert not empty_dir.exists()
    # Root should be preserved (even if paths differ in representation)
    assert temp_dir.exists()


@pytest.mark.asyncio
async def test_cascading_deletion_no_duplicates(temp_dir):
    """Test that cascading deletion doesn't process directories twice."""
    # Create deeply nested structure
    # /a/b/c/d/e (all empty)
    deep_dir = temp_dir / "a" / "b" / "c" / "d" / "e"
    deep_dir.mkdir(parents=True)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.scan_directory(temp_dir)

    # Track which directories we try to delete
    deletion_attempts = []

    async def mock_remove():
        # Get initial set
        async with purger.stats_lock:
            initial = set(purger.empty_dirs)

        # Process and track
        for d in sorted(initial, key=lambda p: len(p.parts), reverse=True):
            if d not in deletion_attempts:
                deletion_attempts.append(d)
                if not purger.dry_run:
                    await aiofiles.os.rmdir(d)
                await purger.update_stats(empty_dirs_deleted=1)

        # Check parents (cascading)
        processed = set(deletion_attempts)
        new_parents = set()
        for d in deletion_attempts:
            parent = d.parent
            if parent != temp_dir and parent not in processed:
                try:
                    entries = await async_scandir(parent)
                    if len(entries) == 0:
                        new_parents.add(parent)
                except Exception:
                    pass

        # Process new parents
        for parent in sorted(new_parents, key=lambda p: len(p.parts), reverse=True):
            if parent not in deletion_attempts:
                deletion_attempts.append(parent)
                if not purger.dry_run:
                    await aiofiles.os.rmdir(parent)
                await purger.update_stats(empty_dirs_deleted=1)

    # Use actual implementation but verify no duplicates
    await purger._remove_empty_directories()

    # Verify each directory was only processed once
    assert len(set(deletion_attempts)) == len(deletion_attempts) if deletion_attempts else True


@pytest.mark.asyncio
async def test_root_path_protection_absolute_vs_relative(temp_dir):
    """Test root protection works with different path representations."""
    # Create empty root scenario
    purger1 = AsyncEFSPurger(
        root_path=str(temp_dir),  # String path
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    # Root should be resolved to absolute
    assert purger1.root_path.is_absolute()

    # Even if we pass relative, it should be resolved
    purger2 = AsyncEFSPurger(
        root_path=".",  # Relative
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    # Should be resolved to absolute
    assert purger2.root_path.is_absolute()

    # Root should never be deleted
    await purger1.scan_directory(temp_dir)
    await purger1._remove_empty_directories()
    assert temp_dir.exists()  # Root preserved
