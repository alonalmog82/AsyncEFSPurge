"""Tests for queue_maxsize parameter (Phase 1a and Phase 2 directory queues).

The queue_maxsize parameter bounds the directory queues used in Phase 1a
(empty directory discovery) and Phase 2 (file scanning). When discovery
outpaces processing, producers block when the queue is full, preventing
unbounded memory growth.
"""

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
# queue_maxsize parameter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_maxsize_default_value(temp_dir):
    """Default queue_maxsize is 10000 when not specified."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
    )
    assert purger.queue_maxsize == 10000


@pytest.mark.asyncio
async def test_queue_maxsize_explicit_value(temp_dir):
    """An explicit queue_maxsize value is stored correctly."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        queue_maxsize=5000,
    )
    assert purger.queue_maxsize == 5000


@pytest.mark.asyncio
async def test_queue_maxsize_zero_unbounded(temp_dir):
    """queue_maxsize=0 means unbounded queue (backward compatible)."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        queue_maxsize=0,
    )
    assert purger.queue_maxsize == 0


def test_queue_maxsize_invalid_raises(temp_dir):
    """queue_maxsize < 0 raises ValueError."""
    with pytest.raises(ValueError, match="queue_maxsize must be >= 0"):
        AsyncEFSPurger(
            root_path=str(temp_dir),
            max_age_days=30,
            dry_run=True,
            queue_maxsize=-1,
        )


@pytest.mark.asyncio
async def test_queue_maxsize_phase2_completes_with_bounded_queue(temp_dir):
    """Phase 2 file scanning completes successfully with bounded queue."""
    # Create a directory tree: root -> a -> b -> c with files
    (temp_dir / "a" / "b" / "c").mkdir(parents=True)
    (temp_dir / "a" / "file1.txt").write_text("content")
    (temp_dir / "a" / "b" / "file2.txt").write_text("content")
    (temp_dir / "a" / "b" / "c" / "file3.txt").write_text("content")

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        queue_maxsize=10,  # Small queue to exercise back-pressure
    )

    await purger.purge()

    assert purger.stats["dirs_scanned"] >= 3
    assert purger.stats["files_scanned"] >= 3


@pytest.mark.asyncio
async def test_queue_maxsize_phase1a_completes_with_bounded_queue(temp_dir):
    """Phase 1a empty directory discovery completes with bounded queue."""
    # Create nested empty directories
    for i in range(5):
        (temp_dir / f"empty_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=False,
        remove_empty_dirs=True,
        max_empty_dirs_to_delete=0,
        queue_maxsize=10,
    )

    await purger.purge()

    assert purger.stats["empty_dirs_deleted"] >= 5


# ---------------------------------------------------------------------------
# CLI / environment variable plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_queue_maxsize_env_var(temp_dir):
    """EFSPURGE_QUEUE_MAXSIZE env var is respected via parse_args."""
    with patch.dict(os.environ, {"EFSPURGE_QUEUE_MAXSIZE": "5000"}):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.queue_maxsize == 5000


@pytest.mark.asyncio
async def test_cli_queue_maxsize_default(temp_dir):
    """Default queue_maxsize is 10000 when env var is not set."""
    env = os.environ.copy()
    env.pop("EFSPURGE_QUEUE_MAXSIZE", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.queue_maxsize == 10000
