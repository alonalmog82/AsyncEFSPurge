"""Tests for Phase 1a checkpoint/resume and Phase 2 empty_dirs checkpoint fix.

Phase 1a checkpoint:
- On memory abort during discovery: saves BFS frontier, runs Phase 1b on found dirs, exits 75
- On resume: restores BFS frontier, continues discovery from where it stopped
- On clean completion: deletes checkpoint file

Phase 2 empty_dirs fix:
- empty_dirs accumulated during Phase 2 are saved in checkpoint
- On resume, empty_dirs are restored so Phase 3 can process them
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.checkpoint import (
    CHECKPOINT_VERSION,
    load_checkpoint,
    load_phase1a_checkpoint,
    save_checkpoint,
    save_phase1a_checkpoint,
)
from efspurge.purger import AsyncEFSPurger, CheckpointExit


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# save_phase1a_checkpoint / load_phase1a_checkpoint unit tests
# ---------------------------------------------------------------------------


def test_save_phase1a_checkpoint_creates_valid_json(tmp_path):
    """save_phase1a_checkpoint writes valid JSON with expected keys."""
    cp = tmp_path / "phase1a.json"
    save_phase1a_checkpoint(
        filepath=cp,
        root_path="/data/root",
        pending_dirs=["/data/root/a", "/data/root/b"],
        config={"root_path": "/data/root"},
    )
    assert cp.exists()
    data = json.loads(cp.read_text())
    assert data["version"] == CHECKPOINT_VERSION
    assert data["phase"] == "phase1a"
    assert data["root_path"] == "/data/root"
    assert data["pending_dirs"] == ["/data/root/a", "/data/root/b"]
    assert data["config"]["root_path"] == "/data/root"


def test_load_phase1a_checkpoint_roundtrip(tmp_path):
    """load_phase1a_checkpoint returns data saved by save_phase1a_checkpoint."""
    cp = tmp_path / "phase1a.json"
    save_phase1a_checkpoint(
        filepath=cp,
        root_path="/data/api_files",
        pending_dirs=["/data/api_files/org1", "/data/api_files/org2"],
        config={"root_path": "/data/api_files"},
    )
    loaded = load_phase1a_checkpoint(cp)
    assert loaded is not None
    assert loaded["root_path"] == "/data/api_files"
    assert loaded["pending_dirs"] == ["/data/api_files/org1", "/data/api_files/org2"]


def test_load_phase1a_checkpoint_missing_file_returns_none(tmp_path):
    """load_phase1a_checkpoint returns None for missing file."""
    assert load_phase1a_checkpoint(tmp_path / "nonexistent.json") is None


def test_load_phase1a_checkpoint_invalid_json_returns_none(tmp_path):
    """load_phase1a_checkpoint returns None for invalid JSON."""
    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    assert load_phase1a_checkpoint(bad) is None


def test_load_phase1a_checkpoint_wrong_phase_returns_none(tmp_path):
    """load_phase1a_checkpoint returns None when phase is not phase1a."""
    cp = tmp_path / "cp.json"
    cp.write_text(
        json.dumps({"version": CHECKPOINT_VERSION, "phase": "phase2", "pending_dirs": ["/a"]})
    )
    assert load_phase1a_checkpoint(cp) is None


def test_load_phase1a_checkpoint_version_mismatch_returns_none(tmp_path):
    """load_phase1a_checkpoint returns None when version does not match."""
    cp = tmp_path / "cp.json"
    cp.write_text(json.dumps({"version": 99, "phase": "phase1a", "pending_dirs": ["/a"]}))
    assert load_phase1a_checkpoint(cp) is None


def test_load_phase1a_checkpoint_empty_pending_returns_none(tmp_path):
    """load_phase1a_checkpoint returns None when pending_dirs is empty (treat as complete)."""
    cp = tmp_path / "cp.json"
    save_phase1a_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=[],
        config={},
    )
    assert load_phase1a_checkpoint(cp) is None


def test_load_checkpoint_rejects_phase1a_file(tmp_path):
    """load_checkpoint (Phase 2) returns None for a Phase 1a checkpoint file."""
    cp = tmp_path / "phase1a.json"
    save_phase1a_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        config={},
    )
    assert load_checkpoint(cp) is None


# ---------------------------------------------------------------------------
# Phase 1a checkpoint/resume parameters stored on purger
# ---------------------------------------------------------------------------


def test_dir_deletion_checkpoint_params_stored(temp_dir, tmp_path):
    """dir_deletion_checkpoint_file and dir_deletion_resume are stored on the purger."""
    cp = tmp_path / "phase1a.json"
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        dir_deletion_checkpoint_file=str(cp),
        dir_deletion_resume=True,
    )
    assert purger.dir_deletion_checkpoint_file == cp
    assert purger.dir_deletion_resume is True


def test_dir_deletion_checkpoint_params_default_none(temp_dir):
    """dir_deletion_checkpoint_file is None and dir_deletion_resume is False by default."""
    purger = AsyncEFSPurger(root_path=str(temp_dir), max_age_days=30, dry_run=True)
    assert purger.dir_deletion_checkpoint_file is None
    assert purger.dir_deletion_resume is False


# ---------------------------------------------------------------------------
# CLI / env var plumbing for new flags
# ---------------------------------------------------------------------------


def test_cli_dir_deletion_checkpoint_file_env_var(temp_dir, tmp_path):
    """EFSPURGE_DIR_DELETION_CHECKPOINT_FILE env var is respected via parse_args."""
    import os
    from unittest.mock import patch as mpatch

    with mpatch.dict(os.environ, {"EFSPURGE_DIR_DELETION_CHECKPOINT_FILE": str(tmp_path / "p1a.json")}):
        with mpatch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.dir_deletion_checkpoint_file == str(tmp_path / "p1a.json")


def test_cli_dir_deletion_resume_env_var(temp_dir):
    """EFSPURGE_DIR_DELETION_RESUME=1 is respected via parse_args."""
    import os
    from unittest.mock import patch as mpatch

    with mpatch.dict(os.environ, {"EFSPURGE_DIR_DELETION_RESUME": "1"}):
        with mpatch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.dir_deletion_resume is True


# ---------------------------------------------------------------------------
# Phase 1a checkpoint save on memory abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1a_checkpoint_saved_on_memory_abort(temp_dir, tmp_path):
    """On memory abort during Phase 1a discovery, checkpoint file is written and CheckpointExit raised."""
    # Create a small tree
    for i in range(5):
        (temp_dir / f"sub_{i}").mkdir()

    cp_file = tmp_path / "phase1a.json"

    # Mock memory to exceed 95% on the second check (after a few dirs discovered)
    call_count = 0

    def fake_memory():
        nonlocal call_count
        call_count += 1
        # First few calls return normal, then simulate critical memory
        return 5226.0 if call_count > 3 else 100.0  # 5226 / 5500 = 95%

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=5500,
        dry_run=False,
        dir_deletion_checkpoint_file=str(cp_file),
    )

    with patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory):
        with pytest.raises(CheckpointExit):
            await purger._purge_empty_directories_standalone()

    # Checkpoint file must exist with phase1a data
    assert cp_file.exists()
    data = json.loads(cp_file.read_text())
    assert data["phase"] == "phase1a"
    assert data["version"] == CHECKPOINT_VERSION
    assert isinstance(data["pending_dirs"], list)


@pytest.mark.asyncio
async def test_phase1a_checkpoint_not_saved_without_checkpoint_file(temp_dir, tmp_path):
    """Without dir_deletion_checkpoint_file, memory abort does NOT raise CheckpointExit."""
    for i in range(5):
        (temp_dir / f"sub_{i}").mkdir()

    call_count = 0

    def fake_memory():
        nonlocal call_count
        call_count += 1
        return 5226.0 if call_count > 3 else 100.0

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=5500,
        dry_run=False,
        # No dir_deletion_checkpoint_file
    )

    # Should complete without raising CheckpointExit (just does partial scan)
    with patch("efspurge.purger.get_memory_usage_mb", side_effect=fake_memory):
        result = await purger._purge_empty_directories_standalone()
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Phase 1a clean completion deletes checkpoint file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1a_checkpoint_deleted_on_clean_completion(temp_dir, tmp_path):
    """When Phase 1a completes without memory abort, the checkpoint file is deleted."""
    (temp_dir / "sub_a").mkdir()
    (temp_dir / "sub_b").mkdir()

    cp_file = tmp_path / "phase1a.json"
    # Pre-create a stale checkpoint file
    save_phase1a_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(temp_dir / "sub_a")],
        config={},
    )
    assert cp_file.exists()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,  # No memory limit → no abort
        dry_run=False,
        dir_deletion_checkpoint_file=str(cp_file),
    )

    await purger._purge_empty_directories_standalone()

    # Checkpoint file should be removed after successful completion
    assert not cp_file.exists()


# ---------------------------------------------------------------------------
# Phase 1a resume: continues BFS from checkpoint frontier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1a_resume_deletes_empty_dirs_from_checkpoint(temp_dir, tmp_path):
    """Phase 1a resume loads frontier from checkpoint and deletes empty dirs it finds."""
    # Create a subtree under sub_a — sub_a/leaf is empty
    sub_a = temp_dir / "sub_a"
    sub_a.mkdir()
    leaf = sub_a / "leaf"
    leaf.mkdir()
    # sub_b has a file — not empty
    sub_b = temp_dir / "sub_b"
    sub_b.mkdir()
    (sub_b / "file.txt").write_text("keep")

    cp_file = tmp_path / "phase1a.json"
    # Simulate checkpoint with sub_a in the BFS frontier (not yet scanned)
    save_phase1a_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(sub_a)],
        config={"root_path": str(temp_dir)},
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
    )

    deleted = await purger._purge_empty_directories_standalone()

    # leaf is empty → should be deleted; sub_a may also become empty after leaf removal
    assert not leaf.exists()
    # Checkpoint file removed on clean completion
    assert not cp_file.exists()
    assert deleted >= 1


@pytest.mark.asyncio
async def test_phase1a_resume_with_overflow_beyond_queue_maxsize(temp_dir, tmp_path):
    """Resume with more checkpoint pending dirs than queue_maxsize completes via loader task."""
    # Create 30 subdirs, each empty
    subdirs = []
    for i in range(30):
        d = temp_dir / f"sub_{i:03d}"
        d.mkdir()
        subdirs.append(d)

    cp_file = tmp_path / "phase1a.json"
    save_phase1a_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(d) for d in subdirs],  # 30 dirs, queue_maxsize=5
        config={"root_path": str(temp_dir)},
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
        queue_maxsize=5,  # Much smaller than 30
        max_concurrent_discovery=3,
    )

    deleted = await purger._purge_empty_directories_standalone()

    # All 30 empty subdirs should be deleted
    assert deleted == 30
    for d in subdirs:
        assert not d.exists()


# ---------------------------------------------------------------------------
# Phase 2 empty_dirs saved and restored in checkpoint
# ---------------------------------------------------------------------------


def test_save_checkpoint_includes_empty_dirs(tmp_path):
    """save_checkpoint writes empty_dirs to JSON when provided."""
    cp = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={"dirs_scanned": 10},
        config={"max_age_days": 30},
        empty_dirs=["/data/empty1", "/data/empty2"],
    )
    data = json.loads(cp.read_text())
    assert data["empty_dirs"] == ["/data/empty1", "/data/empty2"]


def test_save_checkpoint_empty_dirs_defaults_to_empty_list(tmp_path):
    """save_checkpoint writes empty list for empty_dirs when not provided."""
    cp = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={},
        config={},
    )
    data = json.loads(cp.read_text())
    assert data["empty_dirs"] == []


def test_load_checkpoint_returns_empty_dirs(tmp_path):
    """load_checkpoint returns empty_dirs from checkpoint JSON."""
    cp = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={},
        config={},
        empty_dirs=["/data/was_empty"],
    )
    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert loaded["empty_dirs"] == ["/data/was_empty"]


def test_load_checkpoint_empty_dirs_missing_returns_empty_list(tmp_path):
    """load_checkpoint handles old checkpoints without empty_dirs key gracefully."""
    cp = tmp_path / "cp.json"
    # Write an old-style checkpoint without empty_dirs
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": "/data",
        "phase": "phase2",
        "pending_dirs": ["/data/a"],
        "stats": {},
        "config": {},
    }
    cp.write_text(json.dumps(data))
    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert loaded.get("empty_dirs", []) == []


@pytest.mark.asyncio
async def test_phase2_empty_dirs_restored_on_resume(temp_dir, tmp_path):
    """empty_dirs from a previous Phase 2 run are restored on resume so Phase 3 can delete them."""
    # Create a dir that "was found empty" in a previous run — we'll record it in checkpoint
    previously_empty = temp_dir / "was_empty_before"
    previously_empty.mkdir()

    # Create a subdir with a file to give Phase 2 something to scan
    subdir = temp_dir / "has_files"
    subdir.mkdir()
    (subdir / "file.txt").write_text("content")

    cp_file = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(subdir)],
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
        empty_dirs=[str(previously_empty)],
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        checkpoint_file=str(cp_file),
        resume=True,
    )

    await purger.purge()

    # previously_empty was in the checkpoint's empty_dirs — Phase 3 should have deleted it
    assert not previously_empty.exists()


@pytest.mark.asyncio
async def test_phase2_empty_dirs_accumulated_across_checkpoint_resume(temp_dir, tmp_path):
    """empty_dirs from checkpoint are merged with newly-found empty dirs before Phase 3 runs."""
    # Dir from previous run
    from_prev_run = temp_dir / "from_prev_run"
    from_prev_run.mkdir()

    # Dir that will become empty in this run (subdir with no files)
    new_empty = temp_dir / "new_empty"
    new_empty.mkdir()

    cp_file = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(new_empty)],  # Phase 2 will scan new_empty
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
        empty_dirs=[str(from_prev_run)],  # Accumulated from previous run
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        checkpoint_file=str(cp_file),
        resume=True,
    )

    await purger.purge()

    # Both dirs should be deleted by Phase 3
    assert not from_prev_run.exists(), "Dir from previous checkpoint run should be deleted"
    assert not new_empty.exists(), "Dir found empty in this run should be deleted"
