"""Tests for EACCES retry + throttle behavior (issue #53)."""

import errno
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import (
    EACCES_RETRY_BACKOFFS,
    AsyncEFSPurger,
    async_rmdir_with_eacces_retry,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _make_eacces_error() -> OSError:
    e = OSError(errno.EACCES, "Permission denied")
    e.errno = errno.EACCES
    return e


def _make_enoent_error() -> OSError:
    e = OSError(errno.ENOENT, "No such file or directory")
    e.errno = errno.ENOENT
    return e


# ---------- retry helper ----------


@pytest.mark.asyncio
async def test_rmdir_retry_returns_1_on_first_try_success(temp_dir):
    """First-attempt success returns 1 and does not sleep."""
    d = temp_dir / "empty"
    d.mkdir()
    with patch("efspurge.purger.asyncio.sleep") as mock_sleep:
        attempts = await async_rmdir_with_eacces_retry(d)
    assert attempts == 1
    assert not d.exists()
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_rmdir_retry_recovers_on_second_attempt():
    """EACCES on first attempt, success on second → returns 2."""
    calls = {"n": 0}

    async def fake_rmdir(_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_eacces_error()

    with (
        patch("efspurge.purger.aiofiles.os.rmdir", side_effect=fake_rmdir),
        patch("efspurge.purger.asyncio.sleep") as mock_sleep,
    ):
        attempts = await async_rmdir_with_eacces_retry(Path("/does/not/matter"))

    assert attempts == 2
    assert calls["n"] == 2
    # First retry sleeps EACCES_RETRY_BACKOFFS[0]
    mock_sleep.assert_awaited_once_with(EACCES_RETRY_BACKOFFS[0])


@pytest.mark.asyncio
async def test_rmdir_retry_all_attempts_fail_reraises_eacces():
    """Every attempt returns EACCES → raises the last OSError with errno=EACCES."""
    call_count = {"n": 0}

    async def always_eacces(_path):
        call_count["n"] += 1
        raise _make_eacces_error()

    with (
        patch("efspurge.purger.aiofiles.os.rmdir", side_effect=always_eacces),
        patch("efspurge.purger.asyncio.sleep"),
        pytest.raises(OSError) as excinfo,
    ):
        await async_rmdir_with_eacces_retry(Path("/does/not/matter"))

    assert excinfo.value.errno == errno.EACCES
    # Initial attempt + one retry per backoff step
    assert call_count["n"] == 1 + len(EACCES_RETRY_BACKOFFS)


@pytest.mark.asyncio
async def test_rmdir_retry_does_not_retry_non_eacces_errors():
    """ENOENT (or any non-EACCES OSError) is re-raised immediately, no retry."""
    call_count = {"n": 0}

    async def always_enoent(_path):
        call_count["n"] += 1
        raise _make_enoent_error()

    with (
        patch("efspurge.purger.aiofiles.os.rmdir", side_effect=always_enoent),
        patch("efspurge.purger.asyncio.sleep") as mock_sleep,
        pytest.raises(OSError) as excinfo,
    ):
        await async_rmdir_with_eacces_retry(Path("/does/not/matter"))

    assert excinfo.value.errno == errno.ENOENT
    assert call_count["n"] == 1  # no retry
    mock_sleep.assert_not_called()


# ---------- throttled EACCES logger ----------


def _make_purger(temp_dir):
    return AsyncEFSPurger(root_path=str(temp_dir), max_age_days=30)


@pytest.mark.asyncio
async def test_eacces_throttle_first_call_emits_warning(temp_dir, caplog):
    """First EACCES for a given label emits a WARNING summary."""
    import logging

    purger = _make_purger(temp_dir)
    caplog.set_level(logging.WARNING, logger="efspurge")

    await purger._log_eacces_throttled(
        "phase3.rmdir",
        "Could not remove empty directory",
        Path("/x"),
        _make_eacces_error(),
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "throttled" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_eacces_throttle_second_call_within_interval_no_warning(temp_dir, caplog):
    """Second EACCES within the throttle interval does NOT emit another WARNING."""
    import logging

    purger = _make_purger(temp_dir)
    caplog.set_level(logging.WARNING, logger="efspurge")

    await purger._log_eacces_throttled(
        "phase3.rmdir",
        "Could not remove empty directory",
        Path("/x"),
        _make_eacces_error(),
    )
    await purger._log_eacces_throttled(
        "phase3.rmdir",
        "Could not remove empty directory",
        Path("/y"),
        _make_eacces_error(),
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "expected only one throttled WARNING within the interval"


@pytest.mark.asyncio
async def test_eacces_throttle_counter_resets_after_summary(temp_dir):
    """After a summary WARNING fires, the per-label count resets to zero."""
    purger = _make_purger(temp_dir)

    await purger._log_eacces_throttled(
        "phase3.rmdir",
        "msg",
        Path("/x"),
        _make_eacces_error(),
    )
    # Warning fired → count reset
    assert purger._eacces_count_since_warning["phase3.rmdir"] == 0

    # Next call before interval elapses → count increments but no warning
    await purger._log_eacces_throttled(
        "phase3.rmdir",
        "msg",
        Path("/y"),
        _make_eacces_error(),
    )
    assert purger._eacces_count_since_warning["phase3.rmdir"] == 1


@pytest.mark.asyncio
async def test_eacces_throttle_labels_are_independent(temp_dir, caplog):
    """A WARNING for phase3.rmdir does not suppress a first WARNING for phase2.scan."""
    import logging

    purger = _make_purger(temp_dir)
    caplog.set_level(logging.WARNING, logger="efspurge")

    await purger._log_eacces_throttled("phase3.rmdir", "m1", Path("/x"), _make_eacces_error())
    await purger._log_eacces_throttled("phase2.scan", "m2", Path("/y"), _make_eacces_error())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_eacces_throttle_reports_count_since_last_warning(temp_dir, caplog):
    """After N suppressed occurrences, the next summary WARNING reports the accumulated count."""
    import logging

    purger = _make_purger(temp_dir)
    caplog.set_level(logging.WARNING, logger="efspurge")

    # First call → WARNING with count=1
    await purger._log_eacces_throttled("phase3.rmdir", "m", Path("/a"), _make_eacces_error())
    # Three more within the interval → suppressed
    for i in range(3):
        await purger._log_eacces_throttled("phase3.rmdir", "m", Path(f"/b{i}"), _make_eacces_error())

    # Force interval to elapse
    purger._eacces_last_warning_time["phase3.rmdir"] -= purger._eacces_warning_interval + 1

    # Next call → WARNING with count=4 (3 suppressed + 1 current)
    await purger._log_eacces_throttled("phase3.rmdir", "m", Path("/c"), _make_eacces_error())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    # The second warning should carry the accumulated count in its context
    last = warnings[-1]
    extra = getattr(last, "extra_fields", None) or {}
    assert extra.get("eacces_count_since_last_warning") == 4
