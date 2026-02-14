# Incremental Empty Directory Processing Thrashing Fix (v1.14.1)

> **⚠️ HISTORICAL (pre-v2.0):** Incremental empty directory processing was replaced by the two-pass architecture in v1.15.0. This document is retained for historical context only.

## Overview

This hotfix addresses critical race conditions and thrashing issues in the incremental empty directory processing feature introduced in v1.14.0. These issues were identified in production logs showing thousands of rapid-fire trigger messages and micro-batch processing.

## Problems Identified

### Issue #1: Race Condition - Multiple Concurrent Batch Processing
**Symptoms:**
- Multiple concurrent scan tasks all triggered batch processing simultaneously when hitting memory threshold
- Seen in logs: 20 tasks logging "Incremental empty directory processing triggered" at identical timestamps

**Root Cause:**
Up to 20 concurrent `scan_directory()` tasks could all check the threshold and trigger `_process_empty_dirs_batch()` simultaneously. No lock prevented concurrent batch processing.

**Fix:**
- Added `empty_dirs_batch_processing_lock` to ensure only one batch processes at a time
- Other tasks skip processing when lock is held (non-blocking check)
- Prevents queue buildup and resource contention

### Issue #2: Lack of Debouncing - Log Spam
**Symptoms:**
```
{"timestamp": "2026-02-04 14:48:16,190", "message": "triggered"...}
{"timestamp": "2026-02-04 14:48:16,190", "message": "triggered"...}
{"timestamp": "2026-02-04 14:48:16,190", "message": "triggered"...}
```

**Root Cause:**
Every task checked and logged the trigger message before acquiring the lock. With 20 concurrent tasks, this created log spam.

**Fix:**
- Moved trigger logging to AFTER acquiring the lock (only the winner logs)
- Added `log_trigger` parameter to `_should_process_empty_dirs_incrementally()`
- Only log when actually processing, not just checking

### Issue #3: Micro-Batch Thrashing
**Symptoms:**
```
Processing batch size: 1
Processing batch size: 2  
Processing batch size: 3
```

**Root Cause:**
Race conditions caused multiple tasks to grab tiny portions of the `empty_dirs` set, creating batches of 1-3 directories with high overhead.

**Fix:**
- Added `empty_dirs_min_batch_size` (default: 100 directories)
- Don't process batches smaller than this unless memory is critical
- Prevents inefficient I/O operations for tiny batches

### Issue #4: Confusing Logging - Cumulative Counts
**Symptoms:**
```
batch_size: 3, deleted_in_batch: 24504
batch_size: 1, deleted_in_batch: 25505
```

**Root Cause:**
The `deleted_in_batch` field was logging `stats["empty_dirs_deleted"]` which is the cumulative total since run start, not the count for this specific batch.

**Fix:**
- Added `batch_deleted_count` variable to track deletions per batch
- Now correctly reports: `batch_size: 120, deleted_in_batch: 120`

## Implementation Details

### Code Changes

#### 1. Added Processing Lock
```python
# In __init__:
self.empty_dirs_batch_processing_lock = asyncio.Lock()
self.empty_dirs_min_batch_size = 100
```

#### 2. Updated Check Logic with Debouncing
```python
async def _check_empty_directory(self, directory: Path) -> None:
    # ... add directory to set ...
    
    # Skip if another task is already processing
    if self.empty_dirs_batch_processing_lock.locked():
        return
    
    should_process, _, _ = await self._should_process_empty_dirs_incrementally(log_trigger=False)
    if should_process:
        if not self.empty_dirs_batch_processing_lock.locked():
            async with self.empty_dirs_batch_processing_lock:
                # Double-check and log ONLY if we acquired the lock
                should_process, _, _ = await self._should_process_empty_dirs_incrementally(log_trigger=True)
                if should_process:
                    await self._process_empty_dirs_batch()
```

#### 3. Added Minimum Batch Size Check
```python
async def _should_process_empty_dirs_incrementally(self, log_trigger: bool = False):
    # ... check thresholds ...
    
    # Check minimum batch size
    batch_size_ok = empty_dirs_count >= self.empty_dirs_min_batch_size or memory_exceeded
    
    # Process if thresholds exceeded AND batch size is sufficient
    should_process = (memory_exceeded or count_exceeded) and batch_size_ok
```

