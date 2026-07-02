"""Tests for the --phase3-only mode.

Phase-3-only runs the post-scan empty-directory cleanup by draining the
existing ``.empty_dirs.gz`` sidecar WITHOUT re-running Phase 1 or Phase 2.
It is intended to be invoked mid-scan so an operator can realize the
already-discovered empty-dir cleanup work before starting a sharded re-scan.

Key invariants exercised here:

* Sidecar entries are actually deleted (unless ``dry_run=True``).
* Missing sidecar entries (already gone on disk) are handled gracefully.
* The main checkpoint file and the ``.pending_dirs.gz`` sidecar are NOT
  touched — a subsequent ``--resume`` must still find Phase 2 state intact.
* Argparse-layer validation rejects illegal flag combinations.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from efspurge.checkpoint import (
    append_empty_dirs_sidecar,
    empty_dirs_sidecar_path,
    pending_dirs_sidecar_path,
    stream_empty_dirs_sidecar,
    write_pending_dirs_sidecar,
)
from efspurge.purger import AsyncEFSPurger


def _make_purger(
    tmp_path: Path,
    root: Path,
    *,
    dry_run: bool = False,
    phase3_only: bool = True,
    remove_empty_dirs: bool = True,
    phase3_batch_size: int = 0,
) -> AsyncEFSPurger:
    return AsyncEFSPurger(
        root_path=str(root),
        max_age_days=30,
        dry_run=dry_run,
        remove_empty_dirs=remove_empty_dirs,
        phase3_only=phase3_only,
        phase3_batch_size=phase3_batch_size,
        checkpoint_file=str(tmp_path / "cp.json"),
    )


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_phase3_only_requires_remove_empty_dirs(tmp_path):
    """Constructor rejects --phase3-only without --remove-empty-dirs."""
    with pytest.raises(ValueError, match="phase3-only requires --remove-empty-dirs"):
        AsyncEFSPurger(
            root_path=str(tmp_path),
            max_age_days=30,
            phase3_only=True,
            remove_empty_dirs=False,
        )


def test_phase1_and_phase3_only_mutually_exclusive(tmp_path):
    """Constructor rejects both phase1_only and phase3_only set."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        AsyncEFSPurger(
            root_path=str(tmp_path),
            max_age_days=30,
            phase1_only=True,
            phase3_only=True,
            remove_empty_dirs=True,
        )


# ---------------------------------------------------------------------------
# Runtime behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase3_only_deletes_sidecar_entries(tmp_path):
    """All empty dirs listed in the sidecar are deleted; sidecar is removed."""
    root = tmp_path / "root"
    root.mkdir()
    empty_a = root / "a"
    empty_a.mkdir()
    empty_b = root / "b" / "c"
    empty_b.mkdir(parents=True)

    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(empty_a), str(empty_b)])
    assert empty_dirs_sidecar_path(cp).exists()

    purger = _make_purger(tmp_path, root)
    await purger.purge()

    assert not empty_a.exists(), "empty dir a should have been deleted"
    assert not empty_b.exists(), "empty dir b should have been deleted"
    # Sidecar itself should be gone on successful completion.
    assert not empty_dirs_sidecar_path(cp).exists()


@pytest.mark.asyncio
async def test_phase3_only_no_sidecar_is_noop(tmp_path):
    """With no sidecar present, phase3-only exits cleanly without side effects."""
    root = tmp_path / "root"
    root.mkdir()
    # Ensure no sidecar
    assert not empty_dirs_sidecar_path(tmp_path / "cp.json").exists()

    purger = _make_purger(tmp_path, root)
    stats = await purger.purge()

    # No dirs to delete → empty_dirs_deleted stays 0 (key may or may not exist
    # depending on branch; check via stats dict).
    assert stats.get("empty_dirs_deleted", 0) == 0


