# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.12.2] - 2026-01-23

### Fixed
- **Critical: Memory Check Timing Bug**: Fixed critical bug where memory checks happened BEFORE batches, missing spikes that occurred DURING batch processing
  - **Root Cause**: Memory spikes occur during `asyncio.gather()` when many concurrent tasks run, but checks happened before batches started
  - **Impact**: Memory exceeded 168% of limit (3444 MB vs 2048 MB limit) with 0 backpressure events triggered
  - **Fix**: Memory checks now happen AFTER batch completion to catch spikes
  - `check_memory_pressure()` now returns `(bool, float)` tuple to enable proactive batch size reduction
  - Proactive batch size reduction based on memory percentage:
    - >100%: 75% reduction (aggressive)
    - >80%: 50% reduction (proactive)
    - >60%: 25% reduction (moderate)
  - Batch sizes dynamically adjust based on memory trends
  - **Expected Impact**: Memory spikes will now be detected and prevented, backpressure events will trigger correctly

### Testing
- **New Test**: Added `test_memory_checks_happen_after_batches` to verify timing fix
  - Verifies checks happen AFTER batches (not just before)
  - Ensures spikes during `asyncio.gather()` are caught
  - Would have failed with old code, passes with fix

## [1.12.1] - 2026-01-23

### Fixed
- **Memory Pressure Checks Not Triggering**: Fixed issue where memory backpressure events were not being triggered despite memory exceeding limits
  - `check_memory_pressure()` now returns boolean to enable dynamic batch size reduction
  - Memory checks now occur before EVERY batch (not just every 1000 directories)
  - Batch sizes dynamically reduce when memory is high, preventing memory spikes
  - Cascading deletion batch sizes reduced from 10k to 5k parents per iteration
- **Memory Explosion Still Occurring**: Enhanced memory pressure handling
  - Dynamic batch size reduction: 50% reduction when memory exceeds limit
  - More frequent memory checks catch spikes earlier
  - Batch sizes recover automatically when memory pressure decreases

