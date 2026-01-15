"""Basic tests for AsyncEFSPurge."""

import pytest


def test_version():
    """Test that version is defined."""
    from efspurge import __version__

    assert __version__ == "1.3.0"


def test_imports():
    """Test that all modules can be imported."""
    from efspurge import cli, logging, purger

    assert cli is not None
    assert logging is not None
    assert purger is not None


@pytest.mark.asyncio
async def test_purger_initialization():
    """Test that AsyncEFSPurger can be initialized."""
    from efspurge.purger import AsyncEFSPurger

    purger = AsyncEFSPurger(
        root_path="/tmp/test",
        max_age_days=30,
        max_concurrency=100,
        dry_run=True,
        log_level="INFO",
    )

    assert purger.root_path.name == "test"
    assert purger.max_age_days == 30
    assert purger.dry_run is True