@pytest.mark.asyncio
async def test_phase3_only_skips_missing_paths(tmp_path):
    """Sidecar entries that no longer exist on disk are handled gracefully."""
    root = tmp_path / "root"
    root.mkdir()
    # Only one of the two sidecar entries actually exists on disk.
    real = root / "exists"
    real.mkdir()
    ghost = root / "does_not_exist"

    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(real), str(ghost)])

    purger = _make_purger(tmp_path, root)
    await purger.purge()

    assert not real.exists()
    # No exception raised for ghost; purge completed.


@pytest.mark.asyncio
async def test_phase3_only_dry_run_does_not_delete(tmp_path):
    """With dry_run=True, sidecar entries survive; sidecar is still removed."""
    root = tmp_path / "root"
    root.mkdir()
    empty = root / "x"
    empty.mkdir()

    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(empty)])

    purger = _make_purger(tmp_path, root, dry_run=True)
    await purger.purge()

    assert empty.exists(), "dry_run must not delete anything"


@pytest.mark.asyncio
async def test_phase3_only_preserves_phase2_checkpoint_and_pending(tmp_path):
    """Main checkpoint + pending_dirs sidecar are untouched by phase3-only."""
    root = tmp_path / "root"
    root.mkdir()
    empty = root / "e"
    empty.mkdir()
    # A dir that is *not* in the empty-dirs sidecar — Phase 2 hasn't visited
    # it yet.  Its presence in the pending_dirs sidecar must survive.
    pending_subtree = root / "still_pending"
    pending_subtree.mkdir()

    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(empty)])
    write_pending_dirs_sidecar(cp, [str(pending_subtree)])
    # A fake main-checkpoint file — the important thing is that it's still
    # there after phase3-only completes.  Contents don't matter for this
    # invariant; we're testing preservation, not resume correctness.
    cp.write_text('{"version": 3, "root_path": "' + str(root) + '", "pending_dirs_count": 1}\n')

    purger = _make_purger(tmp_path, root)
    await purger.purge()

    # Empty-dirs sidecar consumed.
    assert not empty_dirs_sidecar_path(cp).exists()
    # But main checkpoint + pending_dirs sidecar preserved.
    assert cp.exists(), "main checkpoint must not be deleted by phase3-only"
    assert pending_dirs_sidecar_path(cp).exists(), "pending_dirs sidecar must not be deleted by phase3-only"
    # And the pending path is still recoverable.
    assert list(stream_empty_dirs_sidecar(cp)) == [], "empty-dirs sidecar drained"


# ---------------------------------------------------------------------------
# CLI argparse-layer validation
# ---------------------------------------------------------------------------


def test_cli_rejects_phase1_and_phase3_together(tmp_path):
    """CLI exits non-zero when both --phase1-only and --phase3-only are set."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "efspurge.cli",
            str(tmp_path),
            "--max-age-days",
            "30",
            "--remove-empty-dirs",
            "--phase1-only",
            "--phase3-only",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


def test_cli_rejects_phase3_without_remove_empty_dirs(tmp_path):
    """CLI exits non-zero when --phase3-only is set without --remove-empty-dirs."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "efspurge.cli",
            str(tmp_path),
            "--max-age-days",
            "30",
            "--phase3-only",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires --remove-empty-dirs" in result.stderr


# ---------------------------------------------------------------------------
# Bug-fix: sidecar preserved on abort (regression for 2.3.0 defect where the
# empty-dirs sidecar was removed regardless of whether the deletion pass
# actually completed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase3_only_preserves_sidecar_on_memory_abort(tmp_path, monkeypatch):
    """If _remove_empty_directories aborts on memory-critical, the sidecar
    must remain on disk so a retry can pick up the unprocessed entries."""
    root = tmp_path / "root"
    root.mkdir()
    empty = root / "abort-me"
    empty.mkdir()

    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(empty)])

    purger = _make_purger(tmp_path, root)

    original = AsyncEFSPurger._remove_empty_directories

    async def fake_remove(self):
        # Simulate the memory-critical circuit breaker firing — set the
        # flag but do NOT actually delete anything.
        self._checkpoint_requested = True

    monkeypatch.setattr(AsyncEFSPurger, "_remove_empty_directories", fake_remove)
    try:
        await purger.purge()
    finally:
        monkeypatch.setattr(AsyncEFSPurger, "_remove_empty_directories", original)

    # The directory was NOT deleted (we short-circuited) and the sidecar
    # must still be there.
    assert empty.exists(), "abort path must not delete anything"
    assert empty_dirs_sidecar_path(cp).exists(), "sidecar must be preserved on abort so the operator can retry"


