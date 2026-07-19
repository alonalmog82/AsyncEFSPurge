"""Async file purger optimized for AWS EFS and network storage."""

import asyncio
import errno
import gc
import logging
import os
import queue
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiofiles.os

from . import __version__
from .checkpoint import (
    append_empty_dirs_sidecar,
    empty_dirs_sidecar_path,
    load_checkpoint,
    load_phase1a_checkpoint,
    load_phase1b_checkpoint,
    pending_dirs_sidecar_path,
    remove_empty_dirs_sidecar,
    remove_pending_dirs_sidecar,
    save_checkpoint,
    save_phase1a_checkpoint,
    save_phase1b_checkpoint,
    stream_empty_dirs_sidecar,
    stream_pending_dirs_sidecar,
    write_pending_dirs_sidecar,
)
from .logging import log_with_context, setup_logging

# Maximum number of directories to discover in Phase 1a.
# This caps the memory footprint of dirs_by_depth.  Each Path object
# is ~400-600 bytes, so 1M dirs ≈ 400-600 MB.  Setting this to 1M
# keeps discovery memory well within a 4.5 GB container budget while
# still covering the vast majority of real-world directory trees.
MAX_DISCOVERY_DIRS_DEFAULT = 1_000_000


class CheckpointExit(Exception):
    """Raised when purge exits after saving a checkpoint (memory critical)."""


def get_memory_usage_mb() -> float:
    """
    Get current memory usage in MB.

    In container environments (K8s), this tries to read from cgroup memory stats
    which reflects the container's actual memory usage, not the host's RSS.
    """
    # First, try reading container memory usage from cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            bytes_used = int(f.read().strip())
            return bytes_used / 1024 / 1024  # Convert bytes to MB
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    # Try cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
            bytes_used = int(f.read().strip())
            return bytes_used / 1024 / 1024  # Convert bytes to MB
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    # Fall back to psutil for non-container environments
    try:
        import psutil

        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024  # Convert bytes to MB
    except ImportError:
        pass

    # Last resort: use resource module (less accurate)
    try:
        import resource

        # On Linux, ru_maxrss is in kilobytes
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB to MB
    except Exception:
        return 0.0  # Return 0 if we can't measure


# Backoffs between rmdir retries when the previous attempt returned EACCES.
# Total attempts = 1 (initial) + len(EACCES_RETRY_BACKOFFS) retries.
# See issue #53: EFS/NFS can transiently return EACCES on rmdir of dirs the
# caller has full permission on; a short retry catches the transient case
# without spamming logs or aborting Phase 3.
EACCES_RETRY_BACKOFFS = (0.1, 0.5, 2.0)


async def async_rmdir_with_eacces_retry(directory: Path) -> int:
    """rmdir with bounded retry on EACCES.

    EFS/NFS can transiently return EACCES on rmdir even when the caller has
    permission (see issue #53). Retry with backoff before giving up. Non-EACCES
    OSErrors are re-raised on the first occurrence — no retry.

    Returns the total number of attempts made (1 == first-try success).
    Raises the last OSError if every attempt fails.
    """
    last_err: OSError | None = None
    # Backoffs list is prepended with 0 so the initial attempt has no sleep.
    for attempt, backoff in enumerate((0.0, *EACCES_RETRY_BACKOFFS), start=1):
        if backoff > 0:
            await asyncio.sleep(backoff)
        try:
            await aiofiles.os.rmdir(directory)
            return attempt
        except OSError as e:
            if e.errno != errno.EACCES:
                raise
            last_err = e
    assert last_err is not None  # loop always sets last_err before falling through
    raise last_err


async def async_scandir(path: Path, executor: ThreadPoolExecutor | None = None, purger_instance=None):
    """
    Async wrapper for os.scandir.

    Args:
        path: Directory path to scan
        executor: Optional ThreadPoolExecutor to use. If None, uses default executor (~32 threads).
                  Use a custom executor with more threads to increase directory scanning throughput.
        purger_instance: Optional AsyncEFSPurger instance for diagnostics (DEBUG level only)

    Returns:
        List of directory entries
    """
    loop = asyncio.get_running_loop()
    start_time = time.time() if purger_instance else None

    def _scandir():
        with os.scandir(path) as entries:
            return list(entries)

    result = await loop.run_in_executor(executor, _scandir)

    # Track diagnostics (DEBUG level only)
    if purger_instance and purger_instance.logger.isEnabledFor(logging.DEBUG):
        elapsed = time.time() - start_time
        async with purger_instance.scandir_lock:
            purger_instance.scandir_call_count += 1
            purger_instance.scandir_total_time += elapsed

            # Log diagnostics periodically
            current_time = time.time()
            if (
                current_time - purger_instance.last_scandir_diagnostics_log
                >= purger_instance.scandir_diagnostics_interval
            ):
                purger_instance.last_scandir_diagnostics_log = current_time
                await _log_scandir_diagnostics(purger_instance, executor, current_time)

    return result


async def async_is_dir_empty(path: Path, executor: ThreadPoolExecutor | None = None) -> bool:
    """
    Efficiently check if a directory is empty without materializing all entries.

    Unlike async_scandir which returns list(os.scandir(path)) - materializing every
    entry into memory - this function uses next() to check for just the first entry.
    For directories with thousands of files (common on EFS), this avoids creating
    a list of thousands of DirEntry objects just to determine the dir is non-empty.

    Returns:
        True if the directory is empty, False otherwise.

    Raises:
        FileNotFoundError, PermissionError, OSError on I/O errors.
    """
    loop = asyncio.get_running_loop()

    def _is_empty():
        with os.scandir(path) as it:
            return next(it, None) is None

    return await loop.run_in_executor(executor, _is_empty)


async def async_scandir_batched(
    path: Path,
    executor: ThreadPoolExecutor | None = None,
    batch_size: int = 5000,
):
    """
    Async wrapper for os.scandir that yields results in batches.

    Unlike async_scandir which materializes all entries at once (problematic for
    directories with 100K+ entries on slow filesystems like EFS), this function
    yields entries in fixed-size batches. This allows the caller to check memory
    pressure between batches and abort early if needed.

    Args:
        path: Directory path to scan
        executor: Optional ThreadPoolExecutor to use
        batch_size: Number of entries per batch (default 5000)

    Yields:
        Lists of DirEntry objects, each list up to batch_size entries
    """
    loop = asyncio.get_running_loop()

    # Use a queue to pass batches from the thread to the async caller.
    # The thread reads os.scandir() in chunks and puts them on the queue;
    # the async generator pulls batches off the queue with memory checks in between.
    result_queue: queue.Queue = queue.Queue(maxsize=2)  # Small buffer to limit memory
    _SENTINEL = object()

    def _scandir_batched():
        try:
            with os.scandir(path) as it:
                batch = []
                for entry in it:
                    batch.append(entry)
                    if len(batch) >= batch_size:
                        result_queue.put(batch)
                        batch = []
                if batch:
                    result_queue.put(batch)
        except Exception as exc:
            result_queue.put(exc)
        finally:
            result_queue.put(_SENTINEL)

    # Start the scandir thread
    future = loop.run_in_executor(executor, _scandir_batched)

    # Yield batches as they arrive
    while True:
        # Poll the queue without blocking the event loop
        while True:
            try:
                item = result_queue.get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.01)  # Yield to event loop briefly

        if item is _SENTINEL:
            break
        if isinstance(item, Exception):
            raise item
        yield item

    # Ensure the thread has completed (it should have by the time we get SENTINEL,
    # but await it to propagate any unexpected thread exceptions).
    if not future.done():
        await future


def _drain_pending_to_queue(queue: asyncio.Queue, pending_list: list) -> None:
    """Put as many items from pending_list onto queue as fit; keep rest in list.

    Mutates pending_list: removes items that were successfully put. Call when
    queue has maxsize to avoid deadlock (workers never block on put).
    """
    kept = []
    for item in pending_list:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            kept.append(item)
    pending_list.clear()
    pending_list.extend(kept)


async def _log_scandir_diagnostics(purger_instance, executor, current_time=None):
    """Helper function to log scandir executor diagnostics (DEBUG level only)."""
    if not purger_instance.logger.isEnabledFor(logging.DEBUG):
        return

    if current_time is None:
        current_time = time.time()

    # Calculate metrics
    avg_time = (
        purger_instance.scandir_total_time / purger_instance.scandir_call_count
        if purger_instance.scandir_call_count > 0
        else 0
    )
    calls_per_sec = (
        purger_instance.scandir_call_count / (current_time - (purger_instance.stats.get("start_time", current_time)))
        if purger_instance.scandir_call_count > 0
        else 0
    )

    # Estimate executor thread utilization (approximate)
    # ThreadPoolExecutor doesn't expose queue size or active thread count directly
    executor_active_threads = 0
    executor_total_threads = executor._max_workers if executor else 0
    if executor is not None:
        try:
            # Try to get active thread count (this is approximate)
            # We check if threads are alive and have a thread ID (meaning they're running)
            if hasattr(executor, "_threads"):
                executor_active_threads = sum(1 for t in executor._threads if t.is_alive() and t.ident is not None)
        except Exception:
            pass

    log_with_context(
        purger_instance.logger,
        "debug",
        "scandir executor diagnostics",
        {
            "total_calls": purger_instance.scandir_call_count,
            "avg_time_ms": round(avg_time * 1000, 2),
            "calls_per_sec": round(calls_per_sec, 1),
            "executor_threads_total": executor_total_threads,
            "executor_threads_active_estimate": executor_active_threads,
            "executor_threads_idle_estimate": max(0, executor_total_threads - executor_active_threads),
            "utilization_percent": round(
                (executor_active_threads / executor_total_threads * 100) if executor_total_threads > 0 else 0,
                1,
            ),
            "dirs_per_thread_per_sec": round(
                calls_per_sec / executor_total_threads if executor_total_threads > 0 else 0,
                2,
            ),
        },
    )


class RateTracker:
    """
    Track rates for different phases and time windows.

    Supports:
    - Per-phase rate tracking (scanning, deletion, removing_empty_dirs)
    - Time-windowed rates (instant 10s, short-term 60s, overall)
    - Peak rate tracking
    """

    def __init__(self):
        """Initialize the rate tracker."""
        # Store samples as (timestamp, phase, metric_type, count)
        # Using deque for efficient append/popleft operations
        self.samples: deque[tuple[float, str, str, int]] = deque(maxlen=10000)

        # Track peak rates
        self.peak_rates = {
            "files_per_second": {"value": 0.0, "timestamp": None},
            "dirs_per_second": {"value": 0.0, "timestamp": None},
            "files_deleted_per_second": {"value": 0.0, "timestamp": None},
            "empty_dirs_per_second": {"value": 0.0, "timestamp": None},
        }

        # Track phase start times for per-phase rate calculation
        self.phase_start_times = {
            "scanning": None,
            "deletion": None,
            "removing_empty_dirs": None,
        }

        # Track phase-specific counters
        self.phase_counts = {
            "scanning": {"files": 0, "dirs": 0},
            "deletion": {"files": 0},
            "removing_empty_dirs": {"dirs": 0},
        }

    def record(self, phase: str, metric_type: str, count: int = 1) -> None:
        """
        Record a metric sample.

        Args:
            phase: Current phase ("scanning", "deletion", "removing_empty_dirs")
            metric_type: Type of metric ("files", "dirs")
            count: Count to record (default: 1)
        """
        timestamp = time.time()
        self.samples.append((timestamp, phase, metric_type, count))

        # Update phase counts
        if phase in self.phase_counts:
            if metric_type in self.phase_counts[phase]:
                self.phase_counts[phase][metric_type] += count

    def get_rate(self, phase: str, metric_type: str, window_seconds: float) -> float:
        """
        Calculate rate for a specific phase/metric over time window.

        Args:
            phase: Phase to filter by
            metric_type: Metric type to filter by ("files", "dirs")
            window_seconds: Time window in seconds

        Returns:
            Rate (count per second) over the specified window
        """
        if window_seconds <= 0:
            return 0.0

        cutoff = time.time() - window_seconds

        # Filter samples within window, matching phase and metric_type
        relevant = [s for s in self.samples if s[0] > cutoff and s[1] == phase and s[2] == metric_type]

        if not relevant:
            return 0.0

        total = sum(s[3] for s in relevant)
        time_span = relevant[-1][0] - relevant[0][0] if len(relevant) > 1 else 1.0

        return total / time_span if time_span > 0 else 0.0

    def get_phase_rate(self, phase: str, metric_type: str) -> float:
        """
        Calculate rate for a phase since phase started.

        Args:
            phase: Phase name
            metric_type: Metric type ("files", "dirs")

        Returns:
            Rate since phase started, or 0 if phase hasn't started
        """
        if phase not in self.phase_start_times or self.phase_start_times[phase] is None:
            return 0.0

        elapsed = time.time() - self.phase_start_times[phase]
        if elapsed <= 0:
            return 0.0

        if phase not in self.phase_counts:
            return 0.0

        count = self.phase_counts[phase].get(metric_type, 0)
        return count / elapsed

    def set_phase_start(self, phase: str) -> None:
        """
        Mark the start of a phase.

        Args:
            phase: Phase name
        """
        self.phase_start_times[phase] = time.time()
        # Reset phase counts when phase starts
        if phase in self.phase_counts:
            self.phase_counts[phase] = {k: 0 for k in self.phase_counts[phase]}

    def update_peak_rate(self, metric_name: str, rate: float) -> None:
        """
        Update peak rate if current rate exceeds previous peak.

        Args:
            metric_name: Name of the metric ("files_per_second", etc.)
            rate: Current rate value
        """
        if metric_name in self.peak_rates:
            if rate > self.peak_rates[metric_name]["value"]:
                self.peak_rates[metric_name] = {
                    "value": rate,
                    "timestamp": time.time(),
                }