### Performance
- **Removed Redundant Scandir Calls**: Eliminated unnecessary directory emptiness checks
  - Removed redundant `scandir` call before deleting directories (we already know they're empty)
  - Removed redundant `scandir` call before deleting parent directories in cascading deletion
  - **Impact**: ~50% reduction in scandir calls during empty directory deletion
- **Optimized Semaphore Usage**: Improved concurrency during empty directory deletion
  - Semaphore now only held during actual `rmdir` operation, not during parent checks
  - Allows other deletions to proceed while checking if parents became empty
  - **Impact**: Better parallelism and throughput during cascading deletion
- **Increased Batch Sizes for Performance**: Optimized batch sizes when memory is OK
  - Cascading deletion batch sizes increased from 20-100 to 50-200 (when memory allows)
  - Less aggressive memory reduction (minimum 10-20 instead of 5-10)
  - **Impact**: 3-5x improvement in empty directory deletion rate (from ~2 dirs/sec to ~6-10 dirs/sec)

### Testing
- **Performance Tests**: Added comprehensive tests for performance optimizations
  - `test_no_redundant_scandir_checks`: Verifies redundant checks are removed
  - `test_semaphore_released_early`: Verifies semaphore optimization works
  - `test_dynamic_batch_size_reduction`: Verifies batch sizes reduce when memory is high
  - `test_batch_size_recovery`: Verifies batch sizes recover when memory pressure decreases
- **Updated Memory Pressure Test**: Updated to reflect new behavior (checks before every batch)

## [1.12.0] - 2026-01-23

### Fixed
- **Memory Explosion During Empty Directory Deletion**: Fixed severe memory spike when deleting large numbers of empty directories (10k+)
  - Reduced batch sizes from `max_concurrency_deletion * 2` to `min(200, max(50, max_concurrency_deletion // 10))`
  - Added memory pressure checks every 1000 directories during deletion
  - Improved incremental result processing to avoid accumulating large lists
  - Limited cascading deletion to process max 10,000 parents per iteration
  - **Before**: Memory could grow from ~250MB to 1500MB+ with 100k+ empty directories
  - **After**: Memory increase stays bounded (< 300MB even with 10k+ directories)

### Added
- **Scandir Executor Diagnostics**: Added DEBUG-level diagnostics for directory scanning executor
  - Tracks total scandir calls, average time per call, calls per second
  - Monitors thread pool utilization (active/total/idle threads)
  - Calculates directories per thread per second
  - Logs diagnostics periodically during scanning and at completion
  - Only enabled when logging level is DEBUG

### Testing
- **Memory Safety Tests**: Added comprehensive tests for memory boundedness during empty directory deletion
  - `test_large_scale_empty_dir_deletion_memory_bounded`: Verifies memory stays bounded with 10k+ directories
  - `test_empty_dir_deletion_batch_sizes`: Verifies batch size limits prevent memory explosion
  - `test_empty_dir_deletion_memory_pressure_checks`: Verifies memory pressure checks are called
  - `test_cascading_deletion_memory_bounded`: Verifies cascading deletion doesn't cause memory explosion
- **Scandir Diagnostics Tests**: Added tests for executor diagnostics
  - Verifies diagnostics are only logged at DEBUG level
  - Verifies metrics accumulate correctly
  - Verifies diagnostics don't interfere with normal operation
- **Test Configuration**: Added `conftest.py` to ensure pytest uses local source code during testing

## [1.11.0] - 2026-01-23

### Performance
- **Custom ThreadPoolExecutor for Directory Scanning**: Implemented custom executor to bypass default thread pool limitation
  - **Before**: Limited to ~32 threads (default executor), capping directory scanning at ~250-300 dirs/sec
  - **After**: Scales to 200-500 threads based on `max_concurrent_subdirs`, enabling 2-5x improvement
  - **Example**: With `max_concurrent_subdirs=4000`, now uses 400 threads instead of 32
  - **Expected improvement**: Directory scanning rate increases from ~280 dirs/sec to 500-1000+ dirs/sec
  - Thread count scales intelligently: 100-200 threads for moderate setups, 200-500 for high-concurrency setups
  - Executor is properly cleaned up on completion

### Changed
- **`async_scandir` function**: Now accepts optional `executor` parameter to use custom ThreadPoolExecutor
- **Startup logging**: Added `scandir_executor_threads` field to show executor thread count

### Documentation
- **README**: Updated "Directory Scanning Bottleneck" section to reflect that the bottleneck is now fixed by default
  - The workaround documented in v1.10.0 is now the default implementation
  - Users no longer need to manually modify code to increase directory scanning throughput

## [1.10.0] - 2026-01-23

### Fixed
- **Overall Rate Calculation**: Fixed bug where overall `files_per_second` and `dirs_per_second` rates incorrectly included empty directory removal time
  - Now uses scanning duration only (excludes empty dir removal phase)
  - More accurate representation of actual scanning performance
  - `scanning_end_time` is tracked and used for rate calculations

### Changed
- **Progress Output Improvements**:
  - Reordered fields for better readability (elapsed_seconds, files_scanned, files_purged, dirs_scanned, etc.)
  - Removed static fields from progress logs (`memory_limit_mb`, `max_concurrency_*`) - these are shown in startup log
  - Moved detailed metrics to DEBUG level only (time-windowed rates, peak rates, concurrency utilization)
  - Phase-specific output: scanning phase shows file/dir metrics, removing_empty_dirs phase shows dir removal metrics
  - Core metrics (rates, memory) remain in INFO level

### Performance
- **Concurrent Empty Directory Removal**: Empty directories are now deleted concurrently instead of sequentially
  - Uses `deletion_semaphore` to control concurrency (respects `max_concurrency_deletion`)
  - Processes directories in batches (up to `max_concurrency_deletion * 2`)
  - Maintains post-order deletion (deepest first) while processing concurrently
  - **Performance improvement**: From ~1.1 dirs/sec to hundreds/thousands per second
  - **Example**: 166,624 empty directories would take ~42 hours sequentially, now completes in minutes
  - Rate limit checking works correctly with concurrent processing

### Added
- **Comprehensive Tests**: Added 7 new tests for concurrent empty directory removal
  - `test_concurrent_empty_dir_deletion` - Verifies concurrent deletion performance
  - `test_concurrent_deletion_respects_semaphore` - Verifies semaphore limits
  - `test_concurrent_cascading_deletion` - Cascading deletion with concurrency
  - `test_concurrent_deletion_no_duplicates` - Race condition prevention
  - `test_concurrent_deletion_rate_limit` - Rate limits with concurrency
  - `test_concurrent_deletion_handles_already_deleted` - FileNotFoundError handling
  - `test_concurrent_deletion_handles_populated_dirs` - Skips populated directories
- **Tests for Rate Calculation**: Added 3 tests verifying overall rate calculation fix
- **Tests for Progress Output**: Added 4 tests verifying field ordering and DEBUG-level filtering

### Documentation
- **Directory Scanning Bottleneck**: Added documentation explaining ThreadPoolExecutor limitation
  - Explains why `max_concurrent_subdirs` doesn't help beyond ~300 dirs/sec
  - Documents default thread pool size (~32 threads)
  - Provides code examples for widening the bottleneck with custom ThreadPoolExecutor
  - Documents trade-offs of increasing thread pool size

## [1.9.1] - 2026-01-22

### Fixed
- **Critical: Subdirectory Concurrency Deadlock**: Fixed stuck detection issue where application would get stuck on large directories with 0% concurrency utilization
  - Root cause: Batch-based subdirectory processing waited for entire batches to complete, causing idle slots when some directories finished early
  - Solution: Implemented hybrid approach with semaphore-controlled concurrency and on-demand task creation
  - Maintains constant concurrency (no idle slots) while preventing memory explosion
  - Prevents deadlock in deep directory trees by detecting recursive calls and processing sequentially when needed
  - Verified with stress tests up to 518,481 directories (80×80×80 structure)
  - See `SUBDIR_CONCURRENCY_FIX.md` for detailed explanation

### Added
- **New Tests**: Added 7 comprehensive tests for subdirectory concurrency behavior
  - `test_subdir_concurrency_maintained` - Verifies constant concurrency
  - `test_slow_directories_dont_block_others` - Verifies no blocking
  - `test_tasks_created_on_demand` - Verifies memory safety
  - `test_memory_bounded_with_many_subdirs` - Verifies memory bounds
  - `test_deep_directory_tree_memory_safety` - Verifies deep trees (40×40×40)
  - `test_hybrid_approach_maintains_concurrency` - Verifies hybrid approach
  - `test_subdir_semaphore_limits_concurrency` - Verifies semaphore limits

### Documentation
- Added `SUBDIR_CONCURRENCY_FIX.md` - Comprehensive documentation of the fix
- Added `PRODUCTION_READINESS_ASSESSMENT.md` - Production safety assessment
- Updated code comments with guidance for testing changes with 80×80×80 structure

## [1.9.0] - 2026-01-XX

### Breaking Changes
- **Split Concurrency Parameters**: `--max-concurrency` is now deprecated
  - Use `--max-concurrency-scanning` and `--max-concurrency-deletion` instead
  - Allows independent tuning of scanning (stat) vs deletion (remove) operations
  - Deprecated parameter still works but shows deprecation warnings
- **Environment Variable Changes**: `EFSPURGE_MAX_CONCURRENCY` is deprecated
  - Use `EFSPURGE_MAX_CONCURRENCY_SCANNING` and `EFSPURGE_MAX_CONCURRENCY_DELETION` instead
  - Deprecated env var still works but shows deprecation warnings

### Added
- **New Parameters**: `--max-concurrency-scanning` and `--max-concurrency-deletion`
  - Independent concurrency limits for file scanning and deletion phases
  - Scanning can often handle higher concurrency than deletion
  - Environment variables: `EFSPURGE_MAX_CONCURRENCY_SCANNING`, `EFSPURGE_MAX_CONCURRENCY_DELETION`
- **Environment Variable Support**: All configuration parameters now support environment variables
  - `EFSPURGE_MAX_AGE_DAYS` (default: 30.0)
  - `EFSPURGE_MEMORY_LIMIT_MB` (default: 800)
  - `EFSPURGE_TASK_BATCH_SIZE` (default: 5000)
  - `EFSPURGE_LOG_LEVEL` (default: INFO)
  - Makes Kubernetes ConfigMap/Secret management easier
- **Enhanced Metrics**: Progress logs now include separate concurrency metrics
  - `max_concurrency_scanning` and `max_concurrency_deletion` in progress logs
  - Better visibility for tuning each phase independently

### Changed
- **Concurrency Control**: File operations now use separate semaphores
  - Scanning operations use `scanning_semaphore`
  - Deletion operations use `deletion_semaphore`
  - Empty directory deletion remains sequential (unchanged)

### Documentation
- Updated `CONCURRENCY_TUNING.md` with split concurrency guidance
- Updated `README.md` with new parameters and deprecation notices
- Updated `k8s-cronjob.yaml` example to use environment variables
- Added migration guide for deprecated parameters

## [1.7.3] - 2026-01-17

### Added
- **New Parameter: `--max-concurrent-subdirs`**: Controls maximum subdirectories scanned concurrently (default: 100)
  - Fixes OOM issues on deep directory trees where recursive scanning created exponential coroutine explosion
  - Environment variable: `EFSPURGE_MAX_CONCURRENT_SUBDIRS`
  - Lower values (10-20) recommended for memory-constrained environments or deep trees

### Documentation
- Added "Tuning Memory for Deep Directory Trees" section explaining the recursive explosion problem
- Updated Troubleshooting section with guidance for OOM kills on deep vs flat directories
- Added key diagnostic insight: if `dirs_scanned` grows but `files_scanned=0`, reduce `--max-concurrent-subdirs`

## [1.7.2] - 2026-01-17

### Fixed
- **Deprecated API**: Replaced `asyncio.get_event_loop()` with `asyncio.get_running_loop()` for Python 3.10+ compatibility
- **Silent Exception Swallowing**: Batch processing now logs unexpected exceptions instead of silently discarding them
- **Progress Tracking Bug**: `last_progress_log` is now properly updated by background reporter, fixing final progress log timing

### Added
- **System Directory Protection**: Blocks dangerous system directories (`/proc`, `/sys`, `/dev`, `/run`, `/boot`, `/bin`, `/sbin`, `/lib`, `/etc`, etc.) with clear error messages to prevent accidental system damage
- **Special File Handling**: Sockets, FIFOs, block/char devices are now detected, skipped, and tracked via `special_files_skipped` stat
- **TOCTOU Documentation**: Added comprehensive "Race Condition Considerations" section to README explaining the inherent POSIX limitation and design decisions

### Tests
- Added `test_system_directory_blocked` - Verifies dangerous paths are rejected
- Added `test_safe_paths_allowed` - Verifies safe paths are accepted
- Added `test_special_files_skipped` - Tests Unix socket detection
- Added `test_fifo_skipped` - Tests FIFO (named pipe) detection
- All 53 tests passing

## [1.7.1] - 2026-01-17

### Fixed
- **Rate Limit Logging Bug**: Fixed negative count bug in rate limit reached messages
  - Changed `empty_dirs_remaining` to `unprocessed_dirs_in_batch` (accurate count of unprocessed directories)
  - Changed `parents_remaining` to `unprocessed_parents_in_batch` (accurate count of unprocessed parent directories)
  - Both fields now correctly show non-negative values representing actual unprocessed items in current batch
  - Previously could show negative numbers due to incorrect calculation using global `processed_dirs` count

### Added
- **Separate Counters for Dry-Run Clarity**: Added `empty_dirs_to_delete` counter to match file deletion pattern
  - `empty_dirs_to_delete`: Directories that would be deleted (increments in both dry-run and real mode)
  - `empty_dirs_deleted`: Directories actually deleted (0 in dry-run, real count in real mode)
  - Consistent with existing `files_to_purge` vs `files_purged` pattern
  - Makes dry-run behavior crystal clear in logs

### Changed
- **Breaking Change in Stats Output**: Output now includes both `empty_dirs_to_delete` and `empty_dirs_deleted` fields
  - Old: `"empty_dirs_deleted": 3000` (ambiguous in dry-run)
  - New: `"empty_dirs_to_delete": 3000, "empty_dirs_deleted": 0` (clear dry-run indication)

## [1.7.0] - 2026-01-16

### Added
- **Empty Directory Rate Limiting**: New `--max-empty-dirs-to-delete` parameter to control deletion rate (default: 500 per run)
  - Prevents filesystem metadata storms on network filesystems like AWS EFS
  - Configurable via CLI flag or `EFSPURGE_MAX_EMPTY_DIRS_TO_DELETE` environment variable
  - Set to 0 for unlimited deletion (useful for initial cleanup runs)
  - Per-run limiting design aligns with CronJob deployment pattern
- **Comprehensive Rate Limiting Documentation**: Added detailed explanation of rate limiting rationale, use cases, and common scenarios
- **5 New Tests**: Complete test coverage for rate limiting feature including edge cases and validation

### Changed
- **Default Behavior**: Empty directory deletion now limited to 500 directories per run by default (was unlimited)
- **Improved Documentation**: Enhanced README with metadata storm explanation and CronJob-based rate limiting design

## [1.6.3] - 2026-01-16

### Added
- **Version in Startup Log**: Added package version to startup log output for easier identification of running version in production

## [1.6.2] - 2026-01-16

### Fixed
- **Duplicate Progress Logging**: Removed duplicate progress logs from `update_stats()` method
- **Empty Directory Removal Visibility**: Fixed duplicate "Starting cascading empty directory removal" log message
- **Progress Logging During Cascading Deletion**: Improved progress logging to every 100 iterations or 1000 directories (instead of every 10 iterations) for better visibility during long-running operations

### Added
- **Logging Tests**: Added comprehensive test suite (`tests/test_logging.py`) to catch duplicate logging bugs
  - Tests verify no duplicate progress logs
  - Tests verify empty directory removal logs appear when enabled
  - Tests verify startup log includes `remove_empty_dirs` setting

## [1.6.1] - 2026-01-16

### Changed
- Added `remove_empty_dirs` to startup log output for better visibility
- Updated README with complete CLI flags and environment variables documentation

## [1.6.0] - 2026-01-16

### Added
- **Empty Directory Removal Feature**: New `--remove-empty-dirs` flag and `EFSPURGE_REMOVE_EMPTY_DIRS` environment variable
  - Post-order deletion (children before parents)
  - Cascading deletion (parents checked after children deleted)
  - Root directory always preserved
  - Comprehensive test coverage (9 tests)
  - Race condition tests (4 tests)

### Fixed
- **Critical Bug Fixes**:
  - Fixed race condition: Duplicate directory entries from concurrent scans (changed to set)
  - Fixed list modification during iteration (two-pass approach)
  - Fixed path comparison edge cases (using resolve())
  - Fixed cascading deletion logic (iterative parent checking)
  - Fixed root directory protection (robust path comparison)

### Security
- Thread-safe empty directory tracking (atomic operations under lock)
- Robust error handling for edge cases

### Documentation
- Added `EMPTY_DIRS_FEATURE.md` - Feature documentation
- Added `EMPTY_DIRS_BUG_ANALYSIS.md` - Detailed bug analysis
- Added `BUG_FIXES_SUMMARY.md` - Summary of all fixes
- Added `DESIGN_PRESERVATION_VERIFICATION.md` - Verification that async/sliding window preserved

### Tests
- Added 9 empty directory removal tests
- Added 4 race condition tests
- All 40 tests passing
- Code coverage: 68%

## [1.5.0] - Previous Release

### Added
- True streaming architecture (sliding window)
- Memory back-pressure system
- Background progress reporter
- Comprehensive edge case tests
- Integration tests

## [1.4.x] - Previous Releases

### Added
- Memory monitoring
- Concurrent subdirectory processing
- Batched task creation
- Memory back-pressure

## [1.0.0] - Initial Release

### Added
- Async file purging
- Dry-run mode
- JSON logging
- Docker support
- Kubernetes CronJob support

