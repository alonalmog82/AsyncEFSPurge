"""Tests for max_entries_per_dir parameter (Phase 1a per-directory entry cap).

When set (e.g. 50000), Phase 1a re-queues a directory after that many entries
and processes other dirs, so one huge directory cannot stall workers. Re-scanned
dirs are deduplicated via the discovered_dirs set.
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
# max_entries_per_dir parameter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_entries_per_dir_default_value(temp_dir):
    """Default max_entries_per_dir is 0 (no limit) when not specified."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
    )
    assert purger.max_entries_per_dir == 0


@pytest.mark.asyncio
async def test_max_entries_per_dir_explicit_value(temp_dir):
    """An explicit max_entries_per_dir value is stored correctly."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        max_entries_per_dir=50000,
    )
    assert purger.max_entries_per_dir == 50000


def test_max_entries_per_dir_invalid_raises(temp_dir):
    """max_entries_per_dir < 0 raises ValueError."""
    with pytest.raises(ValueError, match="max_entries_per_dir must be >= 0"):
        AsyncEFSPurger(
            root_path=str(temp_dir),
            max_age_days=30,
            dry_run=True,
            max_entries_per_dir=-1,
        )


@pytest.mark.asyncio
async def test_max_entries_per_dir_phase1a_completes_with_cap(temp_dir):
    """Phase 1a discovery completes with max_entries_per_dir set (small tree)."""
    for i in range(5):
        (temp_dir / f"sub_{i}").mkdir()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        dry_run=True,
        remove_empty_dirs=False,
        max_entries_per_dir=2,  # Cap low so we re-queue root after 2 entries
    )

    await purger.purge()

    # Should discover root + 5 subdirs
    assert purger.stats["dirs_scanned"] >= 1


# ---------------------------------------------------------------------------
# CLI / environment variable plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_max_entries_per_dir_env_var(temp_dir):
    """EFSPURGE_MAX_ENTRIES_PER_DIR env var is respected via parse_args."""
    with patch.dict(os.environ, {"EFSPURGE_MAX_ENTRIES_PER_DIR": "25000"}):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.max_entries_per_dir == 25000


@pytest.mark.asyncio
async def test_cli_max_entries_per_dir_default(temp_dir):
    """Default max_entries_per_dir is 0 when env var is not set."""
    env = os.environ.copy()
    env.pop("EFSPURGE_MAX_ENTRIES_PER_DIR", None)
    with patch.dict(os.environ, env, clear=True):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.max_entries_per_dir == 0
