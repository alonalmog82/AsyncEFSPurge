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
    ):
        """
        Initialize the async EFS purger.

        Args:
            root_path: Root directory to scan
            max_age_days: Files older than this (in days) will be purged
            max_concurrency: Maximum concurrent async operations
            dry_run: If True, only report what would be deleted
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            memory_limit_mb: Soft memory limit in MB (triggers back-pressure)
            task_batch_size: Maximum tasks to create at once (prevents OOM)
        """
        self.root_path = Path(root_path)
        self.max_age_days = max_age_days
        self.cutoff_time = time.time() - (max_age_days * 86400)  # Convert days to seconds
        self.max_concurrency = max_concurrency
        self.dry_run = dry_run
        self.memory_limit_mb = memory_limit_mb
        self.task_batch_size = task_batch_size

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
        }

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

    async def scan_directory(self, directory: Path) -> None:
        """
        Recursively scan a directory and process all files.

        Args:
            directory: Directory path to scan
        """
        try:
            await self.update_stats(dirs_scanned=1)

            # Scan directory entries
            entries = await async_scandir(directory)

            tasks = []
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

                    # Process files and queue subdirectories
                    if entry.is_file(follow_symlinks=False):
                        tasks.append(self.process_file(entry_path))
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

            # FIX #2: Process files in batches to prevent OOM from unbounded task creation
            if tasks:
                for i in range(0, len(tasks), self.task_batch_size):
                    batch = tasks[i : i + self.task_batch_size]

                    # Check memory pressure before processing each batch
                    # (but lock prevents spam from concurrent checks)
                    await self.check_memory_pressure()

                    await asyncio.gather(*batch, return_exceptions=True)

                    batch_num = i // self.task_batch_size + 1
                    total_batches = (len(tasks) + self.task_batch_size - 1) // self.task_batch_size
                    self.logger.debug(
                        f"Processed batch {batch_num}/{total_batches} ({len(batch)} files) in {directory}"
                    )

            # FIX #1: Recursively process subdirectories CONCURRENTLY (not sequentially)
            if subdirs:
                # Only check memory once before spawning all subdirectory tasks
                # (not per subdirectory, to reduce overhead)
                await self.check_memory_pressure()

                # Process all subdirectories concurrently for massive performance boost
                await asyncio.gather(*[self.scan_directory(subdir) for subdir in subdirs], return_exceptions=True)

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
    )

    return await purger.purge()