class AsyncEFSPurger:
    """
    High-performance async file purger for network file systems.

    Optimized for AWS EFS with:
    - Async I/O for overlapping network latency
    - Controlled concurrency to avoid overwhelming the file system
    - Safe symlink handling
    - Comprehensive error handling and statistics
    """

    def __init__(
        self,
        root_path: str,
        max_age_days: float,
        max_concurrency: int | None = None,
        max_concurrency_scanning: int | None = None,
        max_concurrency_deletion: int | None = None,
        dry_run: bool = True,
        log_level: str = "INFO",
        memory_limit_mb: int = 800,
        task_batch_size: int = 5000,
        remove_empty_dirs: bool = False,
        max_empty_dirs_to_delete: int = 500,
        max_discovery_dirs: int = 0,
        max_concurrent_discovery: int = 20,
        queue_maxsize: int = 10000,
        max_entries_per_dir: int = 0,
        checkpoint_file: str | Path | None = None,
        resume: bool = False,
        dir_deletion_checkpoint_file: str | Path | None = None,
        dir_deletion_resume: bool = False,
        phase1_only: bool = False,
        phase3_only: bool = False,
        phase3_batch_size: int = 0,
        phase3_deletion_workers: int = 0,
        backpressure_checkpoint_timeout: int = 600,
        stuck_worker_cancel_timeout: int = 30,
    ):
        """
        Initialize the async EFS purger.

        Args:
            root_path: Root directory to scan
            max_age_days: Files older than this (in days) will be purged
            max_concurrency: Maximum concurrent async operations (deprecated, use max_concurrency_scanning/deletion)
            max_concurrency_scanning: Maximum concurrent file scanning (stat) operations (default: 1000)
            max_concurrency_deletion: Maximum concurrent file deletion (remove) operations (default: 1000)
            dry_run: If True, only report what would be deleted
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            memory_limit_mb: Soft memory limit in MB (triggers back-pressure, 0 = disabled)
            task_batch_size: Maximum tasks to create at once (prevents OOM)
            remove_empty_dirs: If True, remove empty directories after scanning (post-order)
            max_empty_dirs_to_delete: Maximum empty directories to delete per run (0 = unlimited, default: 500)
            max_discovery_dirs: Maximum directories to discover in Phase 1a (0 = use automatic limit based on memory)
            max_concurrent_discovery: Maximum concurrent directory/file scan workers (default: 20)
            queue_maxsize: Maximum size of Phase 1a and Phase 2 directory queues (0 = unbounded, default: 10000).
                Bounds memory when discovery outpaces processing; producers block when full.
            max_entries_per_dir: Cap entries processed per directory in Phase 1a (0 = no limit). When set (e.g. 50000),
                a directory is re-queued after this many entries and scanned again later to avoid one huge dir
                stalling workers.
            checkpoint_file: Path to save checkpoint when memory is critical (enables auto-checkpoint on OOM risk).
            resume: If True, load checkpoint from checkpoint_file and resume Phase 2 from saved state.
            dir_deletion_checkpoint_file: Path to save/load Phase 1a BFS frontier checkpoint on memory abort.
                On abort: saves remaining dirs to scan, runs Phase 1b on dirs found so far, exits 75.
            dir_deletion_resume: If True, resume Phase 1a discovery from dir_deletion_checkpoint_file.
            backpressure_checkpoint_timeout: Seconds of sustained back-pressure before forcing a checkpoint exit
                (default: 600). Prevents the job from stalling indefinitely when memory stabilises between the
                back-pressure threshold (85%) and the critical checkpoint threshold (95%). Set to 0 to disable.
            stuck_worker_cancel_timeout: Seconds to wait for cooperative worker exit after checkpoint is requested
                before force-cancelling workers that are stuck in EFS syscalls (default: 30). Stuck workers'
                in-flight directories are rescued into the checkpoint for retry on the next resume.

        Raises:
            ValueError: If invalid parameters are provided
        """
        # Input validation
        if max_age_days < 0:
            raise ValueError(f"max_age_days must be >= 0, got {max_age_days}")

        # Handle concurrency parameters: backward compatibility with max_concurrency
        # If max_concurrency is provided, use it for both scanning and deletion
        # Otherwise, use individual parameters with defaults
        if max_concurrency is not None:
            if max_concurrency < 1:
                raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
            # Deprecation warning
            import warnings

            warnings.warn(
                "max_concurrency is deprecated. Use max_concurrency_scanning and max_concurrency_deletion instead. "
                f"Setting both to {max_concurrency} for backward compatibility.",
                DeprecationWarning,
                stacklevel=2,
            )
            # Use max_concurrency for both if individual params not specified
            if max_concurrency_scanning is None:
                max_concurrency_scanning = max_concurrency
            if max_concurrency_deletion is None:
                max_concurrency_deletion = max_concurrency
        else:
            # Default to 1000 if neither max_concurrency nor individual params are provided
            if max_concurrency_scanning is None:
                max_concurrency_scanning = 1000
            if max_concurrency_deletion is None:
                max_concurrency_deletion = 1000

        # Validate individual concurrency parameters
        if max_concurrency_scanning < 1:
            raise ValueError(f"max_concurrency_scanning must be >= 1, got {max_concurrency_scanning}")
        if max_concurrency_deletion < 1:
            raise ValueError(f"max_concurrency_deletion must be >= 1, got {max_concurrency_deletion}")

        if task_batch_size < 1:
            raise ValueError(f"task_batch_size must be >= 1, got {task_batch_size}")

        if memory_limit_mb < 0:
            raise ValueError(f"memory_limit_mb must be >= 0, got {memory_limit_mb}")

        if max_empty_dirs_to_delete < 0:
            raise ValueError(f"max_empty_dirs_to_delete must be >= 0, got {max_empty_dirs_to_delete}")

        if queue_maxsize < 0:
            raise ValueError(f"queue_maxsize must be >= 0, got {queue_maxsize}")
        if max_entries_per_dir < 0:
            raise ValueError(f"max_entries_per_dir must be >= 0, got {max_entries_per_dir}")

        # Ensure root_path is absolute
        root_path_obj = Path(root_path)
        if not root_path_obj.is_absolute():
            root_path_obj = root_path_obj.resolve()

        # Block dangerous system directories that should never be purged
        # These contain special files (device nodes, virtual filesystems) that would cause errors
        # and potential system instability if deleted
        dangerous_paths = {
            "/proc",
            "/sys",
            "/dev",
            "/run",
            "/var/run",
            "/boot",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/usr/bin",
            "/usr/sbin",
            "/usr/lib",
            "/etc",
        }

        # Check if root_path is or is inside a dangerous path
        root_str = str(root_path_obj)
        for dangerous in dangerous_paths:
            if root_str == dangerous or root_str.startswith(dangerous + "/"):
                raise ValueError(
                    f"Refusing to purge system directory: {root_path_obj}. "
                    f"This path is inside '{dangerous}' which contains critical system files. "
                    f"Purging this directory could cause system instability or data loss."
                )

        self.root_path = root_path_obj
        self.max_age_days = max_age_days
        self.cutoff_time = time.time() - (max_age_days * 86400)  # Convert days to seconds
        # Store concurrency limits (for backward compatibility, max_concurrency is the max of both)
        self.max_concurrency_scanning = max_concurrency_scanning
        self.max_concurrency_deletion = max_concurrency_deletion
        self.max_concurrency = max(max_concurrency_scanning, max_concurrency_deletion)  # For backward compatibility
        self.dry_run = dry_run
        self.memory_limit_mb = memory_limit_mb
        self.task_batch_size = task_batch_size
        self.remove_empty_dirs = remove_empty_dirs
        self.max_empty_dirs_to_delete = max_empty_dirs_to_delete
        self.max_concurrent_discovery = max(1, max_concurrent_discovery)
        self.queue_maxsize = queue_maxsize
        self.max_entries_per_dir = max_entries_per_dir
        self.checkpoint_file = Path(checkpoint_file) if checkpoint_file else None
        self.resume = resume
        self.dir_deletion_checkpoint_file = Path(dir_deletion_checkpoint_file) if dir_deletion_checkpoint_file else None
        self.dir_deletion_resume = dir_deletion_resume
        self.phase1_only = phase1_only
        self.phase3_only = phase3_only
        self.phase3_batch_size = max(0, phase3_batch_size)
        self.phase3_deletion_workers = max(0, phase3_deletion_workers)
        if phase1_only and phase3_only:
            raise ValueError("--phase1-only and --phase3-only are mutually exclusive")
        if phase3_only and not remove_empty_dirs:
            raise ValueError("--phase3-only requires --remove-empty-dirs")
        # Skip flag used by iterative phase-3-only drain to prevent
        # _remove_empty_directories() from re-reading the sidecar on every batch.
        self._skip_sidecar_load = False
        self.backpressure_checkpoint_timeout = backpressure_checkpoint_timeout
        self.stuck_worker_cancel_timeout = stuck_worker_cancel_timeout

        # Compute discovery limit: if explicitly set use that, otherwise derive from memory budget.
        # Each Path object is ~400-600 bytes, so we allow roughly 60% of the memory budget for paths.
        if max_discovery_dirs > 0:
            self.max_discovery_dirs = max_discovery_dirs
        elif memory_limit_mb > 0:
            # Budget ~60% of memory for directory paths (~500 bytes each)
            self.max_discovery_dirs = int((memory_limit_mb * 0.6 * 1024 * 1024) / 500)
        else:
            self.max_discovery_dirs = MAX_DISCOVERY_DIRS_DEFAULT

        # Note: max_empty_dirs_to_delete=0 (unlimited) is safe with the standalone two-pass approach
        # Phase 1 uses bounded memory (iterative BFS + bottom-up processing with back-pressure)

        # Statistics
        self.stats = {
            "files_scanned": 0,
            "files_to_purge": 0,
            "files_purged": 0,
            "dirs_scanned": 0,
            "symlinks_skipped": 0,
            "special_files_skipped": 0,  # Sockets, FIFOs, device nodes, etc.
            "errors": 0,
            "bytes_freed": 0,
            "start_time": time.time(),
            "memory_backpressure_events": 0,
            "empty_dirs_to_delete": 0,  # Directories that would be deleted (increments in dry-run)
            "empty_dirs_deleted": 0,  # Directories actually deleted (0 in dry-run)
        }

        # Stuck detection: track progress for detecting hangs
        self.last_files_scanned = 0
        self.last_dirs_scanned = 0
        self.last_empty_dirs_deleted = 0
        self.stuck_detection_count = 0  # How many consecutive progress checks showed no change

        # Discovery state: tracked so the progress monitor can report what's happening
        # during Phase 1a instead of emitting false hang warnings.
        self._discovery_active = False
        self._discovery_current_dir: str | None = None
        self._discovery_dirs_found = 0
        self._discovery_queue_size = 0
        self._discovery_entries_scanned = 0

        # Track directories currently being scanned (for diagnostics when stuck)
        self.active_directories: set[Path] = set()
        self.active_directories_lock = asyncio.Lock()

        # Track current phase for better progress reporting
        self.current_phase = "initializing"  # "scanning", "removing_empty_dirs", "completed"

        # Track scanning phase duration for accurate overall rate calculation
        self.scanning_end_time: float | None = None

        # Rate tracking for enhanced metrics
        self.rate_tracker = RateTracker()

        # Track empty directories for post-order deletion (Phase 3: post-scan cleanup)
        # Used by _remove_empty_directories() for dirs that became empty after file purging
        self.empty_dirs: set[Path] = set()

        # Track total empty dirs processed across all phases
        self.empty_dirs_processed_total = 0

        # Concurrency control - separate semaphores for scanning and deletion
        self.scanning_semaphore = asyncio.Semaphore(max_concurrency_scanning)
        self.deletion_semaphore = asyncio.Semaphore(max_concurrency_deletion)
        self.stats_lock = asyncio.Lock()

        # Note: Incremental batch processing during scanning has been removed.
        # Empty dirs are now handled in a standalone phase (_purge_empty_directories_standalone).

        # Custom ThreadPoolExecutor for directory scanning to bypass default thread pool limit
        # Default executor has ~32 threads, limiting directory scanning throughput to ~250-300 dirs/sec
        # Custom executor allows scaling to 200-500 threads for 2-5x improvement
        # Thread count scales with worker count but is capped to avoid excessive overhead
        scandir_threads = min(200, max(100, self.max_concurrent_discovery * 5))

        self.scandir_executor = ThreadPoolExecutor(max_workers=scandir_threads, thread_name_prefix="efspurge-scandir")

        # Diagnostics for executor utilization (DEBUG level only)
        self.scandir_call_count = 0
        self.scandir_total_time = 0.0
        self.scandir_lock = asyncio.Lock()
        self.last_scandir_diagnostics_log = 0.0
        self.scandir_diagnostics_interval = 10.0  # Log every 10 seconds

        # Track active tasks for concurrency utilization metrics
        self.active_tasks = 0
        self.max_active_tasks = 0
        self.active_tasks_lock = asyncio.Lock()

        # Logging
        self.logger = setup_logging("efspurge", log_level)

        # Progress tracking
        self.last_progress_log = time.time()
        self.progress_interval = 30  # Log progress every 30 seconds

        # Memory back-pressure tracking
        self.last_memory_warning = 0  # Track last warning time
        self.memory_warning_interval = 60  # Only warn once per minute
        self.memory_check_lock = asyncio.Lock()  # Prevent concurrent checks
        self._backpressure_start_time: float | None = None  # When sustained back-pressure began

        # EACCES throttle (see issue #53): transient EACCES on Phase 2 scandir and
        # Phase 3 rmdir can fire millions of times on shards with concurrent-writer
        # activity. Log each occurrence at DEBUG and emit one WARNING summary per
        # interval that reports the cumulative count.
        self._eacces_count_since_warning: dict[str, int] = {}
        self._eacces_last_warning_time: dict[str, float] = {}
        self._eacces_warning_interval = 60.0  # once per minute per label
        self._eacces_lock = asyncio.Lock()

        # Checkpoint/resume: when memory critical, save state and exit for resume
        self._checkpoint_requested = False
        self._checkpoint_pending: list[Path] = []
        self._checkpoint_lock = asyncio.Lock()

    def close(self) -> None:
        """Shut down the scandir ThreadPoolExecutor.

        Call this when you're done with the purger to release threads.
        Also called automatically by ``async with AsyncEFSPurger(...)``.
        The ``purge()`` method calls this internally, so you only need to
        call it when using lower-level methods like
        ``_purge_empty_directories_standalone()`` or ``_scan_and_purge_files()``
        directly.
        """
        if hasattr(self, "scandir_executor") and self.scandir_executor is not None:
            self.scandir_executor.shutdown(wait=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    async def update_stats(self, **kwargs) -> None:
        """Thread-safe update of statistics."""
        async with self.stats_lock:
            for key, value in kwargs.items():
                if key in self.stats:
                    self.stats[key] += value

            # Progress logging is handled by _background_progress_reporter()
            # Removed duplicate logging here to prevent duplicate log entries

    async def check_memory_pressure(self) -> tuple[bool, float]:
        """
        Check if memory usage is high and apply back-pressure if needed.

        Uses a lock to prevent concurrent checks and rate-limits warning messages
        to avoid log spam.

        Returns:
            Tuple of (is_high: bool, memory_mb: float)
            - is_high: True if memory is above limit (caller should reduce batch sizes)
            - memory_mb: Current memory usage in MB (for proactive batch size reduction)
        """
        if self.memory_limit_mb <= 0:
            return False, 0.0  # No limit set

        # Use lock to prevent multiple concurrent checks
        async with self.memory_check_lock:
            memory_mb = get_memory_usage_mb()

            # CRITICAL FIX: Trigger back-pressure at 85% threshold to prevent OOM
            # Memory spikes during asyncio.gather() can push usage from 85% to OOM
            # before checks can detect it. Triggering at 85% provides safety margin.
            BACKPRESSURE_THRESHOLD = 0.85  # Trigger at 85% of limit
            backpressure_threshold_mb = self.memory_limit_mb * BACKPRESSURE_THRESHOLD

            if memory_mb > backpressure_threshold_mb:
                current_time = time.time()
                memory_percent = (memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0

                # Record when back-pressure started (used for sustained back-pressure detection below)
                if self._backpressure_start_time is None:
                    self._backpressure_start_time = current_time

                # At 95%+: request checkpoint and exit to avoid OOM (when checkpoint_file is set)
                CRITICAL_THRESHOLD_PERCENT = 95
                if (
                    self.checkpoint_file
                    and memory_percent >= CRITICAL_THRESHOLD_PERCENT
                    and not self._checkpoint_requested
                ):
                    self._checkpoint_requested = True
                    log_with_context(
                        self.logger,
                        "warning",
                        "Memory critical, requesting checkpoint and graceful exit for resume",
                        {
                            "memory_mb": round(memory_mb, 1),
                            "memory_percent": round(memory_percent, 1),
                            "checkpoint_file": str(self.checkpoint_file),
                        },
                    )
                    return True, memory_mb

                # Sustained back-pressure: if memory has been above the back-pressure threshold
                # for longer than backpressure_checkpoint_timeout seconds without reaching the
                # critical threshold (95%), checkpoint and exit. This prevents the job from
                # stalling indefinitely when memory stabilises between the two thresholds.
                if (
                    self.checkpoint_file
                    and self.backpressure_checkpoint_timeout > 0
                    and not self._checkpoint_requested
                    and (current_time - self._backpressure_start_time) >= self.backpressure_checkpoint_timeout
                ):
                    self._checkpoint_requested = True
                    sustained_seconds = int(current_time - self._backpressure_start_time)
                    log_with_context(
                        self.logger,
                        "warning",
                        "Sustained back-pressure timeout reached, requesting checkpoint and graceful exit for resume",
                        {
                            "memory_mb": round(memory_mb, 1),
                            "memory_percent": round(memory_percent, 1),
                            "sustained_seconds": sustained_seconds,
                            "timeout_seconds": self.backpressure_checkpoint_timeout,
                            "checkpoint_file": str(self.checkpoint_file),
                        },
                    )
                    return True, memory_mb

                # Only log warning once per interval to avoid spam
                if current_time - self.last_memory_warning >= self.memory_warning_interval:
                    self.logger.warning(
                        f"Memory usage ({memory_mb:.1f} MB, {memory_percent:.1f}%) exceeds back-pressure threshold "
                        f"({backpressure_threshold_mb:.1f} MB, {BACKPRESSURE_THRESHOLD * 100:.0f}%), "
                        f"applying back-pressure (logged once per {self.memory_warning_interval}s to avoid spam)..."
                    )
                    self.last_memory_warning = current_time

                # Track back-pressure event
                await self.update_stats(memory_backpressure_events=1)

                # Apply actual back-pressure: pause briefly and force GC
                await asyncio.sleep(0.5)  # Shorter pause, but happens under lock

                # Force garbage collection
                gc.collect()

                return True, memory_mb  # Memory is high, caller should reduce batch sizes

            else:
                # Memory has dropped below the back-pressure threshold — reset the sustained timer
                self._backpressure_start_time = None

            return False, memory_mb  # Memory is OK, but return value for proactive reduction

    async def _log_eacces_throttled(
        self,
        label: str,
        message: str,
        directory: Path,
        error: BaseException,
        attempts: int = 1,
    ) -> None:
        """Log a Phase 2/3 EACCES occurrence with per-label throttling.

        Every occurrence is logged at DEBUG. A summary WARNING with the count
        of occurrences since the last summary is emitted at most once per
        ``self._eacces_warning_interval`` per label (see issue #53).
        """
        async with self._eacces_lock:
            self._eacces_count_since_warning[label] = self._eacces_count_since_warning.get(label, 0) + 1
            now = time.time()
            last = self._eacces_last_warning_time.get(label, 0.0)
            if now - last >= self._eacces_warning_interval:
                count = self._eacces_count_since_warning[label]
                self._eacces_last_warning_time[label] = now
                self._eacces_count_since_warning[label] = 0
            else:
                count = None

        self.logger.debug(f"{message} (EACCES): {directory} attempts={attempts} error={error!s}")
        if count is not None:
            log_with_context(
                self.logger,
                "warning",
                f"{message} (EACCES, throttled)",
                {
                    "label": label,
                    "example_directory": str(directory),
                    "example_error": str(error),
                    "eacces_count_since_last_warning": count,
                    "interval_seconds": self._eacces_warning_interval,
                    "note": f"logged once per {self._eacces_warning_interval:.0f}s to avoid spam",
                },
            )

    async def process_file(self, file_path: Path) -> None:
        """
        Process a single file - check age and purge if necessary.

        Args:
            file_path: Path to the file to process
        """
        # Track active tasks for concurrency metrics
        async with self.active_tasks_lock:
            self.active_tasks += 1
            self.max_active_tasks = max(self.max_active_tasks, self.active_tasks)

        try:
            # Use scanning semaphore for stat operation
            async with self.scanning_semaphore:
                try:
                    # Get file stats asynchronously
                    stat = await aiofiles.os.stat(file_path)
                    await self.update_stats(files_scanned=1)
                    # Record sample for rate tracking
                    self.rate_tracker.record(self.current_phase, "files", 1)

                    # Check if file is old enough to purge
                    if stat.st_mtime < self.cutoff_time:
                        await self.update_stats(files_to_purge=1)

                        if not self.dry_run:
                            # Use deletion semaphore for remove operation
                            async with self.deletion_semaphore:
                                # Delete the file
                                await aiofiles.os.remove(file_path)
                                await self.update_stats(files_purged=1, bytes_freed=stat.st_size)
                                # Record deletion sample (use "deletion" phase for purged files)
                                self.rate_tracker.record("deletion", "files", 1)
                                self.logger.debug(f"Purged: {file_path}")
                        else:
                            self.logger.debug(f"Would purge: {file_path}")

                except FileNotFoundError:
                    # File was deleted by another process - not an error
                    self.logger.debug(f"File already deleted: {file_path}")
                except PermissionError as e:
                    log_with_context(
                        self.logger,
                        "warning",
                        "Permission denied",
                        {"file": str(file_path), "error": str(e)},
                    )
                    await self.update_stats(errors=1)
                except Exception as e:
                    log_with_context(
                        self.logger,
                        "error",
                        "Error processing file",
                        {"file": str(file_path), "error": str(e), "error_type": type(e).__name__},
                    )
                    await self.update_stats(errors=1)
        finally:
            # Decrement active tasks counter
            async with self.active_tasks_lock:
                self.active_tasks -= 1

    async def _drain_empty_dirs_sidecar_iterative(self, batch_size: int) -> bool:
        """Memory-bounded drain of the empty-dirs sidecar for --phase3-only.

        Streams the sidecar in batches of ``batch_size`` unique paths.  For
        each batch: pre-populates ``self.empty_dirs`` and calls the existing
        ``_remove_empty_directories`` deletion pass (with sidecar-load
        suppressed via ``_skip_sidecar_load``).  Returns True if every batch
        completed without triggering the memory-critical circuit breaker,
        False if any batch aborted early.

        Duplicate entries across batches are harmless because the deletion
        pass tolerates ENOENT — a second attempt on an already-deleted dir
        is a no-op.  So we deliberately do NOT hold a global "seen" set,
        which is the whole point of batching (memory bound = batch_size).

        Cascade discovery within a batch (bottom-up parent walk) still
        works; parents in later batches may already be gone by the time
        their batch runs, which just yields ENOENT and is fine.
        """
        if self.checkpoint_file is None:
            return True
        sidecar = empty_dirs_sidecar_path(self.checkpoint_file)
        if not sidecar.exists():
            return True

        # Ensure a clean slate: any prior _checkpoint_requested from an
        # earlier phase must not veto sidecar removal here.  Phase-3-only
        # is a standalone entry point; no other phase has run before.
        self._checkpoint_requested = False
        self._skip_sidecar_load = True
        completed_ok = True
        batches_processed = 0
        total_raw = 0
        try:
            batch: set[Path] = set()
            for path_str in stream_empty_dirs_sidecar(self.checkpoint_file):
                total_raw += 1
                batch.add(Path(path_str))
                if len(batch) >= batch_size:
                    log_with_context(
                        self.logger,
                        "info",
                        "Phase 3 standalone: draining batch",
                        {
                            "batch_index": batches_processed,
                            "batch_unique_size": len(batch),
                            "raw_lines_read_so_far": total_raw,
                        },
                    )
                    self.empty_dirs = batch
                    await self._remove_empty_directories()
                    batches_processed += 1
                    if self._checkpoint_requested:
                        completed_ok = False
                        break
                    batch = set()
            if batch and completed_ok:
                log_with_context(
                    self.logger,
                    "info",
                    "Phase 3 standalone: draining final batch",
                    {
                        "batch_index": batches_processed,
                        "batch_unique_size": len(batch),
                        "raw_lines_read_so_far": total_raw,
                    },
                )
                self.empty_dirs = batch
                await self._remove_empty_directories()
                batches_processed += 1
                if self._checkpoint_requested:
                    completed_ok = False
        finally:
            self._skip_sidecar_load = False

        log_with_context(
            self.logger,
            "info",
            "Phase 3 standalone: drain summary",
            {
                "batches_processed": batches_processed,
                "raw_lines_read": total_raw,
                "completed_ok": completed_ok,
                "batch_size": batch_size,
            },
        )
        return completed_ok

    async def _remove_empty_directories(self) -> None:
        """
        Phase 3: Remove directories that became empty after file purging.

        This runs AFTER file scanning/purging to catch directories that were
        non-empty before (had old files) but became empty after those files were purged.

        Uses post-order deletion (children before parents) with cascading parent checks.
        Concurrent processing with deletion_semaphore for high throughput.
        """
        # Merge in carry-over empty-dir candidates from prior runs.  These are
        # streamed from the sidecar file written by save_checkpoint() so they
        # never have to sit in Phase 2 memory.  Phase 3 has the queue and
        # workers torn down, so converting to Path here is safe.
        #
        # The iterative phase-3-only drain path pre-populates self.empty_dirs
        # with a single batch and sets _skip_sidecar_load so this loader
        # doesn't re-hydrate the full sidecar on every batch (which would
        # defeat the memory-bounded purpose of batching).
        if not self._skip_sidecar_load and self.checkpoint_file is not None:
            sidecar = empty_dirs_sidecar_path(self.checkpoint_file)
            if sidecar.exists():
                loaded_from_sidecar = 0
                for path_str in stream_empty_dirs_sidecar(self.checkpoint_file):
                    self.empty_dirs.add(Path(path_str))
                    loaded_from_sidecar += 1
                if loaded_from_sidecar:
                    log_with_context(
                        self.logger,
                        "info",
                        "Loaded carry-over empty-dir candidates from sidecar",
                        {
                            "sidecar_file": str(sidecar),
                            "loaded_count": loaded_from_sidecar,
                            "in_memory_count": len(self.empty_dirs),
                        },
                    )

        if not self.empty_dirs:
            return

        # Set phase for progress reporting
        self.current_phase = "removing_empty_dirs"
        self.rate_tracker.set_phase_start("removing_empty_dirs")

        # Log start of empty directory removal
        async with self.stats_lock:
            empty_dir_count = len(self.empty_dirs)
        log_with_context(
            self.logger,
            "info",
            "Starting empty directory removal",
            {"empty_dirs_found": empty_dir_count},
        )

        # Get initial set of empty directories (copy under lock)
        async with self.stats_lock:
            initial_empty_dirs = set(self.empty_dirs)

        # Normalize root path for comparison
        try:
            root_resolved = self.root_path.resolve()
        except (OSError, RuntimeError):
            root_resolved = self.root_path

        # Sort directories by depth (deepest first) for post-order deletion
        # This ensures children are deleted before parents
        sorted_dirs = sorted(initial_empty_dirs, key=lambda p: len(p.parts), reverse=True)

        # Use a lock to protect shared state during concurrent processing
        processed_dirs_lock = asyncio.Lock()
        processed_dirs = set()  # Track which dirs we've processed
        new_empty_parents_lock = asyncio.Lock()
        new_empty_parents = set()  # Track parents that become empty

        async def remove_single_directory(directory: Path) -> Path | None:
            """Remove a single empty directory and return its parent if it becomes empty."""
            # Check if already processed
            async with processed_dirs_lock:
                if directory in processed_dirs:
                    return None
                processed_dirs.add(directory)

            # Check rate limit atomically and increment if under limit (atomic check-and-increment)
            # This prevents race conditions where multiple workers pass the check before any increment
            if self.max_empty_dirs_to_delete > 0:
                async with self.stats_lock:
                    to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                    if to_delete_count >= self.max_empty_dirs_to_delete:
                        return None
                    # Atomically increment counter while holding lock to prevent race condition
                    self.stats["empty_dirs_to_delete"] = to_delete_count + 1

            try:
                # Normalize directory path for comparison
                try:
                    dir_resolved = directory.resolve()
                except (OSError, RuntimeError):
                    dir_resolved = directory

                # Never delete root directory
                if dir_resolved == root_resolved:
                    # Decrement counter if we're not processing (root protection)
                    if self.max_empty_dirs_to_delete > 0:
                        async with self.stats_lock:
                            self.stats["empty_dirs_to_delete"] = max(0, self.stats.get("empty_dirs_to_delete", 0) - 1)
                    return None

                # Perform deletion (semaphore only for actual rmdir, not for checks)
                # Skip redundant empty check - we already know directory is empty from scanning
                if not self.dry_run:
                    async with self.deletion_semaphore:
                        attempts = await async_rmdir_with_eacces_retry(directory)
                    # Counter already incremented above, just update deleted count
                    await self.update_stats(empty_dirs_deleted=1)
                    # Record sample for rate tracking
                    self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
                    if attempts > 1:
                        self.logger.debug(f"Removed empty directory after {attempts} attempts: {directory}")
                    else:
                        self.logger.debug(f"Removed empty directory: {directory}")
                else:
                    # Dry run: counter already incremented above, just log
                    self.logger.debug(f"Would remove empty directory: {directory}")

                # After deleting, check if parent is now empty (outside semaphore for better concurrency)
                parent = directory.parent
                if parent != directory:
                    try:
                        parent_resolved = parent.resolve()
                        if parent_resolved != root_resolved:
                            # Check if parent is now empty (quick check without holding semaphore)
                            parent_entries = await async_scandir(parent, self.scandir_executor, self)
                            if len(parent_entries) == 0:
                                return parent  # Parent is now empty
                    except (FileNotFoundError, PermissionError, OSError, RuntimeError):
                        pass  # Parent doesn't exist, no permission, or resolve failed

            except FileNotFoundError:
                # Directory was already deleted by another process
                # Decrement counter since we didn't actually delete it
                if self.max_empty_dirs_to_delete > 0:
                    async with self.stats_lock:
                        self.stats["empty_dirs_to_delete"] = max(0, self.stats.get("empty_dirs_to_delete", 0) - 1)
                self.logger.debug(f"Empty directory already deleted: {directory}")
            except OSError as e:
                # Directory might have been populated or permission denied
                # Decrement counter since we didn't actually delete it
                if self.max_empty_dirs_to_delete > 0:
                    async with self.stats_lock:
                        self.stats["empty_dirs_to_delete"] = max(0, self.stats.get("empty_dirs_to_delete", 0) - 1)
                if e.errno == errno.EACCES:
                    await self._log_eacces_throttled(
                        "phase3.rmdir",
                        "Could not remove empty directory",
                        directory,
                        e,
                    )
                else:
                    log_with_context(
                        self.logger,
                        "warning",
                        "Could not remove empty directory",
                        {"directory": str(directory), "error": str(e)},
                    )
                await self.update_stats(errors=1)

            return None

        # First pass: Delete all initially empty directories concurrently
        # DESIGN: Use semaphore + queue pattern to ensure memory is bounded by semaphore limit
        # - Semaphore limits concurrent I/O operations (prevents filesystem overload)
        # - Queue holds directories to process (bounded by semaphore limit + small buffer)
        # - Tasks created on-demand as semaphore slots become available
        # - Memory usage = semaphore_limit * memory_per_task (not batch_size * memory_per_task)

        # Circuit breaker: Stop processing if memory exceeds critical threshold
        CRITICAL_MEMORY_THRESHOLD = 0.95  # 95% of limit - stop processing to prevent OOM

        # Use queue to feed directories to workers
        # Queue size limited to semaphore limit + small buffer to prevent memory growth
        # This ensures memory is bounded by semaphore, not by total directories
        queue_maxsize = self.max_concurrency_deletion + 100  # Small buffer for queue
        directory_queue = asyncio.Queue(maxsize=queue_maxsize)
        results_queue = asyncio.Queue()
        stop_event = asyncio.Event()
        processed_count = 0
        exceptions_count = 0

        async def worker():
            """Worker that processes directories from queue, respecting semaphore limit."""
            nonlocal processed_count, exceptions_count
            while not stop_event.is_set():
                try:
                    # Get directory from queue with timeout to check stop_event
                    directory = await asyncio.wait_for(directory_queue.get(), timeout=1.0)

                    # Process directory (semaphore limits concurrent operations)
                    result = await remove_single_directory(directory)

                    # Put result in results queue
                    await results_queue.put(result)
                    directory_queue.task_done()
                    processed_count += 1

                except asyncio.TimeoutError:
                    # Timeout allows checking stop_event
                    continue
                except Exception as e:
                    exceptions_count += 1
                    self.logger.debug(f"Exception in worker: {e}", exc_info=e)
                    await self.update_stats(errors=1)
                    directory_queue.task_done()

        # Worker count defaults to max_concurrent_discovery (default: 20), NOT max_concurrency_deletion.
        # Each worker calls async_scandir(parent) after rmdir to check for cascading empty parents.
        # Spawning max_concurrency_deletion (e.g. 4000) workers floods the scandir executor and
        # causes multi-minute hangs. The deletion_semaphore already caps concurrent rmdir I/O.
        #
        # Operators cleaning up very large accumulated empty-dir backlogs can raise this via
        # --phase3-deletion-workers / EFSPURGE_PHASE3_DELETION_WORKERS.  When doing so, also
        # bump scandir_executor_threads in proportion (rule of thumb: at least 2x worker count)
        # to prevent the same scandir-executor-flood stall.
        num_workers = self.phase3_deletion_workers or self.max_concurrent_discovery
        workers = [asyncio.create_task(worker()) for _ in range(num_workers)]

        # Producer: Feed directories to queue in batches, checking memory/rate limits
        async def producer():
            """Producer that feeds directories to queue, respecting memory and rate limits."""
            i = 0
            while i < len(sorted_dirs):  # noqa: F821
                # Check memory pressure before adding more to queue
                memory_high, current_memory_mb = await self.check_memory_pressure()

                # Circuit breaker: Stop if memory is critical
                memory_percent = (current_memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0
                if memory_percent > CRITICAL_MEMORY_THRESHOLD * 100:
                    async with self.stats_lock:
                        deleted_count = self.stats.get("empty_dirs_deleted", 0)
                    self.logger.error(
                        f"CRITICAL: Memory usage ({memory_percent:.1f}%, {current_memory_mb:.1f} MB) exceeds "
                        f"critical threshold ({CRITICAL_MEMORY_THRESHOLD * 100:.0f}%). "
                        f"Stopping empty directory deletion to prevent OOM. "
                        f"Processed {i} directories, deleted {deleted_count} before stopping."
                    )
                    stop_event.set()
                    break

                # Check rate limit
                if self.max_empty_dirs_to_delete > 0:
                    async with self.stats_lock:
                        to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                        if to_delete_count >= self.max_empty_dirs_to_delete:
                            unprocessed_count = len(sorted_dirs) - i  # noqa: F821
                            log_with_context(
                                self.logger,
                                "info",
                                "Rate limit reached for empty directory deletion",
                                {
                                    "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                                    "empty_dirs_to_delete": to_delete_count,
                                    "unprocessed_dirs_in_batch": unprocessed_count,
                                },
                            )
                            stop_event.set()
                            break

                # Add directory to queue (will block if queue is full, preventing memory growth)
                # Queue size is bounded, so memory is controlled
                try:
                    await directory_queue.put(sorted_dirs[i])  # noqa: F821
                    i += 1
                except Exception:
                    # Queue closed or error
                    break

        # Start producer
        producer_task = asyncio.create_task(producer())

        # Collect results as they complete
        new_parents_collected = 0
        while True:
            # Check if we're done (producer finished and queue empty)
            if producer_task.done() and directory_queue.empty() and results_queue.empty():
                # Wait a bit for any remaining work
                await asyncio.sleep(0.1)
                if directory_queue.empty() and results_queue.empty():
                    break

            # Get result from queue (with timeout to check completion)
            try:
                result = await asyncio.wait_for(results_queue.get(), timeout=1.0)

                if isinstance(result, Exception):
                    exceptions_count += 1
                    self.logger.debug(f"Exception during directory deletion: {result}", exc_info=result)
                    await self.update_stats(errors=1)
                elif result is not None:  # Parent became empty
                    async with new_empty_parents_lock:
                        new_empty_parents.add(result)
                    new_parents_collected += 1

                results_queue.task_done()

            except asyncio.TimeoutError:
                # Check if producer is done and queue is empty
                if producer_task.done() and directory_queue.empty():
                    # Wait a bit more for workers to finish
                    await asyncio.sleep(0.5)
                    if results_queue.empty():
                        break
                continue

        # Wait for producer to complete
        try:
            await producer_task
        except Exception as e:
            self.logger.debug(f"Producer exception: {e}", exc_info=e)

        # Signal workers to stop
        stop_event.set()

        # Wait for workers to finish current work
        await directory_queue.join()  # Wait for all tasks to be processed

        # Cancel workers
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        # Log progress after first pass
        async with self.stats_lock:
            deleted_count = self.stats.get("empty_dirs_deleted", 0)
        log_with_context(
            self.logger,
            "info",
            "Empty directory removal progress",
            {
                "processed": processed_count,
                "total": len(sorted_dirs),
                "deleted": deleted_count,
                "exceptions": exceptions_count,
                "new_parents_found": new_parents_collected,
                "phase": "first_pass",
            },
        )

        # Free memory: clear sorted_dirs reference after first pass
        del sorted_dirs

        # Second pass: Process parents that became empty (cascading deletion)
        # Continue until no new empty parents are found
        iteration = 0
        while new_empty_parents:
            iteration += 1
            # Get next batch of parents to process
            async with new_empty_parents_lock:
                # Limit batch size to prevent memory explosion during cascading deletion
                # Process in chunks if there are too many parents
                # Increased from 2k to 5k for better performance (still prevents memory spikes)
                max_parents_per_iteration = 5000  # Process max 5k parents per iteration
                if len(new_empty_parents) > max_parents_per_iteration:
                    # Take a subset and keep the rest for next iteration
                    parents_list = sorted(new_empty_parents, key=lambda p: len(p.parts), reverse=True)
                    parents_to_process = parents_list[:max_parents_per_iteration]
                    new_empty_parents = set(parents_list[max_parents_per_iteration:])
                    del parents_list  # Free memory
                else:
                    parents_to_process = sorted(new_empty_parents, key=lambda p: len(p.parts), reverse=True)
                    new_empty_parents = set()  # Reset for next iteration

            if not parents_to_process:
                break

            # Log progress periodically
            if iteration % 10 == 0 or len(parents_to_process) > 1000:
                async with self.stats_lock:
                    to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                    deleted_count = self.stats.get("empty_dirs_deleted", 0)
                log_with_context(
                    self.logger,
                    "info",
                    "Cascading empty directory removal progress",
                    {
                        "iteration": iteration,
                        "empty_dirs_to_delete": to_delete_count,
                        "empty_dirs_deleted": deleted_count,
                        "parents_remaining": len(parents_to_process),
                    },
                )

            # Check memory pressure and rate limit before processing
            memory_high, current_memory_mb = await self.check_memory_pressure()
            memory_percent = (current_memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0

            # Circuit breaker: Stop if memory is critical
            if memory_percent > CRITICAL_MEMORY_THRESHOLD * 100:
                async with self.stats_lock:
                    deleted_count = self.stats.get("empty_dirs_deleted", 0)
                self.logger.error(
                    f"CRITICAL: Memory usage ({memory_percent:.1f}%, {current_memory_mb:.1f} MB) exceeds "
                    f"critical threshold ({CRITICAL_MEMORY_THRESHOLD * 100:.0f}%) during cascading deletion. "
                    f"Stopping to prevent OOM. Deleted {deleted_count} directories before stopping."
                )
                break  # Stop processing to prevent OOM

            # Check rate limit before processing
            if self.max_empty_dirs_to_delete > 0:
                async with self.stats_lock:
                    to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                    if to_delete_count >= self.max_empty_dirs_to_delete:
                        unprocessed_count = len(parents_to_process)
                        log_with_context(
                            self.logger,
                            "info",
                            "Rate limit reached during cascading deletion",
                            {
                                "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                                "empty_dirs_to_delete": to_delete_count,
                                "unprocessed_parents_in_batch": unprocessed_count,
                            },
                        )
                        break

            async def remove_parent_directory(parent: Path) -> Path | None:
                """Remove a single empty parent directory and return grandparent if it becomes empty."""
                # Check if already processed
                async with processed_dirs_lock:
                    if parent in processed_dirs:
                        return None
                    processed_dirs.add(parent)

                try:
                    # Normalize parent path
                    try:
                        parent_resolved = parent.resolve()
                    except (OSError, RuntimeError):
                        parent_resolved = parent

                    # Never delete root directory
                    if parent_resolved == root_resolved:
                        return None

                    # Skip redundant empty check - we know parent is empty (it's in the empty parents set)
                    # Only hold semaphore for actual deletion, not for checks
                    if not self.dry_run:
                        async with self.deletion_semaphore:
                            attempts = await async_rmdir_with_eacces_retry(parent)
                        await self.update_stats(empty_dirs_to_delete=1, empty_dirs_deleted=1)
                        # Record sample for rate tracking
                        self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
                        if attempts > 1:
                            self.logger.debug(f"Removed empty parent directory after {attempts} attempts: {parent}")
                        else:
                            self.logger.debug(f"Removed empty parent directory: {parent}")
                    else:
                        await self.update_stats(empty_dirs_to_delete=1)
                        self.logger.debug(f"Would remove empty parent directory: {parent}")

                    # Check if parent's parent is now empty (cascading) - outside semaphore for better concurrency
                    grandparent = parent.parent
                    if grandparent != parent:
                        try:
                            grandparent_resolved = grandparent.resolve()
                            if grandparent_resolved != root_resolved:
                                grandparent_entries = await async_scandir(grandparent, self.scandir_executor, self)
                                if len(grandparent_entries) == 0:
                                    return grandparent  # Grandparent is now empty
                        except (FileNotFoundError, PermissionError, OSError, RuntimeError):
                            pass

                except FileNotFoundError:
                    self.logger.debug(f"Empty parent directory already deleted: {parent}")
                except OSError as e:
                    if e.errno == errno.EACCES:
                        await self._log_eacces_throttled(
                            "phase3.rmdir_parent",
                            "Could not remove empty parent directory",
                            parent,
                            e,
                        )
                    else:
                        log_with_context(
                            self.logger,
                            "warning",
                            "Could not remove empty parent directory",
                            {"directory": str(parent), "error": str(e)},
                        )
                    await self.update_stats(errors=1)

                return None

            # Use semaphore+queue pattern for cascading deletion (same as first pass)
            # Memory bounded by semaphore limit, not batch size
            queue_maxsize = self.max_concurrency_deletion + 100
            parent_queue = asyncio.Queue(maxsize=queue_maxsize)
            results_queue = asyncio.Queue()
            stop_event = asyncio.Event()
            processed_count = 0
            exceptions_count = 0

            async def parent_worker():
                """Worker that processes parent directories from queue, respecting semaphore limit."""
                nonlocal processed_count, exceptions_count
                while not stop_event.is_set():
                    try:
                        parent = await asyncio.wait_for(parent_queue.get(), timeout=1.0)
                        result = await remove_parent_directory(parent)
                        await results_queue.put(result)
                        parent_queue.task_done()
                        processed_count += 1
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        exceptions_count += 1
                        self.logger.debug(f"Exception in parent worker: {e}", exc_info=e)
                        await self.update_stats(errors=1)
                        parent_queue.task_done()

            # Same worker count as first pass: honor --phase3-deletion-workers override
            # if set, otherwise fall back to max_concurrent_discovery.
            num_workers = self.phase3_deletion_workers or self.max_concurrent_discovery
            workers = [asyncio.create_task(parent_worker()) for _ in range(num_workers)]

            # Producer: Feed parents to queue
            async def parent_producer():
                """Producer that feeds parent directories to queue."""
                for parent in parents_to_process:
                    if stop_event.is_set():
                        break
                    try:
                        await parent_queue.put(parent)
                    except Exception:
                        break

            producer_task = asyncio.create_task(parent_producer())

            # Collect results as they complete
            new_grandparents_collected = 0
            while True:
                if producer_task.done() and parent_queue.empty() and results_queue.empty():
                    await asyncio.sleep(0.1)
                    if parent_queue.empty() and results_queue.empty():
                        break

                try:
                    result = await asyncio.wait_for(results_queue.get(), timeout=1.0)
                    if isinstance(result, Exception):
                        exceptions_count += 1
                        self.logger.debug(f"Exception during parent deletion: {result}", exc_info=result)
                        await self.update_stats(errors=1)
                    elif result is not None:  # Grandparent became empty
                        async with new_empty_parents_lock:
                            new_empty_parents.add(result)
                        new_grandparents_collected += 1
                    results_queue.task_done()
                except asyncio.TimeoutError:
                    if producer_task.done() and parent_queue.empty():
                        await asyncio.sleep(0.5)
                        if results_queue.empty():
                            break
                    continue

            # Wait for producer and workers
            try:
                await producer_task
            except Exception as e:
                self.logger.debug(f"Parent producer exception: {e}", exc_info=e)

            stop_event.set()
            await parent_queue.join()

            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            # Log progress for this iteration
            if exceptions_count > 0 or new_grandparents_collected > 0:
                async with self.stats_lock:
                    deleted_count = self.stats.get("empty_dirs_deleted", 0)
                log_with_context(
                    self.logger,
                    "info" if exceptions_count == 0 else "warning",
                    "Cascading deletion iteration progress",
                    {
                        "iteration": iteration,
                        "processed": processed_count,
                        "total_parents": len(parents_to_process),
                        "deleted": deleted_count,
                        "exceptions": exceptions_count,
                        "new_grandparents_found": new_grandparents_collected,
                    },
                )

        # Log completion
        async with self.stats_lock:
            to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
            deleted_count = self.stats.get("empty_dirs_deleted", 0)
        log_with_context(
            self.logger,
            "info",
            "Empty directory removal completed",
            {
                "total_empty_dirs_to_delete": to_delete_count,
                "total_empty_dirs_deleted": deleted_count,
                "iterations": iteration,
                "phase": "post_scan_cleanup",
            },
        )

    async def _purge_empty_directories_standalone(self) -> int:
        """
        Efficient standalone empty directory purger (Phase 1).

        This is a purpose-built bottom-up directory walker optimized for deleting
        millions of empty directories with bounded memory usage. It runs as a
        separate phase BEFORE file scanning.

        Design:
        - Phase A: Walk the tree iteratively (BFS), bucketing directories by depth.
          Memory: O(total_dirs) during discovery (just Path strings, not coroutines).
        - Phase B: Process depth levels from deepest to shallowest. For each level,
          check each directory and delete if empty, then FREE the entire level before
          moving to the next. This means deletion memory is O(widest_single_level)
          rather than O(total_dirs). Parents that become empty after their children
          are deleted in the current level are naturally caught when their level is
          processed next.
        - Real back-pressure: When memory exceeds threshold, reduce active concurrency
          dynamically and pause discovery to let deletions catch up.
        - No recursive coroutine tree - just bucketed paths processed level by level.

        Returns:
            Number of empty directories deleted
        """
        self.current_phase = "removing_empty_dirs"
        self.rate_tracker.set_phase_start("removing_empty_dirs")

        # Normalize root path for comparison
        try:
            root_resolved = self.root_path.resolve()
        except (OSError, RuntimeError):
            root_resolved = self.root_path

        log_with_context(
            self.logger,
            "info",
            "Phase 1: Starting standalone empty directory purge",
            {
                "root_path": str(self.root_path),
                "max_concurrency_deletion": self.max_concurrency_deletion,
                "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                "memory_limit_mb": self.memory_limit_mb,
                "max_discovery_dirs": self.max_discovery_dirs,
                "max_concurrent_discovery": self.max_concurrent_discovery,
                "queue_maxsize": self.queue_maxsize,
                "max_entries_per_dir": self.max_entries_per_dir,
            },
        )

        # Phase A: Discover all directories using parallel BFS, bucketed by depth.
        # Each directory is placed into a bucket keyed by its depth (number of path
        # components). This avoids a costly sort later and enables level-by-level
        # freeing during deletion.
        #
        # Multiple worker coroutines scan directories concurrently to overlap EFS/NFS
        # latency. On high-latency filesystems like EFS, sequential scanning is limited
        # to ~1 dir/sec; with N workers we can achieve ~N dirs/sec.
        dirs_by_depth: defaultdict[int, list[Path]] = defaultdict(list)
        discovery_queue: asyncio.Queue[Path] = (
            asyncio.Queue(maxsize=self.queue_maxsize) if self.queue_maxsize > 0 else asyncio.Queue()
        )
        discovery_errors = 0
        total_dirs_discovered = 0
        root_depth = len(self.root_path.parts)
        # When max_entries_per_dir is set we may re-queue a directory; discovered_dirs avoids double-counting.
        discovered_dirs: set[Path] = set()
        # pending_dirs tracks dirs enqueued but not yet fully processed.
        # When it reaches 0 all reachable directories have been scanned.
        pending_dirs = 0

        # Check for Phase 1b resume (memory aborted during bottom-up deletion).
        # Phase 1b checkpoint takes priority over Phase 1a — it means discovery
        # already completed and we just need to finish the deletion pass.
        phase1b_resume = False
        if self.dir_deletion_resume and self.dir_deletion_checkpoint_file:
            cp1b = load_phase1b_checkpoint(self.dir_deletion_checkpoint_file)
            if cp1b:
                for depth_str, paths in cp1b["dirs_by_depth"].items():
                    depth = int(depth_str)
                    dirs_by_depth[depth] = [Path(p) for p in paths]
                total_dirs_discovered = sum(len(v) for v in dirs_by_depth.values())
                phase1b_resume = True
                log_with_context(
                    self.logger,
                    "info",
                    "Phase 1b: Resuming from Phase 1b checkpoint",
                    {
                        "checkpoint_file": str(self.dir_deletion_checkpoint_file),
                        "depth_levels": len(dirs_by_depth),
                        "total_dirs_remaining": total_dirs_discovered,
                    },
                )

        # Resume from Phase 1a checkpoint if requested, otherwise start from root.
        # remaining_phase1a_pending holds checkpoint dirs that didn't fit in the queue;
        # they are fed in by a loader task running alongside the discovery workers.
        remaining_phase1a_pending: list[Path] = []
        if not phase1b_resume and self.dir_deletion_resume and self.dir_deletion_checkpoint_file:
            cp1a = load_phase1a_checkpoint(self.dir_deletion_checkpoint_file)
            if cp1a:
                resume_dirs = [Path(p) for p in cp1a["pending_dirs"]]
                loaded = 0
                for p in resume_dirs:
                    try:
                        discovery_queue.put_nowait(p)
                        loaded += 1
                    except asyncio.QueueFull:
                        break
                remaining_phase1a_pending = resume_dirs[loaded:]
                pending_dirs = len(resume_dirs)
                discovered_dirs = set(resume_dirs)  # prevent re-queuing
                # Pre-populate dirs_by_depth with checkpoint dirs.
                # On a normal run, dirs are added to dirs_by_depth when their parent is scanned.
                # On resume we skip the parent scan, so we must seed dirs_by_depth directly —
                # otherwise Phase 1b has no dirs to check/delete.
                for p in resume_dirs:
                    depth = len(p.parts) - root_depth
                    if depth > 0:
                        dirs_by_depth[depth].append(p)
                total_dirs_discovered = len(resume_dirs)
                log_with_context(
                    self.logger,
                    "info",
                    "Phase 1a: Resuming from checkpoint",
                    {
                        "checkpoint_file": str(self.dir_deletion_checkpoint_file),
                        "pending_dirs": pending_dirs,
                        "loaded_into_queue": loaded,
                        "remaining_to_load": len(remaining_phase1a_pending),
                    },
                )

        if not phase1b_resume and pending_dirs == 0:
            # Not resuming or checkpoint invalid/empty — start fresh from root
            discovery_queue.put_nowait(self.root_path)
            discovered_dirs = {self.root_path}
            pending_dirs = 1

        # Shared list for collecting unprocessed dirs when memory aborts discovery.
        # Workers append their per-worker pending lists + current dir here on abort.
        # Single-threaded asyncio: no lock needed.
        phase1a_checkpoint_pending: list[Path] = []

        log_with_context(
            self.logger,
            "info",
            "Phase 1a: Discovering directory tree structure",
            {
                "root_path": str(self.root_path),
                "memory_limit_mb": self.memory_limit_mb,
                "max_discovery_dirs": self.max_discovery_dirs,
                "max_concurrent_discovery": self.max_concurrent_discovery,
                "max_entries_per_dir": self.max_entries_per_dir,
                "initial_memory_mb": round(get_memory_usage_mb(), 1),
            },
        )

        self._discovery_active = True
        self._discovery_dirs_found = 0
        self._discovery_current_dir = str(self.root_path)
        self._discovery_queue_size = 1
        self._discovery_entries_scanned = 0

        memory_abort = False
        discovery_limit_reached = False
        discovery_done = asyncio.Event()
        # Track last milestone for periodic progress logging
        last_progress_milestone = 0

        async def _discovery_worker(worker_id: int) -> None:
            """Worker coroutine that scans directories from the queue."""
            nonlocal total_dirs_discovered, discovery_errors, memory_abort
            nonlocal discovery_limit_reached, pending_dirs, last_progress_milestone
            nonlocal dirs_by_depth, discovered_dirs
            pending_discovery: list[Path] = []  # Per-worker buffer when queue full (avoids deadlock)

            while not discovery_done.is_set():
                # Drain pending into queue so we never block on put (deadlock fix)
                _drain_pending_to_queue(discovery_queue, pending_discovery)
                if discovery_done.is_set():
                    pending_dirs -= len(pending_discovery)
                    break

                # Check termination conditions
                if memory_abort or discovery_limit_reached:
                    if memory_abort and self.dir_deletion_checkpoint_file:
                        phase1a_checkpoint_pending.extend(pending_discovery)
                    pending_dirs -= len(pending_discovery)
                    break

                # Check directory count limit
                if self.max_discovery_dirs > 0 and total_dirs_discovered >= self.max_discovery_dirs:
                    if not discovery_limit_reached:
                        discovery_limit_reached = True
                        log_with_context(
                            self.logger,
                            "info",
                            "Discovery directory count limit reached, proceeding with partial tree",
                            {
                                "max_discovery_dirs": self.max_discovery_dirs,
                                "dirs_discovered": total_dirs_discovered,
                                "dirs_remaining_in_queue": discovery_queue.qsize(),
                                "memory_mb": round(get_memory_usage_mb(), 1),
                            },
                        )
                        discovery_done.set()
                    break

                # Try to get a directory from the queue
                try:
                    current_dir = discovery_queue.get_nowait()
                except asyncio.QueueEmpty:
                    if discovery_done.is_set():
                        break
                    # Wait briefly for new work
                    await asyncio.sleep(0.05)
                    continue

                # Check memory pressure before scanning
                if self.memory_limit_mb > 0:
                    memory_mb = get_memory_usage_mb()
                    memory_percent = memory_mb / self.memory_limit_mb
                    if memory_percent > 0.90:
                        gc.collect()
                        await asyncio.sleep(0.5)
                        memory_mb = get_memory_usage_mb()
                        memory_percent = memory_mb / self.memory_limit_mb
                        if memory_percent > 0.95:
                            log_with_context(
                                self.logger,
                                "warning",
                                "Memory critical during directory discovery, proceeding with partial tree",
                                {
                                    "worker_id": worker_id,
                                    "dirs_discovered": total_dirs_discovered,
                                    "dirs_remaining_in_queue": discovery_queue.qsize(),
                                    "memory_mb": round(memory_mb, 1),
                                    "memory_percent": round(memory_percent * 100, 1),
                                },
                            )
                            memory_abort = True
                            discovery_done.set()
                            # current_dir was dequeued but not processed — save it for checkpoint
                            if self.dir_deletion_checkpoint_file:
                                phase1a_checkpoint_pending.append(current_dir)
                                phase1a_checkpoint_pending.extend(pending_discovery)
                            pending_dirs -= 1
                            if pending_dirs <= 0:
                                discovery_done.set()
                            break

                # Update progress monitoring state
                self._discovery_current_dir = str(current_dir)
                self._discovery_dirs_found = total_dirs_discovered
                self._discovery_queue_size = discovery_queue.qsize()

                try:
                    # Use batched scandir to avoid blocking the event loop for a long
                    # time when a single directory has 100K+ entries (common on EFS).
                    subdirs_added = 0
                    batches_processed = 0
                    entries_in_dir = 0
                    yielded_early = False
                    async for batch in async_scandir_batched(current_dir, self.scandir_executor):
                        if memory_abort or discovery_limit_reached:
                            break

                        batches_processed += 1
                        entries_in_dir += len(batch)
                        self._discovery_entries_scanned += len(batch)
                        for entry in batch:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    entry_path = Path(entry.path)
                                    if entry_path in discovered_dirs:
                                        continue
                                    discovered_dirs.add(entry_path)
                                    depth = len(entry_path.parts) - root_depth
                                    dirs_by_depth[depth].append(entry_path)
                                    pending_dirs += 1
                                    try:
                                        discovery_queue.put_nowait(entry_path)
                                    except asyncio.QueueFull:
                                        pending_discovery.append(entry_path)
                                    total_dirs_discovered += 1
                                    subdirs_added += 1
                            except OSError:
                                discovery_errors += 1

                        # Per-dir entry cap: re-queue this dir and process another to avoid one huge dir
                        # stalling workers
                        if self.max_entries_per_dir > 0 and entries_in_dir >= self.max_entries_per_dir:
                            try:
                                discovery_queue.put_nowait(current_dir)
                            except asyncio.QueueFull:
                                pending_discovery.append(current_dir)
                            yielded_early = True
                            break

                        # Update discovery state for progress monitor visibility
                        self._discovery_dirs_found = total_dirs_discovered
                        self._discovery_queue_size = discovery_queue.qsize()

                        # Check memory between batches (every 10 batches = ~50,000 entries)
                        if self.memory_limit_mb > 0 and batches_processed % 10 == 0:
                            memory_mb = get_memory_usage_mb()
                            memory_percent = memory_mb / self.memory_limit_mb
                            if memory_percent > 0.90:
                                gc.collect()
                                await asyncio.sleep(0.1)
                                memory_mb = get_memory_usage_mb()
                                memory_percent = memory_mb / self.memory_limit_mb
                                if memory_percent > 0.95:
                                    log_with_context(
                                        self.logger,
                                        "warning",
                                        "Memory critical during large directory scan, aborting discovery",
                                        {
                                            "worker_id": worker_id,
                                            "current_dir": str(current_dir),
                                            "entries_scanned_in_dir": entries_in_dir,
                                            "subdirs_in_this_dir": subdirs_added,
                                            "dirs_discovered": total_dirs_discovered,
                                            "dirs_remaining_in_queue": discovery_queue.qsize(),
                                            "memory_mb": round(memory_mb, 1),
                                            "memory_percent": round(memory_percent * 100, 1),
                                        },
                                    )
                                    memory_abort = True
                                    discovery_done.set()
                                    # current_dir was mid-scan — save it for checkpoint
                                    if self.dir_deletion_checkpoint_file:
                                        phase1a_checkpoint_pending.append(current_dir)
                                        phase1a_checkpoint_pending.extend(pending_discovery)
                                    break

                    if not yielded_early:
                        # Drain pending after finishing directory (deadlock fix)
                        _drain_pending_to_queue(discovery_queue, pending_discovery)

                        await self.update_stats(dirs_scanned=1)
                        self.rate_tracker.record("removing_empty_dirs", "dirs", 1)

                except PermissionError:
                    discovery_errors += 1
                    self.logger.debug(f"Permission denied during discovery: {current_dir}")
                except FileNotFoundError:
                    pass  # Directory was deleted concurrently
                except OSError as e:
                    discovery_errors += 1
                    self.logger.debug(f"Error scanning {current_dir}: {e}")

                # Mark this directory as fully processed (skip when we re-queued it for later)
                if not yielded_early:
                    pending_dirs -= 1
                    if pending_dirs <= 0:
                        discovery_done.set()

                # Log progress periodically during discovery (every 50,000 dirs)
                current_milestone = (total_dirs_discovered // 50000) * 50000
                if current_milestone > last_progress_milestone and total_dirs_discovered > 0:
                    last_progress_milestone = current_milestone
                    memory_mb = get_memory_usage_mb() if self.memory_limit_mb > 0 else 0
                    memory_percent = (memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0
                    log_with_context(
                        self.logger,
                        "info",
                        "Directory discovery progress",
                        {
                            "dirs_discovered": total_dirs_discovered,
                            "dirs_remaining_in_queue": discovery_queue.qsize(),
                            "depth_levels": len(dirs_by_depth),
                            "memory_mb": round(memory_mb, 1),
                            "memory_percent": round(memory_percent, 1),
                            "memory_limit_mb": self.memory_limit_mb,
                            "discovery_errors": discovery_errors,
                            "active_workers": self.max_concurrent_discovery,
                        },
                    )

        # Launch parallel discovery workers (plus an optional loader task for resume overflow)
        num_workers = self.max_concurrent_discovery
        # Skip Phase 1a workers entirely when resuming from Phase 1b checkpoint
        workers = [] if phase1b_resume else [asyncio.create_task(_discovery_worker(i)) for i in range(num_workers)]

        # If resuming with more pending dirs than queue_maxsize, feed them incrementally
        # using a loader task (same pattern as Phase 2 checkpoint loader).
        phase1a_loader_task: asyncio.Task | None = None
        if remaining_phase1a_pending and not phase1b_resume:

            async def _phase1a_loader() -> None:
                for p in remaining_phase1a_pending:
                    if memory_abort or discovery_done.is_set():
                        if memory_abort and self.dir_deletion_checkpoint_file:
                            phase1a_checkpoint_pending.extend(
                                remaining_phase1a_pending[remaining_phase1a_pending.index(p) :]
                            )
                        return
                    while True:
                        if memory_abort or discovery_done.is_set():
                            return
                        try:
                            discovery_queue.put_nowait(p)
                            break
                        except asyncio.QueueFull:
                            await asyncio.sleep(0.1)

            phase1a_loader_task = asyncio.create_task(_phase1a_loader())

        # Wait for all workers to complete
        await asyncio.gather(*workers, return_exceptions=True)
        if phase1a_loader_task is not None:
            phase1a_loader_task.cancel()
            await asyncio.gather(phase1a_loader_task, return_exceptions=True)

        # Discovery complete - clear state so progress monitor stops reporting discovery
        self._discovery_active = False
        self._discovery_current_dir = None

        max_depth = max(dirs_by_depth.keys()) if dirs_by_depth else 0

        if not phase1b_resume:
            log_with_context(
                self.logger,
                "info",
                "Phase 1a complete: Directory tree discovered",
                {
                    "total_dirs_discovered": total_dirs_discovered,
                    "depth_levels": len(dirs_by_depth),
                    "max_depth": max_depth,
                    "discovery_errors": discovery_errors,
                    "concurrent_workers": self.max_concurrent_discovery,
                    "memory_mb": round(get_memory_usage_mb(), 1),
                },
            )

        # Free discovery-only state before Phase 1b to reduce memory pressure
        discovered_dirs.clear()

        if not dirs_by_depth:
            log_with_context(self.logger, "info", "No subdirectories found, skipping empty dir purge", {})
            return 0

        # Phase B: Process depth levels from deepest to shallowest (bottom-up).
        # After processing each level, the list for that level is deleted, freeing
        # memory before the next level is processed. This means peak deletion memory
        # is O(dirs_at_widest_level) rather than O(total_dirs).
        depth_levels = sorted(dirs_by_depth.keys(), reverse=True)

        log_with_context(
            self.logger,
            "info",
            "Phase 1b: Processing directories bottom-up for empty dir deletion",
            {
                "total_dirs_to_check": total_dirs_discovered,
                "depth_levels_count": len(depth_levels),
                "max_depth": max_depth,
                "max_concurrency_deletion": self.max_concurrency_deletion,
            },
        )

        deleted_count = 0
        checked_count = 0
        deletion_errors = 0
        skipped_not_empty = 0
        deleted_lock = asyncio.Lock()
        rate_limit_reached = False
        phase1b_checkpoint_saved = False

        # Use a semaphore for concurrency control
        deletion_sem = asyncio.Semaphore(self.max_concurrency_deletion)

        async def check_and_delete_if_empty(directory: Path) -> None:
            """Check if directory is empty and delete it if so."""
            nonlocal deleted_count, checked_count, deletion_errors, skipped_not_empty

            async with deletion_sem:
                try:
                    # Rate limit check - atomic check-and-increment under lock
                    if self.max_empty_dirs_to_delete > 0:
                        async with self.stats_lock:
                            to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                            if to_delete_count >= self.max_empty_dirs_to_delete:
                                return
                            # Reserve a slot atomically
                            self.stats["empty_dirs_to_delete"] = to_delete_count + 1

                    # Normalize for root protection
                    try:
                        dir_resolved = directory.resolve()
                    except (OSError, RuntimeError):
                        dir_resolved = directory

                    if dir_resolved == root_resolved:
                        # Unreserve slot
                        if self.max_empty_dirs_to_delete > 0:
                            async with self.stats_lock:
                                current = self.stats.get("empty_dirs_to_delete", 0)
                                self.stats["empty_dirs_to_delete"] = max(0, current - 1)
                        return

                    # Check if directory is empty using a lightweight check.
                    # async_is_dir_empty only peeks at the first entry via next(scandir),
                    # avoiding materializing a full list(scandir) which for non-empty dirs
                    # on EFS could be thousands of DirEntry objects that bloat memory.
                    is_empty = await async_is_dir_empty(directory, self.scandir_executor)

                    async with deleted_lock:
                        checked_count += 1

                    if not is_empty:
                        async with deleted_lock:
                            skipped_not_empty += 1
                        # Unreserve slot since we're not deleting
                        if self.max_empty_dirs_to_delete > 0:
                            async with self.stats_lock:
                                current = self.stats.get("empty_dirs_to_delete", 0)
                                self.stats["empty_dirs_to_delete"] = max(0, current - 1)
                        return

                    # Directory is empty - delete it
                    if not self.dry_run:
                        await async_rmdir_with_eacces_retry(directory)
                        # Update stats: empty_dirs_to_delete tracks total attempted (for reporting)
                        # empty_dirs_deleted tracks actual deletions
                        if self.max_empty_dirs_to_delete == 0:
                            # No rate limit - increment to_delete for reporting only
                            await self.update_stats(empty_dirs_deleted=1, empty_dirs_to_delete=1)
                        else:
                            # Rate limited - slot already reserved above, just update deleted
                            await self.update_stats(empty_dirs_deleted=1)
                        self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
                        self.logger.debug(f"Removed empty directory: {directory}")
                    else:
                        # Dry run - count but don't delete
                        if self.max_empty_dirs_to_delete == 0:
                            await self.update_stats(empty_dirs_to_delete=1)
                        # else: slot already reserved above
                        self.logger.debug(f"Would remove empty directory: {directory}")

                    async with deleted_lock:
                        deleted_count += 1

                except FileNotFoundError:
                    # Already deleted - unreserve slot
                    if self.max_empty_dirs_to_delete > 0:
                        async with self.stats_lock:
                            current = self.stats.get("empty_dirs_to_delete", 0)
                            self.stats["empty_dirs_to_delete"] = max(0, current - 1)
                except OSError as e:
                    if "not empty" in str(e).lower() or "directory not empty" in str(e).lower():
                        async with deleted_lock:
                            skipped_not_empty += 1
                    else:
                        async with deleted_lock:
                            deletion_errors += 1
                        self.logger.debug(f"Error deleting {directory}: {e}")
                        await self.update_stats(errors=1)
                    # Unreserve slot on error
                    if self.max_empty_dirs_to_delete > 0:
                        async with self.stats_lock:
                            current = self.stats.get("empty_dirs_to_delete", 0)
                            self.stats["empty_dirs_to_delete"] = max(0, current - 1)

        # Process each depth level from deepest to shallowest
        BATCH_SIZE = 5000  # Process this many at a time within each level

        for current_depth in depth_levels:
            if rate_limit_reached:
                break

            level_dirs = dirs_by_depth.pop(current_depth)  # pop to free as we go
            level_size = len(level_dirs)
            i = 0

            while i < level_size:
                # Check rate limit
                if self.max_empty_dirs_to_delete > 0:
                    async with self.stats_lock:
                        current_deleted = self.stats.get("empty_dirs_deleted", 0)
                        if current_deleted >= self.max_empty_dirs_to_delete:
                            log_with_context(
                                self.logger,
                                "info",
                                "Rate limit reached for empty directory deletion",
                                {
                                    "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                                    "deleted": current_deleted,
                                    "checked": checked_count,
                                    "current_depth": current_depth,
                                    "remaining_in_level": level_size - i,
                                    "remaining_levels": len(dirs_by_depth),
                                },
                            )
                            rate_limit_reached = True
                            break

                # Apply back-pressure: check memory and adjust batch size
                if self.memory_limit_mb > 0:
                    memory_mb = get_memory_usage_mb()
                    memory_percent = memory_mb / self.memory_limit_mb

                    if memory_percent > 0.95:
                        # Critical memory during deletion - GC aggressively and recheck
                        gc.collect()
                        await asyncio.sleep(2.0)
                        memory_mb = get_memory_usage_mb()
                        memory_percent = memory_mb / self.memory_limit_mb
                        if memory_percent > 0.95:
                            # Still critical after GC - save Phase 1b checkpoint and abort
                            # Remaining dirs: unprocessed tail of current level + all shallower levels
                            _remaining: dict[str, list[str]] = {str(current_depth): [str(p) for p in level_dirs[i:]]}
                            for _d, _paths in dirs_by_depth.items():
                                _remaining[str(_d)] = [str(p) for p in _paths]
                            if self.dir_deletion_checkpoint_file:
                                save_phase1b_checkpoint(
                                    self.dir_deletion_checkpoint_file,
                                    str(self.root_path),
                                    _remaining,
                                    {"root_path": str(self.root_path)},
                                )
                                phase1b_checkpoint_saved = True
                            log_with_context(
                                self.logger,
                                "warning",
                                "Memory critical during deletion, checkpoint saved",
                                {
                                    "memory_mb": round(memory_mb, 1),
                                    "memory_percent": round(memory_percent * 100, 1),
                                    "memory_limit_mb": self.memory_limit_mb,
                                    "deleted": deleted_count,
                                    "checked": checked_count,
                                    "current_depth": current_depth,
                                    "remaining_in_level": level_size - i,
                                    "remaining_levels": len(dirs_by_depth),
                                    "checkpoint_saved": phase1b_checkpoint_saved,
                                },
                            )
                            rate_limit_reached = True
                            break
                        current_batch_size = max(100, BATCH_SIZE // 4)
                    elif memory_percent > 0.90:
                        # High memory - reduce batch size and pause
                        gc.collect()
                        await asyncio.sleep(1.0)
                        current_batch_size = max(100, BATCH_SIZE // 4)
                    elif memory_percent > 0.75:
                        current_batch_size = max(500, BATCH_SIZE // 2)
                    else:
                        current_batch_size = BATCH_SIZE
                else:
                    current_batch_size = BATCH_SIZE

                # Get the next batch from this level
                batch_end = min(i + current_batch_size, level_size)
                batch = level_dirs[i:batch_end]
                i = batch_end

                # Process batch concurrently (semaphore limits actual concurrency)
                # Use asyncio.wait with timeout to prevent Phase 1b hangs (Issue 3):
                # Previously, a few directories at depth 1 stalled for ~5 minutes
                # because asyncio.gather waited indefinitely on slow EFS operations.
                tasks = [asyncio.create_task(check_and_delete_if_empty(d)) for d in batch]
                if tasks:
                    done, pending_tasks = await asyncio.wait(tasks, timeout=120.0)

                    if pending_tasks:
                        timed_out = len(pending_tasks)
                        for t in pending_tasks:
                            t.cancel()
                        await asyncio.gather(*pending_tasks, return_exceptions=True)
                        log_with_context(
                            self.logger,
                            "warning",
                            f"Phase 1b: {timed_out} directory operations timed out after 120s, skipping",
                            {"timed_out": timed_out, "batch_size": len(batch), "depth": current_depth},
                        )
                        async with deleted_lock:
                            deletion_errors += timed_out
                        await self.update_stats(errors=timed_out)

                # Free batch reference
                del batch
                del tasks

                # Periodic GC during large deletion runs to prevent memory creep
                if checked_count % 10000 == 0:
                    gc.collect()

            # Free this entire depth level before moving to the next
            del level_dirs

            # Log progress after each depth level
            memory_mb = get_memory_usage_mb() if self.memory_limit_mb > 0 else 0
            remaining_dirs = sum(len(v) for v in dirs_by_depth.values())
            log_with_context(
                self.logger,
                "info",
                "Empty directory purge progress",
                {
                    "checked": checked_count,
                    "deleted": deleted_count,
                    "skipped_not_empty": skipped_not_empty,
                    "errors": deletion_errors,
                    "total_dirs": total_dirs_discovered,
                    "completed_depth": current_depth,
                    "remaining_levels": len(dirs_by_depth),
                    "remaining_dirs": remaining_dirs,
                    "memory_mb": round(memory_mb, 1),
                },
            )

        # Drain discovery queue — if memory aborted, collect remaining dirs for checkpoint
        while True:
            try:
                p = discovery_queue.get_nowait()
                if memory_abort and self.dir_deletion_checkpoint_file:
                    phase1a_checkpoint_pending.append(p)
            except asyncio.QueueEmpty:
                break

        # Free all Phase 1 directory data
        dirs_by_depth.clear()
        del dirs_by_depth

        # Aggressive GC so Phase 2 starts with lower baseline memory
        for _ in range(3):
            gc.collect()

        log_with_context(
            self.logger,
            "info",
            "Phase 1 complete: Standalone empty directory purge finished",
            {
                "total_dirs_discovered": total_dirs_discovered,
                "checked": checked_count,
                "deleted": deleted_count,
                "skipped_not_empty": skipped_not_empty,
                "errors": deletion_errors + discovery_errors,
                "memory_mb": round(get_memory_usage_mb(), 1),
                "checkpoint_pending": len(phase1a_checkpoint_pending) if memory_abort else 0,
                "phase1b_checkpoint_saved": phase1b_checkpoint_saved,
            },
        )

        # If Phase 1b memory aborted and checkpoint was saved, exit 75 so the loop respawns.
        if phase1b_checkpoint_saved:
            log_with_context(
                self.logger,
                "info",
                "Phase 1b checkpoint saved. Run with --dir-deletion-resume to continue deletion.",
                {
                    "checkpoint_file": str(self.dir_deletion_checkpoint_file),
                    "dirs_deleted_this_run": deleted_count,
                },
            )
            raise CheckpointExit(
                f"Phase 1b memory critical: checkpoint saved to {self.dir_deletion_checkpoint_file}. "
                "Run with --dir-deletion-resume to continue."
            )

        # If memory aborted discovery and a checkpoint file is configured:
        # save the BFS frontier so the next run can resume from where we stopped,
        # then exit 75 so the loop knows to respawn.
        if memory_abort and self.dir_deletion_checkpoint_file:
            save_phase1a_checkpoint(
                self.dir_deletion_checkpoint_file,
                str(self.root_path),
                [str(p) for p in phase1a_checkpoint_pending],
                {"root_path": str(self.root_path)},
            )
            log_with_context(
                self.logger,
                "info",
                "Phase 1a checkpoint saved. Run with --dir-deletion-resume to continue discovery.",
                {
                    "checkpoint_file": str(self.dir_deletion_checkpoint_file),
                    "pending_dirs_count": len(phase1a_checkpoint_pending),
                    "dirs_deleted_this_run": deleted_count,
                },
            )
            raise CheckpointExit(
                f"Phase 1a memory critical: checkpoint saved to {self.dir_deletion_checkpoint_file}. "
                "Run with --dir-deletion-resume to continue."
            )

        # Clean completion — delete checkpoint file if it exists (tree fully discovered)
        if self.dir_deletion_checkpoint_file and self.dir_deletion_checkpoint_file.exists():
            try:
                self.dir_deletion_checkpoint_file.unlink()
                log_with_context(
                    self.logger,
                    "info",
                    "Phase 1a checkpoint deleted (discovery complete)",
                    {"checkpoint_file": str(self.dir_deletion_checkpoint_file)},
                )
            except OSError as e:
                self.logger.warning("Could not delete Phase 1a checkpoint: %s", e)

        return deleted_count

    async def _process_file_batch(self, file_tasks: list) -> None:
        """
        Process a batch of file tasks and free memory immediately.

        Args:
            file_tasks: List of file processing tasks
        """
        if not file_tasks:
            return

        # Check memory before processing
        await self.check_memory_pressure()  # Ignore return value for file batch processing

        # Process batch - return_exceptions=True prevents one failure from canceling others
        results = await asyncio.gather(*file_tasks, return_exceptions=True)

        # Log any unexpected exceptions that weren't handled by process_file
        # (process_file handles its own exceptions, but defensive check is good)
        for result in results:
            if isinstance(result, Exception):
                log_with_context(
                    self.logger,
                    "error",
                    "Unexpected exception in batch processing",
                    {"error": str(result), "error_type": type(result).__name__},
                )

        self.logger.debug(f"Processed batch of {len(file_tasks)} files")

    async def _scan_and_purge_files(self) -> None:
        """
        Phase 2: Scan the directory tree and purge old files using BFS queue + worker pool.

        Uses the same flat BFS pattern as Phase 1a discovery, extended to also
        process files. This replaces the previous recursive scan_directory() approach
        which had issues with:
        - Blocking scandir for large directories (37-min hang on 700K+ entries)
        - 10K iteration limit silently abandoning subdirectories
        - Unawaited coroutines on shutdown/cancellation
        - Recursive coroutine stack causing memory growth

        Architecture:
        - One shared asyncio.Queue holds directories to scan
        - N worker coroutines pull directories and process them
        - Each worker uses async_scandir_batched() for non-blocking streaming
        - Files are batched and processed via _process_file_batch()
        - Discovered subdirectories are pushed back onto the queue
        - Workers exit when queue is drained (tracked via pending_dirs counter)
        - queue_maxsize bounds memory: producers block when full (back-pressure)

        Checkpoint/resume: When memory exceeds 95% and checkpoint_file is set,
        workers save pending dirs and exit. Main saves checkpoint and raises
        CheckpointExit. Use --resume to continue from checkpoint.
        """
        scan_queue: asyncio.Queue[Path] = (
            asyncio.Queue(maxsize=self.queue_maxsize) if self.queue_maxsize > 0 else asyncio.Queue()
        )
        pending_dirs = 0
        pending_lock = asyncio.Lock()
        scan_done = asyncio.Event()
        # Gates ``scan_done`` so Phase 2 cannot declare itself "complete"
        # while the pending-dirs feeder still has frontier paths to deliver.
        # Workers can transiently drain the queue faster than the feeder can
        # pump it (e.g. a stretch of leaf dirs that decrement pending_dirs
        # without re-incrementing); on 2026-06-04 that race fired with a
        # mis-initialised counter and the worker-set scan_done deleted the
        # entire checkpoint after a premature "completion".  We set the
        # event up front when no feeder will run so single-pass scans
        # (Phase 2 from scratch) behave as before.
        feeder_done = asyncio.Event()

        # Resume from checkpoint if requested.
        #
        # The frontier ("pending_dirs") lives in a sidecar file
        # ``<checkpoint>.pending_dirs.gz`` and is streamed line-by-line into
        # the bounded ``scan_queue`` by a dedicated feeder task.  Before
        # this change the frontier was loaded into a single ``list[str]``
        # at resume time — at ~30 M paths that one list alone was the
        # dominant driver of the resume-baseline spiral that recurred on
        # prod after the empty_dirs sidecar fix.
        #
        # Legacy embedded frontier: older checkpoints still carry
        # ``pending_dirs`` inline in the main JSON.  We migrate them to the
        # sidecar via ``write_pending_dirs_sidecar`` and drop the in-memory
        # copy immediately so the resume baseline is the same on the first
        # post-upgrade resume as on every subsequent one.
        pending_dirs_iter: Iterator[str] | None = None  # set when resuming
        if self.resume and self.checkpoint_file and self.checkpoint_file.exists():
            cp = load_checkpoint(self.checkpoint_file)
            if cp:
                # Restore stats for progress reporting
                for k, v in cp.get("stats", {}).items():
                    if k in self.stats and isinstance(self.stats[k], (int, float)):
                        self.stats[k] = v
                # Legacy empty_dirs migration (see commit 736e513).  If an
                # older checkpoint still has empty_dirs embedded, flush
                # them to the sidecar and drop the in-memory copy so
                # Phase 2 runs with a clean baseline.
                legacy_empty = cp.get("empty_dirs")
                if legacy_empty and self.checkpoint_file is not None:
                    try:
                        append_empty_dirs_sidecar(self.checkpoint_file, legacy_empty)
                    except OSError as e:
                        self.logger.warning("Could not migrate legacy empty_dirs to sidecar: %s", e)
                    cp["empty_dirs"] = []
                    del legacy_empty
                # Legacy pending_dirs migration.  Older checkpoints embed
                # the frontier as a list; rewrite it to the sidecar and
                # release the list reference before any worker starts so
                # the resume baseline drops back to ~queue_maxsize.  Crucially
                # we also propagate the migrated count into
                # ``pending_dirs_count`` so the resume bookkeeping below sees
                # the real frontier size — without this, the legacy path
                # leaves pending_dirs=0 and the "fresh start" branch
                # below redundantly re-seeds the root, forcing a re-scan
                # of the entire tree.
                legacy_pending = cp.get("pending_dirs")
                if legacy_pending and self.checkpoint_file is not None:
                    try:
                        migrated_count = write_pending_dirs_sidecar(self.checkpoint_file, legacy_pending)
                        cp["pending_dirs_count"] = migrated_count
                    except OSError as e:
                        self.logger.warning("Could not migrate legacy pending_dirs to sidecar: %s", e)
                    cp["pending_dirs"] = []
                    del legacy_pending
                # ``pending_dirs`` counts outstanding work in flight; it is
                # incremented by the feeder per-pushed path and by workers
                # per-discovered subdir, and decremented when a directory
                # finishes scanning.  The pre-feeder initial value is
                # therefore always 0 — the count in the main JSON is used
                # only for progress logging (and historically also caused
                # the race that wiped the prod checkpoint on 2026-06-04).
                resume_pending_count_for_log = cp.get("pending_dirs_count", 0) or 0
                # Open the sidecar (best-effort: missing sidecar means the
                # frontier was already fully drained — Phase 2 simply
                # exits cleanly after the root is seeded below).
                if self.checkpoint_file is not None and pending_dirs_sidecar_path(self.checkpoint_file).exists():
                    pending_dirs_iter = stream_pending_dirs_sidecar(self.checkpoint_file)
                log_with_context(
                    self.logger,
                    "info",
                    "Phase 2: Resuming from checkpoint",
                    {
                        "checkpoint_file": str(self.checkpoint_file),
                        "pending_dirs": resume_pending_count_for_log,
                        "files_scanned_so_far": self.stats.get("files_scanned", 0),
                        "dirs_scanned_so_far": self.stats.get("dirs_scanned", 0),
                        "pending_dirs_sidecar_exists": (
                            self.checkpoint_file is not None
                            and pending_dirs_sidecar_path(self.checkpoint_file).exists()
                        ),
                        "empty_dirs_sidecar_exists": (
                            self.checkpoint_file is not None and empty_dirs_sidecar_path(self.checkpoint_file).exists()
                        ),
                    },
                )
        if pending_dirs_iter is None:
            # No feeder (not resuming, or sidecar missing).  Seed the
            # scan from the configured root.  When a feeder is set up
            # we never reseed root here — the feeder pushes the frontier
            # and increments pending_dirs per path.
            scan_queue.put_nowait(self.root_path)
            pending_dirs = 1

        # Normalize root path once for Phase 3 empty-dir checks
        try:
            root_resolved = self.root_path.resolve()
        except (OSError, RuntimeError):
            root_resolved = self.root_path

        num_workers = self.max_concurrent_discovery

        log_with_context(
            self.logger,
            "info",
            "Phase 2: Starting BFS file scan with worker pool",
            {
                "root_path": str(self.root_path),
                "num_workers": num_workers,
                "queue_maxsize": self.queue_maxsize,
                "task_batch_size": self.task_batch_size,
                "max_concurrency_scanning": self.max_concurrency_scanning,
                "max_concurrency_deletion": self.max_concurrency_deletion,
            },
        )

        # Per-worker current directory: used to rescue in-flight dirs when workers are
        # blocked inside EFS syscalls (run_in_executor) and cannot respond to
        # _checkpoint_requested cooperatively.  Keyed by worker_id; value is the
        # directory the worker is currently scanning, or None when idle.
        worker_active_dirs: dict[int, Path | None] = {}

        async def _scan_worker(worker_id: int) -> None:
            """Worker that scans directories from queue and processes files."""
            nonlocal pending_dirs
            pending_subdirs: list[Path] = []  # Per-worker buffer when queue full (avoids deadlock)
            directory: Path | None = None  # Current dir being processed (for checkpoint)

            while not scan_done.is_set():
                # Checkpoint requested: contribute our pending and exit
                if self._checkpoint_requested:
                    async with self._checkpoint_lock:
                        if directory is not None:
                            self._checkpoint_pending.append(directory)
                        self._checkpoint_pending.extend(pending_subdirs)
                    scan_done.set()
                    break

                # Drain pending subdirs into queue so we never block on put (deadlock fix)
                _drain_pending_to_queue(scan_queue, pending_subdirs)
                if scan_done.is_set():
                    async with pending_lock:
                        pending_dirs -= len(pending_subdirs)
                    break

                # Get next directory from queue
                try:
                    directory = await asyncio.wait_for(scan_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if scan_done.is_set():
                        break
                    continue

                # Record current dir so the gather loop can rescue it if this worker
                # gets stuck on an EFS syscall and cannot exit cooperatively.
                worker_active_dirs[worker_id] = directory

                # Track this directory as active (for stuck detection diagnostics)
                async with self.active_directories_lock:
                    self.active_directories.add(directory)

                file_task_buffer: list = []

                try:
                    await self.update_stats(dirs_scanned=1)
                    self.rate_tracker.record(self.current_phase, "dirs", 1)

                    # Track scandir diagnostics (DEBUG level only)
                    scan_start_time = time.time() if self.logger.isEnabledFor(logging.DEBUG) else None

                    # Scan directory entries using batched scandir (streaming, non-blocking)
                    # This prevents the 37-min hang that occurred with list(os.scandir())
                    # on directories with 700K+ entries.
                    async for batch in async_scandir_batched(directory, self.scandir_executor):
                        # Check memory pressure between batches (may set _checkpoint_requested)
                        await self.check_memory_pressure()
                        if self._checkpoint_requested:
                            async with self._checkpoint_lock:
                                self._checkpoint_pending.append(directory)
                                self._checkpoint_pending.extend(pending_subdirs)
                            break

                        for entry in batch:
                            entry_path = Path(entry.path)
                            try:
                                # Check if entry is a symlink (don't follow)
                                is_symlink = await aiofiles.os.path.islink(entry_path)
                                if is_symlink:
                                    await self.update_stats(symlinks_skipped=1)
                                    self.logger.debug(f"Skipping symlink: {entry_path}")
                                    continue

                                # Handle files with streaming buffer
                                if entry.is_file(follow_symlinks=False):
                                    # Skip file processing when max_age_days=0 (empty dir deletion only)
                                    if self.max_age_days > 0:
                                        file_task_buffer.append(self.process_file(entry_path))

                                        # Flush buffer when it reaches batch size
                                        if len(file_task_buffer) >= self.task_batch_size:
                                            try:
                                                await self._process_file_batch(file_task_buffer)
                                            finally:
                                                file_task_buffer.clear()

                                elif entry.is_dir(follow_symlinks=False):
                                    # Push subdirectory onto queue; buffer if full to avoid deadlock
                                    async with pending_lock:
                                        pending_dirs += 1
                                    try:
                                        scan_queue.put_nowait(entry_path)
                                    except asyncio.QueueFull:
                                        pending_subdirs.append(entry_path)

                                else:
                                    # Special file types: sockets, FIFOs, block/char devices, etc.
                                    await self.update_stats(special_files_skipped=1)
                                    self.logger.debug(f"Skipping special file: {entry_path}")

                            except OSError as e:
                                log_with_context(
                                    self.logger,
                                    "warning",
                                    "Error checking entry",
                                    {"path": str(entry_path), "error": str(e)},
                                )
                                await self.update_stats(errors=1)

                    # Record scandir diagnostics (DEBUG level only)
                    if scan_start_time is not None:
                        scan_elapsed = time.time() - scan_start_time
                        async with self.scandir_lock:
                            self.scandir_call_count += 1
                            self.scandir_total_time += scan_elapsed

                    # Flush remaining file buffer after scanning all entries
                    if file_task_buffer:
                        try:
                            await self._process_file_batch(file_task_buffer)
                        finally:
                            file_task_buffer.clear()

                    # Phase 3 prep: check if directory became empty after processing
                    if self.remove_empty_dirs and self.max_age_days > 0:
                        try:
                            is_empty = await async_is_dir_empty(directory, self.scandir_executor)
                            if is_empty:
                                try:
                                    dir_resolved = directory.resolve()
                                except (OSError, RuntimeError):
                                    dir_resolved = directory
                                if dir_resolved != root_resolved:
                                    self.empty_dirs.add(directory)
                        except (FileNotFoundError, PermissionError, OSError):
                            pass  # Directory gone or inaccessible

                    # Drain pending subdirs after finishing directory (deadlock fix)
                    _drain_pending_to_queue(scan_queue, pending_subdirs)

                except PermissionError as e:
                    await self._log_eacces_throttled(
                        "phase2.scan",
                        "Permission denied for directory",
                        directory,
                        e,
                    )
                    await self.update_stats(errors=1)
                except Exception as e:
                    log_with_context(
                        self.logger,
                        "error",
                        "Error scanning directory",
                        {"directory": str(directory), "error": str(e), "error_type": type(e).__name__},
                    )
                    await self.update_stats(errors=1)
                finally:
                    # Close any unawaited coroutines remaining in buffer (Issue 2 fix)
                    # After successful flush, buffer is empty so this is a no-op.
                    # After exception, this prevents RuntimeWarning about unawaited coroutines.
                    for coro in file_task_buffer:
                        coro.close()
                    file_task_buffer.clear()

                    # Remove from active directories
                    async with self.active_directories_lock:
                        self.active_directories.discard(directory)

                    # Clear the per-worker active-dir tracker now that this dir is done
                    worker_active_dirs[worker_id] = None

                    # Decrement pending counter; when zero AND the feeder has
                    # finished, all work is done.  Gating on feeder_done
                    # prevents a transient ``pending_dirs == 0`` (workers
                    # racing ahead of the feeder) from terminating Phase 2
                    # while the feeder still holds sidecar entries.
                    async with pending_lock:
                        pending_dirs -= 1
                        if pending_dirs == 0 and feeder_done.is_set():
                            scan_done.set()

        # Pending-dirs feeder: stream the frontier sidecar line-by-line
        # into ``scan_queue``.  Replaces the prior "load whole list, slice
        # remainder, drain via loader task" pattern, which forced a
        # ~30 M-string materialisation at resume time and was the cause of
        # the second-wave death-spiral observed on 2026-06-04.
        #
        # The feeder always holds at most one in-flight path ``_unsent``
        # in addition to the open file handle.  On checkpoint exit the
        # caller pipes ``_unsent`` + the file iterator's unread tail into
        # the new sidecar via ``feeder_tail()`` — see the rescue/cooperative
        # checkpoint paths below for the merge point.
        feeder_unsent: list[str] = []  # 0- or 1-element holder

        def feeder_tail() -> Iterator[str]:
            """Yield (in order): the path the feeder pulled but never
            enqueued, then the remainder of the sidecar from the feeder's
            current file position.  Safe to call after the feeder has
            exited.  Yields nothing when the feeder consumed the whole
            sidecar cleanly.
            """
            if feeder_unsent:
                yield feeder_unsent.pop()
            if pending_dirs_iter is not None:
                yield from pending_dirs_iter

        async def _pending_dirs_feeder() -> None:
            nonlocal pending_dirs
            try:
                if pending_dirs_iter is None:
                    return
                for p in pending_dirs_iter:
                    if self._checkpoint_requested or scan_done.is_set():
                        feeder_unsent.append(p)
                        return
                    # Reserve the slot in pending_dirs BEFORE pushing so a
                    # worker can't grab the path and decrement before the
                    # increment lands (which would let pending_dirs hit 0
                    # transiently and — with the feeder_done gate clear —
                    # fire scan_done prematurely).
                    async with pending_lock:
                        pending_dirs += 1
                    # put_nowait + back-off polling keeps the feeder responsive
                    # to the checkpoint flag even when workers have exited and
                    # the queue can no longer drain.
                    while True:
                        try:
                            scan_queue.put_nowait(Path(p))
                            break
                        except asyncio.QueueFull:
                            if self._checkpoint_requested or scan_done.is_set():
                                # Roll back the slot we reserved; this path
                                # is captured via feeder_unsent and saved to
                                # the new sidecar by the checkpoint code.
                                async with pending_lock:
                                    pending_dirs -= 1
                                feeder_unsent.append(p)
                                return
                            await asyncio.sleep(0.1)
            finally:
                # Always signal feeder completion so the worker decrement
                # check can fire ``scan_done`` once pending_dirs hits 0.
                feeder_done.set()
                # If workers are quiesced (pending_dirs==0 because they
                # raced ahead while the feeder was queue-blocked or sleeping),
                # they're stuck in ``scan_queue.get()`` with a 1s timeout
                # and won't notice the queue is now permanently empty until
                # something flips ``scan_done``.  Flip it here.
                async with pending_lock:
                    if pending_dirs == 0:
                        scan_done.set()

        # Launch workers and (when resuming with a sidecar) the feeder.
        # When there is no feeder, signal ``feeder_done`` immediately so the
        # worker decrement check can fire ``scan_done`` on the normal
        # single-pass path (Phase 2 from scratch / no resume).
        workers = [asyncio.create_task(_scan_worker(i)) for i in range(num_workers)]
        # Maps task object-id → worker_id so we can look up worker_active_dirs on cancel
        task_to_worker_id: dict[int, int] = {id(task): i for i, task in enumerate(workers)}
        if pending_dirs_iter is not None:
            loader_task: asyncio.Task | None = asyncio.create_task(_pending_dirs_feeder())
        else:
            feeder_done.set()
            loader_task = None

        # Wait for workers and loader to complete.
        # On the checkpoint path, workers stuck inside EFS syscalls (run_in_executor) cannot
        # respond to _checkpoint_requested. After _STUCK_WORKER_CANCEL_TIMEOUT seconds we
        # cancel them (after stuck_worker_cancel_timeout seconds), rescue their in-flight
        # directory into _checkpoint_pending for retry on the next run, then proceed to
        # save the checkpoint normally.
        pending_tasks: set[asyncio.Task] = set(workers)
        if loader_task is not None:
            pending_tasks.add(loader_task)
        # Checkpoint write task started early (before finally overhead) on the rescue path.
        # Initialised here so the post-finally block can always reference it.
        _cp_task_early: asyncio.Future | None = None
        _cp_pending_count: int = 0
        try:
            while pending_tasks:
                done, pending_tasks = await asyncio.wait(pending_tasks, timeout=1.0)
                if self._checkpoint_requested and pending_tasks:
                    # Give remaining tasks up to stuck_worker_cancel_timeout to exit
                    # cooperatively (normal workers check the flag each loop iteration).
                    _, still_stuck = await asyncio.wait(pending_tasks, timeout=self.stuck_worker_cancel_timeout)
                    if still_stuck:
                        for task in still_stuck:
                            wid = task_to_worker_id.get(id(task))
                            if wid is not None:
                                stuck_dir = worker_active_dirs.get(wid)
                                if stuck_dir is not None:
                                    async with self._checkpoint_lock:
                                        self._checkpoint_pending.append(stuck_dir)
                                    log_with_context(
                                        self.logger,
                                        "warning",
                                        "Worker stuck on EFS syscall, rescued in-flight directory for checkpoint retry",
                                        {
                                            "worker_id": wid,
                                            "directory": str(stuck_dir),
                                        },
                                    )
                        for task in still_stuck:
                            task.cancel()
                        # Do NOT await still_stuck here: run_in_executor threads blocked on EFS
                        # cannot be cancelled (cf_future.cancel() returns False for running
                        # threads), so asyncio.gather would block indefinitely.  os._exit(75)
                        # will kill the threads when the process exits after saving the checkpoint.
                    pending_tasks.clear()

                    # START CHECKPOINT WRITE EARLY — before the finally-block worker cleanup.
                    # The finally block waits up to 5 s for cooperative workers + 5 s for the
                    # feeder; starting the write here means it runs concurrently with that
                    # cleanup, gaining ~10 s of NFS write time before the OOM killer fires.
                    #
                    # Why it's safe to drain the queue here:
                    #   - cooperative workers already exited (they responded to _checkpoint_requested
                    #     during the stuck_worker_cancel_timeout wait above)
                    #   - stuck workers are blocked inside run_in_executor and cannot push to queue
                    #   - the feeder has had >= stuck_worker_cancel_timeout seconds to see the flag
                    #     and store its unsent path in feeder_unsent; we do a brief extra wait to confirm
                    if self.checkpoint_file:
                        # Brief wait for feeder to park its unsent path.  After
                        # stuck_worker_cancel_timeout seconds with the flag set the feeder
                        # has almost certainly already exited cooperatively; this is a safety net.
                        if loader_task is not None and not loader_task.done():
                            await asyncio.wait({loader_task}, timeout=1.0)
                        # Drain the bounded queue (≤ queue_maxsize entries).
                        _in_flight: list[Path] = []
                        while True:
                            try:
                                _in_flight.append(scan_queue.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                        async with self._checkpoint_lock:
                            _in_flight.extend(self._checkpoint_pending)
                        _in_flight_count = len(_in_flight)

                        def _pending_iter() -> Iterator[str]:
                            for p in _in_flight:
                                yield str(p)
                            # feeder_tail() yields feeder_unsent (≤ 1) and then
                            # streams the sidecar's unread tail line-by-line.
                            # Crucially this never materialises the whole tail
                            # in Python — even at 30 M+ entries, peak memory
                            # stays at queue_maxsize Path objects.
                            yield from feeder_tail()

                        # Count is best-effort for logging: known lower bound is
                        # the in-flight count plus the prior checkpoint's
                        # ``pending_dirs_count`` from the cp dict.  We don't pre-
                        # count the feeder tail because doing so would require
                        # iterating it twice.
                        _cp_pending_count = _in_flight_count + len(feeder_unsent)
                        _loop = asyncio.get_running_loop()
                        _empty_strs = [str(p) for p in self.empty_dirs]
                        _cp_task_early = asyncio.ensure_future(
                            _loop.run_in_executor(
                                None,
                                lambda: save_checkpoint(
                                    self.checkpoint_file,
                                    str(self.root_path),
                                    _pending_iter(),
                                    dict(self.stats),
                                    {
                                        "max_age_days": self.max_age_days,
                                        "root_path": str(self.root_path),
                                    },
                                    empty_dirs=_empty_strs,
                                ),
                            )
                        )
                        log_with_context(
                            self.logger,
                            "info",
                            "Checkpoint write started early (concurrent with worker cleanup)",
                            {
                                "in_flight_pending": _in_flight_count,
                                "checkpoint_file": str(self.checkpoint_file),
                            },
                        )
                    break
        finally:
            # Ensure clean shutdown: signal workers and cancel any still running
            scan_done.set()
            for w in workers:
                if not w.done():
                    w.cancel()
            if loader_task is not None and not loader_task.done():
                loader_task.cancel()
            # Brief timeout: cooperative workers finish quickly; stuck EFS threads cannot be
            # cancelled and will be killed by os._exit — don't block indefinitely waiting for them.
            # NOTE: asyncio.wait_for is intentionally NOT used here. In Python 3.12, wait_for
            # after its timeout fires calls fut.cancel() and then awaits cancellation acknowledgment
            # before raising TimeoutError. Since run_in_executor threads blocked on EFS cannot
            # acknowledge cancellation, wait_for would hang indefinitely. asyncio.wait(timeout=N)
            # simply returns (done, pending) after N seconds without attempting to cancel anything.
            await asyncio.wait(set(workers), timeout=5.0)
            if loader_task is not None:
                await asyncio.wait({loader_task}, timeout=5.0)

        # If checkpoint was requested (memory critical), wait for the in-progress write
        # (started early in the rescue path) or start one now (cooperative-exit path where
        # all workers finished before the stuck-worker cancel timeout fired).
        if self._checkpoint_requested and self.checkpoint_file:
            if _cp_task_early is not None:
                # Write was started before the finally block.  The finally block took up to 10 s
                # (5 s worker wait + 5 s feeder wait); give the remaining budget of ~50 s.
                done, _ = await asyncio.wait({_cp_task_early}, timeout=50.0)
                if _cp_task_early in done:
                    try:
                        written = _cp_task_early.result()  # re-raises if save_checkpoint threw
                        log_with_context(
                            self.logger,
                            "info",
                            "Checkpoint saved, exit for resume. Run with --resume to continue.",
                            {
                                "checkpoint_file": str(self.checkpoint_file),
                                "pending_dirs_count": written,
                            },
                        )
                    except Exception as exc:
                        log_with_context(
                            self.logger,
                            "warning",
                            "Checkpoint write failed (NFS error). Old checkpoint preserved — will retry on next run.",
                            {
                                "checkpoint_file": str(self.checkpoint_file),
                                "error": str(exc),
                            },
                        )
                else:
                    log_with_context(
                        self.logger,
                        "warning",
                        "Checkpoint write timed out (EFS NFS open/write syscall hung). "
                        "Old checkpoint preserved — will retry on next run.",
                        {"checkpoint_file": str(self.checkpoint_file)},
                    )
            else:
                # Cooperative-exit path: all workers finished before stuck_worker_cancel_timeout
                # fired, so no early write was started.  Start one now.
                # (Same executor + asyncio.wait pattern — see rationale in the rescue path above.)
                _coop_in_flight: list[Path] = []
                while True:
                    try:
                        _coop_in_flight.append(scan_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                async with self._checkpoint_lock:
                    _coop_in_flight.extend(self._checkpoint_pending)
                loop = asyncio.get_running_loop()
                empty_dirs_strs = [str(p) for p in self.empty_dirs]

                def _coop_pending_iter() -> Iterator[str]:
                    for p in _coop_in_flight:
                        yield str(p)
                    yield from feeder_tail()

                cp_task = asyncio.ensure_future(
                    loop.run_in_executor(
                        None,
                        lambda: save_checkpoint(
                            self.checkpoint_file,
                            str(self.root_path),
                            _coop_pending_iter(),
                            dict(self.stats),
                            {
                                "max_age_days": self.max_age_days,
                                "root_path": str(self.root_path),
                            },
                            empty_dirs=empty_dirs_strs,
                        ),
                    ),
                )
                done, _ = await asyncio.wait({cp_task}, timeout=60.0)
                if cp_task in done:
                    try:
                        written = cp_task.result()  # re-raises if save_checkpoint threw
                        log_with_context(
                            self.logger,
                            "info",
                            "Checkpoint saved, exit for resume. Run with --resume to continue.",
                            {
                                "checkpoint_file": str(self.checkpoint_file),
                                "pending_dirs_count": written,
                            },
                        )
                    except Exception as exc:
                        log_with_context(
                            self.logger,
                            "warning",
                            "Checkpoint write failed (NFS error). Old checkpoint preserved — will retry on next run.",
                            {
                                "checkpoint_file": str(self.checkpoint_file),
                                "error": str(exc),
                            },
                        )
                else:
                    log_with_context(
                        self.logger,
                        "warning",
                        "Checkpoint write timed out (EFS NFS open/write syscall hung). "
                        "Old checkpoint preserved — will retry on next run.",
                        {"checkpoint_file": str(self.checkpoint_file)},
                    )
            raise CheckpointExit("Memory critical, checkpoint save attempted. Run with --resume to continue.")

    async def _background_progress_reporter(self) -> None:
        """
        Background task that logs progress every N seconds.

        This ensures progress updates even when processing is slow or
        there are long periods of directory traversal without file processing.
        Also detects stuck conditions and provides diagnostic information.
        """
        while True:
            await asyncio.sleep(self.progress_interval)

            # Log current progress
            async with self.stats_lock:
                current_time = time.time()
                elapsed = current_time - self.stats.get("start_time", current_time)

                current_files = self.stats["files_scanned"]
                current_dirs = self.stats["dirs_scanned"]

                # Calculate overall rates using scanning duration only (excludes empty dir removal time)
                # If scanning is complete, use scanning duration; otherwise use elapsed time
                if self.scanning_end_time is not None:
                    scanning_duration = self.scanning_end_time - self.stats.get("start_time", current_time)
                    files_per_second_overall = (
                        self.stats["files_scanned"] / scanning_duration if scanning_duration > 0 else 0
                    )
                    dirs_per_second_overall = current_dirs / scanning_duration if scanning_duration > 0 else 0.0
                else:
                    # Still scanning, use elapsed time
                    files_per_second_overall = self.stats["files_scanned"] / elapsed if elapsed > 0 else 0
                    dirs_per_second_overall = current_dirs / elapsed if elapsed > 0 else 0.0

                memory_mb = get_memory_usage_mb()
                memory_percent = (memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0

                # Time-windowed rates (instant 10s, short-term 60s)
                files_per_second_instant = self.rate_tracker.get_rate("scanning", "files", 10.0)
                dirs_per_second_instant = self.rate_tracker.get_rate("scanning", "dirs", 10.0)
                files_per_second_short = self.rate_tracker.get_rate("scanning", "files", 60.0)
                dirs_per_second_short = self.rate_tracker.get_rate("scanning", "dirs", 60.0)

                # Per-phase rates
                scanning_files_rate = self.rate_tracker.get_phase_rate("scanning", "files")
                scanning_dirs_rate = self.rate_tracker.get_phase_rate("scanning", "dirs")
                deletion_files_rate = self.rate_tracker.get_phase_rate("deletion", "files")
                empty_dirs_rate = self.rate_tracker.get_phase_rate("removing_empty_dirs", "dirs")

                # Update peak rates
                self.rate_tracker.update_peak_rate("files_per_second", files_per_second_overall)
                self.rate_tracker.update_peak_rate("dirs_per_second", dirs_per_second_overall)
                if deletion_files_rate > 0:
                    self.rate_tracker.update_peak_rate("files_deleted_per_second", deletion_files_rate)
                if empty_dirs_rate > 0:
                    self.rate_tracker.update_peak_rate("empty_dirs_per_second", empty_dirs_rate)

                # Get concurrency utilization metrics
                async with self.active_tasks_lock:
                    current_active_tasks = self.active_tasks
                    peak_active_tasks = self.max_active_tasks

                # Calculate semaphore availability (approximate)
                # Note: Semaphore doesn't expose available count, so we estimate
                # For backward compatibility, use max of both limits
                max_concurrency_total = max(self.max_concurrency_scanning, self.max_concurrency_deletion)
                available_slots = max(0, max_concurrency_total - current_active_tasks)
                concurrency_utilization_percent = (
                    (current_active_tasks / max_concurrency_total * 100) if max_concurrency_total > 0 else 0.0
                )

                # Check if DEBUG level logging is enabled
                is_debug = self.logger.isEnabledFor(logging.DEBUG)

                # Build progress update with phase-specific metrics
                progress_data = {
                    # Always shown
                    "elapsed_seconds": round(elapsed, 1),
                    "phase": self.current_phase,
                    "errors": self.stats["errors"],
                    "memory_backpressure_events": self.stats.get("memory_backpressure_events", 0),
                }

                # Phase-specific metrics
                if self.current_phase == "removing_empty_dirs":
                    # During empty dir removal: show dir removal metrics
                    progress_data["dirs_purged"] = self.stats.get("empty_dirs_deleted", 0)
                    progress_data["dirs_to_purge"] = self.stats.get("empty_dirs_to_delete", 0)
                    # Show overall rates (from scanning phase)
                    progress_data["files_per_second"] = round(files_per_second_overall, 1)
                    progress_data["dirs_per_second"] = round(dirs_per_second_overall, 1)
                else:
                    # During scanning: show file/dir scanning metrics
                    progress_data["files_scanned"] = current_files
                    progress_data["files_purged"] = self.stats["files_purged"]
                    progress_data["dirs_scanned"] = current_dirs
                    # Add files/dirs to purge if non-zero
                    if self.stats["files_to_purge"] > 0:
                        progress_data["files_to_purge"] = self.stats["files_to_purge"]
                    # Show overall rates
                    progress_data["files_per_second"] = round(files_per_second_overall, 1)
                    progress_data["dirs_per_second"] = round(dirs_per_second_overall, 1)

                # Memory usage (always shown)
                progress_data["memory_mb"] = round(memory_mb, 1)
                progress_data["memory_usage_percent"] = round(memory_percent, 1)

                # DEBUG-only detailed metrics
                if is_debug:
                    # Enhanced rate metrics - overall
                    progress_data["files_per_second_overall"] = round(files_per_second_overall, 1)
                    progress_data["dirs_per_second_overall"] = round(dirs_per_second_overall, 1)
                    # Time-windowed rates
                    progress_data["files_per_second_instant"] = round(files_per_second_instant, 1)
                    progress_data["dirs_per_second_instant"] = round(dirs_per_second_instant, 1)
                    progress_data["files_per_second_short"] = round(files_per_second_short, 1)
                    progress_data["dirs_per_second_short"] = round(dirs_per_second_short, 1)
                    # Per-phase rates
                    progress_data["scanning_files_per_second"] = round(scanning_files_rate, 1)
                    progress_data["scanning_dirs_per_second"] = round(scanning_dirs_rate, 1)
                    progress_data["deletion_files_per_second"] = round(deletion_files_rate, 1)
                    progress_data["empty_dirs_per_second"] = round(empty_dirs_rate, 1)
                    # Peak rates
                    progress_data["peak_files_per_second"] = round(
                        self.rate_tracker.peak_rates["files_per_second"]["value"], 1
                    )
                    progress_data["peak_dirs_per_second"] = round(
                        self.rate_tracker.peak_rates["dirs_per_second"]["value"], 1
                    )
                    progress_data["peak_files_deleted_per_second"] = round(
                        self.rate_tracker.peak_rates["files_deleted_per_second"]["value"], 1
                    )
                    progress_data["peak_empty_dirs_per_second"] = round(
                        self.rate_tracker.peak_rates["empty_dirs_per_second"]["value"], 1
                    )
                    # Concurrency utilization metrics
                    progress_data["active_tasks"] = current_active_tasks
                    progress_data["max_active_tasks"] = peak_active_tasks
                    progress_data["available_concurrency_slots"] = available_slots
                    progress_data["concurrency_utilization_percent"] = round(concurrency_utilization_percent, 1)
                    # Detailed memory metrics
                    progress_data["memory_mb_per_1k_files"] = (
                        round(memory_mb / (self.stats["files_scanned"] / 1000), 2)
                        if self.stats["files_scanned"] > 0
                        else 0.0
                    )

                log_with_context(
                    self.logger,
                    "info",
                    "Progress update",
                    progress_data,
                )

                # Track when we last logged progress (used by final progress check)
                self.last_progress_log = current_time

            # Get empty dir deletion progress
            current_empty_dirs_deleted = self.stats.get("empty_dirs_deleted", 0)

            # Stuck detection: check if progress has stalled
            # During scanning phase: check files_scanned and dirs_scanned
            # During empty dir removal phase: check empty_dirs_deleted
            # During discovery (Phase 1a): report progress instead of false hang warnings
            if self.current_phase == "removing_empty_dirs":
                if self._discovery_active:
                    # Still in Phase 1a directory discovery - not stuck, just scanning
                    # a large directory tree. Report discovery progress instead of a hang warning.
                    log_with_context(
                        self.logger,
                        "info",
                        "Phase 1a: Directory discovery in progress",
                        {
                            "phase": "discovery",
                            "current_directory": self._discovery_current_dir,
                            "dirs_discovered": self._discovery_dirs_found,
                            "dirs_queued": self._discovery_queue_size,
                            "entries_scanned": self._discovery_entries_scanned,
                            "memory_mb": round(get_memory_usage_mb(), 1),
                            "memory_usage_percent": round(get_memory_usage_mb() / self.memory_limit_mb * 100, 1)
                            if self.memory_limit_mb > 0
                            else 0,
                        },
                    )
                    # Reset stuck counter - discovery is making progress even if deletion isn't
                    self.stuck_detection_count = 0
                elif current_empty_dirs_deleted == self.last_empty_dirs_deleted:
                    # Actual deletion phase with no progress
                    self.stuck_detection_count += 1

                    if self.stuck_detection_count >= 2:
                        # Force GC during stalls to prevent memory creep (Issue 4)
                        gc.collect()
                        log_with_context(
                            self.logger,
                            "warning",
                            "POSSIBLE HANG DETECTED during empty directory removal: No progress in last "
                            f"{self.stuck_detection_count * self.progress_interval} seconds",
                            {
                                "phase": "removing_empty_dirs",
                                "empty_dirs_deleted": current_empty_dirs_deleted,
                                "empty_dirs_to_delete": self.stats.get("empty_dirs_to_delete", 0),
                                "stuck_intervals": self.stuck_detection_count,
                                "hint": "Large number of empty directories can take time. "
                                "If this persists, the filesystem may be slow or unresponsive.",
                            },
                        )
                else:
                    # Progress was made, reset stuck counter
                    self.stuck_detection_count = 0

                self.last_empty_dirs_deleted = current_empty_dirs_deleted

            else:
                # During scanning phase
                if current_files == self.last_files_scanned and current_dirs == self.last_dirs_scanned:
                    self.stuck_detection_count += 1

                    # After 2 consecutive checks with no progress (60+ seconds), warn user
                    if self.stuck_detection_count >= 2:
                        # Force GC during stalls to prevent memory creep (Issue 4)
                        gc.collect()
                        async with self.active_directories_lock:
                            active_dirs_copy = list(self.active_directories)

                        # Log warning with diagnostic information
                        log_with_context(
                            self.logger,
                            "warning",
                            "POSSIBLE HANG DETECTED: No progress in last "
                            f"{self.stuck_detection_count * self.progress_interval} seconds",
                            {
                                "phase": "scanning",
                                "files_scanned": current_files,
                                "dirs_scanned": current_dirs,
                                "active_directories_count": len(active_dirs_copy),
                                "stuck_intervals": self.stuck_detection_count,
                            },
                        )

                        # Log the directories currently being scanned (likely culprits)
                        if active_dirs_copy:
                            # Show up to 10 directories being scanned
                            dirs_to_show = active_dirs_copy[:10]
                            log_with_context(
                                self.logger,
                                "warning",
                                "Directories currently being scanned (potential hang location)",
                                {
                                    "directories": [str(d) for d in dirs_to_show],
                                    "total_active": len(active_dirs_copy),
                                    "hint": "If this persists, the filesystem may be unresponsive. "
                                    "Consider excluding problematic paths or checking NFS/EFS health.",
                                },
                            )
                else:
                    # Progress was made, reset stuck counter
                    self.stuck_detection_count = 0

                # Update last known values for next comparison
                self.last_files_scanned = current_files
                self.last_dirs_scanned = current_dirs

    async def purge(self) -> dict:
        """
        Main purge operation - scan and clean the file system.

        Returns:
            Dictionary with operation statistics
        """
        start_time = time.time()
        mode = "DRY RUN" if self.dry_run else "PURGE"

        # Detect event loop type for startup log
        loop = asyncio.get_running_loop()
        event_loop_type = type(loop).__module__ + "." + type(loop).__name__

        log_with_context(
            self.logger,
            "info",
            f"Starting EFS purge - {mode} MODE",
            {
                "version": __version__,
                "root_path": str(self.root_path),
                "max_age_days": self.max_age_days,
                "cutoff_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.cutoff_time)),
                "max_concurrency_scanning": self.max_concurrency_scanning,
                "max_concurrency_deletion": self.max_concurrency_deletion,
                "max_concurrency": self.max_concurrency,  # For backward compatibility
                "dry_run": self.dry_run,
                "progress_interval_seconds": self.progress_interval,
                "memory_limit_mb": self.memory_limit_mb,
                "task_batch_size": self.task_batch_size,
                "remove_empty_dirs": self.remove_empty_dirs,
                "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                "max_concurrent_discovery": self.max_concurrent_discovery,
                "queue_maxsize": self.queue_maxsize,
                "max_entries_per_dir": self.max_entries_per_dir,
                "scandir_executor_threads": self.scandir_executor._max_workers,
                "event_loop": event_loop_type,
            },
        )

        # Verify root path exists
        if not await aiofiles.os.path.exists(self.root_path):
            error_msg = f"Root path does not exist: {self.root_path}"
            log_with_context(self.logger, "error", error_msg, {"root_path": str(self.root_path)})
            raise FileNotFoundError(error_msg)

        # Start background progress reporter
        progress_task = asyncio.create_task(self._background_progress_reporter())

        try:
            if self.phase3_only:
                # Phase 3 standalone: drain the empty-dirs sidecar written by a
                # prior Phase 2 run WITHOUT re-running Phases 1 or 2.  Used to
                # realize accumulated empty-dir cleanup mid-scan (e.g. before a
                # sharded re-scan).  We deliberately preserve the pending_dirs
                # sidecar and the main checkpoint file so a subsequent
                # --resume continues Phase 2 where it left off.
                log_with_context(
                    self.logger,
                    "info",
                    "Phase 3 standalone: draining empty-dirs sidecar only",
                    {
                        "checkpoint_file": str(self.checkpoint_file) if self.checkpoint_file else None,
                        "batch_size": self.phase3_batch_size,
                    },
                )
                # Reset the memory-critical flag so we can detect an abort
                # that fires DURING this drain (as opposed to leftover state
                # from an earlier run).
                self._checkpoint_requested = False
                if self.phase3_batch_size > 0:
                    completed_ok = await self._drain_empty_dirs_sidecar_iterative(self.phase3_batch_size)
                else:
                    await self._remove_empty_directories()
                    completed_ok = not self._checkpoint_requested
                # Sidecar removal is CONDITIONAL on the drain actually
                # completing.  Removing it when the run aborted early would
                # silently lose the unprocessed candidates (bug fixed in
                # 2.3.0: the empty-dirs sidecar was being removed regardless
                # of whether _remove_empty_directories() actually finished).
                if completed_ok and self.checkpoint_file is not None:
                    remove_empty_dirs_sidecar(self.checkpoint_file)
                    log_with_context(
                        self.logger,
                        "info",
                        "Phase 3 standalone: sidecar drained and removed",
                        {"checkpoint_file": str(self.checkpoint_file)},
                    )
                elif not completed_ok:
                    log_with_context(
                        self.logger,
                        "warning",
                        "Phase 3 standalone: aborted early, sidecar preserved for retry",
                        {
                            "checkpoint_file": str(self.checkpoint_file) if self.checkpoint_file else None,
                            "hint": "Increase --phase3-batch-size headroom, raise the container memory limit, "
                            "or drop --phase3-batch-size to 0 to retry with load-all mode.",
                        },
                    )
            else:
                # Phase 1: Remove empty directories FIRST (standalone, efficient walker)
                # Skip when resuming - we already ran Phase 1 before the checkpoint.
                if self.remove_empty_dirs and not self.resume:
                    await self._purge_empty_directories_standalone()

                if not self.phase1_only:
                    # Phase 2: Scan and purge files (BFS queue + worker pool)
                    self.current_phase = "scanning"
                    self.rate_tracker.set_phase_start("scanning")
                    await self._scan_and_purge_files()

                    # Mark scanning phase as complete (for accurate overall rate calculation)
                    self.scanning_end_time = time.time()

                    # Phase 3: Post-scan empty directory cleanup
                    # After purging files, some directories may have become empty.
                    # Run the existing post-order deletion to catch these.
                    if self.remove_empty_dirs:
                        await self._remove_empty_directories()

            # Purge completed successfully - remove checkpoint file so a future
            # run with --resume won't mistakenly resume from stale state.
            # Phase-3-only mode deliberately preserves the checkpoint + pending
            # sidecar (Phase 2 progress is intentionally not touched — a later
            # --resume must still find it).  The empty-dirs sidecar was already
            # consumed and removed inside the phase3-only branch above.
            if not self.phase3_only:
                if self.checkpoint_file and self.checkpoint_file.exists():
                    try:
                        self.checkpoint_file.unlink()
                        log_with_context(
                            self.logger,
                            "info",
                            "Checkpoint file removed after successful purge",
                            {"checkpoint_file": str(self.checkpoint_file)},
                        )
                    except OSError as e:
                        log_with_context(
                            self.logger,
                            "warning",
                            "Could not remove checkpoint file after successful purge",
                            {"checkpoint_file": str(self.checkpoint_file), "error": str(e)},
                        )
                # Also remove the empty-dirs and pending-dirs sidecars (written
                # next to the checkpoint to keep carry-over off the Phase 2
                # heap).  Best-effort: a stale sidecar with no matching
                # checkpoint is harmless because the next purge starts fresh.
                if self.checkpoint_file is not None:
                    remove_empty_dirs_sidecar(self.checkpoint_file)
                    remove_pending_dirs_sidecar(self.checkpoint_file)
        finally:
            # Cancel background reporter
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass  # Expected

            # Log final diagnostics if DEBUG is enabled
            if self.logger.isEnabledFor(logging.DEBUG) and self.scandir_call_count > 0:
                await _log_scandir_diagnostics(self, self.scandir_executor)

            # Shutdown custom executor for directory scanning
            self.close()

        # Log one final progress update if we haven't logged recently
        elapsed = time.time() - self.stats.get("start_time", time.time())
        if elapsed > self.progress_interval and (time.time() - self.last_progress_log) > 10:
            # Force a final progress update
            # Use scanning duration for rate calculation (excludes empty dir removal time)
            if self.scanning_end_time is not None:
                scanning_duration = self.scanning_end_time - self.stats.get("start_time", time.time())
                rate = self.stats["files_scanned"] / scanning_duration if scanning_duration > 0 else 0
            else:
                rate = self.stats["files_scanned"] / elapsed if elapsed > 0 else 0

            memory_mb = get_memory_usage_mb()
            is_debug = self.logger.isEnabledFor(10)  # 10 = DEBUG level

            final_progress_data = {
                # Core metrics in requested order
                "elapsed_seconds": round(elapsed, 1),
                "files_scanned": self.stats["files_scanned"],
                "files_purged": self.stats["files_purged"],
                "dirs_scanned": self.stats["dirs_scanned"],
                "errors": self.stats["errors"],
                "memory_backpressure_events": self.stats.get("memory_backpressure_events", 0),
            }

            # Add dirs purged if any were deleted
            if self.stats.get("empty_dirs_deleted", 0) > 0:
                final_progress_data["dirs_purged"] = self.stats.get("empty_dirs_deleted", 0)

            # Add files/dirs to purge if non-zero
            if self.stats["files_to_purge"] > 0:
                final_progress_data["files_to_purge"] = self.stats["files_to_purge"]
            if self.stats.get("empty_dirs_to_delete", 0) > 0:
                final_progress_data["dirs_to_purge"] = self.stats.get("empty_dirs_to_delete", 0)

            # Rates and memory
            final_progress_data["files_per_second"] = round(rate, 1)
            final_progress_data["memory_mb"] = round(memory_mb, 1)

            log_with_context(
                self.logger,
                "info",
                "Final progress before completion",
                final_progress_data,
            )

        # Calculate final statistics
        duration = time.time() - start_time
        # Use scanning duration for files_per_second (excludes empty dir removal time)
        if self.scanning_end_time is not None:
            scanning_duration = self.scanning_end_time - start_time
            files_per_sec = self.stats["files_scanned"] / scanning_duration if scanning_duration > 0 else 0
        else:
            files_per_sec = self.stats["files_scanned"] / duration if duration > 0 else 0
        mb_freed = self.stats["bytes_freed"] / (1024 * 1024)
        memory_mb = get_memory_usage_mb()
        is_debug = self.logger.isEnabledFor(10)  # 10 = DEBUG level

        # Build final stats with reordered fields (most important first)
        final_stats = {
            # Core metrics in requested order
            "duration_seconds": round(duration, 2),
            "files_scanned": self.stats["files_scanned"],
            "files_purged": self.stats["files_purged"],
            "dirs_scanned": self.stats["dirs_scanned"],
            "errors": self.stats["errors"],
            "memory_backpressure_events": self.stats.get("memory_backpressure_events", 0),
        }

        # Add dirs purged if any were deleted
        if self.stats.get("empty_dirs_deleted", 0) > 0:
            final_stats["dirs_purged"] = self.stats.get("empty_dirs_deleted", 0)

        # Add files/dirs to purge if non-zero
        if self.stats["files_to_purge"] > 0:
            final_stats["files_to_purge"] = self.stats["files_to_purge"]
        if self.stats.get("empty_dirs_to_delete", 0) > 0:
            final_stats["dirs_to_purge"] = self.stats.get("empty_dirs_to_delete", 0)

        # Rates and memory
        final_stats["files_per_second"] = round(files_per_sec, 2)
        final_stats["mb_freed"] = round(mb_freed, 2)
        final_stats["peak_memory_mb"] = round(memory_mb, 1)

        # DEBUG-only: include all stats for detailed analysis
        if is_debug:
            final_stats.update(
                {
                    "symlinks_skipped": self.stats.get("symlinks_skipped", 0),
                    "special_files_skipped": self.stats.get("special_files_skipped", 0),
                    "bytes_freed": self.stats.get("bytes_freed", 0),
                    "start_time": self.stats.get("start_time"),
                }
            )

        log_with_context(
            self.logger,
            "info",
            "Purge operation completed",
            final_stats,
        )

        return final_stats


async def async_main(
    path: str,
    max_age_days: float,
    max_concurrency: int | None = None,
    max_concurrency_scanning: int | None = None,
    max_concurrency_deletion: int | None = None,
    dry_run: bool = True,
    log_level: str = "INFO",
    memory_limit_mb: int = 800,
    task_batch_size: int = 5000,
    remove_empty_dirs: bool = False,
    max_empty_dirs_to_delete: int = 500,
    max_discovery_dirs: int = 0,
    max_concurrent_discovery: int = 20,
    queue_maxsize: int = 10000,
    max_entries_per_dir: int = 0,
    checkpoint_file: str | Path | None = None,
    resume: bool = False,
    dir_deletion_checkpoint_file: str | Path | None = None,
    dir_deletion_resume: bool = False,
    phase1_only: bool = False,
    phase3_only: bool = False,
    phase3_batch_size: int = 0,
    phase3_deletion_workers: int = 0,
    backpressure_checkpoint_timeout: int = 600,
) -> dict:
    """
    Async entry point for the purger.

    Args:
        path: Root path to purge
        max_age_days: Maximum age of files in days
        max_concurrency: Maximum concurrent operations (deprecated, use max_concurrency_scanning/deletion)
        max_concurrency_scanning: Maximum concurrent file scanning operations (default: 1000)
        max_concurrency_deletion: Maximum concurrent file deletion operations (default: 1000)
        dry_run: If True, don't actually delete files
        log_level: Logging level
        memory_limit_mb: Soft memory limit in MB (0 = no limit)
        task_batch_size: Maximum tasks to create at once
        remove_empty_dirs: If True, remove empty directories after scanning
        max_empty_dirs_to_delete: Maximum empty directories to delete per run (0 = unlimited, default: 500)
        max_discovery_dirs: Maximum directories to discover in Phase 1a (0 = auto based on memory)
        max_concurrent_discovery: Maximum concurrent directory/file scan workers (default: 20)
        queue_maxsize: Maximum size of Phase 1a and Phase 2 directory queues (0 = unbounded, default: 10000)
        max_entries_per_dir: Cap entries per directory in Phase 1a (0 = no limit) to avoid one huge dir stalling workers
        checkpoint_file: Path to save checkpoint when memory critical (enables auto-checkpoint)
        resume: If True, load checkpoint and resume Phase 2 from saved state
        dir_deletion_checkpoint_file: Path to save/load Phase 1a BFS frontier checkpoint on memory abort
        dir_deletion_resume: If True, resume Phase 1a discovery from dir_deletion_checkpoint_file
        backpressure_checkpoint_timeout: Seconds of sustained back-pressure before forcing checkpoint exit
            (default: 600)

    Returns:
        Operation statistics
    """
    purger = AsyncEFSPurger(
        root_path=path,
        max_age_days=max_age_days,
        max_concurrency=max_concurrency,
        max_concurrency_scanning=max_concurrency_scanning,
        max_concurrency_deletion=max_concurrency_deletion,
        dry_run=dry_run,
        log_level=log_level,
        memory_limit_mb=memory_limit_mb,
        task_batch_size=task_batch_size,
        remove_empty_dirs=remove_empty_dirs,
        max_empty_dirs_to_delete=max_empty_dirs_to_delete,
        max_discovery_dirs=max_discovery_dirs,
        max_concurrent_discovery=max_concurrent_discovery,
        queue_maxsize=queue_maxsize,
        max_entries_per_dir=max_entries_per_dir,
        checkpoint_file=checkpoint_file,
        resume=resume,
        dir_deletion_checkpoint_file=dir_deletion_checkpoint_file,
        dir_deletion_resume=dir_deletion_resume,
        phase1_only=phase1_only,
        phase3_only=phase3_only,
        phase3_batch_size=phase3_batch_size,
        phase3_deletion_workers=phase3_deletion_workers,
        backpressure_checkpoint_timeout=backpressure_checkpoint_timeout,
    )

    return await purger.purge()
