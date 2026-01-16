"""Tests for logging behavior, including duplicate detection.

These tests verify that:
1. Progress logs are not duplicated
2. Empty directory removal logs appear when enabled
3. Empty directory removal logs don't appear when disabled
4. Startup log includes remove_empty_dirs setting

Note: These tests verify the code structure and behavior rather than
capturing actual log output, since logs go directly to stdout as JSON.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_no_duplicate_progress_logging_code(temp_dir):
    """Test that progress logging code doesn't have duplicate paths."""
    # Verify that update_stats() doesn't log progress
    # (progress is only logged by _background_progress_reporter)
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        log_level="INFO",
    )

    # Mock the logger to track calls
    log_calls = []

    def track_log(level, msg, **kwargs):
        log_calls.append((level, msg))

    purger.logger.info = Mock(side_effect=lambda msg, **kwargs: track_log("info", msg))

    # Call update_stats multiple times
    for _ in range(10):
        await purger.update_stats(files_scanned=1)

    # Should NOT have any "Progress update" logs from update_stats
    progress_logs = [call for call in log_calls if "Progress update" in str(call)]
    assert len(progress_logs) == 0, (
        "update_stats() should not log progress updates. Only _background_progress_reporter() should log progress."
    )


@pytest.mark.asyncio
async def test_empty_dir_removal_logs_when_enabled(temp_dir):
    """Test that empty directory removal logs when enabled."""
    # Create empty directories
    empty_dir1 = temp_dir / "empty1"
    empty_dir2 = temp_dir / "empty2"
    empty_dir1.mkdir()
    empty_dir2.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
        log_level="INFO",
    )

    # Mock logger to track calls
    log_calls = []

    def track_log(level, msg, **kwargs):
        log_calls.append((level, msg))

    purger.logger.info = Mock(side_effect=lambda msg, **kwargs: track_log("info", msg))

    # Run purge
    await purger.purge()

    # Check that empty directory removal logs were called
    messages = [str(call) for call in log_calls]
    assert any("Starting empty directory removal" in str(call) for call in log_calls), (
        f"Should log start of empty directory removal. Got: {messages}"
    )
    # Completion log only appears if there are cascading deletions
    # Progress log should appear after first pass
    assert any("Empty directory removal progress" in str(call) for call in log_calls), (
        f"Should log progress of empty directory removal. Got: {messages}"
    )


@pytest.mark.asyncio
async def test_empty_dir_removal_no_logs_when_disabled(temp_dir):
    """Test that empty directory removal logs don't appear when disabled."""
    # Create empty directories
    empty_dir = temp_dir / "empty"
    empty_dir.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=False,  # Disabled
        dry_run=True,
        log_level="INFO",
    )

    # Mock logger to track calls
    log_calls = []

    def track_log(level, msg, **kwargs):
        log_calls.append((level, msg))

    purger.logger.info = Mock(side_effect=lambda msg, **kwargs: track_log("info", msg))

    # Run purge
    await purger.purge()

    # Check for empty directory removal messages
    empty_dir_messages = [call for call in log_calls if "empty directory removal" in str(call).lower()]

    # Should have NO empty directory removal messages
    assert len(empty_dir_messages) == 0, (
        f"Should not log empty directory removal when disabled. Found: {empty_dir_messages}"
    )


@pytest.mark.asyncio
async def test_startup_log_includes_remove_empty_dirs(temp_dir):
    """Test that startup log includes remove_empty_dirs setting."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
        log_level="INFO",
    )

    # Mock logger to capture startup log
    startup_log_extra = None

    def capture_startup(msg, **kwargs):
        nonlocal startup_log_extra
        if "Starting EFS purge" in str(msg):
            startup_log_extra = kwargs.get("extra", {}).get("extra_fields", {})

    purger.logger.info = Mock(side_effect=capture_startup)

    # Run purge (will call log_with_context for startup)
    await purger.purge()

    assert startup_log_extra is not None, "Should have startup log"
    assert startup_log_extra.get("remove_empty_dirs") is True, (
        f"Startup log should include remove_empty_dirs=True. Got: {startup_log_extra}"
    )
    assert "version" in startup_log_extra, f"Startup log should include version. Got: {startup_log_extra}"
    assert startup_log_extra.get("version") is not None, (
        f"Startup log version should not be None. Got: {startup_log_extra}"
    )
