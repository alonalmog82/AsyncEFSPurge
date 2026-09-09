"""Tests for in-band Phase 3 respecting `--phase3-batch-size` / `EFSPURGE_PHASE3_BATCH_SIZE`.

In-band Phase 3 (the one that runs automatically after Phase 2 completes)
historically loaded the entire empty-dirs sidecar into memory in a single pass.
On very large accumulated sidecars this triggers a cascade pathology that hangs
the deletion loop with zero forward progress (verified in prod 2026-09-09: 500k+
batch loaded via `_remove_empty_directories` hangs; 100k works).

Fix: extend the existing batched-drain machinery (previously only used by
`--phase3-only`) to also apply in-band. In-memory `empty_dirs` from the just-
finished Phase 2 are appended to the sidecar first so the batch stream sees
them; then `_drain_empty_dirs_sidecar_iterative` runs.

Default behaviour (batch size 0) is unchanged — load-all path still runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from efspurge.checkpoint import (
    append_empty_dirs_sidecar,
    empty_dirs_sidecar_path,
    stream_empty_dirs_sidecar,
)
from efspurge.purger import AsyncEFSPurger


def _make_purger(
    tmp_path: Path,
    root: Path,
    *,
    phase3_batch_size: int = 0,
    remove_empty_dirs: bool = True,
) -> AsyncEFSPurger:
    return AsyncEFSPurger(
        root_path=str(root),
        max_age_days=30,
        remove_empty_dirs=remove_empty_dirs,
        phase3_batch_size=phase3_batch_size,
        checkpoint_file=str(tmp_path / "cp.json"),
    )


# ---------------------------------------------------------------------------
# Branch selection: in-band Phase 3 with batch_size=0 uses load-all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inband_phase3_load_all_when_batch_size_zero(tmp_path):
    """batch_size=0 keeps historical behavior: _remove_empty_directories called directly, no iterative drain."""
    root = tmp_path / "root"
    root.mkdir()
    purger = _make_purger(tmp_path, root, phase3_batch_size=0)

    with (
        patch.object(purger, "_scan_and_purge_files", new_callable=AsyncMock) as mock_scan,
        patch.object(purger, "_remove_empty_directories", new_callable=AsyncMock) as mock_remove,
        patch.object(purger, "_drain_empty_dirs_sidecar_iterative", new_callable=AsyncMock) as mock_drain,
    ):
        await purger.purge()

    mock_scan.assert_awaited_once()
    mock_remove.assert_awaited_once()
    mock_drain.assert_not_called()


# ---------------------------------------------------------------------------
# Branch selection: in-band Phase 3 with batch_size>0 uses batched drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inband_phase3_batched_when_batch_size_positive(tmp_path):
    """batch_size>0 delegates to _drain_empty_dirs_sidecar_iterative instead of _remove_empty_directories."""
    root = tmp_path / "root"
    root.mkdir()
    purger = _make_purger(tmp_path, root, phase3_batch_size=100_000)

    with (
        patch.object(purger, "_scan_and_purge_files", new_callable=AsyncMock) as mock_scan,
        patch.object(purger, "_remove_empty_directories", new_callable=AsyncMock) as mock_remove,
        patch.object(purger, "_drain_empty_dirs_sidecar_iterative", new_callable=AsyncMock) as mock_drain,
    ):
        await purger.purge()

    mock_scan.assert_awaited_once()
    mock_remove.assert_not_called()
    mock_drain.assert_awaited_once_with(100_000)


# ---------------------------------------------------------------------------
# In-memory empty_dirs are flushed to sidecar before batched drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inband_phase3_flushes_inmem_empty_dirs_to_sidecar(tmp_path):
    """When batch_size>0, in-memory empty_dirs from Phase 2 are appended to the sidecar first
    and cleared, so the batched drain sees them alongside historical accumulations.

    We snapshot the sidecar contents from inside the (mocked) drain call — the purge()
    success path removes the sidecar in its finalize step, so we can't inspect it post-hoc.
    """
    root = tmp_path / "root"
    root.mkdir()
    purger = _make_purger(tmp_path, root, phase3_batch_size=50_000)

    # Simulate Phase 2 having accumulated some in-memory candidates
    inmem_paths = {Path("/data/foo/a"), Path("/data/foo/b"), Path("/data/foo/c")}
    purger.empty_dirs = set(inmem_paths)

    # Also seed the sidecar with a historical carry-over entry to prove we don't clobber it
    checkpoint_path = purger.checkpoint_file
    append_empty_dirs_sidecar(checkpoint_path, ["/data/historical/x"])

    snapshot: set[str] = set()
    snapshot_empty_dirs: set[Path] | None = None

    async def snapshot_drain(_batch_size):
        # Capture the state at the moment drain is called: sidecar should now hold
        # historical + flushed entries, and self.empty_dirs should be cleared.
        snapshot.update(stream_empty_dirs_sidecar(checkpoint_path))
        nonlocal snapshot_empty_dirs
        snapshot_empty_dirs = set(purger.empty_dirs)

    with (
        patch.object(purger, "_scan_and_purge_files", new_callable=AsyncMock),
        patch.object(purger, "_drain_empty_dirs_sidecar_iterative", side_effect=snapshot_drain),
    ):
        await purger.purge()

    # At drain-call time, in-memory set had been cleared
    assert snapshot_empty_dirs == set()

    # Sidecar at drain-call time held historical + flushed entries
    expected = {str(p) for p in inmem_paths} | {"/data/historical/x"}
    assert snapshot == expected


# ---------------------------------------------------------------------------
# Empty in-memory empty_dirs is a no-op flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inband_phase3_no_flush_when_empty_dirs_empty(tmp_path):
    """No sidecar append when Phase 2 didn't discover any new empty dirs."""
    root = tmp_path / "root"
    root.mkdir()
    purger = _make_purger(tmp_path, root, phase3_batch_size=100)
    purger.empty_dirs = set()  # nothing to flush

    with (
        patch.object(purger, "_scan_and_purge_files", new_callable=AsyncMock),
        patch.object(purger, "_drain_empty_dirs_sidecar_iterative", new_callable=AsyncMock) as mock_drain,
    ):
        await purger.purge()

    # Sidecar file should not exist since we never appended
    sidecar = empty_dirs_sidecar_path(purger.checkpoint_file)
    assert not sidecar.exists()
    # But the batched drain was still invoked (in case a prior-run sidecar existed)
    mock_drain.assert_awaited_once_with(100)
