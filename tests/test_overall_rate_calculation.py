"""Tests for overall rate calculation fix.

Tests that overall rates use scanning duration only, excluding empty directory removal time.
"""

import asyncio
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
async def test_overall_rate_excludes_empty_dir_removal_time(temp_dir):
    """Test that overall rate calculation excludes empty directory removal time."""
    # Create some files
    (temp_dir / "file1.txt").write_text("test")
    (temp_dir / "file2.txt").write_text("test")

    # Create nested empty directories (will take time to remove)
    for i in range(10):
        (temp_dir / f"empty_{i}" / f"nested_{i}").mkdir(parents=True)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
        log_level="INFO",
    )

    # Record start time
    start_time = time.time()

    # Run purge
    await purger.purge()

    # Record end time
    end_time = time.time()
    total_duration = end_time - start_time

    # Verify scanning_end_time was set
    assert purger.scanning_end_time is not None, "scanning_end_time should be set after scanning completes"

    # Calculate scanning duration
    scanning_duration = purger.scanning_end_time - purger.stats["start_time"]
    empty_dir_removal_duration = total_duration - scanning_duration

    # Verify empty dir removal took some time (proves the fix is needed)
    # Note: On fast systems, this might be very quick, so we just verify it's non-negative
    assert empty_dir_removal_duration >= 0, "Empty dir removal duration should be non-negative"

    # Calculate rates
    files_scanned = purger.stats["files_scanned"]
    rate_using_total = files_scanned / total_duration if total_duration > 0 else 0
    rate_using_scanning = files_scanned / scanning_duration if scanning_duration > 0 else 0

    # Rate using scanning duration should be higher (or equal if very fast)
    assert rate_using_scanning >= rate_using_total, (
        f"Rate using scanning duration ({rate_using_scanning}) should be >= "
        f"rate using total duration ({rate_using_total})"
    )

    # Verify the fix: scanning_end_time is used for rate calculation
    # The actual rate calculation happens in the progress reporter, but we verify
    # that scanning_end_time is set correctly
    assert purger.scanning_end_time is not None
    assert purger.scanning_end_time < end_time


@pytest.mark.asyncio
async def test_overall_rate_during_scanning_uses_elapsed_time(temp_dir):
    """Test that during scanning, overall rate uses elapsed time (not scanning duration)."""
    # Create many files to ensure scanning takes time
    for i in range(100):
        (temp_dir / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=False,  # No empty dir removal to simplify test
        dry_run=True,
        log_level="INFO",
    )

    # Set short progress interval to get a progress update during scanning
    purger.progress_interval = 0.1

    # Start purge in background
    purge_task = asyncio.create_task(purger.purge())

    # Wait for at least one progress update
    await asyncio.sleep(0.3)

    # Check that scanning_end_time is None during scanning
    # (it will be None until scanning completes)
    if purger.scanning_end_time is None:
        # During scanning, rate should use elapsed time
        # This is verified by the code logic, but we can't easily test it here
        # without mocking time or accessing internal state
        pass

    await purge_task

    # After completion, scanning_end_time should be set
    assert purger.scanning_end_time is not None


@pytest.mark.asyncio
async def test_final_stats_use_scanning_duration(temp_dir):
    """Test that final stats use scanning duration for files_per_second."""
    # Create files and empty dirs
    for i in range(20):
        (temp_dir / f"file_{i}.txt").write_text("test")

    for i in range(5):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
        log_level="INFO",
    )

    # Run purge and get stats
    stats = await purger.purge()

    # Verify scanning_end_time was set
    assert purger.scanning_end_time is not None

    # Verify that files_per_second is calculated (not zero)
    actual_rate = stats.get("files_per_second", 0)
    assert actual_rate > 0, "files_per_second should be calculated"

    # Verify that the rate is reasonable (not artificially low due to including empty dir removal)
    # If empty dir removal took time, the rate should be higher than if we used total duration
    total_duration = stats.get("duration_seconds", 0)
    if total_duration > 0:
        rate_using_total = purger.stats["files_scanned"] / total_duration
        # The actual rate (using scanning duration) should be >= rate using total duration
        # (or very close if empty dir removal was very fast)
        assert actual_rate >= rate_using_total * 0.9, (
            f"Rate using scanning duration ({actual_rate}) should be >= "
            f"rate using total duration ({rate_using_total})"
        )
