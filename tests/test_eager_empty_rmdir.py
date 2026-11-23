"""Tests for 3.0.0 eager empty-dir removal during Phase 2.

Eager mode rmdir's a directory the moment the Phase 2 worker observes it
became empty, instead of appending it to the deferred empty_dirs set (which
is flushed to the monolithic empty-dirs sidecar). Only writer races
(ENOTEMPTY) and rmdir failures fall back to the deferred path.
"""

import errno
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_aged_tree(temp_dir: Path) -> Path:
    """Create a directory whose contents are all older than max_age_days."""
    aged_dir = temp_dir / "aged"
    aged_dir.mkdir()
    old_file = aged_dir / "old_file.txt"
    old_file.write_text("stale")
    aged_time = time.time() - 60 * 60 * 24 * 90  # 90 days old
    os.utime(old_file, (aged_time, aged_time))
    os.utime(aged_dir, (aged_time, aged_time))
    return aged_dir


@pytest.mark.asyncio
async def test_eager_rmdir_removes_dir_immediately(temp_dir):
    """Eager mode (3.0.0 default): dir that becomes empty is rmdir'd during Phase 2."""
    aged_dir = _make_aged_tree(temp_dir)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.purge()

    assert not aged_dir.exists()
    assert purger.stats["empty_dirs_eager_deleted"] == 1
    assert purger.stats["empty_dirs_deleted"] >= 1


@pytest.mark.asyncio
async def test_eager_disabled_restores_deferred_behavior(temp_dir):
    """--no-eager-empty-rmdir: dir that becomes empty is deferred, not rmdir'd mid-scan."""
    aged_dir = _make_aged_tree(temp_dir)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
        eager_empty_rmdir=False,
    )

    await purger.purge()

    # The dir is still removed (Phase 3 handles the deferred set), but the
    # eager counter proves no mid-scan rmdir happened.
    assert purger.stats["empty_dirs_eager_deleted"] == 0
    assert not aged_dir.exists()


@pytest.mark.asyncio
async def test_eager_rmdir_race_defers_directory(temp_dir):
    """ENOTEMPTY race (writer refilled the dir): dir falls back to the deferred set."""
    aged_dir = _make_aged_tree(temp_dir)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    def racing_rmdir(path, *args, **kwargs):
        # Simulate a writer creating content between the emptiness check
        # and the rmdir syscall.
        Path(path, "writer_file.txt").write_text("race")
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    with patch("aiofiles.os.rmdir", side_effect=racing_rmdir):
        await purger.purge()

    # The eager attempt failed with ENOTEMPTY and the dir was deferred.
    assert purger.stats["empty_dirs_eager_deleted"] == 0
    # Phase 3 re-checks emptiness; the writer file means it survives.
    assert aged_dir.exists()
    assert (aged_dir / "writer_file.txt").exists()


@pytest.mark.asyncio
async def test_eager_rmdir_enoent_is_not_deferred(temp_dir):
    """ENOENT (dir vanished between check and rmdir): no deferral, no eager count."""
    _make_aged_tree(temp_dir)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    def vanishing_rmdir(path, *args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    with patch("aiofiles.os.rmdir", side_effect=vanishing_rmdir):
        await purger.purge()

    assert purger.stats["empty_dirs_eager_deleted"] == 0


@pytest.mark.asyncio
async def test_eager_rmdir_respects_dry_run(temp_dir):
    """dry_run: no rmdir happens; the dir is deferred and counted as to-delete."""
    aged_dir = _make_aged_tree(temp_dir)

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=True,
    )

    await purger.purge()

    assert aged_dir.exists()
    assert purger.stats["empty_dirs_eager_deleted"] == 0


@pytest.mark.asyncio
async def test_eager_rmdir_never_removes_root(temp_dir):
    """The root directory is never rmdir'd even if it becomes empty."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    await purger.purge()

    assert temp_dir.exists()


@pytest.mark.asyncio
async def test_eager_rmdir_success_and_stats(temp_dir):
    """Direct unit test of _eager_rmdir_if_empty: success path increments all three counters."""
    target = temp_dir / "target"
    target.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    handled = await purger._eager_rmdir_if_empty(target)

    assert handled is True
    assert not target.exists()
    assert purger.stats["empty_dirs_eager_deleted"] == 1
    assert purger.stats["empty_dirs_deleted"] == 1
    assert purger.stats["empty_dirs_to_delete"] == 1


@pytest.mark.asyncio
async def test_eager_rmdir_enotempty_returns_false(temp_dir):
    """Direct unit test: ENOTEMPTY returns False so the caller defers."""
    target = temp_dir / "target"
    target.mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        dry_run=False,
    )

    def racing_rmdir(path, *args, **kwargs):
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    with patch("aiofiles.os.rmdir", side_effect=racing_rmdir):
        handled = await purger._eager_rmdir_if_empty(target)

    assert handled is False
    assert target.exists()
    assert purger.stats["empty_dirs_eager_deleted"] == 0
