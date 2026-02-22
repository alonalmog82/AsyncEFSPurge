"""Command-line interface for EFS Purge."""

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .purger import CheckpointExit, async_main

logger = logging.getLogger("efspurge")


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
        help=(
            "Files older than this (in days) will be purged. "
            "Use 0 to skip file processing entirely (useful for empty directory deletion only)"
        ),
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
        help=(
            "Remove empty directories using two-pass approach: "
            "Phase 1 deletes existing empty dirs before scanning, "
            "Phase 3 cleans up dirs that became empty after file purging"
        ),
    )

    parser.add_argument(
        "--max-empty-dirs-to-delete",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE", "500")),
        help="Maximum empty directories to delete per run (0 = unlimited, default: 500)",
    )

    parser.add_argument(
        "--max-discovery-dirs",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_DISCOVERY_DIRS", "0")),
        help="Maximum directories to discover in Phase 1a (0 = auto based on memory limit)",
    )

    parser.add_argument(
        "--max-concurrent-discovery",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_CONCURRENT_DISCOVERY", "20")),
        help="Maximum directories to scan concurrently during Phase 1a discovery (default: 20)",
    )

    parser.add_argument(
        "--queue-maxsize",
        type=int,
        default=int(os.getenv("EFSPURGE_QUEUE_MAXSIZE", "10000")),
        help=(
            "Max size of Phase 1a/2 directory queues (0 = unbounded, default: 10000). "
            "Bounds memory when discovery outpaces processing."
        ),
    )

    parser.add_argument(
        "--max-entries-per-dir",
        type=int,
        default=int(os.getenv("EFSPURGE_MAX_ENTRIES_PER_DIR", "0")),
        help=(
            "Cap entries processed per directory in Phase 1a (0 = no limit, default: 0). "
            "When set (e.g. 50000), huge directories are re-queued and scanned in chunks to avoid stalling workers."
        ),
    )

    parser.add_argument(
        "--checkpoint-file",
        type=str,
        default=os.getenv("EFSPURGE_CHECKPOINT_FILE", ""),
        help=(
            "Path to save checkpoint when memory is critical (95%%+). "
            "Enables auto-checkpoint and graceful exit for resume. Use with --resume to continue."
        ),
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        default=os.getenv("EFSPURGE_RESUME", "").lower() in ("1", "true", "yes"),
        help="Resume Phase 2 from checkpoint file (requires --checkpoint-file). Skips Phase 1.",
    )

    parser.add_argument(
        "--no-uvloop",
        action="store_true",
        default=os.getenv("EFSPURGE_UVLOOP", "true").lower() in ("0", "false", "no"),
        help=(
            "Disable uvloop and use the default asyncio event loop. "
            "uvloop is enabled by default on Linux/macOS for better I/O performance. "
            "Set EFSPURGE_UVLOOP=false to disable via environment variable. "
            "Has no effect on Windows where uvloop is not available."
        ),
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

    # Determine event loop factory: use uvloop by default on Linux/macOS
    loop_factory = None
    if not args.no_uvloop:
        try:
            import uvloop

            loop_factory = uvloop.new_event_loop
        except ImportError:
            # uvloop not installed (e.g. Windows, or minimal install)
            pass

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
                max_discovery_dirs=args.max_discovery_dirs,
                max_concurrent_discovery=args.max_concurrent_discovery,
                queue_maxsize=args.queue_maxsize,
                max_entries_per_dir=args.max_entries_per_dir,
                checkpoint_file=args.checkpoint_file or None,
                resume=args.resume,
            ),
            loop_factory=loop_factory,
        )

        # Exit with success
        sys.exit(0)

    except CheckpointExit as e:
        print(f"\n{e}", file=sys.stderr)
        print("Run with --resume to continue from checkpoint.", file=sys.stderr)
        sys.exit(75)  # EX_TEMPFAIL - checkpoint saved, resume suggested
    except KeyboardInterrupt:
        print("\nOperation cancelled by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
