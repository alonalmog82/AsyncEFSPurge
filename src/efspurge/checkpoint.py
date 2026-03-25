"""Checkpoint/resume support for long-running purge operations."""

import gzip
import json
import logging
import os
import tempfile
from pathlib import Path

CHECKPOINT_VERSION = 1
_logger = logging.getLogger("efspurge")


def save_checkpoint(
    filepath: Path,
    root_path: str,
    pending_dirs: list[str],
    stats: dict,
    config: dict,
    empty_dirs: list[str] | None = None,
) -> None:
    """
    Save a checkpoint to disk.

    Args:
        filepath: Path to write checkpoint JSON
        root_path: Root path being purged
        pending_dirs: List of directory paths still to scan (Phase 2)
        stats: Partial stats (files_scanned, dirs_scanned, etc.)
        config: Key config for validation on resume
        empty_dirs: Directories found empty during Phase 2 scan so far (for Phase 3 on resume)
    """
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": root_path,
        "phase": "phase2",
        "pending_dirs": pending_dirs,
        "stats": stats,
        "config": config,
        "empty_dirs": empty_dirs or [],
    }
    # Atomic write: write to a temp file on the same filesystem, then rename.
    # This ensures the previous checkpoint is never truncated unless the new one
    # is fully written — critical when an NFS open(O_TRUNC) on the existing file
    # can hang indefinitely (same NFS saturation mechanism as getdents).
    #
    # Gzip (level 1): reduces ~500 MB plain-JSON to ~60 MB, cutting NFS write time
    # from 50+ s to ~6 s even on a saturated mount — well within the OOMKill window.
    # Level 1 (fastest) is intentional: we want minimum CPU overhead and maximum
    # throughput, not maximum compression ratio.
    filepath = Path(filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1) as gz:
                gz.write(json.dumps(data).encode())
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_phase1a_checkpoint(
    filepath: Path,
    root_path: str,
    pending_dirs: list[str],
    config: dict,
) -> None:
    """
    Save a Phase 1a (directory discovery) checkpoint to disk.

    Args:
        filepath: Path to write checkpoint JSON
        root_path: Root path being purged
        pending_dirs: BFS frontier — directories still to be scanned
        config: Key config for validation on resume
    """
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": root_path,
        "phase": "phase1a",
        "pending_dirs": pending_dirs,
        "config": config,
    }
    filepath = Path(filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_phase1a_checkpoint(filepath: Path) -> dict | None:
    """
    Load a Phase 1a checkpoint from disk.

    Returns:
        Checkpoint dict with keys: root_path, pending_dirs, config; or None if invalid/missing
    """
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _logger.warning("Cannot load Phase 1a checkpoint: %s", e)
        return None

    if data.get("version") != CHECKPOINT_VERSION:
        _logger.warning(
            "Phase 1a checkpoint version mismatch: expected %s, got %s",
            CHECKPOINT_VERSION,
            data.get("version"),
        )
        return None

    if data.get("phase") != "phase1a":
        _logger.warning("Checkpoint is not Phase 1a: %s", data.get("phase"))
        return None

    pending = data.get("pending_dirs", [])
    if not pending:
        _logger.info("Phase 1a checkpoint has no pending directories, treating as complete")
        return None

    return data


def save_phase1b_checkpoint(
    filepath: Path,
    root_path: str,
    dirs_by_depth: dict,
    config: dict,
) -> None:
    """
    Save a Phase 1b (bottom-up deletion) checkpoint to disk.

    Args:
        filepath: Path to write checkpoint JSON
        root_path: Root path being purged
        dirs_by_depth: Remaining dirs to process, keyed by depth (str) → list of path strings
        config: Key config for validation on resume
    """
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": root_path,
        "phase": "phase1b",
        "dirs_by_depth": dirs_by_depth,
        "config": config,
    }
    filepath = Path(filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_phase1b_checkpoint(filepath: Path) -> dict | None:
    """
    Load a Phase 1b checkpoint from disk.

    Returns:
        Checkpoint dict with keys: root_path, dirs_by_depth, config; or None if invalid/missing
    """
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _logger.warning("Cannot load Phase 1b checkpoint: %s", e)
        return None

    if data.get("version") != CHECKPOINT_VERSION:
        _logger.warning(
            "Phase 1b checkpoint version mismatch: expected %s, got %s",
            CHECKPOINT_VERSION,
            data.get("version"),
        )
        return None

    if data.get("phase") != "phase1b":
        _logger.warning("Checkpoint is not Phase 1b: %s", data.get("phase"))
        return None

    dirs_by_depth = data.get("dirs_by_depth", {})
    if not dirs_by_depth or not any(dirs_by_depth.values()):
        _logger.info("Phase 1b checkpoint has no remaining directories, treating as complete")
        return None

    return data


def load_checkpoint(filepath: Path) -> dict | None:
    """
    Load a checkpoint from disk.

    Supports both gzip-compressed (new) and plain-JSON (legacy) checkpoint files.

    Returns:
        Checkpoint dict with keys: root_path, pending_dirs, stats, config, empty_dirs; or None if invalid/missing
    """
    try:
        try:
            with gzip.open(filepath, "rt") as f:
                data = json.load(f)
        except gzip.BadGzipFile:
            # Legacy plain-JSON checkpoint written before gzip was introduced.
            with open(filepath) as f:
                data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _logger.warning("Cannot load checkpoint: %s", e)
        return None

    if data.get("version") != CHECKPOINT_VERSION:
        _logger.warning(
            "Checkpoint version mismatch: expected %s, got %s",
            CHECKPOINT_VERSION,
            data.get("version"),
        )
        return None

    if data.get("phase") != "phase2":
        _logger.warning("Checkpoint phase not supported: %s", data.get("phase"))
        return None

    pending = data.get("pending_dirs", [])
    if not pending:
        _logger.info("Checkpoint has no pending directories, treating as complete")
        return None

    return data
