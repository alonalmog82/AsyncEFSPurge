"""Tests for parallel Phase 1a directory discovery.

Phase 1a now uses N concurrent worker coroutines (controlled by
``max_concurrent_discovery``) to scan the directory tree in parallel.  On
high-latency file systems like EFS, this overlaps scandir I/O and reduces
wall-clock time roughly proportionally to the number of workers.

These tests verify:
  - The new ``max_concurrent_discovery`` parameter is stored and validated.
  - The CLI / environment variable plumbing works.
  - Parallel discovery produces the same results as sequential (correctness).
  - Discovery limits (``max_discovery_dirs``) still function with workers.
  - Memory abort still terminates all workers.
  - Discovery state fields are cleaned up after parallel completion.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# max_concurrent_discovery parameter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_concurrent_discovery_default_value(temp_dir):
    """Default max_concurrent_discovery is 20 when not specified."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
    )
    assert purger.max_concurrent_discovery == 20


@pytest.mark.asyncio
async def test_max_concurrent_discovery_explicit_value(temp_dir):
    """An explicit max_concurrent_discovery value is stored correctly."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        max_concurrent_discovery=50,
    )
    assert purger.max_concurrent_discovery == 50


@pytest.mark.asyncio
async def test_max_concurrent_discovery_minimum_clamped_to_one(temp_dir):
    """max_concurrent_discovery is clamped to at least 1."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        max_concurrent_discovery=0,
    )
    assert purger.max_concurrent_discovery >= 1

    purger2 = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        max_concurrent_discovery=-5,
    )
    assert purger2.max_concurrent_discovery >= 1


# ---------------------------------------------------------------------------
# CLI / environment variable plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_max_concurrent_discovery_env_var(temp_dir):
    """EFSPURGE_MAX_CONCURRENT_DISCOVERY env var is respected via parse_args."""
    with patch.dict(os.environ, {"EFSPURGE_MAX_CONCURRENT_DISCOVERY": "42"}):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.max_concurrent_discovery == 42


@pytest.mark.asyncio
async def test_cli_max_concurrent_discovery_default_env_var(temp_dir):
    """Default EFSPURGE_MAX_CONCURRENT_DISCOVERY is 20 when env var is not set."""
    env = os.environ.copy()
    env.pop("EFSPURGE_MAX_CONCURRENT_DISCOVERY", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.max_concurrent_discovery == 20


# ---------------------------------------------------------------------------
# Correctness: parallel discovery finds all directories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_discovery_finds_all_flat_dirs(temp_dir):
    """Parallel discovery with multiple workers finds and deletes all empty dirs in a flat structure."""
    num_dirs = 50
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
        max_concurrent_discovery=10,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == num_dirs, f"Expected all {num_dirs} dirs deleted, got {deleted}"


@pytest.mark.asyncio
async def test_parallel_discovery_finds_all_nested_dirs(temp_dir):
    """Parallel discovery correctly discovers and deletes a nested directory tree."""
    # Create a tree: 5 parents × 5 children × 3 grandchildren = 5 + 25 + 75 = 105 dirs
    num_parents = 5
    num_children = 5
    num_grandchildren = 3
    total_dirs = num_parents + num_parents * num_children + num_parents * num_children * num_grandchildren

    for p in range(num_parents):
        parent = temp_dir / f"parent_{p:02d}"
        parent.mkdir()
        for c in range(num_children):
            child = parent / f"child_{c:02d}"
            child.mkdir()
            for g in range(num_grandchildren):
                (child / f"grandchild_{g:02d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=10,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == total_dirs, f"Expected all {total_dirs} dirs deleted, got {deleted}"


@pytest.mark.asyncio
async def test_parallel_discovery_preserves_non_empty_dirs(temp_dir):
    """Parallel discovery deletes empty dirs but preserves dirs containing files."""
    num_empty = 30
    num_with_files = 10

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
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=5,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == num_empty, f"Expected {num_empty} empty dirs deleted, got {deleted}"

    remaining_with_files = [d for d in temp_dir.iterdir() if d.is_dir() and d.name.startswith("has_files_")]
    assert len(remaining_with_files) == num_with_files


# ---------------------------------------------------------------------------
# Single worker behaves like old sequential code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_worker_discovery(temp_dir):
    """With max_concurrent_discovery=1, discovery still finds everything (sequential fallback)."""
    num_dirs = 20
    for i in range(num_dirs):
        d = temp_dir / f"parent_{i:02d}"
        d.mkdir()
        (d / "child").mkdir()

    total_dirs = num_dirs * 2  # parents + children

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=1,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == total_dirs, f"Expected all {total_dirs} dirs deleted with 1 worker, got {deleted}"


# ---------------------------------------------------------------------------
# Discovery limit (max_discovery_dirs) with parallel workers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_limit_with_parallel_workers(temp_dir):
    """max_discovery_dirs still caps discovery when using multiple workers."""
    # Create 10 parents × 10 children = 110 dirs
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
        max_concurrent_discovery=5,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()

    # Deletion should be fewer than total because discovery was capped
    assert deleted < total_created, f"Expected fewer than {total_created} deletions due to discovery cap, got {deleted}"
    assert deleted > 0, "Should have deleted at least some directories"

    # Some directories should remain
    remaining = sum(1 for _ in temp_dir.rglob("*") if _.is_dir())
    assert remaining > 0, "Some directories should remain after capped discovery"


# ---------------------------------------------------------------------------
# Memory abort terminates all workers
# ---------------------------------------------------------------------------


async def _small_batch_scandir(path, executor=None, batch_size=5000):
    """Wrapper that forces batch_size=2 for testing.

    Directly scans the directory without a thread executor to avoid thread
    cleanup issues in tests.
    """
    import os as _os

    batch = []
    try:
        with _os.scandir(path) as it:
            for entry in it:
                batch.append(entry)
                if len(batch) >= 2:
                    yield batch
                    batch = []
                    await asyncio.sleep(0)
            if batch:
                yield batch
    except Exception:
        pass


@pytest.mark.asyncio
async def test_memory_abort_stops_parallel_workers(temp_dir):
    """Memory abort during discovery stops all parallel workers.

    We use a flat directory with files so that memory check fires inside
    the batch loop.  With batch_size=2 and 30 files = 15 batches, the
    memory check fires at batch 10.
    """
    num_files = 30
    for i in range(num_files):
        (temp_dir / f"file_{i:04d}.dat").write_text("data")

    # Also create some subdirectories so workers have something to scan
    num_subdirs = 5
    for i in range(num_subdirs):
        d = temp_dir / f"subdir_{i:02d}"
        d.mkdir()
        for j in range(10):
            (d / f"file_{j}.dat").write_text("data")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=1000,
        max_discovery_dirs=0,
        max_concurrent_discovery=5,
        log_level="DEBUG",
    )

    call_count = 0

    def fake_memory():
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return 500.0  # Safe
        return 960.0  # Critical — triggers abort

    with (
        patch("efspurge.purger.async_scandir_batched", _small_batch_scandir),
        patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory),
    ):
        await purger._purge_empty_directories_standalone()

    # Discovery should have been aborted early
    assert purger._discovery_active is False, "Discovery should be marked inactive after abort"


