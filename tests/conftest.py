"""Pytest configuration to ensure tests use local source code."""

import asyncio
import sys
from pathlib import Path

import pytest

# Add src directory to Python path to ensure tests use local source code
# instead of installed package
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))


# ---------------------------------------------------------------------------
# Event loop policy: use uvloop by default (matches production behavior).
#
# All async tests run on uvloop (Linux/macOS) to match the Docker/K8s
# production environment. On Windows, falls back to the default asyncio
# policy since uvloop is not available there.
#
# The test_default_asyncio_loop_sanity test overrides this to explicitly
# verify compatibility with the standard asyncio event loop.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop_policy():
    """Use uvloop for all async tests, matching production default."""
    try:
        import uvloop

        return uvloop.EventLoopPolicy()
    except ImportError:
        return asyncio.DefaultEventLoopPolicy()


def pytest_report_header():
    """Display which event loop policy tests will use in the pytest header."""
    try:
        import uvloop

        return [f"event loop: uvloop {uvloop.__version__} (production default)"]
    except ImportError:
        return ["event loop: default asyncio (uvloop not available)"]


# ---------------------------------------------------------------------------
# Executor cleanup: ensure no ThreadPoolExecutor threads leak between tests.
#
# AsyncEFSPurger creates a ThreadPoolExecutor in __init__ but only shuts it
# down inside purge().  Tests that call lower-level methods (_scan_and_purge_files,
# _purge_empty_directories_standalone, _remove_empty_directories) skip that
# cleanup, leaving non-daemon threads alive.  At process exit Python's atexit
# handler joins them, which can block indefinitely if any thread is stuck.
#
# This autouse fixture tracks every AsyncEFSPurger created during a test and
# calls close() on each one during teardown.
# ---------------------------------------------------------------------------

_purger_instances: list = []
_original_init = None


@pytest.fixture(autouse=True)
def _cleanup_purger_executors():
    """Auto-cleanup ThreadPoolExecutors after every test."""
    from efspurge.purger import AsyncEFSPurger

    global _original_init

    if _original_init is None:
        _original_init = AsyncEFSPurger.__init__

    def _tracking_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        _purger_instances.append(self)

    AsyncEFSPurger.__init__ = _tracking_init
    _purger_instances.clear()

    yield

    # Teardown: close every purger created during this test
    for purger in _purger_instances:
        try:
            purger.close()
        except Exception:
            pass
    _purger_instances.clear()

    # Restore original __init__
    AsyncEFSPurger.__init__ = _original_init
