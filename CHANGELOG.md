# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

