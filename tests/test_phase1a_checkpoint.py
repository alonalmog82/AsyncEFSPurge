"""Tests for Phase 1a/1b checkpoint/resume, Phase 2 empty_dirs fix, and --phase1-only.

Phase 1a checkpoint:
- On memory abort during discovery: saves BFS frontier, runs Phase 1b on found dirs, exits 75
- On resume: restores BFS frontier, continues discovery from where it stopped
- On clean completion: deletes checkpoint file

Phase 1b checkpoint:
- On memory abort during bottom-up deletion: saves remaining dirs_by_depth, exits 75
- On resume: skips Phase 1a entirely, continues deletion from checkpoint
- On clean completion: deletes checkpoint file

Phase 2 empty_dirs fix:
- empty_dirs accumulated during Phase 2 are saved in checkpoint
- On resume, empty_dirs are restored so Phase 3 can process them

--phase1-only flag:
- Skips Phase 2 and Phase 3 entirely
- Works with checkpoint/resume for iterative empty-dir cleanup
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from efspurge.checkpoint import (
    CHECKPOINT_VERSION,
    load_checkpoint,
    load_phase1a_checkpoint,
    load_phase1b_checkpoint,
    save_checkpoint,
    save_phase1a_checkpoint,
    save_phase1b_checkpoint,
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
    cp.write_text(json.dumps({"version": CHECKPOINT_VERSION, "phase": "phase2", "pending_dirs": ["/a"]}))
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

    # Mock memory:
    # - Calls 1-2: normal so the initial check and root directory scan succeed
    # - Calls 3+:  high (5226/5500 > 0.95) to trigger Phase 1a worker memory_abort
    # - Once "Phase 1a complete" is logged, switch back to normal so Phase 1b
    #   runs cleanly and the Phase 1a checkpoint is saved afterward.
    import efspurge.purger as _purger_module

    phase1a_done = False
    call_count = 0
    _real_log = _purger_module.log_with_context  # capture before patch

    def spy_log(logger, level, msg, extra=None):
        nonlocal phase1a_done
        if "Phase 1a complete" in msg:
            phase1a_done = True
        _real_log(logger, level, msg, extra)

    def fake_memory():
        nonlocal call_count
        if phase1a_done:
            return 100.0  # Phase 1b: normal memory — no abort
        call_count += 1
        # First 2 calls: normal (initial log + root dir memory check → root gets scanned)
        # Call 3+: high → triggers memory_abort in worker processing sub-dirs
        return 100.0 if call_count <= 2 else 5226.0

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=5500,
        dry_run=False,
        dir_deletion_checkpoint_file=str(cp_file),
    )

    with patch("efspurge.purger.log_with_context", side_effect=spy_log):
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


def test_save_checkpoint_writes_empty_dirs_to_sidecar(tmp_path):
    """save_checkpoint appends empty_dirs to ``<checkpoint>.empty_dirs.gz`` (not inline).

    Embedding millions of empty-dir paths in the main checkpoint forced the
    purger to re-materialise them on every resume, which drove the resume
    baseline above the back-pressure threshold and caused a death-spiral on
    prod.  The new design persists them to a sidecar that's streamed in only
    at the start of Phase 3.
    """
    from efspurge.checkpoint import empty_dirs_sidecar_path, stream_empty_dirs_sidecar

    cp = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={"dirs_scanned": 10},
        config={"max_age_days": 30},
        empty_dirs=["/data/empty1", "/data/empty2"],
    )
    # Main checkpoint must not embed empty_dirs anymore (keeps resume baseline low).
    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert "empty_dirs" not in loaded or not loaded.get("empty_dirs")

    # Sidecar exists and contains the paths in order.
    assert empty_dirs_sidecar_path(cp).exists()
    assert list(stream_empty_dirs_sidecar(cp)) == ["/data/empty1", "/data/empty2"]


def test_save_checkpoint_without_empty_dirs_skips_sidecar(tmp_path):
    """save_checkpoint with no empty_dirs does not create the sidecar."""
    from efspurge.checkpoint import empty_dirs_sidecar_path

    cp = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={},
        config={},
    )
    assert not empty_dirs_sidecar_path(cp).exists()


def test_save_checkpoint_appends_to_existing_sidecar(tmp_path):
    """Repeated save_checkpoint calls append to the sidecar instead of rewriting it.

    This is what lets the in-memory ``empty_dirs`` set shed entries between
    checkpoints: each save flushes the current run's incremental findings to
    the sidecar, and prior runs' findings stay on disk where they belong.
    """
    from efspurge.checkpoint import stream_empty_dirs_sidecar

    cp = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={},
        config={},
        empty_dirs=["/data/run1_a", "/data/run1_b"],
    )
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={},
        config={},
        empty_dirs=["/data/run2_a"],
    )
    assert list(stream_empty_dirs_sidecar(cp)) == [
        "/data/run1_a",
        "/data/run1_b",
        "/data/run2_a",
    ]


def test_load_checkpoint_no_longer_embeds_empty_dirs(tmp_path):
    """load_checkpoint returns the main checkpoint dict without an embedded empty_dirs list.

    Older checkpoints written with embedded empty_dirs remain readable so
    the purger can migrate them to the sidecar on first resume; that
    backwards-compat path is exercised by the integration test below.
    """
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
    # New checkpoints omit empty_dirs from the main payload entirely.
    assert "empty_dirs" not in loaded


def test_load_checkpoint_legacy_embedded_empty_dirs_preserved(tmp_path):
    """A pre-existing checkpoint with embedded empty_dirs is still readable.

    The purger consumes this field once on resume and migrates the entries
    to the sidecar so subsequent runs never see it again.
    """
    cp = tmp_path / "cp.json"
    # Write a legacy-style checkpoint with empty_dirs inline (pre-sidecar format).
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": "/data",
        "phase": "phase2",
        "pending_dirs": ["/data/a"],
        "stats": {},
        "config": {},
        "empty_dirs": ["/data/legacy_empty"],
    }
    cp.write_text(json.dumps(data))
    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert loaded.get("empty_dirs") == ["/data/legacy_empty"]


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


@pytest.mark.asyncio
async def test_phase2_resume_does_not_load_empty_dirs_into_memory(temp_dir, tmp_path):
    """Regression: carry-over empty_dirs from prior runs must NOT live in self.empty_dirs during Phase 2.

    Prod symptom (death-spiral, ~1 dir/s, exit 75 every 10 min): on resume the
    purger materialised the entire saved empty_dirs list into a ``set[Path]``
    before workers started, pushing memory above the 85% back-pressure
    threshold for the entire run.  This test asserts the in-memory set is
    empty at the start of Phase 2 even when the checkpoint records many
    carry-over empty-dir candidates, and that those candidates are still
    available to Phase 3 (via the sidecar) once scanning finishes.
    """
    # Directory that the checkpoint says was found empty in a prior run.
    carry_over = temp_dir / "carry_over"
    carry_over.mkdir()

    # Something for Phase 2 to actually scan so we exercise the full code path.
    scan_target = temp_dir / "to_scan"
    scan_target.mkdir()
    (scan_target / "file.txt").write_text("x")

    cp_file = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(scan_target)],
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
        empty_dirs=[str(carry_over)],
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

    # Patch _remove_empty_directories to snapshot the in-memory state right
    # before Phase 3 streams the sidecar back in.
    state_before_phase3: dict = {}
    original_remove = purger._remove_empty_directories

    async def spy_remove():
        # At this point Phase 2 is done; self.empty_dirs should contain only
        # what *this run* found (nothing in this test, since scan_target has
        # a file in it and so isn't empty).  Carry-over dirs must NOT be
        # present yet — they are streamed from the sidecar inside Phase 3.
        state_before_phase3["empty_dirs_count_pre_phase3"] = len(purger.empty_dirs)
        state_before_phase3["carry_over_in_memory"] = Path(str(carry_over)) in purger.empty_dirs
        await original_remove()

    purger._remove_empty_directories = spy_remove

    await purger.purge()

    # Phase 2 must not have held the carry-over dir in RAM.
    assert state_before_phase3["empty_dirs_count_pre_phase3"] == 0, (
        f"Phase 2 should not hold carry-over empty_dirs in memory; "
        f"found {state_before_phase3['empty_dirs_count_pre_phase3']}"
    )
    assert state_before_phase3["carry_over_in_memory"] is False

    # Phase 3 still picked up the carry-over dir from the sidecar and deleted it.
    assert not carry_over.exists(), "Phase 3 should still delete carry-over empty dirs via sidecar"


# ---------------------------------------------------------------------------
# Phase 2 pending_dirs (BFS frontier) moved to a sidecar
# ---------------------------------------------------------------------------


def test_save_checkpoint_writes_pending_dirs_to_sidecar(tmp_path):
    """save_checkpoint streams the BFS frontier into ``<cp>.pending_dirs.gz``.

    Before this change the frontier was embedded in the main checkpoint
    JSON as a ``list[str]``.  At ~30 M entries observed on prod that
    list alone consumed several GB on resume — the second wave of the
    death-spiral that recurred on 2026-06-04 after the empty_dirs
    sidecar fix shipped.  The frontier now lives on disk and is streamed
    line-by-line into the bounded scan queue by a feeder task.
    """
    from efspurge.checkpoint import pending_dirs_sidecar_path, stream_pending_dirs_sidecar

    cp = tmp_path / "cp.json"
    n = save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a", "/data/b", "/data/c"],
        stats={"dirs_scanned": 100},
        config={"max_age_days": 30},
    )
    assert n == 3

    # Sidecar holds the paths; main JSON holds only the count.
    assert pending_dirs_sidecar_path(cp).exists()
    assert list(stream_pending_dirs_sidecar(cp)) == ["/data/a", "/data/b", "/data/c"]

    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert loaded["pending_dirs_count"] == 3
    assert not loaded.get("pending_dirs"), "Frontier paths should not be inline in main JSON"


def test_save_checkpoint_accepts_generator_as_pending_dirs(tmp_path):
    """save_checkpoint never materialises the iterable as a Python list.

    This is the key memory invariant for the fix: a 30 M-entry generator
    can be passed in and only one line at a time touches Python memory.
    We assert by passing a generator that *would* fail if iterated twice.
    """
    from efspurge.checkpoint import stream_pending_dirs_sidecar

    cp = tmp_path / "cp.json"

    def single_use_gen():
        yield "/data/a"
        yield "/data/b"

    n = save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=single_use_gen(),
        stats={},
        config={},
    )
    assert n == 2
    assert list(stream_pending_dirs_sidecar(cp)) == ["/data/a", "/data/b"]


def test_save_checkpoint_empty_pending_drops_sidecar(tmp_path):
    """Saving with an empty frontier removes any existing sidecar.

    Otherwise an empty trailing sidecar would survive across saves and
    confuse the "done" detection in load_checkpoint.
    """
    from efspurge.checkpoint import pending_dirs_sidecar_path

    cp = tmp_path / "cp.json"
    # First save with content.
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=["/data/a"],
        stats={},
        config={},
    )
    assert pending_dirs_sidecar_path(cp).exists()

    # Subsequent save with empty frontier — sidecar should be cleared.
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=[],
        stats={},
        config={},
    )
    assert not pending_dirs_sidecar_path(cp).exists()
    # And load_checkpoint treats the result as "done".
    assert load_checkpoint(cp) is None


def test_load_checkpoint_legacy_embedded_pending_dirs_preserved(tmp_path):
    """A legacy checkpoint with the frontier embedded inline is still readable.

    The purger surfaces ``cp["pending_dirs"]`` to its resume code so the
    list can be migrated to the sidecar on the first post-upgrade resume.
    """
    cp = tmp_path / "cp.json"
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": "/data",
        "phase": "phase2",
        "pending_dirs": ["/data/legacy_a", "/data/legacy_b"],
        "stats": {},
        "config": {},
    }
    cp.write_text(json.dumps(data))
    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert loaded["pending_dirs"] == ["/data/legacy_a", "/data/legacy_b"]


@pytest.mark.asyncio
async def test_phase2_checkpoint_exit_merges_inflight_and_feeder_tail(temp_dir, tmp_path):
    """Regression: a checkpoint exit mid-resume must preserve every frontier entry.

    The novel merge path in commit <pending_dirs_sidecar> writes the new
    sidecar from a generator that yields (queue contents → per-worker
    buffers → feeder's unsent path → feeder's unread file tail).  A bug
    in any of those legs would silently drop directories on a memory-
    critical exit, leaving them un-scanned forever.

    This test seeds a sidecar with N frontier entries, resumes with a
    queue smaller than N so the feeder cannot drain everything in one
    pass, raises the checkpoint flag, and asserts the *new* sidecar
    written on exit contains the full original set (minus anything that
    was actually scanned, which we lower-bound via the dirs_scanned
    stat).
    """
    from efspurge.checkpoint import stream_pending_dirs_sidecar
    from efspurge.purger import AsyncEFSPurger, CheckpointExit

    # Build a small tree of scannable dirs so the resume path is exercised.
    seeded: list[Path] = []
    for i in range(40):
        d = temp_dir / f"seed_{i:02d}"
        d.mkdir()
        # Each dir empty — fast scans so the feeder gets multiple turns.
        seeded.append(d)

    cp_file = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(p) for p in seeded],
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=False,
        memory_limit_mb=0,
        dry_run=True,
        checkpoint_file=str(cp_file),
        resume=True,
        queue_maxsize=5,  # < 40 so the feeder must serve in multiple passes
        max_concurrent_discovery=2,
    )

    async def _trigger():
        # Let the feeder/workers churn briefly so some entries are scanned
        # and some are still in the feeder's unread tail / in-flight queue.
        await asyncio.sleep(0.3)
        purger._checkpoint_requested = True

    asyncio.create_task(_trigger())
    with pytest.raises(CheckpointExit):
        await purger._scan_and_purge_files()

    # New sidecar must collectively cover every directory that wasn't
    # already counted as scanned.  In practice with such a small tree we
    # expect *all* 40 to be present (the loop almost certainly hadn't
    # scanned anything before the flag flipped).
    new_pending = set(stream_pending_dirs_sidecar(cp_file))
    seeded_strs = {str(p) for p in seeded}
    scanned = purger.stats.get("dirs_scanned", 0)
    missing = seeded_strs - new_pending
    assert len(missing) <= scanned, (
        f"Checkpoint exit dropped frontier entries: dirs_scanned={scanned}, "
        f"missing from new sidecar={sorted(missing)[:10]} (total {len(missing)})"
    )


@pytest.mark.asyncio
async def test_phase2_resume_migrates_legacy_embedded_pending_dirs(temp_dir, tmp_path):
    """End-to-end: a legacy checkpoint with inline ``pending_dirs`` migrates to the sidecar.

    Older checkpoints (pre-sidecar format) embedded the frontier directly
    in the main JSON.  The Phase 2 resume code calls
    ``write_pending_dirs_sidecar`` to migrate the inline list before any
    worker starts, so the resume baseline is the same on the first
    post-upgrade resume as on every subsequent one.  This test exercises
    that path end-to-end.
    """
    from efspurge.checkpoint import pending_dirs_sidecar_path
    from efspurge.purger import AsyncEFSPurger

    # Seed dir lives under root and carries the only file we expect to
    # see in stats.  A *sibling* subtree under root holds files we expect
    # NOT to be scanned, since the legacy checkpoint's frontier names
    # only the seed dir.  If migration accidentally falls through to
    # the "fresh start" branch and re-seeds root, the sibling files
    # would be counted too and the assertion below catches it.
    seed = temp_dir / "legacy_seed"
    seed.mkdir()
    (seed / "f.txt").write_text("x")

    sibling = temp_dir / "should_be_untouched"
    sibling.mkdir()
    (sibling / "ignored_a.txt").write_text("y")
    (sibling / "ignored_b.txt").write_text("z")

    cp_file = tmp_path / "cp.json"
    # Hand-craft a legacy checkpoint: gzip+JSON with embedded pending_dirs,
    # no sidecar present.  Mirrors the on-disk shape produced by the
    # pre-sidecar save_checkpoint().
    import gzip as _gzip

    legacy = {
        "version": CHECKPOINT_VERSION,
        "root_path": str(temp_dir),
        "phase": "phase2",
        "pending_dirs": [str(seed)],
        "stats": {"files_scanned": 0, "dirs_scanned": 0},
        "config": {"max_age_days": 30, "root_path": str(temp_dir)},
    }
    with _gzip.open(cp_file, "wt") as f:
        json.dump(legacy, f)
    assert not pending_dirs_sidecar_path(cp_file).exists()

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=False,
        memory_limit_mb=0,
        dry_run=False,
        checkpoint_file=str(cp_file),
        resume=True,
    )
    await purger.purge()

    # Phase 2 ran to completion: both the checkpoint and the (migrated)
    # sidecar are gone, and the legacy entry was actually scanned.
    assert not cp_file.exists(), "Checkpoint should be removed after a successful purge"
    assert not pending_dirs_sidecar_path(cp_file).exists(), (
        "Migrated sidecar should be removed on clean Phase 2 completion"
    )
    # Only the legacy-checkpoint frontier was scanned — not the entire tree
    # under root.  If migration forgets to propagate the migrated count
    # into pending_dirs_count, the resume code falls into the "fresh
    # start" branch and re-seeds the root, sweeping up the sibling tree.
    assert purger.stats["files_scanned"] == 1, (
        f"Migration regression: expected to scan only seed/f.txt, "
        f"got files_scanned={purger.stats['files_scanned']} — likely a "
        f"redundant root reseed after legacy migration."
    )


@pytest.mark.asyncio
async def test_phase2_resume_streams_pending_dirs_without_materialising(temp_dir, tmp_path):
    """Regression: Phase 2 resume must not allocate a Python list of all frontier paths.

    Production symptom (2026-06-04): after a normal day of operation the
    frontier grew past 30 M entries, and the Phase 2 resume code
    ``cp["pending_dirs"]`` materialised the entire list as ``list[str]``,
    pushing the resume baseline above 85 % memory and re-triggering the
    death-spiral.  This test asserts that ``load_checkpoint`` no longer
    surfaces the frontier as an in-memory list — the paths are streamed
    from the sidecar by a feeder task instead.
    """
    # Stand up a small Phase-2 scan target so the purge runs end-to-end.
    scan_target = temp_dir / "to_scan"
    scan_target.mkdir()
    (scan_target / "f.txt").write_text("x")

    cp_file = tmp_path / "cp.json"
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(scan_target)],
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
    )

    # Verify the in-memory shape: load_checkpoint should not return a
    # populated ``pending_dirs`` list (only ``pending_dirs_count``).
    loaded = load_checkpoint(cp_file)
    assert loaded is not None
    assert not loaded.get("pending_dirs"), (
        "load_checkpoint must not return frontier paths as an in-memory list — "
        "those should be streamed from the sidecar by the Phase 2 feeder task."
    )
    assert loaded["pending_dirs_count"] == 1

    # End-to-end: the resume still works (feeder picks the path up from disk).
    from efspurge.purger import AsyncEFSPurger

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        remove_empty_dirs=False,
        memory_limit_mb=0,
        dry_run=False,
        checkpoint_file=str(cp_file),
        resume=True,
    )
    await purger.purge()
    # Checkpoint and sidecar are both removed on clean completion.
    assert not cp_file.exists()
    from efspurge.checkpoint import pending_dirs_sidecar_path

    assert not pending_dirs_sidecar_path(cp_file).exists()


# ---------------------------------------------------------------------------
# save_phase1b_checkpoint / load_phase1b_checkpoint unit tests
# ---------------------------------------------------------------------------


def test_save_phase1b_checkpoint_creates_valid_json(tmp_path):
    """save_phase1b_checkpoint writes valid JSON with expected keys."""
    cp = tmp_path / "phase1b.json"
    save_phase1b_checkpoint(
        filepath=cp,
        root_path="/data/root",
        dirs_by_depth={"3": ["/data/root/a/b/c"], "2": ["/data/root/a/b"]},
        config={"root_path": "/data/root"},
    )
    assert cp.exists()
    data = json.loads(cp.read_text())
    assert data["version"] == CHECKPOINT_VERSION
    assert data["phase"] == "phase1b"
    assert data["root_path"] == "/data/root"
    assert data["dirs_by_depth"]["3"] == ["/data/root/a/b/c"]
    assert data["dirs_by_depth"]["2"] == ["/data/root/a/b"]


def test_load_phase1b_checkpoint_roundtrip(tmp_path):
    """load_phase1b_checkpoint returns the saved data."""
    cp = tmp_path / "phase1b.json"
    save_phase1b_checkpoint(
        filepath=cp,
        root_path="/data/root",
        dirs_by_depth={"3": ["/data/root/a/b/c"]},
        config={"root_path": "/data/root"},
    )
    result = load_phase1b_checkpoint(cp)
    assert result is not None
    assert result["phase"] == "phase1b"
    assert result["dirs_by_depth"]["3"] == ["/data/root/a/b/c"]


def test_load_phase1b_checkpoint_missing_file(tmp_path):
    """Returns None when checkpoint file does not exist."""
    result = load_phase1b_checkpoint(tmp_path / "nonexistent.json")
    assert result is None


def test_load_phase1b_checkpoint_invalid_json(tmp_path):
    """Returns None on malformed JSON."""
    cp = tmp_path / "bad.json"
    cp.write_text("not json")
    assert load_phase1b_checkpoint(cp) is None


def test_load_phase1b_checkpoint_wrong_phase(tmp_path):
    """Returns None when phase field is not phase1b."""
    cp = tmp_path / "cp.json"
    save_phase1a_checkpoint(cp, "/data", ["/data/a"], {})
    assert load_phase1b_checkpoint(cp) is None


def test_load_phase1b_checkpoint_version_mismatch(tmp_path):
    """Returns None on version mismatch."""
    cp = tmp_path / "cp.json"
    cp.write_text(
        json.dumps(
            {
                "version": 999,
                "phase": "phase1b",
                "root_path": "/data",
                "dirs_by_depth": {"2": ["/data/a/b"]},
                "config": {},
            }
        )
    )
    assert load_phase1b_checkpoint(cp) is None


def test_load_phase1b_checkpoint_empty_dirs(tmp_path):
    """Returns None when all dirs_by_depth lists are empty."""
    cp = tmp_path / "cp.json"
    save_phase1b_checkpoint(cp, "/data", {"2": [], "3": []}, {})
    assert load_phase1b_checkpoint(cp) is None


def test_load_phase1a_checkpoint_rejects_phase1b_file(tmp_path):
    """load_phase1a_checkpoint returns None for a phase1b file."""
    cp = tmp_path / "cp.json"
    save_phase1b_checkpoint(cp, "/data", {"2": ["/data/a/b"]}, {})
    assert load_phase1a_checkpoint(cp) is None


def test_load_checkpoint_rejects_phase1b_file(tmp_path):
    """load_checkpoint returns None for a phase1b file."""
    cp = tmp_path / "cp.json"
    save_phase1b_checkpoint(cp, "/data", {"2": ["/data/a/b"]}, {})
    assert load_checkpoint(cp) is None


# ---------------------------------------------------------------------------
# --phase1-only flag: purger params and CLI plumbing
# ---------------------------------------------------------------------------


def test_phase1_only_default_false():
    """phase1_only defaults to False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = AsyncEFSPurger(root_path=tmpdir, max_age_days=30)
        assert p.phase1_only is False


