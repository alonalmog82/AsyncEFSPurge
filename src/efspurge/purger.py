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
    ):
        """
        Initialize the async EFS purger.

        Args:
            root_path: Root directory to scan
            max_age_days: Files older than this (in days) will be purged
            max_concurrency: Maximum concurrent async operations
            dry_run: If True, only report what would be deleted
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        """
        self.root_path = Path(root_path)
        self.max_age_days = max_age_days
        self.cutoff_time = time.time() - (max_age_days * 86400)  # Convert days to seconds
        self.max_concurrency = max_concurrency
        self.dry_run = dry_run

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
        }

        # Concurrency control
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.stats_lock = asyncio.Lock()

        # Logging
        self.logger = setup_logging("efspurge", log_level)

        # Progress tracking
        self.last_progress_log = time.time()
        self.progress_interval = 30  # Log progress every 30 seconds

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
                        "elapsed_seconds": round(elapsed, 1),
                        "files_per_second": round(rate, 1),
                        "memory_mb": round(memory_mb, 1),
                        "memory_mb_per_1k_files": (
                            round(memory_mb / (self.stats["files_scanned"] / 1000), 2)
                            if self.stats["files_scanned"] > 0
                            else 0.0
                        ),
                    },
                )

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

            # Process all files in this directory concurrently
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Recursively process subdirectories
            for subdir in subdirs:
                await self.scan_directory(subdir)

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
            },
        )

        # Verify root path exists
        if not await aiofiles.os.path.exists(self.root_path):
            error_msg = f"Root path does not exist: {self.root_path}"
            log_with_context(self.logger, "error", error_msg, {"root_path": str(self.root_path)})
            raise FileNotFoundError(error_msg)

        # Start the recursive scan
        await self.scan_directory(self.root_path)

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
) -> dict:
    """
    Async entry point for the purger.

    Args:
        path: Root path to purge
        max_age_days: Maximum age of files in days
        max_concurrency: Maximum concurrent operations
        dry_run: If True, don't actually delete files
        log_level: Logging level

    Returns:
        Operation statistics
    """
    purger = AsyncEFSPurger(
        root_path=path,
        max_age_days=max_age_days,
        max_concurrency=max_concurrency,
        dry_run=dry_run,
        log_level=log_level,
    )

    return await purger.purge()
