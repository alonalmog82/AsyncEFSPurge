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


def pending_dirs_sidecar_path(checkpoint_path: Path) -> Path:
    """Return the sidecar path used to persist the Phase 2 BFS frontier.

    The sidecar lives next to the main checkpoint as
    ``<checkpoint>.pending_dirs.gz`` and stores one path per line
    (gzip+JSONL). Keeping the frontier out of the main checkpoint JSON
    avoids the second wave of the death-spiral observed on prod after the
    empty_dirs fix shipped: once the frontier grew past ~30 M paths, simply
    loading ``pending_dirs`` from the JSON into a ``list[str]`` consumed
    several GB before workers ever started.  The fix is to stream the
    frontier from disk on resume (a feeder task fills the bounded queue
    line-by-line) and to write the frontier to disk on checkpoint exit
    using the same line-by-line stream, never materialising the full list
    in Python.
    """
    return Path(str(checkpoint_path) + ".pending_dirs.gz")


def write_pending_dirs_sidecar(checkpoint_path: Path, paths: Iterable[str]) -> int:
    """Atomically (re)write the pending-dirs sidecar from a path iterator.

    Unlike :func:`append_empty_dirs_sidecar`, this replaces the sidecar
    contents — Phase 2 always writes a *full* new frontier on checkpoint
    exit (in-flight + unread-tail of the prior sidecar), so an append-only
    file would accumulate stale entries across runs.

    Writes to a sibling tempfile then ``os.replace`` so a partial write
    never corrupts an existing sidecar.  Returns the number of entries
    written so callers can record ``pending_dirs_count`` in the main
    checkpoint JSON without iterating ``paths`` twice.
    """
    sidecar = pending_dirs_sidecar_path(checkpoint_path)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=sidecar.parent, suffix=".pending.tmp")
    count = 0
    try:
        with os.fdopen(tmp_fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1) as gz:
                for p in paths:
                    gz.write((json.dumps(p) + "\n").encode("utf-8"))
                    count += 1
        os.replace(tmp_path, sidecar)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return count


def stream_pending_dirs_sidecar(checkpoint_path: Path) -> Iterator[str]:
    """Yield pending-dirs frontier paths from the sidecar lazily.

    Returns an empty iterator if the sidecar does not exist.  Skips
    malformed lines (e.g. a partial trailing line from a crash mid-write).
    """
    sidecar = pending_dirs_sidecar_path(checkpoint_path)
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
                continue


def remove_pending_dirs_sidecar(checkpoint_path: Path) -> None:
    """Delete the pending-dirs sidecar if present.  Best-effort."""
    sidecar = pending_dirs_sidecar_path(checkpoint_path)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _logger.warning("Could not remove pending-dirs sidecar %s: %s", sidecar, e)


def save_checkpoint(
    filepath: Path,
    root_path: str,
    pending_dirs: Iterable[str],
    stats: dict,
    config: dict,
    empty_dirs: Iterable[str] | None = None,
) -> int:
    """Save a Phase 2 checkpoint to disk, spilling the frontier to a sidecar.

    Args:
        filepath: Path to write checkpoint JSON
        root_path: Root path being purged
        pending_dirs: BFS frontier (directories still to scan).  Streamed
            line-by-line into the ``<filepath>.pending_dirs.gz`` sidecar
            via :func:`write_pending_dirs_sidecar` so we never materialise
            the full list as a Python collection.  At ~30 M entries the
            list alone was driving the resume baseline above the 85%
            back-pressure threshold and re-triggering the death-spiral
            after the empty_dirs sidecar fix shipped.
        stats: Partial stats (files_scanned, dirs_scanned, etc.)
        config: Key config for validation on resume
        empty_dirs: Directories found empty during this run's Phase 2 scan
            (for Phase 3 cleanup). When provided, they are appended to the
            sidecar file ``<filepath>.empty_dirs.gz`` rather than embedded
            in the main checkpoint JSON.

    Returns:
        Number of pending_dirs entries written to the sidecar.  Stored in
        the main checkpoint JSON as ``pending_dirs_count`` and surfaced to
        callers for progress logging.
    """
    filepath = Path(filepath)
    # Write the frontier sidecar first so a partial write can't leave the
    # main JSON pointing at a half-written/missing frontier.  os.replace is
    # atomic on POSIX; on failure the previous sidecar is preserved.
    pending_count = write_pending_dirs_sidecar(filepath, pending_dirs)
    # An empty frontier means "done" — drop the sidecar to keep the
    # on-disk state coherent with the main JSON's pending_dirs_count=0.
    if pending_count == 0:
        remove_pending_dirs_sidecar(filepath)

    data = {
        "version": CHECKPOINT_VERSION,
        "root_path": root_path,
        "phase": "phase2",
        "pending_dirs_count": pending_count,
        "stats": stats,
        "config": config,
        # pending_dirs is intentionally omitted from the main checkpoint;
        # see write_pending_dirs_sidecar() above.  Older checkpoints with
        # an embedded list remain readable — load_checkpoint surfaces the
        # legacy field so the purger can migrate it to the sidecar on its
        # next resume.  empty_dirs also lives in a sidecar (see
        # append_empty_dirs_sidecar) for the same baseline reasons.
    }
    # Atomic write: temp file on the same filesystem, then rename.  Gzip
    # level 1 keeps NFS write time low (~6 s vs 50+ s on a saturated mount).
    tmp_fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1) as gz:
                wrapper = io.TextIOWrapper(gz, encoding="utf-8")
                json.dump(data, wrapper)
                wrapper.flush()
                wrapper.detach()
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Append new empty-dir candidates to the sidecar after the main checkpoint
    # is safely on disk.  Appending (vs rewriting) means the in-memory
    # ``empty_dirs`` set only carries this run's incremental findings.
    if empty_dirs:
        append_empty_dirs_sidecar(filepath, empty_dirs)

    return pending_count


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
    """Load a Phase 2 checkpoint from disk.

    Supports three on-disk formats:
      1. New (sidecar): main JSON has ``pending_dirs_count`` only; the
         frontier lives in ``<filepath>.pending_dirs.gz``.
      2. Legacy embedded: main JSON has ``pending_dirs`` as an inline list.
      3. Legacy plain-JSON (pre-gzip) embedded.

    For (2) and (3) the caller is expected to migrate the embedded list to
    the sidecar on its first save and then never re-read it.

    Returns:
        Checkpoint dict with keys: root_path, stats, config, plus either
        ``pending_dirs_count`` (new) or ``pending_dirs`` (legacy).  Returns
        None when there is verifiably nothing left to do (no embedded
        pending_dirs *and* no sidecar entries).
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

    # Decide whether there is any work left.  With the sidecar format the
    # main JSON carries only a count; "no embedded pending and count==0"
    # is the unambiguous done signal (an empty sidecar can hang around as
    # debris if a previous run wrote it with zero entries).
    embedded_pending = data.get("pending_dirs") or []
    sidecar_count = data.get("pending_dirs_count", 0) or 0
    if not embedded_pending and sidecar_count == 0:
        _logger.info("Checkpoint has no pending directories, treating as complete")
        return None

    return data
