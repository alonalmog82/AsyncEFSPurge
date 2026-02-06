# Batch Processing Performance Fix

## Problem

After implementing the critical memory threshold fix in v1.14.2, we observed that the tool was still producing rapid-fire small batch processing logs and high memory usage. Analysis of the logs revealed:

```
2026-02-04 06:14:08 - Processing 17739 empty directories (batch 1, memory: 592.3/800.0 MB [74.0%])...
# ... 59 seconds later ...
2026-02-04 06:15:07 - Incremental processing complete: deleted 17739 dirs in 59.02s (300.7/s)
# Memory climbed to 753.0 MB [94.1%] during this time
2026-02-04 06:15:08 - Processing 7 empty directories (batch 2, memory: 753.0/800.0 MB [94.1%])...
# ... many small batches followed ...
```

### Root Cause

The v1.14.2 fix (critical memory threshold) was working correctly - it prevented small batches when memory was below 90%. However, the **real problem** was that processing the first large batch (17,739 directories) took too long (59 seconds), allowing memory to continue climbing from 74% to 94% **during** the batch deletion itself.

Once memory reached 94% (above the 90% critical threshold), the system correctly entered "emergency mode" and allowed small batches, leading to the log spam.

The bottleneck was in `_process_empty_dirs_batch()`:

```python
# OLD CODE - Sequential sub-batching
tasks = [remove_directory(d) for d in sorted_batch]

# Process in smaller sub-batches to avoid memory spike
sub_batch_size = min(1000, self.max_concurrency_deletion * 2)
for i in range(0, len(tasks), sub_batch_size):
    sub_batch = tasks[i : i + sub_batch_size]
    await asyncio.gather(*sub_batch, return_exceptions=True)
```

This code:
1. Created all deletion tasks upfront (17,739 tasks)
2. Processed them in sequential sub-batches of 1000
3. With `max_concurrency_deletion=4000`, this artificial 1000-task limit prevented full utilization
4. Sequential processing meant: batch 1 (1000 tasks) → wait → batch 2 (1000 tasks) → wait → ...
5. This took 59 seconds for 17,739 directories (300.7 dirs/sec)

## Solution

Remove the sequential sub-batching and rely on the `deletion_semaphore` to naturally limit concurrency:

```python
# NEW CODE - Process all at once, semaphore controls concurrency
tasks = [remove_directory(d) for d in sorted_batch]
await asyncio.gather(*tasks, return_exceptions=True)
```

### Why This Works

1. **Semaphore-Based Concurrency Control**: Each `remove_directory()` call acquires the `deletion_semaphore` before executing, naturally limiting active concurrent operations to `max_concurrency_deletion`

2. **Non-Blocking Queue**: All tasks are created and added to asyncio's event loop, but only `max_concurrency_deletion` tasks execute at once. As tasks complete, new ones start immediately without artificial delays

3. **Faster Processing**: With `max_concurrency_deletion=4000`, we can now fully utilize this concurrency instead of the artificial 1000-task sub-batch limit

4. **Lower Memory Pressure**: Faster processing means less time for memory to accumulate during batch deletion, keeping it below the critical 90% threshold

### Expected Results

- **Before**: 17,739 dirs in 59 seconds (300.7 dirs/sec) → memory climbs to 94%
- **After**: Much faster processing (potentially 4x faster with full concurrency) → memory stays lower → no emergency mode → no log spam

## Changes Made

### 1. Code Changes

**File**: `src/efspurge/purger.py`
**Function**: `_process_empty_dirs_batch()`
**Lines**: 887-895

Removed sequential sub-batching logic and replaced with direct `asyncio.gather()` on all tasks.

### 2. Testing

All 128 tests pass, including:
- Memory safety tests (`test_empty_dirs_memory.py`)
- Incremental processing tests (`test_incremental_empty_dir_processing.py`)
- Concurrent deletion tests (`test_concurrent_empty_dir_removal.py`)
- Performance tests (`test_empty_dirs_performance.py`)

No new tests were needed because:
1. The change is internal to `_process_empty_dirs_batch()`
2. Existing tests already verify correct behavior (all dirs deleted, memory bounded, proper error handling)
3. The semaphore-based concurrency control was already tested

### 3. Documentation

This document serves as the primary documentation for the fix.

## Version

- **Previous**: v1.14.2 (critical memory threshold fix)
- **Current**: v1.14.3 (batch processing performance fix)

## Technical Details

### Semaphore Implementation

The `deletion_semaphore` is initialized in `__init__`:

```python
self.deletion_semaphore = asyncio.Semaphore(self.max_concurrency_deletion)
```

And each directory removal acquires it:

```python
async def remove_directory(directory: Path) -> None:
    async with self.deletion_semaphore:
        # ... actual deletion logic ...
```

This ensures that no more than `max_concurrency_deletion` deletions happen concurrently, regardless of how many tasks are created.

### Memory Safety

Memory safety is maintained through:

1. **Incremental Processing**: Large batches are detected and processed during scanning (prevents accumulating 100k+ dirs in memory)
2. **Critical Memory Threshold**: At 90%+ memory, allows smaller batches to be processed immediately
3. **Fast Batch Processing**: This fix ensures batches complete quickly, reducing time for memory accumulation
4. **Deletion Semaphore**: Limits concurrent operations to prevent memory spikes from too many simultaneous tasks

### Performance Characteristics

With the old sequential sub-batching:
- Effective concurrency: min(1000, max_concurrency_deletion * 2) = 1000 (for default settings)
- Processing pattern: Sequential waves of 1000 tasks
- Throughput: Limited by sub-batch size

With the new approach:
- Effective concurrency: max_concurrency_deletion (e.g., 4000)
- Processing pattern: Continuous flow as tasks complete
- Throughput: Full utilization of configured concurrency

## Related Issues

- v1.14.1: Initial incremental processing implementation
- v1.14.2: Critical memory threshold fix (90% threshold for bypassing min_batch_size)
- v1.14.3: This performance fix (remove sequential sub-batching)

## Testing Notes

The fix was validated by:
1. Running all 128 tests successfully
2. Verifying memory safety tests pass (bounded memory growth)
3. Confirming concurrent deletion tests pass (proper semaphore usage)
4. Checking that performance tests don't timeout (faster processing)

Real-world validation will come from production logs showing:
- Faster batch processing times
- Memory staying below 90% during batch deletion
- Fewer/no emergency small batches
- Overall improved throughput
