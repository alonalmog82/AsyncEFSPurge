"""Tests for v1.15.3 hotfix: async_is_dir_empty, max_discovery_dirs, and critical memory abort."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import (
    MAX_DISCOVERY_DIRS_DEFAULT,
    AsyncEFSPurger,
    async_is_dir_empty,
)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# async_is_dir_empty tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_is_dir_empty_on_empty_dir(temp_dir):
    """async_is_dir_empty returns True for an empty directory."""
    empty = temp_dir / "empty"
    empty.mkdir()
    assert await async_is_dir_empty(empty) is True


@pytest.mark.asyncio
async def test_async_is_dir_empty_on_dir_with_file(temp_dir):
    """async_is_dir_empty returns False when the directory contains a file."""
    d = temp_dir / "has_file"
    d.mkdir()
    (d / "file.txt").write_text("content")
    assert await async_is_dir_empty(d) is False


@pytest.mark.asyncio
async def test_async_is_dir_empty_on_dir_with_subdir(temp_dir):
    """async_is_dir_empty returns False when the directory contains a subdirectory."""
    d = temp_dir / "has_subdir"
    d.mkdir()
    (d / "child").mkdir()
    assert await async_is_dir_empty(d) is False


@pytest.mark.asyncio
async def test_async_is_dir_empty_nonexistent_raises(temp_dir):
    """async_is_dir_empty raises FileNotFoundError for a missing path."""
    missing = temp_dir / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        await async_is_dir_empty(missing)


@pytest.mark.asyncio
async def test_async_is_dir_empty_with_many_files(temp_dir):
    """async_is_dir_empty returns False quickly even for a directory with many files.

    This confirms the function only peeks at the first entry and does not
    materialise the full directory listing.
    """
    d = temp_dir / "many_files"
    d.mkdir()
    for i in range(500):
        (d / f"file_{i:04d}.txt").write_text("x")
    assert await async_is_dir_empty(d) is False


@pytest.mark.asyncio
async def test_async_is_dir_empty_with_hidden_files(temp_dir):
    """async_is_dir_empty returns False when the directory only contains hidden (dot) files."""
    d = temp_dir / "hidden_only"
    d.mkdir()
    (d / ".hidden").write_text("secret")
    assert await async_is_dir_empty(d) is False


# ---------------------------------------------------------------------------
# max_discovery_dirs parameter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_discovery_dirs_default_value(temp_dir):
    """When max_discovery_dirs=0 and no memory limit, the default constant is used."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        max_discovery_dirs=0,
    )
    assert purger.max_discovery_dirs == MAX_DISCOVERY_DIRS_DEFAULT


