"""Async file purger optimized for AWS EFS and network storage."""

import asyncio
import os
import time
from pathlib import Path

import aiofiles.os

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
    loop = asyncio.get_event_loop()

    def _scandir():
        with os.scandir(path) as entries:
            return list(entries)

    return await loop.run_in_executor(None, _scandir)


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
        max_concurrency: int = 1000,
        dry_run: bool = True,
        log_level: str = "INFO",
        memory_limit_mb: int = 800,
        task_batch_size: int = 5000,
        remove_empty_dirs: bool = False,
    ):
        """
        Initialize the async EFS purger.

        Args:
            root_path: Root directory to scan
            max_age_days: Files older than this (in days) will be purged
            max_concurrency: Maximum concurrent async operations
            dry_run: If True, only report what would be deleted
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            memory_limit_mb: Soft memory limit in MB (triggers back-pressure, 0 = disabled)
            task_batch_size: Maximum tasks to create at once (prevents OOM)
            remove_empty_dirs: If True, remove empty directories after scanning (post-order)

        Raises:
            ValueError: If invalid parameters are provided
        """
        # Input validation
        if max_age_days < 0:
            raise ValueError(f"max_age_days must be >= 0, got {max_age_days}")

        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")

        if task_batch_size < 1:
            raise ValueError(f"task_batch_size must be >= 1, got {task_batch_size}")

        if memory_limit_mb < 0:
            raise ValueError(f"memory_limit_mb must be >= 0, got {memory_limit_mb}")

        # Ensure root_path is absolute
        root_path_obj = Path(root_path)
        if not root_path_obj.is_absolute():
            root_path_obj = root_path_obj.resolve()

        self.root_path = root_path_obj
        self.max_age_days = max_age_days
        self.cutoff_time = time.time() - (max_age_days * 86400)  # Convert days to seconds
        self.max_concurrency = max_concurrency
        self.dry_run = dry_run
        self.memory_limit_mb = memory_limit_mb
        self.task_batch_size = task_batch_size
        self.remove_empty_dirs = remove_empty_dirs

        # Statistics
        self.stats = {
            "files_scanned": 0,
            "files_to_purge": 0,
            "files_purged": 0,
            "dirs_scanned": 0,
            "symlinks_skipped": 0,
            "errors": 0,
            "bytes_freed": 0,
            "start_time": time.time(),
            "memory_backpressure_events": 0,
            "empty_dirs_deleted": 0,
        }

        # Track empty directories for post-order deletion
        # Use set to prevent duplicates from concurrent scans
        self.empty_dirs: set[Path] = set()

        # Concurrency control
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.stats_lock = asyncio.Lock()

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

            # Log progress periodically
            current_time = time.time()
            if current_time - self.last_progress_log >= self.progress_interval:
                self.last_progress_log = current_time
                elapsed = current_time - self.stats.get("start_time", current_time)
                rate = self.stats["files_scanned"] / elapsed if elapsed > 0 else 0
                memory_mb = get_memory_usage_mb()

                log_with_context(
                    self.logger,
                    "info",
                    "Progress update",
                    {
                        "files_scanned": self.stats["files_scanned"],
                        "files_to_purge": self.stats["files_to_purge"],
                        "files_purged": self.stats["files_purged"],
                        "dirs_scanned": self.stats["dirs_scanned"],
                        "errors": self.stats["errors"],
                        "memory_backpressure_events": self.stats["memory_backpressure_events"],
                        "elapsed_seconds": round(elapsed, 1),
                        "files_per_second": round(rate, 1),
                        "memory_mb": round(memory_mb, 1),
                        "memory_limit_mb": self.memory_limit_mb,
                        "memory_usage_percent": round((memory_mb / self.memory_limit_mb) * 100, 1)
                        if self.memory_limit_mb > 0
                        else 0.0,
                        "memory_mb_per_1k_files": (
                            round(memory_mb / (self.stats["files_scanned"] / 1000), 2)
                            if self.stats["files_scanned"] > 0
                            else 0.0
                        ),
                    },
                )

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
        async with self.semaphore:
            try:
                # Get file stats asynchronously
                stat = await aiofiles.os.stat(file_path)
                await self.update_stats(files_scanned=1)

                # Check if file is old enough to purge
                if stat.st_mtime < self.cutoff_time:
                    await self.update_stats(files_to_purge=1)

                    if not self.dry_run:
                        # Delete the file
                        await aiofiles.os.remove(file_path)
                        await self.update_stats(files_purged=1, bytes_freed=stat.st_size)
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
                    await self.update_stats(empty_dirs_deleted=1)
                    self.logger.debug(f"Removed empty directory: {directory}")
                else:
                    await self.update_stats(empty_dirs_deleted=1)
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

        # Second pass: Process parents that became empty (cascading deletion)
        # Continue until no new empty parents are found
        while new_empty_parents:
            # Get next batch of parents to process
            parents_to_process = sorted(new_empty_parents, key=lambda p: len(p.parts), reverse=True)
            new_empty_parents = set()  # Reset for next iteration

            for parent in parents_to_process:
                if parent in processed_dirs:
                    continue

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
                        await self.update_stats(empty_dirs_deleted=1)
                        self.logger.debug(f"Removed empty parent directory: {parent}")
                    else:
                        await self.update_stats(empty_dirs_deleted=1)
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

        # Process batch
        await asyncio.gather(*file_tasks, return_exceptions=True)

        self.logger.debug(f"Processed batch of {len(file_tasks)} files")

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
        try:
            await self.update_stats(dirs_scanned=1)

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

            # Process subdirectories concurrently
            # Limit concurrent subdirs to prevent memory explosion (max 100 at a time)
            if subdirs:
                await self.check_memory_pressure()
                max_concurrent_subdirs = 100
                for i in range(0, len(subdirs), max_concurrent_subdirs):
                    batch_subdirs = subdirs[i : i + max_concurrent_subdirs]
                    subdir_tasks = [self.scan_directory(subdir) for subdir in batch_subdirs]
                    await asyncio.gather(*subdir_tasks, return_exceptions=True)

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

    async def _background_progress_reporter(self) -> None:
        """
        Background task that logs progress every N seconds.

        This ensures progress updates even when processing is slow or
        there are long periods of directory traversal without file processing.
        """
        while True:
            await asyncio.sleep(self.progress_interval)

            # Log current progress
            async with self.stats_lock:
                current_time = time.time()
                elapsed = current_time - self.stats.get("start_time", current_time)
                rate = self.stats["files_scanned"] / elapsed if elapsed > 0 else 0
                memory_mb = get_memory_usage_mb()
                memory_percent = (memory_mb / self.memory_limit_mb * 100) if self.memory_limit_mb > 0 else 0

                log_with_context(
                    self.logger,
                    "info",
                    "Progress update",
                    {
                        "files_scanned": self.stats["files_scanned"],
                        "files_to_purge": self.stats["files_to_purge"],
                        "files_purged": self.stats["files_purged"],
                        "dirs_scanned": self.stats["dirs_scanned"],
                        "errors": self.stats["errors"],
                        "memory_backpressure_events": self.stats.get("memory_backpressure_events", 0),
                        "elapsed_seconds": round(elapsed, 1),
                        "files_per_second": round(rate, 1),
                        "memory_mb": round(memory_mb, 1),
                        "memory_limit_mb": self.memory_limit_mb,
                        "memory_usage_percent": round(memory_percent, 1),
                        "memory_mb_per_1k_files": (
                            round(memory_mb / (self.stats["files_scanned"] / 1000), 2)
                            if self.stats["files_scanned"] > 0
                            else 0.0
                        ),
                    },
                )

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
                "root_path": str(self.root_path),
                "max_age_days": self.max_age_days,
                "cutoff_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.cutoff_time)),
                "max_concurrency": self.max_concurrency,
                "dry_run": self.dry_run,
                "progress_interval_seconds": self.progress_interval,
                "memory_limit_mb": self.memory_limit_mb,
                "task_batch_size": self.task_batch_size,
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
            await self.scan_directory(self.root_path)

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
            rate = self.stats["files_scanned"] / elapsed if elapsed > 0 else 0
            memory_mb = get_memory_usage_mb()
            log_with_context(
                self.logger,
                "info",
                "Final progress before completion",
                {
                    "files_scanned": self.stats["files_scanned"],
                    "files_to_purge": self.stats["files_to_purge"],
                    "files_purged": self.stats["files_purged"],
                    "dirs_scanned": self.stats["dirs_scanned"],
                    "errors": self.stats["errors"],
                    "elapsed_seconds": round(elapsed, 1),
                    "files_per_second": round(rate, 1),
                    "memory_mb": round(memory_mb, 1),
                },
            )

        # Calculate final statistics
        duration = time.time() - start_time
        files_per_sec = self.stats["files_scanned"] / duration if duration > 0 else 0
        mb_freed = self.stats["bytes_freed"] / (1024 * 1024)
        memory_mb = get_memory_usage_mb()

        final_stats = {
            **self.stats,
            "duration_seconds": round(duration, 2),
            "files_per_second": round(files_per_sec, 2),
            "mb_freed": round(mb_freed, 2),
            "peak_memory_mb": round(memory_mb, 1),
        }

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
    max_concurrency: int = 1000,
    dry_run: bool = True,
    log_level: str = "INFO",
    memory_limit_mb: int = 800,
    task_batch_size: int = 5000,
    remove_empty_dirs: bool = False,
) -> dict:
    """
    Async entry point for the purger.

    Args:
        path: Root path to purge
        max_age_days: Maximum age of files in days
        max_concurrency: Maximum concurrent operations
        dry_run: If True, don't actually delete files
        log_level: Logging level
        memory_limit_mb: Soft memory limit in MB (0 = no limit)
        task_batch_size: Maximum tasks to create at once
        remove_empty_dirs: If True, remove empty directories after scanning

    Returns:
        Operation statistics
    """
    purger = AsyncEFSPurger(
        root_path=path,
        max_age_days=max_age_days,
        max_concurrency=max_concurrency,
        dry_run=dry_run,
        log_level=log_level,
        memory_limit_mb=memory_limit_mb,
        task_batch_size=task_batch_size,
        remove_empty_dirs=remove_empty_dirs,
    )

    return await purger.purge()
