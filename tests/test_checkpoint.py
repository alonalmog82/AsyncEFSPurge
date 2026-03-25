"""Tests for checkpoint/resume support (save_checkpoint, load_checkpoint).

When memory exceeds 95%, the purger can save Phase 2 state (pending_dirs, stats, config)
to a checkpoint file and exit. Use --resume to continue from the checkpoint.
"""

import json
import tempfile
from pathlib import Path

import pytest

from efspurge.checkpoint import (
    CHECKPOINT_VERSION,
    load_checkpoint,
    save_checkpoint,
)
from efspurge.purger import AsyncEFSPurger


def test_save_checkpoint_creates_valid_json(tmp_path):
    """save_checkpoint writes valid gzip-compressed JSON with expected keys."""
    import gzip

    cp = tmp_path / "checkpoint.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data/root",
        pending_dirs=["/data/root/a", "/data/root/b"],
        stats={"files_scanned": 10, "dirs_scanned": 5},
        config={"max_age_days": 30},
    )
    assert cp.exists()
    with gzip.open(cp, "rt") as f:
        data = json.loads(f.read())
    assert data["version"] == CHECKPOINT_VERSION
    assert data["phase"] == "phase2"
    assert data["root_path"] == "/data/root"
    assert data["pending_dirs"] == ["/data/root/a", "/data/root/b"]
    assert data["stats"]["files_scanned"] == 10
    assert data["config"]["max_age_days"] == 30


def test_load_checkpoint_roundtrip(tmp_path):
    """load_checkpoint returns data saved by save_checkpoint."""
    cp = tmp_path / "checkpoint.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data/api_files",
        pending_dirs=["/data/api_files/org1", "/data/api_files/org2"],
        stats={"files_scanned": 100, "dirs_scanned": 50},
        config={"max_age_days": 30, "dry_run": False},
    )
    loaded = load_checkpoint(cp)
    assert loaded is not None
    assert loaded["root_path"] == "/data/api_files"
    assert loaded["pending_dirs"] == ["/data/api_files/org1", "/data/api_files/org2"]
    assert loaded["stats"]["files_scanned"] == 100
    assert loaded["config"]["max_age_days"] == 30


def test_load_checkpoint_missing_file_returns_none(tmp_path):
    """load_checkpoint returns None for missing file."""
    assert load_checkpoint(tmp_path / "nonexistent.json") is None


def test_load_checkpoint_invalid_json_returns_none(tmp_path):
    """load_checkpoint returns None for invalid JSON."""
    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    assert load_checkpoint(bad) is None


def test_load_checkpoint_version_mismatch_returns_none(tmp_path):
    """load_checkpoint returns None when version does not match."""
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({"version": 99, "phase": "phase2", "pending_dirs": ["/a"]}))
    assert load_checkpoint(cp) is None


def test_load_checkpoint_phase_mismatch_returns_none(tmp_path):
    """load_checkpoint returns None when phase is not phase2."""
    cp = tmp_path / "checkpoint.json"
    cp.write_text(
        json.dumps(
            {
                "version": CHECKPOINT_VERSION,
                "phase": "phase1",
                "pending_dirs": ["/a"],
            }
        )
    )
    assert load_checkpoint(cp) is None


def test_load_checkpoint_empty_pending_returns_none(tmp_path):
    """load_checkpoint returns None when pending_dirs is empty (treat as complete)."""
    cp = tmp_path / "checkpoint.json"
    save_checkpoint(
        filepath=cp,
        root_path="/data",
        pending_dirs=[],
        stats={},
        config={},
    )
    assert load_checkpoint(cp) is None


# ---------------------------------------------------------------------------
# Purger checkpoint_file / resume parameter tests
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.mark.asyncio
async def test_checkpoint_file_and_resume_stored(temp_dir, tmp_path):
    """checkpoint_file and resume are stored on the purger."""
    cp_file = tmp_path / "cp.json"
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        checkpoint_file=str(cp_file),
        resume=True,
    )
    assert purger.checkpoint_file == cp_file
    assert purger.resume is True


@pytest.mark.asyncio
async def test_checkpoint_file_none_by_default(temp_dir):
    """checkpoint_file is None when not specified."""
    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
    )
    assert purger.checkpoint_file is None
    assert purger.resume is False


# ---------------------------------------------------------------------------
# CLI / environment variable plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_checkpoint_file_env_var(temp_dir, tmp_path):
    """EFSPURGE_CHECKPOINT_FILE env var is respected via parse_args."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"EFSPURGE_CHECKPOINT_FILE": str(tmp_path / "cp.json")}):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.checkpoint_file == str(tmp_path / "cp.json")


@pytest.mark.asyncio
async def test_cli_resume_env_var(temp_dir):
    """EFSPURGE_RESUME=1 is respected via parse_args."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"EFSPURGE_RESUME": "1"}):
        with patch("sys.argv", ["efspurge", str(temp_dir)]):
            from efspurge.cli import parse_args

            args = parse_args()
            assert args.resume is True


# ---------------------------------------------------------------------------
# Checkpoint resume with more paths than queue_maxsize (loader task fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_resume_completes_when_pending_exceeds_queue_maxsize(temp_dir, tmp_path):
    """Resume with more pending dirs than queue_maxsize completes (loader task feeds queue)."""
    # Create 25 subdirs (each with a file). queue_maxsize=10, so we need loader to feed the rest.
    for i in range(25):
        subdir = temp_dir / f"dir_{i}"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

    cp_file = tmp_path / "checkpoint.json"
    # Simulate checkpoint with 25 pending dirs (root + 25 subdirs)
    pending_paths = [str(temp_dir)] + [str(temp_dir / f"dir_{i}") for i in range(25)]
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=pending_paths,
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        checkpoint_file=cp_file,
        resume=True,
        queue_maxsize=10,  # Smaller than 25
        max_concurrent_discovery=5,
    )

    await purger.purge()

    # Must complete without hanging (loader feeds queue when pending > queue_maxsize).
    # Due to overlap between checkpoint paths and discovered subdirs, we may scan
    # some dirs twice; the key is we finish and process all files.
    assert purger.stats["files_scanned"] >= 25
    assert purger.stats["dirs_scanned"] >= 26
    # Checkpoint should be removed after successful purge
    assert not cp_file.exists()


@pytest.mark.asyncio
async def test_checkpoint_file_removed_after_successful_purge(temp_dir, tmp_path):
    """Checkpoint file is removed when purge completes successfully."""
    (temp_dir / "file.txt").write_text("content")

    cp_file = tmp_path / "checkpoint.json"
    save_checkpoint(
        filepath=cp_file,
        root_path=str(temp_dir),
        pending_dirs=[str(temp_dir)],
        stats={"files_scanned": 0, "dirs_scanned": 0},
        config={"max_age_days": 30},
    )

    purger = AsyncEFSPurger(
        root_path=str(temp_dir),
        max_age_days=30,
        dry_run=True,
        checkpoint_file=cp_file,
        resume=True,
    )

    await purger.purge()

    assert not cp_file.exists(), "Checkpoint file should be removed after successful purge"
