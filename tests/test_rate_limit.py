"""Tests for empty directory deletion rate limiting."""

import tempfile
from pathlib import Path

import pytest

from efspurge.purger import AsyncEFSPurger


@pytest.mark.asyncio
async def test_empty_dir_rate_limit():
    """Test that max_empty_dirs_to_delete correctly limits deletions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Create 10 empty directories
        for i in range(10):
            (root / f"empty_{i}").mkdir()

        # Set rate limit to 5
        purger = AsyncEFSPurger(
            root_path=str(root),
            max_age_days=30,
            remove_empty_dirs=True,
            max_empty_dirs_to_delete=5,
            dry_run=False,
        )

        await purger.purge()

        # Should have deleted exactly 5 directories
        assert purger.stats["empty_dirs_deleted"] == 5

        # Should have 5 directories remaining
        remaining = [d for d in root.iterdir() if d.is_dir()]
        assert len(remaining) == 5


@pytest.mark.asyncio
async def test_empty_dir_no_rate_limit():
    """Test that unlimited deletion works (max_empty_dirs_to_delete=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Create 10 empty directories
        for i in range(10):
            (root / f"empty_{i}").mkdir()

        # No rate limit
        purger = AsyncEFSPurger(
            root_path=str(root),
            max_age_days=30,
            remove_empty_dirs=True,
            max_empty_dirs_to_delete=0,  # Unlimited
            dry_run=False,
        )

        await purger.purge()

        # Should have deleted all 10 directories
        assert purger.stats["empty_dirs_deleted"] == 10

        # Should have 0 directories remaining
        remaining = [d for d in root.iterdir() if d.is_dir()]
        assert len(remaining) == 0


@pytest.mark.asyncio
async def test_rate_limit_with_cascading():
    """Test rate limiting with cascading empty directory deletion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Create nested empty directories that will cascade
        # a/b/c, d/e/f, g/h/i
        for parent in ["a", "d", "g"]:
            for child in ["b", "e", "h"]:
                for grandchild in ["c", "f", "i"]:
                    (root / parent / child / grandchild).mkdir(parents=True, exist_ok=True)

        # Set rate limit to 5 (should stop during cascading)
        purger = AsyncEFSPurger(
            root_path=str(root),
            max_age_days=30,
            remove_empty_dirs=True,
            max_empty_dirs_to_delete=5,
            dry_run=False,
        )

        await purger.purge()

        # Should have deleted exactly 5 directories (stopped by rate limit)
        assert purger.stats["empty_dirs_deleted"] == 5


@pytest.mark.asyncio
async def test_rate_limit_validation():
    """Test that negative rate limit is rejected."""
    with pytest.raises(ValueError, match="max_empty_dirs_to_delete must be >= 0"):
        AsyncEFSPurger(
            root_path="/tmp",
            max_age_days=30,
            remove_empty_dirs=True,
            max_empty_dirs_to_delete=-1,
        )


@pytest.mark.asyncio
async def test_default_rate_limit():
    """Test that default rate limit is 500."""
    with tempfile.TemporaryDirectory() as tmpdir:
        purger = AsyncEFSPurger(
            root_path=str(tmpdir),
            max_age_days=30,
            remove_empty_dirs=True,
        )
        assert purger.max_empty_dirs_to_delete == 500
