"""Async file purger optimized for AWS EFS and network storage."""

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path

import aiofiles.os

from . import __version__
from .logging import log_with_context, setup_logging


def get_memory_usage_mb() -> float:
    """Get current memory usage in MB."""
    try:
        import psutil

        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024  # Convert bytes to MB
    except ImportError:
        # If psutil not available, try alternative method
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB to MB on Linux
        except Exception:
            return 0.0  # Return 0 if we can't measure


async def async_scandir(path: Path):
    """Async wrapper for os.scandir."""
    loop = asyncio.get_running_loop()

    def _scandir():
        with os.scandir(path) as entries:
            return list(entries)

    return await loop.run_in_executor(None, _scandir)


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

        # Track directories currently being scanned (for diagnostics when stuck)
        self.active_directories: set[Path] = set()
        self.active_directories_lock = asyncio.Lock()

        # Track current phase for better progress reporting
        self.current_phase = "initializing"  # "scanning", "removing_empty_dirs", "completed"

        # Track scanning phase duration for accurate overall rate calculation
        self.scanning_end_time: float | None = None

        # Rate tracking for enhanced metrics
        self.rate_tracker = RateTracker()

        # Track empty directories for post-order deletion
        # Use set to prevent duplicates from concurrent scans
        self.empty_dirs: set[Path] = set()

        # Concurrency control - separate semaphores for scanning and deletion
        self.scanning_semaphore = asyncio.Semaphore(max_concurrency_scanning)
        self.deletion_semaphore = asyncio.Semaphore(max_concurrency_deletion)
        # Semaphore for subdirectory scanning to maintain constant concurrency
        self.subdir_semaphore = asyncio.Semaphore(max_concurrent_subdirs)
        self.stats_lock = asyncio.Lock()

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

    async def update_stats(self, **kwargs) -> None:
        """Thread-safe update of statistics."""
        async with self.stats_lock:
            for key, value in kwargs.items():
                if key in self.stats:
                    self.stats[key] += value

            # Progress logging is handled by _background_progress_reporter()
            # Removed duplicate logging here to prevent duplicate log entries

    async def check_memory_pressure(self) -> None:
        """
        Check if memory usage is high and apply back-pressure if needed.

        Uses a lock to prevent concurrent checks and rate-limits warning messages
        to avoid log spam.
        """
        if self.memory_limit_mb <= 0:
            return  # No limit set

        # Use lock to prevent multiple concurrent checks
        async with self.memory_check_lock:
            memory_mb = get_memory_usage_mb()
            if memory_mb > self.memory_limit_mb:
                current_time = time.time()

                # Only log warning once per interval to avoid spam
                if current_time - self.last_memory_warning >= self.memory_warning_interval:
                    self.logger.warning(
                        f"Memory usage ({memory_mb:.1f} MB) exceeds limit ({self.memory_limit_mb} MB), "
                        f"applying back-pressure (logged once per {self.memory_warning_interval}s to avoid spam)..."
                    )
                    self.last_memory_warning = current_time

                # Track back-pressure event
                await self.update_stats(memory_backpressure_events=1)

                # Apply actual back-pressure: pause briefly and force GC
                await asyncio.sleep(0.5)  # Shorter pause, but happens under lock

                # Force garbage collection
                import gc

                gc.collect()

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

    async def _check_empty_directory(self, directory: Path) -> None:
        """
        Check if directory is empty and add to deletion set if so.

        This is called after all subdirectories have been processed,
        so we can safely check if the directory is now empty.

        Args:
            directory: Directory path to check
        """
        # Normalize paths for comparison (handle symlinks, relative paths, etc.)
        try:
            dir_resolved = directory.resolve()
            root_resolved = self.root_path.resolve()
        except (OSError, RuntimeError):
            # If resolve fails (e.g., broken symlink), use original paths
            dir_resolved = directory
            root_resolved = self.root_path

        # Never delete root directory
        if dir_resolved == root_resolved:
            return

        # Lock entire check-and-add operation to prevent race conditions
        async with self.stats_lock:
            # Double-check directory is still empty (might have been populated)
            # This check happens under lock to prevent race conditions
            try:
                entries = await async_scandir(directory)
                if len(entries) == 0:
                    # Directory is empty, add to deletion set
                    # Set automatically prevents duplicates from concurrent scans
                    self.empty_dirs.add(directory)
                    self.logger.debug(f"Found empty directory: {directory}")
            except (FileNotFoundError, PermissionError):
                # Directory was deleted or permission denied - ignore
                pass
            except Exception as e:
                # Log but don't fail
                self.logger.debug(f"Error checking empty directory {directory}: {e}")

    async def _remove_empty_directories(self) -> None:
        """
        Remove empty directories in post-order (children before parents).

        This ensures we can delete nested empty directories correctly.
        After deleting a directory, we check if its parent is now empty.

        Uses a two-pass approach to avoid modifying list during iteration:
        1. First pass: Delete all directories in the initial set
        2. Second pass: Check parents and delete if they became empty
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

        processed_dirs = set()  # Track which dirs we've processed
        new_empty_parents = set()  # Track parents that become empty

        # First pass: Delete all initially empty directories
        for directory in sorted_dirs:
            if directory in processed_dirs:
                continue

            # Check rate limit (based on directories processed, not just deleted)
            if self.max_empty_dirs_to_delete > 0:
                async with self.stats_lock:
                    to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                if to_delete_count >= self.max_empty_dirs_to_delete:
                    # Count unprocessed directories in this batch
                    unprocessed_count = sum(1 for d in sorted_dirs if d not in processed_dirs)
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
                    break

            try:
                # Normalize directory path for comparison
                try:
                    dir_resolved = directory.resolve()
                except (OSError, RuntimeError):
                    dir_resolved = directory

                # Never delete root directory
                if dir_resolved == root_resolved:
                    processed_dirs.add(directory)
                    continue

                # Double-check directory is still empty (might have been populated)
                entries = await async_scandir(directory)
                if len(entries) > 0:
                    # Directory is no longer empty, skip it
                    processed_dirs.add(directory)
                    continue

                if not self.dry_run:
                    await aiofiles.os.rmdir(directory)
                    await self.update_stats(empty_dirs_to_delete=1, empty_dirs_deleted=1)
                    # Record sample for rate tracking
                    self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
                    self.logger.debug(f"Removed empty directory: {directory}")
                else:
                    await self.update_stats(empty_dirs_to_delete=1)
                    self.logger.debug(f"Would remove empty directory: {directory}")

                processed_dirs.add(directory)

                # After deleting a directory, check if its parent is now empty
                parent = directory.parent
                # Check parent is valid and not root
                if parent != directory and parent not in processed_dirs:
                    try:
                        parent_resolved = parent.resolve()
                        if parent_resolved != root_resolved:
                            # Check if parent is now empty
                            parent_entries = await async_scandir(parent)
                            if len(parent_entries) == 0:
                                # Parent is now empty, add to set for second pass
                                new_empty_parents.add(parent)
                    except (FileNotFoundError, PermissionError, OSError, RuntimeError):
                        pass  # Parent doesn't exist, no permission, or resolve failed

            except FileNotFoundError:
                # Directory was already deleted by another process
                processed_dirs.add(directory)
                self.logger.debug(f"Empty directory already deleted: {directory}")
            except OSError as e:
                # Directory might have been populated or permission denied
                processed_dirs.add(directory)
                log_with_context(
                    self.logger,
                    "warning",
                    "Could not remove empty directory",
                    {"directory": str(directory), "error": str(e)},
                )
                await self.update_stats(errors=1)

        # Log progress after first pass
        async with self.stats_lock:
            deleted_count = self.stats.get("empty_dirs_deleted", 0)
        log_with_context(
            self.logger,
            "info",
            "Empty directory removal progress",
            {"empty_dirs_deleted": deleted_count, "phase": "first_pass"},
        )

        # Log before second pass
        if new_empty_parents:
            log_with_context(
                self.logger,
                "info",
                "Starting cascading empty directory removal",
                {"parents_to_check": len(new_empty_parents)},
            )

        # Second pass: Process parents that became empty (cascading deletion)
        # Continue until no new empty parents are found
        iteration = 0
        while new_empty_parents:
            iteration += 1
            # Get next batch of parents to process
            parents_to_process = sorted(new_empty_parents, key=lambda p: len(p.parts), reverse=True)
            new_empty_parents = set()  # Reset for next iteration

            # Log progress every 100 iterations or every 1000 directories processed
            if iteration % 100 == 0 or len(parents_to_process) > 1000:
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

            for parent in parents_to_process:
                if parent in processed_dirs:
                    continue

                # Check rate limit (based on directories processed, not just deleted)
                if self.max_empty_dirs_to_delete > 0:
                    async with self.stats_lock:
                        to_delete_count = self.stats.get("empty_dirs_to_delete", 0)
                    if to_delete_count >= self.max_empty_dirs_to_delete:
                        # Count unprocessed parents in current batch
                        unprocessed_count = sum(1 for p in parents_to_process if p not in processed_dirs)
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
                        # Exit both loops
                        new_empty_parents = set()
                        break

                try:
                    # Normalize parent path
                    try:
                        parent_resolved = parent.resolve()
                    except (OSError, RuntimeError):
                        parent_resolved = parent

                    # Never delete root directory
                    if parent_resolved == root_resolved:
                        processed_dirs.add(parent)
                        continue

                    # Double-check parent is still empty
                    entries = await async_scandir(parent)
                    if len(entries) > 0:
                        # Parent is no longer empty, skip it
                        processed_dirs.add(parent)
                        continue

                    if not self.dry_run:
                        await aiofiles.os.rmdir(parent)
                        await self.update_stats(empty_dirs_to_delete=1, empty_dirs_deleted=1)
                        # Record sample for rate tracking
                        self.rate_tracker.record("removing_empty_dirs", "dirs", 1)
                        self.logger.debug(f"Removed empty parent directory: {parent}")
                    else:
                        await self.update_stats(empty_dirs_to_delete=1)
                        self.logger.debug(f"Would remove empty parent directory: {parent}")

                    processed_dirs.add(parent)

                    # Check if parent's parent is now empty (cascading)
                    grandparent = parent.parent
                    if grandparent != parent and grandparent not in processed_dirs:
                        try:
                            grandparent_resolved = grandparent.resolve()
                            if grandparent_resolved != root_resolved:
                                grandparent_entries = await async_scandir(grandparent)
                                if len(grandparent_entries) == 0:
                                    new_empty_parents.add(grandparent)
                        except (FileNotFoundError, PermissionError, OSError, RuntimeError):
                            pass

                except FileNotFoundError:
                    processed_dirs.add(parent)
                    self.logger.debug(f"Empty parent directory already deleted: {parent}")
                except OSError as e:
                    processed_dirs.add(parent)
                    log_with_context(
                        self.logger,
                        "warning",
                        "Could not remove empty parent directory",
                        {"directory": str(parent), "error": str(e)},
                    )
                    await self.update_stats(errors=1)

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
            },
        )

    async def _process_file_batch(self, file_tasks: list) -> None:
        """
        Process a batch of file tasks and free memory immediately.

        Args:
            file_tasks: List of file processing tasks
        """
        if not file_tasks:
            return

        # Check memory before processing
        await self.check_memory_pressure()

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
            entries = await async_scandir(directory)

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
                        file_task_buffer.append(self.process_file(entry_path))

                        # STREAMING: Process and clear buffer when it reaches batch size
                        if len(file_task_buffer) >= self.task_batch_size:
                            try:
                                await self._process_file_batch(file_task_buffer)
                            finally:
                                file_task_buffer.clear()  # Always clear, even on exception

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
                await self.check_memory_pressure()
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

            # Check if directory is empty (AFTER all subdirs have been fully processed recursively)
            # This ensures nested empty directories are handled correctly
            # Only check if remove_empty_dirs is enabled
            if self.remove_empty_dirs:
                await self._check_empty_directory(directory)

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
            if self.current_phase == "removing_empty_dirs":
                # During empty directory removal, check empty_dirs_deleted for progress
                if current_empty_dirs_deleted == self.last_empty_dirs_deleted:
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
            # Start the recursive scan
            self.current_phase = "scanning"
            self.rate_tracker.set_phase_start("scanning")
            await self.scan_directory(self.root_path)

            # Mark scanning phase as complete (for accurate overall rate calculation)
            self.scanning_end_time = time.time()

            # After all scanning is complete, remove empty directories in post-order
            if self.remove_empty_dirs:
                await self._remove_empty_directories()
        finally:
            # Cancel background reporter
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass  # Expected

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
    )

    return await purger.purge()
