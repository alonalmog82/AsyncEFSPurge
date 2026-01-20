"""Command-line interface for EFS Purge."""

import argparse
import asyncio
import os
import sys

from . import __version__
from .purger import async_main


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="AsyncEFSPurge - High-performance async file purger for AWS EFS",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "path",
        help="Root path to scan and purge",
    )

    parser.add_argument(
        "--max-age-days",
        type=float,
        default=float(os.getenv("EFSPURGE_MAX_AGE_DAYS", "30.0")),
        help="Files older than this (in days) will be purged",
    )

    # Backward compatibility: if EFSPURGE_MAX_CONCURRENCY is set, use it for both
    env_max_concurrency = os.getenv("EFSPURGE_MAX_CONCURRENCY")
    default_max_concurrency = int(env_max_concurrency) if env_max_concurrency else None

    # Warn if deprecated env var is used
    if env_max_concurrency:
        import warnings

        warnings.warn(
            "EFSPURGE_MAX_CONCURRENCY is deprecated. Use EFSPURGE_MAX_CONCURRENCY_SCANNING and "
            "EFSPURGE_MAX_CONCURRENCY_DELETION instead. Setting both to the same value for backward compatibility.",
            DeprecationWarning,
            stacklevel=2,
        )

    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=default_max_concurrency,
        help="[DEPRECATED] Maximum concurrent async operations (use --max-concurrency-scanning/deletion instead)",
    )

    parser.add_argument(
        "--max-concurrency-scanning",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_CONCURRENCY_SCANNING", "0") or "0") or None,
        help="Maximum concurrent file scanning (stat) operations (default: 1000, or --max-concurrency if set)",
    )

    parser.add_argument(
        "--max-concurrency-deletion",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_CONCURRENCY_DELETION", "0") or "0") or None,
        help="Maximum concurrent file deletion (remove) operations (default: 1000, or --max-concurrency if set)",
    )

    parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=int(os.getenv("EFSPURGE_MEMORY_LIMIT_MB", "800")),
        help="Soft memory limit in MB (triggers back-pressure, 0 = no limit)",
    )

    parser.add_argument(
        "--task-batch-size",
        type=int,
        default=int(os.getenv("EFSPURGE_TASK_BATCH_SIZE", "5000")),
        help="Maximum tasks to create at once (prevents OOM)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't actually delete files, just report what would be deleted",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default=os.getenv("EFSPURGE_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )

    parser.add_argument(
        "--remove-empty-dirs",
        action="store_true",
        default=os.getenv("EFSPURGE_REMOVE_EMPTY_DIRS", "").lower() in ("1", "true", "yes"),
        help="Remove empty directories after scanning (post-order deletion)",
    )

    parser.add_argument(
        "--max-empty-dirs-to-delete",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE", "500")),
        help="Maximum empty directories to delete per run (0 = unlimited, default: 500)",
    )

    parser.add_argument(
        "--max-concurrent-subdirs",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_CONCURRENT_SUBDIRS", "100")),
        help="Maximum subdirectories to scan concurrently (lower = less memory, default: 100)",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"efspurge {__version__}",
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point for the CLI."""
    args = parse_args()

    # Warn if deprecated --max-concurrency is explicitly set (not just from env var)
    if args.max_concurrency is not None:
        import warnings

        # Check if it was set via command line (not just env var default)
        # This is approximate - we can't perfectly detect CLI vs env, but we warn anyway
        warnings.warn(
            "--max-concurrency is deprecated. Use --max-concurrency-scanning and "
            "--max-concurrency-deletion instead. Setting both to the same value for backward compatibility.",
            DeprecationWarning,
            stacklevel=2,
        )

    try:
        # Run the async purger
        asyncio.run(
            async_main(
                path=args.path,
                max_age_days=args.max_age_days,
                max_concurrency=args.max_concurrency,
                max_concurrency_scanning=args.max_concurrency_scanning,
                max_concurrency_deletion=args.max_concurrency_deletion,
                dry_run=args.dry_run,
                log_level=args.log_level,
                memory_limit_mb=args.memory_limit_mb,
                task_batch_size=args.task_batch_size,
                remove_empty_dirs=args.remove_empty_dirs,
                max_empty_dirs_to_delete=args.max_empty_dirs_to_delete,
                max_concurrent_subdirs=args.max_concurrent_subdirs,
            )
        )

        # Exit with success
        sys.exit(0)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
