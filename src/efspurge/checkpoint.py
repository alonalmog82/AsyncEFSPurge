"""Checkpoint/resume support for long-running purge operations."""

import json
import logging
from pathlib import Path

CHECKPOINT_VERSION = 1
_logger = logging.getLogger("efspurge")


def save_checkpoint(
    filepath: Path,
    root_path: str,
    pending_dirs: list[str],
    stats: dict,
    config: dict,
) -> None:
    """
    Save a checkpoint to disk.

    Args:
        filepath: Path to write checkpoint JSON
        root_path: Root path being purged
        pending_dirs: List of directory paths still to scan (Phase 2)
        stats: Partial stats (files_scanned, dirs_scanned, etc.)
        config: Key config for validation on resume
    """
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": root_path,
        "phase": "phase2",
        "pending_dirs": pending_dirs,
        "stats": stats,
        "config": config,
    }
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def load_checkpoint(filepath: Path) -> dict | None:
    """
    Load a checkpoint from disk.

    Returns:
        Checkpoint dict with keys: root_path, pending_dirs, stats, config; or None if invalid/missing
    """
    try:
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
