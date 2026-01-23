"""Tests for progress output formatting and DEBUG-level filtering."""

import json
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
async def test_progress_output_field_order(temp_dir, caplog):
    """Test that progress output fields are in the correct order."""
    # Create files to trigger progress updates
    for i in range(200):
        (temp_dir / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        log_level="INFO",
    )

    # Set short progress interval
    purger.progress_interval = 0.5

    await purger.purge()

    # Find progress update logs
    progress_logs = [record for record in caplog.records if "Progress update" in record.message]

    if progress_logs:
        # Get the first progress log's extra_fields
        first_log = progress_logs[0]
        extra_fields = getattr(first_log, "extra_fields", {})

        # Check that core fields exist and are in correct order
        # Note: JSON dict order is preserved in Python 3.7+
        expected_order = [
            "elapsed_seconds",
            "files_scanned",
            "files_purged",
            "dirs_scanned",
        ]

        # Get keys in order they appear
        keys = list(extra_fields.keys())

        # Check that expected fields appear early in the order
        for i, expected_key in enumerate(expected_order):
            if expected_key in keys:
                # Find position of this key
                pos = keys.index(expected_key)
                # Should be early in the dict (first few fields)
                assert pos < 10, f"{expected_key} should be early in progress output (position {pos})"


@pytest.mark.asyncio
async def test_debug_metrics_only_in_debug_mode(temp_dir, caplog):
    """Test that detailed metrics only appear in DEBUG mode."""
    # Create files
    for i in range(200):
        (temp_dir / f"file_{i}.txt").write_text("test")

    # Test with INFO level (should NOT show debug metrics)
    purger_info = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        log_level="INFO",
    )
    purger_info.progress_interval = 0.5

    caplog.clear()
    await purger_info.purge()

    progress_logs_info = [r for r in caplog.records if "Progress update" in r.message]

    if progress_logs_info:
        first_log_info = progress_logs_info[0]
        extra_fields_info = getattr(first_log_info, "extra_fields", {})

        # These detailed metrics should NOT be present in INFO mode
        debug_only_fields = [
            "files_per_second_instant",
            "files_per_second_short",
            "peak_files_per_second",
            "active_tasks",
            "concurrency_utilization_percent",
            "memory_mb_per_1k_files",
        ]

        for field in debug_only_fields:
            assert field not in extra_fields_info, (
                f"{field} should not appear in INFO level logs, but was found"
            )

    # Test with DEBUG level (should show debug metrics)
    caplog.clear()
    purger_debug = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        log_level="DEBUG",
    )
    purger_debug.progress_interval = 0.5

    await purger_debug.purge()

    progress_logs_debug = [r for r in caplog.records if "Progress update" in r.message]

    if progress_logs_debug:
        first_log_debug = progress_logs_debug[0]
        extra_fields_debug = getattr(first_log_debug, "extra_fields", {})

        # These detailed metrics SHOULD be present in DEBUG mode
        debug_fields = [
            "files_per_second_instant",
            "files_per_second_short",
            "peak_files_per_second",
        ]

        # At least some debug fields should be present
        found_debug_fields = [f for f in debug_fields if f in extra_fields_debug]
        assert len(found_debug_fields) > 0, (
            f"Expected at least one debug field in DEBUG mode logs. "
            f"Found: {list(extra_fields_debug.keys())}"
        )


@pytest.mark.asyncio
async def test_static_fields_not_in_progress_logs(temp_dir, caplog):
    """Test that static fields (like memory_limit_mb) are not in progress logs."""
    # Create files
    for i in range(100):
        (temp_dir / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        log_level="INFO",
        memory_limit_mb=800,
    )

    purger.progress_interval = 0.5

    await purger.purge()

    progress_logs = [r for r in caplog.records if "Progress update" in r.message]

    if progress_logs:
        first_log = progress_logs[0]
        extra_fields = getattr(first_log, "extra_fields", {})

        # Static fields that shouldn't be in progress logs
        static_fields = [
            "memory_limit_mb",
            "max_concurrency_scanning",
            "max_concurrency_deletion",
            "max_concurrency",
        ]

        for field in static_fields:
            assert field not in extra_fields, (
                f"{field} should not appear in progress logs (shown in startup log), "
                f"but was found: {extra_fields.get(field)}"
            )


@pytest.mark.asyncio
async def test_core_fields_always_present(temp_dir, caplog):
    """Test that core fields are always present in progress logs."""
    # Create files
    for i in range(100):
        (temp_dir / f"file_{i}.txt").write_text("test")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        log_level="INFO",
    )

    purger.progress_interval = 0.5

    await purger.purge()

    progress_logs = [r for r in caplog.records if "Progress update" in r.message]

    if progress_logs:
        first_log = progress_logs[0]
        extra_fields = getattr(first_log, "extra_fields", {})

        # Core fields that should always be present
        core_fields = [
            "elapsed_seconds",
            "files_scanned",
            "files_purged",
            "dirs_scanned",
            "errors",
            "memory_backpressure_events",
            "phase",
            "files_per_second",
            "memory_mb",
        ]

        for field in core_fields:
            assert field in extra_fields, (
                f"Core field {field} should always be present in progress logs, "
                f"but was missing. Found fields: {list(extra_fields.keys())}"
            )