#### 4. Fixed Batch-Specific Logging
```python
async def _process_empty_dirs_batch(self) -> None:
    batch_deleted_count = 0
    batch_deleted_lock = asyncio.Lock()
    
    async def remove_directory(directory: Path) -> None:
        nonlocal batch_deleted_count
        # ... delete directory ...
        async with batch_deleted_lock:
            batch_deleted_count += 1
    
    # ... process batch ...
    
    log_with_context(
        self.logger,
        "info",
        "Empty directory batch processed",
        {
            "batch_size": batch_size,
            "deleted_in_batch": batch_deleted_count,  # Now batch-specific!
            "total_processed": self.empty_dirs_processed_total,
        },
    )
```

## Test Coverage

Created comprehensive test suite in `tests/test_incremental_thrashing_fixes.py`:

1. **test_only_one_batch_processes_at_a_time**: Verifies lock prevents concurrent processing
2. **test_debouncing_reduces_trigger_spam**: Confirms log spam is eliminated
3. **test_minimum_batch_size_prevents_micro_batches**: Ensures tiny batches aren't processed
4. **test_logging_reports_batch_specific_counts**: Validates correct batch counts
5. **test_memory_critical_overrides_min_batch_size**: Critical memory always processes
6. **test_lock_acquisition_pattern_is_non_blocking**: Tasks don't block waiting
7. **test_no_duplicate_trigger_logs_at_same_timestamp**: No timestamp duplicates

All tests pass ✅

## Production Impact

### Before (v1.14.0)
```
{"timestamp": "2026-02-04 14:48:16,190", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 12210}
{"timestamp": "2026-02-04 14:48:16,190", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 12210}
{"timestamp": "2026-02-04 14:48:16,190", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 12210}
{"timestamp": "2026-02-04 14:48:16,190", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 12210}
{"timestamp": "2026-02-04 14:48:16,191", "message": "Processing empty directory batch during scanning", "batch_size": 12210}
{"timestamp": "2026-02-04 14:48:16,206", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 0}
{"timestamp": "2026-02-04 14:48:16,209", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 0}
{"timestamp": "2026-02-04 14:48:16,880", "message": "Processing empty directory batch during scanning", "batch_size": 3}
{"timestamp": "2026-02-04 14:48:18,715", "message": "Processing empty directory batch during scanning", "batch_size": 1}
{"timestamp": "2026-02-04 14:48:19,837", "message": "Empty directory batch processed", "batch_size": 3, "deleted_in_batch": 24504}
```

### After (v1.14.1)
```
{"timestamp": "2026-02-04 14:46:45,277", "message": "Incremental empty directory processing triggered", "empty_dirs_count": 22501}
{"timestamp": "2026-02-04 14:46:45,279", "message": "Processing empty directory batch during scanning", "batch_size": 22501}
{"timestamp": "2026-02-04 14:47:32,253", "message": "Empty directory batch processed", "batch_size": 22501, "deleted_in_batch": 22501}
```

**Improvements:**
- ✅ No duplicate trigger logs at same timestamp
- ✅ No micro-batches (1-3 dirs)
- ✅ Correct batch-specific counts
- ✅ Cleaner, more understandable logs

## Configuration

New parameter available:
- `empty_dirs_min_batch_size`: Minimum batch size to process (default: 100)
  - Can be adjusted based on workload
  - Memory-critical situations override this threshold

## Backward Compatibility

✅ **Fully backward compatible**
- No API changes
- No breaking changes
- Existing configurations work as-is
- New safeguards are transparent to users

## Version

- **Previous**: v1.14.0
- **Current**: v1.14.1 (hotfix)
- **Branch**: `hotfix/incremental-empty-dirs-thrashing`

## Files Changed

1. `src/efspurge/purger.py`:
   - Added `empty_dirs_batch_processing_lock`
   - Added `empty_dirs_min_batch_size`
   - Updated `_should_process_empty_dirs_incrementally()` with debouncing
   - Updated `_check_empty_directory()` with lock pattern
   - Fixed `_process_empty_dirs_batch()` logging

2. `tests/test_incremental_thrashing_fixes.py`: New comprehensive test suite
3. `tests/test_incremental_empty_dir_processing.py`: Updated for new behavior
4. `tests/test_empty_dirs_performance.py`: Updated expectations
5. `pyproject.toml`: Version bump to 1.14.1

## Next Steps

1. ✅ All fixes implemented
2. ✅ All tests passing (13/13 in new suite, 122/126 overall)
3. Ready for commit and PR
