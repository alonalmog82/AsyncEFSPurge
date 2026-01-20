"""Basic tests for AsyncEFSPurge."""

import pytest


def test_version():
    """Test that version is defined and matches pyproject.toml."""
    import tomllib
    from pathlib import Path

    from efspurge import __version__

    # Read version from pyproject.toml (single source of truth)
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)
        expected_version = pyproject["project"]["version"]

    # Version should match pyproject.toml
    assert __version__ == expected_version, f"Version mismatch: {__version__} != {expected_version}"
    # Version should be a valid semantic version format
    assert __version__.count(".") >= 1, f"Invalid version format: {__version__}"


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
