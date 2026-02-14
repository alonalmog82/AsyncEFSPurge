"""Tests for race conditions in empty directory removal."""

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

    # Run purge - Phase 1 will detect and delete all empty dirs
    await purger.purge()

    # All 5 empty dirs should be deleted (c, e, b, d, a) by Phase 1
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

    await purger.purge()

    # Empty dir should be deleted
    assert not empty_dir.exists()
    # Root should be preserved (even if paths differ in representation)
    assert temp_dir.exists()


@pytest.mark.asyncio
async def test_cascading_deletion_no_duplicates(temp_dir):
    """Test that cascading deletion doesn't process directories twice.

    Uses purge() to run the full pipeline (Phase 1 handles nested empty dirs
    with level-by-level bottom-up deletion, which is the correct code path for
    deeply nested empty directory trees). Phase 3 (_remove_empty_directories)
    processes concurrently and is best-effort for cascading — not suitable for
    testing strict nested deletion ordering.
    """
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

    await purger.purge()

    # All 5 nested dirs should be deleted (e, d, c, b, a) by Phase 1
    assert purger.stats["empty_dirs_deleted"] == 5
    assert not deep_dir.exists()
    assert not (temp_dir / "a").exists()


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
    await purger1.purge()
    assert temp_dir.exists()  # Root preserved
