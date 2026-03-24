"""Tests for the sustained back-pressure checkpoint escape hatch.

When memory stabilises between the back-pressure threshold (85%) and the critical
checkpoint threshold (95%), the job can stall indefinitely. The
backpressure_checkpoint_timeout setting forces a checkpoint exit after the configured
number of seconds of continuous back-pressure, breaking the stall.
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import AsyncEFSPurger


def make_purger(tmp_path: Path, timeout: int = 600) -> AsyncEFSPurger:
    return AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=30,
        memory_limit_mb=1000,
        checkpoint_file=str(tmp_path / "checkpoint.json"),
        backpressure_checkpoint_timeout=timeout,
    )


@pytest.mark.asyncio
async def test_sustained_backpressure_sets_checkpoint_requested(tmp_path):
    """After backpressure_checkpoint_timeout seconds of back-pressure, _checkpoint_requested is set."""
    purger = make_purger(tmp_path, timeout=5)

    # Simulate memory just above back-pressure threshold (87% of 1000 MB = 870 MB)
    with patch("efspurge.purger.get_memory_usage_mb", return_value=870.0):
        # First call — starts the timer, no checkpoint yet
        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert not purger._checkpoint_requested
        assert purger._backpressure_start_time is not None

        # Fake the start time to be timeout+1 seconds ago
        purger._backpressure_start_time = time.time() - 6

        # Second call — timeout exceeded, should request checkpoint
        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert purger._checkpoint_requested


@pytest.mark.asyncio
async def test_sustained_backpressure_not_triggered_before_timeout(tmp_path):
    """_checkpoint_requested is NOT set before the timeout elapses."""
    purger = make_purger(tmp_path, timeout=300)

    with patch("efspurge.purger.get_memory_usage_mb", return_value=870.0):
        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert not purger._checkpoint_requested

        # Only 10 seconds have passed — well within the 300s timeout
        purger._backpressure_start_time = time.time() - 10

        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert not purger._checkpoint_requested


@pytest.mark.asyncio
async def test_sustained_backpressure_disabled_when_timeout_zero(tmp_path):
    """Setting backpressure_checkpoint_timeout=0 disables the sustained escape hatch."""
    purger = make_purger(tmp_path, timeout=0)

    with patch("efspurge.purger.get_memory_usage_mb", return_value=870.0):
        await purger.check_memory_pressure()
        purger._backpressure_start_time = time.time() - 9999  # Way past any reasonable timeout

        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert not purger._checkpoint_requested


@pytest.mark.asyncio
async def test_sustained_backpressure_disabled_without_checkpoint_file(tmp_path):
    """Sustained escape hatch is a no-op when no checkpoint_file is configured."""
    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=30,
        memory_limit_mb=1000,
        checkpoint_file=None,
        backpressure_checkpoint_timeout=5,
    )

    with patch("efspurge.purger.get_memory_usage_mb", return_value=870.0):
        await purger.check_memory_pressure()
        purger._backpressure_start_time = time.time() - 9999

        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert not purger._checkpoint_requested


@pytest.mark.asyncio
async def test_backpressure_timer_resets_when_memory_drops(tmp_path):
    """When memory drops below the back-pressure threshold the timer resets."""
    purger = make_purger(tmp_path, timeout=300)

    with patch("efspurge.purger.get_memory_usage_mb", return_value=870.0):
        await purger.check_memory_pressure()
        assert purger._backpressure_start_time is not None

    # Memory drops below the 85% threshold (850 MB)
    with patch("efspurge.purger.get_memory_usage_mb", return_value=800.0):
        await purger.check_memory_pressure()
        assert purger._backpressure_start_time is None


@pytest.mark.asyncio
async def test_critical_threshold_still_triggers_before_timeout(tmp_path):
    """The 95% critical checkpoint still fires even when the sustained timer hasn't elapsed."""
    purger = make_purger(tmp_path, timeout=600)

    # Memory at 96% (960 MB of 1000 MB limit) — above the critical 95% threshold
    with patch("efspurge.purger.get_memory_usage_mb", return_value=960.0):
        is_high, _ = await purger.check_memory_pressure()
        assert is_high
        assert purger._checkpoint_requested
