"""Tests for memory safety during empty directory deletion.

CI-friendly versions use reduced scale (500–1000 dirs, lower concurrency).
High-scale stress tests (2k–11k dirs) are marked ``@pytest.mark.stress``
and run on-demand via the ``stress-tests`` workflow before releases.
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path

import psutil
import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# CI tests – lightweight versions that exercise the same code paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_dir_deletion_memory_bounded(temp_dir):
    """Memory stays bounded when deleting empty directories (CI-scale)."""
    num_dirs = 500
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=200,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    process = psutil.Process(os.getpid())
    initial_memory = process.memory_info().rss / 1024 / 1024
    peak_memory = initial_memory

    async def monitor_memory():
        nonlocal peak_memory
        while True:
            current = process.memory_info().rss / 1024 / 1024
            peak_memory = max(peak_memory, current)
            await asyncio.sleep(0.1)

    monitor_task = asyncio.create_task(monitor_memory())
    try:
        await purger._purge_empty_directories_standalone()
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    assert purger.stats["empty_dirs_deleted"] == num_dirs

    memory_increase = peak_memory - initial_memory
    assert memory_increase < 300, (
        f"Memory increase ({memory_increase:.1f}MB) should be bounded. "
        f"Peak: {peak_memory:.1f}MB, Initial: {initial_memory:.1f}MB."
    )


@pytest.mark.asyncio
async def test_empty_dir_deletion_queue_memory_bounded(temp_dir):
    """Queue-based deletion keeps memory bounded (CI-scale)."""
    num_dirs = 500
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=200,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
        dry_run=False,
    )

    # Populate empty_dirs for queue-based _remove_empty_directories
    for i in range(num_dirs):
        purger.empty_dirs.add(temp_dir / f"empty_{i:04d}")

    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    start_time = time.time()
    await purger._remove_empty_directories()
    deletion_time = time.time() - start_time

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    assert purger.stats["empty_dirs_deleted"] == num_dirs
    assert memory_increase < 200, f"Memory increase ({memory_increase:.1f}MB) suggests queue approach isn't working."
    assert deletion_time < 30, f"Deletion took {deletion_time:.2f}s, expected < 30s"


@pytest.mark.asyncio
async def test_empty_dir_deletion_memory_pressure_checks(temp_dir):
    """Memory pressure checks are triggered during deletion (CI-scale)."""
    num_dirs = 500
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=200,
        memory_limit_mb=200,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    # Populate empty_dirs for _remove_empty_directories (tests producer memory checks)
    for i in range(num_dirs):
        purger.empty_dirs.add(temp_dir / f"empty_{i:04d}")

    check_calls = []
    check_results = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        result = await original_check()
        check_calls.append(time.time())
        check_results.append(result)
        return result

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    assert purger.stats["empty_dirs_deleted"] == num_dirs

    # With 500 dirs we expect at least 50 memory checks
    assert len(check_calls) >= 50, f"Memory checks should be called many times, but was called {len(check_calls)} times"
    assert all(isinstance(r, tuple) and len(r) == 2 for r in check_results)
    assert all(isinstance(r[0], bool) and isinstance(r[1], (int, float)) for r in check_results)


@pytest.mark.asyncio
async def test_memory_checks_in_producer(temp_dir):
    """Memory checks happen in producer before adding to queue (CI-scale)."""
    num_dirs = 500
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=200,
        memory_limit_mb=500,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    # Populate empty_dirs for _remove_empty_directories (tests producer memory checks)
    for i in range(num_dirs):
        purger.empty_dirs.add(temp_dir / f"empty_{i:04d}")

    check_count = [0]
    check_timings = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        check_count[0] += 1
        check_timings.append(time.time())
        return await original_check()

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    assert purger.stats["empty_dirs_deleted"] == num_dirs
    assert check_count[0] > 50, f"Memory checks should be called many times, but was called {check_count[0]} times"
    if len(check_timings) > 1:
        assert check_timings[-1] - check_timings[0] > 0


@pytest.mark.asyncio
async def test_cascading_deletion_memory_bounded(temp_dir):
    """Cascading deletion doesn't cause memory explosion (CI-scale)."""
    # depth=3, width=5 → 155 directories (manageable for CI)
    depth = 3
    width = 5

    def create_nested(base, current_depth):
        if current_depth >= depth:
            return
        for i in range(width):
            subdir = base / f"dir_{i}"
            subdir.mkdir()
            create_nested(subdir, current_depth + 1)

    create_nested(temp_dir, 0)

    total_dirs = sum(1 for _ in temp_dir.rglob("*") if _.is_dir())

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=200,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    await purger.purge()

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    assert purger.stats["empty_dirs_deleted"] == total_dirs
    assert memory_increase < 300, f"Cascading deletion caused {memory_increase:.1f}MB increase, expected < 300MB"


@pytest.mark.asyncio
async def test_standalone_empty_dir_purger(temp_dir):
    """Phase 1 standalone purger deletes empty dirs and keeps non-empty ones."""
    num_empty = 200
    num_with_files = 50

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
        memory_limit_mb=800,
        log_level="INFO",
    )

    deleted = await purger._purge_empty_directories_standalone()

    assert deleted == num_empty, f"Expected {num_empty} empty dirs deleted, got {deleted}"

    remaining = [d for d in temp_dir.iterdir() if d.is_dir()]
    assert len(remaining) == num_with_files
    assert purger.stats["empty_dirs_deleted"] == num_empty


# ---------------------------------------------------------------------------
# Stress tests – original high-scale versions, run on-demand before releases
# ---------------------------------------------------------------------------