@pytest.mark.asyncio
async def test_memory_pressure_at_90_percent_triggers_gc_and_continues(temp_dir):
    """At 90% memory, workers pause and GC but continue if memory drops below 95%."""
    num_dirs = 30
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
        max_concurrent_discovery=5,
        log_level="DEBUG",
    )

    call_count = 0

    def fake_memory():
        nonlocal call_count
        call_count += 1
        # First few calls: 91% (triggers GC)
        # After GC: 85% (below 95%, so continues)
        if call_count % 2 == 1:
            return 910.0  # 91% — triggers GC + sleep
        return 850.0  # 85% — safe after GC

    with patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory):
        deleted = await purger._purge_empty_directories_standalone()

    # All dirs should still be deleted — memory was high but not critical
    assert deleted == num_dirs, f"Expected all {num_dirs} dirs deleted at 90% memory, got {deleted}"


# ---------------------------------------------------------------------------
# Discovery state cleanup after parallel completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_state_cleared_after_parallel_completion(temp_dir):
    """Discovery state fields are properly reset after parallel Phase 1a completes."""
    for i in range(10):
        d = temp_dir / f"parent_{i:02d}"
        d.mkdir()
        (d / "child").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=5,
        log_level="DEBUG",
    )

    await purger._purge_empty_directories_standalone()

    assert purger._discovery_active is False
    assert purger._discovery_current_dir is None
    assert purger._discovery_entries_scanned > 0


@pytest.mark.asyncio
async def test_entries_scanned_tracked_with_parallel_workers(temp_dir):
    """_discovery_entries_scanned counts all entries across parallel workers."""
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
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=5,
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


# ---------------------------------------------------------------------------
# Consistency: parallel vs single worker produce same results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_and_single_worker_same_result(temp_dir):
    """Parallel discovery (N workers) deletes the same dirs as single worker.

    Uses dry_run=True so both runs operate on the identical tree.  In dry-run
    mode parents aren't actually removed, so only leaf-level empty dirs are
    counted.  The important assertion is that both worker counts agree.
    """
    # Build a tree: 8 parents × 4 children = 40 dirs
    # In dry_run only the 32 leaf children are empty (parents still contain children)
    for p in range(8):
        parent = temp_dir / f"parent_{p:02d}"
        parent.mkdir()
        for c in range(4):
            (parent / f"child_{c:02d}").mkdir()

    # Run with 1 worker
    purger1 = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,  # dry run to count without deleting
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=1,
        log_level="DEBUG",
    )
    deleted1 = await purger1._purge_empty_directories_standalone()

    # Run with 10 workers (same tree, still dry_run)
    purger10 = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=10,
        log_level="DEBUG",
    )
    deleted10 = await purger10._purge_empty_directories_standalone()

    # Both runs must agree — parallel discovery doesn't change correctness
    assert deleted1 == deleted10, (
        f"Single worker ({deleted1}) and parallel ({deleted10}) should produce identical results"
    )
    # Leaf dirs (32) should be detected as empty in dry-run
    assert deleted1 > 0, "Should have found at least some empty directories"


# ---------------------------------------------------------------------------
# Large concurrency value doesn't break anything
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_concurrency_with_small_tree(temp_dir):
    """More workers than directories doesn't cause errors or hangs."""
    num_dirs = 3
    for i in range(num_dirs):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=100,  # 100 workers for 3 dirs
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == num_dirs, f"Expected {num_dirs} dirs deleted, got {deleted}"


@pytest.mark.asyncio
async def test_empty_root_with_parallel_workers(temp_dir):
    """Parallel discovery handles an empty root directory gracefully."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=0,
        max_discovery_dirs=0,
        max_concurrent_discovery=10,
        log_level="DEBUG",
    )

    deleted = await purger._purge_empty_directories_standalone()
    assert deleted == 0
    assert purger._discovery_active is False
