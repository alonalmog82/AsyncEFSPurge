"""Tests for scandir executor diagnostics (DEBUG level only)."""

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
async def test_diagnostics_not_logged_at_info_level(temp_dir, caplog):
    """Test that scandir executor diagnostics are NOT logged at INFO level."""
    # Create directory structure
    for i in range(50):
        (temp_dir / f"dir_{i}").mkdir()
        (temp_dir / f"dir_{i}" / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        log_level="INFO",
        max_concurrent_subdirs=100,
    )

    await purger.purge()

    # Check that diagnostics are NOT logged at INFO level
    diagnostic_logs = [r for r in caplog.records if "scandir executor diagnostics" in r.message]
    assert len(diagnostic_logs) == 0, (
        f"Diagnostics should NOT be logged at INFO level. Found {len(diagnostic_logs)} diagnostic logs"
    )


@pytest.mark.asyncio
async def test_diagnostics_logged_at_debug_level(temp_dir, caplog):
    """Test that scandir executor diagnostics ARE logged at DEBUG level."""
    # Create directory structure
    for i in range(100):
        (temp_dir / f"dir_{i}").mkdir()
        (temp_dir / f"dir_{i}" / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        log_level="DEBUG",
        max_concurrent_subdirs=100,
    )

    await purger.purge()

    # Check that diagnostics ARE logged at DEBUG level (should log at end even if interval hasn't passed)
    diagnostic_logs = [r for r in caplog.records if "scandir executor diagnostics" in r.message]
    assert len(diagnostic_logs) > 0, (
        f"Diagnostics should be logged at DEBUG level (at least at end). Found {len(diagnostic_logs)} diagnostic logs"
    )

    # Verify diagnostic log structure
    if diagnostic_logs:
        log = diagnostic_logs[0]
        extra_fields = getattr(log, "extra_fields", {})
        assert "total_calls" in extra_fields, "Diagnostics should include total_calls"
        assert "avg_time_ms" in extra_fields, "Diagnostics should include avg_time_ms"
        assert "calls_per_sec" in extra_fields, "Diagnostics should include calls_per_sec"
        assert "executor_threads_total" in extra_fields, "Diagnostics should include executor_threads_total"
        assert "executor_threads_active_estimate" in extra_fields, (
            "Diagnostics should include executor_threads_active_estimate"
        )
        assert "utilization_percent" in extra_fields, "Diagnostics should include utilization_percent"
        assert "dirs_per_thread_per_sec" in extra_fields, "Diagnostics should include dirs_per_thread_per_sec"

        # Verify values are reasonable
        assert extra_fields["total_calls"] > 0, "total_calls should be > 0"
        assert extra_fields["executor_threads_total"] > 0, "executor_threads_total should be > 0"
        assert 0 <= extra_fields["utilization_percent"] <= 100, "utilization_percent should be 0-100"


@pytest.mark.asyncio
async def test_diagnostics_metrics_accumulate(temp_dir, caplog):
    """Test that diagnostics metrics accumulate correctly."""
    # Create many directories to ensure multiple diagnostic logs
    for i in range(200):
        (temp_dir / f"dir_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        log_level="DEBUG",
        max_concurrent_subdirs=100,
    )

    # Set a shorter diagnostics interval to potentially get multiple logs
    purger.scandir_diagnostics_interval = 0.1

    await purger.purge()

    # Get all diagnostic logs
    diagnostic_logs = [r for r in caplog.records if "scandir executor diagnostics" in r.message]

    # Should have at least one log (at the end)
    assert len(diagnostic_logs) > 0, "Should have at least one diagnostic log"

    if len(diagnostic_logs) >= 2:
        # Verify that total_calls increases over time
        first_log = diagnostic_logs[0]
        last_log = diagnostic_logs[-1]

        first_calls = getattr(first_log, "extra_fields", {}).get("total_calls", 0)
        last_calls = getattr(last_log, "extra_fields", {}).get("total_calls", 0)

        assert last_calls >= first_calls, (
            f"total_calls should increase over time. First: {first_calls}, Last: {last_calls}"
        )


@pytest.mark.asyncio
async def test_diagnostics_dont_break_normal_operation(temp_dir):
    """Test that diagnostics don't interfere with normal purge operation."""
    # Create directory structure
    for i in range(50):
        (temp_dir / f"dir_{i}").mkdir()
        (temp_dir / f"dir_{i}" / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        log_level="DEBUG",  # Enable diagnostics
        max_concurrent_subdirs=100,
    )

    # Run purge - should complete without errors
    stats = await purger.purge()

    # Verify normal operation
    assert stats["dirs_scanned"] > 0, "Should scan directories"
    assert stats["files_scanned"] > 0, "Should scan files"

    # Verify diagnostics tracked calls correctly
    # scandir_call_count should match dirs_scanned (one call per directory scanned)
    assert purger.scandir_call_count > 0, "Should have tracked scandir calls"
    assert purger.scandir_call_count == stats["dirs_scanned"], (
        f"scandir_call_count ({purger.scandir_call_count}) should match dirs_scanned ({stats['dirs_scanned']})"
    )

    # Verify total_time was tracked
    assert purger.scandir_total_time > 0, "Should have tracked total scandir time"


@pytest.mark.asyncio
async def test_diagnostics_initialized_correctly(temp_dir):
    """Test that diagnostics variables are initialized correctly."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        log_level="DEBUG",
        max_concurrent_subdirs=100,
    )

    # Verify diagnostics are initialized
    assert purger.scandir_call_count == 0, "scandir_call_count should start at 0"
    assert purger.scandir_total_time == 0.0, "scandir_total_time should start at 0.0"
    assert purger.scandir_diagnostics_interval == 10.0, "scandir_diagnostics_interval should default to 10.0"
    assert hasattr(purger, "scandir_lock"), "Should have scandir_lock"
    assert hasattr(purger, "scandir_executor"), "Should have scandir_executor"