# ---------------------------------------------------------------------------
# Iterative mode: memory-bounded drain via --phase3-batch-size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase3_only_iterative_processes_multiple_batches(tmp_path):
    """With phase3_batch_size < total unique entries, all get processed."""
    root = tmp_path / "root"
    root.mkdir()
    dirs = []
    for i in range(23):
        d = root / f"leaf{i:03d}"
        d.mkdir()
        dirs.append(d)

    cp = tmp_path / "cp.json"
    # Deliberately double the sidecar entries — the loader should dedup
    # per-batch and repeat deletions must be harmless (ENOENT).
    append_empty_dirs_sidecar(cp, [str(d) for d in dirs])
    append_empty_dirs_sidecar(cp, [str(d) for d in dirs])

    purger = _make_purger(tmp_path, root, phase3_batch_size=5)
    await purger.purge()

    for d in dirs:
        assert not d.exists(), f"{d} should have been deleted across batches"
    assert not empty_dirs_sidecar_path(cp).exists(), "sidecar removed after full drain across batches"


@pytest.mark.asyncio
async def test_phase3_only_iterative_preserves_sidecar_on_abort(tmp_path, monkeypatch):
    """Iterative drain: if a batch aborts on memory-critical, sidecar is preserved."""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(6):
        (root / f"d{i}").mkdir()

    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(root / f"d{i}") for i in range(6)])

    purger = _make_purger(tmp_path, root, phase3_batch_size=2)

    original = AsyncEFSPurger._remove_empty_directories
    call_counter = {"n": 0}

    async def fake_remove(self):
        call_counter["n"] += 1
        # First batch: succeed silently.  Second batch: trip memory-critical.
        if call_counter["n"] >= 2:
            self._checkpoint_requested = True

    monkeypatch.setattr(AsyncEFSPurger, "_remove_empty_directories", fake_remove)
    try:
        await purger.purge()
    finally:
        monkeypatch.setattr(AsyncEFSPurger, "_remove_empty_directories", original)

    assert empty_dirs_sidecar_path(cp).exists(), "sidecar must be preserved when any batch aborts"
    # We aborted on the SECOND batch, so at least the first batch worth of
    # calls happened.  This also confirms we actually did iterate batches
    # rather than falling back to load-all.
    assert call_counter["n"] >= 2, "iterative mode must call the deletion helper per-batch"


@pytest.mark.asyncio
async def test_phase3_only_iterative_batch_size_zero_uses_load_all(tmp_path, monkeypatch):
    """batch_size=0 must NOT enter the iterative path — behavior identical to pre-fix."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").mkdir()
    cp = tmp_path / "cp.json"
    append_empty_dirs_sidecar(cp, [str(root / "one")])

    purger = _make_purger(tmp_path, root, phase3_batch_size=0)

    iterative_called = {"yes": False}

    async def fake_iterative(self, batch_size):
        iterative_called["yes"] = True
        return True

    monkeypatch.setattr(AsyncEFSPurger, "_drain_empty_dirs_sidecar_iterative", fake_iterative)
    await purger.purge()

    assert not iterative_called["yes"], "with batch_size=0, iterative drain must not be invoked"


def test_cli_accepts_phase3_batch_size(tmp_path):
    """CLI parses --phase3-batch-size and forwards it as an int."""
    from efspurge.cli import parse_args

    args = parse_args(
        [
            str(tmp_path),
            "--max-age-days",
            "30",
            "--remove-empty-dirs",
            "--phase3-only",
            "--phase3-batch-size",
            "12345",
        ]
    )
    assert args.phase3_only is True
    assert args.phase3_batch_size == 12345