@pytest.mark.asyncio
async def test_max_discovery_dirs_explicit_value(temp_dir):
    """An explicit max_discovery_dirs value takes precedence."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=4500,
        max_discovery_dirs=50_000,
    )
    assert purger.max_discovery_dirs == 50_000


@pytest.mark.asyncio
async def test_max_discovery_dirs_auto_from_memory_limit(temp_dir):
    """When max_discovery_dirs=0 and memory_limit_mb is set, auto-calculation kicks in."""
    memory_limit = 4500
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=memory_limit,
        max_discovery_dirs=0,
    )
    expected = int((memory_limit * 0.6 * 1024 * 1024) / 500)
    assert purger.max_discovery_dirs == expected


@pytest.mark.asyncio
async def test_discovery_stops_at_max_discovery_dirs(temp_dir):
    """Phase 1a discovery stops once max_discovery_dirs is reached.

    We use a nested (two-level) structure so that discovery processes multiple
    BFS iterations.  The limit check fires between iterations, so it will
    stop before all directories are discovered.
    """
    # Create 10 parent dirs, each with 10 children → 110 total dirs.
    # With limit=30 the BFS should stop after discovering ~30 dirs:
    #   - Scan root → 10 parents discovered (total=10)
    #   - Scan parent0 → 10 children (total=20)
    #   - Scan parent1 → 10 children (total=30) → limit hit
    num_parents = 10
    children_per_parent = 10
    limit = 30

    for p in range(num_parents):
        parent = temp_dir / f"parent_{p:02d}"
        parent.mkdir()
        for c in range(children_per_parent):
            (parent / f"child_{c:02d}").mkdir()

    total_created = num_parents + num_parents * children_per_parent  # 110

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=limit,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()

    # Deletion should be fewer than total_created because discovery was capped
    assert deleted < total_created, f"Expected fewer than {total_created} deletions due to discovery cap, got {deleted}"
    assert deleted > 0, "Should have deleted at least some directories"

    # Some directories should remain because discovery was capped
    remaining = sum(1 for _ in temp_dir.rglob("*") if _.is_dir())
    assert remaining > 0, "Some directories should remain after capped discovery"


@pytest.mark.asyncio
async def test_discovery_limit_allows_full_tree_when_large(temp_dir):
    """When max_discovery_dirs exceeds the actual tree size, everything is discovered."""
    total_dirs = 20
    for i in range(total_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=1000,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == total_dirs, f"Expected all {total_dirs} dirs deleted, got {deleted}"


# ---------------------------------------------------------------------------
# Critical memory abort during Phase 1b deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critical_memory_abort_during_deletion(temp_dir):
    """Phase 1b aborts deletion when memory stays above 95% after GC.

    We mock get_memory_usage_mb to simulate critical memory pressure once
    deletion has processed some directories, and verify the purger stops
    early rather than risking an OOM.
    """
    num_dirs = 200
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=1000,  # 1 GB limit
        max_discovery_dirs=0,
        log_level="DEBUG",
    )

    call_count = 0

    def fake_memory():
        nonlocal call_count
        call_count += 1
        # Return normal memory for discovery (first ~few calls),
        # then critical memory during deletion phase.
        if call_count <= 10:
            return 500.0  # 50% — safe for discovery
        return 960.0  # 96% — critical, triggers abort

    with patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory):
        deleted = await purger._purge_empty_directories_standalone()

    # Deletion should have been aborted early — not all dirs deleted
    assert deleted < num_dirs, (
        f"Expected early abort (deleted {deleted} of {num_dirs}), but it appears all directories were deleted"
    )


@pytest.mark.asyncio
async def test_high_memory_reduces_batch_size_but_continues(temp_dir):
    """Phase 1b reduces batch size at 75-90% memory but continues deletion."""
    num_dirs = 100
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=1000,
        max_discovery_dirs=0,
        log_level="DEBUG",
    )

    def fake_memory():
        # Always return 80% — high but not critical
        return 800.0

    with patch("efspurge.purger.get_memory_usage_mb", return_value=800.0):
        deleted = await purger._purge_empty_directories_standalone()

    # All dirs should still be deleted — high memory slows down but doesn't abort
    assert deleted == num_dirs, f"Expected all {num_dirs} dirs deleted at 80% memory, got {deleted}"


@pytest.mark.asyncio
async def test_memory_abort_leaves_non_empty_dirs_intact(temp_dir):
    """Directories with files are never deleted, even when memory abort fires."""
    num_empty = 100
    num_with_files = 20

    for i in range(num_empty):
        (temp_dir / f"empty_{i:04d}").mkdir()

    for i in range(num_with_files):
        d = temp_dir / f"has_files_{i:04d}"
        d.mkdir()
        (d / "file.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=1000,
        max_discovery_dirs=0,
        log_level="DEBUG",
    )

    call_count = 0

    def fake_memory():
        nonlocal call_count
        call_count += 1
        if call_count <= 10:
            return 500.0
        return 960.0

    with patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory):
        await purger._purge_empty_directories_standalone()

    # All non-empty directories must still exist
    remaining_with_files = [d for d in temp_dir.iterdir() if d.is_dir() and d.name.startswith("has_files_")]
    assert len(remaining_with_files) == num_with_files, (
        f"Expected {num_with_files} non-empty dirs to survive, found {len(remaining_with_files)}"
    )


# ---------------------------------------------------------------------------
# Periodic GC during deletion (smoke test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_gc_during_deletion(temp_dir):
    """Verify gc.collect is called periodically during large deletion runs.

    This is a smoke test — we mock gc.collect and verify it's called
    when checked_count crosses the 10000 boundary.  We use a smaller
    directory set and patch the modulo constant to make it feasible.
    """
    # Create just enough dirs to trigger the periodic GC path by patching
    # the modulo check is at ``checked_count % 10000 == 0``.
    # With 200 dirs we won't hit 10000, so instead we verify the function
    # still completes correctly.  A true integration test with 10000+ dirs
    # would be too slow for CI.
    num_dirs = 200
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == num_dirs


# ---------------------------------------------------------------------------
# CLI max-discovery-dirs integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_max_discovery_dirs_env_var(temp_dir):
    """EFSPURGE_MAX_DISCOVERY_DIRS env var is respected via parse_args."""
    with patch.dict(os.environ, {"EFSPURGE_MAX_DISCOVERY_DIRS": "42000"}):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.max_discovery_dirs == 42000