def test_phase1_only_stored():
    """phase1_only=True is stored on the purger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = AsyncEFSPurger(root_path=tmpdir, max_age_days=0, phase1_only=True)
        assert p.phase1_only is True


def test_cli_phase1_only_flag(tmp_path):
    """--phase1-only flag is parsed and passed to async_main."""
    from efspurge.cli import parse_args

    args = parse_args(
        [
            str(tmp_path),
            "--max-age-days",
            "0",
            "--phase1-only",
        ]
    )
    assert args.phase1_only is True


def test_cli_phase1_only_env_var(tmp_path, monkeypatch):
    """EFSPURGE_PHASE1_ONLY=1 sets phase1_only via env var."""
    from efspurge.cli import parse_args

    monkeypatch.setenv("EFSPURGE_PHASE1_ONLY", "1")
    args = parse_args([str(tmp_path), "--max-age-days", "0"])
    assert args.phase1_only is True


def test_cli_phase1_only_default_false(tmp_path, monkeypatch):
    """phase1_only defaults to False with no flag or env var."""
    from efspurge.cli import parse_args

    monkeypatch.delenv("EFSPURGE_PHASE1_ONLY", raising=False)
    args = parse_args([str(tmp_path), "--max-age-days", "30"])
    assert args.phase1_only is False


# ---------------------------------------------------------------------------
# main() exit-path tests
# ---------------------------------------------------------------------------


def test_main_checkpoint_exit_uses_os_exit(tmp_path, monkeypatch):
    """main() must call os._exit(75) for CheckpointExit, not sys.exit(75).

    sys.exit() triggers ThreadPoolExecutor's atexit handler which waits for
    all in-flight threads (EFS scandir calls), causing multi-minute hangs.
    os._exit() bypasses atexit and exits immediately once the checkpoint is
    safely on disk.
    """
    from unittest.mock import AsyncMock, patch

    from efspurge.cli import main

    monkeypatch.setattr("sys.argv", ["efspurge", str(tmp_path), "--max-age-days", "0"])

    with patch("efspurge.cli.async_main", new=AsyncMock(side_effect=CheckpointExit("test cp"))):
        with patch("os._exit") as mock_os_exit:
            main()
            mock_os_exit.assert_called_once_with(75)


def test_main_success_uses_sys_exit_0(tmp_path, monkeypatch):
    """main() calls sys.exit(0) on success (executor is already shut down cleanly)."""
    from unittest.mock import AsyncMock, patch

    from efspurge.cli import main

    monkeypatch.setattr("sys.argv", ["efspurge", str(tmp_path), "--max-age-days", "0"])

    with patch("efspurge.cli.async_main", new=AsyncMock(return_value={})):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# --phase1-only: async behaviour tests
# ---------------------------------------------------------------------------


async def test_phase1_only_skips_phase2_and_deletes_empty_dirs(tmp_path):
    """With --phase1-only, Phase 2 is skipped and empty dirs are deleted."""
    # Create: empty subdir + subdir with a file
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    nonempty = tmp_path / "nonempty_dir"
    nonempty.mkdir()
    (nonempty / "file.txt").write_text("data")

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
    )
    await purger.purge()

    # Empty dir deleted, nonempty preserved, file preserved
    assert not empty.exists()
    assert nonempty.exists()
    assert (nonempty / "file.txt").exists()


async def test_phase1_only_does_not_delete_files(tmp_path):
    """With --phase1-only, no files are deleted regardless of age."""
    old_dir = tmp_path / "old_dir"
    old_dir.mkdir()
    old_file = old_dir / "old_file.txt"
    old_file.write_text("old")
    # Set mtime to 100 days ago
    import os
    import time

    old_time = time.time() - 100 * 86400
    os.utime(old_file, (old_time, old_time))

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=30,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
    )
    await purger.purge()

    # File must still exist — Phase 2 was skipped
    assert old_file.exists()


# ---------------------------------------------------------------------------
# Phase 1b checkpoint: memory abort during deletion
# ---------------------------------------------------------------------------


async def test_phase1b_checkpoint_saved_on_memory_abort(tmp_path):
    """When memory aborts during Phase 1b, a phase1b checkpoint is saved and CheckpointExit raised."""
    # Create several empty dirs
    for i in range(5):
        (tmp_path / f"empty_{i}").mkdir()

    cp_file = tmp_path / "dircp.json"

    # Simulate memory critical (>95%) on first Phase 1b batch memory check
    call_count = 0

    def mock_memory_mb():
        nonlocal call_count
        call_count += 1
        # Return normal memory for Phase 1a discovery, then critical for Phase 1b
        if call_count <= 5:
            return 10.0  # Phase 1a: low memory
        return 5226.0  # Phase 1b: 5226/5500 = 0.9502 > 0.95 threshold

    with patch("efspurge.purger.get_memory_usage_mb", side_effect=mock_memory_mb):
        purger = AsyncEFSPurger(
            root_path=str(tmp_path),
            max_age_days=0,
            remove_empty_dirs=True,
            memory_limit_mb=5500,
            dry_run=False,
            phase1_only=True,
            dir_deletion_checkpoint_file=str(cp_file),
        )
        with pytest.raises(CheckpointExit):
            await purger.purge()

    # Checkpoint file should exist with phase=phase1b
    assert cp_file.exists()
    data = json.loads(cp_file.read_text())
    assert data["phase"] == "phase1b"
    assert "dirs_by_depth" in data


async def test_phase1b_no_checkpoint_without_flag(tmp_path):
    """Memory abort during Phase 1b does NOT save checkpoint when no checkpoint file configured."""
    for i in range(3):
        (tmp_path / f"empty_{i}").mkdir()

    call_count = 0

    def mock_memory_mb():
        nonlocal call_count
        call_count += 1
        return 10.0 if call_count <= 5 else 5225.0

    with patch("efspurge.purger.get_memory_usage_mb", side_effect=mock_memory_mb):
        purger = AsyncEFSPurger(
            root_path=str(tmp_path),
            max_age_days=0,
            remove_empty_dirs=True,
            memory_limit_mb=5500,
            dry_run=False,
            phase1_only=True,
            # No dir_deletion_checkpoint_file
        )
        # Should complete without CheckpointExit (just stops deletion early)
        await purger.purge()


async def test_phase1b_resume_skips_phase1a_and_deletes(tmp_path):
    """Resuming from Phase 1b checkpoint skips Phase 1a and deletes remaining empty dirs."""
    # Create empty dirs
    empty_a = tmp_path / "a"
    empty_b = tmp_path / "b"
    empty_a.mkdir()
    empty_b.mkdir()

    cp_file = tmp_path / "dircp.json"

    # Save a phase1b checkpoint with these dirs at depth 1
    depth = 1
    save_phase1b_checkpoint(
        filepath=cp_file,
        root_path=str(tmp_path),
        dirs_by_depth={str(depth): [str(empty_a), str(empty_b)]},
        config={"root_path": str(tmp_path)},
    )

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
    )
    await purger.purge()

    # Both empty dirs should be deleted
    assert not empty_a.exists()
    assert not empty_b.exists()
    # Checkpoint file should be deleted on clean completion
    assert not cp_file.exists()


async def test_phase1b_resume_checkpoint_deleted_on_clean_completion(tmp_path):
    """Phase 1b checkpoint file is deleted when deletion completes cleanly."""
    empty = tmp_path / "empty"
    empty.mkdir()
    cp_file = tmp_path / "dircp.json"

    save_phase1b_checkpoint(
        filepath=cp_file,
        root_path=str(tmp_path),
        dirs_by_depth={"1": [str(empty)]},
        config={},
    )

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
    )
    await purger.purge()

    assert not cp_file.exists()


async def test_phase1b_resume_multiple_depths(tmp_path):
    """Phase 1b resume handles multiple depth levels correctly (deepest first)."""
    # Create two-level deep structure: tmp/a/b (depth 2), tmp/a (depth 1)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    shallow = tmp_path / "a"

    cp_file = tmp_path / "dircp.json"
    save_phase1b_checkpoint(
        filepath=cp_file,
        root_path=str(tmp_path),
        dirs_by_depth={
            "2": [str(deep)],
            "1": [str(shallow)],
        },
        config={},
    )

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
    )
    await purger.purge()

    # Both levels should be deleted bottom-up
    assert not deep.exists()
    assert not shallow.exists()


async def test_phase1b_resume_prefers_phase1b_over_phase1a_checkpoint(tmp_path):
    """When phase1b checkpoint exists, it takes priority over phase1a checkpoint."""
    empty = tmp_path / "empty"
    empty.mkdir()

    cp_file = tmp_path / "dircp.json"
    # Write a phase1b checkpoint (not phase1a)
    save_phase1b_checkpoint(
        filepath=cp_file,
        root_path=str(tmp_path),
        dirs_by_depth={"1": [str(empty)]},
        config={},
    )

    # load_phase1a_checkpoint should return None (wrong phase)
    assert load_phase1a_checkpoint(cp_file) is None
    # load_phase1b_checkpoint should return the data
    assert load_phase1b_checkpoint(cp_file) is not None

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
    )
    await purger.purge()

    assert not empty.exists()


async def test_phase1b_resume_with_nonempty_dirs_preserved(tmp_path):
    """Phase 1b resume skips non-empty dirs — only truly empty ones are deleted."""
    empty = tmp_path / "empty"
    nonempty = tmp_path / "nonempty"
    empty.mkdir()
    nonempty.mkdir()
    (nonempty / "file.txt").write_text("data")

    cp_file = tmp_path / "dircp.json"
    save_phase1b_checkpoint(
        filepath=cp_file,
        root_path=str(tmp_path),
        dirs_by_depth={"1": [str(empty), str(nonempty)]},
        config={},
    )

    purger = AsyncEFSPurger(
        root_path=str(tmp_path),
        max_age_days=0,
        remove_empty_dirs=True,
        memory_limit_mb=0,
        dry_run=False,
        phase1_only=True,
        dir_deletion_checkpoint_file=str(cp_file),
        dir_deletion_resume=True,
    )
    await purger.purge()

    assert not empty.exists()
    assert nonempty.exists()
    assert (nonempty / "file.txt").exists()
