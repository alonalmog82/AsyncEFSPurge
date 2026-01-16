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
        default=30.0,
        help="Files older than this (in days) will be purged",
    )

    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=1000,
        help="Maximum concurrent async operations (higher for network storage)",
    )

    parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=800,
        help="Soft memory limit in MB (triggers back-pressure, 0 = no limit)",
    )

    parser.add_argument(
        "--task-batch-size",
        type=int,
        default=5000,
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
        default="INFO",
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
        "--version",
        action="version",
        version=f"efspurge {__version__}",
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point for the CLI."""
    args = parse_args()

    try:
        # Run the async purger
        asyncio.run(
            async_main(
                path=args.path,
                max_age_days=args.max_age_days,
                max_concurrency=args.max_concurrency,
                dry_run=args.dry_run,
                log_level=args.log_level,
                memory_limit_mb=args.memory_limit_mb,
                task_batch_size=args.task_batch_size,
                remove_empty_dirs=args.remove_empty_dirs,
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
