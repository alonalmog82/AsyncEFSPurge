"""Async file purger optimized for AWS EFS and network storage."""

import asyncio
import gc
import logging
import os
import queue
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiofiles.os

from . import __version__
from .logging import log_with_context, setup_logging

# Maximum number of directories to discover in Phase 1a.
# This caps the memory footprint of dirs_by_depth.  Each Path object
# is ~400-600 bytes, so 1M dirs ≈ 400-600 MB.  Setting this to 1M
# keeps discovery memory well within a 4.5 GB container budget while
# still covering the vast majority of real-world directory trees.
MAX_DISCOVERY_DIRS_DEFAULT = 1_000_000


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
        max_concurrent_subdirs: int = 100,
        max_discovery_dirs: int = 0,
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
            max_concurrent_subdirs: Maximum subdirectories to scan concurrently (lower = less memory, default: 100)
            max_discovery_dirs: Maximum directories to discover in Phase 1a (0 = use automatic limit based on memory)

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

        if max_concurrent_subdirs < 1:
            raise ValueError(f"max_concurrent_subdirs must be >= 1, got {max_concurrent_subdirs}")

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
        self.max_concurrent_subdirs = max_concurrent_subdirs

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
        # Semaphore for subdirectory scanning to maintain constant concurrency
        self.subdir_semaphore = asyncio.Semaphore(max_concurrent_subdirs)
        self.stats_lock = asyncio.Lock()

        # Note: Incremental batch processing during scanning has been removed.
        # Empty dirs are now handled in a standalone phase (_purge_empty_directories_standalone).

        # Custom ThreadPoolExecutor for directory scanning to bypass default thread pool limit
        # Default executor has ~32 threads, limiting directory scanning throughput to ~250-300 dirs/sec
        # Custom executor allows scaling to 200-500 threads for 2-5x improvement
        # Thread count scales with max_concurrent_subdirs but is capped to avoid excessive overhead
        if max_concurrent_subdirs >= 1000:
            scandir_threads = min(500, max(200, max_concurrent_subdirs // 10))
        elif max_concurrent_subdirs >= 500:
            scandir_threads = min(300, max(150, max_concurrent_subdirs // 8))
        else:
            scandir_threads = min(200, max(100, max_concurrent_subdirs // 5))

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

    def close(self) -> None:
        """Shut down the scandir ThreadPoolExecutor.

        Call this when you're done with the purger to release threads.
        Also called automatically by ``async with AsyncEFSPurger(...)``.
        The ``purge()`` method calls this internally, so you only need to
        call it when using lower-level methods like
        ``_purge_empty_directories_standalone()`` or ``scan_directory()``
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

                # Only log warning once per interval to avoid spam
                if current_time - self.last_memory_warning >= self.memory_warning_interval:
                    memory_percent = (memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0
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

            return False, memory_mb  # Memory is OK, but return value for proactive reduction

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

    async def _remove_empty_directories(self) -> None:
        """
        Phase 3: Remove directories that became empty after file purging.

        This runs AFTER file scanning/purging to catch directories that were
        non-empty before (had old files) but became empty after those files were purged.

        Uses post-order deletion (children before parents) with cascading parent checks.
        Concurrent processing with deletion_semaphore for high throughput.
        """
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
                        await aiofiles.os.rmdir(directory)
                    # Counter already incremented above, just update deleted count
                    await self.update_stats(empty_dirs_deleted=1)
                    # Record sample for rate tracking
                    self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
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

        # Start workers (number limited by semaphore - workers wait for semaphore slots)
        # Number of workers = semaphore limit ensures we use all available concurrency
        # Memory bounded by: num_workers * memory_per_task = semaphore_limit * memory_per_task
        num_workers = self.max_concurrency_deletion
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
                            await aiofiles.os.rmdir(parent)
                        await self.update_stats(empty_dirs_to_delete=1, empty_dirs_deleted=1)
                        # Record sample for rate tracking
                        self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
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

            # Start workers (number limited by semaphore)
            num_workers = self.max_concurrency_deletion
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
                "max_concurrent_subdirs": self.max_concurrent_subdirs,
                "max_concurrency_deletion": self.max_concurrency_deletion,
                "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                "memory_limit_mb": self.memory_limit_mb,
                "max_discovery_dirs": self.max_discovery_dirs,
            },
        )

        # Phase A: Discover all directories using iterative BFS, bucketed by depth.
        # Each directory is placed into a bucket keyed by its depth (number of path
        # components). This avoids a costly sort later and enables level-by-level
        # freeing during deletion.
        dirs_by_depth: defaultdict[int, list[Path]] = defaultdict(list)
        dirs_to_visit: deque[Path] = deque([self.root_path])
        discovery_errors = 0
        total_dirs_discovered = 0
        root_depth = len(self.root_path.parts)

        log_with_context(
            self.logger,
            "info",
            "Phase 1a: Discovering directory tree structure",
            {
                "root_path": str(self.root_path),
                "memory_limit_mb": self.memory_limit_mb,
                "max_discovery_dirs": self.max_discovery_dirs,
                "initial_memory_mb": round(get_memory_usage_mb(), 1),
            },
        )

        self._discovery_active = True
        self._discovery_dirs_found = 0
        self._discovery_current_dir = str(self.root_path)
        self._discovery_queue_size = len(dirs_to_visit)
        self._discovery_entries_scanned = 0

        memory_abort = False
        discovery_limit_reached = False
        while dirs_to_visit and not memory_abort and not discovery_limit_reached:
            # Check directory count limit
            if self.max_discovery_dirs > 0 and total_dirs_discovered >= self.max_discovery_dirs:
                log_with_context(
                    self.logger,
                    "info",
                    "Discovery directory count limit reached, proceeding with partial tree",
                    {
                        "max_discovery_dirs": self.max_discovery_dirs,
                        "dirs_discovered": total_dirs_discovered,
                        "dirs_remaining_in_queue": len(dirs_to_visit),
                        "memory_mb": round(get_memory_usage_mb(), 1),
                    },
                )
                discovery_limit_reached = True
                break

            # Check memory pressure during discovery
            if self.memory_limit_mb > 0:
                memory_mb = get_memory_usage_mb()
                memory_percent = memory_mb / self.memory_limit_mb
                if memory_percent > 0.90:
                    # At 90%+, pause discovery briefly and GC
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
                                "dirs_discovered": total_dirs_discovered,
                                "dirs_remaining_in_queue": len(dirs_to_visit),
                                "memory_mb": round(memory_mb, 1),
                                "memory_percent": round(memory_percent * 100, 1),
                            },
                        )
                        break

            current_dir = dirs_to_visit.popleft()
            self._discovery_current_dir = str(current_dir)
            self._discovery_dirs_found = total_dirs_discovered
            self._discovery_queue_size = len(dirs_to_visit)

            try:
                # Use batched scandir to avoid blocking the event loop for a long
                # time when a single directory has 100K+ entries (common on EFS).
                # Between batches we can check memory and abort early.
                subdirs_added = 0
                batches_processed = 0
                entries_in_dir = 0
                async for batch in async_scandir_batched(current_dir, self.scandir_executor):
                    batches_processed += 1
                    entries_in_dir += len(batch)
                    self._discovery_entries_scanned += len(batch)
                    for entry in batch:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                entry_path = Path(entry.path)
                                depth = len(entry_path.parts) - root_depth
                                dirs_by_depth[depth].append(entry_path)
                                dirs_to_visit.append(entry_path)
                                total_dirs_discovered += 1
                                subdirs_added += 1
                        except OSError:
                            discovery_errors += 1

                    # Update discovery state for progress monitor visibility
                    self._discovery_dirs_found = total_dirs_discovered
                    self._discovery_queue_size = len(dirs_to_visit)

                    # Check memory between batches unconditionally (every 10 batches
                    # = every ~50,000 entries to limit overhead). This catches memory
                    # growth even in flat directories with no subdirectories, which
                    # was previously missed when the check required subdirs_added > 0.
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
                                        "current_dir": str(current_dir),
                                        "entries_scanned_in_dir": entries_in_dir,
                                        "subdirs_in_this_dir": subdirs_added,
                                        "dirs_discovered": total_dirs_discovered,
                                        "dirs_remaining_in_queue": len(dirs_to_visit),
                                        "memory_mb": round(memory_mb, 1),
                                        "memory_percent": round(memory_percent * 100, 1),
                                    },
                                )
                                memory_abort = True
                                break

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

            # Log progress periodically during discovery
            if total_dirs_discovered % 50000 == 0 and total_dirs_discovered > 0:
                memory_mb = get_memory_usage_mb() if self.memory_limit_mb > 0 else 0
                memory_percent = (memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0
                log_with_context(
                    self.logger,
                    "info",
                    "Directory discovery progress",
                    {
                        "dirs_discovered": total_dirs_discovered,
                        "dirs_remaining_in_queue": len(dirs_to_visit),
                        "depth_levels": len(dirs_by_depth),
                        "memory_mb": round(memory_mb, 1),
                        "memory_percent": round(memory_percent, 1),
                        "memory_limit_mb": self.memory_limit_mb,
                        "discovery_errors": discovery_errors,
                    },
                )

        # Discovery complete - clear state so progress monitor stops reporting discovery
        self._discovery_active = False
        self._discovery_current_dir = None

        # Free the BFS queue now that discovery is complete
        del dirs_to_visit

        max_depth = max(dirs_by_depth.keys()) if dirs_by_depth else 0

        log_with_context(
            self.logger,
            "info",
            "Phase 1a complete: Directory tree discovered",
            {
                "total_dirs_discovered": total_dirs_discovered,
                "depth_levels": len(dirs_by_depth),
                "max_depth": max_depth,
                "discovery_errors": discovery_errors,
                "memory_mb": round(get_memory_usage_mb(), 1),
            },
        )

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
                        await aiofiles.os.rmdir(directory)
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
                            # Still critical after GC - abort remaining deletion
                            log_with_context(
                                self.logger,
                                "warning",
                                "Memory critical during deletion, stopping to prevent OOM",
                                {
                                    "memory_mb": round(memory_mb, 1),
                                    "memory_percent": round(memory_percent * 100, 1),
                                    "memory_limit_mb": self.memory_limit_mb,
                                    "deleted": deleted_count,
                                    "checked": checked_count,
                                    "current_depth": current_depth,
                                    "remaining_in_level": level_size - i,
                                    "remaining_levels": len(dirs_by_depth),
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
                tasks = [asyncio.create_task(check_and_delete_if_empty(d)) for d in batch]
                await asyncio.gather(*tasks, return_exceptions=True)

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

        # Free any remaining depth levels (e.g. if rate limit stopped us early)
        dirs_by_depth.clear()
        del dirs_by_depth

        # Force GC after releasing all directory data
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
            },
        )

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

    async def _process_subdirs_with_constant_concurrency(self, subdirs: list[Path]) -> None:
        """
        Process subdirectories with constant concurrency using a hybrid approach.

        This method maintains high concurrency utilization while preventing memory explosion:
        - Uses semaphore to limit concurrent execution (maintains constant concurrency)
        - Creates tasks on-demand as slots become available (prevents memory explosion)
        - As tasks complete, new ones start immediately (high utilization)

        Key benefits:
        - Never creates more than max_concurrent_subdirs tasks at once
        - Maintains constant concurrency (no idle slots waiting for slow directories)
        - Prevents recursive memory explosion in deep directory trees

        IMPORTANT: Before modifying this method or scan_directory's subdirectory processing,
        test with 80×80×80 directory structure (518,481 dirs) to ensure no deadlock or
        memory issues. See test_deep_directory_tree_memory_safety for details.

        Args:
            subdirs: List of subdirectory paths to process
        """
        if not subdirs:
            return

        # Use a queue to track remaining subdirectories
        remaining_subdirs = list(subdirs)
        active_tasks: list[asyncio.Task] = []

        async def scan_with_semaphore(subdir: Path) -> None:
            """Scan a subdirectory with semaphore control."""
            async with self.subdir_semaphore:
                await self.scan_directory(subdir)

        # Process subdirectories maintaining constant concurrency
        # We create tasks on-demand as slots become available, never exceeding max_concurrent_subdirs
        iterations = 0
        while remaining_subdirs or active_tasks:
            iterations += 1

            # Start new tasks up to the concurrency limit
            # The semaphore ensures only max_concurrent_subdirs run concurrently,
            # but we can have a few more tasks waiting (bounded by max_concurrent_subdirs)
            while len(active_tasks) < self.max_concurrent_subdirs and remaining_subdirs:
                subdir = remaining_subdirs.pop(0)
                task = asyncio.create_task(scan_with_semaphore(subdir))
                active_tasks.append(task)

            # Wait for at least one task to complete before starting more
            # This ensures we maintain constant concurrency without creating all tasks upfront
            if active_tasks:
                done, pending = await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)

                # Remove completed tasks and check for exceptions
                for task in done:
                    active_tasks.remove(task)
                    # Check for exceptions (scan_directory handles its own, but log unexpected ones)
                    try:
                        await task
                    except Exception as e:
                        # scan_directory should handle all exceptions, but log unexpected ones
                        log_with_context(
                            self.logger,
                            "error",
                            "Unexpected exception in subdirectory scan",
                            {"error": str(e), "error_type": type(e).__name__},
                        )

            # Debug: Log if we're stuck in a loop (shouldn't happen, but helps diagnose)
            if iterations > 10000:
                self.logger.warning(
                    f"Warning: _process_subdirs_with_constant_concurrency has run {iterations} iterations. "
                    f"Remaining subdirs: {len(remaining_subdirs)}, Active tasks: {len(active_tasks)}"
                )
                break

    async def scan_directory(self, directory: Path) -> None:
        """
        Recursively scan a directory and process files using TRUE STREAMING.

        This implementation uses a sliding window approach:
        - Accumulates files into a buffer
        - Processes and frees buffer when it reaches batch_size
        - Never holds all files in memory at once
        - Much lower memory footprint

        Args:
            directory: Directory path to scan
        """
        # Track this directory as actively being scanned (for stuck detection diagnostics)
        async with self.active_directories_lock:
            self.active_directories.add(directory)

        try:
            await self.update_stats(dirs_scanned=1)
            # Record sample for rate tracking
            self.rate_tracker.record(self.current_phase, "dirs", 1)

            # Scan directory entries
            entries = await async_scandir(directory, self.scandir_executor, self)

            # STREAMING: Use buffer instead of accumulating all tasks
            file_task_buffer = []
            subdirs = []

            for entry in entries:
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
                        # Skip file processing entirely when max_age_days=0 (empty dir deletion only)
                        # This avoids expensive os.stat() calls when we only want to delete empty directories
                        if self.max_age_days > 0:
                            file_task_buffer.append(self.process_file(entry_path))

                            # STREAMING: Process and clear buffer when it reaches batch size
                            if len(file_task_buffer) >= self.task_batch_size:
                                try:
                                    await self._process_file_batch(file_task_buffer)
                                finally:
                                    file_task_buffer.clear()  # Always clear, even on exception
                        # else: max_age_days == 0, skip file processing (no os.stat calls)

                    elif entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry_path)

                    else:
                        # Special file types: sockets, FIFOs, block/char devices, etc.
                        # These are skipped and counted separately
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

            # STREAMING: Process any remaining files in buffer
            if file_task_buffer:
                try:
                    await self._process_file_batch(file_task_buffer)
                finally:
                    file_task_buffer.clear()  # Always clear, even on exception

            # Process subdirectories using hybrid approach:
            # - Semaphore maintains constant concurrency (prevents idle slots)
            # - Tasks created in batches (prevents memory explosion)
            # - As tasks complete, new ones start immediately (high utilization)
            # Note: If we're already holding the semaphore (recursive call), process directly
            # to avoid deadlock. Otherwise use the semaphore-controlled approach.
            if subdirs:
                await self.check_memory_pressure()  # Ignore return value for subdir processing
                # Check if semaphore is available (not held by current task)
                # If semaphore value equals limit, we're not holding it
                if self.subdir_semaphore._value == self.max_concurrent_subdirs:
                    # Not holding semaphore - use controlled concurrency
                    await self._process_subdirs_with_constant_concurrency(subdirs)
                else:
                    # Already holding semaphore (recursive call) - process directly without semaphore
                    # to avoid deadlock. Process sequentially to avoid creating too many tasks.
                    for subdir in subdirs:
                        await self.scan_directory(subdir)

            # Phase 3 prep: After all subdirs processed, check if this directory is now empty
            # (it may have become empty because we purged all its files in this scan).
            # Just add to the set - actual deletion happens in _remove_empty_directories().
            if self.remove_empty_dirs and self.max_age_days > 0:
                try:
                    post_entries = await async_scandir(directory, self.scandir_executor, self)
                    if len(post_entries) == 0:
                        try:
                            dir_resolved = directory.resolve()
                            root_resolved = self.root_path.resolve()
                        except (OSError, RuntimeError):
                            dir_resolved = directory
                            root_resolved = self.root_path
                        if dir_resolved != root_resolved:
                            self.empty_dirs.add(directory)
                except (FileNotFoundError, PermissionError, OSError):
                    pass  # Directory gone or inaccessible

        except PermissionError as e:
            log_with_context(
                self.logger,
                "warning",
                "Permission denied for directory",
                {"directory": str(directory), "error": str(e)},
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
            # Remove from active directories when done (success or failure)
            async with self.active_directories_lock:
                self.active_directories.discard(directory)

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
                "max_concurrent_subdirs": self.max_concurrent_subdirs,
                "remove_empty_dirs": self.remove_empty_dirs,
                "max_empty_dirs_to_delete": self.max_empty_dirs_to_delete,
                "scandir_executor_threads": self.scandir_executor._max_workers,
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
            # Phase 1: Remove empty directories FIRST (standalone, efficient walker)
            # This runs before file scanning to reduce the directory tree size,
            # making subsequent file scanning faster and lighter on memory.
            if self.remove_empty_dirs:
                await self._purge_empty_directories_standalone()

            # Phase 2: Scan and purge files
            self.current_phase = "scanning"
            self.rate_tracker.set_phase_start("scanning")
            await self.scan_directory(self.root_path)

            # Mark scanning phase as complete (for accurate overall rate calculation)
            self.scanning_end_time = time.time()

            # Phase 3: Post-scan empty directory cleanup
            # After purging files, some directories may have become empty.
            # Run the existing post-order deletion to catch these.
            if self.remove_empty_dirs:
                await self._remove_empty_directories()
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
    max_concurrent_subdirs: int = 100,
    max_discovery_dirs: int = 0,
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
        max_concurrent_subdirs: Maximum subdirectories to scan concurrently (lower = less memory, default: 100)
        max_discovery_dirs: Maximum directories to discover in Phase 1a (0 = auto based on memory)

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
        max_concurrent_subdirs=max_concurrent_subdirs,
        max_discovery_dirs=max_discovery_dirs,
    )

    return await purger.purge()
