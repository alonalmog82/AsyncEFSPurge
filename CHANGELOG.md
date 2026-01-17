# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

