"""Tests for v1.15.4 hotfix: memory abort during Phase 1a discovery in flat directories.

In v1.15.3, the between-batch memory check inside async_scandir_batched was gated
on ``subdirs_added > 0``.  This meant that scanning a huge flat directory (millions
of files, zero subdirectories) would never trigger the memory safety valve, leading
to unbounded memory growth and eventual OOM.

v1.15.4 fixes this by:
  1. Removing the ``subdirs_added > 0`` gate — memory is now checked unconditionally.
  2. Checking every 10 batches (≈50 000 entries) to limit overhead.
  3. Tracking ``_discovery_entries_scanned`` for progress visibility.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import AsyncEFSPurger

# Capture a reference to the *original* function before any patches are applied.
# This lets _small_batch_scandir delegate to the real implementation with a
# reduced batch_size without infinite recursion when the module-level name is
# replaced by a mock.
from efspurge.purger import async_scandir_batched as _original_scandir_batched


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# Helper: force a tiny batch_size so we can trigger the every-10-batches
# memory check without creating tens of thousands of files.
# ---------------------------------------------------------------------------


async def _small_batch_scandir(path, executor=None, batch_size=5000):
    """Wrapper that forces batch_size=2 for testing."""
    async for batch in _original_scandir_batched(path, executor, batch_size=2):
        yield batch


# ---------------------------------------------------------------------------
# Phase 1a: memory abort in flat directories (the core bug fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_abort_during_discovery_flat_directory(temp_dir):
    """Phase 1a aborts when memory is critical during a flat-directory scan.

    Scenario reproduced from production:
      - Root directory contains only files (no subdirectories).
      - Memory grows during scanning but the old code never checked because
        ``subdirs_added`` stayed at 0.

    With the fix the memory check fires every 10 batches regardless of
    subdirectory count, and the abort triggers correctly.
    """
    # 30 files, zero subdirectories.  With batch_size=2 that gives 15 batches.
    # The memory check fires at batch 10 (10 % 10 == 0).
    num_files = 30
    for i in range(num_files):
        (temp_dir / f"file_{i:04d}.dat").write_text("data")

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
        # Calls 1-2: initial log message + outer-loop memory check → safe.
        if call_count <= 2:
            return 500.0  # 50% — well below threshold
        # All subsequent calls (between-batch checks) → critical.
        return 960.0  # 96% — triggers abort after GC retry

    with (
        patch("efspurge.purger.async_scandir_batched", _small_batch_scandir),
        patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory),
    ):
        await purger._purge_empty_directories_standalone()

    # Abort at batch 10 means only 20 of 30 entries were scanned.
    assert purger._discovery_entries_scanned < num_files, (
        f"Expected early abort (scanned {purger._discovery_entries_scanned} of "
        f"{num_files}), but all entries were processed — memory abort did not "
        f"fire during flat directory scan"
    )
    assert purger._discovery_entries_scanned == 20, (
        f"Expected exactly 20 entries scanned (10 batches × 2), got {purger._discovery_entries_scanned}"
    )


@pytest.mark.asyncio
async def test_memory_abort_during_discovery_prevents_further_bfs(temp_dir):
    """Memory abort during root scan stops the entire BFS — queued dirs are skipped.

    Root has files *and* subdirectories.  Each subdirectory has children of its
    own.  When the abort fires during the root scan, the subdirectories that
    were already discovered sit in the queue but are never visited, so the
    grandchildren are never discovered or deleted.
    """
    # 30 files + 10 subdirs in root → 40 entries, 20 batches at batch_size=2.
    # Memory check fires at batch 10, aborting part-way through root scan.
    num_root_files = 30
    num_subdirs = 10
    children_per_subdir = 5

    for i in range(num_root_files):
        (temp_dir / f"file_{i:04d}.dat").write_text("data")
    for i in range(num_subdirs):
        parent = temp_dir / f"subdir_{i:02d}"
        parent.mkdir()
        for j in range(children_per_subdir):
            (parent / f"child_{j:02d}").mkdir()

    total_dirs = num_subdirs + num_subdirs * children_per_subdir  # 60

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
        if call_count <= 2:
            return 500.0
        return 960.0

    with (
        patch("efspurge.purger.async_scandir_batched", _small_batch_scandir),
        patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory),
    ):
        deleted = await purger._purge_empty_directories_standalone()

    # The abort during root scan means:
    #   - Only some of the 10 subdirectories were discovered (those in first 10 batches).
    #   - None of the 50 grandchildren were discovered because the BFS stopped.
    #   - Therefore far fewer than 60 directories could be deleted.
    assert deleted < total_dirs, (
        f"Expected early abort to limit deletions (deleted {deleted} of {total_dirs} dirs), but all were processed"
    )


# ---------------------------------------------------------------------------
# _discovery_entries_scanned tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entries_scanned_tracked_during_normal_discovery(temp_dir):
    """_discovery_entries_scanned counts all entries (files + dirs) during discovery."""
    num_dirs = 5
    files_per_dir = 3
    num_root_files = 4

    for i in range(num_dirs):
        d = temp_dir / f"subdir_{i:02d}"
        d.mkdir()
        for j in range(files_per_dir):
            (d / f"file_{j}.txt").write_text("data")

    for i in range(num_root_files):
        (temp_dir / f"root_file_{i}.txt").write_text("data")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,  # no memory limit
        max_discovery_dirs=0,
        log_level="DEBUG",
    )

    await purger._purge_empty_directories_standalone()

    # Root scan:    5 dirs + 4 files  =  9 entries
    # Subdir scans: 5 × 3 files      = 15 entries
    # Total:                           24 entries
    expected = num_dirs + num_root_files + num_dirs * files_per_dir
    assert purger._discovery_entries_scanned == expected, (
        f"Expected {expected} entries scanned, got {purger._discovery_entries_scanned}"
    )


@pytest.mark.asyncio
async def test_discovery_state_cleared_after_completion(temp_dir):
    """Discovery state fields are properly reset once Phase 1a finishes."""
    for i in range(5):
        (temp_dir / f"empty_{i}").mkdir()

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

    await purger._purge_empty_directories_standalone()

    assert purger._discovery_active is False
    assert purger._discovery_current_dir is None
    assert purger._discovery_entries_scanned > 0
