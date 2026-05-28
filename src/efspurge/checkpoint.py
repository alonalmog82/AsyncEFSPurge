"""Checkpoint/resume support for long-running purge operations."""

import gzip
import io
import json
import logging
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

CHECKPOINT_VERSION = 1
_logger = logging.getLogger("efspurge")


def empty_dirs_sidecar_path(checkpoint_path: Path) -> Path:
    """Return the sidecar path used to persist Phase 3 empty-dir candidates.

    The sidecar lives next to the main checkpoint as
    ``<checkpoint>.empty_dirs.gz`` and stores one path per line (gzip+JSONL).
    Keeping these out of the main checkpoint avoids re-materialising a
    multi-million-entry list into Python objects on every resume — the main
    cause of the back-pressure death-spiral observed on prod where the
    resume baseline sat at ~85% memory before workers ever started.
    """
    return Path(str(checkpoint_path) + ".empty_dirs.gz")


def save_checkpoint(
    filepath: Path,
    root_path: str,
    pending_dirs: list[str],
    stats: dict,
    config: dict,
    empty_dirs: Iterable[str] | None = None,
) -> None:
    """
    Save a checkpoint to disk.

    Args:
        filepath: Path to write checkpoint JSON
        root_path: Root path being purged
        pending_dirs: List of directory paths still to scan (Phase 2)
        stats: Partial stats (files_scanned, dirs_scanned, etc.)
        config: Key config for validation on resume
        empty_dirs: Directories found empty during this run's Phase 2 scan
            (for Phase 3 cleanup). When provided, they are appended to the
            sidecar file ``<filepath>.empty_dirs.gz`` rather than embedded in
            the main checkpoint JSON. This keeps the resume baseline low: the
            main checkpoint contains only the BFS frontier and stats, while
            empty dirs (which can grow to millions across restarts) live on
            disk and are streamed back only at the start of Phase 3.
    """
    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": root_path,
        "phase": "phase2",
        "pending_dirs": pending_dirs,
        "stats": stats,
        "config": config,
        # empty_dirs intentionally omitted from the main checkpoint; see
        # append_empty_dirs_sidecar() below.  Older checkpoints that still
        # carry an embedded empty_dirs list remain readable — load_checkpoint
        # exposes that legacy field so the purger can migrate it to the
        # sidecar on first save.
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
                # Stream JSON directly into gzip rather than building the full
                # string first.  json.dumps(data).encode() for 7M+ pending dirs
                # allocates ~1 GB of temporary objects — fatal at 85% cgroup
                # memory usage.  json.dump() writes incrementally to the wrapper,
                # keeping peak memory near zero for the serialisation step.
                wrapper = io.TextIOWrapper(gz, encoding="utf-8")
                json.dump(data, wrapper)
                wrapper.flush()
                wrapper.detach()  # prevent TextIOWrapper.__exit__ from closing gz
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Append any new empty-dir candidates to the sidecar after the main
    # checkpoint is safely on disk.  We append (rather than rewrite) so the
    # in-memory set fed in here only needs to be this run's incremental
    # findings — prior runs' findings stay on disk and are never re-loaded
    # into the Phase 2 process, which is what keeps the resume baseline low.
    if empty_dirs:
        append_empty_dirs_sidecar(filepath, empty_dirs)


def append_empty_dirs_sidecar(checkpoint_path: Path, paths: Iterable[str]) -> int:
    """Append empty-dir candidate paths to the sidecar, one per line.

    Uses gzip append mode: concatenated gzip streams are valid gzip and
    ``gzip.open(..., "rt")`` reads through all of them transparently.  No
    temp file / rename is needed because a partial trailing entry is
    discarded by the JSONL reader on the next load (we round-trip strings,
    so a truncated line just fails json.loads and is skipped).

    Returns the number of entries written.
    """
    sidecar = empty_dirs_sidecar_path(checkpoint_path)
    count = 0
    # Buffer writes into the gzip stream; level 1 to match save_checkpoint().
    with gzip.open(sidecar, "ab", compresslevel=1) as gz:
        for p in paths:
            line = json.dumps(p) + "\n"
            gz.write(line.encode("utf-8"))
            count += 1
    return count


def stream_empty_dirs_sidecar(checkpoint_path: Path) -> Iterator[str]:
    """Yield empty-dir candidate paths from the sidecar lazily.

    Returns an empty iterator if the sidecar does not exist.  Skips
    malformed lines (e.g. a partial trailing line from a crash mid-write).
    """
    sidecar = empty_dirs_sidecar_path(checkpoint_path)
    if not sidecar.exists():
        return
    with gzip.open(sidecar, "rt", encoding="utf-8") as gz:
        for line in gz:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Partial trailing line from an aborted append — safe to skip.
                continue


def remove_empty_dirs_sidecar(checkpoint_path: Path) -> None:
    """Delete the sidecar file if present.  Best-effort; ignores OSError."""
    sidecar = empty_dirs_sidecar_path(checkpoint_path)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _logger.warning("Could not remove empty-dirs sidecar %s: %s", sidecar, e)


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