@pytest.mark.stress
@pytest.mark.asyncio
async def test_stress_large_scale_empty_dir_deletion_memory_bounded(temp_dir):
    """
    STRESS: Memory stays bounded when deleting 10k empty directories.

    Verifies the fix for memory explosion when deleting 100k+ empty directories.
    Before the fix, memory could grow from ~250MB to 1500MB+.
    """
    num_dirs = 10000

    print(f"\nCreating {num_dirs} empty directories...")
    start_create = time.time()
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:06d}").mkdir()
    create_time = time.time() - start_create
    print(f"Created {num_dirs} directories in {create_time:.2f}s")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    process = psutil.Process(os.getpid())
    initial_memory = process.memory_info().rss / 1024 / 1024
    print(f"Initial memory: {initial_memory:.1f}MB")

    # Populate empty_dirs for _remove_empty_directories (stress test of queue-based deletion)
    for i in range(num_dirs):
        purger.empty_dirs.add(temp_dir / f"empty_{i:06d}")

    memory_after_scan = process.memory_info().rss / 1024 / 1024
    print(f"Memory after scan: {memory_after_scan:.1f}MB")

    deletion_start = time.time()
    peak_memory = memory_after_scan
    memory_samples = []

    async def monitor_memory():
        nonlocal peak_memory
        while True:
            current = process.memory_info().rss / 1024 / 1024
            peak_memory = max(peak_memory, current)
            memory_samples.append(current)
            await asyncio.sleep(0.1)

    monitor_task = asyncio.create_task(monitor_memory())
    try:
        await purger._remove_empty_directories()
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    deletion_time = time.time() - deletion_start
    final_memory = process.memory_info().rss / 1024 / 1024

    print(f"Memory after deletion: {final_memory:.1f}MB")
    print(f"Peak memory during deletion: {peak_memory:.1f}MB")
    print(f"Deletion took: {deletion_time:.2f}s")
    print(f"Memory increase: {peak_memory - initial_memory:.1f}MB")

    assert purger.stats["empty_dirs_deleted"] == num_dirs

    memory_increase = peak_memory - initial_memory
    assert memory_increase < 300, (
        f"Memory increase ({memory_increase:.1f}MB) should be bounded. "
        f"Original bug showed 1309MB increase. Peak: {peak_memory:.1f}MB, Initial: {initial_memory:.1f}MB."
    )
    assert peak_memory < purger.memory_limit_mb * 1.5, (
        f"Peak memory ({peak_memory:.1f}MB) exceeded limit ({purger.memory_limit_mb}MB) by too much"
    )


@pytest.mark.stress
@pytest.mark.asyncio
async def test_stress_queue_memory_bounded(temp_dir):
    """STRESS: Queue-based deletion keeps memory bounded with 5k dirs."""
    num_dirs = 5000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        max_empty_dirs_to_delete=0,
        memory_limit_mb=800,
        dry_run=False,
    )

    for i in range(num_dirs):
        purger.empty_dirs.add(temp_dir / f"empty_{i:04d}")

    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    start_time = time.time()
    await purger._remove_empty_directories()
    deletion_time = time.time() - start_time

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    assert purger.stats["empty_dirs_deleted"] == num_dirs
    assert memory_increase < 200, f"Memory increase ({memory_increase:.1f}MB) suggests queue approach isn't working."
    assert deletion_time < 30, f"Deletion took {deletion_time:.2f}s"


@pytest.mark.stress
@pytest.mark.asyncio
async def test_stress_memory_pressure_checks(temp_dir):
    """STRESS: Memory pressure checks triggered with 5k dirs."""
    num_dirs = 5000
    for i in range(num_dirs):
        (temp_dir / f"empty_{i:04d}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=200,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    for i in range(num_dirs):
        purger.empty_dirs.add(temp_dir / f"empty_{i:04d}")

    check_calls = []
    check_results = []
    original_check = purger.check_memory_pressure

    async def tracked_check():
        result = await original_check()
        check_calls.append(time.time())
        check_results.append(result)
        return result

    purger.check_memory_pressure = tracked_check

    await purger._remove_empty_directories()

    assert purger.stats["empty_dirs_deleted"] == num_dirs
    expected_min_calls = min(num_dirs // 5, 1000)
    assert len(check_calls) >= expected_min_calls, (
        f"Expected >= {expected_min_calls} memory checks, got {len(check_calls)}"
    )
    assert all(isinstance(r, tuple) and len(r) == 2 for r in check_results)


@pytest.mark.stress
@pytest.mark.asyncio
async def test_stress_cascading_deletion_memory_bounded(temp_dir):
    """STRESS: Cascading deletion with 11k+ nested directories."""
    depth = 5
    width = 10

    def create_nested(base, current_depth):
        if current_depth >= depth:
            return
        for i in range(width):
            subdir = base / f"dir_{i}"
            subdir.mkdir()
            create_nested(subdir, current_depth + 1)

    create_nested(temp_dir, 0)

    total_dirs = sum(1 for _ in temp_dir.rglob("*") if _.is_dir())
    print(f"Created {total_dirs} nested directories")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        max_concurrency_deletion=1000,
        memory_limit_mb=800,
        max_empty_dirs_to_delete=0,
        dry_run=False,
    )

    process = psutil.Process(os.getpid())
    memory_before = process.memory_info().rss / 1024 / 1024

    await purger.purge()

    memory_after = process.memory_info().rss / 1024 / 1024
    memory_increase = memory_after - memory_before

    assert purger.stats["empty_dirs_deleted"] == total_dirs
    assert memory_increase < 300, f"Cascading deletion caused {memory_increase:.1f}MB increase, expected < 300MB"
